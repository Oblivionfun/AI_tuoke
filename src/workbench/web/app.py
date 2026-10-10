"""网页入口：组装中间件、模板和各页面路由。web 进程只排队任务和展示，agent 由 worker 进程执行。"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from workbench.agents.runner import TRACE_URL, AgentRunner, SdkRunner
from workbench.config import WorkbenchSettings, get_settings
from workbench.pipeline import Workbench
from workbench.store.db import Database
from workbench.store.models import CompanyStatus, Direction, DraftStatus, Queue, RunKind, RunStatus, utcnow
from workbench.web import view
from workbench.web.deps import LoginRequired, redirect
from workbench.web.routes import auth, companies, followups, products, runs, today

WEB_DIR = Path(__file__).parent


class SameOriginMiddleware(BaseHTTPMiddleware):
    """表单 POST 只接受同源请求（配合 SameSite=Lax 会话 cookie 防 CSRF）。"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlsplit(origin).netloc != request.headers.get("host", request.url.netloc):
                return PlainTextResponse("跨站请求被拒绝", status_code=403)
        return await call_next(request)


def build_templates(settings: WorkbenchSettings) -> Jinja2Templates:
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    tz = ZoneInfo(settings.timezone)

    def localtime(value: datetime | None, fmt: str = "%m-%d %H:%M") -> str:
        return value.astimezone(tz).strftime(fmt) if value else ""

    templates.env.filters["localtime"] = localtime
    templates.env.filters["short_url"] = view.short_url
    templates.env.globals.update(
        view=view,
        TRACE_URL=TRACE_URL,
        CompanyStatus=CompanyStatus,
        Direction=Direction,
        DraftStatus=DraftStatus,
        Queue=Queue,
        RunKind=RunKind,
        RunStatus=RunStatus,
    )
    return templates


def create_app(
    settings: WorkbenchSettings | None = None,
    db: Database | None = None,
    runner: AgentRunner | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> FastAPI:
    settings = settings or get_settings()
    db = db or Database(settings.database_url)

    app = FastAPI(title="冠维获客工作台", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.wb = Workbench(db, settings, runner or SdkRunner(settings), clock=clock)
    app.state.templates = build_templates(settings)
    app.add_middleware(SameOriginMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="wb_session",
        same_site="lax",
        max_age=14 * 24 * 3600,
        https_only=settings.cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    for module in (auth, today, companies, runs, products, followups):
        app.include_router(module.router)

    @app.exception_handler(LoginRequired)
    async def _login_redirect(request: Request, exc: LoginRequired) -> Response:
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return redirect("/login")

    return app
