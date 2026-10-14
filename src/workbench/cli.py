"""workbench 命令行。日常命令在顶层；样册、演示数据、评测各自是一个子命令组。"""

import asyncio
from pathlib import Path

import typer
import uvicorn
import yaml
from sqlalchemy import func, select

from workbench.agents.prompts import PromptLibrary, products_summary
from workbench.agents.runner import SdkRunner, live_check
from workbench.agents.schemas import AgentRole
from workbench.catalog import apply_demo_prices, clear_demo_prices, confirm_sku, import_catalog
from workbench.config import get_settings
from workbench.demo import seed_demo, validate_demo_file
from workbench.evals import (
    build_golden,
    compare_baseline,
    compare_reports,
    emails_in,
    load_golden,
    render_baseline,
    run_golden,
    write_report,
)
from workbench.pipeline import Workbench
from workbench.sanctions import download_csl
from workbench.store.db import Database
from workbench.store.models import Company, Product, Queue, Run, utcnow
from workbench.store.users import upsert_user

app = typer.Typer(help="冠维获客工作台", no_args_is_help=True, add_completion=False)
catalog_app = typer.Typer(help="样册与首推 SKU", no_args_is_help=True)
demo_app = typer.Typer(help="演示数据", no_args_is_help=True)
eval_app = typer.Typer(help="评测集：改提示词前后各跑一次", no_args_is_help=True)
app.add_typer(catalog_app, name="catalog")
app.add_typer(demo_app, name="demo")
app.add_typer(eval_app, name="eval")

CATALOG_PATH = Path("seed/guanwei_catalog.sqlite")
DEMO_DIR = Path("seed/demo")


def _db() -> Database:
    db = Database(get_settings().database_url)
    db.migrate()
    return db


def _wb() -> Workbench:
    settings = get_settings()
    return Workbench(_db(), settings, SdkRunner(settings))


def _require_api_key() -> None:
    if not get_settings().api_key:
        raise typer.BadParameter("没有 API key（WB_LLM_API_KEY 或 OPENAI_API_KEY）")


# ---------- 日常 ----------


@app.command()
def init(
    catalog: Path = typer.Option(CATALOG_PATH, help="冠维样册数据库"),
    admin: str = typer.Option("", help="同时创建的管理员账号"),
) -> None:
    """建表（迁移到最新）、导入样册、可选创建账号。重复执行是安全的。"""
    db = _db()
    typer.echo(f"数据库已就绪：{db.url}")
    if catalog.exists():
        _import_catalog(db, catalog)
    else:
        typer.echo(f"没找到样册数据库 {catalog}，跳过导入")
    if admin:
        _ensure_user(db, admin, "")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """启动网页。"""
    _db()
    uvicorn.run(
        "workbench.web.app:create_app", factory=True, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*"
    )


@app.command()
def worker(slots: int = typer.Option(3, help="同时执行的顶层任务数")) -> None:
    """后台 worker：执行网页上排队的寻客、调研、文案任务。"""
    wb = _wb()
    typer.echo(f"worker 启动：模型 {wb.settings.model}，调研并发 {wb.settings.research_concurrency}")
    asyncio.run(wb.worker.loop(slots=slots))


@app.command()
def prospect(
    country: str = typer.Option(..., help="AE / ZA / SA"),
    group: str = typer.Option("", help="产品组，例如 自钻螺钉"),
    count: int = typer.Option(0, help="本次最多几家，0 表示用默认值"),
    url: list[str] = typer.Option([], help="直接给官网网址（可多次），跳过寻客"),
    note: str = "",
    now: bool = typer.Option(False, "--now", help="在当前进程直接执行，不等 worker"),
) -> None:
    """发起一次寻客（寻客 → 去重 → 并行调研 → 审查）。"""
    wb = _wb()
    run_id = wb.prospect.start(
        country=country, product_group=group, urls=url, count=count or None, note=note, user="cli"
    )
    _queued_or_run(wb, run_id, now)


@app.command()
def research(company_id: int, note: str = "", now: bool = typer.Option(False, "--now")) -> None:
    """对一家公司重新调研 + 审查。"""
    wb = _wb()
    _queued_or_run(wb, wb.research.start(company_id, note=note, user="cli"), now)


@app.command("user-add")
def user_add(username: str, password: str = typer.Option("", help="不填则交互输入")) -> None:
    """创建账号或重设密码。"""
    _ensure_user(_db(), username, password)


