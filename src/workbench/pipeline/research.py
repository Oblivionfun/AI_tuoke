"""调研 + 审查：每家公司一个干净上下文；审查不通过最多返工一次（次数见配置 max_revisions）。"""

import asyncio
from typing import Any

from workbench.agents.schemas import AgentRole, CompanyDossier, ReviewResult
from workbench.normalize import display_company_name
from workbench.pipeline.context import Context
from workbench.sanctions import SanctionScreener, read_csl
from workbench.store.models import Company, CompanyStatus, Queue, Run, RunKind, RunStatus
from workbench.store.queries import is_suppressed


class Research:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._slots = asyncio.Semaphore(ctx.settings.research_concurrency)
        self._screener: SanctionScreener | None = None

    def start(self, company_id: int, *, note: str = "", user: str = "") -> int:
        """排队一次调研（网页上的"重新调研"、补证、CLI）。返回运行 id。"""
        with self.ctx.db.session() as session:
            company = session.get(Company, company_id)
            if company is None:
                raise ValueError("公司不存在")
            if is_suppressed(session, company.domain):
                raise ValueError("这家公司在拒绝联系名单里，不能再调研")
            company.status = CompanyStatus.RESEARCHING
            return self.ctx.create_run(
                session, RunKind.RESEARCH, company_id=company_id, params={"note": note}, created_by=user
            ).id

    async def execute(self, run_id: int) -> None:
        await self.ctx.guarded(run_id, self.run, on_failure=self._mark_failed)

    async def run_many(self, company_ids: list[int], *, parent_id: int | None, note: str = "") -> list[int]:
        """并行调研一批公司，同时最多 research_concurrency 家。返回子运行 id。"""
        with self.ctx.db.session() as session:
            child_ids = [
                self.ctx.create_run(
                    session, RunKind.RESEARCH, company_id=cid, parent_id=parent_id, params={"note": note}
                ).id
                for cid in company_ids
            ]

        async def one(child_id: int) -> None:
            async with self._slots:
                await self.execute(child_id)

        await asyncio.gather(*(one(cid) for cid in child_ids))
        return child_ids

    async def run(self, run_id: int) -> None:
        payload, country, company_id, label = self._load(run_id)
        self.ctx.event(run_id, "step", f"调研：{label}")

        dossier: CompanyDossier = await self.ctx.call_agent(run_id, AgentRole.RESEARCHER, payload, country=country)
        review = await self._review(run_id, dossier, round_=1, country=country)
        revisions = 0
        while review.verdict == "needs_fix" and revisions < self.ctx.settings.max_revisions:
            revisions += 1
            self.ctx.event(run_id, "review", f"审查要求修改（第 {revisions} 次返工）：{review.summary_zh}")
            revise = {
                **payload,
                "previous_dossier": dossier.model_dump(),
                "review_notes": {
                    "notes": review.notes_for_researcher,
                    "issues": [i.model_dump() for i in review.issues],
                },
            }
            dossier = await self.ctx.call_agent(run_id, AgentRole.RESEARCHER, revise, country=country)
            review = await self._review(run_id, dossier, round_=revisions + 1, country=country)
        passed = review.verdict == "pass"
        self.ctx.event(run_id, "review", f"审查结论：{'通过' if passed else '仍需修改'}，{review.summary_zh}")

        with self.ctx.db.session() as session:
            company = session.get(Company, company_id)
            assert company is not None
            self.apply_dossier(company, dossier, review, revisions)
            if is_suppressed(session, company.domain):
                company.status = CompanyStatus.ARCHIVED
        summary = (
            f"{label}：{Queue(dossier.queue).label}，{dossier.total_score} 分，审查{'通过' if passed else '未通过'}"
        )
        self.ctx.finish(run_id, RunStatus.SUCCEEDED, summary)
        if dossier.queue == Queue.UNFIT:
            self.ctx.event(run_id, "step", f"判定不合适，已归档：{dossier.queue_reason}")

    def apply_dossier(
        self, company: Company, dossier: CompanyDossier, review: ReviewResult | None, revisions: int
    ) -> None:
        """把档案写到公司上，并按队列决定状态：不合适直接归档，其余进入待审核。"""
        company.dossier = dossier.model_dump()
        company.review = review.model_dump() if review else None
        company.revisions = revisions
        company.queue = Queue(dossier.queue)
        company.score = dossier.total_score
        if company.source == "url" or company.name == company.domain:
            company.name = dossier.company_name or company.name
        company.sanctions_hint = self.sanctions_hint(dossier.company_name or company.name, company.country)
        company.queued_on = None
        company.updated_at = self.ctx.clock()
        if company.queue is Queue.UNFIT:
            company.status = CompanyStatus.ARCHIVED
            company.decision_note = f"调研判定不合适：{dossier.queue_reason}"
        else:
            company.status = CompanyStatus.QUEUED

    def sanctions_hint(self, name: str, country: str) -> dict[str, Any] | None:
        """制裁名单名称比对，只作提示。没下载名单时返回 None。"""
        if self._screener is None:
            if not self.ctx.settings.csl_path.exists():
                return None
            self._screener = SanctionScreener(read_csl(self.ctx.settings.csl_path))
        hit = self._screener.match(display_company_name(name), country)
        checked_at = self.ctx.clock().isoformat()
        if hit is None:
            return {"hit": False, "checked_at": checked_at}
        return {
            "hit": True,
            "listed_name": hit.listed_name,
            "score": hit.score,
            "source": hit.source,
            "programs": hit.programs,
            "checked_at": checked_at,
        }

    async def _review(self, run_id: int, dossier: CompanyDossier, *, round_: int, country: str) -> ReviewResult:
        payload = {"dossier": dossier.model_dump(), "round": round_}
        return await self.ctx.call_agent(run_id, AgentRole.REVIEWER, payload, country=country)

    def _load(self, run_id: int) -> tuple[dict[str, Any], str, int, str]:
        """组装调研 agent 的输入：公司、寻客依据、运营备注、联系记录；补证时附上原档案。"""
        with self.ctx.db.session() as session:
            run = session.get(Run, run_id)
            assert run is not None and run.company_id is not None
            company = session.get(Company, run.company_id)
            assert company is not None
            company.status = CompanyStatus.RESEARCHING
            note = run.params.get("note", "")
            payload: dict[str, Any] = {
                "company": {
                    "name": company.name,
                    "website": company.website,
                    "country": company.country,
                    "prospect_evidence": company.prospect_evidence,
                },
                "operator_note": note,
                "interactions": [
                    {
                        "channel": i.channel.value,
                        "direction": i.direction.value,
                        "summary": i.summary,
                        "at": i.happened_at.isoformat(),
                    }
                    for i in company.interactions[:10]
                ],
            }
            if company.dossier and note:
                payload["previous_dossier"] = company.dossier
            return payload, company.country, company.id, company.name

    def _mark_failed(self, run_id: int) -> None:
        with self.ctx.db.session() as session:
            run = session.get(Run, run_id)
            company = session.get(Company, run.company_id) if run and run.company_id else None
            if company is not None and company.status is CompanyStatus.RESEARCHING:
                company.status = CompanyStatus.FAILED
