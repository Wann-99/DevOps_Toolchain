"""Slamware map telemetry and motion boundary checks."""

from __future__ import annotations

import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import call, patch

from PIL import Image

from ksq.web import robot_map_api as api


def _scan() -> dict[str, object]:
    return {
        "pose": {"x": 1.0, "y": 2.0, "yaw": 0.25},
        "laser_points": [
            {"distance": 1.2, "angle": -0.5, "valid": True},
            {"distance": 0.0, "angle": 0.0, "valid": False},
        ],
    }


class RobotMapImageTests(unittest.TestCase):
    def test_signed_grid_cells_use_official_slamware_palette_and_flip_y(self) -> None:
        cells = bytes([0, 128, 127, 255])
        header = struct.pack("<ffIIf12xI", 1.5, -2.0, 2, 2, 0.05, len(cells))
        with patch.object(api, "_request_bytes", return_value=header + cells):
            payload, metadata = api.get_map_image()

        with Image.open(io.BytesIO(payload)) as image:
            self.assertEqual(image.mode, "L")
            self.assertEqual(image.size, (2, 2))
            # Official conversion is uint8(128 + signed cell), then Y is flipped.
            self.assertEqual(list(image.tobytes()), [255, 127, 128, 0])
        self.assertEqual(metadata["width"], 2)
        self.assertEqual(metadata["height"], 2)
        self.assertAlmostEqual(metadata["resolution"], 0.05)


