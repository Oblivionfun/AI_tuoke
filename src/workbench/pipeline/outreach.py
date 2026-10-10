"""文案与跟进：接受后写首封（南非、沙特写征求同意信）；业务员手动发送后登记；联系记录、拒绝联系。

系统不发邮件：这里只有生成草稿和"登记已发送"，没有任何发信代码。
"""

from enum import StrEnum
from typing import Any, assert_never

from workbench.agents.schemas import AgentRole, DraftEmail
from workbench.pipeline.context import Context
from workbench.store.models import (
    Channel,
    Company,
    CompanyStatus,
    Direction,
    Draft,
    DraftStatus,
    Interaction,
    Run,
    RunStatus,
    Suppression,
)
from workbench.store.queries import is_suppressed

CONSENT_FIRST_COUNTRIES = {"ZA", "SA"}
PURCHASING_HINTS = ("purchas", "procure", "buy", "sourcing", "采购")


class DraftAction(StrEnum):
    SAVE = "save"
    MARK_SENT = "mark_sent"
    DISCARD = "discard"


def draft_kind_for(country: str) -> str:
    return "consent_request" if country.upper() in CONSENT_FIRST_COUNTRIES else "first_email"


def pick_email(dossier: dict[str, Any] | None) -> str:
    """从档案的公开联系方式里挑收件地址：采购邮箱优先，其次第一个邮箱。"""
    emails = [c for c in (dossier or {}).get("contacts", []) if c.get("kind") == "email" and "@" in c.get("value", "")]
    for contact in emails:
        text = f"{contact.get('value', '')} {contact.get('label', '')}".lower()
        if any(hint in text for hint in PURCHASING_HINTS):
            return contact["value"]
    return emails[0]["value"] if emails else ""


class Outreach:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def execute(self, run_id: int) -> None:
        await self.ctx.guarded(run_id, self.write)

    async def write(self, run_id: int) -> None:
        with self.ctx.db.session() as session:
            run = session.get(Run, run_id)
            assert run is not None and run.company_id is not None
            company = session.get(Company, run.company_id)
            assert company is not None
            kind = draft_kind_for(company.country)
            settings = self.ctx.settings
            payload = {
                "kind": kind,
                "dossier": company.dossier,
                "signature": {
                    "name": settings.seller_contact_name,
                    "company": settings.seller_company,
                    "email": settings.seller_email,
                    "whatsapp": settings.seller_whatsapp,
                    "website": settings.seller_website,
                },
                "operator_note": run.params.get("note", ""),
            }
            country, company_id, to_address = company.country, company.id, pick_email(company.dossier)
        draft: DraftEmail = await self.ctx.call_agent(run_id, AgentRole.WRITER, payload, country=country)
        with self.ctx.db.session() as session:
            session.add(
                Draft(
                    company_id=company_id,
                    run_id=run_id,
                    kind=draft.kind,
                    to_address=to_address,
                    subject_en=draft.subject_en,
                    body_en=draft.body_en,
                    subject_zh=draft.subject_zh,
                    body_zh=draft.body_zh,
                    notes={"personalization_basis": draft.personalization_basis, "send_tips_zh": draft.send_tips_zh},
                    created_at=self.ctx.clock(),
                )
            )
        label = "征求同意信" if kind == "consent_request" else "首封邮件"
        self.ctx.finish(run_id, RunStatus.SUCCEEDED, f"草稿已生成（{label}）")

    def update_draft(
        self,
        draft_id: int,
        action: DraftAction,
        *,
        subject_en: str | None = None,
        body_en: str | None = None,
        to_address: str | None = None,
        user: str = "",
    ) -> Draft:
        with self.ctx.db.session() as session:
            draft = session.get(Draft, draft_id)
            if draft is None:
                raise ValueError("草稿不存在")
            if subject_en is not None:
                draft.subject_en = subject_en
            if body_en is not None:
                draft.body_en = body_en
            if to_address is not None:
                draft.to_address = to_address.strip()
            match action:
                case DraftAction.SAVE:
                    pass
                case DraftAction.MARK_SENT:
                    draft.status = DraftStatus.SENT
                    draft.sent_by = user
                    draft.sent_at = self.ctx.clock()
                    session.add(
                        Interaction(
                            company_id=draft.company_id,
                            channel=Channel.EMAIL,
                            direction=Direction.OUT,
                            summary=f"已手动发送：{draft.subject_en}（收件 {draft.to_address or '未填'}）",
                            created_by=user,
                            happened_at=self.ctx.clock(),
                        )
                    )
                    company = session.get(Company, draft.company_id)
                    if company is not None and company.status in (CompanyStatus.ACCEPTED, CompanyStatus.QUEUED):
                        company.status = CompanyStatus.CONTACTED
                case DraftAction.DISCARD:
                    draft.status = DraftStatus.DISCARDED
                case _:
                    assert_never(action)
            return draft

    def add_interaction(
        self, company_id: int, *, channel: Channel, direction: Direction, summary: str, user: str = ""
    ) -> None:
        with self.ctx.db.session() as session:
            company = session.get(Company, company_id)
            if company is None:
                raise ValueError("公司不存在")
            session.add(
                Interaction(
                    company_id=company_id,
                    channel=channel,
                    direction=direction,
                    summary=summary,
                    created_by=user,
                    happened_at=self.ctx.clock(),
                )
            )
            if direction is Direction.IN and company.status is not CompanyStatus.ARCHIVED:
                company.status = CompanyStatus.REPLIED

    def suppress(self, company_id: int, *, reason: str, user: str = "") -> None:
        """拒绝联系：加入排除清单并归档，作废未发的草稿；以后的寻客和去重都会跳过这个域名。"""
        with self.ctx.db.session() as session:
            company = session.get(Company, company_id)
            if company is None:
                raise ValueError("公司不存在")
            if not is_suppressed(session, company.domain):
                session.add(
                    Suppression(
                        domain=company.domain,
                        company_name=company.name,
                        reason=reason,
                        created_by=user,
                        created_at=self.ctx.clock(),
                    )
                )
            company.status = CompanyStatus.ARCHIVED
            company.decision_note = f"拒绝联系：{reason}"
            for draft in company.drafts:
                if draft.status is DraftStatus.DRAFT:
                    draft.status = DraftStatus.DISCARDED
