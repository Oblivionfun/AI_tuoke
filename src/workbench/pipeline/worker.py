"""worker：从数据库抢占排队的顶层任务并执行。寻客派生的调研子任务由寻客自己执行，worker 不碰。"""

import asyncio
from typing import assert_never

from sqlalchemy import select, update

from workbench.pipeline.context import Context
from workbench.pipeline.outreach import Outreach
from workbench.pipeline.prospect import Prospecting
from workbench.pipeline.research import Research
from workbench.store.models import Company, CompanyStatus, Run, RunKind, RunStatus


class Worker:
    def __init__(self, ctx: Context, prospect: Prospecting, research: Research, outreach: Outreach):
        self.ctx = ctx
        self.prospect = prospect
        self.research = research
        self.outreach = outreach

    async def execute(self, run_id: int) -> None:
        with self.ctx.db.session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            kind = run.kind
        match kind:
            case RunKind.PROSPECT:
                await self.prospect.execute(run_id)
            case RunKind.RESEARCH:
                await self.research.execute(run_id)
            case RunKind.WRITE:
                await self.outreach.execute(run_id)
            case _:
                assert_never(kind)

    def claim_next(self) -> int | None:
        """原子地把最早排队的顶层任务改成运行中；抢不到（被别的 worker 拿走）返回 None。"""
        with self.ctx.db.session() as session:
            run_id = session.scalar(
                select(Run.id).where(Run.status == RunStatus.QUEUED, Run.parent_id.is_(None)).order_by(Run.id).limit(1)
            )
            if run_id is None:
                return None
            claimed = session.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == RunStatus.QUEUED)
                .values(status=RunStatus.RUNNING, started_at=self.ctx.clock())
            ).rowcount
            return run_id if claimed else None

    def recover_stale(self) -> int:
        """启动时把上次中断的任务标记为失败，调研中的公司改为失败（网页上可以重试）。"""
        with self.ctx.db.session() as session:
            interrupted = (Run.status == RunStatus.RUNNING) | (
                (Run.status == RunStatus.QUEUED) & Run.parent_id.is_not(None)
            )
            stale = session.execute(select(Run).where(interrupted)).scalars().all()
            for run in stale:
                run.status = RunStatus.FAILED
                run.error = "worker 重启，任务中断"
                run.finished_at = self.ctx.clock()
            session.execute(
                update(Company).where(Company.status == CompanyStatus.RESEARCHING).values(status=CompanyStatus.FAILED)
            )
            return len(stale)

    async def loop(self, *, slots: int = 3, stop: asyncio.Event | None = None) -> None:
        self.recover_stale()
        running: set[asyncio.Task[None]] = set()
        while stop is None or not stop.is_set():
            while len(running) < slots and (run_id := self.claim_next()) is not None:
                task = asyncio.create_task(self.execute(run_id))
                running.add(task)
                task.add_done_callback(running.discard)
            await asyncio.sleep(self.ctx.settings.worker_poll_seconds)
        if running:
            await asyncio.gather(*running, return_exceptions=True)
