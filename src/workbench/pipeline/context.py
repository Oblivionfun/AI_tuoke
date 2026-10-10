"""流水线各环节共用的依赖：数据库、配置、agent 运行器、时钟，以及运行记录和 agent 调用。"""

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from workbench.agents.prompts import PromptLibrary, products_summary
from workbench.agents.runner import AgentRunError, AgentRunner, AgentSpec, UsageRecord
from workbench.agents.schemas import OUTPUT_TYPES, ROLE_LABELS, AgentRole
from workbench.config import WorkbenchSettings
from workbench.store.db import Database
from workbench.store.models import Run, RunEvent, RunKind, RunStatus, utcnow

WEB_SEARCH_ROLES = {AgentRole.PROSPECTOR, AgentRole.RESEARCHER, AgentRole.REVIEWER}


class Context:
    def __init__(
        self,
        db: Database,
        settings: WorkbenchSettings,
        runner: AgentRunner,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.db = db
        self.settings = settings
        self.runner = runner
        self.clock = clock
        self.prompts = PromptLibrary(settings)

    # ---------- 时间（按工厂所在时区算"今天"） ----------

    def today(self) -> date:
        return self.clock().astimezone(ZoneInfo(self.settings.timezone)).date()

    def day_start_utc(self) -> datetime:
        return datetime.combine(self.today(), time.min, tzinfo=ZoneInfo(self.settings.timezone)).astimezone(UTC)

    # ---------- 运行记录 ----------

    def create_run(
        self,
        session: Session,
        kind: RunKind,
        *,
        params: dict[str, Any] | None = None,
        company_id: int | None = None,
        parent_id: int | None = None,
        created_by: str = "",
    ) -> Run:
        run = Run(
            kind=kind,
            params=params or {},
            company_id=company_id,
            parent_id=parent_id,
            created_by=created_by,
            created_at=self.clock(),
        )
        session.add(run)
        session.flush()
        return run

    def event(self, run_id: int, kind: str, text: str, data: dict[str, Any] | None = None) -> None:
        with self.db.session() as session:
            session.add(RunEvent(run_id=run_id, kind=kind, text=text, data=data or {}, created_at=self.clock()))

    def finish(self, run_id: int, status: RunStatus, summary: str = "", error: str = "") -> None:
        with self.db.session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.status = status
            run.summary = summary
            run.error = error
            run.finished_at = self.clock()
        self.event(run_id, "status", f"{'完成' if status is RunStatus.SUCCEEDED else '失败'}：{summary or error}")

    async def guarded(
        self,
        run_id: int,
        body: Callable[[int], Awaitable[None]],
        on_failure: Callable[[int], None] | None = None,
    ) -> None:
        """标记运行中 → 执行 → 出错时标记失败。任何一家公司出错都不影响同批其他公司。"""
        with self.db.session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.status = RunStatus.RUNNING
            run.started_at = self.clock()
        try:
            await body(run_id)
        except Exception as exc:
            self.finish(run_id, RunStatus.FAILED, error=str(exc) or type(exc).__name__)
            if on_failure is not None:
                on_failure(run_id)

    # ---------- agent 调用 ----------

    async def call_agent(self, run_id: int, role: AgentRole, payload: dict[str, Any], *, country: str | None) -> Any:
        """拼好分层提示词，调用一次 agent，把步骤写进运行记录，把用量和 trace 记到运行上。"""
        with self.db.session() as session:
            products = products_summary(session)
        bundle = self.prompts.build(role, country=country, products=products)
        spec = AgentSpec(
            role=role,
            instructions=bundle.text,
            output_type=OUTPUT_TYPES[role],
            model=self.settings.model_for(role),
            max_turns=self.settings.max_turns_for(role),
            web_search=role in WEB_SEARCH_ROLES,
            reasoning_effort=self.settings.reasoning_effort or None,
            search_context_size=self.settings.search_context_size,
            country=country,
            prompt_version=bundle.version,
        )
        label = ROLE_LABELS[role]
        self.event(
            run_id, "agent", f"{label} agent 开始（{spec.model}，提示词 {spec.prompt_version}）", {"role": role.value}
        )

        def on_step(kind: str, text: str, data: dict[str, Any]) -> None:
            self.event(run_id, kind, f"[{label}] {text}", {"role": role.value, **data})

        try:
            result = await self.runner.run(
                spec,
                json.dumps(payload, ensure_ascii=False, default=str),
                on_step=on_step,
                trace_group=f"run-{run_id}",
                trace_metadata={"run_id": str(run_id), "role": role.value},
            )
        except AgentRunError as exc:
            self._record_call(run_id, spec, exc.usage, exc.trace_id, error=str(exc))
            raise
        self._record_call(run_id, spec, result.usage, result.trace_id)
        return result.output

    def _record_call(
        self, run_id: int, spec: AgentSpec, usage: UsageRecord, trace_id: str | None, error: str = ""
    ) -> None:
        with self.db.session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.agent_calls = [
                *run.agent_calls,
                {
                    "role": spec.role.value,
                    "model": spec.model,
                    "prompt_version": spec.prompt_version,
                    "trace_id": trace_id,
                    "usage": usage.as_dict(),
                    "error": error,
                    "at": self.clock().isoformat(),
                },
            ]
            total = dict(run.usage)
            for key, value in usage.as_dict().items():
                total[key] = int(total.get(key, 0)) + value
            run.usage = total
            run.cost_usd = round(run.cost_usd + usage.cost_usd(self.settings), 4)
