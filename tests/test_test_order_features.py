from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ksq.test_order_select import (
    DEFAULT_COLUMNS,
    parse_import_csv,
    parse_import_csv_full,
    public_item,
)
from ksq.web import logs_api, test_order_api


def candidate(out_id: str, location: str, barcode: str) -> dict[str, str]:
    return {
        "out_item_id": out_id,
        "location_code": location,
        "sku_code": barcode,
        "name": "药品" + out_id,
        "推荐工具": "gripper",
        "包装类型": "纸盒",
        "shelf_attribute": "normal",
        "shelf_number": location[:2],
        "level": location[2:4],
        "bin_unit": location[4:6],
        "is_small": "0",
        "is_special": "1",
        "is_code_pusher": "0",
    }


class FlexibleCsvImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = [
            candidate("OUT-1", "010203", "690001"),
            candidate("OUT-2", "010204", "690002"),
        ]

    def parse(self, text: str) -> tuple[list[dict[str, str]], list[str]]:
        return parse_import_csv(text, self.candidates, {}, set(), {})

    def test_accepts_any_one_supported_identifier_column(self) -> None:
        cases = (
            ("商品编码\nOUT-1\n", "OUT-1"),
            ("库位\n01-02-04\n", "OUT-2"),
            ("69码\n690001\n", "OUT-1"),
            ("barcode\n690002.0\n", "OUT-2"),
        )
        for csv_text, expected in cases:
            with self.subTest(csv_text=csv_text):
                rows, errors = self.parse(csv_text)
                self.assertEqual(rows[0]["out_item_id"], expected)
                self.assertEqual(errors, [])

    def test_multiple_identifiers_prefer_the_matching_candidate(self) -> None:
        rows, errors = self.parse("商品编码,库位\nOUT-2,01-02-04\n")
        self.assertEqual(rows[0]["sku_code"], "690002")
        self.assertEqual(errors, [])

    def test_row_absent_from_candidates_is_imported_from_the_file(self) -> None:
        # 候选数据里没有这个组合（OUT-1 在 010203），仍按原文件导入不拦截
        rows, errors = self.parse("商品编码,库位\nOUT-1,01-02-04\n")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["out_item_id"], "OUT-1")
        self.assertEqual(rows[0]["location_code"], "010204")
        self.assertEqual(rows[0]["sku_code"], "")
        self.assertEqual(errors, [])

    def test_unknown_sku_is_imported_and_keeps_orderable_key(self) -> None:
        rows, _errors = self.parse("69码,库位\n699999,52-07-01\n")

        self.assertEqual(rows[0]["sku_code"], "699999")
        self.assertEqual(public_item(rows[0])["key"], "699999|520701")

    def test_out_item_id_only_row_still_has_a_usable_key(self) -> None:
        # 无 sku_id/69码 时回退用商品编码做标识，否则 key 为 "|520701" 无法下单
        rows, _errors = self.parse("商品编码,库位\nOUT-9,52-07-01\n")

        self.assertEqual(public_item(rows[0])["key"], "OUT-9|520701")

    def test_rejects_csv_without_supported_identifier_header(self) -> None:
        with self.assertRaisesRegex(ValueError, "至少需要商品编码、库位、69码"):
            self.parse("药品名称\n测试药品\n")


class TestOrderBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temporary.name) / "test-order-state.json"
        self.state_patch = patch.object(test_order_api, "STATE_FILE", self.state_file)
        self.state_patch.start()
        self.packaging_patch = patch.object(
            test_order_api, "_candidate_packaging_choices", return_value=["全部"]
        )
        self.packaging_patch.start()

    def tearDown(self) -> None:
        self.packaging_patch.stop()
        self.state_patch.stop()
        self.temporary.cleanup()

    def save_pending(self, items: list[dict[str, str]]) -> None:
        state = test_order_api._empty_state()
        state["pending"] = items
        test_order_api._save_state(state)

    def test_multiple_skus_in_one_submission_count_as_one_order(self) -> None:
        first = candidate("OUT-1", "010203", "690001")
        second = candidate("OUT-2", "010204", "690002")
        self.save_pending([first, second])

        result = test_order_api.mark_ordered(
            {
                "keys": ["690001|010203", "690002|010204"],
                "order_no": "ORDER-1",
                "task_id": "TASK-1",
            }
        )

        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["ordered_count"], 2)
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["order_batches"][0]["sku_count"], 2)
        self.assertTrue(all(row["order_no"] == "ORDER-1" for row in result["ordered"]))

    def test_mark_ordered_retry_is_idempotent_for_same_external_order(self) -> None:
        item = candidate("OUT-1", "010203", "690001")
        self.save_pending([item])
        payload = {
            "keys": ["690001|010203"],
            "order_no": "ORDER-RETRY",
            "task_id": "TASK-RETRY",
        }

        first = test_order_api.mark_ordered(payload)
        second = test_order_api.mark_ordered(payload)

        self.assertNotIn("idempotent", first)
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["pending_count"], 0)
        self.assertEqual(second["ordered_count"], 1)
        self.assertEqual(second["order_count"], 1)
        self.assertEqual(len(second["order_batches"]), 1)

    def test_restore_cleans_order_metadata_and_trims_batches(self) -> None:
        first = candidate("OUT-1", "010203", "690001")
        second = candidate("OUT-2", "010204", "690002")
        third = candidate("OUT-3", "010205", "690003")
        self.save_pending([first, second, third])
        test_order_api.mark_ordered(
            {
                "keys": ["690001|010203", "690002|010204"],
                "order_no": "ORDER-A",
                "task_id": "TASK-A",
            }
        )
        test_order_api.mark_ordered(
            {"keys": ["690003|010205"], "order_no": "ORDER-B", "task_id": "TASK-B"}
        )

        restored = test_order_api.restore({"keys": ["690001|010203"]})

        self.assertEqual(
            [item["sku_code"] for item in restored["pending"]], ["690001"]
        )
        self.assertEqual(
            [item["sku_code"] for item in restored["ordered"]], ["690003", "690002"]
        )
        self.assertEqual(restored["order_count"], 2)
        self.assertEqual(
            [(batch["order_no"], batch["sku_count"]) for batch in restored["order_batches"]],
            [("ORDER-B", 1), ("ORDER-A", 1)],
        )
        raw = test_order_api._load_state_file()
        self.assertNotIn("order_no", raw["pending"][0])
        self.assertNotIn("task_id", raw["pending"][0])

        restored_again = test_order_api.restore({"keys": ["690002|010204"]})
        self.assertEqual(restored_again["order_count"], 1)
        self.assertEqual(restored_again["order_batches"][0]["order_no"], "ORDER-B")

    def test_restore_rejects_missing_or_mixed_keys_without_mutation(self) -> None:
        first = candidate("OUT-1", "010203", "690001")
        second = candidate("OUT-2", "010204", "690002")
        self.save_pending([first, second])
        test_order_api.mark_ordered(
            {"keys": ["690001|010203"], "order_no": "ORDER-A"}
        )
        before = self.state_file.read_bytes()

        with self.assertRaisesRegex(ValueError, "同时包含"):
            test_order_api.restore(
                {"keys": ["690001|010203", "690002|010204"]}
            )
        self.assertEqual(self.state_file.read_bytes(), before)

        with self.assertRaisesRegex(ValueError, "不存在"):
            test_order_api.restore({"keys": ["690999|010299"]})
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_batch_id_fallback_is_stable_across_state_reads(self) -> None:
        item = candidate("OUT-1", "010203", "690001")
        state = test_order_api._empty_state()
        state["ordered"] = [item]
        state["order_batches"] = [
            {
                "ordered_at": "2026-08-26T10:00:00+08:00",
                "order_no": "ORDER-1",
                "task_id": "TASK-1",
                "item_keys": ["690001|010203"],
            }
        ]
        test_order_api._save_state(state)

        first = test_order_api.get_state()
        second = test_order_api.get_state()

        self.assertEqual(first["order_batches"], second["order_batches"])
        self.assertTrue(first["order_batches"][0]["batch_id"].startswith("batch-"))

    def test_config_rejects_string_boolean_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "布尔值"):
            test_order_api.update_config(
                {"config": {"closed_loop_enabled": "false"}}
            )

    def test_config_rejects_coerced_numbers_and_nonfinite_ratios(self) -> None:
        for key, value in (("count", True), ("count", 1.5), ("seed", 1.5)):
            with self.subTest(key=key, value=value), self.assertRaisesRegex(
                ValueError, "整数"
            ):
                test_order_api._normalize_config({key: value})
        for value in (float("nan"), float("inf"), "NaN"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                test_order_api._normalize_config({"closed_loop_ratio": value})

    def test_malformed_key_and_state_count_are_safe(self) -> None:
        self.assertIsNone(test_order_api._parse_key("690001|010203|group|extra"))
        state = test_order_api._empty_state()
        state["candidate_count"] = "not-a-number"
        test_order_api._save_state(state)
        self.assertEqual(test_order_api._load_state_file()["candidate_count"], 0)

    def test_packaging_choices_cache_changes_with_data_revision(self) -> None:
        self.packaging_patch.stop()
        old_cache = test_order_api._packaging_choices_cache
        old_cache_at = test_order_api._packaging_choices_cache_at
        old_cache_revision = test_order_api._packaging_choices_cache_revision
        old_revision = test_order_api.state.data_revision
        try:
            test_order_api._packaging_choices_cache = None
            test_order_api._packaging_choices_cache_at = 0
            test_order_api._packaging_choices_cache_revision = None
            with patch.object(
                test_order_api,
                "_load_packaging_choices",
                side_effect=[["纸盒"], ["塑料袋"]],
            ) as loader, patch.object(test_order_api.state, "data_revision", 10):
                self.assertEqual(test_order_api._candidate_packaging_choices(), ["纸盒"])
                self.assertEqual(test_order_api._candidate_packaging_choices(), ["纸盒"])
                test_order_api.state.data_revision = 11
                self.assertEqual(test_order_api._candidate_packaging_choices(), ["塑料袋"])
                self.assertEqual(loader.call_count, 2)
        finally:
            test_order_api._packaging_choices_cache = old_cache
            test_order_api._packaging_choices_cache_at = old_cache_at
            test_order_api._packaging_choices_cache_revision = old_cache_revision
            test_order_api.state.data_revision = old_revision
            self.packaging_patch.start()

    def test_mark_ordered_retry_rejects_mismatched_external_identity(self) -> None:
        item = candidate("OUT-1", "010203", "690001")
        self.save_pending([item])
        test_order_api.mark_ordered(
            {
                "keys": ["690001|010203"],
                "order_no": "ORDER-ORIGINAL",
                "task_id": "TASK-ORIGINAL",
            }
        )

        with self.assertRaisesRegex(ValueError, "未在待下单"):
            test_order_api.mark_ordered(
                {
                    "keys": ["690001|010203"],
                    "order_no": "ORDER-OTHER",
                    "task_id": "TASK-OTHER",
                }
            )

    def test_mark_ordered_does_not_partially_move_missing_keys(self) -> None:
        item = candidate("OUT-1", "010203", "690001")
        self.save_pending([item])

        with self.assertRaisesRegex(ValueError, "不存在"):
            test_order_api.mark_ordered(
                {
                    "keys": ["690001|010203", "690999|010299"],
                    "task_id": "TASK-MISSING",
                }
            )

        state = test_order_api.get_state()
        self.assertEqual(state["pending_count"], 1)
        self.assertEqual(state["ordered_count"], 0)

    def test_legacy_ordered_rows_are_not_treated_as_idempotent(self) -> None:
        item = candidate("OUT-1", "010203", "690001")
        state = test_order_api._empty_state()
        state["ordered"] = [item]
        test_order_api._save_state(state)

        with self.assertRaisesRegex(ValueError, "未在待下单"):
            test_order_api.mark_ordered(
                {
                    "keys": ["690001|010203"],
                    "task_id": "TASK-LEGACY",
                }
            )

    def test_update_config_saves_switches_without_touching_lists(self) -> None:
        item = candidate("OUT-1", "010203", "690001")
        state = test_order_api._empty_state()
        state["pending"] = [item]
        state["ordered"] = [dict(item, order_no="ORDER-1")]
        test_order_api._save_state(state)

        result = test_order_api.update_config(
            {"config": {"closed_loop_enabled": False, "tool_enabled": False}}
        )

        self.assertFalse(result["config"]["closed_loop_enabled"])
        self.assertFalse(result["config"]["tool_enabled"])
        self.assertEqual(result["pending"][0]["out_item_id"], "OUT-1")
        self.assertEqual(result["ordered"][0]["out_item_id"], "OUT-1")

    def test_newest_order_is_first(self) -> None:
        first = candidate("OUT-1", "010203", "690001")
        second = candidate("OUT-2", "010204", "690002")
        third = candidate("OUT-3", "010205", "690003")
        self.save_pending([first, second, third])
        test_order_api.mark_ordered(
            {"keys": ["690001|010203", "690002|010204"], "order_no": "OLD"}
        )
        result = test_order_api.mark_ordered(
            {"keys": ["690003|010205"], "order_no": "NEW"}
        )

        self.assertEqual(result["order_count"], 2)
        self.assertEqual(result["ordered"][0]["order_no"], "NEW")
        self.assertEqual([row["order_no"] for row in result["ordered"][1:]], ["OLD", "OLD"])

    def test_legacy_ordered_rows_are_migrated_as_one_batch(self) -> None:
        state = test_order_api._empty_state()
        state["ordered"] = [candidate("OUT-1", "010203", "690001")]
        test_order_api._save_state(state)

        result = test_order_api.get_state()

        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["order_batches"][0]["batch_id"], "legacy")


class DynamicColumnsImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = [
            candidate("OUT-1", "010203", "690001"),
            candidate("OUT-2", "010204", "690002"),
        ]

    def parse_full(
        self, text: str, group_field: str = ""
    ) -> tuple[list[dict[str, str]], list[str], list[dict[str, str]]]:
        return parse_import_csv_full(
            text, self.candidates, {}, set(), {}, group_field
        )

    def test_columns_follow_csv_headers_and_keep_raw_values(self) -> None:
        rows, errors, columns = self.parse_full("批次,69码,备注\nA01,690001,加急\n")

        self.assertEqual(errors, [])
        self.assertEqual(
            [column["label"] for column in columns], ["批次", "69码", "备注"]
        )
        self.assertEqual(
            rows[0]["display"], {"批次": "A01", "69码": "690001", "备注": "加急"}
        )
        self.assertNotIn("group_id", rows[0])

    def test_group_field_assigns_group_id(self) -> None:
        rows, errors, _columns = self.parse_full(
            "批次,69码\nA01,690001\nA01,690002\n", "批次"
        )

        self.assertEqual(errors, [])
        self.assertEqual([row["group_id"] for row in rows], ["A01", "A01"])

    def test_unknown_group_field_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "组合字段"):
            self.parse_full("批次,69码\nA01,690001\n", "不存在的列")

    def test_duplicate_headers_are_deduplicated(self) -> None:
        _rows, _errors, columns = self.parse_full("69码,69码\n690001,690001\n")

        self.assertEqual([column["key"] for column in columns], ["69码", "69码 (2)"])

    def test_fullwidth_comma_header_and_wide_sku_columns(self) -> None:
        # 表头用全角逗号、数据用半角逗号；一行两个 69码 列 → 拆成两条
        rows, errors, columns = self.parse_full(
            "组\uff0c69码\uff0c69码\nd001,690001,690002\n", "组"
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            [column["label"] for column in columns], ["组", "69码", "69码"]
        )
        self.assertEqual([row["sku_code"] for row in rows], ["690001", "690002"])
        self.assertEqual([row["group_id"] for row in rows], ["d001", "d001"])

    def test_wide_row_with_empty_sku_cell_is_partially_imported(self) -> None:
        rows, errors, _columns = self.parse_full("组,69码,69码\nd001,690001,\n")

        self.assertEqual(errors, [])
        self.assertEqual([row["sku_code"] for row in rows], ["690001"])

    def test_multiple_sku_id_columns_split_into_one_item_each(self) -> None:
        # 一行两个 SKU ID 列也是宽表；曾只认 69码 列，导致只取第一个、
        # 第二个被静默丢弃，组合行里也就没有对应的勾选框
        rows, errors, _columns = self.parse_full(
            "id,sku_id,sku_id\n002,P000000745,P000000851\n", "id"
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            [row["sku_id"] for row in rows], ["P000000745", "P000000851"]
        )
        self.assertEqual([row["group_id"] for row in rows], ["002", "002"])

    def test_one_sku_id_plus_one_barcode_stays_a_single_item(self) -> None:
        # 不同字段各一列是同一药品的两种标识，不能当宽表拆成两条
        rows, _errors, _columns = self.parse_full(
            "SKU ID,69码\nsku-9,699999\n"
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku_id"], "sku-9")
        self.assertEqual(rows[0]["sku_code"], "699999")

    def test_fullwidth_header_and_value_are_matched(self) -> None:
        rows, errors, _columns = self.parse_full("\uff16\uff19码\n\uff16\uff19\uff10\uff10\uff10\uff11\n")

        self.assertEqual(errors, [])
        self.assertEqual([row["sku_code"] for row in rows], ["690001"])


class TestOrderViewSchemeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temporary.name) / "test-order-state.json"
        self.state_patch = patch.object(test_order_api, "STATE_FILE", self.state_file)
        self.state_patch.start()
        self.packaging_patch = patch.object(
            test_order_api, "_candidate_packaging_choices", return_value=["全部"]
        )
        self.packaging_patch.start()
        self.candidates = [
            candidate("OUT-1", "010203", "690001"),
            candidate("OUT-2", "010204", "690002"),
        ]
        loader_patches = [
            patch.object(test_order_api, "load_candidates", return_value=self.candidates),
            patch.object(test_order_api, "load_unavailable", return_value=set()),
            patch.object(test_order_api, "load_tool_mapping", return_value={}),
            patch.object(test_order_api, "load_small_skus", return_value=set()),
            patch.object(test_order_api, "load_packaging", return_value={}),
        ]
        self.loaders = loader_patches
        for loader in loader_patches:
            loader.start()

    def tearDown(self) -> None:
        for loader in self.loaders:
            loader.stop()
        self.packaging_patch.stop()
        self.state_patch.stop()
        self.temporary.cleanup()

    def test_legacy_state_falls_back_to_default_columns(self) -> None:
        state = test_order_api._empty_state()
        state["pending"] = [candidate("OUT-1", "010203", "690001")]
        test_order_api._save_state(state)

        result = test_order_api.get_state()

        self.assertEqual(result["group_mode"], "raw")
        self.assertEqual(result["group_field"], "")
        self.assertEqual(
            [column["key"] for column in result["columns"]],
            [column["key"] for column in DEFAULT_COLUMNS],
        )
        # 生成行没有 display 原始值时，按规范字段回退合成
        self.assertEqual(result["pending"][0]["display"]["out_item_id"], "OUT-1")
        self.assertEqual(result["pending"][0]["display"]["sku_code"], "690001")

    def test_group_import_persists_scheme_and_raw_display(self) -> None:
        result = test_order_api.import_csv(
            {
                "csv": "批次,69码\nA01,690001\nA01,690002\n",
                "mode": "group",
                "group_field": "批次",
            }
        )

        self.assertEqual(result["group_mode"], "group")
        self.assertEqual(result["group_field"], "批次")
        self.assertEqual(
            [column["label"] for column in result["columns"]], ["批次", "69码"]
        )
        self.assertEqual(
            [row["group_id"] for row in result["pending"]], ["A01", "A01"]
        )
        self.assertEqual(result["pending"][0]["display"]["批次"], "A01")

        # 重新读取状态文件，方案仍然保持
        reloaded = test_order_api.get_state()
        self.assertEqual(reloaded["group_mode"], "group")
        self.assertEqual(reloaded["group_field"], "批次")

    def test_group_import_requires_group_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "组合字段"):
            test_order_api.import_csv(
                {"csv": "批次,69码\nA01,690001\n", "mode": "group"}
            )

    def test_mark_ordered_keeps_view_scheme(self) -> None:
        test_order_api.import_csv(
            {
                "csv": "批次,69码\nA01,690001\n",
                "mode": "group",
                "group_field": "批次",
            }
        )

        result = test_order_api.mark_ordered({"keys": ["690001|010203|A01"]})

        self.assertEqual(result["group_mode"], "group")
        self.assertEqual(result["group_field"], "批次")
        self.assertEqual(result["ordered"][0]["group_id"], "A01")

    def test_group_import_keeps_cross_group_duplicates(self) -> None:
        result = test_order_api.import_csv(
            {
                "csv": "批次,69码\nA01,690001\nA02,690001\n",
                "mode": "group",
                "group_field": "批次",
            }
        )

        self.assertEqual(len(result["pending"]), 2)
        self.assertEqual(
            [row["group_id"] for row in result["pending"]], ["A01", "A02"]
        )
        self.assertEqual(
            [row["key"] for row in result["pending"]],
            ["690001|010203|A01", "690001|010203|A02"],
        )

        # 按组合下单只移动该组合的副本，另一组合的保留在待下单
        moved = test_order_api.mark_ordered({"keys": ["690001|010203|A02"]})
        self.assertEqual(len(moved["ordered"]), 1)
        self.assertEqual(moved["ordered"][0]["group_id"], "A02")
        self.assertEqual(len(moved["pending"]), 1)
        self.assertEqual(moved["pending"][0]["group_id"], "A01")

    def test_export_uses_dynamic_columns(self) -> None:
        test_order_api.import_csv(
            {"csv": "批次,69码,备注\nA01,690001,加急\n", "mode": "raw"}
        )

        filename, body = test_order_api.export_pending_csv()

        self.assertEqual(filename, "test_order_pending.csv")
        text = body.decode("utf-8-sig")
        header = text.splitlines()[0]
        self.assertEqual(header, "批次,69码,备注")
        self.assertIn("A01,690001,加急", text)

    def test_export_ordered_includes_time_and_order_no(self) -> None:
        test_order_api.import_csv({"csv": "69码\n690001\n", "mode": "raw"})
        test_order_api.mark_ordered(
            {"keys": ["690001|010203"], "order_no": "ORDER-9"}
        )

        _filename, body = test_order_api.export_ordered_csv()

        text = body.decode("utf-8-sig")
        self.assertEqual(text.splitlines()[0], "下单时间,订单号,69码")
        self.assertIn("ORDER-9", text)
        self.assertIn("690001", text)


