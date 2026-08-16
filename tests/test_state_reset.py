from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ksq import state_reset
from ksq.order.config import DEFAULT_ORDER_CONFIG


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


# ---------------------------------------------------------------------------
# reset_state_if_version_changed
# ---------------------------------------------------------------------------


class ResetStateIfVersionChangedTests(unittest.TestCase):
    """Tests for :func:`ksq.state_reset.reset_state_if_version_changed`."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

        self.settings_file = self.tmp / "dashboard_settings.json"
        self.test_order_file = self.tmp / "test_order_state.json"
        self.active_order_file = self.tmp / "dashboard_active_order.json"
        self.order_config_file = self.tmp / "order_config.json"
        self.order_config_prod_file = self.tmp / "order_config.prod.json"
        self.build_dir = self.tmp / "build"
        self.build_dir.mkdir()

        self.reset_files = (
            self.test_order_file,
            self.active_order_file,
            self.order_config_file,
            self.order_config_prod_file,
        )

        self.patches = [
            patch("ksq.state_reset.DASHBOARD_SETTINGS_FILE", self.settings_file),
            patch("ksq.state_reset.TEST_ORDER_STATE_FILE", self.test_order_file),
            patch(
                "ksq.state_reset.DASHBOARD_ACTIVE_ORDER_FILE",
                self.active_order_file,
            ),
            patch("ksq.state_reset.ORDER_CONFIG_FILE", self.order_config_file),
            patch(
                "ksq.state_reset.ORDER_CONFIG_PROD_FILE",
                self.order_config_prod_file,
            ),
            patch("ksq.state_reset._RESET_FILES", self.reset_files),
            patch("ksq.state_reset.SOURCE_APP_DIRECTORY", self.build_dir),
            patch("ksq.state_reset.APP_VERSION", "dev"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self._tmpdir.cleanup()

    # -- helpers --

    def _write_json(self, path: Path, data: dict) -> None:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _seed_stale_state(self) -> None:
        """Populate the four reset-target files with recognisable stale data."""
        self._write_json(self.test_order_file, {"stale": "test_order"})
        self._write_json(self.active_order_file, {"stale": "active_order"})
        self._write_json(
            self.order_config_file,
            {"server": "https://old.example.com", "client_id": "old"},
        )
        self._write_json(
            self.order_config_prod_file,
            {"server": "https://old.prod.example.com"},
        )

    def _seed_settings(self, marker: str | None = None) -> None:
        data: dict[str, object] = {
            "keyboard_device": "/dev/input/event3",
            "mode": "prod",
            "etm_base_url": "http://10.0.0.1:12005",
            "auto_confirm": True,
            "feishu": {"enabled": True, "site": "test_site"},
        }
        if marker is not None:
            data["_app_version_marker"] = marker
        self._write_json(self.settings_file, data)

    # -- required test cases --

    def test_version_change_triggers_reset(self) -> None:
        """Version change → reset 4 files, preserve settings, backup, marker."""
        self._seed_stale_state()
        self._seed_settings(marker="v1.0.0")

        with patch("ksq.state_reset.get_app_version", return_value="v2.0.0"):
            result = state_reset.reset_state_if_version_changed()

        self.assertTrue(result)

        # Four files reset to clean defaults.
        self.assertEqual(self._read_json(self.test_order_file), {})
        self.assertEqual(self._read_json(self.active_order_file), {})
        self.assertEqual(
            self._read_json(self.order_config_file), DEFAULT_ORDER_CONFIG
        )
        self.assertEqual(
            self._read_json(self.order_config_prod_file), DEFAULT_ORDER_CONFIG
        )

        # dashboard_settings preserved; marker updated.
        settings = self._read_json(self.settings_file)
        self.assertEqual(settings["keyboard_device"], "/dev/input/event3")
        self.assertEqual(settings["mode"], "prod")
        self.assertEqual(settings["feishu"]["enabled"], True)
        self.assertEqual(settings["_app_version_marker"], "v2.0.0")

        # Backup directory created with copies of the 4 original files.
        backup_dirs = list((self.tmp / ".backup").iterdir())
        self.assertEqual(len(backup_dirs), 1)
        backup_names = {f.name for f in backup_dirs[0].iterdir()}
        self.assertEqual(
            backup_names,
            {
                "test_order_state.json",
                "dashboard_active_order.json",
                "order_config.json",
                "order_config.prod.json",
            },
        )
        # Backup content matches the stale data.
        backed_up = json.loads(
            (backup_dirs[0] / "test_order_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(backed_up, {"stale": "test_order"})

    def test_version_same_skips_reset(self) -> None:
        """Version unchanged → no reset, no backup."""
        self._seed_stale_state()
        self._seed_settings(marker="v2.0.0")

        with patch("ksq.state_reset.get_app_version", return_value="v2.0.0"):
            result = state_reset.reset_state_if_version_changed()

        self.assertFalse(result)
        # Files unchanged.
        self.assertEqual(
            self._read_json(self.test_order_file), {"stale": "test_order"}
        )
        self.assertEqual(
            self._read_json(self.active_order_file), {"stale": "active_order"}
        )
        # No backup created.
        self.assertFalse((self.tmp / ".backup").exists())

    def test_new_deployment_no_marker_triggers_reset(self) -> None:
        """Missing marker (new deployment) → reset and set marker."""
        self._seed_stale_state()
        self._seed_settings(marker=None)

        with patch("ksq.state_reset.get_app_version", return_value="v1.0.0"):
            result = state_reset.reset_state_if_version_changed()

        self.assertTrue(result)
        # Four files reset.
        self.assertEqual(self._read_json(self.test_order_file), {})
        self.assertEqual(self._read_json(self.active_order_file), {})
        # Marker set; other settings preserved.
        settings = self._read_json(self.settings_file)
        self.assertEqual(settings["_app_version_marker"], "v1.0.0")
        self.assertEqual(settings["keyboard_device"], "/dev/input/event3")

    def test_dashboard_settings_content_preserved_except_marker(self) -> None:
        """All dashboard_settings fields (except marker) remain untouched."""
        self._seed_stale_state()
        original: dict[str, object] = {
            "keyboard_device": "/dev/input/event5",
            "mode": "test",
            "etm_base_url": "http://192.168.1.1:12005",
            "auto_confirm": True,
            "feishu": {"enabled": False, "site": "abc"},
            "_app_version_marker": "v1.0.0",
        }
        self._write_json(self.settings_file, original)

        with patch("ksq.state_reset.get_app_version", return_value="v2.0.0"):
            state_reset.reset_state_if_version_changed()

        settings = self._read_json(self.settings_file)
        for key in (
            "keyboard_device",
            "mode",
            "etm_base_url",
            "auto_confirm",
            "feishu",
        ):
            self.assertEqual(settings[key], original[key])
        self.assertEqual(settings["_app_version_marker"], "v2.0.0")

    def test_exception_does_not_block_startup(self) -> None:
        """Any exception is swallowed; startup is never blocked."""
        with patch(
            "ksq.state_reset.get_app_version",
            side_effect=RuntimeError("simulated failure"),
        ):
            result = state_reset.reset_state_if_version_changed()

        self.assertFalse(result)

    def test_reset_then_restart_does_not_reset_again(self) -> None:
        """After a reset, a second call with the same version skips."""
        self._seed_stale_state()
        self._seed_settings(marker="v1.0.0")

        with patch("ksq.state_reset.get_app_version", return_value="v2.0.0"):
            result1 = state_reset.reset_state_if_version_changed()
        self.assertTrue(result1)

        # Re-introduce stale data to prove it is NOT reset on the second call.
        self._seed_stale_state()

        with patch("ksq.state_reset.get_app_version", return_value="v2.0.0"):
            result2 = state_reset.reset_state_if_version_changed()
        self.assertFalse(result2)

        # Stale data remains (not reset).
        self.assertEqual(
            self._read_json(self.test_order_file), {"stale": "test_order"}
        )

    def test_missing_settings_file_treated_as_new_deployment(self) -> None:
        """No dashboard_settings.json → reset and create file with marker."""
        self._seed_stale_state()
        # Deliberately do NOT create the settings file.

        with patch("ksq.state_reset.get_app_version", return_value="v1.2.3"):
            result = state_reset.reset_state_if_version_changed()

        self.assertTrue(result)
        settings = self._read_json(self.settings_file)
        self.assertEqual(settings.get("_app_version_marker"), "v1.2.3")


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
                "ksq.web.dashboard_api.DASHBOARD_SETTINGS_FILE",
                self.settings_file,
            ),
            patch(
                "ksq.web.dashboard_api.ROBOT_KEYBOARD_ENV_FILE",
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
        from ksq.web import dashboard_api

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
        from ksq.web import dashboard_api

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
