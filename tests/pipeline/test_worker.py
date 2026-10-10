"""worker：重启恢复、抢占并执行排队任务。"""

import asyncio

from tests.factories import dossier, draft
from workbench.agents.schemas import AgentRole
from workbench.pipeline import Decision
from workbench.store.models import (
    Company,
    CompanyStatus,
    Run,
    RunKind,
    RunStatus,
)


def test_recover_stale_and_worker_loop(db, settings, make_wb):
    with db.session() as s:
        s.add(
            Company(
                name="Lima", domain="lima.ae", website="https://lima.ae", country="AE", status=CompanyStatus.RESEARCHING
            )
        )
        s.add(Run(kind=RunKind.RESEARCH, status=RunStatus.RUNNING, company_id=1))
    wb, _ = make_wb({AgentRole.WRITER: lambda s_, t: draft()})
    assert wb.worker.recover_stale() == 1
    with db.session() as s:
        assert s.get(Company, 1).status is CompanyStatus.FAILED
        s.get(Company, 1).dossier = dossier("Lima", "https://lima.ae").model_dump()
        s.get(Company, 1).status = CompanyStatus.QUEUED
    write_run = wb.review.decide(1, Decision.ACCEPT, user="op")

    async def drive():
        stop = asyncio.Event()
        loop_task = asyncio.create_task(wb.worker.loop(stop=stop))
        for _ in range(200):
            await asyncio.sleep(0.01)
            with db.session() as s:
                if s.get(Run, write_run).status is RunStatus.SUCCEEDED:
                    break
        stop.set()
        await loop_task

    asyncio.run(drive())
    with db.session() as s:
        assert s.get(Run, write_run).status is RunStatus.SUCCEEDED
