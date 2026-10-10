"""运行：发起寻客、运行列表、单次运行（按公司分道显示实时步骤，SSE 推送）。"""

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from workbench.store.models import Run, RunEvent, RunStatus
from workbench.store.queries import company_names_by_run, events_after, run_tree_ids, tree_finished
from workbench.web.deps import current_user, flash, get_wb, redirect, render

router = APIRouter()
STAGES = [("researcher", "调研"), ("reviewer", "审查"), ("done", "结论")]


@dataclass
class Lane:
    """单次运行里的一家公司（或运行本身）：走到了哪一步、开了多少页面、全部步骤。"""

    run: Run
    title: str
    events: list[RunEvent] = field(default_factory=list)

    @property
    def stage(self) -> str:
        if self.run.status is RunStatus.SUCCEEDED:
            return "done"
        roles = {e.data.get("role") for e in self.events if e.kind == "agent"}
        return "reviewer" if "reviewer" in roles else "researcher" if roles else ""

    @property
    def stage_index(self) -> int:
        """已到达的阶段数：0 未开始，1 调研，2 审查，3 出结论。"""
        return {"": 0, "researcher": 1, "reviewer": 2, "done": 3}[self.stage]

    @property
    def opened(self) -> int:
        return sum(1 for e in self.events if e.kind == "open")

    @property
    def searches(self) -> int:
        return sum(1 for e in self.events if e.kind == "search")


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    wb = get_wb(request)
    with wb.db.session() as s:
        rows = (
            s.execute(
                select(Run)
                .options(selectinload(Run.company))
                .where(Run.parent_id.is_(None))
                .order_by(Run.id.desc())
                .limit(60)
            )
            .scalars()
            .all()
        )
        children = defaultdict(lambda: {"total": 0, "done": 0, "cost": 0.0})
        for parent_id, status, n, cost in s.execute(
            select(Run.parent_id, Run.status, func.count(Run.id), func.sum(Run.cost_usd))
            .where(Run.parent_id.is_not(None))
            .group_by(Run.parent_id, Run.status)
        ):
            children[parent_id]["total"] += n
            children[parent_id]["cost"] += cost or 0
            if status in (RunStatus.SUCCEEDED, RunStatus.FAILED):
                children[parent_id]["done"] += n
    live = any(r.status in (RunStatus.QUEUED, RunStatus.RUNNING) for r in rows)
    return render(request, "pages/runs.html", nav="runs", rows=rows, children=children, live=live)


@router.post("/runs/prospect")
def start_prospect(
    request: Request,
    country: str = Form(...),
    product_group: str = Form(""),
    count: str = Form(""),
    urls: str = Form(""),
    note: str = Form(""),
    user: str = Depends(current_user),
) -> Response:
    url_list = [u.strip() for u in urls.replace(",", "\n").splitlines() if u.strip()]
    run_id = get_wb(request).prospect.start(
        country=country.strip().upper()[:2],
        product_group=product_group.strip(),
        urls=url_list,
        count=int(count) if count.strip().isdigit() else None,
        note=note.strip(),
        user=user,
    )
    return redirect(f"/runs/{run_id}")


@router.post("/runs/research")
def start_research(
    request: Request, company_id: int = Form(...), note: str = Form(""), user: str = Depends(current_user)
) -> Response:
    try:
        run_id = get_wb(request).research.start(company_id, note=note.strip(), user=user)
    except ValueError as exc:
        flash(request, str(exc), error=True)
        return redirect(f"/companies/{company_id}")
    return redirect(f"/runs/{run_id}")


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int, user: str = Depends(current_user)) -> HTMLResponse:
    wb = get_wb(request)
    with wb.db.session() as s:
        run = s.execute(select(Run).options(selectinload(Run.company)).where(Run.id == run_id)).scalar_one_or_none()
        if run is None:
            raise HTTPException(404, "运行不存在")
        children = (
            s.execute(select(Run).options(selectinload(Run.company)).where(Run.parent_id == run_id).order_by(Run.id))
            .scalars()
            .all()
        )
        ids = run_tree_ids(s, run_id)
        events = events_after(s, ids, 0)
        finished = tree_finished(s, ids)
    lanes = {c.id: Lane(c, c.company.name if c.company else f"#{c.id}") for c in children}
    if not lanes:
        lanes[run.id] = Lane(run, run.company.name if run.company else "本次运行")
    own_steps = []
    for event in events:
        if event.run_id in lanes:
            lanes[event.run_id].events.append(event)
        else:
            own_steps.append(event)
    all_runs = [run, *children]
    return render(
        request,
        "pages/run.html",
        nav="runs",
        run=run,
        lanes=list(lanes.values()),
        own_steps=own_steps,
        finished=finished,
        last_event_id=events[-1].id if events else 0,
        cost=sum(r.cost_usd for r in all_runs),
        searches=sum(int(r.usage.get("web_search_calls", 0)) for r in all_runs),
        stages=STAGES,
    )


@router.get("/runs/{run_id}/events")
async def run_events(
    request: Request, run_id: int, after: int = 0, user: str = Depends(current_user)
) -> StreamingResponse:
    """SSE：每秒查一次新步骤，推给运行页；整棵运行树结束后发 done。"""
    wb = get_wb(request)
    tz = ZoneInfo(wb.settings.timezone)
    start = int(request.headers.get("last-event-id") or after or 0)

    async def stream():
        last = start
        while not await request.is_disconnected():
            with wb.db.session() as s:
                ids = run_tree_ids(s, run_id)
                names = company_names_by_run(s, ids)
                events = events_after(s, ids, last)
                finished = tree_finished(s, ids)
            for e in events:
                last = e.id
                payload = {
                    "id": e.id,
                    "run": e.run_id,
                    "kind": e.kind,
                    "text": e.text,
                    "who": names.get(e.run_id, ""),
                    "at": e.created_at.astimezone(tz).strftime("%H:%M:%S"),
                }
                yield f"id: {e.id}\nevent: step\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if finished and not events:
                yield "event: done\ndata: {}\n\n"
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
