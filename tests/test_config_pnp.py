from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ksq import config_pnp
from ksq.web import state

# A representative config.py excerpt matching the device format.
CONFIG_PY_CONTENT = """\
config.scene.obj_tool_mapping = config_pnp_path("obj_tool_mapping.json")
config.scene.unavailable_obj = config_pnp_path("unavailable_obj.json")
config.scene.pick_strategy_obj = config_pnp_path("pick_strategy_obj.json")
config.scene.real_location_code_mapping = ""
config.scene.obj_baffle = config_pnp_path("obj_baffle.json")
config.scene.sku_shelf_export_csv = config_pnp_path("sku-shelves_20260812.csv")
config.visualize_robot_base = "odjist"
"""


class LoadConfigPnpPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, content: str) -> Path:
        (self.directory / "config.py").write_text(content, encoding="utf-8")
        return self.directory

    # ------------------------------------------------------------------
    # Standard parsing & key mapping
    # ------------------------------------------------------------------

    def test_parses_full_config_py_and_maps_keys(self) -> None:
        self.write_config(CONFIG_PY_CONTENT)
        result = config_pnp.load_config_pnp_paths(self.directory)

        self.assertEqual(
            result["shelves"],
            (self.directory / "sku-shelves_20260812.csv").resolve(),
        )
        self.assertEqual(
            result["tool_mapping"],
            (self.directory / "obj_tool_mapping.json").resolve(),
        )
        self.assertEqual(
            result["unavailable"],
            (self.directory / "unavailable_obj.json").resolve(),
        )
        self.assertEqual(
            result["pick_strategy"],
            (self.directory / "pick_strategy_obj.json").resolve(),
        )
        # Only the four KSQ keys are returned.
        self.assertEqual(
            set(result), {"shelves", "tool_mapping", "unavailable", "pick_strategy"}
        )

    def test_partial_config_py_only_returns_set_keys(self) -> None:
        self.write_config(
            'config.scene.sku_shelf_export_csv = config_pnp_path("my-shelves.csv")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(set(result), {"shelves"})
        self.assertEqual(
            result["shelves"], (self.directory / "my-shelves.csv").resolve()
        )

    def test_key_not_in_scene_map_is_ignored(self) -> None:
        """Keys not in SCENE_KEY_MAP (e.g. obj_baffle) are silently ignored."""
        self.write_config(
            'config.scene.obj_baffle = config_pnp_path("obj_baffle.json")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(result, {})

    # ------------------------------------------------------------------
    # Non-matching value patterns are skipped (no crash, no phantom key)
    # ------------------------------------------------------------------

    def test_empty_string_value_is_skipped(self) -> None:
        self.write_config(
            'config.scene.real_location_code_mapping = ""\n'
            'config.scene.sku_shelf_export_csv = config_pnp_path("ok.csv")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        # real_location_code_mapping is not in SCENE_KEY_MAP anyway, but the
        # empty string must not cause a crash or phantom key.
        self.assertEqual(set(result), {"shelves"})

    def test_non_path_value_is_skipped(self) -> None:
        self.write_config(
            'config.scene.obj_tool_mapping = 42\n'
            'config.scene.sku_shelf_export_csv = config_pnp_path("ok.csv")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(set(result), {"shelves"})

    def test_string_literal_value_is_skipped(self) -> None:
        """A bare string literal (not a config_pnp_path call) is skipped."""
        self.write_config(
            'config.scene.sku_shelf_export_csv = "custom-shelves.csv"\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(result, {})

    def test_bare_name_value_is_skipped(self) -> None:
        """A bare name (e.g. an undefined variable) is skipped, not crashed."""
        self.write_config(
            'config.scene.sku_shelf_export_csv = odjist\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(result, {})

    def test_non_config_pnp_path_call_is_skipped(self) -> None:
        """A call to a function other than config_pnp_path is skipped."""
        self.write_config(
            'config.scene.sku_shelf_export_csv = other_func("ok.csv")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(result, {})

    def test_non_literal_argument_is_skipped(self) -> None:
        """config_pnp_path(variable) — a non-string-literal arg is skipped."""
        self.write_config(
            'name = "ok.csv"\n'
            'config.scene.sku_shelf_export_csv = config_pnp_path(name)\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(result, {})

    # ------------------------------------------------------------------
    # Attribute-style call variants: config.pnp_path(), cfg.config_pnp_path()
    # ------------------------------------------------------------------

    def test_attribute_style_pnp_path_is_parsed(self) -> None:
        """config.pnp_path("xxx") — attribute-style call is recognised."""
        self.write_config(
            'config.scene.sku_shelf_export_csv = config.pnp_path("attr-shelves.csv")\n'
            'config.scene.obj_tool_mapping = config.pnp_path("attr-mapping.json")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(
            result["shelves"],
            (self.directory / "attr-shelves.csv").resolve(),
        )
        self.assertEqual(
            result["tool_mapping"],
            (self.directory / "attr-mapping.json").resolve(),
        )

    def test_attribute_style_config_pnp_path_is_parsed(self) -> None:
        """config.scene.x = cfg.config_pnp_path("xxx") — full-name attribute variant."""
        self.write_config(
            'config.scene.sku_shelf_export_csv = cfg.config_pnp_path("cfg-shelves.csv")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(set(result), {"shelves"})
        self.assertEqual(
            result["shelves"], (self.directory / "cfg-shelves.csv").resolve()
        )

    def test_mixed_bare_and_attribute_styles(self) -> None:
        """Both bare config_pnp_path() and attribute .pnp_path() in one file."""
        self.write_config(
            'config.scene.sku_shelf_export_csv = config_pnp_path("bare.csv")\n'
            'config.scene.obj_tool_mapping = config.pnp_path("attr.json")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(
            result["shelves"], (self.directory / "bare.csv").resolve()
        )
        self.assertEqual(
            result["tool_mapping"], (self.directory / "attr.json").resolve()
        )

    def test_attribute_with_wrong_method_name_is_skipped(self) -> None:
        """config.other_path("xxx") — attr not pnp_path/config_pnp_path, skip."""
        self.write_config(
            'config.scene.sku_shelf_export_csv = config.other_path("skip.csv")\n'
            'config.scene.obj_tool_mapping = config.pnp_path("keep.json")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(set(result), {"tool_mapping"})

    def test_attribute_style_non_literal_arg_is_skipped(self) -> None:
        """config.pnp_path(variable) — non-string-literal arg is skipped."""
        self.write_config(
            'name = "ok.csv"\n'
            'config.scene.sku_shelf_export_csv = config.pnp_path(name)\n'
            'config.scene.obj_tool_mapping = config.pnp_path("keep.json")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(set(result), {"tool_mapping"})

    # ------------------------------------------------------------------
    # Security: path traversal & nested code
    # ------------------------------------------------------------------

    def test_path_traversal_is_skipped(self) -> None:
        """A filename escaping config_pnp_dir is skipped; valid ones kept."""
        self.write_config(
            'config.scene.sku_shelf_export_csv = config_pnp_path("../../etc/passwd")\n'
            'config.scene.obj_tool_mapping = config_pnp_path("ok.json")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(set(result), {"tool_mapping"})

    def test_top_level_config_attr_is_skipped(self) -> None:
        """config.<key> (without .scene) must not be processed."""
        self.write_config(
            'config.visualize_robot_base = config_pnp_path("base.json")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(result, {})

    def test_imports_and_conditionals_are_skipped(self) -> None:
        """Only top-level assignments are processed; nested code is skipped."""
        self.write_config(
            'import os\n'
            'if True:\n'
            '    config.scene.sku_shelf_export_csv = config_pnp_path("ok.csv")\n'
        )
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(result, {})

    # ------------------------------------------------------------------
    # Error / missing-file fallbacks (zero regression)
    # ------------------------------------------------------------------

    def test_missing_config_py_returns_empty_dict(self) -> None:
        result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(result, {})

    def test_missing_directory_returns_empty_dict(self) -> None:
        result = config_pnp.load_config_pnp_paths(Path("/nonexistent/xyz"))
        self.assertEqual(result, {})

    def test_syntax_error_returns_empty_dict(self) -> None:
        self.write_config("this is not valid python !!!\n")
        with patch("sys.stderr") as mock_stderr:
            result = config_pnp.load_config_pnp_paths(self.directory)
        self.assertEqual(result, {})
        # A warning should have been printed.
        self.assertTrue(mock_stderr.write.called)


class ReloadConfigPnpPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        # Save and restore state globals
        self._old_config_pnp = state.configured_config_pnp
        self._old_explicit = state._explicit_config_keys
        self._old_shelves = state.configured_shelves
        self._old_unavailable = state.configured_unavailable
        self._old_tool_mapping = state.configured_tool_mapping
        self._old_pick_strategy = state.configured_pick_strategy

    def tearDown(self) -> None:
        state.configured_config_pnp = self._old_config_pnp
        state._explicit_config_keys = self._old_explicit
        state.configured_shelves = self._old_shelves
        state.configured_unavailable = self._old_unavailable
        state.configured_tool_mapping = self._old_tool_mapping
        state.configured_pick_strategy = self._old_pick_strategy
        self.temporary.cleanup()

    def test_reload_updates_non_explicit_fields(self) -> None:
        (self.directory / "config.py").write_text(
            'config.scene.sku_shelf_export_csv = config_pnp_path("new-shelves.csv")\n'
            'config.scene.obj_tool_mapping = config_pnp_path("new-mapping.json")\n',
            encoding="utf-8",
        )
        state.configured_config_pnp = self.directory
        state._explicit_config_keys = frozenset({"shelves"})
        state.configured_shelves = Path("/original/shelves.csv")
        state.configured_tool_mapping = Path("/original/mapping.json")

        state.reload_config_pnp_paths()

        # shelves was explicit, so config.py must NOT override it
        self.assertEqual(state.configured_shelves, Path("/original/shelves.csv"))
        # tool_mapping was NOT explicit, so config.py overrides it
        self.assertEqual(
            state.configured_tool_mapping,
            (self.directory / "new-mapping.json").resolve(),
        )

    def test_reload_no_config_pnp_dir_is_noop(self) -> None:
        state.configured_config_pnp = None
        state.configured_shelves = Path("/keep/shelves.csv")
        state.reload_config_pnp_paths()
        self.assertEqual(state.configured_shelves, Path("/keep/shelves.csv"))

    def test_reload_missing_config_py_is_noop(self) -> None:
        state.configured_config_pnp = self.directory
        state._explicit_config_keys = frozenset()
        state.configured_shelves = Path("/keep/shelves.csv")
        state.reload_config_pnp_paths()
        self.assertEqual(state.configured_shelves, Path("/keep/shelves.csv"))


if __name__ == "__main__":
    unittest.main()
