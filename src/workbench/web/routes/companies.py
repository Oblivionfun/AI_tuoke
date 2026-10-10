"""客户：列表（按处理阶段分组）、档案（结论优先，详情分标签页）、草稿、联系记录、RFQ、拒绝联系。"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from workbench.pipeline import DraftAction
from workbench.store.models import Channel, Company, CompanyStatus, Direction, Rfq, RfqStatus, Run, RunKind, RunStatus
from workbench.web.deps import current_user, flash, get_wb, redirect, render

router = APIRouter()

STAGES = {
    "review": ("待审核", [CompanyStatus.QUEUED]),
    "active": (
        "进行中",
        [
            CompanyStatus.ACCEPTED,
            CompanyStatus.NEEDS_EVIDENCE,
            CompanyStatus.RESEARCHING,
            CompanyStatus.NEW,
            CompanyStatus.FAILED,
        ],
    ),
    "contacted": ("已联系", [CompanyStatus.CONTACTED]),
    "replied": ("已回复", [CompanyStatus.REPLIED]),
    "archived": ("已归档", [CompanyStatus.ARCHIVED]),
    "all": ("全部", list(CompanyStatus)),
}
DRAFT_DONE = {"save": "草稿已保存", "mark_sent": "已登记为已发送", "discard": "草稿已丢弃"}


@router.get("/companies", response_class=HTMLResponse)
def companies(
    request: Request, stage: str = "all", country: str = "", q: str = "", user: str = Depends(current_user)
) -> HTMLResponse:
    stage = stage if stage in STAGES else "all"
    wb = get_wb(request)
    stmt = select(Company).options(selectinload(Company.drafts)).where(Company.status.in_(STAGES[stage][1]))
    if country:
        stmt = stmt.where(Company.country == country.upper())
    if q:
        stmt = stmt.where(Company.name.ilike(f"%{q}%") | Company.domain.ilike(f"%{q}%"))
    stmt = stmt.order_by((Company.status == CompanyStatus.QUEUED).desc(), Company.score.desc().nulls_last()).limit(300)
    with wb.db.session() as s:
        rows = s.execute(stmt).scalars().all()
        counts = dict(s.execute(select(Company.status, func.count(Company.id)).group_by(Company.status)).all())
    tabs = [(key, label, sum(counts.get(st, 0) for st in statuses)) for key, (label, statuses) in STAGES.items()]
    return render(
        request,
        "pages/companies.html",
        nav="companies",
        rows=rows,
        tabs=tabs,
        stage=stage,
        country=country.upper(),
        q=q,
    )


@router.get("/companies/{company_id}", response_class=HTMLResponse)
def company_page(
    request: Request, company_id: int, tab: str = "dossier", user: str = Depends(current_user)
) -> HTMLResponse:
    company, runs = _load(request, company_id)
    return render(
        request,
        "pages/company.html",
        nav="companies",
        c=company,
        runs=runs,
        tab=tab,
        writing=_writing(runs),
    )


@router.get("/companies/{company_id}/drafts", response_class=HTMLResponse)
def company_drafts(request: Request, company_id: int, user: str = Depends(current_user)) -> HTMLResponse:
    """文案 agent 运行期间，草稿区每 3 秒轮询一次这个片段。"""
    company, runs = _load(request, company_id)
    return render(request, "partials/drafts.html", c=company, writing=_writing(runs))


@router.post("/companies/{company_id}/interactions")
def add_interaction(
    request: Request,
    company_id: int,
    channel: Channel = Form(...),
    direction: Direction = Form(...),
    summary: str = Form(...),
    user: str = Depends(current_user),
) -> Response:
    get_wb(request).outreach.add_interaction(
        company_id, channel=channel, direction=direction, summary=summary.strip(), user=user
    )
    flash(request, "已记录联系")
    return redirect(f"/companies/{company_id}?tab=followup")


@router.post("/companies/{company_id}/suppress")
def suppress(request: Request, company_id: int, reason: str = Form(...), user: str = Depends(current_user)) -> Response:
    get_wb(request).outreach.suppress(company_id, reason=reason.strip(), user=user)
    flash(request, "已加入拒绝联系名单，以后不会再出现")
    return redirect(f"/companies/{company_id}")


@router.post("/drafts/{draft_id}")
def update_draft(
    request: Request,
    draft_id: int,
    action: DraftAction = Form(...),
    subject_en: str | None = Form(None),
    body_en: str | None = Form(None),
    to_address: str | None = Form(None),
    user: str = Depends(current_user),
) -> Response:
    try:
        draft = get_wb(request).outreach.update_draft(
            draft_id, action, subject_en=subject_en, body_en=body_en, to_address=to_address, user=user
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    flash(request, DRAFT_DONE[action.value])
    return redirect(f"/companies/{draft.company_id}?tab=drafts")


@router.post("/rfqs")
def add_rfq(
    request: Request,
    company_id: int = Form(...),
    products: str = Form(...),
    quantity: str = Form(""),
    target_price: str = Form(""),
    incoterm: str = Form(""),
    notes: str = Form(""),
    user: str = Depends(current_user),
) -> Response:
    get_wb(request).outreach.add_rfq(
        company_id,
        products=products.strip(),
        quantity=quantity.strip(),
        target_price=target_price.strip(),
        incoterm=incoterm.strip(),
        notes=notes.strip(),
        user=user,
    )
    flash(request, "已记录 RFQ")
    return redirect(f"/companies/{company_id}?tab=followup")


@router.post("/rfqs/{rfq_id}/status")
def rfq_status(
    request: Request, rfq_id: int, status: RfqStatus = Form(...), user: str = Depends(current_user)
) -> Response:
    with get_wb(request).db.session() as s:
        rfq = s.get(Rfq, rfq_id)
        if rfq is None:
            raise HTTPException(404, "RFQ 不存在")
        rfq.status = status
        company_id = rfq.company_id
    flash(request, "RFQ 状态已更新")
    return redirect(request.headers.get("referer") or f"/companies/{company_id}?tab=followup")


def _load(request: Request, company_id: int) -> tuple[Company, list[Run]]:
    with get_wb(request).db.session() as s:
        company = s.execute(
            select(Company)
            .options(selectinload(Company.drafts), selectinload(Company.interactions), selectinload(Company.rfqs))
            .where(Company.id == company_id)
        ).scalar_one_or_none()
        if company is None:
            raise HTTPException(404, "公司不存在")
        runs = s.execute(select(Run).where(Run.company_id == company_id).order_by(Run.id.desc())).scalars().all()
    return company, list(runs)


def _writing(runs: list[Run]) -> bool:
    return any(r.kind is RunKind.WRITE and r.status in (RunStatus.QUEUED, RunStatus.RUNNING) for r in runs)
