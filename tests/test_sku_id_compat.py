from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from ksq.dataset import build_dataset
from ksq.naming import is_shelves_file_name
from ksq.package_io import load_package, save_package
from ksq.shelves import parse_shelf_locations
from ksq.test_order_select import load_candidates, parse_import_csv, public_item
from ksq.web import dashboard_api, edit_workspace, order_api
from ksq.web.pages import records_payload


NEW_HEADER = (
    "sku_id,out_item_id,sku_code,name,shelf_number,level,bin_unit,"
    "shelf_attribute,baffle_height,future_column\n"
)
NEW_ROW = (
    "sku-new,OUT-1,690001,新格式药品,0085,05,03,regular_shelf,,future-value\n"
)
OLD_CSV = (
    "out_item_id,sku_code,name,shelf_number,level,bin_unit\n"
    "OUT-1,690001,旧格式药品,01,02,03\n"
)


class SkuIdCompatibilityTests(unittest.TestCase):
    def test_new_sku_id_links_knowledge_and_keeps_barcode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            (knowledge / "sku-new.json").write_text(
                json.dumps({"id": "sku-new", "包装类型": "纸盒"}),
                encoding="utf-8",
            )
            shelves = root / "etm_sku_locations_cache.csv"
            shelves.write_text(NEW_HEADER + NEW_ROW, encoding="utf-8")

            dataset = build_dataset(knowledge, shelves)
            payload = records_payload(
                dataset,
                {"690001": "gripper"},
                frozenset({"690001"}),
                frozenset({"sku-new"}),
            )
            record = payload["records"][0]

            self.assertEqual(dataset.report.shelves_without_knowledge, ())
            self.assertEqual(record["id"], "sku-new")
            self.assertEqual(record["sku_id"], "sku-new")
            self.assertEqual(record["sku_code"], "690001")
            self.assertEqual(record["knowledge"]["包装类型"], "纸盒")
            self.assertEqual(record["tool"], "gripper")
            self.assertEqual(record["closed_loop"], "是")
            self.assertEqual(record["unavailable"], "是")
            self.assertEqual(record["order_lines"][0]["sku_id"], "sku-new")
            self.assertEqual(record["order_lines"][0]["barcode"], "690001")

            package = root / "data.kpkg"
            save_package(dataset, package)
            restored = load_package(package)
            self.assertEqual(restored.shelf_entries["sku-new"][0].sku_code, "690001")

    def test_old_csv_still_uses_sku_code_as_record_id(self) -> None:
        parsed = parse_shelf_locations(StringIO(OLD_CSV))

        self.assertEqual(list(parsed.entries), ["690001"])
        self.assertEqual(parsed.entries["690001"][0].sku_code, "690001")

    def test_test_order_supports_sku_id_and_preserves_unknown_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shelves = Path(temporary) / "etm_sku_locations_cache.csv"
            shelves.write_text(NEW_HEADER + NEW_ROW, encoding="utf-8")
            candidates = load_candidates(
                shelves,
                set(),
                {"690001": "gripper"},
                {"sku-new"},
                {"sku-new": "纸盒"},
            )

        self.assertEqual(candidates[0]["sku_id"], "sku-new")
        self.assertEqual(public_item(candidates[0])["key"], "sku-new|00850503")
        rows, errors = parse_import_csv("SKU ID\nsku-new\n", candidates, {}, set(), {})
        self.assertEqual(rows[0]["sku_code"], "690001")
        self.assertEqual(errors, [])

    def test_etm_cache_filename_is_accepted(self) -> None:
        self.assertTrue(is_shelves_file_name("etm_sku_locations_cache.csv"))
        self.assertTrue(is_shelves_file_name("sku-shelves_20260820.csv"))

    def test_edit_and_dashboard_keep_both_identifiers(self) -> None:
        row = {
            "sku_id": "sku-new",
            "sku_code": "690001",
            "shelf_number": "0085",
            "level": "05",
            "bin_unit": "03",
        }
        workspace = {"shelf_rows": [row]}
        self.assertEqual(edit_workspace._rows_for_sku(workspace, "sku-new"), [row])
        self.assertEqual(
            edit_workspace._side_item_id(workspace, "sku-new", {"690001"}),
            "690001",
        )

        broker_body = {"items": [{"item_id": "OUT-1", "barcode": "690001"}]}
        local_body = order_api._local_request_body(
            broker_body,
            [{"sku_id": "sku-new", "item_id": "OUT-1", "barcode": "690001"}],
        )
        self.assertNotIn("sku_id", broker_body["items"][0])
        self.assertEqual(local_body["items"][0]["sku_id"], "sku-new")

        order = dashboard_api._build_active_order(
            {"items": local_body["items"], "task_id": "TASK-1"}
        )
        self.assertEqual(order["items"][0]["sku_id"], "sku-new")
        self.assertEqual(
            dashboard_api._merged_code_aliases(order, ""),
            {"sku-new": "690001"},
        )


if __name__ == "__main__":
    unittest.main()
