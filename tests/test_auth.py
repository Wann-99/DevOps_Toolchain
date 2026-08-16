"""登录认证与角色权限（ksq.web.auth）的单元测试。"""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ksq.web import auth, dashboard_api

try:
    from ksq.web.handlers import QueryHandler
except ModuleNotFoundError as error:
    if error.name != "cgi":
        raise
    QueryHandler = None


class AuthTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._original_users_file = auth.USERS_FILE
        auth.USERS_FILE = Path(self._tmp.name) / "users.json"
        self.addCleanup(self._restore_users_file)
        auth._SESSIONS.clear()
        self.addCleanup(auth._SESSIONS.clear)

    def _restore_users_file(self) -> None:
        auth.USERS_FILE = self._original_users_file

    def _write_users(self, payload: dict) -> None:
        auth.USERS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_missing_file_seeds_default_accounts(self) -> None:
        users = auth.load_users()
        usernames = {entry["username"] for entry in users}
        self.assertEqual(usernames, {"admin", "nvidia"})
        roles = {entry["username"]: entry["role"] for entry in users}
        self.assertEqual(roles["admin"], auth.ROLE_ADMIN)
        self.assertEqual(roles["nvidia"], auth.ROLE_VIEWER)

    def test_plaintext_password_is_migrated_to_hash(self) -> None:
        self._write_users(
            {
                "users": [
                    {
                        "username": "admin",
                        "role": "admin",
                        "password": "secret-pw",
                    }
                ]
            }
        )
        entry = auth.verify_credentials("admin", "secret-pw")
        self.assertIsNotNone(entry)
        persisted = json.loads(auth.USERS_FILE.read_text(encoding="utf-8"))
        stored = persisted["users"][0]
        self.assertNotIn("password", stored)
        self.assertTrue(stored["salt"])
        self.assertTrue(stored["password_hash"])
        self.assertNotEqual(stored["password_hash"], "secret-pw")
        # 迁移后再次验证仍然通过
        self.assertIsNotNone(auth.verify_credentials("admin", "secret-pw"))

    def test_wrong_password_and_unknown_user_rejected(self) -> None:
        self._write_users(
            {"users": [{"username": "admin", "role": "admin", "password": "pw"}]}
        )
        self.assertIsNone(auth.verify_credentials("admin", "bad"))
        self.assertIsNone(auth.verify_credentials("nobody", "pw"))

    def test_broken_file_is_backed_up_and_reseeded(self) -> None:
        auth.USERS_FILE.write_text("{ not json", encoding="utf-8")
        users = auth.load_users()
        self.assertEqual({entry["username"] for entry in users}, {"admin", "nvidia"})
        backups = list(Path(self._tmp.name).glob("users.json.broken-*"))
        self.assertEqual(len(backups), 1)

    def test_session_lifecycle(self) -> None:
        entry = auth.verify_credentials("admin", "noematrix")
        self.assertIsNotNone(entry)
        token = auth.create_session(entry)
        session = auth.get_session(token)
        self.assertIsNotNone(session)
        self.assertEqual(session["role"], auth.ROLE_ADMIN)
        auth.destroy_session(token)
        self.assertIsNone(auth.get_session(token))

    def test_expired_session_is_dropped(self) -> None:
        entry = auth.verify_credentials("nvidia", "nvidia")
        token = auth.create_session(entry)
        auth._SESSIONS[token]["expires"] = time.time() - 1
        self.assertIsNone(auth.get_session(token))
        self.assertNotIn(token, auth._SESSIONS)

    def test_cookie_parsing_and_headers(self) -> None:
        token = auth.token_from_cookie("a=1; ksq_session=abc123; b=2")
        self.assertEqual(token, "abc123")
        self.assertEqual(auth.token_from_cookie(""), "")
        self.assertIn("ksq_session=tok", auth.session_cookie_header("tok"))
        self.assertIn("HttpOnly", auth.session_cookie_header("tok"))
        self.assertIn("Max-Age=0", auth.clear_cookie_header())

    def test_viewer_post_forbidlist(self) -> None:
        # 仅两类编辑操作被禁：库位编辑保存、导入
        # （设置配置保存走 PUT 拦截；获取 Token 属下单凭据刷新，放行）
        self.assertIn("/api/edit/save", auth.VIEWER_FORBIDDEN_POST_PATHS)
        self.assertIn("/api/edit/persist", auth.VIEWER_FORBIDDEN_POST_PATHS)
        self.assertIn("/api/import", auth.VIEWER_FORBIDDEN_POST_PATHS)
        # 其余一律放行（含加载、下单、Token 刷新、模式切换）
        self.assertNotIn("/load-auto", auth.VIEWER_FORBIDDEN_POST_PATHS)
        self.assertNotIn("/api/order/create", auth.VIEWER_FORBIDDEN_POST_PATHS)
        self.assertNotIn("/api/order/token", auth.VIEWER_FORBIDDEN_POST_PATHS)
        self.assertNotIn(
            "/api/dashboard/keyboard", auth.VIEWER_FORBIDDEN_POST_PATHS
        )

    def test_public_user_shape(self) -> None:
        user = auth.public_user(
            {"username": "user", "display_name": "普通用户", "role": "viewer"}
        )
        self.assertEqual(user["role_label"], "普通用户")
        self.assertNotIn("password_hash", user)