class RobotMapConnectionTests(unittest.TestCase):
    def test_write_guard_rejects_a_stale_robot_endpoint(self) -> None:
        current = "http://192.168.5.9:1448"
        with patch.object(api, "_base_url", return_value=current):
            self.assertEqual(api.require_current_base_url(None), current)
            self.assertEqual(api.require_current_base_url(current), current)
            with self.assertRaises(ValueError):
                api.require_current_base_url("http://192.168.5.10:1448")

    def test_switch_cancels_the_old_endpoint_before_saving(self) -> None:
        robot_a = "http://192.168.5.9:1448"
        robot_b = "http://192.168.5.10:1448"
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "robot_map_settings.json"
            _write_json(settings_file, {"robot_base_url": robot_a})
            with (
                patch.object(api, "ROBOT_MAP_SETTINGS_FILE", settings_file),
                patch.object(api, "_request", return_value=(200, {})) as request,
                patch.object(api, "_invalidate_telemetry_cache") as invalidate,
            ):
                settings = api.save_settings(
                    {
                        "robot_base_url": robot_b,
                        "expected_robot_base_url": robot_a,
                    }
                )

        self.assertEqual(settings, {"robot_base_url": robot_b})
        self.assertEqual(
            request.call_args_list,
            [
                call(
                    "GET",
                    "/api/core/system/v1/robot/info",
                    timeout=3,
                    base_url=robot_b,
                ),
                call(
                    "DELETE",
                    "/api/core/motion/v1/actions/:current",
                    base_url=robot_a,
                ),
            ],
        )
        invalidate.assert_called_once_with()

    def test_switch_is_rejected_when_the_old_endpoint_cannot_be_stopped(self) -> None:
        robot_a = "http://192.168.5.9:1448"
        robot_b = "http://192.168.5.10:1448"
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "robot_map_settings.json"
            _write_json(settings_file, {"robot_base_url": robot_a})
            with (
                patch.object(api, "ROBOT_MAP_SETTINGS_FILE", settings_file),
                patch.object(
                    api,
                    "_request",
                    side_effect=[
                        (200, {}),
                        api.RobotApiError("offline", status_code=504),
                    ],
                ),
                patch.object(api, "_invalidate_telemetry_cache") as invalidate,
            ):
                with self.assertRaisesRegex(
                    api.RobotConnectionSwitchRequired, "无法确认旧底盘已停止"
                ):
                    api.save_settings(
                        {
                            "robot_base_url": robot_b,
                            "expected_robot_base_url": robot_a,
                        }
                    )

            self.assertEqual(
                json.loads(settings_file.read_text(encoding="utf-8")),
                {"robot_base_url": robot_a},
            )
            invalidate.assert_not_called()

    def test_force_switch_recovers_after_old_endpoint_is_unreachable(self) -> None:
        robot_a = "http://192.168.5.9:1448"
        robot_b = "http://192.168.5.10:1448"
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "robot_map_settings.json"
            _write_json(settings_file, {"robot_base_url": robot_a})
            with (
                patch.object(api, "ROBOT_MAP_SETTINGS_FILE", settings_file),
                patch.object(
                    api,
                    "_request",
                    side_effect=[
                        (200, {}),
                        api.RobotApiError("offline", status_code=504),
                    ],
                ),
                patch.object(api, "_invalidate_telemetry_cache") as invalidate,
            ):
                settings = api.save_settings(
                    {
                        "robot_base_url": robot_b,
                        "expected_robot_base_url": robot_a,
                        "force_switch": True,
                    }
                )

            self.assertEqual(settings, {"robot_base_url": robot_b})
            self.assertEqual(
                json.loads(settings_file.read_text(encoding="utf-8")), settings
            )
            invalidate.assert_called_once_with()

    def test_unreachable_new_endpoint_is_never_saved(self) -> None:
        robot_a = "http://192.168.5.9:1448"
        robot_b = "http://192.168.5.10:1448"
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "robot_map_settings.json"
            _write_json(settings_file, {"robot_base_url": robot_a})
            with (
                patch.object(api, "ROBOT_MAP_SETTINGS_FILE", settings_file),
                patch.object(
                    api,
                    "_request",
                    side_effect=api.RobotApiError("offline", status_code=504),
                ),
                patch.object(api, "_invalidate_telemetry_cache") as invalidate,
            ):
                with self.assertRaises(api.RobotApiError):
                    api.save_settings(
                        {
                            "robot_base_url": robot_b,
                            "expected_robot_base_url": robot_a,
                        }
                    )

            self.assertEqual(
                json.loads(settings_file.read_text(encoding="utf-8")),
                {"robot_base_url": robot_a},
            )
            invalidate.assert_not_called()

    def test_save_settings_normalizes_http_ipv4_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "robot_map_settings.json"
            with (
                patch.object(api, "ROBOT_MAP_SETTINGS_FILE", settings_file),
                patch.object(
                    api,
                    "_request",
                    side_effect=[
                        (200, {}),
                        api.RobotApiError("idle", status_code=404),
                    ],
                ),
                patch.object(api, "_invalidate_telemetry_cache") as invalidate,
            ):
                settings = api.save_settings(
                    {"robot_base_url": "  HTTP://192.168.5.9:01448  "}
                )

            self.assertEqual(
                settings, {"robot_base_url": "http://192.168.5.9:1448"}
            )
            self.assertEqual(json.loads(settings_file.read_text(encoding="utf-8")), settings)
            invalidate.assert_called_once_with()

    def test_save_settings_rejects_non_ipv4_endpoint_shapes(self) -> None:
        invalid_urls = (
            "https://192.168.5.9:1448",
            "http://robot.local:1448",
            "http://[::1]:1448",
            "http://192.168.5.9",
            "http://192.168.5.9:0",
            "http://192.168.5.9:65536",
            "http://user:pass@192.168.5.9:1448",
            "http://192.168.5.9:1448/",
            "http://192.168.5.9:1448/api",
            "http://192.168.5.9:1448?debug=1",
            "http://192.168.5.9:1448?",
            "http://192.168.5.9:1448#fragment",
            "http://192.168.5.9:1448#",
        )
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "robot_map_settings.json"
            with (
                patch.object(api, "ROBOT_MAP_SETTINGS_FILE", settings_file),
                patch.object(api, "_invalidate_telemetry_cache") as invalidate,
            ):
                for base_url in invalid_urls:
                    with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                        api.save_settings({"robot_base_url": base_url})

            self.assertFalse(settings_file.exists())
            invalidate.assert_not_called()


