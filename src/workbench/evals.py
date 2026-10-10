"""评测集与对比。

- 评测集（eval/golden.yaml）：20-30 家人工标注过的真实公司。每次改提示词前后各跑一次：
  在临时数据库里走完整的调研 + 审查流程，再由评委 agent 对照人工标注打分，生成报告。
- 基线对比（compare_baseline）：检查"直接问聊天机器人"得到的名单——网站能不能打开、邮箱是不是官网域名、
  邮箱在官网上找不找得到——用来和工作台的卡片并排比较。

评测是测量，不是生产流程里的校验；生产流程仍然只靠提示词契约和审查 agent。
"""

import asyncio
import json
import re
import socket
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select

from workbench.agents.runner import AgentRunner
from workbench.agents.schemas import AgentRole, JudgeResult
from workbench.config import WorkbenchSettings
from workbench.normalize import host_of, normalize_domain
from workbench.pipeline import Workbench
from workbench.store.db import Database
from workbench.store.models import Company, Run, RunKind

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class GoldenLabel(BaseModel):
    expected_queue: str = Field(description="first_touch / qualified / unfit")
    company_type: str = ""
    known_contacts: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    notes: str = ""


class GoldenCase(BaseModel):
    id: str
    name: str
    website: str
    country: str
    label: GoldenLabel
    labeled_by: str = ""
    verified: bool = Field(False, description="人工核对过标注后改为 true；未核对的样本单独统计")


class GoldenSet(BaseModel):
    version: str
    cases: list[GoldenCase]


def load_golden(path: Path) -> GoldenSet:
    return GoldenSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass
class CaseResult:
    case_id: str
    name: str
    verified: bool
    expected_queue: str
    queue: str | None = None
    review_verdict: str | None = None
    revisions: int = 0
    judge: dict[str, Any] | None = None
    unknown_emails: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    web_search_calls: int = 0
    error: str = ""


def emails_in(dossier: dict[str, Any] | None) -> list[str]:
    return [c["value"].lower() for c in (dossier or {}).get("contacts", []) if c.get("kind") == "email"]


async def run_case(wb: Workbench, case: GoldenCase) -> CaseResult:
    result = CaseResult(case.id, case.name, case.verified, case.label.expected_queue)
    domain = normalize_domain(case.website) or case.website
    with wb.db.session() as s:
        company = Company(
            name=case.name, domain=domain, website=case.website, country=case.country.upper(), source="eval"
        )
        s.add(company)
        s.flush()
        run_id = wb.ctx.create_run(s, RunKind.RESEARCH, company_id=company.id).id
        company_id = company.id
    await wb.worker.execute(run_id)
    with wb.db.session() as s:
        run = s.get(Run, run_id)
        company = s.get(Company, company_id)
        assert run is not None and company is not None
        result.cost_usd = run.cost_usd
        result.web_search_calls = int(run.usage.get("web_search_calls", 0))
        if run.error:
            result.error = run.error
            return result
        result.queue = company.queue.value if company.queue else None
        result.review_verdict = (company.review or {}).get("verdict")
        result.revisions = company.revisions
        dossier, review = company.dossier, company.review
    known = {c.lower() for c in case.label.known_contacts}
    result.unknown_emails = [e for e in emails_in(dossier) if known and e not in known]
    try:
        judged: JudgeResult = await wb.ctx.call_agent(
            run_id,
            AgentRole.JUDGE,
            {"label": case.label.model_dump(), "dossier": dossier, "review": review},
            country=case.country,
        )
        result.judge = judged.model_dump()
    except Exception as exc:
        result.error = f"评委失败：{exc}"
    with wb.db.session() as s:
        run = s.get(Run, run_id)
        assert run is not None
        result.cost_usd = run.cost_usd
    return result


