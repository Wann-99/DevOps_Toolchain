"""Logged-in upgrade controls; roles do not restrict checking or installing."""

import os
from urllib.parse import urlparse

from ksq import upgrade
from ksq.constants import APP_VERSION
from ksq.dashboard import service as dashboard, settings
from ksq.order import active, config, model, service as orders
from ksq.robot import mapping
from ksq.web import auth
from ksq.web.request_utils import read_json_body


def readiness():
    if upgrade.active_writes():
        return {"ready": False, "reason": "还有写操作正在执行，请完成后再升级。"}
    if mapping._DRIVE_LEASES:
        return {"ready": False, "reason": "请先结束底盘遥控并恢复速度后再升级。"}
    loaded = settings.load_dashboard_settings()
    mode = loaded.get("mode") or "test"
    snapshot = dashboard.get_dashboard_monitor_snapshot(2500, force=True)
    if (snapshot.get("keyboard") or snapshot).get("needs_confirm"):
        return {"ready": False, "reason": "机器人仍在等待人工确认，请完成后再升级。"}
    current = active.get_active_order()
    if current and current.get("queued_orders"):
        return {"ready": False, "reason": "还有排队订单，请完成后再升级。"}
    if orders._is_broker_configured(mode):
        configured = config.load_order_config(orders.config_file_for_mode(mode))
        tasks = orders._fetch_all_tasks(configured, mode, str(configured.get("store_id") or ""), "desc", "", "Asia/Shanghai")
        if any(not isinstance(task, dict) or task.get("status") not in model._BROKER_ORDER_TERMINAL for task in tasks):
            return {"ready": False, "reason": "门店还有未结束的订单，请完成后再升级。"}
    elif current or snapshot.get("service_running"):
        return {"ready": False, "reason": "门店接口未配置，无法确认任务状态，请配置后再升级。"}
    return {"ready": True}


def handle_get(handler, session):
    path = urlparse(handler.path).path
    if path == "/api/upgrade/status":
        if not os.environ.get("KSQ_UPGRADE_DIRECTORY"):
            result = {"ok": True, "installed": False, "current_version": APP_VERSION,
                      "repository": "https://github.com/" + upgrade.REPOSITORY,
                      "message": "尚未安装在线升级服务，请使用完整部署包执行 bash start.sh upgrade install。"}
        else:
            try:
                result = upgrade.host_request("status")
            except ValueError as error:
                result = {"ok": True, "installed": False, "current_version": APP_VERSION, "message": str(error)}
        handler._send_json(200, result)
        return True
    if path == "/api/upgrade/readiness":
        try:
            result = readiness()
        except Exception:
            result = {"ready": False, "reason": "无法确认门店或机器人状态，原版本未修改，请恢复连接后再升级。"}
        handler._send_json(200, result)
        return True
    return False


def handle_post(handler, session):
    path = urlparse(handler.path).path
    if path not in {"/api/upgrade/check", "/api/upgrade/start"}:
        return False
    payload = read_json_body(handler)
    if set(payload) - ({"version"} if path.endswith("start") else set()):
        raise ValueError("升级仅接受已发布的版本，不接受下载地址、脚本或其他参数。")
    cookie = auth.SESSION_COOKIE + "=" + auth.token_from_cookie(handler.headers.get("Cookie", ""))
    handler._send_json(200, upgrade.host_request(path.rsplit("/", 1)[1], payload, cookie))
    return True


ROUTES = {"GET": ("/api/upgrade/status", "/api/upgrade/readiness"),
          "POST": ("/api/upgrade/check", "/api/upgrade/start")}
