"""FastAPI composition root; one process owns sessions, state and listeners."""

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from ksq import upgrade
from ksq.dashboard import service as dashboard
from ksq.data import storage
from ksq.robot import logs as robot_logs
from ksq.web import files_api
from ksq.web.asgi import dispatch
from ksq.web.routes import data, dashboard as dashboard_routes, logs, map as map_routes, mapping, orders, test_orders, upgrade as upgrade_routes


class UpgradeGuard:
    """Keep streaming writes counted until their entire ASGI response completes."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] == "http" and scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
                and not scope["path"].startswith(("/api/auth/", "/api/upgrade/"))):
            try:
                with upgrade.writing():
                    await self.app(scope, receive, send)
            except upgrade.UpgradeBusy as error:
                await JSONResponse({"error": str(error)}, status_code=503)(scope, receive, send)
        else:
            await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await run_in_threadpool(storage.start_data_cleanup)
        await run_in_threadpool(dashboard.start_dashboard_monitor)
        yield
    finally:
        # Nested finally blocks guarantee cleanup even if one owner fails.
        try:
            await run_in_threadpool(files_api.close_terminals)
        finally:
            try:
                await run_in_threadpool(dashboard.stop_dashboard_monitor)
            finally:
                try:
                    await run_in_threadpool(robot_logs._stop_log_followers)
                finally:
                    await run_in_threadpool(storage.stop_data_cleanup)


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.add_middleware(UpgradeGuard)
    from ksq.web.routes.arm import router as arm_router
    app.include_router(arm_router)
    for name, module in (
        ("orders", orders), ("dashboard", dashboard_routes), ("data", data),
        ("logs", logs), ("test-orders", test_orders), ("mapping", mapping), ("map", map_routes),
        ("upgrade", upgrade_routes),
    ):
        router = APIRouter(tags=[name])
        for method, paths in module.ROUTES.items():
            for path in paths:
                router.add_api_route(path, dispatch, methods=[method], response_model=None)
        app.include_router(router)
    # Authentication/static/file endpoints and the existing JSON 404 behavior.
    router = APIRouter(tags=["system"])
    router.add_api_route("/{path:path}", dispatch, methods=["GET", "POST", "PUT"], response_model=None)
    app.include_router(router)
    from ksq.web.routes.desktop import router as desktop_router
    app.include_router(desktop_router)
    return app
