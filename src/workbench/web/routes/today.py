"""今日审核：左侧最多 10 张卡的队列，右侧当前卡的决策面板。决定后自动切到下一张待审核的卡。"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from workbench.pipeline import Decision
from workbench.store.models import CompanyStatus
from workbench.web.deps import current_user, flash, get_wb, is_htmx, redirect, render, toast_header

router = APIRouter()

DECISION_DONE = {
    Decision.ACCEPT: "已接受，文案 agent 正在写首封",
    Decision.NEEDS_EVIDENCE: "已退回补证，调研 agent 会重新查",
    Decision.UNFIT: "已标记不合适并归档",
}


@router.get("/")
def home(user: str = Depends(current_user)) -> Response:
    return redirect("/today")


@router.get("/today", response_class=HTMLResponse)
def today(request: Request, focus: int | None = None, user: str = Depends(current_user)) -> HTMLResponse:
    return _render_today(request, focus)


@router.post("/companies/{company_id}/review", response_model=None)
def review(
    request: Request,
    company_id: int,
    decision: Decision = Form(...),
    note: str = Form(""),
    user: str = Depends(current_user),
) -> Response:
    wb = get_wb(request)
    following = _next_pending(request, company_id)
    try:
        wb.review.decide(company_id, decision, note=note.strip(), user=user)
    except ValueError as exc:
        if is_htmx(request):
            return _render_today(request, company_id, headers=toast_header(str(exc), error=True))
        flash(request, str(exc), error=True)
        return redirect(f"/companies/{company_id}")

    link = f"/companies/{company_id}?tab=drafts" if decision is Decision.ACCEPT else ""
    if is_htmx(request):
        headers = toast_header(DECISION_DONE[decision], link=link, link_text="查看草稿" if link else "")
        return _render_today(request, following, headers=headers)
    flash(request, DECISION_DONE[decision])
    return redirect(request.headers.get("referer") or f"/companies/{company_id}")


def _next_pending(request: Request, company_id: int) -> int | None:
    """决定之前先记下队列里的下一张待审核卡（没有就取上一张），决定后焦点移过去。"""
    wb = get_wb(request)
    with wb.db.session() as s:
        ids = [c.id for c in wb.review.today(s) if c.status is CompanyStatus.QUEUED]
    if company_id not in ids:
        return None
    index = ids.index(company_id)
    rest = ids[index + 1 :] or ids[:index][-1:]
    return rest[0] if rest else None


def _render_today(request: Request, focus: int | None, *, headers: dict[str, str] | None = None) -> HTMLResponse:
    wb = get_wb(request)
    with wb.db.session() as s:
        cards = wb.review.today(s)
        pending = [c for c in cards if c.status is CompanyStatus.QUEUED]
        decided = [c for c in cards if c.status is not CompanyStatus.QUEUED]
        current = next((c for c in cards if c.id == focus), None)
        current = current or (pending[0] if pending else (decided[0] if decided else None))
    response = render(
        request,
        "pages/today.html",
        nav="today",
        pending=pending,
        decided=decided,
        current=current,
        limit=wb.settings.daily_review_limit,
    )
    if headers:
        response.headers.update(headers)
    if current is not None and is_htmx(request):
        response.headers["HX-Push-Url"] = f"/today?focus={current.id}"
    return response
