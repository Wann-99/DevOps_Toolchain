from __future__ import annotations

import unittest

from ksq.web import dashboard_api as da


# 现场 Broker 单任务详情的真实形状（字段已裁剪，保留取值相关的部分）：
# 条目挂在 data.data.params.items，名称在 item_name，库位在顶层 location_code，
# 同时 locations[0] 里另有一份可还原的库位。
REAL_ITEM = {
    "item_id": "761",
    "sku_id": "P000019273",
    "barcode": "6958152200066",
    "item_name": "复方氯己定含漱液",
    "common_name": "复方氯己定含漱液",
    "location_code": "00530302",
    "locations": [
        {
            "level": "03",
            "bin_unit": "02",
            "shelf_number": "0053",
            "customer_location_code": "00530302",
        }
    ],
    "quantity": 1,
}


def broker(items, ok=True, container="params"):
    return {"ok": ok, "raw": {container: {"items": items}}}


class BrokerItemExtractionTests(unittest.TestCase):
    def test_extracts_name_and_location_from_params_items(self) -> None:
        items = da._broker_order_items(broker([REAL_ITEM]))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "复方氯己定含漱液")
        self.assertEqual(items[0]["location_code"], "00530302")
        self.assertEqual(items[0]["sku_id"], "P000019273")
        self.assertEqual(items[0]["item_id"], "761")

    def test_also_reads_task_detail_items_from_list_rows(self) -> None:
        items = da._broker_order_items(broker([REAL_ITEM], container="task_detail"))

        self.assertEqual(items[0]["barcode"], "6958152200066")

    def test_location_falls_back_to_customer_location_code(self) -> None:
        entry = dict(REAL_ITEM, location_code="")
        items = da._broker_order_items(broker([entry]))

        self.assertEqual(items[0]["location_code"], "00530302")

    def test_location_rebuilt_from_shelf_level_bin(self) -> None:
        entry = dict(REAL_ITEM, location_code="")
        entry["locations"] = [
            {"shelf_number": "0053", "level": "03", "bin_unit": "02"}
        ]
        items = da._broker_order_items(broker([entry]))

        self.assertEqual(items[0]["location_code"], "00530302")

    def test_name_falls_back_through_common_name_and_alias(self) -> None:
        entry = dict(REAL_ITEM)
        entry.pop("item_name")
        self.assertEqual(
            da._broker_order_items(broker([entry]))[0]["name"], "复方氯己定含漱液"
        )

        entry2 = dict(REAL_ITEM, item_name="", common_name="")
        entry2["alias"] = "别名药"
        self.assertEqual(
            da._broker_order_items(broker([entry2]))[0]["name"], "别名药"
        )

    def test_broker_failure_yields_no_items(self) -> None:
        self.assertEqual(da._broker_order_items(broker([REAL_ITEM], ok=False)), [])
        self.assertEqual(da._broker_order_items({"ok": True}), [])
        self.assertEqual(da._broker_order_items({"ok": True, "raw": {}}), [])


class ApplyBrokerItemDetailsTests(unittest.TestCase):
    def test_fills_empty_name_and_location_matched_by_barcode(self) -> None:
        """截图里的情形：日志只解析出条码，名称/库位为空。"""
        tasks = [
            {
                "code": "6958152200066",
                "barcode": "6958152200066",
                "name": "",
                "location_code": "",
                "status": "success",
            }
        ]

        da._apply_broker_item_details(tasks, broker([REAL_ITEM]))

        self.assertEqual(tasks[0]["name"], "复方氯己定含漱液")
        self.assertEqual(tasks[0]["location_code"], "00530302")
        self.assertEqual(tasks[0]["sku_id"], "P000019273")
        self.assertTrue(tasks[0]["broker_matched"])

    def test_broker_barcode_overrides_sku_id_polluted_barcode(self) -> None:
        """日志 item 行编号是 sku_id 时，barcode/item_id 会被回退污染成 sku_id，
        必须用 Broker 的真 69码 覆盖，否则页面「69码」里显示的是 sku_id。"""
        tasks = [
            {
                "code": "P000019273",
                "barcode": "P000019273",   # 被 code 回退污染
                "item_id": "P000019273",   # 同样被污染
                "sku_id": "P000019273",
                "name": "",
                "location_code": "",
                "status": "success",
            }
        ]

        da._apply_broker_item_details(tasks, broker([REAL_ITEM]))

        self.assertEqual(tasks[0]["barcode"], "6958152200066")
        self.assertEqual(tasks[0]["item_id"], "761")
        self.assertEqual(tasks[0]["sku_id"], "P000019273")
        self.assertEqual(tasks[0]["name"], "复方氯己定含漱液")

    def test_matches_by_sku_id_and_item_id_too(self) -> None:
        for key, value in (("sku_id", "P000019273"), ("item_id", "761")):
            with self.subTest(key=key):
                tasks = [{key: value, "name": "", "location_code": ""}]
                da._apply_broker_item_details(tasks, broker([REAL_ITEM]))
                self.assertEqual(tasks[0]["location_code"], "00530302")

    def test_broker_value_overrides_stale_local_value(self) -> None:
        """名称/库位以 Broker 为准，本地快照来自 CSV 不可信。"""
        tasks = [
            {
                "barcode": "6958152200066",
                "name": "旧名称",
                "location_code": "999999",
            }
        ]

        da._apply_broker_item_details(tasks, broker([REAL_ITEM]))

        self.assertEqual(tasks[0]["name"], "复方氯己定含漱液")
        self.assertEqual(tasks[0]["location_code"], "00530302")

    def test_never_blanks_existing_value_when_broker_is_empty(self) -> None:
        entry = dict(REAL_ITEM, item_name="", common_name="", location_code="")
        entry["locations"] = []
        entry.pop("alias", None)
        tasks = [
            {"barcode": "6958152200066", "name": "本地名", "location_code": "520701"}
        ]

        da._apply_broker_item_details(tasks, broker([entry]))

        self.assertEqual(tasks[0]["name"], "本地名")
        self.assertEqual(tasks[0]["location_code"], "520701")
        # Broker 侧 barcode 非空，仍应以它为准
        self.assertEqual(tasks[0]["barcode"], "6958152200066")

    def test_unmatched_task_is_left_untouched(self) -> None:
        tasks = [{"barcode": "699999999", "name": "", "location_code": ""}]

        da._apply_broker_item_details(tasks, broker([REAL_ITEM]))

        self.assertEqual(tasks[0]["name"], "")
        self.assertNotIn("broker_matched", tasks[0])


class FocusMatchFieldsTests(unittest.TestCase):
    def test_match_fields_cover_every_identifier_the_log_may_carry(self) -> None:
        # 日志里的 active_code 可能是条码，也可能是 sku_id / item_id
        self.assertEqual(
            set(da._ITEM_MATCH_FIELDS),
            {"item_id", "sku_id", "barcode", "code"},
        )

    def test_match_broker_item_uses_index(self) -> None:
        index = da._broker_item_index(da._broker_order_items(broker([REAL_ITEM])))

        self.assertIsNotNone(da._match_broker_item({"sku_id": "P000019273"}, index))
        self.assertIsNotNone(da._match_broker_item({"code": "6958152200066"}, index))
        self.assertIsNone(da._match_broker_item({"code": "nope"}, index))


if __name__ == "__main__":
    unittest.main()