@unittest.skipIf(QueryHandler is None, "HTTP handlers require Python 3.12 or older")
class AuthRouteTests(unittest.TestCase):
    """handlers 层的会话校验与角色拦截。"""

    def _request(
        self,
        method: str,
        path: str,
        role: str | None,
        payload: object = None,
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
        handler._send_json = lambda status, data: response.update(
            status=int(status), data=data
        )
        handler.send_error = lambda status, message="": response.update(
            status=int(status), data={"error": message}
        )
        handler._send_redirect = lambda location: response.update(
            status=302, data={"location": location}
        )
        session = (
            None
            if role is None
            else {"username": "u", "display_name": "u", "role": role}
        )
        with patch.object(auth, "session_from_cookie", return_value=session):
            getattr(handler, f"do_{method}")()
        return int(response["status"]), response["data"]

    def test_anonymous_page_request_redirects_to_login(self) -> None:
        status, data = self._request("GET", "/", role=None)
        self.assertEqual(status, 302)
        self.assertEqual(data["location"], "/login")

    def test_anonymous_api_request_is_unauthorized(self) -> None:
        status, data = self._request("GET", "/api/status", role=None)
        self.assertEqual(status, 401)
        self.assertIn("error", data)

    def test_anonymous_health_check_is_public(self) -> None:
        status, data = self._request("GET", "/api/health", role=None)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_viewer_cannot_import(self) -> None:
        status, data = self._request("POST", "/api/import", role=auth.ROLE_VIEWER)
        self.assertEqual(status, 403)
        self.assertIn("管理员", data["error"])

    def test_viewer_cannot_save_edit(self) -> None:
        status, data = self._request(
            "POST", "/api/edit/save", role=auth.ROLE_VIEWER, payload={}
        )
        self.assertEqual(status, 403)
        self.assertIn("管理员", data["error"])

    def test_viewer_cannot_update_order_config(self) -> None:
        status, data = self._request(
            "PUT", "/api/order/config", role=auth.ROLE_VIEWER, payload={}
        )
        self.assertEqual(status, 403)

    def test_viewer_allowed_readonly_post(self) -> None:
        with patch.object(
            dashboard_api, "preview_feishu_submission", return_value={"ok": True}
        ):
            status, data = self._request(
                "POST", "/api/dashboard/feishu/preview", role=auth.ROLE_VIEWER
            )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_admin_passes_role_gate(self) -> None:
        with patch.object(
            dashboard_api, "set_active_order", return_value={"task_id": "t"}
        ):
            status, data = self._request(
                "POST", "/api/dashboard/order", role=auth.ROLE_ADMIN, payload={}
            )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_viewer_mode_switch_filters_settings_payload(self) -> None:
        """普通用户仅可切换工作模式，其余设置字段被后端过滤。"""
        captured: dict[str, object] = {}

        def fake_save(payload, restart_robot):
            captured["payload"] = payload
            captured["restart_robot"] = restart_robot
            return {"ok": True}

        with patch.object(
            dashboard_api, "save_dashboard_settings", side_effect=fake_save
        ):
            status, _ = self._request(
                "POST",
                "/api/dashboard/keyboard",
                role=auth.ROLE_VIEWER,
                payload={
                    "keyboard_device": "/dev/input/event9",
                    "mode": "prod",
                    "etm_base_url": "http://evil",
                    "auto_confirm": True,
                    "restart_robot": True,
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(captured["payload"], {"mode": "prod"})
        self.assertFalse(captured["restart_robot"])

    def test_admin_keyboard_payload_untouched(self) -> None:
        captured: dict[str, object] = {}

        def fake_save(payload, restart_robot):
            captured["payload"] = payload
            captured["restart_robot"] = restart_robot
            return {"ok": True}

        with patch.object(
            dashboard_api, "save_dashboard_settings", side_effect=fake_save
        ):
            status, _ = self._request(
                "POST",
                "/api/dashboard/keyboard",
                role=auth.ROLE_ADMIN,
                payload={
                    "keyboard_device": "/dev/input/event9",
                    "mode": "prod",
                    "etm_base_url": "http://example",
                    "restart_robot": False,
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            captured["payload"],
            {
                "keyboard_device": "/dev/input/event9",
                "mode": "prod",
                "etm_base_url": "http://example",
                "restart_robot": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
