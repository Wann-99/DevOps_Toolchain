from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ksq import config_pnp
from ksq.order import broker
from ksq.order import config as order_config
from ksq.web import dashboard_api, edit_workspace, import_api, loader, order_api, state


class _Form:
    def __init__(self, field: str, values: list[object]) -> None:
        self.field = field
        self.values = values

    def __contains__(self, key: object) -> bool:
        return key == self.field

    def __getitem__(self, key: str) -> list[object]:
        if key != self.field:
            raise KeyError(key)
        return self.values


def _upload(name: str, payload: bytes) -> SimpleNamespace:
    return SimpleNamespace(filename=name, file=io.BytesIO(payload))


class ConfigAndWorkspaceTests(unittest.TestCase):
    def test_config_prefix_sibling_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cfg"
            sibling = Path(temporary) / "cfg_evil"
            root.mkdir()
            sibling.mkdir()
            (root / "config.py").write_text(
                'config.scene.sku_shelf_export_csv = config_pnp_path("../cfg_evil/x.csv")\n',
                encoding="utf-8",
            )
            result = config_pnp.load_config_pnp_paths(root)
        self.assertEqual(result, {})

    def test_item_id_path_traversal_is_rejected_before_workspace_access(self) -> None:
        with self.assertRaisesRegex(ValueError, "无效路径"):
            edit_workspace.save_field("../outside", "备注", "x")

    def test_duplicate_knowledge_id_keeps_first_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shelves = root / "shelves.csv"
            shelves.write_text(
                "sku_code,name,shelf_number,level,bin_unit\n"
                "sku-1,Drug,1,1,1\n",
                encoding="utf-8",
            )
            old = {
                "configured_knowledge": state.configured_knowledge,
                "configured_shelves": state.configured_shelves,
                "loaded_dataset": state.loaded_dataset,
                "loaded_tool_mapping": state.loaded_tool_mapping,
                "loaded_closed_loop_ids": state.loaded_closed_loop_ids,
                "loaded_unavailable_ids": state.loaded_unavailable_ids,
                "edit_workspace": state.edit_workspace,
            }
            try:
                state.configured_shelves = shelves
                state.configured_knowledge = root / "knowledge"
                state.loaded_dataset = SimpleNamespace(
                    knowledge_records=(
                        {"id": "sku-1", "value": "first"},
                        {"id": "sku-1", "value": "last"},
                    )
                )
                state.loaded_tool_mapping = None
                state.loaded_closed_loop_ids = None
                state.loaded_unavailable_ids = None
                edit_workspace.init_workspace_from_loaded()
                assert state.edit_workspace is not None
                record = state.edit_workspace["knowledge_by_id"]["sku-1"]
                self.assertEqual(record["value"], "first")
            finally:
                for key, value in old.items():
                    setattr(state, key, value)


class OrderConfigAndBrokerTests(unittest.TestCase):
    VALID = {
        "server": "https://broker.example",
        "client_id": "client",
        "client_secret": "secret",
        "store_id": "store-1",
    }

    def test_order_config_rejects_wrong_known_types(self) -> None:
        with self.assertRaisesRegex(ValueError, "server.*字符串"):
            order_config.merge_config_update(self.VALID, {"server": []})
        with self.assertRaisesRegex(ValueError, "need_image_upload.*布尔"):
            order_config.validate_order_config(
                dict(self.VALID, need_image_upload=1)
            )

    def test_read_apis_reject_http_200_business_errors(self) -> None:
        response = (200, {"code": 4511, "msg": "no robot"})
        with (
            patch.object(order_api, "load_order_config", return_value=self.VALID),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(order_api.broker, "list_my_stores", return_value=response),
        ):
            with self.assertRaises(broker.OrderBrokerError):
                order_api.list_stores("test")

        order_api.clear_task_list_cache()
        with (
            patch.object(order_api, "load_order_config", return_value=self.VALID),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(
                order_api.broker, "list_robot_tasks", return_value=response
            ),
        ):
            with self.assertRaises(broker.OrderBrokerError):
                order_api.list_tasks("test", refresh=True)

        with (
            patch.object(dashboard_api, "resolve_dashboard_mode", return_value="test"),
            patch.object(order_api, "load_order_config", return_value=self.VALID),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(order_api.broker, "list_business_modes", return_value=response),
        ):
            with self.assertRaises(broker.OrderBrokerError):
                order_api.list_business_modes()


class ImportTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.knowledge = self.root / "knowledge"
        self.knowledge.mkdir()
        (self.knowledge / "old.json").write_text(
            json.dumps({"id": "old"}), encoding="utf-8"
        )
        self.shelves = self.root / "shelves.csv"
        self.shelves.write_text(
            "sku_code,name,shelf_number,level,bin_unit\n"
            "old,Old,1,1,1\n",
            encoding="utf-8",
        )
        self._state = {
            key: getattr(state, key)
            for key in (
                "configured_knowledge",
                "configured_shelves",
                "configured_unavailable",
                "configured_tool_mapping",
                "configured_pick_strategy",
                "configured_config_pnp",
                "_explicit_config_keys",
                "loaded_dataset",
                "loaded_tool_mapping",
                "loaded_closed_loop_ids",
                "loaded_unavailable_ids",
                "data_source_ready",
                "data_load_method",
                "edit_workspace",
                "data_revision",
            )
        }
        state.configured_knowledge = self.knowledge
        state.configured_shelves = self.shelves
        state.configured_unavailable = None
        state.configured_tool_mapping = None
        state.configured_pick_strategy = None
        state.configured_config_pnp = None
        state._explicit_config_keys = frozenset()
        state.data_source_ready = False
        state.data_load_method = "none"
        state.edit_workspace = None

    def tearDown(self) -> None:
        for key, value in self._state.items():
            setattr(state, key, value)
        self.temporary.cleanup()

    def test_bad_batch_leaves_targets_and_runtime_untouched(self) -> None:
        runtime = self.root / ".runtime_upload"
        runtime.mkdir()
        (runtime / "sentinel").write_text("old", encoding="utf-8")
        good = _upload("new.json", json.dumps({"id": "new"}).encode())
        bad = _upload("bad.json", b"{bad")
        with patch.object(import_api, "RUNTIME_UPLOAD_DIRECTORY", runtime):
            with self.assertRaises(ValueError):
                import_api.import_uploaded_files(_Form("files", [good, bad]))
        self.assertFalse((self.knowledge / "new.json").exists())
        self.assertEqual((self.knowledge / "old.json").read_text(), '{"id": "old"}')
        self.assertEqual((runtime / "sentinel").read_text(), "old")

    def test_invalid_bundle_does_not_replace_runtime_upload(self) -> None:
        runtime = self.root / ".runtime_upload"
        runtime.mkdir()
        (runtime / "sentinel").write_text("old", encoding="utf-8")
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("not-a-bundle.txt", "x")
        payload.seek(0)
        upload = _upload("bad.zip", payload.getvalue())
        form = _Form("bundle_zip", [upload])
        with patch.object(loader, "RUNTIME_UPLOAD_DIRECTORY", runtime):
            with self.assertRaises(ValueError):
                loader.load_uploaded_zip(form)
        self.assertEqual((runtime / "sentinel").read_text(), "old")


if __name__ == "__main__":
    unittest.main()
