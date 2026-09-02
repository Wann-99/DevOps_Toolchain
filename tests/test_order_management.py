from __future__ import annotations

import base64
import inspect
import io
import json
import tempfile
import threading
import time
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

    def test_public_config_hides_secret_unless_admin(self) -> None:
        """GET /api/order/config 不校验角色，密钥只能对管理员下发。"""
        config = dict(VALID_CONFIG, client_secret="SUPER-SECRET")

        viewer = order_config.public_order_config(config)
        admin = order_config.public_order_config(config, include_secret=True)

        self.assertEqual(viewer["client_secret"], "")
        self.assertTrue(viewer["has_client_secret"])
        self.assertEqual(admin["client_secret"], "SUPER-SECRET")
        self.assertTrue(admin["has_client_secret"])

    def test_empty_secret_never_wipes_saved_secret(self) -> None:
        """密钥现在预填到输入框；普通用户拿到空串，保存时不得抹掉已存值。"""
        config = dict(VALID_CONFIG, client_secret="SUPER-SECRET")

        merged = order_config.merge_config_update(
            config, {"client_secret": "", "client_id": "new-cid"}
        )

        self.assertEqual(merged["client_secret"], "SUPER-SECRET")
        self.assertEqual(merged["client_id"], "new-cid")

    def test_customer_options_include_none_and_shuyu(self) -> None:
        public = order_config.public_order_config(order_config.default_order_config())
        self.assertEqual(
            public["customers"],
            [
                {"value": "", "cn": "—"},
                {"value": "dashenlin", "cn": "大参林"},
                {"value": "yaoshibang", "cn": "药师帮"},
                {"value": "shuyu", "cn": "漱玉"},
                {"value": "noematrix", "cn": "穹彻智能"},
            ],
        )
        shuyu = order_config.apply_customer_defaults(
            {"customer": "shuyu", "order_source": "legacy-source"}
        )
        self.assertEqual(shuyu["order_source"], "legacy-source")
        self.assertEqual(shuyu["business_mode_code"], "MODE_PICK_WAIT_PACK")

        noematrix = order_config.apply_customer_defaults(
            {"customer": "noematrix", "order_source": "legacy-source"}
        )
        self.assertEqual(noematrix["order_source"], "legacy-source")
        self.assertTrue(noematrix["need_image_upload"])
        self.assertEqual(noematrix["business_mode_code"], "MODE_PICK")

        custom = order_config.apply_customer_defaults(
            {"customer": "custom_customer", "order_source": "legacy-source"}
        )
        self.assertEqual(custom["order_source"], "legacy-source")

    def test_order_source_is_generated_independently_from_customer(self) -> None:
        def choose_value(values: object) -> str:
            if isinstance(values, list):
                return "jd"
            return str(values)[0]

        with patch(
            "ksq.order.payload.secrets.choice", side_effect=choose_value
        ) as choose:
            body = build_create_task_body(
                {
                    "store_id": "store-1",
                    "customer": "桂中大药房",
                    "order_source": "桂中大药房",
                },
                [{"item_id": "item-1", "location_code": "A-01"}],
            )

        self.assertEqual(
            choose.call_args_list[0].args[0], ["meituan", "eleme", "jd", "dy"]
        )
        self.assertEqual(body["order_source"], "jd")
        self.assertTrue(str(body["platform_order_no"]).startswith("JD"))

    def test_feishu_default_is_disabled(self) -> None:
        self.assertFalse(dashboard_api._default_feishu_settings()["enabled"])

    def test_store_id_is_only_required_for_order_validation(self) -> None:
        config = dict(VALID_CONFIG)
        config["store_id"] = ""

        order_config.validate_order_config(config, require_store=False)
        with self.assertRaisesRegex(ValueError, "store_id"):
            order_config.validate_order_config(config)


