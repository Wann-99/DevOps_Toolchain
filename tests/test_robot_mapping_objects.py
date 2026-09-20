"""Mapping object changes are bounded to the selected object and chassis."""

from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ksq.web import robot_map_api as api
from ksq.web import robot_mapping_objects as objects


BASE = "http://192.0.2.10:1448"
POI_PATH = "/api/core/artifact/v1/pois"
TRACK_PATH = "/api/core/artifact/v1/lines/tracks"


def entry(item_id: str, name: str) -> dict:
    return {"id": item_id, "pose": {"x": 1, "y": 2, "yaw": 0.3},
            "metadata": {"display_name": name, "type": "waypoint", "custom": "kept"}}


def deployment(kind: str) -> tuple[dict, dict]:
    raw = {"id": "one" if kind in {"poi", "dock"} else 1, "metadata": None}
    payload = {"type": kind, "name": "Renamed", "x": 1, "y": 2, "yaw": 0.3}
    if kind in {"poi", "dock"}:
        raw["pose"] = {"x": 1, "y": 2, "yaw": 0.3}
    elif kind in {"wall", "track"}:
        raw.update(start={"x": 0, "y": 2}, end={"x": 2, "y": 2})
        payload.update(endX=3, endY=4)
    else:
        raw["area"] = {"start": {"x": 0, "y": 2}, "end": {"x": 2, "y": 2}, "half_width": 0.5}
        payload.update(width=2, height=1)
        if kind == "danger":
            payload["speed_mps"] = 0.2
        elif kind == "sensor":
            payload["sensor_types"] = [2]
    return raw, payload


