"""路由共用的依赖：当前用户、Workbench、渲染（含侧栏数据）、提示消息。"""

import json
from datetime import timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from workbench.pipeline import Workbench
from workbench.store.models import Company, CompanyStatus, Draft, DraftStatus, Run, RunStatus
from workbench.store.queries import usage_since


class LoginRequired(Exception):
    pass


def get_wb(request: Request) -> Workbench:
    return request.app.state.wb


def current_user(request: Request) -> str:
    username = request.session.get("user")
    if not username:
        raise LoginRequired
    return username


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def flash(request: Request, text: str, *, error: bool = False) -> None:
    """下一次页面渲染时以提示消息显示（3-5 秒自动消失）。"""
    request.session.setdefault("flash", []).append({"text": text, "error": error})


def toast_header(text: str, *, error: bool = False, link: str = "", link_text: str = "") -> dict[str, str]:
    """htmx 请求的提示消息：通过 HX-Trigger 交给前端显示。"""
    payload = {"text": text, "error": error, "link": link, "linkText": link_text}
    return {"HX-Trigger": json.dumps({"toast": payload})}


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _count(session: Any, *conditions: Any) -> int:
    return session.scalar(select(func.count(Company.id)).where(*conditions)) or 0


def review_remaining(session: Any, wb: Workbench) -> int:
    """今日审核里还剩几张待审：口径与 ReviewQueue.today 一致，但只读不写。"""
    queued = Company.status == CompanyStatus.QUEUED
    assigned = _count(session, queued, Company.queued_on.is_not(None))
    decided_today = _count(session, Company.queued_on == wb.ctx.today(), Company.status != CompanyStatus.QUEUED)
    unassigned = _count(session, queued, Company.queued_on.is_(None))
    return assigned + max(0, min(unassigned, wb.settings.daily_review_limit - assigned - decided_today))


def sidebar(wb: Workbench) -> dict[str, Any]:
    with wb.db.session() as s:
        review = review_remaining(s, wb)
        running = s.scalar(
            select(func.count(Run.id)).where(
                Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]), Run.parent_id.is_(None)
            )
        )
        drafts = s.scalar(select(func.count(Draft.id)).where(Draft.status == DraftStatus.DRAFT)) or 0
        usage = usage_since(s, wb.ctx.clock() - timedelta(days=7))
    return {
        "review": review,
        "running": bool(running),
        "drafts": drafts,
        "usage": usage,
    }


def render(request: Request, template: str, *, nav: str = "", status_code: int = 200, **context: Any) -> HTMLResponse:
    wb = get_wb(request)
    base = {
        "user": request.session.get("user"),
        "nav": nav,
        "flashes": request.session.pop("flash", []),
        "side": sidebar(wb),
        "settings": wb.settings,
        "now": wb.ctx.clock(),
    }
    return request.app.state.templates.TemplateResponse(request, template, {**base, **context}, status_code=status_code)
