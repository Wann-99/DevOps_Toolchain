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
from ksq.web import dashboard_api, data_storage, edit_workspace, import_api, loader, order_api, state


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
                "loaded_paths": state.loaded_paths,
                "loaded_dataset": state.loaded_dataset,
                "loaded_tool_mapping": state.loaded_tool_mapping,
                "loaded_closed_loop_ids": state.loaded_closed_loop_ids,
                "loaded_unavailable_ids": state.loaded_unavailable_ids,
                "edit_workspace": state.edit_workspace,
            }
            try:
                state.configured_shelves = shelves
                state.loaded_paths = {}
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
        self.current = self.root / "data" / "current"
        self.backups = self.root / "data" / "backups"
        storage = patch.multiple(
            data_storage,
            DATA_DIRECTORY=self.root / "data",
            CURRENT_DIRECTORY=self.current,
            BACKUP_DIRECTORY=self.backups,
        )
        storage.start()
        self.addCleanup(storage.stop)
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
                "configured_knowledge_root",
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
                "loaded_paths",
                "data_source_ready",
                "data_load_method",
                "edit_workspace",
                "data_revision",
            )
        }
        state.configured_knowledge = self.knowledge
        state.configured_knowledge_root = None
        state.configured_shelves = self.shelves
        state.configured_unavailable = None
        state.configured_tool_mapping = None
        state.configured_pick_strategy = None
        state.configured_config_pnp = None
        state._explicit_config_keys = frozenset()
        state.loaded_paths = {}
        state.loaded_dataset = None
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
        with self.assertRaises(ValueError):
            import_api.import_uploaded_files(_Form("files", [good, bad]))
        self.assertFalse((self.knowledge / "new.json").exists())
        self.assertEqual((self.knowledge / "old.json").read_text(), '{"id": "old"}')
        self.assertEqual((runtime / "sentinel").read_text(), "old")

    def test_import_uses_managed_base_and_never_writes_sources(self) -> None:
        result = import_api.import_uploaded_files(_Form("files", [
            _upload("new.json", b'{"id": "new"}'),
        ]))
        self.assertTrue(result["reloaded"])
        self.assertTrue(state.data_source_ready)
        self.assertEqual(state.configured_knowledge, self.current / "knowledge")
        self.assertEqual(state.configured_knowledge_root, self.current)
        self.assertEqual(state.loaded_paths["shelves"], self.current / "config_pnp" / "sku-shelves.csv")
        self.assertFalse((self.knowledge / "new.json").exists())
        self.assertTrue((self.current / "knowledge" / "old.json").is_file())
        self.shelves.write_text("changed source", encoding="utf-8")
        result = import_api.import_uploaded_files(_Form("files", [
            _upload("other.json", b'{"id": "other"}'),
        ]))
        self.assertTrue(result["reloaded"])
        self.assertEqual(result["backup_count"], 1)
        self.assertEqual({path.name for path in (self.current / "knowledge").iterdir()}, {"old.json", "new.json", "other.json"})
        self.assertEqual(self.shelves.read_text(), "changed source")
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_partial_imports_accumulate_until_dataset_is_complete(self) -> None:
        state.configured_knowledge = self.root / "missing-knowledge"
        state.configured_shelves = self.root / "missing-shelves.csv"
        result = import_api.import_uploaded_files(_Form("files", [
            _upload("new.json", b'{"id": "new"}'),
        ]))
        self.assertFalse(result["reloaded"])
        self.assertFalse(state.data_source_ready)
        self.assertEqual(state.loaded_paths, {"knowledge": self.current / "knowledge"})
        result = import_api.import_uploaded_files(_Form("files", [
            _upload("sku-shelves.csv", b"sku_code,name,shelf_number,level,bin_unit\nnew,New,1,1,1\n"),
        ]))
        self.assertTrue(result["reloaded"])
        self.assertTrue(state.data_source_ready)
        self.assertEqual(set(state.loaded_dataset.shelf_entries), {"new"})
        self.assertFalse((self.root / "missing-knowledge").exists())
        self.assertFalse((self.root / "missing-shelves.csv").exists())

    def test_install_failure_restores_dataset_state_and_order_config(self) -> None:
        import_api.import_uploaded_files(_Form("files", [
            _upload("new.json", b'{"id": "new"}'),
        ]))
        previous_dataset = state.loaded_dataset
        previous_paths = dict(state.loaded_paths)
        config = self.root / "order_config.json"
        old_config = b'{"server": "https://old.example"}'
        config.write_bytes(old_config)
        with (
            patch.object(import_api, "ORDER_CONFIG_FILE", config),
            patch.object(edit_workspace, "init_workspace_from_loaded", side_effect=RuntimeError("install failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "install failed"):
                import_api.import_uploaded_files(_Form("files", [
                    _upload("order_config.json", b'{"server": "https://new.example"}'),
                    _upload("other.json", b'{"id": "other"}'),
                ]))
        self.assertEqual(config.read_bytes(), old_config)
        self.assertIs(state.loaded_dataset, previous_dataset)
        self.assertEqual(state.loaded_paths, previous_paths)
        self.assertFalse((self.current / "knowledge" / "other.json").exists())
        self.assertTrue((self.current / "knowledge" / "new.json").is_file())
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_order_only_import_validates_entire_batch_and_preserves_dataset(self) -> None:
        config = self.root / "order_config.json"
        with patch.object(import_api, "ORDER_CONFIG_FILE", config):
            with patch.object(import_api, "_write_bytes") as write:
                with self.assertRaises(ValueError):
                    import_api.import_uploaded_files(_Form("files", [
                        _upload("order_config.json", b'{"server": "https://new.example"}'),
                        _upload("bad.json", b"{bad"),
                    ]))
                write.assert_not_called()
            result = import_api.import_uploaded_files(_Form("files", [
                _upload("order_config.json", b'{"server": "https://new.example"}'),
            ]))
        self.assertFalse(result["reloaded"])
        self.assertFalse(self.current.exists())
        self.assertEqual(state.loaded_paths, {})
        self.assertEqual(json.loads(config.read_bytes()), {"server": "https://new.example"})

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
