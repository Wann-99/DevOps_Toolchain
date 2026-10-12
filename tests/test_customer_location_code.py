"""Keep customer locations intact from shelf CSV to Broker requests."""

import tempfile
import unittest
from io import StringIO
from pathlib import Path

from ksq.dataset import build_dataset
from ksq.naming import classify_import_kind
from ksq.order.payload import OrderPayloadError, build_create_task_body, normalize_order_items
from ksq.package_io import load_package, save_package
from ksq.shelves import parse_shelf_locations
from ksq.test_order_select import load_candidates, parse_import_csv_full, public_item
from ksq.web.pages import records_payload


CSV = (
    "sku_id,out_item_id,sku_code,name,manufacturer,specifications,shelf_number,level,bin_unit,customer_location_code\n"
    "P000067355,5473,6923099206433,多酶片,测试厂家,测试规格,0014,02,06,140206\n"
    "P000067355,5473,6923099206433,多酶片,测试厂家,测试规格,0015,02,02,150202\n"
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
                self.assertEqual([line["manufacturer"] for line in lines], ["测试厂家", "测试厂家"])
                self.assertEqual([line["specifications"] for line in lines], ["测试规格", "测试规格"])
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

    def test_customer_profiles_map_available_fields_and_generate_retail_id(self):
        item = {
            "item_id": "1783",
            "customer_location_code": "270104",
            "quantity": 1,
            "name": "测试药品",
            "specifications": "10mg*7片",
            "manufacturer": "测试厂家",
            "approval_number": "国药准字TEST",
            "barcode": "6920587904956",
        }
        yaoshi = build_create_task_body(
            {
                "store_id": "6",
                "customer": "yaoshibang",
                "order_source": "meituan",
                "need_image_upload": True,
            },
            [item],
            {
                "store_phone": "020-12345678",
                "recipient_name": "测试收货人",
                "unknown_field": "不应透传",
            },
        )
        self.assertEqual(
            yaoshi["items"][0],
            {
                "item_id": "1783",
                "common_name": "测试药品",
                "quantity": 1,
                "spec": "10mg*7片",
                "barcode": "6920587904956",
                "location_code": "270104",
                "manufacturer": "测试厂家",
                "approval_number": "国药准字TEST",
            },
        )
        self.assertTrue(str(yaoshi["retail_order_id"]).startswith("RO"))
        self.assertEqual(yaoshi["store_phone"], "020-12345678")
        self.assertEqual(yaoshi["recipient_name"], "测试收货人")
        self.assertNotIn("unknown_field", yaoshi)
        self.assertTrue(yaoshi["need_image_upload"])

        shuyu = build_create_task_body(
            {"store_id": "B877", "customer": "shuyu", "order_source": "meituan"},
            [{key: value for key, value in item.items() if key != "customer_location_code"}],
        )
        self.assertEqual(
            shuyu["items"][0],
            {
                "item_id": "1783",
                "item_name": "测试药品",
                "specification": "10mg*7片",
                "quantity": 1,
            },
        )
        self.assertNotIn("location_code", shuyu["items"][0])
        lzgzdyf = build_create_task_body(
            {"store_id": "lzgzdyf_test", "customer": "lzgzdyf"},
            [item],
        )
        self.assertEqual(lzgzdyf["items"][0]["spec"], "10mg*7片")
        self.assertEqual(lzgzdyf["items"][0]["manufacturer"], "测试厂家")
        self.assertNotIn("total_price", shuyu)
        self.assertIn("total_price", lzgzdyf)

        dashenlin = build_create_task_body(
            {"store_id": "dsl_test", "customer": "dashenlin"}, [item]
        )
        self.assertEqual(dashenlin["items"][0]["common_name"], "测试药品")
        self.assertEqual(dashenlin["items"][0]["spec"], "10mg*7片")
        self.assertEqual(dashenlin["items"][0]["location_code"], "270104")
        self.assertIn("total_price", dashenlin)
        self.assertNotIn("retail_order_id", dashenlin)

        noematrix = build_create_task_body(
            {
                "store_id": "noematrix_test",
                "customer": "noematrix",
                "need_image_upload": True,
            },
            [item],
        )
        self.assertEqual(noematrix["items"][0]["approval_number"], "国药准字TEST")
        self.assertTrue(str(noematrix["retail_order_id"]).startswith("RO"))
        self.assertTrue(noematrix["need_image_upload"])

    def test_missing_item_fields_are_returned_together(self):
        with self.assertRaises(OrderPayloadError) as raised:
            normalize_order_items([{"item_id": ""}, {"item_id": "5473"}])
        self.assertEqual(
            raised.exception.missing_fields,
            [
                {"item_index": 0, "item_id": "", "fields": ["item_id", "location_code"]},
                {"item_index": 1, "item_id": "5473", "fields": ["location_code"]},
            ],
        )


if __name__ == "__main__":
    unittest.main()
