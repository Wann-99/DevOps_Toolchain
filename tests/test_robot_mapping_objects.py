"""Mapping object changes are bounded to the selected object and chassis."""

from copy import deepcopy
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


class MappingObjectTests(unittest.TestCase):
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
