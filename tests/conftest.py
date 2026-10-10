from datetime import UTC, datetime
from pathlib import Path

import pytest

from workbench.agents.runner import ScriptedRunner
from workbench.config import WorkbenchSettings
from workbench.pipeline import Workbench
from workbench.store.db import Database

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path) -> WorkbenchSettings:
    return WorkbenchSettings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'wb.db'}",
        prompts_dir=ROOT / "prompts",
        knowledge_dir=ROOT / "knowledge",
        csl_path=tmp_path / "missing.csv",
        research_concurrency=3,
        worker_poll_seconds=0.01,
        price_input_per_mtok=1.0,
        price_output_per_mtok=8.0,
        seller_email="sales@guanwei.example",
        seller_whatsapp="+86 310 000 0000",
    )


@pytest.fixture
def db(settings: WorkbenchSettings) -> Database:
    database = Database(settings.database_url)
    database.migrate()
    return database


@pytest.fixture
def make_wb(db, settings):
    """用脚本化的 agent 组装一个 Workbench；返回 (wb, runner)，runner.calls 记录每次调用的输入。"""

    def build(scripts):
        runner = ScriptedRunner(scripts)
        return Workbench(db, settings, runner, clock=lambda: NOW), runner

    return build
