"""演示数据：把 seed/demo/ 里的真实调研结果载入工作台。

这些档案由研究子代理按同一份提示词契约联网完成（没有 API key 时用于演示），
载入后和系统 agent 跑出来的卡片走同样的队列、审核、文案流程。运行记录会标明"演示数据"。
"""

import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from workbench.agents.schemas import Candidate, CompanyDossier, DraftEmail, ReviewResult
from workbench.normalize import normalize_domain
from workbench.pipeline import Workbench, pick_email
from workbench.store.models import Company, CompanyStatus, Draft, DraftStatus, Queue, Run, RunEvent, RunKind, RunStatus

DEMO_CREATOR = "演示数据"


@dataclass
class SeedReport:
    files: int = 0
    companies: int = 0
    updated: int = 0
    runs: list[int] = field(default_factory=list)
    drafts: int = 0


def dossier_sources(dossier: CompanyDossier) -> list[str]:
    urls: list[str] = []
    for item in (dossier.identity, dossier.business_type, dossier.buyer_role, dossier.city):
        urls += item.sources
    urls += (
        [p.source for p in dossier.products]
        + [c.source for c in dossier.contacts]
        + [s.source for s in dossier.signals]
    )
    seen: list[str] = []
    for url in urls:
        if url.startswith("http") and url not in seen:
            seen.append(url)
    return seen


