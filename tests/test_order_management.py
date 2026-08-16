from __future__ import annotations

import inspect
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from ksq.order import broker
from ksq.order import config as order_config
from ksq.order.payload import build_create_task_body
from ksq.web import auth, dashboard_api, order_api

try:
    from ksq.web.handlers import QueryHandler
except ModuleNotFoundError as error:
    if error.name != "cgi":
        raise
    QueryHandler = None


VALID_CONFIG = {
    "server": "https://broker.example.test/",
    "client_id": "client",
    "client_secret": "secret",
    "store_id": "dashenlin_test",
}


class ConfigurationDefaultsTests(unittest.TestCase):
    def test_new_mode_starts_with_empty_editable_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = order_config.load_order_config(
                Path(directory) / "missing-order-config.json"
            )

        for key in ("server", "customer", "client_id", "client_secret", "store_id"):
            self.assertEqual(config[key], "")
        self.assertEqual(config["order_source"], "")
        self.assertEqual(config["business_mode_code"], "")

    def test_customer_options_include_none_and_shuyu(self) -> None:
        public = order_config.public_order_config(order_config.default_order_config())
        self.assertEqual(
            public["customers"],
            [
                {"value": "", "cn": "—"},
                {"value": "dashenlin", "cn": "大参林"},
                {"value": "yaoshibang", "cn": "药师帮"},
                {"value": "shuyu", "cn": "漱玉"},
            ],
        )
        shuyu = order_config.apply_customer_defaults({"customer": "shuyu"})
        self.assertEqual(shuyu["order_source"], "meituan")
        self.assertEqual(shuyu["business_mode_code"], "MODE_PICK_WAIT_PACK")

    def test_unselected_customer_cannot_silently_create_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "请先选择客户"):
            build_create_task_body(
                {"store_id": "store-1", "order_source": ""},
                [{"item_id": "item-1", "location_code": "A-01"}],
            )

    def test_feishu_default_is_disabled(self) -> None:
        self.assertFalse(dashboard_api._default_feishu_settings()["enabled"])


class BrokerClientTests(unittest.TestCase):
    def test_list_tasks_builds_expected_query(self) -> None:
        with patch.object(
            broker, "_request_json", return_value=(200, {"data": {"tasks": []}})
        ) as request:
            broker.list_robot_tasks(
                VALID_CONFIG["server"],
                "token",
                VALID_CONFIG["store_id"],
                25,
                "desc",
                "running",
                "Asia/Shanghai",
                "cursor-token",
            )

        method, url, payload, token = request.call_args.args
        self.assertEqual(method, "GET")
        self.assertIsNone(payload)
        self.assertEqual(token, "token")
        parsed = urlparse(url)
        self.assertEqual(parsed.path, "/api/robot-tasks")
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "store_id": ["dashenlin_test"],
                "page_size": ["25"],
                "order_by": ["desc"],
                "status": ["running"],
                "tz": ["Asia/Shanghai"],
                "cursor": ["cursor-token"],
            },
        )

    def test_manual_order_paths_encode_order_number(self) -> None:
        with patch.object(broker, "_request_json", return_value=(200, {})) as request:
            broker.manual_claim_order("https://broker.example.test", "token", "A/B 1")
            broker.manual_complete_order("https://broker.example.test", "token", "A/B 1")

        self.assertEqual(
            request.call_args_list[0].args,
            (
                "POST",
                "https://broker.example.test/api/orders/A%2FB%201/manual-claim",
                None,
                "token",
            ),
        )
        self.assertEqual(
            request.call_args_list[1].args,
            (
                "POST",
                "https://broker.example.test/api/orders/A%2FB%201/manual-complete",
                None,
                "token",
            ),
        )