class DockerLogStreamTests(unittest.TestCase):
    def state(self) -> dict[str, object]:
        state = logs_api._new_follower_state("robot_workspace_move_test")
        state["generation"] = 1
        state["running"] = True
        state["status"] = "streaming"
        state["source"] = "logs"
        return state

    def append(self, state: dict[str, object], index: int, text: str) -> None:
        timestamp = f"2026-08-12T10:00:{index % 60:02d}.{index:09d}Z"
        appended, corrupt = logs_api._append_log_line(
            state, f"{timestamp} {text}\n"
        )
        self.assertTrue(appended)
        self.assertFalse(corrupt)

    def test_snapshot_hides_docker_timestamp_and_preserves_duplicate_lines(self) -> None:
        state = self.state()
        self.append(state, 1, "same line")
        self.append(state, 2, "same line")
        with patch.object(logs_api, "_ensure_log_follower", return_value=state):
            events = logs_api.stream_log_events("0", 800, heartbeat_seconds=0.01)
            first = next(events)
            events.close()

        self.assertEqual(first["event"], "snapshot")
        self.assertEqual(first["data"]["lines"], ["same line", "same line"])

    def test_parser_snapshot_keeps_timestamp_without_running_new_docker_command(self) -> None:
        state = self.state()
        self.append(state, 1, "[INFO] start process object")
        with (
            patch.object(logs_api, "_ensure_log_follower", return_value=state),
            patch.object(logs_api, "inspect_container") as inspect,
            patch.object(logs_api, "_start_follow_process") as start_process,
        ):
            result = logs_api.fetch_logs("0", 10)

        self.assertRegex(result["logs"], r"^2026-08-12T10:00:01.*Z \[INFO\]")
        inspect.assert_not_called()
        start_process.assert_not_called()

    def test_buffer_retains_latest_five_thousand_lines(self) -> None:
        state = self.state()
        for index in range(logs_api._FOLLOW_BUFFER_LINES + 2):
            self.append(state, index, f"line {index}")

        entries = list(state["entries"])
        self.assertEqual(len(entries), logs_api._FOLLOW_BUFFER_LINES)
        self.assertEqual(entries[0]["sequence"], 3)
        self.assertEqual(entries[-1]["display"], "line 5001")

    def test_resume_sends_only_lines_after_event_id(self) -> None:
        state = self.state()
        self.append(state, 1, "first")
        self.append(state, 2, "second")
        with patch.object(logs_api, "_ensure_log_follower", return_value=state):
            events = logs_api.stream_log_events(
                "0", 800, last_event_id="1:1", heartbeat_seconds=0.01
            )
            event = next(events)
            events.close()

        self.assertEqual(event["event"], "line")
        self.assertEqual(event["data"]["line"], "second")
        self.assertEqual(event["id"], "1:2")

    def test_resume_without_missed_lines_sends_immediate_state_handshake(self) -> None:
        state = self.state()
        self.append(state, 1, "first")
        with patch.object(logs_api, "_ensure_log_follower", return_value=state):
            events = logs_api.stream_log_events(
                "0", 800, last_event_id="1:1", heartbeat_seconds=30.0
            )
            event = next(events)
            events.close()

        self.assertEqual(event["event"], "state")
        self.assertEqual(event["id"], "1:1")
        self.assertTrue(event["data"]["running"])

    def test_expired_cursor_receives_fresh_snapshot(self) -> None:
        state = self.state()
        for index in range(5):
            self.append(state, index, f"line {index}")
        state["entries"] = __import__("collections").deque(
            list(state["entries"])[-2:], maxlen=2
        )
        with patch.object(logs_api, "_ensure_log_follower", return_value=state):
            events = logs_api.stream_log_events(
                "0", 800, last_event_id="1:1", heartbeat_seconds=0.01
            )
            event = next(events)
            events.close()

        self.assertEqual(event["event"], "snapshot")
        self.assertEqual(event["data"]["lines"], ["line 3", "line 4"])

    def test_idle_stream_sends_heartbeat_and_close_keeps_shared_follower(self) -> None:
        state = self.state()
        with patch.object(logs_api, "_ensure_log_follower", return_value=state):
            events = logs_api.stream_log_events("0", 10, heartbeat_seconds=0.001)
            self.assertEqual(next(events)["event"], "snapshot")
            self.assertEqual(next(events)["event"], "heartbeat")
            events.close()

        self.assertFalse(state["stop_event"].is_set())

    def test_initial_log_wait_runs_only_once_for_empty_container(self) -> None:
        state = self.state()
        condition = state["condition"]
        with patch.object(condition, "wait", wraps=condition.wait) as wait:
            logs_api._wait_for_initial_entries(state, timeout=0.001)
            logs_api._wait_for_initial_entries(state, timeout=0.001)

        wait.assert_called_once()

    def test_line_arriving_after_snapshot_is_emitted_without_waiting(self) -> None:
        state = self.state()
        with patch.object(logs_api, "_ensure_log_follower", return_value=state):
            events = logs_api.stream_log_events("0", 10, heartbeat_seconds=30.0)
            self.assertEqual(next(events)["event"], "snapshot")
            self.append(state, 1, "arrived after snapshot")
            event = next(events)
            events.close()

        self.assertEqual(event["event"], "line")
        self.assertEqual(event["data"]["line"], "arrived after snapshot")

    def test_notice_and_state_events_are_streamed(self) -> None:
        state = self.state()
        with patch.object(logs_api, "_ensure_log_follower", return_value=state):
            events = logs_api.stream_log_events("0", 10, heartbeat_seconds=30.0)
            self.assertEqual(next(events)["event"], "snapshot")
            logs_api._publish_notice(state, "history damaged")
            state_event = next(events)
            notice_event = next(events)
            events.close()

        self.assertEqual(state_event["event"], "state")
        self.assertTrue(state_event["data"]["degraded"])
        self.assertEqual(notice_event["event"], "notice")
        self.assertEqual(notice_event["data"]["message"], "history damaged")

    def test_sse_encoder_writes_id_event_and_utf8_json_frame(self) -> None:
        frame = logs_api.encode_sse_event(
            {
                "id": "2:9",
                "event": "notice",
                "data": {"message": "历史日志损坏"},
            }
        ).decode("utf-8")

        self.assertEqual(
            frame,
            'id: 2:9\nevent: notice\ndata: {"message":"历史日志损坏"}\n\n',
        )

    def test_http_chunk_encoder_wraps_sse_frame(self) -> None:
        payload = "data: 中文\n\n".encode("utf-8")
        chunk = logs_api.encode_http_chunk(payload)

        size, body = chunk.split(b"\r\n", 1)
        self.assertEqual(int(size, 16), len(payload))
        self.assertEqual(body, payload + b"\r\n")

    def test_raw_attached_line_gets_internal_timestamp_only(self) -> None:
        state = self.state()
        appended, corrupt = logs_api._append_log_line(
            state, "[INFO] start process object\n"
        )
        entry = state["entries"][0]

        self.assertTrue(appended)
        self.assertFalse(corrupt)
        self.assertEqual(entry["display"], "[INFO] start process object")
        self.assertRegex(
            entry["parser"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*Z \[INFO\]",
        )

    def test_nul_is_removed_and_switches_to_tail_zero(self) -> None:
        state = self.state()

        class Process:
            stdout = iter(["2026-08-12T10:00:00.000000001Z a\x00b\n"])

            def terminate(self) -> None:
                return

            def wait(self) -> int:
                return 1

        return_code, corrupt, appended = logs_api._consume_follow_process(
            state, Process()
        )

        self.assertEqual((return_code, corrupt, appended), (1, True, 1))
        self.assertEqual(state["entries"][0]["display"], "ab")
        self.assertEqual(
            logs_api._next_follow_source("logs", 1, True, 1, True),
            "tail0",
        )

    def test_failed_tail_zero_falls_back_to_attach(self) -> None:
        self.assertEqual(
            logs_api._next_follow_source("tail0", 1, False, 0, True),
            "attach",
        )
        self.assertEqual(
            logs_api._follow_command("robot", "attach"),
            ["docker", "attach", "--no-stdin", "--sig-proxy=false", "robot"],
        )

    def test_follow_commands_cover_initial_resume_and_tail_zero(self) -> None:
        self.assertEqual(
            logs_api._follow_command("robot", "logs"),
            [
                "docker",
                "logs",
                "--follow",
                "--tail",
                "2500",
                "--timestamps",
                "robot",
            ],
        )
        self.assertEqual(
            logs_api._follow_command(
                "robot", "resume", "2026-08-12T10:00:00.000000001Z"
            ),
            [
                "docker",
                "logs",
                "--follow",
                "--since",
                "2026-08-12T10:00:00.000000001Z",
                "--timestamps",
                "robot",
            ],
        )
        self.assertEqual(
            logs_api._next_follow_source("resume", 1, True, 0, True),
            "resume_tail",
        )
        self.assertEqual(
            logs_api._follow_command(
                "robot", "resume_tail", "2026-08-12T10:00:00.000000001Z"
            ),
            [
                "docker",
                "logs",
                "--follow",
                "--tail",
                str(logs_api._FOLLOW_BUFFER_LINES),
                "--timestamps",
                "robot",
            ],
        )
        self.assertIn("0", logs_api._follow_command("robot", "tail0"))

    def test_attach_cleans_nul_without_restarting_itself(self) -> None:
        state = self.state()

        class Process:
            stdout = iter(["attached\x00line\n"])

            def terminate(self) -> None:
                raise AssertionError("attach should not terminate for a cleaned NUL")

            def wait(self) -> int:
                return 0

        result = logs_api._consume_follow_process(
            state, Process(), detect_corruption=False
        )

        self.assertEqual(result, (0, False, 1))
        self.assertEqual(state["entries"][0]["display"], "attachedline")

    def test_shutdown_stops_process_and_wakes_stream_waiters(self) -> None:
        state = self.state()
        process = Mock(spec=__import__("subprocess").Popen)
        process.poll.return_value = None
        state["process"] = process
        with patch.object(logs_api, "_FOLLOWERS", {"robot": state}):
            logs_api._stop_log_followers()

        self.assertTrue(state["stop_event"].is_set())
        process.terminate.assert_called_once_with()

    def test_supervisor_reconnects_with_since_after_process_exit(self) -> None:
        state = self.state()
        state["generation"] = 0
        state["running"] = False
        commands: list[list[str]] = []

        class Process:
            def __init__(self, lines: list[str], stop_after: bool = False) -> None:
                self.stdout = iter(lines)
                self.stop_after = stop_after

            def wait(self) -> int:
                if self.stop_after:
                    state["stop_event"].set()
                return 1

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                return

        processes = iter(
            [
                Process(["2026-08-12T10:00:00.000000001Z first\n"]),
                Process([], stop_after=True),
            ]
        )

        def start(command: list[str]) -> Process:
            commands.append(command)
            return next(processes)

        with (
            patch.object(
                logs_api,
                "inspect_container",
                return_value={"running": True, "status": "running", "message": ""},
            ),
            patch.object(logs_api, "_start_follow_process", side_effect=start),
            patch.object(logs_api, "_FOLLOW_RETRY_DELAYS", (0.0, 0.0, 0.0)),
        ):
            logs_api._follow_supervisor("robot", state)

        self.assertEqual(commands[0], logs_api._follow_command("robot", "logs"))
        self.assertEqual(
            commands[1],
            logs_api._follow_command(
                "robot", "resume", "2026-08-12T10:00:00.000000001Z"
            ),
        )
        self.assertEqual(state["entries"][0]["display"], "first")


if __name__ == "__main__":
    unittest.main()