class RobotMapPoiCacheTests(unittest.TestCase):
    def test_cache_is_partitioned_and_legacy_list_is_not_reused(self) -> None:
        robot_a = "http://192.168.5.9:1448"
        robot_b = "http://192.168.5.10:1448"
        poi_a = {"id": "a", "name": "A", "x": 1, "y": 2}
        poi_b = {"id": "b", "name": "B", "x": 3, "y": 4}
        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "robot_map_pois_cache.json"
            with patch.object(api, "ROBOT_MAP_POIS_FILE", cache_file):
                _write_json(cache_file, [poi_a])
                with (
                    patch.object(api, "_base_url", return_value=robot_b),
                    patch.object(
                        api, "_request", side_effect=api.RobotApiError("offline")
                    ) as request,
                ):
                    self.assertEqual(api.list_pois(), [])
                request.assert_called_once_with(
                    "GET", "/api/core/artifact/v1/pois", base_url=robot_b
                )

                api._save_poi_cache(robot_a, [poi_a])
                api._save_poi_cache(robot_b, [poi_b])
                for base_url, expected in ((robot_a, [poi_a]), (robot_b, [poi_b])):
                    with (
                        self.subTest(base_url=base_url),
                        patch.object(api, "_base_url", return_value=base_url),
                        patch.object(
                            api, "_request", side_effect=api.RobotApiError("offline")
                        ),
                    ):
                        self.assertEqual(api.list_pois(), expected)

    def test_create_and_delete_only_update_current_endpoint(self) -> None:
        robot_a = "http://192.168.5.9:1448"
        robot_b = "http://192.168.5.10:1448"
        poi_a = {"id": "a", "name": "A", "x": 1, "y": 2}
        poi_b = {"id": "b", "name": "B", "x": 3, "y": 4}
        current = {"base_url": robot_b}
        calls: list[tuple[str, str]] = []

        def request(method, path, payload=None, *, timeout=8, base_url=None):
            calls.append((method, base_url))
            return 200, {}

        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "robot_map_pois_cache.json"
            with (
                patch.object(api, "ROBOT_MAP_POIS_FILE", cache_file),
                patch.object(api, "_base_url", side_effect=lambda: current["base_url"]),
                patch.object(api, "_request", side_effect=request),
                patch.object(api.uuid, "uuid4", return_value="new"),
            ):
                api._save_poi_cache(robot_a, [poi_a])
                api._save_poi_cache(robot_b, [poi_b])
                created = api.create_poi("New", 5, 6)
                current["base_url"] = robot_a
                api.delete_poi("a")

                self.assertEqual(api._load_poi_cache(robot_a), [])
                self.assertEqual(
                    [item["id"] for item in api._load_poi_cache(robot_b)],
                    ["b", "new"],
                )

        self.assertEqual(created["id"], "new")
        self.assertEqual(calls, [("POST", robot_b), ("DELETE", robot_a)])

    def test_list_preserves_cached_order_when_robot_reorders(self) -> None:
        base_url = "http://192.168.5.9:1448"
        cached = [
            {"id": "b", "name": "B", "x": 2, "y": 2},
            {"id": "a", "name": "A", "x": 1, "y": 1},
        ]
        live = [
            {"id": "a", "pose": {"x": 1, "y": 1}, "metadata": {"display_name": "A"}},
            {"id": "b", "pose": {"x": 2, "y": 2}, "metadata": {"display_name": "B"}},
            {"id": "c", "pose": {"x": 3, "y": 3}, "metadata": {"display_name": "C"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "robot_map_pois_cache.json"
            _write_json(cache_file, {"version": 3, "endpoints": {base_url: cached}})
            with (
                patch.object(api, "ROBOT_MAP_POIS_FILE", cache_file),
                patch.object(api, "_base_url", return_value=base_url),
                patch.object(api, "_request", return_value=(200, live)),
            ):
                result = api.list_pois()

            self.assertEqual([item["id"] for item in result], ["b", "a", "c"])
            saved = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 3)
            self.assertEqual(
                [item["id"] for item in saved["endpoints"][base_url]],
                ["b", "a", "c"],
            )

    def test_version_two_default_names_recover_creation_order(self) -> None:
        base_url = "http://192.168.5.9:1448"
        cached = [
            {"id": "two", "name": "停留点2", "x": 2, "y": 2},
            {"id": "one", "name": "停留点1", "x": 1, "y": 1},
        ]
        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "robot_map_pois_cache.json"
            _write_json(cache_file, {"version": 2, "endpoints": {base_url: cached}})
            with patch.object(api, "ROBOT_MAP_POIS_FILE", cache_file):
                result = api._load_poi_cache(base_url)

        self.assertEqual([item["id"] for item in result], ["one", "two"])

    def test_fresh_cache_orders_default_named_live_pois_numerically(self) -> None:
        base_url = "http://192.168.5.9:1448"
        live = [
            {"id": "two", "pose": {"x": 2, "y": 2}, "metadata": {"display_name": "停留点2"}},
            {"id": "one", "pose": {"x": 1, "y": 1}, "metadata": {"display_name": "停留点1"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "robot_map_pois_cache.json"
            with (
                patch.object(api, "ROBOT_MAP_POIS_FILE", cache_file),
                patch.object(api, "_base_url", return_value=base_url),
                patch.object(api, "_request", return_value=(200, live)),
            ):
                result = api.list_pois()

        self.assertEqual([item["id"] for item in result], ["one", "two"])


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class RobotMapTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._reset_cache()

    def tearDown(self) -> None:
        self._reset_cache()

    @staticmethod
    def _reset_cache() -> None:
        with api._TELEMETRY_CONDITION:
            api._TELEMETRY_SNAPSHOT = None
            api._TELEMETRY_REFRESHING = False
            api._TELEMETRY_NEXT_REFRESH_MONOTONIC = 0.0
            api._TELEMETRY_SEQUENCE = 0
            api._TELEMETRY_GENERATION = 0
            api._TELEMETRY_CONDITION.notify_all()

    def test_normalize_laser_scan_keeps_invalid_slots(self) -> None:
        normalized = api.normalize_laser_scan(_scan())
        self.assertEqual(normalized["pose"]["x"], 1.0)
        self.assertEqual(len(normalized["laser_points"]), 2)
        self.assertFalse(normalized["laser_points"][1]["valid"])
        info = api._sensor_info(normalized["laser_points"])
        self.assertEqual(info["valid_count"], 1)
        self.assertEqual(info["observed_angle_min"], -0.5)
        self.assertEqual(info["observed_angle_max"], 0.0)

    def test_normalize_laser_scan_rejects_malformed_frame(self) -> None:
        with self.assertRaises(api.RobotApiError):
            api.normalize_laser_scan({"laser_points": []})
        with self.assertRaises(api.RobotApiError):
            api.normalize_laser_scan(
                {
                    "pose": {"x": 0, "y": 0},
                    "laser_points": [{"distance": "nan", "angle": 0}],
                }
            )

    def test_quality_accepts_integer_response_and_rejects_out_of_range(self) -> None:
        with patch.object(
            api, "_request", return_value=(200, 87)
        ) as request:
            self.assertEqual(api.get_localization_quality(timeout=0.2), 87)
        request.assert_called_once_with(
            "GET", "/api/core/slam/v1/localization/quality", timeout=0.2
        )
        with patch.object(api, "_request", return_value=(200, 101)):
            with self.assertRaises(api.RobotApiError):
                api.get_localization_quality()

    def test_current_action_reads_the_read_only_current_endpoint(self) -> None:
        payload = {
            "action_id": 12,
            "action_name": "slamtec.agent.actions.MoveToAction",
            "stage": "GOING_TO_TARGET",
            "state": {"status": 1, "result": 0},
        }
        base_url = "http://192.168.5.9:1448"
        with (
            patch.object(api, "_base_url", return_value=base_url),
            patch.object(api, "_request", return_value=(200, payload)) as request,
        ):
            self.assertEqual(api.get_current_action(), payload)
        request.assert_called_once_with(
            "GET", "/api/core/motion/v1/actions/:current", base_url=base_url
        )

    def test_move_to_rejects_non_finite_and_out_of_range_motion_values(self) -> None:
        with patch.object(api, "_create_action") as create_action:
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field="x", value=value):
                    with self.assertRaises(ValueError):
                        api.move_to(value, 1.0)
            with self.subTest(field="speed_ratio", value=1.1):
                with self.assertRaises(ValueError):
                    api.move_to(0.0, 1.0, speed_ratio=1.1)
            with self.subTest(field="speed_ratio", value=-0.1):
                with self.assertRaises(ValueError):
                    api.move_to(0.0, 1.0, speed_ratio=-0.1)
            with self.subTest(field="speed_ratio", value=0.05):
                with self.assertRaises(ValueError):
                    api.move_to(0.0, 1.0, speed_ratio=0.05)
            with self.subTest(field="yaw", value=float("nan")):
                with self.assertRaises(ValueError):
                    api.move_to(0.0, 1.0, yaw=float("nan"))
        create_action.assert_not_called()

    def test_move_to_normalizes_valid_motion_values(self) -> None:
        base_url = "http://192.168.5.9:1448"
        with (
            patch.object(api, "_base_url", return_value=base_url),
            patch.object(api, "_request") as request,
            patch.object(api, "_create_action", return_value={}) as create_action,
        ):
            result = api.move_to("1.5", "-2", yaw="0.25", speed_ratio="0.4")
        self.assertEqual(result, {})
        request.assert_not_called()
        create_action.assert_called_once_with(
            "MoveToAction",
            {
                "target": {"x": 1.5, "y": -2.0, "z": 0},
                "move_options": {
                    "mode": 0,
                    "flags": ["precise", "with_yaw"],
                    "speed_ratio": 0.4,
                    "yaw": 0.25,
                },
            },
            base_url=base_url,
        )

    def test_move_to_converts_mps_using_the_robot_speed_limit(self) -> None:
        base_url = "http://192.168.5.9:1448"
        with (
            patch.object(api, "_base_url", return_value=base_url),
            patch.object(api, "_request", return_value=(200, "0.8")) as request,
            patch.object(api, "_create_action", return_value={}) as create_action,
        ):
            result = api.move_to(1, 2, speed_mps=0.4)
        self.assertEqual(result, {})
        request.assert_called_once_with(
            "GET",
            "/api/core/system/v1/parameter?param=base.max_moving_speed",
            base_url=base_url,
            accept="text/plain",
        )
        self.assertEqual(
            create_action.call_args.args[1]["move_options"]["speed_ratio"], 0.5
        )
        self.assertEqual(create_action.call_args.kwargs["base_url"], base_url)

    def test_move_to_rejects_unusable_or_out_of_range_mps(self) -> None:
        base_url = "http://192.168.5.9:1448"
        for maximum in ("high", 0, float("nan")):
            with self.subTest(maximum=maximum), patch.object(
                api, "_base_url", return_value=base_url
            ), patch.object(
                api, "_request", return_value=(200, maximum)
            ), patch.object(api, "_create_action") as create_action:
                with self.assertRaises(api.RobotApiError):
                    api.move_to(1, 2, speed_mps=0.4)
                create_action.assert_not_called()

        for speed in (0.07, 0.81):
            with self.subTest(speed=speed), patch.object(
                api, "_base_url", return_value=base_url
            ), patch.object(
                api, "_request", return_value=(200, "0.8")
            ), patch.object(api, "_create_action") as create_action:
                with self.assertRaises(ValueError):
                    api.move_to(1, 2, speed_mps=speed)
                create_action.assert_not_called()

    def test_snapshot_is_shared_and_marks_partial_refresh_stale(self) -> None:
        parts = {
            "scan": _scan(),
            "pose": {"x": 1.1, "y": 2.1, "yaw": 0.3},
            "quality": 92,
        }
        with patch.object(api, "_fetch_telemetry_parts", return_value=(parts, {})) as fetch:
            first = api.get_telemetry_snapshot(force=True)
            second = api.get_telemetry_snapshot()
        self.assertEqual(first["seq"], 1)
        self.assertEqual(first["laser_points"], _scan()["laser_points"])
        self.assertEqual(first["scan_pose"]["x"], 1.0)
        self.assertEqual(first["localization_pose"]["x"], 1.1)
        self.assertEqual(first["localization_quality"], 92)
        self.assertFalse(first["stale"])
        self.assertFalse(first["partial"])
        self.assertEqual(second["seq"], first["seq"])
        fetch.assert_called_once()

        with patch.object(
            api,
            "_fetch_telemetry_parts",
            return_value=(
                {"pose": {"x": 1.2, "y": 2.2, "yaw": 0.4}},
                {"scan": {"message": "timeout", "status_code": 504}},
            ),
        ):
            partial = api.get_telemetry_snapshot(force=True)
        self.assertTrue(partial["stale"])
        self.assertTrue(partial["partial"])
        self.assertEqual(partial["seq"], first["seq"])
        self.assertEqual(partial["laser_points"], first["laser_points"])
        self.assertEqual(partial["pose"]["x"], 1.2)
        self.assertIn("scan", partial["errors"])

    def test_concurrent_requests_use_single_flight_refresh(self) -> None:
        calls = 0
        lock = threading.Lock()
        entered = threading.Event()
        release = threading.Event()

        def fetch() -> tuple[dict[str, object], dict[str, object]]:
            nonlocal calls
            with lock:
                calls += 1
            entered.set()
            release.wait(timeout=1)
            return (
                {"scan": _scan(), "pose": _scan()["pose"], "quality": 90},
                {},
            )

        results: list[dict[str, object]] = []
        with patch.object(api, "_fetch_telemetry_parts", side_effect=fetch):
            first = threading.Thread(
                target=lambda: results.append(api.get_telemetry_snapshot(force=True))
            )
            second = threading.Thread(
                target=lambda: results.append(api.get_telemetry_snapshot(force=True))
            )
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second.start()
            time.sleep(0.02)
            release.set()
            first.join(timeout=1)
            second.join(timeout=1)
        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual({item["seq"] for item in results}, {1})


if __name__ == "__main__":
    unittest.main()
