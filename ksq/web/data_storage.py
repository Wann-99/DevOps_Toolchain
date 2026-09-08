"""Managed copies of loaded datasets and daily backup retention."""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Optional

from ksq.constants import (
    APP_DIRECTORY,
    PICK_STRATEGY_FILE_NAME,
    SHELVES_FILE_NAME,
    TOOL_MAPPING_FILE_NAME,
)
from ksq.knowledge import list_knowledge_files
from ksq.models import BundlePaths


DATA_DIRECTORY = APP_DIRECTORY / "data"
CURRENT_DIRECTORY = DATA_DIRECTORY / "current"
BACKUP_DIRECTORY = DATA_DIRECTORY / "backups"
_BACKUP_NAME = re.compile(r"\d{8}_\d{6}_\d{6}\Z")
_BACKUP_FORMAT = "%Y%m%d_%H%M%S_%f"
_LOGGER = logging.getLogger(__name__)
# ponytail: one process owns data; add an interprocess lock if workers are added.
_DATA_LOCK = threading.Lock()
_CLEANUP_LOCK = threading.Lock()
_CLEANUP_THREAD: Optional[threading.Thread] = None
_CLEANUP_STOP: Optional[threading.Event] = None


def _ensure_directories() -> None:
    for directory in (DATA_DIRECTORY, CURRENT_DIRECTORY, BACKUP_DIRECTORY):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError(f"Invalid dataset directory: {directory}")
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)


@contextmanager
def staged_dataset() -> Iterator[Path]:
    """Create a private staging directory; discard it unless published."""
    with _DATA_LOCK:
        _ensure_directories()
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=DATA_DIRECTORY))
    try:
        yield staging
    finally:
        if staging.is_symlink():
            staging.unlink()
        elif staging.exists():
            shutil.rmtree(staging)


def copy_dataset_files(
    staging: Path,
    knowledge: Path,
    shelves: Path,
    unavailable: Optional[Path] = None,
    tool_mapping: Optional[Path] = None,
    pick_strategy: Optional[Path] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> BundlePaths:
    """Copy only files used by the loaders, preserving every source file."""
    knowledge_files, _ = list_knowledge_files(knowledge)
    knowledge_target = staging / "knowledge"
    config_target = staging / "config_pnp"
    knowledge_target.mkdir(parents=True, exist_ok=True)
    config_target.mkdir(parents=True, exist_ok=True)
    paths = BundlePaths(
        knowledge_target,
        config_target / SHELVES_FILE_NAME,
        config_target / "unavailable_obj.json" if unavailable is not None else None,
        config_target / TOOL_MAPPING_FILE_NAME if tool_mapping is not None else None,
        config_target / PICK_STRATEGY_FILE_NAME if pick_strategy is not None else None,
    )
    copies = [(path, knowledge_target / path.name) for path in knowledge_files]
    copies.extend(
        (source, target)
        for source, target in (
            (shelves, paths.shelves_file),
            (unavailable, paths.unavailable_file),
            (tool_mapping, paths.tool_mapping_file),
            (pick_strategy, paths.pick_strategy_file),
        )
        if source is not None and target is not None
    )
    for completed, (source, target) in enumerate(copies, 1):
        shutil.copy2(source, target)
        if progress is not None:
            progress(completed, len(copies))
    return paths


@contextmanager
def publish_dataset(staging: Path) -> Iterator[Path]:
    """Publish a validated copy and restore the prior dataset if install fails."""
    with _DATA_LOCK:
        _ensure_directories()
        if (
            staging.is_symlink()
            or not staging.is_dir()
            or staging.parent.resolve() != DATA_DIRECTORY.resolve()
            or not staging.name.startswith(".staging-")
        ):
            raise ValueError(f"Invalid dataset staging directory: {staging}")
        previous = None
        if CURRENT_DIRECTORY.exists():
            BACKUP_DIRECTORY.mkdir(exist_ok=True)
            stamp = datetime.now()
            previous = BACKUP_DIRECTORY / stamp.strftime(_BACKUP_FORMAT)
            while previous.exists() or previous.is_symlink():
                stamp += timedelta(microseconds=1)
                previous = BACKUP_DIRECTORY / stamp.strftime(_BACKUP_FORMAT)
            CURRENT_DIRECTORY.rename(previous)
        published = False
        try:
            staging.rename(CURRENT_DIRECTORY)
            published = True
            yield CURRENT_DIRECTORY
        except BaseException:
            if published:
                CURRENT_DIRECTORY.rename(staging)
            if previous is not None:
                previous.rename(CURRENT_DIRECTORY)
            raise


def cleanup_backups(now: Optional[datetime] = None) -> list[Path]:
    """Keep today's backups and the latest backup, ignoring unknown entries."""
    today = (now or datetime.now()).date()
    removed: list[Path] = []
    with _DATA_LOCK:
        _ensure_directories()
        if not BACKUP_DIRECTORY.exists():
            return removed
        backups = []
        for path in BACKUP_DIRECTORY.iterdir():
            if (
                path.is_symlink()
                or not path.is_dir()
                or not _BACKUP_NAME.fullmatch(path.name)
            ):
                continue
            try:
                stamp = datetime.strptime(path.name, _BACKUP_FORMAT)
            except ValueError:
                continue
            backups.append((stamp, path))
        backups.sort()
        for stamp, path in backups[:-1]:
            if stamp.date() < today:
                shutil.rmtree(path)
                removed.append(path)
    return removed


def _cleanup_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            removed = cleanup_backups()
            if removed:
                _LOGGER.info("Removed %s expired dataset backups", len(removed))
        except Exception:
            _LOGGER.exception("Dataset backup cleanup failed")
        now = datetime.now()
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        if stop.wait(max(1.0, midnight.timestamp() - now.timestamp())):
            break


def start_data_cleanup() -> None:
    """Reconcile expired backups on startup, then clean at local midnight."""
    global _CLEANUP_THREAD, _CLEANUP_STOP
    with _CLEANUP_LOCK:
        if _CLEANUP_THREAD is not None and _CLEANUP_THREAD.is_alive():
            return
        _CLEANUP_STOP = threading.Event()
        _CLEANUP_THREAD = threading.Thread(
            target=_cleanup_loop,
            args=(_CLEANUP_STOP,),
            name="dataset-backup-cleanup",
            daemon=True,
        )
        _CLEANUP_THREAD.start()


def stop_data_cleanup() -> None:
    with _CLEANUP_LOCK:
        thread = _CLEANUP_THREAD
        if _CLEANUP_STOP is not None:
            _CLEANUP_STOP.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=5.0)
