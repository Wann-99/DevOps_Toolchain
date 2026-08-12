"""Backup-then-write helpers with verified, durable writes.

All persistent writes go through :func:`safe_write_bytes` so that every write
follows the same contract:

1. 备份原文件（备份文件的 mtime 为写入时刻，不是原文件的旧时间）。
2. 原子替换写入；绑定挂载的单文件无法 rename 时退回原地写。
3. fsync 落盘，再回读校验内容一致。
4. 按保留策略清理旧备份（始终保留最早的一份原始版本）。
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set, Tuple

# 历史上存在两种备份命名：`.bak<stamp>`（编辑落盘）与 `.bak.<stamp>`（导入）。
# 两种都要能被识别，否则清理时会退回 mtime 判断而误删刚建好的备份。
BACKUP_STAMP_RE = re.compile(r"\.bak\.?(\d{8}_\d{6})(?:_(\d+))?$")
_STAMP_FORMAT = "%Y%m%d_%H%M%S"


def backup_timestamp(backup_path: Path) -> Optional[datetime]:
    match = BACKUP_STAMP_RE.search(backup_path.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), _STAMP_FORMAT)
    except ValueError:
        return None


def _sort_key(backup_path: Path) -> Tuple[datetime, int, str]:
    stamp = backup_timestamp(backup_path)
    if stamp is None:
        try:
            stamp = datetime.fromtimestamp(backup_path.stat().st_mtime)
        except OSError:
            stamp = datetime.min
    match = BACKUP_STAMP_RE.search(backup_path.name)
    counter = int(match.group(2)) if match and match.group(2) else 0
    return stamp, counter, backup_path.name


def iter_backup_files(path: Path) -> List[Path]:
    """Backups of ``path``, oldest first.

    Sorted by parsed timestamp rather than file name: the two naming schemes
    do not sort consistently as plain strings ('.' < digits).
    """
    prefix = f"{path.name}.bak"
    try:
        candidates = [
            item
            for item in path.parent.iterdir()
            if item.is_file() and item.name.startswith(prefix)
        ]
    except OSError:
        return []
    return sorted(candidates, key=_sort_key)


def cleanup_backups(
    path: Path,
    keep_latest: Optional[int] = None,
    keep_days: Optional[int] = None,
    keep_oldest: int = 1,
) -> List[str]:
    """Delete backups outside the retention policy.

    ``keep_oldest`` protects the earliest backups unconditionally — those hold
    the pre-edit original. Without it, repeated saves rotate the pristine copy
    out and leave only already-modified content.
    """
    backups = iter_backup_files(path)
    if not backups:
        return []
    retain: Set[Path] = set()
    if keep_oldest > 0:
        retain.update(backups[:keep_oldest])
    if keep_latest is not None:
        if keep_latest < 0:
            raise ValueError("keep_latest 不能为负数。")
        if keep_latest > 0:
            retain.update(backups[-keep_latest:])
    if keep_days is not None:
        if keep_days < 0:
            raise ValueError("keep_days 不能为负数。")
        cutoff = datetime.now() - timedelta(days=keep_days)
        for backup in backups:
            stamp, _, _ = _sort_key(backup)
            if stamp >= cutoff:
                retain.add(backup)
    removed: List[str] = []
    for backup in backups:
        if backup in retain:
            continue
        try:
            backup.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise OSError(f"清理备份失败：{backup}，原因：{error}") from error
        removed.append(str(backup))
    return removed


def _unique_backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime(_STAMP_FORMAT)
    backup_path = path.with_name(f"{path.name}.bak{stamp}")
    counter = 1
    while backup_path.exists():
        backup_path = path.with_name(f"{path.name}.bak{stamp}_{counter}")
        counter += 1
    return backup_path


def backup_file(
    path: Path,
    keep_latest: Optional[int] = None,
    keep_days: Optional[int] = None,
    keep_oldest: int = 1,
    skip_if_unchanged: bool = True,
) -> Optional[Path]:
    """Copy ``path`` aside before it is overwritten.

    Returns the backup path, or ``None`` when the file does not exist yet or
    an identical backup already exists.
    """
    if not path.is_file():
        return None
    try:
        current = path.read_bytes()
    except OSError as error:
        raise OSError(f"读取待备份文件失败：{path}，原因：{error}") from error

    existing = iter_backup_files(path)
    if skip_if_unchanged and existing:
        try:
            if existing[-1].read_bytes() == current:
                return existing[-1]
        except OSError:
            pass

    backup_path = _unique_backup_path(path)
    try:
        shutil.copy2(path, backup_path)
        # copy2 preserves the source mtime, so a fresh backup would carry the
        # original's old date — misleading in `ls`, and it makes keep_days
        # treat a just-created backup as already expired.
        os.utime(backup_path, None)
    except OSError as error:
        raise OSError(f"备份失败：{path} -> {backup_path}，原因：{error}") from error
    cleanup_backups(
        path,
        keep_latest=keep_latest,
        keep_days=keep_days,
        keep_oldest=keep_oldest,
    )
    return backup_path


def _fsync_path(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_in_place(path: Path, payload: bytes) -> None:
    with path.open("wb") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())


def _write_via_temporary(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        with temporary.open("wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    _fsync_path(path.parent)


def write_bytes_durably(path: Path, payload: bytes) -> str:
    """Write ``payload`` to ``path``, returning the strategy actually used.

    Prefers write-temp-then-rename. A bind-mounted single file cannot be
    replaced by rename (EBUSY), which is the normal deployment layout here, so
    that case falls back to writing in place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_via_temporary(path, payload)
        return "atomic"
    except OSError as error:
        if error.errno not in {getattr(os, "EBUSY", 16), 16, 18, 26}:
            raise OSError(f"写入失败：{path}，原因：{error}") from error
    try:
        _write_in_place(path, payload)
    except OSError as error:
        raise OSError(f"写入失败：{path}，原因：{error}") from error
    return "in_place"


def verify_written(path: Path, payload: bytes) -> None:
    try:
        actual = path.read_bytes()
    except OSError as error:
        raise OSError(f"写入校验读取失败：{path}，原因：{error}") from error
    if actual != payload:
        raise OSError(
            f"写入校验失败：{path}，期望 {len(payload)} 字节，实际 {len(actual)} 字节。"
        )


def safe_write_bytes(
    path: Path,
    payload: bytes,
    keep_latest: Optional[int] = None,
    keep_days: Optional[int] = None,
    keep_oldest: int = 1,
    backup: bool = True,
) -> Optional[Path]:
    """Backup, write durably, then verify. Returns the backup path if made."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = (
        backup_file(
            path,
            keep_latest=keep_latest,
            keep_days=keep_days,
            keep_oldest=keep_oldest,
        )
        if backup
        else None
    )
    write_bytes_durably(path, payload)
    verify_written(path, payload)
    return backup_path


def safe_write_text(
    path: Path,
    text: str,
    encoding: str = "utf-8",
    keep_latest: Optional[int] = None,
    keep_days: Optional[int] = None,
    keep_oldest: int = 1,
    backup: bool = True,
) -> Optional[Path]:
    return safe_write_bytes(
        path,
        text.encode(encoding),
        keep_latest=keep_latest,
        keep_days=keep_days,
        keep_oldest=keep_oldest,
        backup=backup,
    )
