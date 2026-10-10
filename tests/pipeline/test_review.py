"""今日审核：每日上限与排序、同一张卡只能决定一次、补证退回调研。"""

import asyncio
import json

import pytest
from sqlalchemy import select

from tests.factories import dossier, researcher_echo, review
from workbench.agents.schemas import AgentRole
from workbench.pipeline import Decision
from workbench.store.models import (
    Company,
    CompanyStatus,
    Queue,
    Run,
    RunKind,
)


def test_today_queue_limits_and_orders(db, settings, make_wb):
    with db.session() as s:
        for i in range(12):
            s.add(
                Company(
                    name=f"C{i}",
                    domain=f"c{i}.ae",
                    country="AE",
                    status=CompanyStatus.QUEUED,
                    queue=Queue.QUALIFIED if i == 11 else Queue.FIRST_TOUCH,
                    score=50 + i,
                )
            )
    wb, _ = make_wb({})
    with db.session() as s:
        today = wb.review.today(s)
        assert len(today) == 10
        assert today[0].name == "C11"
        assert [c.score for c in today[1:]] == sorted([c.score for c in today[1:]], reverse=True)
    with db.session() as s:
        assert len(wb.review.today(s)) == 10
    wb.review.decide(today[0].id, Decision.UNFIT, note="规模太小", user="op")
    with db.session() as s:
        again = wb.review.today(s)
        assert len(again) == 10 and again[-1].status is CompanyStatus.ARCHIVED


def test_decide_twice_is_rejected(db, settings, make_wb):
    with db.session() as s:
        s.add(Company(name="Hotel", domain="hotel.ae", country="AE", status=CompanyStatus.QUEUED))
    wb, _ = make_wb({})
    wb.review.decide(1, Decision.ACCEPT, user="op")
    with pytest.raises(ValueError, match="已经处理过"):
        wb.review.decide(1, Decision.ACCEPT, user="op")
    with db.session() as s:
        assert len(s.execute(select(Run).where(Run.kind == RunKind.WRITE)).scalars().all()) == 1


def test_needs_evidence_requeues_research_with_note(db, settings, make_wb):
    with db.session() as s:
        s.add(
            Company(
                name="Echo",
                domain="echo.ae",
                website="https://echo.ae",
                country="AE",
                status=CompanyStatus.QUEUED,
                dossier=dossier("Echo", "https://echo.ae").model_dump(),
            )
        )
    wb, runner = make_wb({AgentRole.RESEARCHER: researcher_echo, AgentRole.REVIEWER: lambda s_, t: review()})
    run_id = wb.review.decide(1, Decision.NEEDS_EVIDENCE, note="补查供应商注册入口", user="op")
    asyncio.run(wb.worker.execute(run_id))
    payload = json.loads(runner.calls[0][1])
    assert payload["operator_note"] == "补查供应商注册入口" and "previous_dossier" in payload
    with db.session() as s:
        assert s.get(Company, 1).status is CompanyStatus.QUEUED
