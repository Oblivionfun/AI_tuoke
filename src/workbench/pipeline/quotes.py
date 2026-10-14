"""报价：一张报价单从询价走到成交。人填价格，生成报价单时存一份版本快照，PDF 按快照出。

价格和成本只由人填写：报价单里手填，或者用产品库里的参考价。AI 只做一件事——把客户的询价原文
拆成产品、尺寸和数量（拆询价 agent，在 worker 里跑），不碰价格。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from workbench.agents.prompts import quote_catalog
from workbench.agents.schemas import AgentRole, InquiryItem, InquiryParse
from workbench.catalog import DEMO_PRICE_NOTE
from workbench.normalize import country_name_en
from workbench.pipeline.context import Context
from workbench.store.models import (
    Company,
    Product,
    Quote,
    QuoteStatus,
    QuoteVersion,
    Run,
    RunKind,
    RunStatus,
    SkuVariant,
    Suppression,
)
from workbench.store.queries import is_suppressed

CENT = Decimal("0.01")
TERM_FIELDS = ("currency", "incoterm", "validity_days", "lead_time", "payment_terms", "packing", "remarks")
UNIT_FACTORS = {"pcs": Decimal(1), "1000 pcs": Decimal(1000)}
_SEPARATORS = re.compile(r"[,，\s]")


class QuoteAction(StrEnum):
    SAVE = "save"
    GENERATE = "generate"
    PARSE = "parse"
    WON = "won"
    LOST = "lost"
    DELETE = "delete"


class QuoteLine(BaseModel):
    """一行。单价按 unit 计，例如 unit 是 "1000 pcs" 时是每千件的价格；unit_cost 只用来算毛利，不打印。"""

    product_id: str | None = None
    description: str
    size: str = ""
    spec: str = ""
    quantity: Decimal | None = None
    unit: str = "pcs"
    unit_price: Decimal | None = None
    unit_cost: Decimal | None = None

    @property
    def specification(self) -> str:
        return ", ".join(part for part in (self.size, self.spec) if part)

    @property
    def amount(self) -> Decimal | None:
        if self.quantity is None or self.unit_price is None:
            return None
        return (self.quantity * self.unit_price).quantize(CENT, ROUND_HALF_UP)

    @property
    def cost(self) -> Decimal | None:
        if self.quantity is None or self.unit_cost is None:
            return None
        return self.quantity * self.unit_cost


@dataclass(frozen=True)
class Customer:
    name: str
    country: str = ""
    contact: str = ""
    company_id: int | None = None


@dataclass(frozen=True)
class QuoteTerms:
    currency: str
    incoterm: str
    validity_days: int
    lead_time: str
    payment_terms: str
    packing: str
    remarks: str


@dataclass(frozen=True)
class LastPrice:
    price: Decimal
    unit: str
    at: datetime


@dataclass(frozen=True)
class QuoteSheet:
    """算出来的数：合计、毛利、报价日期和有效期。编辑页、列表和 PDF 共用。

    毛利只按填了成本的行算；uncosted 是有金额但没成本的行数。
    """

    lines: list[QuoteLine]
    total: Decimal
    margin: float | None
    uncosted: int
    issued: date
    valid_until: date

    @classmethod
    def build(cls, items: list[Any], validity_days: int, issued: date) -> "QuoteSheet":
        lines = [QuoteLine.model_validate(item) for item in items]
        priced = [(line, amount) for line in lines if (amount := line.amount) is not None]
        costed = [(amount, cost) for line, amount in priced if (cost := line.cost) is not None]
        revenue = sum((amount for amount, _ in costed), Decimal(0))
        margin = float((revenue - sum((c for _, c in costed), Decimal(0))) / revenue) if revenue > 0 else None
        total = sum((amount for _, amount in priced), Decimal(0))
        return cls(lines, total, margin, len(priced) - len(costed), issued, issued + timedelta(days=validity_days))

    @classmethod
    def of(cls, quote: Quote, tz: str, now: datetime) -> "QuoteSheet":
        """正在编辑的明细，按今天生成计算有效期。"""
        return cls.build(quote.items, quote.validity_days, now.astimezone(ZoneInfo(tz)).date())

    @classmethod
    def of_version(cls, version: QuoteVersion, tz: str) -> "QuoteSheet":
        terms = version.snapshot["terms"]
        return cls.build(
            version.snapshot["items"], terms["validity_days"], version.created_at.astimezone(ZoneInfo(tz)).date()
        )


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def price_text(value: Decimal | None) -> str:
    """单价保留实际填写的位数，至少两位小数：0.0185、1.20。"""
    if value is None:
        return ""
    whole, _, frac = format(value.normalize(), "f").partition(".")
    return f"{int(whole):,}.{frac.ljust(2, '0')}"


def quantity_text(value: Decimal | None) -> str:
    return format(value.normalize(), ",f") if value is not None else ""


def plain(value: Decimal | None) -> str:
    """填回输入框的数字：不带千分位。"""
    return format(value.normalize(), "f") if value is not None else ""


def to_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(_SEPARATORS.sub("", str(value))) if value not in (None, "") else None
    except InvalidOperation:
        return None
    return number if number is not None and number.is_finite() and number >= 0 else None


def parse_number(text: str, label: str) -> Decimal | None:
    """ "20,000" → 20000；空着返回 None；不是数字或是负数时报错，提示是哪一项。"""
    if not _SEPARATORS.sub("", text or ""):
        return None
    number = to_decimal(text)
    if number is None:
        raise ValueError(f"{label}要填不小于 0 的数字：{text}")
    return number


def parse_lines(
    product_id: list[str],
    description: list[str],
    size: list[str],
    spec: list[str],
    quantity: list[str],
    unit: list[str],
    unit_price: list[str],
    unit_cost: list[str],
) -> list[QuoteLine]:
    """表单里按列提交的多行转成报价行；整行空着的跳过。"""
    lines = []
    rows = zip(product_id, description, size, spec, quantity, unit, unit_price, unit_cost, strict=True)
    for n, row in enumerate(rows, 1):
        pid, desc, sz, sp, qty, un, price, cost = (value.strip() for value in row)
        if not any((pid, desc, sz, qty, price)):
            continue
        lines.append(
            QuoteLine(
                product_id=pid or None,
                description=desc,
                size=sz,
                spec=sp,
                quantity=parse_number(qty, f"第 {n} 行数量"),
                unit=un or "pcs",
                unit_price=parse_number(price, f"第 {n} 行单价"),
                unit_cost=parse_number(cost, f"第 {n} 行成本"),
            )
        )
    return lines


def snapshot(quote: Quote) -> dict[str, Any]:
    customer = {"name": quote.customer_name, "country": quote.customer_country, "contact": quote.customer_contact}
    return {"customer": customer, "terms": {k: getattr(quote, k) for k in TERM_FIELDS}, "items": list(quote.items)}


def missing_for_quote(quote: Quote) -> list[str]:
    """生成报价单前必须有：客户名，至少一行，每行的产品、数量和单价。"""
    lines = [QuoteLine.model_validate(item) for item in quote.items]
    problems = [] if quote.customer_name.strip() else ["没写客户"]
    if not lines:
        problems.append("还没有产品")
    for n, line in enumerate(lines, 1):
        missing = [
            label
            for label, ok in (("产品", line.description), ("数量", line.quantity), ("单价", line.unit_price))
            if not ok
        ]
        if missing:
            problems.append(f"第 {n} 行没填{'、'.join(missing)}")
    return problems


def convert_quantity(quantity: Decimal | None, unit: str, to_unit: str) -> Decimal | None:
    """pcs 和 1000 pcs 之间换算数量；其他单位不换算，返回 None。"""
    if quantity is None or unit not in UNIT_FACTORS or to_unit not in UNIT_FACTORS:
        return None
    return (quantity * UNIT_FACTORS[unit] / UNIT_FACTORS[to_unit]).normalize()


def line_from_product(product: Product, last: LastPrice | None) -> QuoteLine:
    """产品库里的一个产品变成报价行：英文名、英文规格、计价单位、成本；单价用上次报给这个客户的价，没有就用参考价。"""
    variant = product.variants[0] if product.variants else None
    spec = product.spec_en or (
        ", ".join(filter(None, [variant.standard, variant.material, variant.finish])) if variant else ""
    )
    line = QuoteLine(
        product_id=product.id,
        description=product.name_en,
        spec=spec,
        unit=product.price_unit or "pcs",
        unit_price=to_decimal(product.ref_price),
        unit_cost=to_decimal(product.cost),
    )
    if last is not None:
        line = line.model_copy(update={"unit": last.unit, "unit_price": last.price})
    return line


def last_prices(
    session: Session, customer_name: str, company_id: int | None, *, exclude_quote_id: int | None = None
) -> dict[str, LastPrice]:
    """这个客户以前生成过的报价单里，每个产品最近一次的单价；exclude_quote_id 排除正在编辑的这张。"""
    conditions = []
    if company_id:
        conditions.append(Quote.company_id == company_id)
    if customer_name.strip():
        conditions.append(func.lower(Quote.customer_name) == customer_name.strip().lower())
    if not conditions:
        return {}
    stmt = select(QuoteVersion).join(Quote).where(or_(*conditions))
    if exclude_quote_id is not None:
        stmt = stmt.where(QuoteVersion.quote_id != exclude_quote_id)
    versions = session.execute(stmt.order_by(QuoteVersion.created_at.desc())).scalars()
    found: dict[str, LastPrice] = {}
    for version in versions:
        for item in version.snapshot.get("items", []):
            pid, price = item.get("product_id"), to_decimal(item.get("unit_price"))
            if pid and price is not None and pid not in found:
                found[pid] = LastPrice(price, item.get("unit") or "pcs", version.created_at)
    return found


class Quoting:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    # ---------- 报价单 ----------

    def new(self, *, company_id: int | None = None, product_id: str | None = None) -> Quote:
        """新报价单的初始内容（还没存库）：默认条款；从客户档案进来带上客户，从产品库进来带上一行。"""
        settings = self.ctx.settings
        quote = Quote(
            status=QuoteStatus.PENDING,
            customer_name="",
            customer_country="",
            customer_contact="",
            currency=settings.quote_currency,
            incoterm=settings.quote_incoterm,
            validity_days=settings.quote_validity_days,
            lead_time="",
            payment_terms=settings.quote_payment_terms,
            packing="",
            remarks="",
            items=[],
            inquiry="",
            lost_reason="",
        )
        with self.ctx.db.session() as session:
            company = session.get(Company, company_id) if company_id else None
            if company is not None:
                quote.company_id = company.id
                quote.customer_name = company.name
                quote.customer_country = country_name_en(company.country)
            product = self._product(session, product_id) if product_id else None
            if product is not None:
                last = last_prices(session, quote.customer_name, quote.company_id).get(product.id)
                quote.items = [line_from_product(product, last).model_dump(mode="json")]
        return quote

    def save(
        self,
        quote_id: int | None,
        customer: Customer,
        terms: QuoteTerms,
        lines: list[QuoteLine],
        *,
        inquiry: str | None = None,
        user: str = "",
    ) -> int:
        """新建或保存，返回报价单 id。任何状态都能改：已生成的版本是快照，不受影响。"""
        now = self.ctx.clock()
        with self.ctx.db.session() as session:
            if quote_id is None:
                quote = Quote(status=QuoteStatus.PENDING, created_by=user, created_at=now)
                session.add(quote)
            else:
                quote = self._get(session, quote_id)
            company = session.get(Company, customer.company_id) if customer.company_id else None
            if company is not None:
                self._check_contactable(session, company)
            quote.company_id = company.id if company is not None else None
            quote.customer_name = customer.name.strip()
            quote.customer_country = customer.country.strip()
            quote.customer_contact = customer.contact.strip()
            quote.currency = terms.currency.strip().upper()[:3] or self.ctx.settings.quote_currency
            quote.incoterm = terms.incoterm.strip()
            quote.validity_days = terms.validity_days
            quote.lead_time = terms.lead_time.strip()
            quote.payment_terms = terms.payment_terms.strip()
            quote.packing = terms.packing.strip()
            quote.remarks = terms.remarks.strip()
            quote.items = [line.model_dump(mode="json") for line in lines]
            if inquiry is not None:
                quote.inquiry = inquiry.strip()
            quote.updated_at = now
            session.flush()
            return quote.id

    def generate(self, quote_id: int, *, user: str = "") -> int:
        """生成报价单：内容和上一版一样就沿用上一版，否则存一个新版本。返回版本号。"""
        with self.ctx.db.session() as session:
            quote = self._get(session, quote_id)
            if quote.company is not None:
                self._check_contactable(session, quote.company)
            problems = missing_for_quote(quote)
            if problems:
                raise ValueError("还不能生成报价单：" + "；".join(problems))
            content = snapshot(quote)
            latest = quote.versions[0] if quote.versions else None
            if latest is not None and latest.snapshot == content:
                return latest.version
            now = self.ctx.clock()
            sheet = QuoteSheet.build(content["items"], quote.validity_days, now.date())
            version = QuoteVersion(
                quote=quote,
                version=latest.version + 1 if latest else 1,
                snapshot=content,
                total=str(sheet.total),
                created_by=user,
                created_at=now,
            )
            session.add(version)
            if quote.status in (QuoteStatus.PENDING, QuoteStatus.LOST):
                quote.status = QuoteStatus.QUOTED
                quote.lost_reason = ""
            return version.version

    def set_outcome(self, quote_id: int, *, won: bool, reason: str = "") -> None:
        with self.ctx.db.session() as session:
            quote = self._get(session, quote_id)
            quote.status = QuoteStatus.WON if won else QuoteStatus.LOST
            quote.lost_reason = "" if won else reason.strip()

    def delete(self, quote_id: int) -> None:
        with self.ctx.db.session() as session:
            quote = self._get(session, quote_id)
            if quote.versions:
                raise ValueError("已经生成过报价单，不能删除；不做了可以标为未成交")
            session.delete(quote)

    # ---------- 选产品、选客户 ----------

    def search_products(
        self, query: str, *, customer_name: str = "", company_id: int | None = None, limit: int = 8
    ) -> list[tuple[Product, QuoteLine, LastPrice | None]]:
        """按中英文名、编号、品类、标准号找产品；有参考价的排前面。每个结果附上带好默认值的报价行。"""
        stmt = select(Product).options(selectinload(Product.variants))
        if query.strip():
            like = f"%{query.strip()}%"
            stmt = stmt.where(
                or_(
                    Product.name_zh.ilike(like),
                    Product.name_en.ilike(like),
                    Product.id.ilike(like),
                    Product.category.ilike(like),
                    Product.spec_en.ilike(like),
                    Product.variants.any(SkuVariant.standard.ilike(like)),
                )
            )
        stmt = stmt.order_by(Product.ref_price == "", Product.sales_ready.desc(), Product.id).limit(limit)
        with self.ctx.db.session() as session:
            products = session.execute(stmt).scalars().all()
            prices = last_prices(session, customer_name, company_id)
        return [(p, line_from_product(p, prices.get(p.id)), prices.get(p.id)) for p in products]

    def customers(self, query: str, *, limit: int = 8) -> list[Customer]:
        """客户联想：AI 获客里的公司（不含拒绝联系的），和以前报过价的客户名。"""
        like = f"%{query.strip()}%"
        with self.ctx.db.session() as session:
            companies = session.execute(
                select(Company)
                .where(Company.name.ilike(like), Company.domain.not_in(select(Suppression.domain)))
                .order_by(Company.updated_at.desc())
                .limit(limit)
            ).scalars()
            found = [Customer(c.name, country_name_en(c.country), "", c.id) for c in companies]
            quoted = session.execute(
                select(Quote)
                .where(Quote.company_id.is_(None), Quote.customer_name.ilike(like), Quote.customer_name != "")
                .order_by(Quote.updated_at.desc())
                .limit(limit * 3)
            ).scalars()
            for quote in quoted:
                found.append(Customer(quote.customer_name, quote.customer_country, quote.customer_contact))
        unique: dict[str, Customer] = {}
        for customer in found:
            unique.setdefault(customer.name.casefold(), customer)
        return list(unique.values())[:limit]

    # ---------- 产品库的报价信息 ----------

    def set_pricing(self, product_id: str, *, spec_en: str, price_unit: str, ref_price: str, cost: str) -> None:
        with self.ctx.db.session() as session:
            product = self._product(session, product_id)
            if product is None:
                raise LookupError("产品不存在")
            self._apply_pricing(product, spec_en, price_unit, ref_price, cost)

    def add_product(
        self,
        *,
        category: str,
        name_zh: str,
        name_en: str,
        spec_en: str,
        price_unit: str,
        ref_price: str,
        cost: str,
    ) -> str:
        """工厂自己新增的产品（样册里没有的），编号 GW-M001 起。"""
        if not name_en.strip():
            raise ValueError("英文品名要填，报价单上印的是英文")
        now = self.ctx.clock()
        with self.ctx.db.session() as session:
            taken = session.execute(select(Product.id).where(Product.id.like("GW-M%"))).scalars().all()
            number = max((int(pid[4:]) for pid in taken if pid[4:].isdigit()), default=0) + 1
            product = Product(
                id=f"GW-M{number:03d}",
                category=category.strip() or "其他",
                name_en=name_en.strip(),
                name_zh=name_zh.strip(),
                created_at=now,
            )
            self._apply_pricing(product, spec_en, price_unit, ref_price, cost)
            session.add(product)
            return product.id

    @staticmethod
    def _apply_pricing(product: Product, spec_en: str, price_unit: str, ref_price: str, cost: str) -> None:
        ref, unit_cost = parse_number(ref_price, "参考价"), parse_number(cost, "成本")
        product.spec_en = spec_en.strip()
        product.price_unit = price_unit.strip() or "pcs"
        product.ref_price = str(ref) if ref is not None else ""
        product.cost = str(unit_cost) if unit_cost is not None else ""
        # 网页里改过价就去掉示例价标记，避免正式价还挂着「未经工厂确认」
        for variant in product.variants:
            if variant.note == DEMO_PRICE_NOTE:
                variant.note = ""

    # ---------- AI 拆询价（worker 执行） ----------

    def start_parse(self, quote_id: int, *, user: str = "") -> int:
        with self.ctx.db.session() as session:
            quote = self._get(session, quote_id)
            if not quote.inquiry.strip():
                raise ValueError("先把客户的询价原文粘贴进来")
            run = self.ctx.create_run(
                session, RunKind.PARSE, params={"quote_id": quote_id}, company_id=quote.company_id, created_by=user
            )
            return run.id

    def parse_run(self, quote_id: int) -> Run | None:
        """这张报价单最近一次拆询价的运行。"""
        with self.ctx.db.session() as session:
            return session.execute(
                select(Run)
                .where(Run.kind == RunKind.PARSE, func.json_extract(Run.params, "$.quote_id") == quote_id)
                .order_by(Run.id.desc())
                .limit(1)
            ).scalar_one_or_none()

    async def execute(self, run_id: int) -> None:
        await self.ctx.guarded(run_id, self._parse)

    async def _parse(self, run_id: int) -> None:
        with self.ctx.db.session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            quote_id = int(run.params["quote_id"])
            quote = self._get(session, quote_id)
            payload = {"inquiry": quote.inquiry, "customer": quote.customer_name}
            catalog = quote_catalog(session)
        result: InquiryParse = await self.ctx.call_agent(
            run_id, AgentRole.PARSER, payload, country=None, products=catalog
        )
        with self.ctx.db.session() as session:
            quote = self._get(session, quote_id)
            ids = [item.product_id for item in result.items if item.product_id]
            products = {
                p.id: p
                for p in session.execute(
                    select(Product).options(selectinload(Product.variants)).where(Product.id.in_(ids))
                ).scalars()
            }
            prices = last_prices(session, quote.customer_name, quote.company_id, exclude_quote_id=quote.id)
            lines = [_line_from_inquiry(item, products.get(item.product_id), prices) for item in result.items]
            kept = [item for item in quote.items if QuoteLine.model_validate(item).description.strip()]
            quote.items = kept + [line.model_dump(mode="json") for line in lines]
            if result.incoterm and quote.incoterm in ("", self.ctx.settings.quote_incoterm):
                quote.incoterm = result.incoterm
            run = session.get(Run, run_id)
            assert run is not None
            run.params = {**run.params, "lines": len(lines), "questions": result.questions_zh}
        questions = f"，{len(result.questions_zh)} 个问题要向客户确认" if result.questions_zh else ""
        self.ctx.finish(run_id, RunStatus.SUCCEEDED, f"拆出 {len(lines)} 行{questions}")

    # ---------- 内部 ----------

    @staticmethod
    def _get(session: Session, quote_id: int) -> Quote:
        quote = session.get(Quote, quote_id)
        if quote is None:
            raise LookupError("报价单不存在")
        return quote

    @staticmethod
    def _product(session: Session, product_id: str) -> Product | None:
        return session.execute(
            select(Product).options(selectinload(Product.variants)).where(Product.id == product_id)
        ).scalar_one_or_none()

    @staticmethod
    def _check_contactable(session: Session, company: Company) -> None:
        if is_suppressed(session, company.domain):
            raise ValueError("这家公司在拒绝联系名单里，不能报价")


def _line_from_inquiry(item: InquiryItem, product: Product | None, prices: dict[str, LastPrice]) -> QuoteLine:
    """拆出来的一行：对上了产品就带上产品的默认值，数量换算到产品的计价单位；单价只来自上次报价或参考价。"""
    quantity = to_decimal(item.quantity)
    if product is None:
        return QuoteLine(
            description=item.description, size=item.size, spec=item.spec, quantity=quantity, unit=item.unit
        )
    line = line_from_product(product, prices.get(product.id))
    converted = convert_quantity(quantity, item.unit, line.unit)
    if converted is None and item.unit != line.unit:
        return line.model_copy(
            update={
                "size": item.size,
                "spec": item.spec or line.spec,
                "quantity": quantity,
                "unit": item.unit,
                "unit_price": None,
            }
        )
    return line.model_copy(
        update={
            "size": item.size,
            "spec": item.spec or line.spec,
            "quantity": converted if converted is not None else quantity,
        }
    )