class TaskListTests(unittest.TestCase):
    def setUp(self) -> None:
        order_api.clear_task_list_cache()

    def tearDown(self) -> None:
        order_api.clear_task_list_cache()

    def test_uses_cursor_and_returns_distinct_local_pages_with_total(self) -> None:
        upstream_pages = [
            (
                200,
                {
                    "data": {
                        "tasks": [
                            {"task_id": "task-1", "status": "running"},
                            {"task_id": "task-2", "status": "running"},
                        ],
                        "has_more": True,
                        "next_cursor": "cursor-2",
                    }
                },
            ),
            (
                200,
                {
                    "data": {
                        "tasks": [{"task_id": "task-3", "status": "running"}],
                        "has_more": False,
                    }
                },
            ),
        ]
        with (
            patch.object(order_api, "load_order_config", return_value=VALID_CONFIG),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(
                order_api.broker,
                "list_robot_tasks",
                side_effect=upstream_pages,
            ) as list_tasks,
        ):
            first = order_api.list_tasks(
                "test", page="1", page_size="1", status="running"
            )
            second = order_api.list_tasks(
                "test", page="2", page_size="1", status="running"
            )

        self.assertEqual(first["tasks"], [{"task_id": "task-1", "status": "running"}])
        self.assertEqual(second["tasks"], [{"task_id": "task-2", "status": "running"}])
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["total_pages"], 3)
        self.assertTrue(first["has_more"])
        self.assertTrue(second["cached"])
        self.assertEqual(list_tasks.call_count, 2)
        self.assertEqual(list_tasks.call_args_list[0].args[2], "dashenlin_test")
        self.assertEqual(list_tasks.call_args_list[0].args[-1], "")
        self.assertEqual(list_tasks.call_args_list[1].args[-1], "cursor-2")
        self.assertNotIn("store_id", inspect.signature(order_api.list_tasks).parameters)

    def test_clamps_page_after_total_changes(self) -> None:
        upstream = {
            "data": {
                "tasks": [{"task_id": "task-1"}],
                "has_more": False,
            }
        }
        with (
            patch.object(order_api, "load_order_config", return_value=VALID_CONFIG),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(
                order_api.broker,
                "list_robot_tasks",
                return_value=(200, upstream),
            ),
        ):
            result = order_api.list_tasks("test", page=99, page_size=10)

        self.assertEqual(result["page"], 1)
        self.assertEqual(result["tasks"], [{"task_id": "task-1"}])
        self.assertEqual(result["total"], 1)
        self.assertFalse(result["has_more"])

    def test_manual_refresh_bypasses_cached_total(self) -> None:
        responses = [
            (200, {"data": {"tasks": [{"task_id": "task-1"}], "has_more": False}}),
            (
                200,
                {
                    "data": {
                        "tasks": [{"task_id": "task-1"}, {"task_id": "task-2"}],
                        "has_more": False,
                    }
                },
            ),
        ]
        with (
            patch.object(order_api, "load_order_config", return_value=VALID_CONFIG),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(
                order_api.broker,
                "list_robot_tasks",
                side_effect=responses,
            ) as list_tasks,
        ):
            first = order_api.list_tasks("test")
            cached = order_api.list_tasks("test")
            refreshed = order_api.list_tasks("test", refresh="1")

        self.assertEqual(first["total"], 1)
        self.assertEqual(cached["total"], 1)
        self.assertTrue(cached["cached"])
        self.assertEqual(refreshed["total"], 2)
        self.assertFalse(refreshed["cached"])
        self.assertEqual(list_tasks.call_count, 2)

    def test_rejects_invalid_paging_sort_status_and_timezone(self) -> None:
        invalid_cases = (
            {"page": 0},
            {"page_size": 0},
            {"page_size": 51},
            {"order_by": "newest"},
            {"status": "unknown"},
            {"timezone_name": "UTC"},
        )
        for arguments in invalid_cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    order_api.list_tasks("test", **arguments)

    def test_retries_once_after_unauthorized(self) -> None:
        unauthorized = broker.OrderBrokerError("expired", 401, {"msg": "expired"})
        request = Mock(side_effect=[unauthorized, (200, {"ok": True})])
        with (
            patch.object(order_api, "_ensure_token", side_effect=["old", "new"]) as token,
            patch.object(order_api, "_clear_token") as clear,
        ):
            result = order_api._request_with_token_retry(
                VALID_CONFIG, "test", request
            )

        self.assertEqual(result, (200, {"ok": True}))
        self.assertEqual(request.call_args_list[0].args, ("old",))
        self.assertEqual(request.call_args_list[1].args, ("new",))
        self.assertEqual(token.call_count, 2)
        clear.assert_called_once_with(VALID_CONFIG)

    def test_does_not_retry_non_auth_upstream_error(self) -> None:
        failure = broker.OrderBrokerError("invalid", 422, {"code": 10001})
        request = Mock(side_effect=failure)
        with (
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(order_api, "_clear_token") as clear,
        ):
            with self.assertRaises(broker.OrderBrokerError):
                order_api._request_with_token_retry(VALID_CONFIG, "test", request)
        self.assertEqual(request.call_count, 1)
        clear.assert_not_called()


