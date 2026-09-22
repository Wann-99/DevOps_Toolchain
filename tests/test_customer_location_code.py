"""Keep customer locations intact from shelf CSV to Broker requests."""

import tempfile
import unittest
from io import StringIO
from pathlib import Path

from ksq.dataset import build_dataset
from ksq.naming import classify_import_kind
from ksq.order.payload import build_create_task_body, normalize_order_items
from ksq.package_io import load_package, save_package
from ksq.shelves import parse_shelf_locations
from ksq.test_order_select import load_candidates, parse_import_csv_full, public_item
from ksq.web.pages import records_payload


CSV = (
    "sku_id,out_item_id,sku_code,name,shelf_number,level,bin_unit,customer_location_code\n"
    "P000067355,5473,6923099206433,多酶片,0014,02,06,140206\n"
    "P000067355,5473,6923099206433,多酶片,0015,02,02,150202\n"
)


class CustomerLocationCodeTests(unittest.TestCase):
    def test_csv_package_and_catalog_keep_each_locations_customer_code(self):
        self.assertEqual(classify_import_kind("goods-locations.csv"), "shelves")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            (knowledge / "P000067355.json").write_text(
                '{"id":"P000067355"}', encoding="utf-8"
            )
            shelves = root / "goods-locations.csv"
            shelves.write_text(CSV, encoding="utf-8")
            dataset = build_dataset(knowledge, shelves)
            package = root / "data.kpkg"
            save_package(dataset, package)
            for current in (dataset, load_package(package)):
                lines = records_payload(current, None, None, None)["records"][0]["order_lines"]
                self.assertEqual([line["location_code"] for line in lines], ["0014-02-06", "0015-02-02"])
                body = build_create_task_body({"store_id": "6"}, lines)
                self.assertEqual([item["location_code"] for item in body["items"]], ["140206", "150202"])
                self.assertTrue(all(item["item_id"] == "5473" for item in body["items"]))
                self.assertTrue(all("customer_location_code" not in item for item in body["items"]))

            candidates = load_candidates(shelves, set(), {}, set(), {})
            for available in (candidates, []):
                imported, errors, _ = parse_import_csv_full(CSV, available, {}, set(), {})
                self.assertEqual(errors, [])
                self.assertEqual(len(imported), 2)
                for items in (candidates, imported):
                    rows = [public_item(item) for item in items]
                    self.assertEqual([row["location_code"] for row in rows], ["00140206", "00150202"])
                    body = build_create_task_body(
                        {"store_id": "6"},
                        [dict(row, item_id=row["out_item_id"]) for row in rows],
                    )
                    self.assertEqual([item["location_code"] for item in body["items"]], ["140206", "150202"])

    def test_customer_code_is_preserved_and_old_data_keeps_its_fallback(self):
        for customer, expected in ((" 00-A-02 ", "00-A-02"), ("000123", "000123"), ("", "00150202"), ("  ", "00150202"), (None, "00150202")):
            with self.subTest(customer=customer):
                item = {"item_id": "5473", "location_code": "0015-02-02"}
                if customer is not None:
                    item["customer_location_code"] = customer
                self.assertEqual(normalize_order_items([item])[0]["location_code"], expected)
        for invalid in (150202, {}, []):
            with self.assertRaisesRegex(ValueError, "customer_location_code 必须是字符串"):
                normalize_order_items([{"item_id": "5473", "customer_location_code": invalid}])
        with self.assertRaisesRegex(ValueError, "location_code 不能为空"):
            normalize_order_items([{"item_id": "5473"}])

    def test_duplicate_rows_fill_missing_code_and_report_conflicts(self):
        source = StringIO(
            "sku_code,name,shelf_number,level,bin_unit,customer_location_code\n"
            "sku,药品,0015,02,02,\n"
            "sku,药品,0015,02,02,150202\n"
            "sku,药品,0015,02,02,DIFFERENT\n"
        )
        parsed = parse_shelf_locations(source)
        self.assertEqual(parsed.entries["sku"][0].customer_location_code, "150202")
        self.assertEqual(len(parsed.merge_conflicts), 1)
        self.assertIn("客户库位", parsed.merge_conflicts[0])


if __name__ == "__main__":
    unittest.main()