@app.command()
def doctor(live: bool = typer.Option(False, help="真实调用一次模型和联网搜索")) -> None:
    """体检：数据库、提示词、知识包、API key，可选真实调用一次。"""
    settings = get_settings()
    db = _db()
    with db.session() as s:
        n_products = s.scalar(select(func.count(Product.id))) or 0
        n_confirmed = s.scalar(select(func.count(Product.id)).where(Product.sales_ready.is_(True))) or 0
        n_companies = s.scalar(select(func.count(Company.id))) or 0
        products = products_summary(s)
    typer.echo(f"数据库：{db.url}（产品 {n_products}，已确认 SKU {n_confirmed}，公司 {n_companies}）")
    ok = True
    library = PromptLibrary(settings)
    for role in AgentRole:
        try:
            bundle = library.build(role, country="AE", products=products)
            typer.echo(f"提示词 {role.value}：{bundle.version}，{len(bundle.text)} 字")
        except FileNotFoundError as exc:
            ok = False
            typer.echo(f"提示词 {role.value} 缺失：{exc}")
    typer.echo(
        f"模型：{settings.model}（推理档 {settings.reasoning_effort}），网关：{settings.llm_base_url or 'OpenAI 官方'}"
    )
    typer.echo(f"制裁名单：{'已下载' if settings.csl_path.exists() else '未下载（workbench sanctions-update）'}")
    if not settings.api_key:
        ok = False
        typer.echo("没有 API key（WB_LLM_API_KEY 或 OPENAI_API_KEY）：agent 无法运行")
    elif live:
        try:
            typer.echo(f"真实调用成功：{asyncio.run(live_check(settings))[:120]}")
        except Exception as exc:
            ok = False
            typer.echo(f"真实调用失败：{exc}")
    if n_confirmed == 0:
        typer.echo("提醒：工厂还没确认首推 SKU，文案只能写产品名")
    raise typer.Exit(0 if ok else 1)


@app.command("sanctions-update")
def sanctions_update() -> None:
    """下载美国综合筛查清单（CSL），用于制裁名称比对提示。"""
    typer.echo(f"已下载到 {download_csl(get_settings().csl_path)}")


@app.command()
def backup(dest: Path = typer.Option(Path("data/backups")), keep: int = typer.Option(14, help="保留最近几份")) -> None:
    """在线备份数据库（服务运行中也可以执行）。"""
    typer.echo(f"已备份到 {_db().backup(dest, keep)}")


# ---------- 样册 ----------


@catalog_app.command("import")
def catalog_import(path: Path = typer.Argument(CATALOG_PATH)) -> None:
    """导入（或更新）样册数据库里的产品卡和候选公司。"""
    _import_catalog(_db(), path)


@catalog_app.command("confirm")
def catalog_confirm(
    product_id: str,
    standard: str = "",
    material: str = "",
    sizes: str = "",
    finish: str = "",
    moq: str = "",
    lead_time: str = "",
    note: str = "",
    by: str = typer.Option("工厂", help="确认人"),
) -> None:
    """工厂确认首推 SKU 的规格（也可以在网页"产品库"里操作）。"""
    with _db().session() as s:
        confirm_sku(
            s,
            product_id,
            standard=standard,
            material=material,
            sizes=sizes,
            finish=finish,
            moq=moq,
            lead_time=lead_time,
            note=note,
            user=by,
            now=utcnow(),
        )
    typer.echo(f"{product_id} 已确认")


@catalog_app.command("demo-prices")
def catalog_demo_prices() -> None:
    """给建议优先的 10 个常卖 SKU 填示例价（会醒目标注「示例价，未经工厂确认」）。"""
    with _db().session() as s:
        updated = apply_demo_prices(s)
    typer.echo(f"已写入示例价 {len(updated)} 款：{', '.join(updated) or '（产品库里还没有这些编号）'}")


@catalog_app.command("clear-demo-prices")
def catalog_clear_demo_prices() -> None:
    """清掉仍标着示例价的产品报价信息；网页里已改成工厂价的不会动。"""
    with _db().session() as s:
        cleared = clear_demo_prices(s)
    typer.echo(f"已清除示例价 {len(cleared)} 款：{', '.join(cleared) or '（没有仍标示例价的产品）'}")


# ---------- 演示数据 ----------


@demo_app.command("seed")
def demo_seed(directory: Path = typer.Option(DEMO_DIR)) -> None:
    """载入演示数据（研究子代理按同一契约联网完成的真实客户档案）。重复执行是安全的。"""
    report = seed_demo(_wb(), directory)
    typer.echo(
        f"载入 {report.files} 个文件：新增公司 {report.companies}、更新 {report.updated}，"
        f"运行记录 {len(report.runs)} 条，演示草稿 {report.drafts} 封"
    )


