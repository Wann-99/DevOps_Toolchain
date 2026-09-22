from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ksq.data import imports, state, storage


def upload(name: str, payload: bytes) -> SimpleNamespace:
    return SimpleNamespace(filename=name, file=io.BytesIO(payload))


class KnowledgeImportFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.knowledge = self.root / "source" / "knowledge"
        self.knowledge.mkdir(parents=True)
        (self.knowledge / "old.json").write_text('{"id":"old"}', encoding="utf-8")
        self.shelves = self.root / "source" / "sku-shelves.csv"
        self.shelves.write_bytes(self.csv("old"))
        self.current = self.root / "data" / "current"
        self.backups = self.root / "data" / "backups"
        for module, values in (
            (storage, {
                "DATA_DIRECTORY": self.current.parent,
                "CURRENT_DIRECTORY": self.current,
                "BACKUP_DIRECTORY": self.backups,
            }),
            (state, {
                "configured_knowledge": self.knowledge,
                "configured_knowledge_root": self.knowledge.parent,
                "configured_shelves": self.shelves,
                "configured_unavailable": None,
                "configured_tool_mapping": None,
                "configured_pick_strategy": None,
                "_explicit_config_keys": frozenset(),
                "loaded_dataset": None,
                "loaded_tool_mapping": None,
                "loaded_closed_loop_ids": None,
                "loaded_unavailable_ids": None,
                "loaded_paths": {},
                "data_source_ready": False,
                "data_load_method": "none",
                "edit_workspace": None,
                "data_revision": 0,
                "shelves_source": "local",
            }),
        ):
            patched = patch.multiple(module, **values)
            patched.start()
            self.addCleanup(patched.stop)

    @staticmethod
    def csv(item_id: str) -> bytes:
        return f"sku_code,name,shelf_number,level,bin_unit\n{item_id},Drug,1,1,1\n".encode()

    def run_import(self, *files: SimpleNamespace) -> dict:
        return imports.import_uploaded_files({"files": list(files)})

    def current_files(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.current)): path.read_bytes()
            for path in self.current.rglob("*") if path.is_file()
        }

    def test_mixed_zip_and_file_import_reports_helpers_and_preserves_sources(self) -> None:
        (self.knowledge / "fixture.json").write_text('{"width":10}', encoding="utf-8")
        (self.knowledge / "block.json").write_text("not json", encoding="utf-8")
        source = {path: path.read_bytes() for path in self.knowledge.iterdir()}
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("knowledge/new.json", '{"id":"new"}')
            archive.writestr("knowledge/layout.json", '{"shelves":[]}')
            archive.writestr("knowledge/vector.json", "[1,2,3]")
            archive.writestr("knowledge/BOOKSHELF.JSON", "not json")
        result = self.run_import(
            upload("bundle.zip", payload.getvalue()),
            upload("settings.json", b"{}"),
            upload("bottle cap.json", b"not json"),
        )
        self.assertTrue(result["reloaded"])
        self.assertEqual(result["knowledge_files"], 1)
        self.assertEqual([item["source"] for item in result["written"]], ["new.json"])
        ignored = {"layout.json", "vector.json", "BOOKSHELF.JSON", "settings.json", "bottle cap.json", "fixture.json", "block.json"}
        self.assertEqual(set(result["ignored_files"]), ignored)
        self.assertEqual(set(state.loaded_dataset.report.ignored_knowledge_files), ignored)
        self.assertEqual({path.name for path in (self.current / "knowledge").iterdir()}, {"old.json", "new.json"})
        self.assertEqual({path: path.read_bytes() for path in source}, source)

    def test_only_helpers_do_not_publish_or_change_loaded_state(self) -> None:
        self.run_import(upload("new.json", b'{"id":"new"}'))
        before = self.current_files()
        dataset = state.loaded_dataset
        revision = state.data_revision
        paths = dict(state.loaded_paths)
        with patch.object(storage, "publish_dataset", side_effect=AssertionError("must not publish")):
            for files in (
                [upload("settings.json", b"{}"), upload("array.json", b"[]")],
                [upload("block.json", b"not json")],
            ):
                result = self.run_import(*files)
                self.assertEqual(result["written"], [])
                self.assertEqual(result["knowledge_files"], 0)
                self.assertEqual(result["backup_count"], 0)
                self.assertFalse(result["reloaded"])
                self.assertEqual(set(result["ignored_files"]), {item.filename for item in files})
        self.assertIs(state.loaded_dataset, dataset)
        self.assertEqual(state.data_revision, revision)
        self.assertEqual(state.loaded_paths, paths)
        self.assertEqual(self.current_files(), before)
        self.assertFalse(self.backups.exists())

    def test_sku_without_id_uses_uploaded_csv_even_when_csv_is_last(self) -> None:
        for csv_first in (False, True):
            with self.subTest(csv_first=csv_first):
                files = [upload("new-sku.json", b"{}"), upload("sku-shelves.csv", self.csv("new-sku"))]
                if csv_first:
                    files.reverse()
                with self.assertRaisesRegex(ValueError, "缺少有效的 id 字段：new-sku.json"):
                    self.run_import(*files)
                self.assertFalse(self.current.exists())

    def test_configured_sku_and_loaded_dictionary_keep_strict_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "缺少有效的 id 字段：old.json"):
            self.run_import(upload("old.json", b"{}"))
        self.run_import(upload("off-shelf.json", b'{"id":"off-shelf"}'))
        before = self.current_files()
        for name, value in (("old.json", {}), ("off-shelf.json", {}), ("drug.json", {"包装类型": "纸盒"}), ("drug.json", {"id": False})):
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ValueError, "缺少有效的 id 字段"):
                    self.run_import(upload(name, json.dumps(value).encode()))
                self.assertEqual(self.current_files(), before)
        with self.assertRaisesRegex(ValueError, "JSON 根节点必须是对象"):
            self.run_import(upload("off-shelf.json", b"[]"))

    def test_uploaded_csv_takes_priority_over_previous_shelf_ids(self) -> None:
        result = self.run_import(
            upload("old.json", b"{}"),
            upload("sku-shelves.csv", self.csv("new-sku")),
        )
        self.assertTrue(result["reloaded"])
        self.assertEqual(result["ignored_files"], ["old.json"])
        self.assertEqual(set(state.loaded_dataset.shelf_entries), {"new-sku"})
        self.assertEqual((self.current / "knowledge" / "old.json").read_bytes(), b'{"id":"old"}')

    def test_incomplete_import_filters_base_before_publishing(self) -> None:
        state.configured_shelves = self.root / "missing.csv"
        helper = self.knowledge / "fixture.json"
        helper.write_bytes(b'{"width":10}')
        result = self.run_import(upload("new.json", b'{"id":"new"}'), upload("settings.json", b"{}"))
        self.assertFalse(result["reloaded"])
        self.assertEqual(set(result["ignored_files"]), {"fixture.json", "settings.json"})
        self.assertEqual({path.name for path in (self.current / "knowledge").iterdir()}, {"old.json", "new.json"})
        self.assertEqual(helper.read_bytes(), b'{"width":10}')

    def test_incomplete_import_rejects_invalid_base_id(self) -> None:
        state.configured_shelves = self.root / "missing.csv"
        (self.knowledge / "old.json").write_bytes(b'{"id":null}')
        with self.assertRaisesRegex(ValueError, "缺少有效的 id 字段：old.json"):
            self.run_import(upload("new.json", b'{"id":"new"}'))
        self.assertFalse(self.current.exists())
        self.assertEqual(state.loaded_paths, {})

    def test_invalid_json_batch_rolls_back_and_valid_upload_can_replace_bad_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON 文件格式错误：bad.json"):
            self.run_import(upload("new.json", b'{"id":"new"}'), upload("bad.json", b"{bad"))
        self.assertFalse(self.current.exists())
        (self.knowledge / "old.json").write_bytes(b"{bad")
        result = self.run_import(upload("old.json", b'{"id":"old"}'))
        self.assertTrue(result["reloaded"])
        self.assertEqual((self.current / "knowledge" / "old.json").read_bytes(), b'{"id":"old"}')
        self.assertEqual((self.knowledge / "old.json").read_bytes(), b"{bad")


if __name__ == "__main__":
    unittest.main()
