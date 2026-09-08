from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ksq.dataset import build_dataset
from ksq.web import edit_workspace, state, test_order_api


class ShelfEditingTests(unittest.TestCase):
    def test_test_orders_use_current_copy_when_sources_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            knowledge = root / "data" / "knowledge"
            knowledge.mkdir(parents=True)
            (knowledge / "sku-1.json").write_text(
                json.dumps({"id": "sku-1", "包装类型": "纸盒"}), encoding="utf-8"
            )
            shelves = root / "data" / "shelves.csv"
            shelves.write_text(
                "sku_code,out_item_id,name,shelf_number,level,bin_unit,shelf_attribute\n"
                "sku-1,OUT-1,Drug,01,02,03,regular_shelf\n"
                "sku-2,OUT-2,Excluded,01,02,04,regular_shelf\n",
                encoding="utf-8",
            )
            paths = {"knowledge": knowledge, "shelves": shelves}
            for key, payload in (
                ("unavailable", {"unavailable_obj": ["sku-2"]}),
                ("tool_mapping", {"sku-1": "gripper"}),
                ("pick_strategy", {"closed_loop": ["sku-1"]}),
            ):
                paths[key] = root / "data" / (key + ".json")
                paths[key].write_text(json.dumps(payload), encoding="utf-8")
            with (
                patch.multiple(
                    state, loaded_paths=paths, data_revision=7654321,
                    **{"configured_" + key: root / "source-gone" / key for key in paths},
                ),
                patch.multiple(
                    test_order_api,
                    STATE_FILE=root / "orders.json",
                    _packaging_choices_cache=None,
                    _packaging_choices_cache_at=0.0,
                    _packaging_choices_cache_revision=None,
                ),
            ):
                generated = test_order_api.generate({"count": 1})
                self.assertEqual(generated["candidate_count"], 1)
                self.assertIn("纸盒", generated["known_packaging"])
                item = generated["pending"][0]
                self.assertEqual(item["sku_code"], "sku-1")
                self.assertEqual(item["location_code"], "010203")
                self.assertEqual(item["推荐工具"], "gripper")
                self.assertEqual(item["is_small"], "1")
                imported = test_order_api.import_csv({"csv": "69码\nsku-1\n"})
                item = imported["pending"][0]
                self.assertEqual(item["location_code"], "010203")
                self.assertEqual(item["包装类型"], "纸盒")
                self.assertEqual(item["推荐工具"], "gripper")

    def test_only_shelf_fields_change_the_loaded_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "data" / "current"
            knowledge = current / "knowledge"
            knowledge.mkdir(parents=True)
            record = {"id": "sku-1", "重量": 12, "包装类型": "纸盒"}
            knowledge_file = knowledge / "sku-1.json"
            knowledge_file.write_text(json.dumps(record), encoding="utf-8")
            source = root / "source.csv"
            original_csv = (
                "sku_code,name,shelf_number,level,bin_unit,shelf_attribute,baffle_height\n"
                "sku-1,Drug,01,01,01,regular_shelf,10\n"
                "sku-1,Drug,01,01,02,pusher,20\n"
            )
            source.write_text(original_csv, encoding="utf-8")
            shelves = current / "shelves.csv"
            shelves.write_bytes(source.read_bytes())
            tool_file = current / "obj_tool_mapping.json"
            tool_file.write_text('{"sku-1": "gripper"}', encoding="utf-8")
            pick_file = current / "pick_strategy_obj.json"
            pick_file.write_text('{"closed_loop": ["sku-1"]}', encoding="utf-8")
            unavailable_file = current / "unavailable_obj.json"
            unavailable_file.write_text('{"unavailable_obj": ["sku-1"]}', encoding="utf-8")
            readonly_files = (knowledge_file, tool_file, pick_file, unavailable_file)
            before = {path: path.read_bytes() for path in readonly_files}
            paths = {
                "knowledge": knowledge,
                "shelves": shelves,
                "tool_mapping": tool_file,
                "pick_strategy": pick_file,
                "unavailable": unavailable_file,
            }
            dataset = build_dataset(knowledge, shelves)
            mapping = {"sku-1": "gripper"}
            ids = frozenset({"sku-1"})
            with patch.multiple(
                state,
                configured_shelves=source,
                loaded_paths=paths,
                loaded_dataset=dataset,
                loaded_tool_mapping=mapping,
                loaded_closed_loop_ids=ids,
                loaded_unavailable_ids=ids,
                edit_workspace=None,
                data_revision=0,
            ):
                edit_workspace.init_workspace_from_loaded()
                for field in ("重量", "包装类型", "未知字段", "使用工具", "是否闭环", "是否不可处理"):
                    with self.subTest(field=field), self.assertRaisesRegex(ValueError, "不允许修改"):
                        edit_workspace.save_field("sku-1", field, "changed")
                with self.assertRaisesRegex(ValueError, "必须指定库位"):
                    edit_workspace.save_field("sku-1", "货架属性", "code_pusher")
                edit_workspace.save_field("sku-1", "货架属性", "code_pusher", "01-01-02")
                edit_workspace.save_field("sku-1", "挡板高度", "25", "01-01-02")
                edit_workspace.save_field("sku-1", "库位", "02-03-04", "01-01-02")
                with patch.object(edit_workspace, "_write_csv_rows", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        edit_workspace.persist_dirty_files()
                self.assertTrue(edit_workspace.list_export_files()["shelves_dirty"])
                result = edit_workspace.persist_dirty_files()
                self.assertFalse(result["wrote_original"])
                self.assertEqual(result["restart_services"], [])
                self.assertEqual(source.read_text(encoding="utf-8"), original_csv)
                self.assertEqual({path: path.read_bytes() for path in readonly_files}, before)
                self.assertIs(state.loaded_tool_mapping, mapping)
                self.assertIs(state.loaded_closed_loop_ids, ids)
                self.assertIs(state.loaded_unavailable_ids, ids)
                self.assertIs(state.loaded_dataset.knowledge_records, dataset.knowledge_records)
                rows = list(csv.DictReader(io.StringIO(shelves.read_text(encoding="utf-8-sig"))))
                self.assertEqual(rows[0]["shelf_attribute"], "regular_shelf")
                self.assertEqual(rows[1]["shelf_attribute"], "code_pusher")
                self.assertEqual(rows[1]["baffle_height"], "25")
                self.assertEqual([rows[1][key] for key in ("shelf_number", "level", "bin_unit")], ["02", "03", "04"])
                self.assertFalse(edit_workspace.list_export_files()["shelves_dirty"])
                with zipfile.ZipFile(io.BytesIO(edit_workspace.build_export_zip_bytes())) as archive:
                    self.assertEqual(json.loads(archive.read("knowledge/sku-1.json")), record)
                    self.assertEqual(json.loads(archive.read("obj_tool_mapping.json")), mapping)
                    self.assertEqual(json.loads(archive.read("pick_strategy_obj.json")), {"closed_loop": ["sku-1"]})
                    self.assertEqual(json.loads(archive.read("unavailabel_obj.json")), {"unavailable_obj": ["sku-1"]})


if __name__ == "__main__":
    unittest.main()