@demo_app.command("validate")
def demo_validate(files: list[Path] = typer.Argument(None)) -> None:
    """校验演示档案的格式（字段、域名重复、总分等于分项之和）。"""
    failed = False
    for path in files or sorted(DEMO_DIR.glob("dossiers_*.json")):
        problems = validate_demo_file(path)
        typer.echo(f"{path}: {'OK' if not problems else f'{len(problems)} 个问题'}")
        for problem in problems:
            typer.echo(f"  - {problem}")
        failed = failed or bool(problems)
    raise typer.Exit(1 if failed else 0)


# ---------- 评测 ----------


@eval_app.command("run")
def eval_run(
    golden: Path = typer.Option(Path("eval/golden.yaml")),
    out: Path = typer.Option(Path("eval/reports")),
    limit: int = typer.Option(0, help="只跑前 N 家，0 表示全部"),
) -> None:
    """在临时数据库里跑评测集（调研 + 审查 + 评委打分），生成报告。不影响正式数据。"""
    _require_api_key()
    settings = get_settings()
    results, versions = asyncio.run(run_golden(load_golden(golden), settings, SdkRunner(settings), limit=limit or None))
    path = write_report(out, results, versions, settings)
    typer.echo(path.with_suffix(".md").read_text(encoding="utf-8"))
    typer.echo(f"报告：{path}")


@eval_app.command("compare")
def eval_compare(before: Path, after: Path) -> None:
    """对比两次评测报告（改提示词前 / 后）。"""
    typer.echo(compare_reports(before, after))


@eval_app.command("golden")
def eval_golden(
    demo_dir: Path = typer.Option(DEMO_DIR),
    negatives: Path = typer.Option(Path("eval/negatives.yaml")),
    out: Path = typer.Option(Path("eval/golden.yaml")),
) -> None:
    """从演示档案和反例清单重新生成评测集，保留已人工核对（verified=true）的标注。"""
    cases = build_golden(demo_dir, negatives, out)
    unfit = sum(c["label"]["expected_queue"] == "unfit" for c in cases)
    typer.echo(f"{out}：{len(cases)} 家（反例 {unfit}）")


@eval_app.command("baseline")
def eval_baseline(
    baseline: Path = typer.Argument(Path("eval/llm_memory_baseline.yaml")),
    out: Path = typer.Option(Path("eval/reports/baseline-compare.md")),
) -> None:
    """检查"直接问聊天机器人"的名单（网站能否打开、邮箱能否在官网找到），与工作台卡片并排对比。"""
    data = yaml.safe_load(baseline.read_text(encoding="utf-8"))
    with _db().session() as s:
        companies = s.execute(select(Company)).scalars().all()
    researched = [c for c in companies if c.dossier]
    stats = {
        "companies": len(researched),
        "site_ok": f"{len(researched)}（调研时逐家打开）",
        "email_sourced": f"{sum(1 for c in researched if emails_in(c.dossier))} 家有邮箱，每个都附出处页面",
        "unfit": sum(1 for c in researched if c.queue is Queue.UNFIT),
    }
    checks = asyncio.run(compare_baseline(data.get("items", []), {c.domain for c in companies}))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_baseline(data.get("source", str(baseline)), checks, stats), encoding="utf-8")
    typer.echo(out.read_text(encoding="utf-8"))


# ---------- 内部 ----------


def _import_catalog(db: Database, path: Path) -> None:
    with db.session() as s:
        r = import_catalog(s, path, utcnow())
    typer.echo(f"样册导入：新增产品 {r.products_added}、更新 {r.products_updated}；新增候选公司 {r.companies_added}")


def _ensure_user(db: Database, username: str, password: str) -> None:
    if not password:
        password = typer.prompt("密码", hide_input=True, confirmation_prompt=True)
    if len(password) < 8:
        raise typer.BadParameter("密码至少 8 位")
    with db.session() as s:
        upsert_user(s, username, password)
    typer.echo(f"账号 {username} 已就绪")


def _queued_or_run(wb: Workbench, run_id: int, now: bool) -> None:
    typer.echo(f"已排队：运行 #{run_id}")
    if now and wb.worker.claim_next() == run_id:
        asyncio.run(_execute_with_log(wb, run_id))


async def _execute_with_log(wb: Workbench, run_id: int) -> None:
    """在终端里边跑边打印步骤。"""
    record = wb.ctx.event

    def echo(run: int, kind: str, text: str, data: dict | None = None) -> None:
        record(run, kind, text, data)
        typer.echo(f"  #{run} {text}")

    wb.ctx.event = echo  # type: ignore[method-assign]
    await wb.worker.execute(run_id)
    with wb.db.session() as s:
        run = s.get(Run, run_id)
        assert run is not None
        typer.echo(f"结束：{run.status.value} {run.summary or run.error}")
