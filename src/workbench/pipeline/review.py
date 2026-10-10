"""今日审核：每天最多 daily_review_limit 张卡；人工决定接受、补证或不合适。"""

from enum import StrEnum
from typing import assert_never

from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.pipeline.context import Context
from workbench.store.models import Company, CompanyStatus, Queue, RunKind


class Decision(StrEnum):
    ACCEPT = "accept"
    NEEDS_EVIDENCE = "needs_evidence"
    UNFIT = "unfit"


class ReviewQueue:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def today(self, session: Session) -> list[Company]:
        """先放前几天没处理完的，再按"合格买家优先、分数高优先"补足到每日上限；今天已处理的排在最后。"""
        today = self.ctx.today()
        pending = list(
            session.execute(
                select(Company).where(Company.status == CompanyStatus.QUEUED, Company.queued_on.is_not(None))
            ).scalars()
        )
        decided_today = list(
            session.execute(
                select(Company).where(Company.queued_on == today, Company.status != CompanyStatus.QUEUED)
            ).scalars()
        )
        slots = self.ctx.settings.daily_review_limit - len(pending) - len(decided_today)
        if slots > 0:
            fresh = session.execute(
                select(Company)
                .where(Company.status == CompanyStatus.QUEUED, Company.queued_on.is_(None))
                .order_by((Company.queue == Queue.QUALIFIED).desc(), Company.score.desc().nulls_last(), Company.id)
                .limit(slots)
            ).scalars()
            for company in fresh:
                company.queued_on = today
                pending.append(company)
        session.flush()
        pending.sort(key=lambda c: (c.queue is not Queue.QUALIFIED, -(c.score or 0)))
        return pending + decided_today

    def decide(self, company_id: int, decision: Decision, *, note: str = "", user: str = "") -> int | None:
        """接受时排队文案任务，补证时排队重新调研。返回新任务 id；同一张卡只能决定一次。"""
        with self.ctx.db.session() as session:
            company = session.get(Company, company_id)
            if company is None:
                raise ValueError("公司不存在")
            if company.status is not CompanyStatus.QUEUED:
                raise ValueError(f"这张卡已经处理过了（当前状态：{company.status.value}）")
            company.decision_note = note
            company.decided_by = user
            company.decided_at = self.ctx.clock()
            company.queued_on = company.queued_on or self.ctx.today()
            match decision:
                case Decision.ACCEPT:
                    company.status = CompanyStatus.ACCEPTED
                    kind, params = RunKind.WRITE, {"note": note}
                case Decision.NEEDS_EVIDENCE:
                    company.status = CompanyStatus.NEEDS_EVIDENCE
                    kind, params = RunKind.RESEARCH, {"note": note or "请补充证据"}
                case Decision.UNFIT:
                    company.status = CompanyStatus.ARCHIVED
                    return None
                case _:
                    assert_never(decision)
            return self.ctx.create_run(session, kind, company_id=company_id, params=params, created_by=user).id
