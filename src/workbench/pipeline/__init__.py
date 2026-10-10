"""代码编排的固定工作流。

    寻客 → 按域名去重 → 调研（每家一个干净上下文，并行）→ 审查（不通过最多返工一次）
    → 今日审核（每天最多 10 张）→ 人工决定 → 接受后写信 → 业务员手动发送
    → 对方回复、询价 → 人填报价单 → 业务员手动发送

agent 之间只传结构化结果，不传网页原文。代码不校验 agent 的结论，只负责去重、排队、记账和三条底线。
入口是 `Workbench`：网页和命令行都通过它调用各环节，例如 `wb.review.decide(...)`。
"""

from collections.abc import Callable
from datetime import datetime

from workbench.agents.runner import AgentRunner
from workbench.config import WorkbenchSettings
from workbench.pipeline.context import Context
from workbench.pipeline.outreach import DraftAction, Outreach, draft_kind_for, pick_email
from workbench.pipeline.prospect import Prospecting
from workbench.pipeline.quotes import QuoteAction, Quoting
from workbench.pipeline.research import Research
from workbench.pipeline.review import Decision, ReviewQueue
from workbench.pipeline.worker import Worker
from workbench.store.db import Database
from workbench.store.models import utcnow

__all__ = ["Decision", "DraftAction", "QuoteAction", "Workbench", "draft_kind_for", "pick_email"]


class Workbench:
    def __init__(
        self,
        db: Database,
        settings: WorkbenchSettings,
        runner: AgentRunner,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.ctx = Context(db, settings, runner, clock)
        self.research = Research(self.ctx)
        self.prospect = Prospecting(self.ctx, self.research)
        self.review = ReviewQueue(self.ctx)
        self.outreach = Outreach(self.ctx)
        self.quotes = Quoting(self.ctx)
        self.worker = Worker(self.ctx, self.prospect, self.research, self.outreach)

    @property
    def db(self) -> Database:
        return self.ctx.db

    @property
    def settings(self) -> WorkbenchSettings:
        return self.ctx.settings
