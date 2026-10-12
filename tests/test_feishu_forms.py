"""Native-form completion prompts, using temporary state and no Feishu requests."""

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from ksq.dashboard import service, settings
from ksq.feishu import forms
from ksq.order import active, model, store


FORM = {"id": "test-form", "name": "机器人测试", "url": "https://example.feishu.cn/share/base/form/shrExample"}
CONFIG = {"enabled": True, "forms": [FORM], "selected_form": FORM["id"]}


class NativeFeishuFormsTests(unittest.TestCase):
    def test_native_links_need_no_app_credentials_and_reject_unsafe_urls(self):
        normalized = settings._normalize_feishu_settings(CONFIG, None, strict=True)
        self.assertEqual(forms.selected_form(normalized), FORM)
        standard_port = FORM["url"].replace("feishu.cn/", "feishu.cn:443/")
        self.assertEqual(settings._normalize_feishu_page_url(standard_port), standard_port)
        legacy = settings._normalize_feishu_forms([
            {"name": "原表单", "app_token": "sample-token", "table_id": "sample-table"},
        ])
        self.assertIn("/base/sample-token?table=sample-table", legacy[0]["url"])
        for url in (
            "javascript:alert(1)", "http://example.feishu.cn/share/base/form/shrExample",
            "https://feishu.cn.evil.test/share/base/form/shrExample",
            "https://example.feishu.cn@evil.test/share/base/form/shrExample",
            "https://example.feishu.cn:8443/share/base/form/shrExample",
            "https://example.feishu.cn/arm/login",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                settings._normalize_feishu_forms([{**FORM, "url": url}], strict=True)

    def test_completion_prompts_survive_new_orders_restart_and_dismissal(self):
        order = model._build_active_order({"task_id": "new", "order_no": "NEW", "items": []})
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(store, "DASHBOARD_ACTIVE_ORDER_FILE", Path(temporary) / "active.json"),
            patch.object(store, "_ACTIVE_ORDER", order),
            patch.object(store, "_ACTIVE_ORDER_LOADED", True),
            patch.object(settings, "load_dashboard_settings", return_value={"mode": "test", "feishu": CONFIG}),
            patch("ksq.feishu.client.create_bitable_record") as submit,
        ):
            tasks = [
                {"task_id": "new", "order_no": "NEW", "status": "pending"},
                {"task_id": "old", "order_no": "OLD", "status": "running"},
                {"task_id": "history", "order_no": "HISTORY", "status": "success"},
            ]
            forms.observe_completed_orders(tasks, "test", CONFIG)
            self.assertEqual(forms.form_status()["pending"], [])
            tasks[1]["status"] = "awaiting_pack"
            forms.observe_completed_orders(tasks, "test", CONFIG)
            self.assertEqual(forms.form_status()["pending"], [])
            tasks[1]["status"] = "manual_claimed_in_progress"
            forms.observe_completed_orders(tasks, "test", CONFIG)
            self.assertEqual(forms.form_status()["pending"], [])
            tasks[1]["status"] = "manual_claimed_completed"
            forms.observe_completed_orders(tasks, "test", CONFIG)
            pending = forms.form_status()["pending"]
            self.assertEqual([item["order_no"] for item in pending], ["OLD"])
            self.assertEqual(pending[0]["form"], FORM)
            for _ in range(3):
                forms.observe_completed_orders(tasks, "test", CONFIG)
            self.assertEqual(forms.form_status()["pending"], pending)
            tasks[0]["status"] = "success"
            forms.observe_completed_orders(tasks, "test", CONFIG)
            self.assertEqual([item["order_no"] for item in forms.form_status()["pending"]], ["OLD", "NEW"])
            active.set_active_order({"task_id": "later", "items": []})
            store._ACTIVE_ORDER = None
            store._ACTIVE_ORDER_LOADED = False
            self.assertEqual(len(forms.form_status()["pending"]), 2)
            self.assertTrue(forms.dismiss_form("test:old")["ok"])
            forms.observe_completed_orders(tasks, "test", CONFIG)
            self.assertEqual([item["order_no"] for item in forms.form_status()["pending"]], ["NEW"])
            self.assertNotIn("feishu_forms", service._public_order(active.get_active_order())["ui"])
            self.assertEqual(service.preview_feishu_submission()["form"], FORM)
            with self.assertRaisesRegex(ValueError, "原表单"):
                service.submit_feishu_manual()
            submit.assert_not_called()

    def test_disabled_and_other_modes_do_not_enqueue_finished_orders(self):
        order = model._build_active_order({"task_id": "new", "items": []})
        with (
            patch.object(store, "_ACTIVE_ORDER", order),
            patch.object(store, "_ACTIVE_ORDER_LOADED", True),
            patch.object(store, "_save_active_order_unlocked") as save,
            patch.object(settings, "load_dashboard_settings", return_value={"mode": "test", "feishu": CONFIG}),
        ):
            tasks = [{"task_id": "new", "status": "running"}]
            forms.observe_completed_orders(tasks, "test", {**CONFIG, "enabled": False})
            save.assert_not_called()
            forms.observe_completed_orders(tasks, "prod", CONFIG)
            tasks[0]["status"] = "cancel"
            forms.observe_completed_orders(tasks, "prod", CONFIG)
            self.assertEqual(forms.form_status()["pending"], [])
            with patch.object(settings, "load_dashboard_settings", return_value={"mode": "prod", "feishu": CONFIG}):
                self.assertEqual(len(forms.form_status()["pending"]), 1)

    def test_queue_promotion_keeps_form_prompts_and_the_new_order_ui(self):
        order = model._build_active_order({"task_id": "finished", "items": []})
        next_order = model._build_active_order({"task_id": "next", "items": []})
        state = {"mode": "test", "pending": [{"id": "test:finished", "form": FORM}]}
        order["ui"]["feishu_forms"] = state
        order["ui"]["dismissed_fingerprint"] = "old-keyboard-prompt"
        order["queued_orders"] = [next_order]
        with (
            patch.object(store, "_ACTIVE_ORDER", order),
            patch.object(store, "_ACTIVE_ORDER_LOADED", True),
            patch.object(store, "_save_active_order_unlocked"),
            patch.object(model, "_order_is_queue_terminal", return_value=True),
            patch.object(model, "_order_has_pending_confirmation", return_value=False),
        ):
            self.assertTrue(active.promote_queued_order_if_ready())
            promoted = active.get_active_order()
            self.assertEqual(promoted["ui"]["feishu_forms"], state)
            self.assertIsNot(promoted["ui"]["feishu_forms"], state)
            self.assertEqual(promoted["ui"]["dismissed_fingerprint"], next_order["ui"]["dismissed_fingerprint"])


if __name__ == "__main__":
    unittest.main()
