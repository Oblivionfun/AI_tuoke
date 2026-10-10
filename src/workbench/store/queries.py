"""网页和流水线共用的只读查询。"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from workbench.store.models import Company, Run, RunEvent, RunStatus, Suppression


def is_suppressed(session: Session, domain: str) -> bool:
    return session.scalar(select(Suppression.id).where(Suppression.domain == domain)) is not None


def run_tree_ids(session: Session, run_id: int) -> list[int]:
    """一次寻客运行和它派生的全部调研子任务。"""
    return [run_id, *session.execute(select(Run.id).where(Run.parent_id == run_id)).scalars()]


def events_after(session: Session, run_ids: list[int], after_id: int) -> list[RunEvent]:
    stmt = select(RunEvent).where(RunEvent.run_id.in_(run_ids), RunEvent.id > after_id).order_by(RunEvent.id)
    return list(session.execute(stmt).scalars())


def tree_finished(session: Session, run_ids: list[int]) -> bool:
    open_runs = session.scalar(
        select(func.count(Run.id)).where(
            Run.id.in_(run_ids), or_(Run.status == RunStatus.QUEUED, Run.status == RunStatus.RUNNING)
        )
    )
    return not open_runs


def usage_since(session: Session, since: datetime) -> dict[str, Any]:
    runs = session.execute(select(Run).where(Run.created_at >= since)).scalars().all()
    total: dict[str, Any] = {"runs": len(runs), "cost_usd": round(sum(r.cost_usd for r in runs), 2)}
    for run in runs:
        for key, value in run.usage.items():
            total[key] = total.get(key, 0) + value
    return total


def company_names_by_run(session: Session, run_ids: list[int]) -> dict[int, str]:
    stmt = select(Run.id, Company.name).join(Company, Run.company_id == Company.id).where(Run.id.in_(run_ids))
    return dict(session.execute(stmt).all())