class CurrentOrderActionTests(unittest.TestCase):
    STATUS_MATRIX = {
        "pending": {"cancel"},
        "dispatched": {"cancel"},
        "running": {"cancel", "manual_claim"},
        "awaiting_pack": {"cancel"},
        "manual_transferred": {"manual_complete"},
        "success": set(),
        "error": set(),
        "cancel": set(),
        "manual_transferred_completed": set(),
    }

    def test_full_status_matrix(self) -> None:
        for status, allowed in self.STATUS_MATRIX.items():
            for action in ("cancel", "manual_claim", "manual_complete"):
                with self.subTest(status=status, action=action):
                    context = (
                        VALID_CONFIG,
                        "current-task",
                        {"task_id": "current-task", "order_no": "ORDER-1", "status": status},
                        "ORDER-1",
                    )
                    with (
                        patch.object(
                            order_api, "_current_task_context", return_value=context
                        ),
                        patch.object(
                            order_api,
                            "_request_with_token_retry",
                            return_value=(200, {"ok": True}),
                        ) as request,
                    ):
                        if action in allowed:
                            result = order_api.operate_current_order(
                                action, "operator request"
                            )
                            self.assertTrue(result["ok"])
                            request.assert_called_once()
                        else:
                            with self.assertRaises(order_api.CurrentOrderConflict):
                                order_api.operate_current_order(
                                    action, "operator request"
                                )
                            request.assert_not_called()

    def test_cancel_always_uses_current_task_and_user_type(self) -> None:
        context = (
            VALID_CONFIG,
            "current-task",
            {"task_id": "current-task", "order_no": "ORDER-1", "status": "running"},
            "ORDER-1",
        )
        with (
            patch.object(order_api, "_current_task_context", return_value=context),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(
                order_api.broker, "cancel_robot_task", return_value=(200, {"ok": True})
            ) as cancel,
        ):
            order_api.operate_current_order("cancel", "operator request")

        cancel.assert_called_once_with(
            VALID_CONFIG["server"],
            "token",
            "current-task",
            "user",
            "operator request",
        )
        self.assertEqual(
            set(inspect.signature(order_api.operate_current_order).parameters),
            {"action", "cancel_reason"},
        )

    def test_rejects_empty_cancel_reason(self) -> None:
        context = (
            VALID_CONFIG,
            "current-task",
            {"task_id": "current-task", "order_no": "ORDER-1", "status": "pending"},
            "ORDER-1",
        )
        with patch.object(order_api, "_current_task_context", return_value=context):
            with self.assertRaises(ValueError):
                order_api.operate_current_order("cancel", "  ")

    def test_production_mode_is_forbidden_before_reading_active_order(self) -> None:
        with (
            patch.object(dashboard_api, "resolve_dashboard_mode", return_value="prod"),
            patch.object(dashboard_api, "get_active_order") as active,
        ):
            with self.assertRaises(order_api.ProductionOrderWriteForbidden):
                order_api.operate_current_order("manual_claim")
        active.assert_not_called()

    def test_missing_current_order_is_a_conflict(self) -> None:
        with (
            patch.object(dashboard_api, "resolve_dashboard_mode", return_value="test"),
            patch.object(dashboard_api, "get_active_order", return_value=None),
        ):
            with self.assertRaises(order_api.CurrentOrderConflict):
                order_api.operate_current_order("manual_claim")

    def test_missing_order_number_is_a_conflict(self) -> None:
        with (
            patch.object(dashboard_api, "resolve_dashboard_mode", return_value="test"),
            patch.object(
                dashboard_api,
                "get_active_order",
                return_value={
                    "task_id": "current-task",
                    "order_no": "STALE-ORDER",
                },
            ),
            patch.object(order_api, "load_order_config", return_value=VALID_CONFIG),
            patch.object(
                order_api,
                "_request_with_token_retry",
                return_value=(200, {"data": {"task_id": "current-task", "status": "running"}}),
            ),
        ):
            with self.assertRaises(order_api.CurrentOrderConflict):
                order_api.operate_current_order("manual_claim")

    def test_upstream_failure_is_preserved_for_handler_mapping(self) -> None:
        context = (
            VALID_CONFIG,
            "current-task",
            {"task_id": "current-task", "order_no": "ORDER-1", "status": "running"},
            "ORDER-1",
        )
        failure = broker.OrderBrokerError("failed", 503, {"message": "unavailable"})
        with (
            patch.object(order_api, "_current_task_context", return_value=context),
            patch.object(order_api, "_request_with_token_retry", side_effect=failure),
        ):
            with self.assertRaises(broker.OrderBrokerError) as raised:
                order_api.operate_current_order("manual_claim")
        self.assertIs(raised.exception, failure)


