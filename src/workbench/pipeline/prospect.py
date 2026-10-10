"""寻客：联网找候选（或用运营粘贴的网址）→ 按域名去重、剔除排除清单 → 交给调研并行处理。"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from workbench.agents.schemas import AgentRole, Candidate, CandidateList
from workbench.normalize import normalize_domain
from workbench.pipeline.context import Context
from workbench.pipeline.research import Research
from workbench.store.models import Company, Queue, Run, RunKind, RunStatus, Suppression

EXCLUDE_LIST_LIMIT = 500


DROP_LABELS = {
    "existing": "已有",
    "suppressed": "拒绝联系",
    "directory": "目录站/平台",
    "duplicate": "本批重复",
    "invalid": "网址无效",
    "over_limit": "超出今日上限",
}


@dataclass
class DedupeReport:
    created: list[int] = field(default_factory=list)
    dropped: Counter[str] = field(default_factory=Counter)

    def text(self) -> str:
        detail = "、".join(f"{label} {self.dropped[key]}" for key, label in DROP_LABELS.items() if self.dropped[key])
        return f"去重：新增 {len(self.created)} 家" + (f"，剔除 {detail}" if detail else "")


class Prospecting:
    def __init__(self, ctx: Context, research: Research):
        self.ctx = ctx
        self.research = research

    def start(
        self,
        *,
        country: str,
        product_group: str = "",
        urls: list[str] | None = None,
        count: int | None = None,
        note: str = "",
        user: str = "",
    ) -> int:
        with self.ctx.db.session() as session:
            params = {
                "country": country.upper(),
                "product_group": product_group,
                "urls": urls or [],
                "count": count,
                "note": note,
            }
            return self.ctx.create_run(session, RunKind.PROSPECT, params=params, created_by=user).id

    async def execute(self, run_id: int) -> None:
        await self.ctx.guarded(run_id, self.run)

    async def run(self, run_id: int) -> None:
        with self.ctx.db.session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            params = dict(run.params)
            remaining = self.ctx.settings.daily_candidate_limit - self.candidates_today(session)
            exclude = self.exclusion_domains(session)
        country = str(params.get("country", "")).upper()
        urls = [u for u in params.get("urls", []) if u.strip()]
        if remaining <= 0:
            limit = self.ctx.settings.daily_candidate_limit
            self.ctx.finish(run_id, RunStatus.SUCCEEDED, f"今日新增候选已达上限 {limit} 家")
            return

        if urls:
            source, candidates = "url", [_candidate_from_url(u, country) for u in urls]
            self.ctx.event(run_id, "step", f"使用运营粘贴的 {len(candidates)} 个网址，跳过寻客")
        else:
            source = "prospect"
            payload = {
                "country": country,
                "product_group": params.get("product_group", ""),
                "count": min(int(params.get("count") or self.ctx.settings.prospect_batch_size), remaining),
                "exclude_domains": exclude,
                "operator_note": params.get("note", ""),
            }
            found: CandidateList = await self.ctx.call_agent(run_id, AgentRole.PROSPECTOR, payload, country=country)
            candidates = found.candidates
            self.ctx.event(run_id, "step", f"寻客交回 {len(candidates)} 家候选", {"search_notes": found.search_notes})

        with self.ctx.db.session() as session:
            report = self.dedupe(session, candidates, source=source, default_country=country, limit=remaining)
        self.ctx.event(run_id, "step", report.text())
        if report.created:
            concurrency = self.ctx.settings.research_concurrency
            self.ctx.event(run_id, "step", f"开始调研 {len(report.created)} 家（最多同时 {concurrency} 家）")
        children = await self.research.run_many(report.created, parent_id=run_id)
        self.ctx.finish(run_id, RunStatus.SUCCEEDED, self._summary(len(report.created), children))

    def dedupe(
        self, session: Session, candidates: Iterable[Candidate], *, source: str, default_country: str, limit: int
    ) -> DedupeReport:
        """按可注册域名去重；剔除已有、拒绝联系、目录站和超出今日上限的候选，其余建成公司。"""
        report = DedupeReport()
        suppressed = set(session.execute(select(Suppression.domain)).scalars())
        seen: set[str] = set()
        for cand in candidates:
            domain = normalize_domain(cand.website)
            reason = self._drop_reason(session, domain, seen, suppressed, full=len(report.created) >= limit)
            if domain:
                seen.add(domain)
            if reason:
                report.dropped[reason] += 1
                continue
            assert domain is not None
            company = Company(
                name=cand.name.strip() or domain,
                domain=domain,
                website=cand.website.strip(),
                country=(cand.country or default_country).upper()[:2],
                source=source,
                prospect_evidence=cand.model_dump(),
                created_at=self.ctx.clock(),
                updated_at=self.ctx.clock(),
            )
            session.add(company)
            session.flush()
            report.created.append(company.id)
        return report

    def _drop_reason(
        self, session: Session, domain: str | None, seen: set[str], suppressed: set[str], *, full: bool
    ) -> str | None:
        if not domain:
            return "invalid"
        if domain in seen:
            return "duplicate"
        if domain in suppressed:
            return "suppressed"
        if self._is_directory(domain):
            return "directory"
        if session.scalar(select(Company.id).where(Company.domain == domain)) is not None:
            return "existing"
        if full:
            return "over_limit"
        return None

    def exclusion_domains(self, session: Session) -> list[str]:
        """交给寻客 agent 的排除清单：拒绝联系的 + 已处理过的（最近 500 家）。"""
        suppressed = session.execute(select(Suppression.domain)).scalars().all()
        known_stmt = select(Company.domain).order_by(Company.id.desc()).limit(EXCLUDE_LIST_LIMIT)
        known = session.execute(known_stmt).scalars().all()
        return sorted(set(suppressed) | set(known))

    def candidates_today(self, session: Session) -> int:
        stmt = select(func.count(Company.id)).where(
            Company.created_at >= self.ctx.day_start_utc(), Company.source.in_(["prospect", "url"])
        )
        return session.scalar(stmt) or 0

    def _is_directory(self, domain: str) -> bool:
        return any(domain == d or domain.endswith("." + d) for d in self.ctx.settings.directory_domains)

    def _summary(self, created: int, child_ids: list[int]) -> str:
        if not child_ids:
            return f"新增 {created} 家"
        with self.ctx.db.session() as session:
            rows = session.execute(
                select(Company.queue, Run.status).join(Run, Run.company_id == Company.id).where(Run.id.in_(child_ids))
            ).all()
        failed = sum(1 for _, status in rows if status is RunStatus.FAILED)
        counts = {q: sum(1 for queue, status in rows if queue is q and status is RunStatus.SUCCEEDED) for q in Queue}
        return (
            f"新增 {created} 家：可首触 {counts[Queue.FIRST_TOUCH]}、合格买家 {counts[Queue.QUALIFIED]}、"
            f"不合适 {counts[Queue.UNFIT]}、失败 {failed}"
        )


def _candidate_from_url(url: str, country: str) -> Candidate:
    return Candidate(
        name=normalize_domain(url) or url,
        website=url if "://" in url else f"https://{url}",
        country=country,
        city="未找到",
        company_type="未找到",
        criteria_met=["运营手动粘贴的网址"],
        sources=[url],
        why="运营手动粘贴",
    )
