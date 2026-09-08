"""Cloud shelves share local loading, backup, and rollback behavior."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ksq.web import auth, data_storage, handlers, import_api, load_progress, loader, state


CSV = (
    "\ufeffsku_id,sku_code,name,shelf_number,level,bin_unit,future_column\n"
    "SKU-1,0690001,example,0001,01,02,keep-me\n"
).encode("utf-8")


class CsvResponse(io.BytesIO):
    def __init__(self, body: bytes = CSV, *, length: int | None = None) -> None:
        super().__init__(body)
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = "text/csv; charset=utf-8"
        self.headers["Content-Length"] = str(len(body) if length is None else length)

    def getcode(self) -> int:
        return self.status


class CloudLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.current = self.root / "data/current"
        self.backups = self.root / "data/backups"
        storage = patch.multiple(
            data_storage,
            DATA_DIRECTORY=self.root / "data",
            CURRENT_DIRECTORY=self.current,
            BACKUP_DIRECTORY=self.backups,
        )
        storage.start()
        self.addCleanup(storage.stop)
        fields = set(handlers._LOAD_PATH_STATE_FIELDS) | {
            "shelves_source", "configured_config_pnp", "configured_vfm_app",
            "_cli_config_paths", "_cli_knowledge_root", "_cli_knowledge_path",
        }
        saved = {name: getattr(state, name) for name in fields}
        self.addCleanup(lambda: [setattr(state, name, value) for name, value in saved.items()])

        self.knowledge = self.root / "source/knowledge"
        self.knowledge.mkdir(parents=True)
        (self.knowledge / "SKU-1.json").write_text('{"id":"SKU-1"}', encoding="utf-8")
        self.shelves = self.root / "source/sku-shelves.csv"
        self.sides = {
            "unavailable": ("unavailable_obj.json", {"unavailable_obj": ["SKU-1"]}),
            "tool_mapping": ("obj_tool_mapping.json", {"0690001": "gripper"}),
            "pick_strategy": ("pick_strategy_obj.json", {"closed_loop": ["SKU-1"]}),
        }
        for key, (filename, value) in self.sides.items():
            path = self.root / "source" / filename
            path.write_text(json.dumps(value), encoding="utf-8")
            setattr(state, "configured_" + key, path)
        state.configured_knowledge = self.knowledge
        state.configured_knowledge_root = self.root / "source"
        state.configured_shelves = self.shelves
        state.configured_config_pnp = None
        state.configured_vfm_app = None
        state._cli_config_paths = {}
        state._cli_knowledge_root = self.root / "source"
        state._cli_knowledge_path = self.knowledge
        state._explicit_config_keys = frozenset()
        state.loaded_paths = {}
        state.loaded_dataset = None
        state.loaded_tool_mapping = None
        state.loaded_closed_loop_ids = None
        state.loaded_unavailable_ids = None
        state.edit_workspace = None
        state.data_source_ready = False
        state.data_load_method = "none"
        state.data_revision = 0
        state.shelves_source = "local"

    def _payload(self, source: str = "cloud") -> dict[str, object]:
        return {
            "knowledge": str(self.knowledge),
            "shelves": "" if source == "cloud" else str(self.shelves),
            "shelves_source": source,
            **{key: str(getattr(state, "configured_" + key)) for key in self.sides},
        }

    def _request(self, payload: dict[str, object], path: str = "/load-paths") -> tuple[int, dict]:
        raw = json.dumps(payload).encode("utf-8")
        handler = handlers.QueryHandler.__new__(handlers.QueryHandler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
        handler.rfile = io.BytesIO(raw)
        response = {}
        handler._send_json = lambda status, data: response.update(status=int(status), data=data)
        session = {"username": "cloud-test", "role": auth.ROLE_ADMIN}
        with patch.object(auth, "session_from_cookie", return_value=session):
            handler.do_POST()
        return response["status"], response["data"]

    def _current_files(self) -> dict[Path, bytes]:
        return {path.relative_to(self.current): path.read_bytes() for path in self.current.rglob("*") if path.is_file()}

    def test_cloud_load_and_reload_download_again_keep_local_side_files_and_backup(self) -> None:
        original = {path: path.read_bytes() for path in self.knowledge.parent.rglob("*") if path.is_file()}
        replacement = CSV.replace(b"0001", b"0002")
        with patch.object(urllib.request, "urlopen", side_effect=[CsvResponse(), CsvResponse(replacement)]) as fetch:
            status, result = self._request(self._payload())
            self.assertEqual(status, 200, result)
            self.assertFalse(self.shelves.exists())
            self.assertEqual(state.loaded_paths["shelves"].read_bytes(), CSV)
            self.assertEqual(state.loaded_dataset.shelf_entries["SKU-1"][0].sku_code, "0690001")
            self.assertEqual(state.loaded_tool_mapping, {"0690001": "gripper"})
            self.assertEqual(state.loaded_unavailable_ids, frozenset({"SKU-1"}))
            self.assertEqual(state.loaded_closed_loop_ids, frozenset({"SKU-1"}))
            first_files = self._current_files()
            status, result = self._request({}, "/api/reload")
            self.assertEqual(status, 200, result)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(state.shelves_source, "cloud")
        self.assertEqual(state.loaded_paths["shelves"].read_bytes(), replacement)
        backups = list(self.backups.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual({path.relative_to(backups[0]): path.read_bytes() for path in backups[0].rglob("*") if path.is_file()}, first_files)
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertEqual(list((self.root / "data").glob(".staging-*")), [])

    def test_default_local_load_never_downloads_and_clears_previous_cloud_mode(self) -> None:
        self.shelves.write_bytes(CSV)
        state.shelves_source = "cloud"
        payload = self._payload("local")
        payload.pop("shelves_source")
        with patch.object(urllib.request, "urlopen", side_effect=AssertionError("Local mode must not download")) as fetch:
            status, result = self._request(payload)
            self.assertEqual(status, 200, result)
            status, result = self._request({}, "/api/reload")
            self.assertEqual(status, 200, result)
            fetch.assert_not_called()
        self.assertEqual(state.shelves_source, "local")
        self.assertEqual(state.loaded_paths["shelves"].read_bytes(), CSV)

    def test_failed_cloud_download_or_invalid_csv_keeps_previous_local_dataset(self) -> None:
        self.shelves.write_bytes(CSV)
        status, result = self._request(self._payload("local"))
        self.assertEqual(status, 200, result)
        previous = handlers._snapshot_load_path_state()
        files = self._current_files()
        cases = (urllib.error.URLError("connection refused"), CsvResponse(b"not,a,valid,csv\n"))
        for response in cases:
            with self.subTest(response=type(response).__name__):
                kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with patch.object(urllib.request, "urlopen", **kwargs):
                    status, result = self._request(self._payload())
                self.assertEqual(status, 400, result)
                self.assertEqual(handlers._snapshot_load_path_state(), previous)
                self.assertEqual(self._current_files(), files)
                self.assertEqual(list(self.backups.glob("*")), [])
                self.assertEqual(list((self.root / "data").glob(".staging-*")), [])

    def test_local_empty_path_and_unknown_modes_are_rejected_without_network(self) -> None:
        self.shelves.write_bytes(CSV)
        state.configured_config_pnp = self.knowledge.parent
        (state.configured_config_pnp / "config.py").write_text("pass\n", encoding="utf-8")
        with patch.object(urllib.request, "urlopen", side_effect=AssertionError("Invalid mode must not download")) as fetch:
            payload = self._payload("local")
            payload["shelves"] = ""
            status, result = self._request(payload)
            self.assertEqual(status, 400, result)
            for mode in ("invalid", [], True):
                with self.subTest(mode=mode):
                    payload = self._payload("local")
                    payload["shelves_source"] = mode
                    status, result = self._request(payload)
                    self.assertEqual(status, 400, result)
            for mode in ("invalid", [], True):
                status, result = self._request({"shelves_source": mode}, "/load-auto")
                self.assertEqual(status, 400, result)
            fetch.assert_not_called()

    def test_one_click_cloud_load_has_no_local_shelves_requirement(self) -> None:
        state.configured_config_pnp = self.knowledge.parent
        (state.configured_config_pnp / "config.py").write_text("pass\n", encoding="utf-8")
        with patch.object(urllib.request, "urlopen", return_value=CsvResponse()):
            status, result = self._request({"shelves_source": "cloud"}, "/load-auto")
        self.assertEqual(status, 200, result)
        # Retain the local source as metadata so the local toggle can restore it.
        # Frontend source-control tests assert the cloud input itself stays empty.
        self.assertEqual(result["paths"]["shelves"], self.shelves.name)
        self.assertEqual(state.shelves_source, "cloud")
        self.assertFalse(self.shelves.exists())

    def test_side_import_preserves_cloud_reload_but_imported_shelves_switch_to_local(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=[CsvResponse(), CsvResponse()]) as fetch:
            status, result = self._request(self._payload())
            self.assertEqual(status, 200, result)
            with state.DATASET_LOCK:
                import_api.import_uploaded_files({"files": SimpleNamespace(
                    filename="obj_tool_mapping.json", file=io.BytesIO(b'{"0690001":"suction"}'),
                )})
            self.assertEqual(state.shelves_source, "cloud")
            status, result = self._request({}, "/api/reload")
            self.assertEqual(status, 200, result)
            self.assertEqual(state.loaded_tool_mapping, {"0690001": "suction"})
            self.assertEqual(fetch.call_count, 2)
        with patch.object(urllib.request, "urlopen", side_effect=AssertionError("Imported CSV must stay local")):
            with state.DATASET_LOCK:
                import_api.import_uploaded_files({"files": SimpleNamespace(
                    filename="sku-shelves.csv", file=io.BytesIO(CSV),
                )})
            self.assertEqual(state.shelves_source, "local")
            status, result = self._request({}, "/api/reload")
            self.assertEqual(status, 200, result)

    def test_download_preserves_csv_bytes_and_reports_actual_progress(self) -> None:
        destination = self.root / "download.csv"
        with (
            patch.object(urllib.request, "urlopen", return_value=CsvResponse()) as fetch,
            patch.object(load_progress, "update") as progress,
        ):
            loader.download_cloud_shelves(destination)
        self.assertEqual(destination.read_bytes(), CSV)
        request = fetch.call_args.args[0]
        self.assertEqual(request if isinstance(request, str) else request.full_url, loader.CLOUD_SHELVES_URL)
        self.assertEqual(fetch.call_args.kwargs["timeout"], 30)
        self.assertTrue(any(call.args[-2:] == (len(CSV), len(CSV)) for call in progress.call_args_list))

    def test_download_rejects_http_error_empty_truncated_and_oversized_responses(self) -> None:
        cases = (
            urllib.error.HTTPError(loader.CLOUD_SHELVES_URL, 503, "Unavailable", {}, io.BytesIO()),
            CsvResponse(b""),
            CsvResponse(CSV, length=len(CSV) + 1),
            CsvResponse(CSV, length=64 * 1024 * 1024 + 1),
        )
        for index, response in enumerate(cases):
            with self.subTest(case=index):
                kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with patch.object(urllib.request, "urlopen", **kwargs):
                    with self.assertRaises(ValueError):
                        loader.download_cloud_shelves(self.root / f"invalid-{index}.csv")


if __name__ == "__main__":
    unittest.main()