class OperateTaskTests(unittest.TestCase):
    STATUS_MATRIX = {
        "pending": {"cancel"},
        "dispatched": {"cancel"},
        "running": {"cancel", "manual_claim"},
        "awaiting_pack": {"cancel"},
        "manual_transferred": {"manual_complete"},
        "success": set(),
        "error": set(),
        "cancel": set(),
        "manual_transferred_completed": set(),
    }

    def _operate(
        self,
        action: str,
        status: str,
        active_order: object = None,
        **kwargs: object,
    ) -> tuple:
        detail = (
            200,
            {
                "data": {
                    "task_id": "task-9",
                    "order_no": "ORDER-9",
                    "status": status,
                }
            },
        )
        with (
            patch.object(dashboard_api, "resolve_dashboard_mode", return_value="test"),
            patch.object(dashboard_api, "get_active_order", return_value=active_order),
            patch.object(
                dashboard_api, "order_queue_status", return_value={"total": 1}
            ),
            patch.object(order_api, "load_order_config", return_value=VALID_CONFIG),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(
                order_api.broker, "get_robot_task", return_value=detail
            ),
            patch.object(
                order_api.broker,
                "cancel_robot_task",
                return_value=(200, {"ok": True}),
            ) as cancel,
            patch.object(
                order_api.broker,
                "manual_claim_order",
                return_value=(200, {"ok": True}),
            ) as claim,
            patch.object(
                order_api.broker,
                "manual_complete_order",
                return_value=(200, {"ok": True}),
            ) as complete,
            patch.object(order_api, "clear_task_list_cache") as clear_cache,
        ):
            result = order_api.operate_task(action, "task-9", **kwargs)
        return result, cancel, claim, complete, clear_cache

    def test_full_status_matrix(self) -> None:
        for status, allowed in self.STATUS_MATRIX.items():
            for action in ("cancel", "manual_claim", "manual_complete"):
                with self.subTest(status=status, action=action):
                    if action in allowed:
                        result, *_ = self._operate(
                            action, status, cancel_reason="operator request"
                        )
                        self.assertTrue(result["ok"])
                        self.assertEqual(result["action"], action)
                        self.assertEqual(result["task_id"], "task-9")
                        self.assertEqual(result["order_no"], "ORDER-9")
                    else:
                        with self.assertRaises(order_api.TaskOperationConflict):
                            self._operate(
                                action, status, cancel_reason="operator request"
                            )

    def test_cancel_type_defaults_to_user(self) -> None:
        _, cancel, *_ = self._operate(
            "cancel", "running", cancel_reason="operator request"
        )
        cancel.assert_called_once_with(
            VALID_CONFIG["server"],
            "token",
            "task-9",
            "user",
            "operator request",
        )

    def test_cancel_type_passthrough_and_empty_fallback(self) -> None:
        _, cancel, *_ = self._operate(
            "cancel",
            "pending",
            cancel_reason="operator request",
            cancel_type="system",
        )
        self.assertEqual(cancel.call_args.args[3], "system")

        _, cancel, *_ = self._operate(
            "cancel",
            "pending",
            cancel_reason="operator request",
            cancel_type="  ",
        )
        self.assertEqual(cancel.call_args.args[3], "user")

    def test_success_clears_task_list_cache(self) -> None:
        _, _, claim, _, clear_cache = self._operate("manual_claim", "running")
        claim.assert_called_once()
        clear_cache.assert_called_once_with()

    def test_queue_status_attached_when_task_is_active_order(self) -> None:
        result, *_ = self._operate(
            "manual_claim", "running", active_order={"task_id": "task-9"}
        )
        self.assertEqual(result["queue"], {"total": 1})

        other, *_ = self._operate(
            "manual_claim", "running", active_order={"task_id": "other-task"}
        )
        self.assertNotIn("queue", other)

    def test_production_mode_is_forbidden_before_broker_call(self) -> None:
        with (
            patch.object(dashboard_api, "resolve_dashboard_mode", return_value="prod"),
            patch.object(order_api.broker, "get_robot_task") as detail,
        ):
            with self.assertRaises(order_api.ProductionOrderWriteForbidden):
                order_api.operate_task("cancel", "task-9", "operator request")
        detail.assert_not_called()

    def test_rejects_empty_cancel_reason(self) -> None:
        with self.assertRaises(ValueError):
            self._operate("cancel", "running", cancel_reason="  ")

    def test_rejects_unknown_action(self) -> None:
        with self.assertRaises(ValueError):
            order_api.operate_task("pause", "task-9")

    def test_rejects_empty_task_id(self) -> None:
        with self.assertRaises(ValueError):
            order_api.operate_task("cancel", "  ", "operator request")

    def test_mismatched_broker_task_is_a_conflict(self) -> None:
        detail = (200, {"data": {"task_id": "other-task", "status": "running"}})
        with (
            patch.object(dashboard_api, "resolve_dashboard_mode", return_value="test"),
            patch.object(order_api, "load_order_config", return_value=VALID_CONFIG),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(order_api.broker, "get_robot_task", return_value=detail),
        ):
            with self.assertRaises(order_api.TaskOperationConflict):
                order_api.operate_task("cancel", "task-9", "operator request")


