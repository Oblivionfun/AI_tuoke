"""提示词与分层知识包。

系统提示的拼接顺序固定，越稳定的越靠前，方便命中提示缓存：
角色契约 → 核心摘要（我方、目标客户、评分口径、来源规则）→ 目录站清单 → 我方产品清单 → 写作规范（仅文案）→ 目标国家说明。
每家公司的具体资料放在用户输入里，不进系统提示。
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from workbench.agents.schemas import AgentRole
from workbench.config import WorkbenchSettings
from workbench.store.models import Product

_VERSION_RE = re.compile(r"<!--\s*version:\s*([\w.\-]+)\s*-->")
CATEGORY_ORDER = ["螺钉", "铆钉", "螺母", "垫圈", "锚固件", "螺栓", "钉类"]


@dataclass(frozen=True)
class PromptBundle:
    text: str
    version: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def products_summary(session: Session) -> str:
    products = (
        session.execute(select(Product).options(selectinload(Product.variants)).order_by(Product.id)).scalars().all()
    )
    confirmed = [p for p in products if p.sales_ready and any(v.confirmed_at for v in p.variants)]
    lines = ["# 我方产品清单", "", "## 已确认 SKU（文案只能引用这里写明的规格）", ""]
    if confirmed:
        for p in confirmed:
            for v in p.variants:
                if not v.confirmed_at:
                    continue
                spec = "；".join(
                    f"{label}：{value}"
                    for label, value in [
                        ("标准", v.standard),
                        ("材质", v.material),
                        ("规格", v.sizes),
                        ("表面", v.finish),
                        ("起订量", v.moq),
                        ("交期", v.lead_time),
                        ("备注", v.note),
                    ]
                    if value
                )
                lines.append(f"- [{p.id}] {p.name_en} / {p.name_zh}：{spec or '规格待补'}")
    else:
        lines.append("暂无。工厂尚未确认首推 SKU：产品匹配的 match_level 只能写 family，文案只写产品名，不写规格。")
    lines += ["", f"## 全部产品卡（来自样册，共 {len(products)} 张，规格未确认）", ""]
    by_category: dict[str, list[Product]] = {}
    for p in products:
        by_category.setdefault(p.category, []).append(p)
    for category in sorted(by_category, key=lambda c: (CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99, c)):
        names = "; ".join(f"{p.name_en}/{p.name_zh}" for p in by_category[category])
        lines.append(f"- {category}：{names}")
    return "\n".join(lines)


def quote_catalog(session: Session) -> str:
    """拆询价用的产品清单：每行一个 id，方便把客户写的产品对到我方产品。"""
    products = (
        session.execute(select(Product).options(selectinload(Product.variants)).order_by(Product.id)).scalars().all()
    )
    lines = ["# 我方产品清单（id | 中文名 | 英文名 | 品类 | 规格）", ""]
    for p in products:
        variant = p.variants[0] if p.variants else None
        spec = p.spec_en or (" ".join(filter(None, [variant.standard, variant.sizes])) if variant else "")
        lines.append(f"- {p.id} | {p.name_zh} | {p.name_en} | {p.category} | {spec}")
    return "\n".join(lines)


class PromptLibrary:
    def __init__(self, settings: WorkbenchSettings):
        self.settings = settings
        self.prompts_dir = settings.prompts_dir
        self.knowledge_dir = settings.knowledge_dir

    def contract(self, role: AgentRole) -> tuple[str, str]:
        text = _read(self.prompts_dir / f"{role.value}.md")
        match = _VERSION_RE.search(text)
        return text, match.group(1) if match else f"{role.value}-unversioned"

    def core(self) -> str:
        files = sorted((self.knowledge_dir / "core").glob("*.md"))
        return "\n\n".join(_read(f) for f in files)

    def directory_list(self) -> str:
        return "# 目录站和平台清单（不能当公司官网，也不能作为身份的唯一来源）\n\n" + "、".join(
            self.settings.directory_domains
        )

    def market(self, country: str | None) -> str:
        if not country:
            return ""
        path = self.knowledge_dir / "markets" / f"{country.upper()}.md"
        if path.exists():
            return _read(path)
        return f"# 目标国家：{country.upper()}\n\n知识包里还没有这个国家的说明。按最保守的方式处理：首封只征求同意，并在 risks 里提示'该国规则未整理'。"

    def build(self, role: AgentRole, *, country: str | None, products: str) -> PromptBundle:
        contract, contract_version = self.contract(role)
        if role is AgentRole.PARSER:
            parts = [contract, products]
        else:
            parts = [contract, "# 知识包：核心摘要", self.core(), self.directory_list(), products]
            if role is AgentRole.WRITER:
                parts.append(_read(self.knowledge_dir / "writing.md"))
            market = self.market(country)
            if market:
                parts.append(market)
        text = "\n\n---\n\n".join(p for p in parts if p)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        return PromptBundle(text=text, version=f"{contract_version}+{digest}")
