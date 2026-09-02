from __future__ import annotations

import unittest
from unittest.mock import patch

from ksq.web import dashboard_api as da


# 现场实测数据：日志的 item 行编号用 sku_id，start process object 行同时带
# code 与 barcode，由此建立 code_aliases（sku_id -> 69码）。
SKU_ID = "P000063255"
BARCODE = "6916999320514"
LOCATION = "00600410"
NAME = "苯扎氯铵贴(防水型)"


def active_order(items, aliases=None):
    return {
        "task_id": "6483d367-0902-4933-a811-f660dd730b5e",
        "order_no": "TEST20260827235853032QT",
        "items": items,
        "item_states": {},
        "code_aliases": dict(aliases or {}),
    }


ORDERED_ITEM = {
    "index": 1,
    "code": BARCODE,
    "item_id": "527",
    "barcode": BARCODE,
    "sku_id": SKU_ID,
    "name": NAME,
    "location_code": LOCATION,
}


class PersistItemStatesAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = da._ACTIVE_ORDER
        self.saved_loaded = da._ACTIVE_ORDER_LOADED

    def tearDown(self) -> None:
        da._ACTIVE_ORDER = self.saved
        da._ACTIVE_ORDER_LOADED = self.saved_loaded

    def persist(self, order, tasks):
        da._ACTIVE_ORDER = order
        da._ACTIVE_ORDER_LOADED = True
        with patch.object(da, "_save_active_order_unlocked"):
            da._persist_item_states(order, tasks)
        return da._ACTIVE_ORDER["items"]

    def test_sku_id_task_does_not_duplicate_existing_barcode_item(self) -> None:
        """核心场景：日志按 sku_id 上报同一药品，不得再追加一条影子子任务。"""
        order = active_order([dict(ORDERED_ITEM)], {SKU_ID: BARCODE})

        items = self.persist(order, [{"code": SKU_ID, "status": "skipped"}])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["barcode"], BARCODE)

    def test_without_alias_the_sku_id_item_is_still_appended(self) -> None:
        """别名尚未建立时无法翻译，只能按新条目处理（保持既有行为，不猜形态）。"""
        order = active_order([dict(ORDERED_ITEM)], {})

        items = self.persist(order, [{"code": SKU_ID, "status": "skipped"}])

        self.assertEqual(len(items), 2)
        self.assertEqual(items[1]["code"], SKU_ID)

    def test_translated_entry_keeps_barcode_and_sku_id_in_own_fields(self) -> None:
        """69码 与 sku_id 是两个字段：翻译后 barcode 存 69码，sku_id 存 sku_id。"""
        order = active_order([], {SKU_ID: BARCODE})

        items = self.persist(order, [{"code": SKU_ID, "status": "success"}])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["code"], BARCODE)
        self.assertEqual(items[0]["barcode"], BARCODE)
        self.assertEqual(items[0]["sku_id"], SKU_ID)

    def test_genuine_new_barcode_is_still_appended(self) -> None:
        order = active_order([dict(ORDERED_ITEM)], {SKU_ID: BARCODE})

        items = self.persist(
            order, [{"code": "6928975007920", "status": "success"}]
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(items[1]["barcode"], "6928975007920")

    def test_task_barcode_field_wins_over_resolved_code(self) -> None:
        """task 自带 barcode 时以它为准，不被 resolved_code 覆盖。"""
        order = active_order([], {SKU_ID: BARCODE})

        items = self.persist(
            order,
            [{"code": SKU_ID, "barcode": BARCODE, "item_id": "527", "status": "success"}],
        )

        self.assertEqual(items[0]["barcode"], BARCODE)
        self.assertEqual(items[0]["item_id"], "527")
        self.assertEqual(items[0]["sku_id"], SKU_ID)

    def test_item_states_still_keyed_by_raw_log_code(self) -> None:
        """状态记忆仍按日志原始 code 建键，别名翻译不影响它。"""
        order = active_order([dict(ORDERED_ITEM)], {SKU_ID: BARCODE})

        self.persist(order, [{"code": SKU_ID, "status": "skipped"}])

        self.assertIn(SKU_ID, da._ACTIVE_ORDER["item_states"])


if __name__ == "__main__":
    unittest.main()