class ErrorPayloadTests(unittest.TestCase):
    def test_extracts_summary_codes_and_redacts_secrets(self) -> None:
        error = broker.OrderBrokerError(
            "request failed",
            422,
            {
                "message": "invalid order",
                "code": 10001,
                "trace_id": "trace-1",
                "Authorization": "Bearer abc",
                "data": {
                    "client-secret": "secret",
                    "password": "password",
                    "items": [{"token": "token", "name": "safe"}],
                },
            },
        )

        result = order_api.broker_error_payload(error)

        self.assertEqual(result["error"], "invalid order")
        self.assertEqual(result["upstream_status"], 422)
        self.assertEqual(result["upstream_code"], 10001)
        self.assertEqual(result["request_id"], "trace-1")
        self.assertEqual(result["upstream"]["Authorization"], "***")
        self.assertEqual(result["upstream"]["data"]["client-secret"], "***")
        self.assertEqual(result["upstream"]["data"]["password"], "***")
        self.assertEqual(result["upstream"]["data"]["items"][0]["token"], "***")
        self.assertEqual(result["upstream"]["data"]["items"][0]["name"], "safe")

    def test_redacts_secrets_from_plain_text_upstream(self) -> None:
        value = order_api.sanitize_upstream(
            "Authorization: Bearer abc.def client_secret=plain password: hidden"
        )
        self.assertNotIn("abc.def", value)
        self.assertNotIn("plain", value)
        self.assertNotIn("hidden", value)
        self.assertIn("***", value)

    def test_detail_lookup_does_not_change_active_order(self) -> None:
        with (
            patch.object(dashboard_api, "resolve_dashboard_mode", return_value="test"),
            patch.object(dashboard_api, "get_active_order") as active,
            patch.object(order_api, "load_order_config", return_value=VALID_CONFIG),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(
                order_api.broker,
                "get_robot_task",
                return_value=(200, {"data": {"task_id": "other-task"}}),
            ),
        ):
            result = order_api.get_task_detail("other-task")

        self.assertEqual(result[1]["data"]["task_id"], "other-task")
        active.assert_not_called()


