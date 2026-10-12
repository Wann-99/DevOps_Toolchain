from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ksq import state_reset


# ---------------------------------------------------------------------------
# get_app_version
# ---------------------------------------------------------------------------


class GetAppVersionTests(unittest.TestCase):
    """Tests for :func:`ksq.state_reset.get_app_version`."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.build_dir = self.tmp / "build"
        self.build_dir.mkdir()
        self.patches = [
            patch("ksq.state_reset.SOURCE_APP_DIRECTORY", self.build_dir),
            patch("ksq.state_reset.APP_VERSION", "dev"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self._tmpdir.cleanup()

    def test_reads_version_from_build_json(self) -> None:
        (self.build_dir / "KSQ_BUILD.json").write_text(
            json.dumps({"version": "v3.1.4", "format": 1}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(state_reset.get_app_version(), "v3.1.4")

    def test_falls_back_when_no_build_json(self) -> None:
        self.assertEqual(state_reset.get_app_version(), "dev")

    def test_falls_back_when_build_json_invalid(self) -> None:
        (self.build_dir / "KSQ_BUILD.json").write_text(
            "not valid json{", encoding="utf-8"
        )
        self.assertEqual(state_reset.get_app_version(), "dev")

    def test_falls_back_when_version_empty(self) -> None:
        (self.build_dir / "KSQ_BUILD.json").write_text(
            json.dumps({"version": "  "}) + "\n", encoding="utf-8"
        )
        self.assertEqual(state_reset.get_app_version(), "dev")


class UpgradePreservesStateTests(unittest.TestCase):
    def test_version_changes_and_first_start_preserve_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "dashboard_settings.json"
            files = [root / name for name in ("test_order_state.json", "dashboard_active_order.json",
                                              "order_config.json", "order_config.prod.json", "users.json")]
            for path in files:
                path.write_bytes(b'{"configured": true, "value": "keep-me"}\n')
            original = {path: path.read_bytes() for path in files}
            for marker in (None, "v1.0.0", "v2.0.0"):
                payload = {"mode": "prod", "feishu": {"enabled": True}}
                if marker:
                    payload["_app_version_marker"] = marker
                settings.write_text(json.dumps(payload))
                with patch.object(state_reset, "DASHBOARD_SETTINGS_FILE", settings), patch.object(state_reset, "get_app_version", return_value="v2.0.0"):
                    changed = state_reset.reset_state_if_version_changed()
                self.assertEqual(changed, marker != "v2.0.0")
                self.assertEqual({path: path.read_bytes() for path in files}, original)
                self.assertEqual(json.loads(settings.read_text()), {**payload, "_app_version_marker": "v2.0.0"})
            self.assertFalse((root / ".backup").exists())

    def test_invalid_settings_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "dashboard_settings.json"
            for original in (b'{broken', b'[]'):
                settings.write_bytes(original)
                with patch.object(state_reset, "DASHBOARD_SETTINGS_FILE", settings), patch.object(state_reset, "get_app_version", return_value="v2.0.0"), patch.object(state_reset.LOGGER, "exception"):
                    self.assertFalse(state_reset.reset_state_if_version_changed())
                self.assertEqual(settings.read_bytes(), original)


# ---------------------------------------------------------------------------
# save_dashboard_settings marker preservation
# ---------------------------------------------------------------------------


class SaveDashboardSettingsMarkerPreservationTests(unittest.TestCase):
    """Verify save_dashboard_settings preserves _app_version_marker."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.settings_file = self.tmp / "dashboard_settings.json"
        self.keyboard_env_file = self.tmp / "robot_keyboard.env"

        self.patches = [
            patch(
                "ksq.dashboard.settings.DASHBOARD_SETTINGS_FILE",
                self.settings_file,
            ),
            patch(
                "ksq.dashboard.settings.ROBOT_KEYBOARD_ENV_FILE",
                self.keyboard_env_file,
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self._tmpdir.cleanup()

    def test_marker_preserved_across_settings_save(self) -> None:
        from ksq.dashboard import service as dashboard_api

        initial = {
            "keyboard_device": "/dev/input/event1",
            "mode": "test",
            "etm_base_url": "http://127.0.0.1:12005",
            "auto_confirm": False,
            "feishu": {"enabled": False},
            "_app_version_marker": "v2.5.0",
        }
        self.settings_file.write_text(
            json.dumps(initial, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        dashboard_api.save_dashboard_settings(
            {"auto_confirm": True}, restart_robot=False
        )

        saved = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("_app_version_marker"), "v2.5.0")
        self.assertTrue(saved.get("auto_confirm"))

    def test_no_marker_added_when_absent(self) -> None:
        from ksq.dashboard import service as dashboard_api

        initial = {
            "keyboard_device": "/dev/input/event1",
            "mode": "test",
            "etm_base_url": "http://127.0.0.1:12005",
            "auto_confirm": False,
            "feishu": {"enabled": False},
        }
        self.settings_file.write_text(
            json.dumps(initial, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        dashboard_api.save_dashboard_settings(
            {"auto_confirm": True}, restart_robot=False
        )

        saved = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertNotIn("_app_version_marker", saved)


if __name__ == "__main__":
    unittest.main()
