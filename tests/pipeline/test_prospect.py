"""寻客：去重、每日上限、粘贴网址、并行调研。"""

import asyncio
import json

from sqlalchemy import select

from tests.conftest import NOW
from tests.factories import candidate, researcher_echo, review
from workbench.agents.runner import ScriptedRunner
from workbench.agents.schemas import AgentRole, CandidateList
from workbench.pipeline import Workbench
from workbench.store.models import (
    Company,
    CompanyStatus,
    Queue,
    Run,
    RunEvent,
    RunStatus,
    Suppression,
)


def test_prospect_dedupes_researches_and_queues(db, settings, make_wb):
    with db.session() as s:
        s.add(Company(name="Old Co", domain="oldco.ae", country="AE"))
        s.add(Suppression(domain="nope.ae", reason="对方拒绝"))
    found = CandidateList(
        candidates=[
            candidate("Alpha Fasteners", "https://www.alpha-fasteners.ae/"),
            candidate("Alpha again", "https://alpha-fasteners.ae/contact"),
            candidate("Old Co", "https://oldco.ae"),
            candidate("Nope", "https://nope.ae"),
            candidate("Directory", "https://www.yellowpages.ae/alpha"),
            candidate("Beta Trading", "https://beta-trading.ae"),
        ],
        search_notes="测试",
    )
    wb, runner = make_wb(
        {
            AgentRole.PROSPECTOR: [found],
            AgentRole.RESEARCHER: researcher_echo,
            AgentRole.REVIEWER: lambda spec, text: review("pass"),
        },
    )
    run_id = wb.prospect.start(country="AE", product_group="自钻螺钉", user="op")
    assert wb.worker.claim_next() == run_id
    asyncio.run(wb.worker.execute(run_id))

    prospector_input = json.loads(runner.calls[0][1])
    assert {"oldco.ae", "nope.ae"} <= set(prospector_input["exclude_domains"])
    with db.session() as s:
        run = s.get(Run, run_id)
        assert run.status is RunStatus.SUCCEEDED, run.error
        assert "可首触 2" in run.summary
        companies = {c.domain: c for c in s.execute(select(Company)).scalars()}
        assert set(companies) == {"oldco.ae", "alpha-fasteners.ae", "beta-trading.ae"}
        alpha = companies["alpha-fasteners.ae"]
        assert alpha.status is CompanyStatus.QUEUED and alpha.queue is Queue.FIRST_TOUCH
        assert alpha.review["verdict"] == "pass"
        children = s.execute(select(Run).where(Run.parent_id == run_id)).scalars().all()
        assert len(children) == 2 and all(c.status is RunStatus.SUCCEEDED for c in children)
        assert all(c.usage["web_search_calls"] == 2 for c in children)
        texts = [e.text for e in s.execute(select(RunEvent).where(RunEvent.run_id == run_id)).scalars()]
        assert any("剔除" in t and "目录站/平台 1" in t and "拒绝联系 1" in t and "已有 1" in t for t in texts)


def test_research_runs_in_parallel_up_to_limit(db, settings, make_wb):
    class SlowRunner(ScriptedRunner):
        active = 0
        peak = 0

        async def run(self, spec, input_text, **kwargs):
            SlowRunner.active += 1
            SlowRunner.peak = max(SlowRunner.peak, SlowRunner.active)
            await asyncio.sleep(0.02)
            SlowRunner.active -= 1
            return await super().run(spec, input_text, **kwargs)

    found = CandidateList(candidates=[candidate(f"N{i}", f"https://n{i}.ae") for i in range(7)], search_notes="")
    runner = SlowRunner(
        {
            AgentRole.PROSPECTOR: [found],
            AgentRole.RESEARCHER: researcher_echo,
            AgentRole.REVIEWER: lambda s_, t: review(),
        }
    )
    wb = Workbench(db, settings, runner, clock=lambda: NOW)
    asyncio.run(wb.worker.execute(wb.prospect.start(country="AE")))
    assert SlowRunner.peak == settings.research_concurrency


def test_daily_candidate_cap(db, settings, make_wb):
    settings.daily_candidate_limit = 1
    found = CandidateList(
        candidates=[candidate("A", "https://a1.ae"), candidate("B", "https://b1.ae")], search_notes=""
    )
    wb, runner = make_wb(
        {
            AgentRole.PROSPECTOR: [found],
            AgentRole.RESEARCHER: researcher_echo,
            AgentRole.REVIEWER: lambda s_, t: review(),
        },
    )
    asyncio.run(wb.worker.execute(wb.prospect.start(country="AE")))
    assert json.loads(runner.calls[0][1])["count"] == 1
    with db.session() as s:
        assert len(s.execute(select(Company)).scalars().all()) == 1
    second = wb.prospect.start(country="AE")
    asyncio.run(wb.worker.execute(second))
    with db.session() as s:
        assert "上限" in s.get(Run, second).summary


def test_pasted_urls_skip_prospector(db, settings, make_wb):
    wb, runner = make_wb({AgentRole.RESEARCHER: researcher_echo, AgentRole.REVIEWER: lambda s_, t: review()})
    asyncio.run(
        wb.worker.execute(wb.prospect.start(country="ZA", urls=["https://www.nssfasteners.co.za/", "boltfast.co.za"]))
    )
    assert AgentRole.PROSPECTOR not in [r for r, _ in runner.calls]
    with db.session() as s:
        domains = {c.domain for c in s.execute(select(Company)).scalars()}
    assert domains == {"nssfasteners.co.za", "boltfast.co.za"}


def test_suppress_blocks_future_candidates(db, settings, make_wb):
    with db.session() as s:
        s.add(Company(name="Fox", domain="fox.ae", country="AE", status=CompanyStatus.CONTACTED))
    wb, _ = make_wb({})
    wb.outreach.suppress(1, reason="对方要求不再联系", user="op")
    with db.session() as s:
        assert s.get(Company, 1).status is CompanyStatus.ARCHIVED
        s.delete(s.get(Company, 1))
    with db.session() as s:
        report = wb.prospect.dedupe(
            s, [candidate("Fox", "https://fox.ae")], source="prospect", default_country="AE", limit=10
        )
        assert report.dropped["suppressed"] == 1 and not report.created
