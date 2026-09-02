"""Slamware map telemetry and motion boundary checks."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from ksq.web import robot_map_api as api


def _scan() -> dict[str, object]:
    return {
        "pose": {"x": 1.0, "y": 2.0, "yaw": 0.25},
        "laser_points": [
            {"distance": 1.2, "angle": -0.5, "valid": True},
            {"distance": 0.0, "angle": 0.0, "valid": False},
        ],
    }


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
        with patch.object(api, "_request", return_value=(200, payload)) as request:
            self.assertEqual(api.get_current_action(), payload)
        request.assert_called_once_with(
            "GET", "/api/core/motion/v1/actions/:current"
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
        with patch.object(api, "_create_action", return_value={}) as create_action:
            result = api.move_to("1.5", "-2", yaw="0.25", speed_ratio="0.4")
        self.assertEqual(result, {})
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
        )

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