class StoreListingTests(unittest.TestCase):
    def test_list_stores_can_fetch_before_store_is_selected(self) -> None:
        config = dict(VALID_CONFIG)
        config["store_id"] = ""
        with (
            patch.object(order_api, "load_order_config", return_value=config),
            patch.object(order_api, "_ensure_token", return_value="token") as ensure,
            patch.object(
                order_api.broker,
                "list_my_stores",
                return_value=(200, {"data": [{"store_id": "store-1"}]}),
            ),
        ):
            result = order_api.list_stores("test")

        self.assertEqual(result["stores"], [{"store_id": "store-1"}])
        ensure.assert_called_once_with(config, "test", require_store=False)

    def test_public_config_reports_cached_token_until_jwt_expiry(self) -> None:
        config = dict(VALID_CONFIG)
        payload = base64.urlsafe_b64encode(b'{"exp":200}').decode().rstrip("=")
        token = "header." + payload + ".signature"
        key = order_api._token_cache_key(config)
        with (
            patch.object(order_api, "load_order_config", return_value=config),
            patch.object(order_api.state, "order_access_tokens", {key: token}),
            patch.object(order_api.state, "order_access_token", None),
            patch.object(order_api.time, "time", return_value=100),
        ):
            self.assertTrue(order_api.get_public_config("test")["token_ready"])

        with (
            patch.object(order_api, "load_order_config", return_value=config),
            patch.object(order_api.state, "order_access_tokens", {key: token}),
            patch.object(order_api.state, "order_access_token", None),
            patch.object(order_api.time, "time", return_value=201),
        ):
            self.assertFalse(order_api.get_public_config("test")["token_ready"])


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
        # 每一批都要发同一个过滤值：曾因 status 被 HTTP 状态码覆盖，第二批发出 200，
        # 被 Broker 报 status must be one of: [...]
        self.assertEqual(list_tasks.call_args_list[0].args[5], "running")
        self.assertEqual(list_tasks.call_args_list[1].args[5], "running")
        self.assertNotIn("store_id", inspect.signature(order_api.list_tasks).parameters)

    def test_manual_status_is_filtered_locally_not_sent_upstream(self) -> None:
        upstream = {
            "data": {
                "tasks": [
                    {"task_id": "task-1", "status": "manual_cancel"},
                    {"task_id": "task-2", "status": "running"},
                ],
                "has_more": False,
            }
        }
        with (
            patch.object(order_api, "load_order_config", return_value=VALID_CONFIG),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(
                order_api.broker, "list_robot_tasks", return_value=(200, upstream)
            ) as list_tasks,
        ):
            result = order_api.list_tasks("test", status="manual_cancel")

        # Broker 不接受 manual_* 作为过滤值，不能往上发
        self.assertEqual(list_tasks.call_args.args[5], "")
        # 但筛选结果必须生效（本地过滤）
        self.assertEqual(
            result["tasks"], [{"task_id": "task-1", "status": "manual_cancel"}]
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["status"], "manual_cancel")

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

    def test_stale_cache_serves_old_data_and_defers_refresh(self) -> None:
        """缓存过期不能让前端等拉全量（4~7s），否则分页/刷新按钮被卡死。"""
        upstream = {
            "data": {"tasks": [{"task_id": "task-1"}], "has_more": False}
        }
        with (
            patch.object(order_api, "load_order_config", return_value=VALID_CONFIG),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(
                order_api.broker, "list_robot_tasks", return_value=(200, upstream)
            ) as upstream_call,
        ):
            primed = order_api.list_tasks("test")
            self.assertFalse(primed["stale"])
            self.assertEqual(upstream_call.call_count, 1)

            # TTL 置 0 令缓存恒为过期
            with (
                patch.object(order_api, "_TASK_LIST_CACHE_SECONDS", 0.0),
                patch.object(
                    order_api, "_start_background_task_list_refresh"
                ) as background,
            ):
                served = order_api.list_tasks("test")

        # 返回旧值，且前端未再次请求上游
        self.assertTrue(served["stale"])
        self.assertTrue(served["cached"])
        self.assertEqual(served["total"], 1)
        self.assertEqual(upstream_call.call_count, 1)
        self.assertEqual(background.call_count, 1)

    def test_background_refresh_updates_cache_and_is_single_flight(self) -> None:
        released = threading.Event()
        entered = threading.Event()

        def slow_fetch(*_args: object, **_kwargs: object) -> list:
            entered.set()
            released.wait(5)
            return [{"task_id": "fresh"}]

        cache_key = ("test", "srv", "cid", "store", "desc", "", "Asia/Shanghai")
        args = (VALID_CONFIG, "test", "store", "desc", "", "Asia/Shanghai")
        with patch.object(order_api, "_fetch_all_tasks", side_effect=slow_fetch):
            self.assertTrue(
                order_api._start_background_task_list_refresh(cache_key, *args)
            )
            self.assertTrue(entered.wait(5))
            # 同一 key 正在刷新时不重复开线程
            self.assertFalse(
                order_api._start_background_task_list_refresh(cache_key, *args)
            )
            released.set()
            for _ in range(100):
                with order_api._TASK_LIST_CACHE_LOCK:
                    done = cache_key not in order_api._TASK_LIST_REFRESHING
                if done:
                    break
                time.sleep(0.05)

        with order_api._TASK_LIST_CACHE_LOCK:
            self.assertNotIn(cache_key, order_api._TASK_LIST_REFRESHING)
            self.assertEqual(
                order_api._TASK_LIST_CACHE[cache_key][1], [{"task_id": "fresh"}]
            )

    def test_background_refresh_failure_keeps_old_cache(self) -> None:
        cache_key = ("test", "srv", "cid", "store", "desc", "", "Asia/Shanghai")
        old = [{"task_id": "old"}]
        with order_api._TASK_LIST_CACHE_LOCK:
            order_api._TASK_LIST_CACHE[cache_key] = (time.monotonic(), old)
        args = (VALID_CONFIG, "test", "store", "desc", "", "Asia/Shanghai")

        with patch.object(
            order_api, "_fetch_all_tasks", side_effect=RuntimeError("broker down")
        ):
            order_api._start_background_task_list_refresh(cache_key, *args)
            for _ in range(100):
                with order_api._TASK_LIST_CACHE_LOCK:
                    done = cache_key not in order_api._TASK_LIST_REFRESHING
                if done:
                    break
                time.sleep(0.05)

        with order_api._TASK_LIST_CACHE_LOCK:
            self.assertEqual(order_api._TASK_LIST_CACHE[cache_key][1], old)

    def test_broker_status_is_returned_verbatim(self) -> None:
        """订单状态直出 Broker 原值，不做本地覆盖。"""
        upstream = {
            "data": {
                "tasks": [{"task_id": "task-1", "status": "running"}],
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
            result = order_api.list_tasks("test")

        self.assertEqual(result["tasks"][0]["status"], "running")

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

    def test_retries_once_after_http_200_token_error_code(self) -> None:
        request = Mock(
            side_effect=[
                (200, {"code": 4014, "msg": "expired"}),
                (200, {"code": 0, "data": {}}),
            ]
        )
        with (
            patch.object(order_api, "_ensure_token", side_effect=["old", "new"]) as token,
            patch.object(order_api, "_clear_token") as clear,
        ):
            result = order_api._request_with_token_retry(
                VALID_CONFIG, "test", request
            )

        self.assertEqual(result, (200, {"code": 0, "data": {}}))
        self.assertEqual(request.call_args_list[1].args, ("new",))
        self.assertEqual(token.call_count, 2)
        clear.assert_called_once_with(VALID_CONFIG)

    def test_non_numeric_business_code_is_not_treated_as_success(self) -> None:
        with self.assertRaises(broker.OrderBrokerError):
            order_api._ensure_broker_response_succeeded(
                "查询任务", 200, {"code": "unexpected", "data": {}}
            )

    def test_fractional_business_code_is_not_treated_as_success(self) -> None:
        with self.assertRaises(broker.OrderBrokerError):
            order_api._ensure_broker_response_succeeded(
                "查询任务", 200, {"code": 0.1, "data": {}}
            )


class CurrentOrderActionTests(unittest.TestCase):
    # 状态门槛已对齐 devtools（直发，由 Broker 裁决），矩阵仅用于覆盖各状态。
    ALL_STATUSES = (
        "pending", "dispatched", "running", "awaiting_pack", "manual_transferred",
        "manual_claimed_in_progress", "success", "error", "cancel",
        "manual_transferred_completed",
    )

    def test_all_statuses_send_request_directly(self) -> None:
        for status in self.ALL_STATUSES:
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
                        kwargs = (
                            {"cancel_reason": "operator request"}
                            if action == "cancel"
                            else {}
                        )
                        result = order_api.operate_current_order(action, **kwargs)
                        self.assertTrue(result["ok"])
                        request.assert_called_once()

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
    # 状态门槛已对齐 devtools（直发，由 Broker 裁决），矩阵仅用于覆盖各状态。
    ALL_STATUSES = (
        "pending", "dispatched", "running", "awaiting_pack", "manual_transferred",
        "manual_claimed_in_progress", "success", "error", "cancel",
        "manual_transferred_completed",
    )

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

    def test_all_statuses_send_request_directly(self) -> None:
        for status in self.ALL_STATUSES:
            for action in ("cancel", "manual_claim", "manual_complete"):
                with self.subTest(status=status, action=action):
                    result, *_ = self._operate(
                        action, status, cancel_reason="operator request"
                    )
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["action"], action)
                    self.assertEqual(result["task_id"], "task-9")
                    self.assertEqual(result["order_no"], "ORDER-9")

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

    def test_http_200_business_error_is_not_reported_as_success(self) -> None:
        with self.assertRaisesRegex(broker.OrderBrokerError, "Broker 拒绝操作"):
            order_api._ensure_task_action_succeeded(
                "manual_complete",
                200,
                {"code": 422, "msg": "Broker 拒绝操作"},
            )

    def test_manual_complete_sends_request_regardless_of_stale_status(self) -> None:
        """对齐 devtools：无本地状态门槛，stale running 详情也直发完成请求。"""
        result, _cancel, _claim, complete, _clear = self._operate(
            "manual_complete",
            "running",
        )
        self.assertTrue(result["ok"])
        complete.assert_called_once()

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


class CreateOrderBusinessCodeTests(unittest.TestCase):
    """Broker 业务错误码（HTTP 200 但 code 非 0）的自动重试与指引。"""

    CONFIG = dict(VALID_CONFIG, order_source="meituan")
    ITEMS = [{"item_id": "item-1", "location_code": "A01", "barcode": "690001"}]

    def _create(self, create_side_effect, items=None):
        with (
            patch.object(
                dashboard_api, "ensure_order_queue_capacity", return_value=None
            ),
            patch.object(
                dashboard_api, "active_order_blocking_keys", return_value=[]
            ),
            patch.object(
                order_api, "load_order_config", return_value=dict(self.CONFIG)
            ),
            patch.object(order_api, "_ensure_token", return_value="token"),
            patch.object(order_api, "_clear_token") as clear,
            patch.object(
                order_api.broker, "create_robot_task", side_effect=create_side_effect
            ) as create,
        ):
            result = order_api.create_order(
                {"mode": "test", "items": items or self.ITEMS}
            )
        return result, create, clear

    def test_group_metadata_is_local_only(self) -> None:
        item = dict(self.ITEMS[0], group_id="A01", group_field="批次")
        result, create, _clear = self._create(
            [(200, {"code": 0, "data": {"task_id": "t-group"}})],
            [item],
        )

        broker_item = create.call_args.args[2]["items"][0]
        self.assertNotIn("group_id", broker_item)
        self.assertNotIn("group_field", broker_item)
        self.assertEqual(result[2]["items"][0]["group_id"], "A01")
        self.assertEqual(result[2]["items"][0]["group_field"], "批次")

    def test_4014_refreshes_token_and_retries(self) -> None:
        result, create, clear = self._create(
            [
                (200, {"code": 4014, "msg": "client token is invalid", "data": None}),
                (200, {"code": 0, "data": {"task_id": "t-1"}}),
            ]
        )
        self.assertEqual(create.call_count, 2)
        clear.assert_called_once()
        self.assertEqual(result[1]["data"]["task_id"], "t-1")

    def test_4601_regenerates_order_no_and_retries(self) -> None:
        result, create, _clear = self._create(
            [
                (200, {"code": 4601, "msg": "order is duplicated", "data": None}),
                (200, {"code": 0, "data": {"task_id": "t-2"}}),
            ]
        )
        self.assertEqual(create.call_count, 2)
        first_body = create.call_args_list[0].args[2]
        second_body = create.call_args_list[1].args[2]
        self.assertNotEqual(first_body["order_no"], second_body["order_no"])
        self.assertEqual(result[1]["data"]["task_id"], "t-2")

    def test_non_retryable_code_raises_with_message(self) -> None:
        with self.assertRaises(broker.OrderBrokerError) as ctx:
            self._create(
                [(200, {"code": 4511, "msg": "robot manager device is empty"})]
            )
        self.assertIn("robot manager device is empty", str(ctx.exception))
        payload = order_api.broker_error_payload(ctx.exception)
        self.assertEqual(payload["upstream_code"], 4511)
        self.assertIn("注册机器人", payload["hint"])

    def test_known_codes_carry_hints(self) -> None:
        for code, keyword in (
            (4014, "Token"),
            (4524, "接单"),
            (4552, "库位管理工具"),
            (4601, "单号"),
            (4800, "并发"),
        ):
            error = broker.OrderBrokerError("失败", 200, {"code": code, "msg": "x"})
            hint = order_api.broker_error_payload(error)["hint"]
            self.assertIn(keyword, hint)

    def test_missing_task_id_is_rejected_before_local_registration(self) -> None:
        with (
            patch.object(
                order_api,
                "create_order",
                return_value=(200, {"code": 0, "data": {}}, {"items": []}),
            ),
            patch.object(dashboard_api, "register_created_order") as register,
            self.assertRaisesRegex(broker.OrderBrokerError, "缺少 task_id"),
        ):
            order_api.create_registered_order({"items": []})

        register.assert_not_called()

    def test_unresolved_previous_prompt_is_rejected_before_broker_request(self) -> None:
        with (
            patch.object(
                dashboard_api,
                "active_order_requires_manual_completion",
                return_value=True,
            ),
            patch.object(order_api, "load_order_config") as load_config,
            self.assertRaises(order_api.OrderQueueConflict) as raised,
        ):
            order_api.create_order({"mode": "test", "items": self.ITEMS})

        self.assertEqual(
            raised.exception.code, "PREVIOUS_ORDER_REQUIRES_COMPLETION"
        )
        self.assertIn("完成上一单", raised.exception.hint)
        load_config.assert_not_called()


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

    def test_order_preflight_returns_structured_previous_order_conflict(self) -> None:
        conflict = order_api.OrderQueueConflict(
            "上一单仍在等待人工确认。",
            "PREVIOUS_ORDER_REQUIRES_COMPLETION",
            "请先完成上一单。",
        )
        with patch.object(
            order_api, "ensure_order_creation_allowed", side_effect=conflict
        ):
            status, data = self.request("POST", "/api/order/preflight")

        self.assertEqual(status, 409)
        self.assertEqual(data["error_code"], conflict.code)
        self.assertEqual(data["hint"], conflict.hint)

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