@unittest.skipIf(QueryHandler is None, "HTTP handlers require Python 3.12 or older")
class OrderRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        # 路由测试默认以管理员会话通过鉴权，聚焦原有路由行为。
        patcher = patch.object(
            auth,
            "session_from_cookie",
            return_value={
                "username": "admin",
                "display_name": "管理员",
                "role": auth.ROLE_ADMIN,
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(
        self, method: str, path: str, payload: object = None
    ) -> tuple[int, object]:
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        handler = QueryHandler.__new__(QueryHandler)
        handler.path = path
        handler.headers = {
            "Content-Length": str(len(raw)),
            "Content-Type": "application/json",
        }
        handler.rfile = io.BytesIO(raw)
        response: dict[str, object] = {}

        def send_json(status: object, data: object) -> None:
            response.update(status=int(status), data=data)

        def send_error(status: object, message: str = "") -> None:
            response.update(status=int(status), data={"error": message})

        handler._send_json = send_json
        handler.send_error = send_error
        if method == "GET":
            handler.do_GET()
        elif method == "POST":
            handler.do_POST()
        else:
            raise AssertionError(f"Unsupported test method: {method}")
        return int(response["status"]), response["data"]

    def test_list_ignores_browser_store_id(self) -> None:
        expected = {
            "mode": "test",
            "store_id": "configured-store",
            "page": 1,
            "page_size": 10,
            "order_by": "desc",
            "status": "",
            "tasks": [],
            "has_more": False,
        }
        with (
            patch.object(dashboard_api, "resolve_dashboard_mode", return_value="test"),
            patch.object(order_api, "list_tasks", return_value=expected) as list_tasks,
        ):
            status, data = self.request(
                "GET", "/api/order/tasks?store_id=evil&page=1&page_size=10"
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, expected)
        self.assertNotIn("store_id", list_tasks.call_args.kwargs)

    def test_cancel_ignores_browser_task_and_order_identifiers(self) -> None:
        result = {
            "ok": True,
            "action": "cancel",
            "task_id": "current-task",
            "order_no": "CURRENT-ORDER",
        }
        with patch.object(
            order_api, "operate_current_order", return_value=result
        ) as operate:
            status, data = self.request(
                "POST",
                "/api/order/current/cancel",
                {
                    "cancel_reason": "operator request",
                    "task_id": "other-task",
                    "order_no": "OTHER-ORDER",
                    "cancel_type": "system",
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, result)
        operate.assert_called_once_with("cancel", "operator request")

    def test_current_action_status_mapping(self) -> None:
        cases = (
            (
                order_api.ProductionOrderWriteForbidden("prod forbidden"),
                403,
            ),
            (order_api.CurrentOrderConflict("no active order"), 409),
            (
                broker.OrderBrokerError(
                    "upstream failed",
                    503,
                    {"message": "unavailable", "client_secret": "secret"},
                ),
                502,
            ),
        )
        for failure, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                with patch.object(
                    order_api, "operate_current_order", side_effect=failure
                ):
                    status, data = self.request(
                        "POST", "/api/order/current/manual-claim", {}
                    )
                self.assertEqual(status, expected_status)
                self.assertIn("error", data)
                if expected_status == 502:
                    self.assertEqual(data["upstream_status"], 503)
                    self.assertEqual(data["upstream"]["client_secret"], "***")

    def test_task_cancel_route_passes_task_id_reason_and_type(self) -> None:
        result = {"ok": True, "action": "cancel", "task_id": "other-task"}
        with patch.object(
            order_api, "operate_task", return_value=result
        ) as operate:
            status, data = self.request(
                "POST",
                "/api/order/tasks/other-task/cancel",
                {"cancel_reason": "operator request"},
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, result)
        operate.assert_called_once_with(
            "cancel", "other-task", "operator request", "user"
        )

    def test_task_cancel_route_passes_custom_cancel_type(self) -> None:
        with patch.object(
            order_api, "operate_task", return_value={"ok": True}
        ) as operate:
            status, _ = self.request(
                "POST",
                "/api/order/tasks/other-task/cancel",
                {"cancel_reason": "operator request", "cancel_type": "system"},
            )

        self.assertEqual(status, 200)
        operate.assert_called_once_with(
            "cancel", "other-task", "operator request", "system"
        )

    def test_task_manual_routes_accept_empty_body(self) -> None:
        for suffix, action in (
            ("manual-claim", "manual_claim"),
            ("manual-complete", "manual_complete"),
        ):
            with self.subTest(action=action):
                with patch.object(
                    order_api, "operate_task", return_value={"ok": True}
                ) as operate:
                    status, _ = self.request(
                        "POST", f"/api/order/tasks/other-task/{suffix}"
                    )
                self.assertEqual(status, 200)
                operate.assert_called_once_with(
                    action, "other-task", None, "user"
                )

    def test_task_route_rejects_invalid_task_id(self) -> None:
        for path in (
            "/api/order/tasks//cancel",
            "/api/order/tasks/a%2Fb/cancel",
        ):
            with self.subTest(path=path):
                with patch.object(order_api, "operate_task") as operate:
                    status, data = self.request(
                        "POST", path, {"cancel_reason": "reason"}
                    )
                self.assertEqual(status, 400)
                self.assertEqual(data, {"error": "task_id 无效。"})
                operate.assert_not_called()

    def test_task_action_status_mapping(self) -> None:
        cases = (
            (order_api.ProductionOrderWriteForbidden("prod forbidden"), 403),
            (order_api.TaskOperationConflict("status conflict"), 409),
            (
                broker.OrderBrokerError(
                    "upstream failed",
                    503,
                    {"message": "unavailable", "client_secret": "secret"},
                ),
                502,
            ),
            (ValueError("cancel_reason 不能为空。"), 400),
        )
        for failure, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                with patch.object(order_api, "operate_task", side_effect=failure):
                    status, data = self.request(
                        "POST", "/api/order/tasks/other-task/manual-claim", {}
                    )
                self.assertEqual(status, expected_status)
                self.assertIn("error", data)
                if expected_status == 502:
                    self.assertEqual(data["upstream_status"], 503)
                    self.assertEqual(data["upstream"]["client_secret"], "***")


if __name__ == "__main__":
    unittest.main()
