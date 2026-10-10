"""跟进：待发送的草稿、等对方回复、已回复，三栏看板 + RFQ 列表。"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from workbench.store.models import Company, CompanyStatus, Draft, DraftStatus, Rfq
from workbench.web.deps import current_user, get_wb, render

router = APIRouter()


@router.get("/followups", response_class=HTMLResponse)
def followups(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    with get_wb(request).db.session() as s:
        drafts = (
            s.execute(
                select(Draft)
                .options(selectinload(Draft.company))
                .where(Draft.status == DraftStatus.DRAFT)
                .order_by(Draft.id.desc())
            )
            .scalars()
            .all()
        )
        waiting = (
            s.execute(
                select(Company)
                .options(selectinload(Company.interactions))
                .where(Company.status == CompanyStatus.CONTACTED)
                .order_by(Company.updated_at)
            )
            .scalars()
            .all()
        )
        replied = (
            s.execute(
                select(Company)
                .options(selectinload(Company.interactions), selectinload(Company.rfqs))
                .where(Company.status == CompanyStatus.REPLIED)
                .order_by(Company.updated_at.desc())
            )
            .scalars()
            .all()
        )
        rfqs = (
            s.execute(select(Rfq).options(selectinload(Rfq.company)).order_by(Rfq.id.desc()).limit(100)).scalars().all()
        )
    return render(
        request, "pages/followups.html", nav="followups", drafts=drafts, waiting=waiting, replied=replied, rfqs=rfqs
    )