class MappingObjectTests(unittest.TestCase):
    def test_metadata_accepts_only_absent_null_or_object_and_copies_custom_fields(self) -> None:
        self.assertEqual(objects._metadata({}), {})
        self.assertEqual(objects._metadata({"metadata": None}), {})
        raw = {"metadata": {"custom": {"kept": "value"}}}
        copied = objects._metadata(raw)
        copied["custom"]["kept"] = "changed"
        self.assertEqual(raw["metadata"]["custom"]["kept"], "value")
        for value in (False, 0, [], ["field"], "", "null", '{"display_name":"Area"}'):
            with self.subTest(value=value), self.assertRaises(api.RobotApiError):
                objects._metadata({"metadata": value})

    def test_all_deployment_lists_accept_null_metadata(self) -> None:
        kinds = ("poi", "dock", "wall", "track", "forbidden", "danger", "maintenance", "sensor")
        responses = {objects._PATHS[kind]: [deployment(kind)[0]] for kind in kinds}
        responses[objects._PATHS["pose"]] = {"x": 1, "y": 2, "yaw": 0.3}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            api, "ROBOT_MAP_POIS_FILE", Path(directory) / "pois.json"
        ), patch.object(api, "_request", side_effect=lambda method, path, **kwargs: (200, responses[path])):
            result = objects.list_objects(BASE)
        self.assertFalse(result["errors"])
        self.assertEqual({item["type"] for item in result["objects"]}, {*kinds, "pose"})

    def test_all_deployment_edits_accept_null_metadata_and_preserve_custom_fields(self) -> None:
        for kind in ("poi", "dock", "wall", "track", "forbidden", "danger", "maintenance", "sensor"):
            for metadata in (None, {"custom": "kept"}):
                raw, payload = deployment(kind)
                raw["metadata"] = metadata
                payload["id"] = raw["id"]
                with self.subTest(kind=kind, metadata=metadata), tempfile.TemporaryDirectory() as directory, patch.object(
                    api, "ROBOT_MAP_POIS_FILE", Path(directory) / "pois.json"
                ), patch.object(api, "_request", side_effect=[(200, [raw]), (200, True)]) as request:
                    result = objects.save_object(payload, BASE)
                written = request.call_args_list[1].args[2]
                if kind in {"wall", "track"}:
                    written = written[0]
                self.assertEqual(written["metadata"]["display_name"], "Renamed")
                if metadata is not None:
                    self.assertEqual(written["metadata"]["custom"], "kept")
                self.assertEqual(result["object"]["id"], raw["id"])

    def test_new_lines_and_areas_accept_null_metadata_readback(self) -> None:
        for kind in ("wall", "track", "forbidden", "danger", "maintenance", "sensor"):
            raw, payload = deployment(kind)
            with self.subTest(kind=kind), patch.object(
                api, "_request", side_effect=[(200, []), (200, True), (200, [raw])]
            ) as request:
                result = objects.save_object(payload, BASE)
            self.assertEqual(result["object"]["id"], 1)
            self.assertNotIn("warning", result)
            self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "POST", "GET"])

    def test_confirmed_creation_readback_failures_warn_without_repeating_the_write(self) -> None:
        for kind in ("wall", "track", "forbidden", "danger", "maintenance", "sensor"):
            raw, payload = deployment(kind)
            bad_geometry = {**raw, "start": None, "area": None}
            failures = [
                api.RobotApiError("读取超时。"), (200, []), (200, [raw, {**raw, "id": 2}]),
                (200, "invalid"), (200, [{**raw, "id": "invalid"}]),
                (200, [bad_geometry]), (200, [{**raw, "metadata": "invalid"}]),
            ]
            for failure in failures:
                with self.subTest(kind=kind, failure=failure), patch.object(
                    api, "_request", side_effect=[(200, []), (200, True), failure]
                ) as request:
                    result = objects.save_object(payload, BASE)
                self.assertIsNone(result["object"])
                self.assertIn("底盘已接受新增配置", result["warning"])
                self.assertIn("请勿重复保存", result["warning"])
                self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "POST", "GET"])

    def test_invalid_metadata_edit_is_rejected_before_writing(self) -> None:
        for kind in ("poi", "dock", "wall", "track", "forbidden", "danger", "maintenance", "sensor"):
            raw, payload = deployment(kind)
            payload["id"] = raw["id"]
            for metadata in (False, 0, [], "", "null", '{"display_name":"Area"}'):
                with self.subTest(kind=kind, metadata=metadata), patch.object(
                    api, "_request", return_value=(200, [{**raw, "metadata": metadata}])
                ) as request, self.assertRaises(api.RobotApiError):
                    objects.save_object(payload, BASE)
                self.assertEqual([call.args[0] for call in request.call_args_list], ["GET"])

    def test_unconfirmed_creation_remains_an_error_without_readback(self) -> None:
        for response in (False, None, {}, {"result": True}, "true"):
            with self.subTest(response=response), patch.object(
                api, "_request", side_effect=[(200, []), (200, response)]
            ) as request, self.assertRaises(api.RobotApiError):
                objects.save_object(deployment("maintenance")[1], BASE)
            self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "POST"])

    def test_poi_edit_preserves_metadata_and_cached_order(self) -> None:
        first, second = entry("first", "First"), entry("second", "Second")
        with tempfile.TemporaryDirectory() as directory, patch.object(
            api, "ROBOT_MAP_POIS_FILE", Path(directory) / "pois.json"
        ):
            api._save_poi_cache(BASE, [api._normalize_poi(first), api._normalize_poi(second)])
            with patch.object(api, "_request", side_effect=[(200, [second, first]), (200, True)]) as request:
                result = objects.save_object({"type": "poi", "id": "first", "name": "Renamed",
                                              "x": 5, "y": 6, "yaw": math.pi / 2}, BASE)
            written = request.call_args_list[1]
            self.assertEqual(written.args[:2], ("PUT", POI_PATH + "/first"))
            self.assertEqual(written.args[2]["metadata"], {**first["metadata"], "display_name": "Renamed"})
            self.assertAlmostEqual(written.args[2]["pose"]["yaw"], math.pi / 2)
            self.assertEqual(result["object"]["id"], "first")
            cached = api._load_poi_cache(BASE)
            self.assertEqual([item["id"] for item in cached], ["first", "second"])
            self.assertEqual(cached[0]["name"], "Renamed")
            self.assertTrue(all(call.kwargs["base_url"] == BASE for call in request.call_args_list))
            self.assertEqual(request.call_args_list[0].kwargs["timeout"], api._REQUEST_TIMEOUT_SECONDS)

    def test_line_edit_merges_only_selected_item_and_preserves_unrelated_metadata(self) -> None:
        first = {"id": 1, "start": {"x": 0, "y": 0}, "end": {"x": 1, "y": 1}, "metadata": {"custom": "kept"}}
        first["metadata"].update(api._patrol_track_metadata(first))
        second = {"id": 2, "start": {"x": 4, "y": 4}, "end": {"x": 5, "y": 5}, "metadata": {"control_point1": "curve"}}
        original = deepcopy([first, second])
        with patch.object(api, "_request", side_effect=[(200, original), (200, True)]) as request:
            objects.save_object({"type": "track", "id": 1, "name": "Track", "x": 2, "y": 3,
                                 "endX": 4, "endY": 5}, BASE)
        written = request.call_args_list[1]
        self.assertEqual(written.args[:2], ("PUT", TRACK_PATH))
        self.assertEqual(written.args[2][1], second)
        self.assertEqual(written.args[2][0]["start"], {"x": 2, "y": 3})
        self.assertEqual(written.args[2][0]["metadata"]["custom"], "kept")
        self.assertNotIn(api._PATROL_TRACK_METADATA_KEY, written.args[2][0]["metadata"])
        self.assertEqual(original, [first, second])

    def test_edit_keeps_existing_yaw_when_the_form_does_not_supply_it(self) -> None:
        with patch.object(api, "_request", side_effect=[(200, [entry("dock", "Dock")]), (200, True)]) as request:
            result = objects.save_object({"type": "dock", "id": "dock", "name": "Dock", "x": 4, "y": 5}, BASE)
        self.assertEqual(result["object"]["yaw"], 0.3)
        self.assertEqual(request.call_args_list[1].args[2]["pose"]["yaw"], 0.3)

    def test_new_rectangle_uses_rotated_center_geometry_and_firmware_metadata(self) -> None:
        created = {"id": 9, "area": {"start": {"x": 3, "y": 2}, "end": {"x": 3, "y": 6}, "half_width": 1},
                   "metadata": {"display_name": "Slow", "max_line_speed": "0.2", "dangerous_area_type": "1"}}
        with patch.object(api, "_request", side_effect=[(200, []), (200, True), (200, [created])]) as request:
            result = objects.save_object({"type": "danger", "name": "Slow", "x": 3, "y": 4,
                                          "width": 4, "height": 2, "yaw": math.pi / 2, "speed_mps": 0.2}, BASE)
        written = request.call_args_list[1].args[2]
        self.assertAlmostEqual(written["area"]["start"]["x"], 3)
        self.assertAlmostEqual(written["area"]["start"]["y"], 2)
        self.assertAlmostEqual(written["area"]["end"]["y"], 6)
        self.assertEqual(written["metadata"]["max_line_speed"], "0.2")
        self.assertAlmostEqual(result["object"]["yaw"], math.pi / 2)
        self.assertEqual(result["object"]["id"], 9)

    def test_sensor_region_create_roundtrip_and_selected_delete(self) -> None:
        raw, payload = deployment("sensor")
        raw["metadata"] = {"display_name": "Sensors", "sensor_type": "[2, 6]"}
        payload.update(name="Sensors", sensor_types=[2, 6], yaw=0)
        path = "/api/core/artifact/v1/rectangle-areas/sensor_disable_area"
        with patch.object(api, "_request", side_effect=[(200, []), (200, True), (200, [raw])]) as request:
            result = objects.save_object(payload, BASE)
        written = request.call_args_list[1]
        self.assertEqual(written.args[:2], ("POST", path))
        self.assertEqual(written.args[2]["area"], raw["area"])
        self.assertIsInstance(written.args[2]["metadata"]["sensor_type"], str)
        self.assertEqual(json.loads(written.args[2]["metadata"]["sensor_type"]), [2, 6])
        self.assertEqual(result["object"]["sensor_types"], [2, 6])
        other = {**raw, "id": 2}
        with patch.object(api, "_request", side_effect=[(200, [raw, other]), (200, True), (200, [other])]) as request:
            deleted = objects.delete_object({"type": "sensor", "id": 1}, BASE)
        self.assertEqual(request.call_args_list[1].args[:2], ("DELETE", path + "/1"))
        self.assertEqual(deleted["deleted"], {"type": "sensor", "id": 1})

    def test_sensor_edit_preserves_unknown_types_and_omitted_metadata(self) -> None:
        raw, payload = deployment("sensor")
        raw["metadata"] = {"sensor_type": "[0, 6, 9]", "custom": {"kept": "value"}}
        payload["id"] = 1
        for changes, expected in (({"sensor_types": [2]}, [2, 9]), ({"sensor_types": []}, [9]), ({}, [0, 6, 9])):
            supplied = {key: value for key, value in payload.items() if key != "sensor_types"}
            supplied.update(changes)
            with self.subTest(changes=changes), patch.object(
                api, "_request", side_effect=[(200, [raw]), (200, True)]
            ) as request:
                result = objects.save_object(supplied, BASE)
            metadata = request.call_args_list[1].args[2]["metadata"]
            self.assertEqual(json.loads(metadata["sensor_type"]), expected)
            self.assertEqual(metadata["custom"], {"kept": "value"})
            self.assertEqual(result["object"]["sensor_types"], expected)
            if not changes:
                self.assertEqual(metadata["sensor_type"], raw["metadata"]["sensor_type"])
        self.assertEqual(raw["metadata"]["sensor_type"], "[0, 6, 9]")
        raw["metadata"]["sensor_type"] = "[0]"
        with patch.object(api, "_request", return_value=(200, [raw])) as request, self.assertRaises(ValueError):
            objects.save_object({**payload, "sensor_types": []}, BASE)
        self.assertEqual(request.call_count, 1)

    def test_sensor_type_validation_rejects_invalid_or_unknown_new_values_without_writes(self) -> None:
        raw, payload = deployment("sensor")
        for value in (None, False, 1, "[2]", [], [True], [1.0], [-1], [4], [2, 2], [[2]],
                      [float("nan")], [float("inf")]):
            with self.subTest(value=value), patch.object(api, "_request") as request, self.assertRaises(ValueError):
                objects.save_object({**payload, "sensor_types": value}, BASE)
            request.assert_not_called()
        payload.pop("sensor_types")
        with patch.object(api, "_request") as request, self.assertRaises(ValueError):
            objects.save_object(payload, BASE)
        request.assert_not_called()
        for value in (None, [], "", "null", "{}", "[true]", "[1.0]", "[-1]", "[1, 1]", "[NaN]", "[Infinity]"):
            raw["metadata"] = {"sensor_type": value}
            with self.subTest(firmware=value), self.assertRaises(api.RobotApiError):
                objects._normal("sensor", raw)

    def test_dock_create_and_delete_use_individual_homedock_endpoints(self) -> None:
        dock_path = "/api/core/slam/v1/homedocks"
        with patch.object(api, "_request", side_effect=[(200, []), (200, True)]) as request:
            result = objects.save_object({"type": "dock", "name": "Dock", "x": 1, "y": 2, "yaw": 0}, BASE)
        self.assertEqual(request.call_args_list[1].args[:2], ("POST", dock_path))
        created = request.call_args_list[1].args[2]
        with patch.object(api, "_request", side_effect=[(200, [created]), (200, True), (200, [])]) as request:
            objects.delete_object({"type": "dock", "id": result["object"]["id"]}, BASE)
        self.assertEqual(request.call_args_list[1].args[:2], ("DELETE", dock_path + "/" + created["id"]))

    def test_deletion_handles_legacy_lineid_without_clear_all_calls(self) -> None:
        first = {"lineid": 7, "start": {"x": 0, "y": 0}, "end": {"x": 1, "y": 1}}
        second = {**first, "lineid": 8}
        with patch.object(api, "_request", side_effect=[(200, [first, second]), (200, True), (200, [second])]) as request:
            result = objects.delete_object({"type": "track", "id": "7"}, BASE)
        self.assertEqual(request.call_args_list[1].args[:2], ("DELETE", TRACK_PATH + "/7"))
        self.assertEqual(result["deleted"], {"type": "track", "id": 7})
        with patch.object(api, "_request", return_value=(200, [second])) as request:
            with self.assertRaises(ValueError):
                objects.delete_object({"type": "track", "id": 7}, BASE)
        self.assertEqual(request.call_count, 1)

    def test_deletion_does_not_report_success_when_an_unselected_id_disappears(self) -> None:
        with patch.object(api, "_request", side_effect=[(200, [{"id": 1}, {"id": 2}]), (200, True), (200, [])]):
            with self.assertRaises(api.RobotApiError):
                objects.delete_object({"type": "wall", "id": 1}, BASE)

    def test_invalid_fields_and_singleton_confirmation_do_not_write(self) -> None:
        valid = {"type": "poi", "name": "Stop", "x": 0, "y": 0}
        invalid = [
            {**valid, "x": float("nan")}, {**valid, "yaw": float("inf")},
            {**valid, "y": True}, {**valid, "name": "x" * 129}, {**valid, "id": "../pois"},
            {**valid, "type": []}, {**valid, "type": "/api/core/slam/v1/maps"},
            {**valid, "type": "wall", "endX": 0, "endY": 0},
            {**valid, "type": "danger", "width": 1, "height": 0, "speed_mps": 0.2},
            {**valid, "type": "danger", "width": 1, "height": 1, "speed_mps": -1},
            {**valid, "type": "pose"}, {**valid, "type": "origin", "confirm": "true"},
            {**valid, "type": "origin", "confirm": True, "yaw": 1},
        ]
        with patch.object(api, "_request") as request:
            for payload in invalid:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    objects.save_object(payload, BASE)
            request.assert_not_called()

    def test_origin_uses_new_origin_point_and_never_fabricates_a_read_value(self) -> None:
        with patch.object(api, "_request", return_value=(200, {})) as request, patch.object(api, "_invalidate_telemetry_cache"):
            result = objects.save_object({"type": "origin", "x": 2, "y": -3, "confirm": True}, BASE)
        self.assertIsNone(result["object"])
        self.assertEqual(request.call_args.args[:3], ("PUT", "/api/core/slam/v1/maps/origin", {"new_origin": {"x": 2, "y": -3}}))
        with tempfile.TemporaryDirectory() as directory, patch.object(api, "ROBOT_MAP_POIS_FILE", Path(directory) / "pois.json"):
            with patch.object(api, "_request", side_effect=lambda method, path, **kwargs: (200, {"x": 1, "y": 2, "yaw": 0} if path.endswith("/pose") else [])) as request:
                result = objects.list_objects(BASE)
        self.assertFalse(result["errors"])
        self.assertEqual([item["type"] for item in result["objects"]], ["pose"])
        self.assertFalse(any(call.args[1].endswith("/origin") for call in request.call_args_list))
        self.assertTrue(all(call.kwargs["timeout"] == api._TELEMETRY_REQUEST_TIMEOUT_SECONDS
                            for call in request.call_args_list))

    def test_derived_nonfinite_rectangle_and_curved_track_are_not_written(self) -> None:
        payload = {"type": "forbidden", "name": "Area", "x": 1.7e308, "y": 0,
                   "width": 1.7e308, "height": 1}
        with patch.object(api, "_request", return_value=(200, [])) as request:
            with self.assertRaises(ValueError):
                objects.save_object(payload, BASE)
            self.assertEqual(request.call_count, 1)
        with patch.object(api, "_request", return_value=(200, [{"id": 1, "metadata": {"control_point1": "curve"}}])) as request:
            with self.assertRaises(ValueError):
                objects.save_object({"type": "track", "id": 1, "name": "Curve", "x": 0, "y": 0, "endX": 1, "endY": 1}, BASE)
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
