from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ksq.feishu import client as feishu_client
from ksq.web import dashboard_api


class FeishuAcknowledgementTests(unittest.TestCase):
    def test_record_creation_requires_record_id(self) -> None:
        with (
            patch.object(feishu_client, "get_tenant_access_token", return_value="token"),
            patch.object(
                feishu_client,
                "_request_json",
                return_value=(200, {"code": 0, "data": {}}),
            ),
            self.assertRaises(feishu_client.FeishuApiError),
        ):
            feishu_client.create_bitable_record(
                "app",
                "secret",
                "token-id",
                "table",
                {"field": "value"},
            )


class DashboardRecoverySafetyTests(unittest.TestCase):
    def test_old_log_cannot_recover_a_new_queue_lock(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(
            seconds=dashboard_api._LOG_ORDER_RECOVERY_MAX_AGE_SECONDS + 1
        )
        seen = {"task-old": old}
        self.assertFalse(dashboard_api._has_recent_log_activity(seen, datetime.now(timezone.utc)))

    def test_untimestamped_log_cannot_recover_a_queue_lock(self) -> None:
        self.assertFalse(dashboard_api._has_recent_log_activity({}))


class DashboardSettingsSafetyTests(unittest.TestCase):
    def test_malformed_settings_fall_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "dashboard_settings.json"
            settings_file.write_text("{bad", encoding="utf-8")
            with (
                patch.object(dashboard_api, "DASHBOARD_SETTINGS_FILE", settings_file),
                patch.object(
                    dashboard_api,
                    "ROBOT_KEYBOARD_ENV_FILE",
                    Path(directory) / "missing.env",
                ),
            ):
                settings = dashboard_api.load_dashboard_settings()
        self.assertEqual(settings["mode"], "test")
        self.assertEqual(settings["keyboard_device"], "/dev/input/event1")

    def test_auto_confirm_rejects_string_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "dashboard_settings.json"
            env_file = Path(directory) / "keyboard.env"
            with (
                patch.object(dashboard_api, "DASHBOARD_SETTINGS_FILE", settings_file),
                patch.object(dashboard_api, "ROBOT_KEYBOARD_ENV_FILE", env_file),
            ):
                with self.assertRaisesRegex(ValueError, "auto_confirm"):
                    dashboard_api.save_dashboard_settings(
                        {"auto_confirm": "false"}, restart_robot=False
                    )

    def test_invalid_persisted_auto_confirm_defaults_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "dashboard_settings.json"
            settings_file.write_text('{"auto_confirm": "false"}', encoding="utf-8")
            with (
                patch.object(dashboard_api, "DASHBOARD_SETTINGS_FILE", settings_file),
                patch.object(
                    dashboard_api,
                    "ROBOT_KEYBOARD_ENV_FILE",
                    Path(directory) / "missing.env",
                ),
            ):
                settings = dashboard_api.load_dashboard_settings()
        self.assertFalse(settings["auto_confirm"])

    def test_invalid_persisted_feishu_booleans_default_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "dashboard_settings.json"
            settings_file.write_text(
                '{"feishu": {"enabled": "false", "ai": {"enabled": "false"}}}',
                encoding="utf-8",
            )
            with (
                patch.object(dashboard_api, "DASHBOARD_SETTINGS_FILE", settings_file),
                patch.object(
                    dashboard_api,
                    "ROBOT_KEYBOARD_ENV_FILE",
                    Path(directory) / "missing.env",
                ),
            ):
                settings = dashboard_api.load_dashboard_settings()
        self.assertFalse(settings["feishu"]["enabled"])
        self.assertFalse(settings["feishu"]["ai"]["enabled"])

    def test_feishu_boolean_strings_are_rejected_on_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "dashboard_settings.json"
            env_file = Path(directory) / "keyboard.env"
            with (
                patch.object(dashboard_api, "DASHBOARD_SETTINGS_FILE", settings_file),
                patch.object(dashboard_api, "ROBOT_KEYBOARD_ENV_FILE", env_file),
            ):
                with self.assertRaisesRegex(ValueError, "feishu.enabled"):
                    dashboard_api.save_dashboard_settings(
                        {"feishu": {"enabled": "false"}}, restart_robot=False
                    )
                with self.assertRaisesRegex(ValueError, "feishu.ai.enabled"):
                    dashboard_api.save_dashboard_settings(
                        {"feishu": {"ai": {"enabled": "false"}}},
                        restart_robot=False,
                    )

    def test_malformed_feishu_objects_are_rejected_on_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "dashboard_settings.json"
            env_file = Path(directory) / "keyboard.env"
            with (
                patch.object(dashboard_api, "DASHBOARD_SETTINGS_FILE", settings_file),
                patch.object(dashboard_api, "ROBOT_KEYBOARD_ENV_FILE", env_file),
            ):
                with self.assertRaisesRegex(ValueError, "feishu 必须"):
                    dashboard_api.save_dashboard_settings(
                        {"feishu": "false"}, restart_robot=False
                    )
                with self.assertRaisesRegex(ValueError, "feishu.ai 必须"):
                    dashboard_api.save_dashboard_settings(
                        {"feishu": {"ai": "false"}}, restart_robot=False
                    )


if __name__ == "__main__":
    unittest.main()
