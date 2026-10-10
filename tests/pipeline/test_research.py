"""调研 + 审查：最多返工一次、不合适归档、按国家加载知识包、失败处理、用量记账。"""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.conftest import NOW
from tests.factories import dossier, researcher_echo, review
from workbench.agents.runner import AgentRunError
from workbench.agents.schemas import AgentRole
from workbench.store.models import (
    Company,
    CompanyStatus,
    Run,
    RunStatus,
)
from workbench.store.queries import usage_since


def test_review_allows_at_most_one_revision(db, settings, make_wb):
    with db.session() as s:
        s.add(Company(name="Gamma", domain="gamma.ae", website="https://gamma.ae", country="AE"))
    wb, runner = make_wb(
        {
            AgentRole.RESEARCHER: researcher_echo,
            AgentRole.REVIEWER: lambda spec, text: review("needs_fix"),
        },
    )
    with db.session() as s:
        cid = s.scalar(select(Company.id))
    run_id = wb.research.start(cid)
    asyncio.run(wb.worker.execute(run_id))
    roles = [role for role, _ in runner.calls]
    assert roles == [AgentRole.RESEARCHER, AgentRole.REVIEWER, AgentRole.RESEARCHER, AgentRole.REVIEWER]
    revise_input = json.loads(runner.calls[2][1])
    assert revise_input["review_notes"]["issues"][0]["severity"] == "must_fix"
    assert json.loads(runner.calls[3][1])["round"] == 2
    with db.session() as s:
        company = s.get(Company, cid)
        assert company.revisions == 1 and company.review["verdict"] == "needs_fix"
        assert company.status is CompanyStatus.QUEUED
        assert len(s.get(Run, run_id).agent_calls) == 4


def test_unfit_is_archived(db, settings, make_wb):
    with db.session() as s:
        s.add(Company(name="Maker", domain="maker.co.za", website="https://maker.co.za", country="ZA"))
    wb, _ = make_wb(
        {
            AgentRole.RESEARCHER: lambda spec, text: dossier("Maker", "https://maker.co.za", "ZA", queue="unfit"),
            AgentRole.REVIEWER: lambda spec, text: review("pass"),
        },
    )
    run_id = wb.research.start(1)
    asyncio.run(wb.worker.execute(run_id))
    with db.session() as s:
        company = s.get(Company, 1)
        assert company.status is CompanyStatus.ARCHIVED and "不合适" in company.decision_note


def test_research_prompt_loads_country_notes(db, settings, make_wb):
    with db.session() as s:
        s.add(Company(name="Zed", domain="zed.co.za", website="https://zed.co.za", country="ZA"))
    seen = {}

    def capture(spec, text):
        seen["instructions"] = spec.instructions
        seen["version"] = spec.prompt_version
        return dossier("Zed", "https://zed.co.za", "ZA")

    wb, _ = make_wb({AgentRole.RESEARCHER: capture, AgentRole.REVIEWER: lambda s_, t: review()})
    asyncio.run(wb.worker.execute(wb.research.start(1)))
    assert "POPIA" in seen["instructions"] and "阿联酋（AE）" not in seen["instructions"]
    assert seen["version"].startswith("researcher-v1+")
    with db.session() as s:
        assert s.get(Run, 1).agent_calls[0]["prompt_version"] == seen["version"]


def test_agent_failure_marks_run_and_company_failed(db, settings, make_wb):
    with db.session() as s:
        s.add(Company(name="Kilo", domain="kilo.ae", website="https://kilo.ae", country="AE"))

    def boom(spec, text):
        raise AgentRunError("超过最大轮数 30")

    wb, _ = make_wb({AgentRole.RESEARCHER: boom})
    run_id = wb.research.start(1)
    asyncio.run(wb.worker.execute(run_id))
    with db.session() as s:
        run = s.get(Run, run_id)
        assert run.status is RunStatus.FAILED and "最大轮数" in run.error
        assert s.get(Company, 1).status is CompanyStatus.FAILED


def test_cost_is_recorded(db, settings, make_wb):
    with db.session() as s:
        s.add(Company(name="Mike", domain="mike.ae", website="https://mike.ae", country="AE"))
    wb, _ = make_wb({AgentRole.RESEARCHER: researcher_echo, AgentRole.REVIEWER: lambda s_, t: review()})
    run_id = wb.research.start(1)
    asyncio.run(wb.worker.execute(run_id))
    with db.session() as s:
        run = s.get(Run, run_id)
        assert run.usage["web_search_calls"] == 2
        assert run.cost_usd == pytest.approx(0.02)
        assert usage_since(s, NOW - timedelta(days=7))["web_search_calls"] == 2


def test_suppressed_company_cannot_be_requeued(db, settings, make_wb):
    with db.session() as s:
        s.add(
            Company(name="Golf", domain="golf.ae", website="https://golf.ae", country="AE", status=CompanyStatus.QUEUED)
        )
    wb, _ = make_wb({AgentRole.RESEARCHER: researcher_echo, AgentRole.REVIEWER: lambda s_, t: review()})
    run_id = wb.research.start(1)
    wb.outreach.suppress(1, reason="对方拒绝", user="op")
    asyncio.run(wb.worker.execute(run_id))
    with db.session() as s:
        assert s.get(Company, 1).status is CompanyStatus.ARCHIVED
    with pytest.raises(ValueError, match="拒绝联系"):
        wb.research.start(1)