async def run_golden(
    golden: GoldenSet, settings: WorkbenchSettings, runner: AgentRunner, *, limit: int | None = None
) -> tuple[list[CaseResult], dict[str, str]]:
    """在临时数据库里跑评测集；返回逐家结果和本次用到的提示词版本。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(f"sqlite:///{Path(tmp) / 'eval.db'}")
        db.migrate()
        wb = Workbench(db, settings, runner)
        cases = golden.cases[:limit] if limit else golden.cases
        slots = asyncio.Semaphore(settings.research_concurrency)

        async def limited(case: GoldenCase) -> CaseResult:
            async with slots:
                return await run_case(wb, case)

        results = await asyncio.gather(*(limited(c) for c in cases))
        with db.session() as s:
            versions: dict[str, str] = {}
            for run in s.execute(select(Run)).scalars():
                for call in run.agent_calls:
                    versions[call["role"]] = call["prompt_version"]
    return list(results), versions


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    done = [r for r in results if not r.error and r.queue]
    judged = [r for r in done if r.judge]

    def rate(items: list[CaseResult], pred) -> float:
        return round(sum(1 for r in items if pred(r)) / len(items), 3) if items else 0.0

    criteria: dict[str, list[int]] = {}
    for r in judged:
        for s in r.judge["scores"]:  # type: ignore[index]
            criteria.setdefault(s["criterion"], []).append(int(s["score"]))
    return {
        "cases": len(results),
        "completed": len(done),
        "failed": len(results) - len(done),
        "queue_accuracy": rate(done, lambda r: r.queue == r.expected_queue),
        "queue_accuracy_verified": rate([r for r in done if r.verified], lambda r: r.queue == r.expected_queue),
        "unfit_recall": rate([r for r in done if r.expected_queue == "unfit"], lambda r: r.queue == "unfit"),
        "review_pass_rate": rate(done, lambda r: r.review_verdict == "pass"),
        "revision_rate": rate(done, lambda r: r.revisions > 0),
        "fabrication_suspected_rate": rate(judged, lambda r: bool(r.judge and r.judge["fabrication_suspected"])),
        "unknown_email_rate": rate(done, lambda r: bool(r.unknown_emails)),
        "judge_avg": {k: round(sum(v) / len(v), 2) for k, v in sorted(criteria.items())},
        "cost_usd_total": round(sum(r.cost_usd for r in results), 3),
        "cost_usd_per_company": round(sum(r.cost_usd for r in results) / len(results), 3) if results else 0.0,
        "web_search_calls_per_company": round(sum(r.web_search_calls for r in results) / len(results), 1)
        if results
        else 0.0,
    }


def write_report(
    out_dir: Path, results: list[CaseResult], versions: dict[str, str], settings: WorkbenchSettings
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    summary = summarize(results)
    data = {
        "created_at": stamp,
        "model": settings.model,
        "reasoning_effort": settings.reasoning_effort,
        "prompt_versions": versions,
        "summary": summary,
        "cases": [r.__dict__ for r in results],
    }
    path = out_dir / f"eval-{stamp}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.with_suffix(".md").write_text(render_report(data), encoding="utf-8")
    return path


def render_report(data: dict[str, Any]) -> str:
    s = data["summary"]
    lines = [
        f"# 评测报告 {data['created_at']}",
        "",
        f"- 模型：{data['model']}（推理档 {data['reasoning_effort']}）",
        f"- 提示词版本：{'，'.join(f'{k}={v}' for k, v in sorted(data['prompt_versions'].items()))}",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 样本 / 完成 / 失败 | {s['cases']} / {s['completed']} / {s['failed']} |",
        f"| 队列判断准确率（全部 / 已人工核对） | {s['queue_accuracy']:.0%} / {s['queue_accuracy_verified']:.0%} |",
        f"| 不合适公司识别率 | {s['unfit_recall']:.0%} |",
        f"| 审查通过率 / 返工率 | {s['review_pass_rate']:.0%} / {s['revision_rate']:.0%} |",
        f"| 评委怀疑编造的比例 | {s['fabrication_suspected_rate']:.0%} |",
        f"| 出现标注外邮箱的比例 | {s['unknown_email_rate']:.0%} |",
        f"| 评委平均分 | {'，'.join(f'{k} {v}' for k, v in s['judge_avg'].items())} |",
        f"| 每家花费 / 每家搜索次数 | ${s['cost_usd_per_company']} / {s['web_search_calls_per_company']} |",
        "",
        "## 逐家结果（人工抽检从这里挑）",
        "",
        "| 样本 | 标注队列 | 结果队列 | 审查 | 评委点评 | 标注外邮箱 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for c in data["cases"]:
        comment = (c["judge"] or {}).get("comments", "") if c["judge"] else c["error"]
        lines.append(
            f"| {c['name']}{'' if c['verified'] else '（未核对）'} | {c['expected_queue']} | {c['queue'] or '—'} | "
            f"{c['review_verdict'] or '—'} | {comment} | {'，'.join(c['unknown_emails']) or '—'} |"
        )
    return "\n".join(lines) + "\n"


def compare_reports(before: Path, after: Path) -> str:
    a = json.loads(before.read_text(encoding="utf-8"))["summary"]
    b = json.loads(after.read_text(encoding="utf-8"))["summary"]
    keys = [
        "queue_accuracy",
        "queue_accuracy_verified",
        "unfit_recall",
        "review_pass_rate",
        "fabrication_suspected_rate",
        "unknown_email_rate",
        "cost_usd_per_company",
    ]
    lines = ["| 指标 | 改前 | 改后 |", "| --- | --- | --- |"]
    lines += [f"| {k} | {a.get(k)} | {b.get(k)} |" for k in keys]
    for k in sorted(set(a.get("judge_avg", {})) | set(b.get("judge_avg", {}))):
        lines.append(f"| judge.{k} | {a.get('judge_avg', {}).get(k)} | {b.get('judge_avg', {}).get(k)} |")
    return "\n".join(lines)


# ---------- 基线对比：直接问聊天机器人的名单 ----------


@dataclass
class BaselineCheck:
    country: str
    name: str
    website: str
    email: str
    site_ok: bool = False
    site_blocked: bool = False
    domain_exists: bool = True
    site_note: str = ""
    email_on_site_domain: bool = False
    email_found_on_site: bool = False
    in_workbench: bool = False


async def _fetch(client: httpx.AsyncClient, url: str) -> tuple[bool, str, str]:
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        return False, type(exc).__name__, ""
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}", ""
    return True, f"HTTP {resp.status_code}", resp.text[:400_000]


async def check_item(client: httpx.AsyncClient, item: dict[str, str], known_domains: set[str]) -> BaselineCheck:
    website = item.get("website", "")
    email = (item.get("email") or "").strip().lower()
    check = BaselineCheck(item.get("country", ""), item.get("name", ""), website, email)
    domain = normalize_domain(website)
    check.in_workbench = bool(domain and domain in known_domains)
    check.email_on_site_domain = bool(domain) and (email.endswith(f"@{domain}") or email.endswith(f".{domain}"))
    if not website.startswith("http"):
        check.site_note = "没有网址"
        return check
    host = host_of(website)
    try:
        await asyncio.get_running_loop().getaddrinfo(host, 443)
    except (socket.gaierror, UnicodeError):
        check.domain_exists = False
        check.site_note = "域名不存在"
        return check
    ok, note, html = await _fetch(client, website)
    check.site_ok, check.site_note = ok, note
    check.site_blocked = note in {"HTTP 401", "HTTP 403", "HTTP 429"}
    if ok and email:
        pages = [html]
        for path in ("/contact", "/contact-us", "/contactus", "/about-us"):
            more_ok, _, more = await _fetch(client, website.rstrip("/") + path)
            if more_ok:
                pages.append(more)
        found = {e.lower() for page in pages for e in EMAIL_RE.findall(page)}
        check.email_found_on_site = email in found
    return check


async def compare_baseline(
    items: list[dict[str, str]], known_domains: set[str], concurrency: int = 8
) -> list[BaselineCheck]:
    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; GuanweiWorkbench/0.2; baseline check)"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:

        async def one(item: dict[str, str]) -> BaselineCheck:
            async with sem:
                return await check_item(client, item, known_domains)

        return list(await asyncio.gather(*(one(i) for i in items)))


def render_baseline(source: str, checks: list[BaselineCheck], workbench_stats: dict[str, Any]) -> str:
    n = len(checks) or 1
    site_ok = sum(c.site_ok for c in checks)
    blocked = sum(c.site_blocked for c in checks)
    missing = sum(not c.domain_exists for c in checks)
    other = n - site_ok - blocked - missing if checks else 0
    email_found = sum(c.email_found_on_site for c in checks)
    email_domain = sum(c.email_on_site_domain for c in checks)
    lines = [
        "# 名单对比：直接问聊天机器人 vs 工作台",
        "",
        f"基线来源：{source}",
        "",
        "| | 直接问聊天机器人 | 工作台卡片 |",
        "| --- | --- | --- |",
        f"| 公司数 | {len(checks)} | {workbench_stats['companies']} |",
        f"| 官网能打开 | {site_ok}（{site_ok / n:.0%}） | {workbench_stats['site_ok']} |",
        f"| 网站存在但拒绝自动访问（401/403/429） | {blocked}（{blocked / n:.0%}） | — |",
        f"| 域名根本不存在（DNS 查不到） | {missing}（{missing / n:.0%}） | 0 |",
        f"| 其他打不开（超时、连接失败、404 等） | {other}（{other / n:.0%}） | — |",
        f"| 邮箱在官网上找得到 | {email_found}（{email_found / n:.0%}） | {workbench_stats['email_sourced']} |",
        f"| 邮箱是公司自己的域名 | {email_domain}（{email_domain / n:.0%}） | — |",
        "| 每条信息附来源 | 否 | 是 |",
        f"| 判断过是不是买家 / 有没有红线 | 否 | 是（不合适 {workbench_stats['unfit']} 家已归档） |",
        "",
        "说明：'域名根本不存在'基本可以认定是编造的网址；'邮箱在官网上找得到'只检查首页和常见联系页，"
        "没找到不一定是编造，但需要人工确认后才能用。",
        "",
        "## 逐条检查",
        "",
        "| 国家 | 公司 | 网址 | 官网 | 邮箱 | 邮箱在官网上 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for c in checks:
        lines.append(
            f"| {c.country} | {c.name} | {c.website} | {'能打开' if c.site_ok else c.site_note} | {c.email or '—'} | "
            f"{'找到' if c.email_found_on_site else '未找到'} |"
        )
    return "\n".join(lines) + "\n"


# ---------- 生成评测集 ----------

GOLDEN_HEADER = (
    "# 冠维评测集：每次改提示词前后各跑一次 `workbench eval run`。\n"
    "# verified=false 的样本是研究子代理初标，业务员核对后改为 true；报告会分开统计。\n"
    "# 重新生成：workbench eval golden（会保留 verified=true 的人工标注）。\n"
)


def _case_id(country: str, website: str) -> str:
    return f"{country.lower()}-{(normalize_domain(website) or website).split('.')[0]}"


def _cases_from_dossiers(demo_dir: Path) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(demo_dir.glob("dossiers_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data["items"]:
            d = item["dossier"]
            cases.append(
                {
                    "id": _case_id(data["country"], d["website"]),
                    "name": d["company_name"],
                    "website": d["website"],
                    "country": data["country"],
                    "label": {
                        "expected_queue": d["queue"],
                        "company_type": d["business_type"]["value"][:60],
                        "known_contacts": [c["value"] for c in d["contacts"] if c["kind"] == "email"],
                        "red_flags": [r["text"] for r in d["risks"] if r["severity"] == "red"],
                        "notes": d["queue_reason"],
                    },
                    "labeled_by": "研究子代理初标，待业务员核对",
                    "verified": False,
                }
            )
    return cases


def _cases_from_negatives(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        {
            "id": _case_id(n["country"], n["website"]),
            "name": n["name"],
            "website": n["website"],
            "country": n["country"],
            "label": {
                "expected_queue": "unfit",
                "company_type": n["company_type"],
                "known_contacts": n.get("known_contacts", []),
                "red_flags": n.get("red_flags", []),
                "notes": n.get("notes", ""),
            },
            "labeled_by": "人工核对官网 / ITAC 公告",
            "verified": True,
        }
        for n in data["items"]
    ]


def build_golden(demo_dir: Path, negatives: Path, out: Path) -> list[dict[str, Any]]:
    """正例取自演示档案（未人工核对），反例取自反例清单；已有文件里 verified=true 的人工标注原样保留。"""
    existing: dict[str, Any] = {}
    if out.exists():
        old = yaml.safe_load(out.read_text(encoding="utf-8")) or {}
        existing = {c["id"]: c for c in old.get("cases", []) if c.get("verified")}
    cases = [existing.get(c["id"], c) for c in _cases_from_dossiers(demo_dir) + _cases_from_negatives(negatives)]
    body = yaml.safe_dump(
        {"version": datetime.now().strftime("%Y-%m-%d"), "cases": cases}, allow_unicode=True, sort_keys=False, width=200
    )
    out.write_text(GOLDEN_HEADER + body, encoding="utf-8")
    return cases