def seed_demo(wb: Workbench, directory: Path) -> SeedReport:
    """重复执行会先删掉上一次载入的演示运行记录，公司按域名更新，不会重复。"""
    report = SeedReport()
    with wb.db.session() as s:
        old = list(s.execute(select(Run.id).where(Run.created_by == DEMO_CREATOR)).scalars())
        if old:
            s.execute(delete(RunEvent).where(RunEvent.run_id.in_(old)))
            s.execute(delete(Run).where(Run.id.in_(old), Run.parent_id.is_not(None)))
            s.execute(delete(Run).where(Run.id.in_(old)))
    for path in sorted(directory.glob("dossiers_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        reviews_path = directory / f"reviews_{data['country']}.json"
        review_file = json.loads(reviews_path.read_text(encoding="utf-8")) if reviews_path.exists() else {}
        report.files += 1
        _seed_file(wb, data, report, review_file.get("reviews", {}), set(review_file.get("revised", [])))
    drafts = directory / "drafts.json"
    if drafts.exists():
        report.drafts = _seed_drafts(wb, json.loads(drafts.read_text(encoding="utf-8")))
    return report


def _seed_drafts(wb: Workbench, data: dict[str, Any]) -> int:
    """演示用草稿：按文案契约写好的首封或征求同意信，对应公司标记为"已接受"。"""
    count = 0
    with wb.db.session() as s:
        for item in data.get("items", []):
            company = s.scalar(select(Company).where(Company.domain == item["domain"]))
            if company is None:
                continue
            draft = DraftEmail.model_validate(item["draft"])
            for old in company.drafts:
                if old.status is DraftStatus.DRAFT:
                    s.delete(old)
            s.add(
                Draft(
                    company_id=company.id,
                    kind=draft.kind,
                    to_address=pick_email(company.dossier),
                    subject_en=draft.subject_en,
                    body_en=draft.body_en,
                    subject_zh=draft.subject_zh,
                    body_zh=draft.body_zh,
                    notes={"personalization_basis": draft.personalization_basis, "send_tips_zh": draft.send_tips_zh},
                    created_at=wb.ctx.clock(),
                )
            )
            company.status = CompanyStatus.ACCEPTED
            company.decided_by = DEMO_CREATOR
            company.decided_at = wb.ctx.clock()
            company.queued_on = wb.ctx.today()
            count += 1
    return count


def _seed_file(
    wb: Workbench, data: dict[str, Any], report: SeedReport, reviews: dict[str, Any], revised: set[str]
) -> None:
    """reviews：审查子代理单独写的 reviews_<国家>.json，按域名对应；revised：因审查不通过返工过一次的域名。"""
    country = data["country"]
    method = data.get("method", "")
    now = wb.ctx.clock()
    items = []
    for i in data["items"]:
        dossier = CompanyDossier.model_validate(i["dossier"])
        raw_review = reviews.get(normalize_domain(dossier.website) or "") or i.get("review")
        items.append(
            (
                Candidate.model_validate(i["candidate"]),
                dossier,
                ReviewResult.model_validate(raw_review) if raw_review else None,
            )
        )
    with wb.db.session() as s:
        parent = wb.ctx.create_run(
            s,
            RunKind.PROSPECT,
            params={"country": country, "product_group": "", "demo": True, "method": method},
            created_by=DEMO_CREATOR,
        )
        parent.status = RunStatus.SUCCEEDED
        parent.started_at = now
        parent.finished_at = now
        parent_id = parent.id
        steps = [
            ("agent", "寻客 agent 开始"),
            ("step", f"寻客交回 {len(items)} 家候选"),
            ("step", f"去重：新增 {len(items)} 家"),
            ("step", f"开始调研 {len(items)} 家（最多同时 {wb.settings.research_concurrency} 家）"),
        ]
        for kind, text in steps:
            s.add(RunEvent(run_id=parent_id, kind=kind, text=text, created_at=now))

        counts = {"first_touch": 0, "qualified": 0, "unfit": 0}
        for offset, (cand, dossier, review) in enumerate(items):
            domain = normalize_domain(dossier.website or cand.website)
            if not domain:
                continue
            company = s.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    name=dossier.company_name,
                    domain=domain,
                    website=dossier.website or cand.website,
                    country=country,
                    source="demo",
                    prospect_evidence=cand.model_dump(),
                    created_at=now,
                )
                s.add(company)
                report.companies += 1
            else:
                evidence = cand.model_dump()
                if "catalog_lead" in (company.prospect_evidence or {}):
                    evidence["catalog_lead"] = company.prospect_evidence["catalog_lead"]
                company.prospect_evidence = evidence
                company.name = dossier.company_name or company.name
                report.updated += 1
            s.flush()
            child = wb.ctx.create_run(
                s,
                RunKind.RESEARCH,
                company_id=company.id,
                parent_id=parent_id,
                params={"demo": True},
                created_by=DEMO_CREATOR,
            )
            at = now + timedelta(seconds=offset)
            child.status = RunStatus.SUCCEEDED
            child.started_at = at
            child.finished_at = at
            child.agent_calls = [
                {
                    "role": "researcher",
                    "model": "研究子代理（演示数据）",
                    "prompt_version": "researcher-v1",
                    "trace_id": None,
                    "usage": {},
                    "error": "",
                    "at": at.isoformat(),
                },
            ]
            events = [("step", f"调研：{company.name}")]
            events += [("open", f"[调研] 打开：{url}") for url in dossier_sources(dossier)]
            if domain in revised:
                events.append(("review", "审查要求修改（第 1 次返工），调研 agent 已按意见修改"))
            if review is not None:
                events.append(
                    ("review", f"审查结论：{'通过' if review.verdict == 'pass' else '仍需修改'}，{review.summary_zh}")
                )
            for kind, text in events:
                s.add(RunEvent(run_id=child.id, kind=kind, text=text, created_at=at))
            wb.research.apply_dossier(company, dossier, review, revisions=int(domain in revised))
            child.summary = f"{company.name}：{Queue(dossier.queue).label}，{dossier.total_score} 分" + (
                f"，审查{'通过' if review.verdict == 'pass' else '未通过'}" if review else ""
            )
            counts[dossier.queue] += 1
            report.runs.append(child.id)
        parent.summary = f"【演示数据】新增 {len(items)} 家：可首触 {counts['first_touch']}、合格买家 {counts['qualified']}、不合适 {counts['unfit']}"
        s.add(RunEvent(run_id=parent_id, kind="status", text=f"完成：{parent.summary}", created_at=now))
        report.runs.append(parent_id)


def validate_demo_file(path: Path) -> list[str]:
    """检查演示数据文件：字段格式、域名重复、总分等于分项之和。返回问题列表，空列表表示通过。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(data.get("items", [])):
        label = f"items[{i}]"
        try:
            cand = Candidate.model_validate(item["candidate"])
            dossier = CompanyDossier.model_validate(item["dossier"])
            if item.get("review") is not None:
                ReviewResult.model_validate(item["review"])
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            continue
        domain = normalize_domain(dossier.website or cand.website)
        if not domain:
            errors.append(f"{label}: 官网无效 {dossier.website}")
        elif domain in seen:
            errors.append(f"{label}: 域名重复 {domain}")
        seen.add(domain or "")
        total = sum(s.score for s in dossier.scores)
        if total != dossier.total_score:
            errors.append(f"{label}: total_score {dossier.total_score} 与分项合计 {total} 不一致")
    if not data.get("items"):
        errors.append("items 为空")
    return errors


def demo_runs(wb: Workbench) -> list[Run]:
    with wb.db.session() as s:
        return list(s.execute(select(Run).where(Run.created_by == DEMO_CREATOR)).scalars())
