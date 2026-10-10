import json

from sqlalchemy import func, select

from tests.conftest import NOW, ROOT
from tests.factories import candidate, dossier, draft, review
from workbench.agents.runner import ScriptedRunner
from workbench.demo import seed_demo, validate_demo_file
from workbench.pipeline import Workbench
from workbench.store.models import Company, CompanyStatus, Draft, Queue, Run, RunEvent

DEMO_DIR = ROOT / "seed" / "demo"


def write_demo(tmp_path):
    items = [
        {
            "candidate": candidate("Alpha", "https://alpha.ae").model_dump(),
            "dossier": dossier("Alpha", "https://alpha.ae", email="info@alpha.ae").model_dump(),
            "review": review().model_dump(),
        },
        {
            "candidate": candidate("Maker", "https://maker.ae").model_dump(),
            "dossier": dossier("Maker", "https://maker.ae", queue="unfit").model_dump(),
            "review": None,
        },
    ]
    (tmp_path / "dossiers_AE.json").write_text(
        json.dumps({"country": "AE", "method": "测试", "items": items}), encoding="utf-8"
    )
    reviews = {
        "country": "AE",
        "round": 2,
        "revised": ["maker.ae"],
        "reviews": {"maker.ae": review("needs_fix").model_dump()},
    }
    (tmp_path / "reviews_AE.json").write_text(json.dumps(reviews), encoding="utf-8")
    drafts = {"items": [{"domain": "alpha.ae", "draft": draft().model_dump()}]}
    (tmp_path / "drafts.json").write_text(json.dumps(drafts), encoding="utf-8")


def test_seed_demo_is_idempotent(db, settings, tmp_path):
    write_demo(tmp_path)
    wb = Workbench(db, settings, ScriptedRunner({}), clock=lambda: NOW)
    first = seed_demo(wb, tmp_path)
    assert (first.companies, first.updated, len(first.runs), first.drafts) == (2, 0, 3, 1)
    second = seed_demo(wb, tmp_path)
    assert (second.companies, second.updated) == (0, 2)
    with db.session() as s:
        assert s.scalar(select(func.count(Run.id))) == 3
        alpha = s.execute(select(Company).where(Company.domain == "alpha.ae")).scalar_one()
        assert alpha.queue is Queue.FIRST_TOUCH and alpha.review["verdict"] == "pass"
        maker = s.execute(select(Company).where(Company.domain == "maker.ae")).scalar_one()
        assert (
            maker.status is CompanyStatus.ARCHIVED and maker.review["verdict"] == "needs_fix" and maker.revisions == 1
        )
        texts = [e.text for e in s.execute(select(RunEvent)).scalars()]
        assert any(t.startswith("[调研] 打开：https://alpha.ae") for t in texts)
        assert sum("第 1 次返工" in t for t in texts) == 1
        assert alpha.status is CompanyStatus.ACCEPTED
        assert alpha.drafts[0].to_address == "info@alpha.ae"
        assert s.scalar(select(func.count(Draft.id))) == 1


def test_real_demo_files_validate():
    files = sorted(DEMO_DIR.glob("dossiers_*.json"))
    if not files:
        return
    for path in files:
        assert validate_demo_file(path) == [], path.name
