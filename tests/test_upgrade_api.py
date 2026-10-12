"""Upgrade access and idle checks without a device or release server."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ksq import upgrade
from ksq.web import auth
from ksq.web.app import UpgradeGuard, create_app
from ksq.web.routes import upgrade as routes


class UpgradeAccess(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())
        self.addCleanup(self.client.close)
        self.tokens = [auth.create_session({"username": "upgrade-fixture-" + role, "role": role})
                       for role in (auth.ROLE_ADMIN, auth.ROLE_VIEWER)]
        for token in self.tokens:
            self.addCleanup(auth.destroy_session, token)

    def headers(self, token):
        return {"Cookie": auth.SESSION_COOKIE + "=" + token, "X-KSQ-Request": "1", "Origin": "http://testserver"}

    def test_both_roles_can_check_and_start_but_login_and_same_origin_are_required(self):
        with patch.object(upgrade, "host_request", return_value={"ok": True}) as host:
            for token in self.tokens:
                for action, body in (("check", {}), ("start", {"version": "v2.0.0"})):
                    response = self.client.post("/api/upgrade/" + action, json=body, headers=self.headers(token))
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(host.call_args.args, (action, body, auth.SESSION_COOKIE + "=" + token))
            host.reset_mock()
            self.assertEqual(self.client.post("/api/upgrade/start", json={"version": "v2.0.0"}).status_code, 401)
            for change in ({"Origin": "https://other.invalid"}, {"X-KSQ-Request": ""}, {"Sec-Fetch-Site": "cross-site"}):
                response = self.client.post("/api/upgrade/start", json={"version": "v2.0.0"},
                                            headers={**self.headers(self.tokens[1]), **change})
                self.assertEqual(response.status_code, 403)
            self.assertEqual(self.client.post("/api/upgrade/start", json={"url": "https://other.invalid"},
                                            headers=self.headers(self.tokens[0])).status_code, 400)
            host.assert_not_called()
        self.assertEqual(self.client.post("/api/upgrade/rollback", json={}, headers=self.headers(self.tokens[0])).status_code, 404)

    def test_maintenance_blocks_writes_while_health_status_and_login_remain_accessible(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, KSQ_UPGRADE_DIRECTORY=temporary):
            Path(temporary, "state.json").write_text(json.dumps({"maintenance": True}))
            for path in ("/api/order/create", "/api/map/mapping", "/api/files/upload", "/api/dashboard/keyboard"):
                self.assertEqual(self.client.post(path, json={}, headers=self.headers(self.tokens[0])).status_code, 503)
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            self.assertEqual(self.client.get("/api/auth/me", headers=self.headers(self.tokens[0])).status_code, 200)
            with patch.object(upgrade, "host_request", return_value={"ok": True, "maintenance": True}):
                self.assertTrue(self.client.get("/api/upgrade/status", headers=self.headers(self.tokens[1])).json()["maintenance"])

    def test_readiness_fails_closed_on_connection_error(self):
        with patch.object(routes, "readiness", side_effect=OSError("fixture inaccessible")):
            response = self.client.get("/api/upgrade/readiness", headers=self.headers(self.tokens[1]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ready"])
        self.assertNotIn("fixture inaccessible", response.text)


class UpgradeIdle(unittest.TestCase):
    def test_old_unfinished_tasks_keyboard_wait_and_drive_lease_prevent_upgrade(self):
        with patch.object(routes.settings, "load_dashboard_settings", return_value={"mode": "prod"}), \
                patch.object(routes.dashboard, "get_dashboard_monitor_snapshot", return_value={}) as snapshot, \
                patch.object(routes.active, "get_active_order", return_value=None) as current, \
                patch.object(routes.orders, "_is_broker_configured", return_value=True), \
                patch.object(routes.config, "load_order_config", return_value={"store_id": "fixture"}), \
                patch.object(routes.orders, "_fetch_all_tasks", return_value=[{"status": "success"}] * 50 + [{"status": "running"}]) as tasks, \
                patch.dict(routes.mapping._DRIVE_LEASES, {}, clear=True):
            self.assertFalse(routes.readiness()["ready"])
            tasks.return_value = [{"status": next(iter(routes.model._BROKER_ORDER_TERMINAL))}]
            self.assertTrue(routes.readiness()["ready"])
            snapshot.return_value = {"keyboard": {"needs_confirm": True}}
            self.assertFalse(routes.readiness()["ready"])
            snapshot.return_value = {}
            current.return_value = {"queued_orders": [{"task_id": "fixture"}]}
            self.assertFalse(routes.readiness()["ready"])
            current.return_value = None
            with patch.dict(routes.mapping._DRIVE_LEASES, {"fixture": object()}):
                self.assertFalse(routes.readiness()["ready"])
            with upgrade.writing():
                self.assertFalse(routes.readiness()["ready"])

    def test_streaming_write_remains_counted_until_final_response_and_error_releases_it(self):
        async def exercise():
            messages = []
            async def downstream(scope, receive, send):
                self.assertEqual(upgrade.active_writes(), 1)
                await send({"type": "http.response.start", "status": 200})
                await send({"type": "http.response.body", "body": b"first", "more_body": True})
                self.assertEqual(upgrade.active_writes(), 1)
                raise RuntimeError("fixture stream interrupted")
            async def send(message):
                messages.append(message)
            with self.assertRaises(RuntimeError):
                await UpgradeGuard(downstream)({"type": "http", "method": "POST", "path": "/api/edit/save"}, None, send)
            self.assertEqual(upgrade.active_writes(), 0)
            self.assertEqual(len(messages), 2)
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
