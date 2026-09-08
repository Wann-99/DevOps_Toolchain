from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ksq.web import data_storage


class DataStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.current = self.data / "current"
        self.backups = self.data / "backups"
        paths = patch.multiple(
            data_storage,
            DATA_DIRECTORY=self.data,
            CURRENT_DIRECTORY=self.current,
            BACKUP_DIRECTORY=self.backups,
        )
        paths.start()
        self.addCleanup(paths.stop)
        self.addCleanup(data_storage.stop_data_cleanup)
        self.knowledge = self.root / "source" / "knowledge"
        self.knowledge.mkdir(parents=True)
        (self.knowledge / "1.json").write_text('{"id": "1"}', encoding="utf-8")
        for ignored in (".hidden.json", "1.bak.json", "readme.txt"):
            (self.knowledge / ignored).write_text("ignored", encoding="utf-8")
        self.shelves = self.root / "source" / "etm_sku_locations_cache.csv"
        self.shelves.write_text("sku_code,location\n1,A01\n", encoding="utf-8")

    def _publish(self, value: str) -> None:
        with data_storage.staged_dataset() as staging:
            (staging / "value.txt").write_text(value, encoding="utf-8")
            with data_storage.publish_dataset(staging) as current:
                self.assertEqual(current, self.current)

    def test_copy_publish_reload_preserves_sources_and_all_same_day_backups(self) -> None:
        optional = self.root / "source" / "optional.json"
        optional.write_text("{}", encoding="utf-8")
        sources = {
            path: path.read_bytes()
            for path in (self.root / "source").rglob("*")
            if path.is_file()
        }
        progress = []
        with data_storage.staged_dataset() as staging:
            paths = data_storage.copy_dataset_files(
                staging,
                self.knowledge,
                self.shelves,
                optional,
                optional,
                optional,
                lambda done, total: progress.append((done, total)),
            )
            self.assertEqual([path.name for path in paths.knowledge_directory.iterdir()], ["1.json"])
            self.assertEqual(paths.shelves_file.read_bytes(), self.shelves.read_bytes())
            self.assertEqual(paths.unavailable_file.name, "unavailable_obj.json")
            self.assertEqual(paths.tool_mapping_file.name, "obj_tool_mapping.json")
            self.assertEqual(paths.pick_strategy_file.name, "pick_strategy_obj.json")
            with data_storage.publish_dataset(staging):
                self.assertFalse(staging.exists())
        self.assertEqual(progress, [(count, 5) for count in range(1, 6)])
        self._publish("second")
        self._publish("third")
        self.assertEqual(data_storage.cleanup_backups(), [])
        backups = sorted(self.backups.iterdir())
        self.assertEqual(len(backups), 2)
        self.assertEqual((backups[0] / "knowledge" / "1.json").read_bytes(), sources[self.knowledge / "1.json"])
        self.assertEqual((backups[1] / "value.txt").read_text(), "second")
        self.assertEqual((self.current / "value.txt").read_text(), "third")
        self.assertEqual({path: path.read_bytes() for path in sources}, sources)

    def test_validation_and_copy_failures_leave_current_untouched(self) -> None:
        self._publish("original")
        with self.assertRaisesRegex(ValueError, "invalid dataset"):
            with data_storage.staged_dataset() as staging:
                data_storage.copy_dataset_files(staging, self.knowledge, self.shelves)
                raise ValueError("invalid dataset")
        with self.assertRaises(FileNotFoundError):
            with data_storage.staged_dataset() as staging:
                data_storage.copy_dataset_files(staging, self.knowledge, self.root / "missing.csv")
        self.assertEqual((self.current / "value.txt").read_text(), "original")
        self.assertFalse(self.backups.exists())
        self.assertEqual(list(self.data.glob(".staging-*")), [])

    def test_install_and_publish_failures_restore_previous_current(self) -> None:
        self._publish("original")
        with self.assertRaisesRegex(RuntimeError, "install failed"):
            with data_storage.staged_dataset() as staging:
                (staging / "value.txt").write_text("replacement", encoding="utf-8")
                with data_storage.publish_dataset(staging):
                    raise RuntimeError("install failed")
        self.assertEqual((self.current / "value.txt").read_text(), "original")
        self.assertEqual(list(self.backups.iterdir()), [])
        rename = Path.rename
        with data_storage.staged_dataset() as staging:
            def fail_publish(path, target):
                if path == staging:
                    raise OSError("rename failed")
                return rename(path, target)

            with patch.object(Path, "rename", fail_publish):
                with self.assertRaisesRegex(OSError, "rename failed"):
                    with data_storage.publish_dataset(staging):
                        self.fail("Publishing should have failed")
        self.assertEqual((self.current / "value.txt").read_text(), "original")
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertEqual(list(self.data.glob(".staging-*")), [])

    def test_first_install_failure_leaves_no_current_dataset(self) -> None:
        with self.assertRaises(ValueError):
            with data_storage.staged_dataset() as staging:
                with data_storage.publish_dataset(staging):
                    raise ValueError("install failed")
        self.assertFalse(self.current.exists())
        self.assertEqual(list(self.data.iterdir()), [])

    def test_midnight_cleanup_retains_latest_and_ignores_unknown_entries(self) -> None:
        self._publish("current")
        self.backups.mkdir()
        names = ("20260906_080000_000000", "20260906_090000_000000", "20260907_100000_000000")
        for name in names:
            (self.backups / name).mkdir()
        unknown = self.backups / "manual-backup"
        unknown.mkdir()
        invalid = self.backups / "20269999_120000_000000"
        invalid.mkdir()
        symlink = self.backups / "20260905_080000_000000"
        symlink.symlink_to(self.knowledge, target_is_directory=True)
        removed = data_storage.cleanup_backups(datetime(2026, 9, 7, 12))
        self.assertEqual([path.name for path in removed], list(names[:2]))
        (self.backups / "20260907_110000_000000").mkdir()
        self.assertEqual(data_storage.cleanup_backups(datetime(2026, 9, 7, 23, 59)), [])
        removed = data_storage.cleanup_backups(datetime(2026, 9, 8))
        self.assertEqual([path.name for path in removed], [names[2]])
        self.assertTrue((self.backups / "20260907_110000_000000").is_dir())
        self.assertTrue(unknown.is_dir())
        self.assertTrue(invalid.is_dir())
        self.assertTrue(symlink.is_symlink())
        self.assertTrue((self.knowledge / "1.json").is_file())
        self.assertEqual((self.current / "value.txt").read_text(), "current")

    def test_rejects_external_staging_and_symlinked_backup_root(self) -> None:
        with self.assertRaises(ValueError):
            with data_storage.publish_dataset(self.knowledge):
                self.fail("External staging must not be published")
        self.backups.symlink_to(self.knowledge, target_is_directory=True)
        with self.assertRaises(ValueError):
            data_storage.cleanup_backups()
        self.assertTrue((self.knowledge / "1.json").is_file())

    def test_cleanup_starts_once_and_stops_without_waiting_for_midnight(self) -> None:
        cleaned = threading.Event()
        with patch.object(data_storage, "cleanup_backups", side_effect=lambda: cleaned.set() or []):
            data_storage.start_data_cleanup()
            self.assertTrue(cleaned.wait(3))
            thread = data_storage._CLEANUP_THREAD
            data_storage.start_data_cleanup()
            self.assertIs(data_storage._CLEANUP_THREAD, thread)
            data_storage.stop_data_cleanup()
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
