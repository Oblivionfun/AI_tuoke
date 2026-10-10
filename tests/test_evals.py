import asyncio
import json
import socket

import httpx
import respx

from tests.conftest import ROOT
from tests.factories import company_from_input, dossier, review
from workbench.agents.runner import ScriptedRunner
from workbench.agents.schemas import AgentRole, JudgeResult, JudgeScore
from workbench.evals import (
    GoldenCase,
    GoldenLabel,
    GoldenSet,
    compare_baseline,
    compare_reports,
    load_golden,
    render_baseline,
    run_golden,
    summarize,
    write_report,
)


def judge(spec, text):
    payload = json.loads(text)
    agrees = payload["dossier"]["queue"] == payload["label"]["expected_queue"]
    return JudgeResult(
        scores=[JudgeScore(criterion="accuracy", score=5 if agrees else 2, reason="对照标注")],
        queue_agrees_with_label=agrees,
        fabrication_suspected=False,
        comments="测试",
    )


def researcher(spec, text):
    c = company_from_input(text)
    queue = "unfit" if "maker" in c["website"] else "first_touch"
    return dossier(c["name"], c["website"], c["country"], queue=queue, email="info@" + c["website"].split("//")[1])


def test_run_golden_and_report(settings, tmp_path):
    golden = GoldenSet(
        version="t1",
        cases=[
            GoldenCase(
                id="g1",
                name="Alpha",
                website="https://alpha.ae",
                country="AE",
                label=GoldenLabel(expected_queue="first_touch", known_contacts=["info@alpha.ae"]),
                verified=True,
            ),
            GoldenCase(
                id="g2",
                name="Maker",
                website="https://maker.co.za",
                country="ZA",
                label=GoldenLabel(expected_queue="unfit", known_contacts=["sales@maker.co.za"]),
            ),
            GoldenCase(
                id="g3",
                name="Beta",
                website="https://beta.sa",
                country="SA",
                label=GoldenLabel(expected_queue="qualified"),
            ),
        ],
    )
    runner = ScriptedRunner(
        {AgentRole.RESEARCHER: researcher, AgentRole.REVIEWER: lambda s, t: review(), AgentRole.JUDGE: judge}
    )
    results, versions = asyncio.run(run_golden(golden, settings, runner))
    summary = summarize(results)
    assert summary["completed"] == 3
    assert summary["queue_accuracy"] == round(2 / 3, 3)
    assert summary["queue_accuracy_verified"] == 1.0
    assert summary["unfit_recall"] == 1.0
    assert summary["unknown_email_rate"] == round(1 / 3, 3)
    assert set(versions) == {"researcher", "reviewer", "judge"}
    path = write_report(tmp_path, results, versions, settings)
    md = path.with_suffix(".md").read_text(encoding="utf-8")
    assert "队列判断准确率" in md and "Maker（未核对）" in md
    assert "queue_accuracy" in compare_reports(path, path)


def test_golden_file_is_valid():
    golden = load_golden(ROOT / "eval" / "golden.yaml")
    assert 20 <= len(golden.cases) <= 30
    assert len({c.id for c in golden.cases}) == len(golden.cases)
    assert {c.label.expected_queue for c in golden.cases} <= {"first_touch", "qualified", "unfit"}
    assert any(c.label.expected_queue == "unfit" for c in golden.cases)


@respx.mock
def test_compare_baseline_checks_sites_and_emails():
    respx.get("https://real.ae/").mock(
        return_value=httpx.Response(200, text="<a href='mailto:sales@real.ae'>sales@real.ae</a>")
    )
    respx.get(url__regex=r"https://real\.ae/.+").mock(return_value=httpx.Response(404))
    respx.get("https://fake.ae/").mock(side_effect=httpx.ConnectError("refused"))
    items = [
        {"country": "AE", "name": "Real", "website": "https://real.ae/", "email": "sales@real.ae"},
        {"country": "AE", "name": "Fake", "website": "https://fake.ae/", "email": "info@fake.ae"},
        {"country": "AE", "name": "Gmail", "website": "https://real.ae/", "email": "realco@gmail.com"},
    ]
    items.append(
        {
            "country": "AE",
            "name": "Ghost",
            "website": "https://ghost-company.invalid/",
            "email": "a@ghost-company.invalid",
        }
    )

    async def fake_getaddrinfo(host, port, *args, **kwargs):
        if host.endswith(".invalid"):
            raise socket.gaierror("no such host")
        return [(None, None, None, "", ("127.0.0.1", port))]

    async def run():
        asyncio.get_running_loop().getaddrinfo = fake_getaddrinfo
        return await compare_baseline(items, known_domains={"real.ae"})

    checks = asyncio.run(run())
    assert [c.site_ok for c in checks] == [True, False, True, False]
    assert [c.domain_exists for c in checks] == [True, True, True, False]
    assert [c.email_found_on_site for c in checks] == [True, False, False, False]
    assert [c.email_on_site_domain for c in checks] == [True, True, False, True]
    md = render_baseline("测试", checks, {"companies": 1, "site_ok": "1", "email_sourced": "1", "unfit": 0})
    assert "官网能打开 | 2（50%）" in md and "域名根本不存在（DNS 查不到） | 1（25%）" in md
