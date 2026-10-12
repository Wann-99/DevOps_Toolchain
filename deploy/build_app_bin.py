#!/usr/bin/env python3
"""Build the application as one portable executable zip archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PROJECT_DIRECTORY = SCRIPT_DIRECTORY.parent
DEFAULT_OUTPUT_DIRECTORY = SCRIPT_DIRECTORY / "dist"
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})
EXCLUDED_NAMES = frozenset({".DS_Store"})
REQUIRED_ARCHIVE_FILES = frozenset(
    {
        "__main__.py",
        "ksq/__init__.py",
        "ksq/cli.py",
        "ksq/config_pnp.py",
        "ksq/web/templates/shell.html",
        "ksq/web/static/app.css",
    }
)

BOOTSTRAP = r'''#!/usr/bin/env python3
"""Extract and run the packaged knowledge_shelf_query application."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


def _archive_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_members(archive: zipfile.ZipFile) -> None:
    for info in archive.infolist():
        member = Path(info.filename)
        if member.is_absolute() or ".." in member.parts:
            raise RuntimeError("应用包包含不安全路径：" + info.filename)


def _extract_application(archive_path: Path) -> Path:
    digest = _archive_digest(archive_path)
    cache_root = Path(
        os.environ.get("KSQ_BIN_CACHE_DIRECTORY", "/tmp/knowledge_shelf_query_bin")
    ).expanduser()
    target = cache_root / digest[:20]
    marker = target / ".ready"
    if marker.is_file():
        return target
    if target.exists():
        shutil.rmtree(target)

    cache_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".extract-", dir=str(cache_root)))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            _validate_members(archive)
            archive.extractall(temporary)
        (temporary / ".ready").write_text(digest + "\n", encoding="ascii")
        try:
            temporary.replace(target)
        except OSError:
            if not marker.is_file():
                raise
            shutil.rmtree(temporary, ignore_errors=True)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def main() -> None:
    archive_path = Path(sys.argv[0]).resolve()
    application_directory = _extract_application(archive_path)
    sys.path.insert(0, str(application_directory))

    # The package manifest is the single version source for the running UI.
    manifest = json.loads(
        (application_directory / "KSQ_BUILD.json").read_text(encoding="utf-8")
    )
    version = str(manifest.get("version") or "").strip()
    if not version:
        raise RuntimeError("应用包版本为空。")
    os.environ["KSQ_APP_VERSION"] = version

    from ksq.runtime_logging import configure as configure_runtime_logging
    from ksq.runtime_logging import get_logger

    configure_runtime_logging()
    try:
        from ksq.cli import main as application_main
    except BaseException:
        get_logger("bootstrap").exception("服务启动失败")
        raise

    application_main()


if __name__ == "__main__":
    main()
'''


def iter_application_files() -> Iterable[Path]:
    for path in sorted((PROJECT_DIRECTORY / "ksq").rglob("*")):
        if not path.is_file():
            continue
        if (
            "__pycache__" in path.parts
            or path.suffix in EXCLUDED_SUFFIXES
            or path.name in EXCLUDED_NAMES
        ):
            continue
        yield path


def archive_name(path: Path) -> str:
    return path.relative_to(PROJECT_DIRECTORY).as_posix()


def write_bytes(
    archive: zipfile.ZipFile,
    name: str,
    payload: bytes,
    *,
    executable: bool = False,
) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, payload)


def build(output: Path, version: str) -> tuple[int, str]:
    files = list(iter_application_files())
    if not files:
        raise RuntimeError("未找到可打包的应用文件。")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    manifest = {
        "format": 1,
        "name": "knowledge_shelf_query",
        "version": version,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "builder_python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "source_file_count": len(files),
    }
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            write_bytes(
                archive,
                "__main__.py",
                BOOTSTRAP.encode("utf-8"),
                executable=True,
            )
            write_bytes(
                archive,
                "KSQ_BUILD.json",
                (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                ),
            )
            for path in files:
                write_bytes(archive, archive_name(path), path.read_bytes())
        validate(temporary)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.chmod(output.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return len(files), digest


def validate(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"应用包不是有效 ZIP：{path}")
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise RuntimeError(f"应用包文件损坏：{bad_member}")
        names = set(archive.namelist())
    missing = sorted(REQUIRED_ARCHIVE_FILES - names)
    if missing:
        raise RuntimeError("应用包缺少文件：" + "、".join(missing))


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 knowledge_shelf_query 单文件应用包")
    parser.add_argument("version", help="应用版本，例如 v1.2.5")
    parser.add_argument(
        "--output",
        type=Path,
        help="输出路径；默认 deploy/dist/knowledge_shelf_query_<version>.bin",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    version = str(arguments.version).strip()
    if not version:
        raise ValueError("版本号不能为空。")
    output = arguments.output or (
        DEFAULT_OUTPUT_DIRECTORY / f"knowledge_shelf_query_{version}.bin"
    )
    output = output.expanduser().resolve()
    count, digest = build(output, version)
    print(f"应用包：{output}")
    print(f"版本：  {version}")
    print(f"文件数：{count}")
    print(f"大小：  {output.stat().st_size / 1024:.1f} KiB")
    print(f"SHA256：{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
