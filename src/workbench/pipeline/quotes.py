"""报价单：从询价开一张报价，人填价格，业务员自己发给客户后回来登记；改价出新版本。

价格、成本和交期都由人填写，这里没有 agent 调用。系统也不发报价：打印页存成 PDF，由业务员从 WhatsApp 或邮箱发出。
发出过的版本不再修改，每一版都留着，可以追溯当时报了什么价。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy.orm import Session

from workbench.pipeline.context import Context
from workbench.store.models import Channel, Company, Direction, Interaction, Quote, QuoteStatus, Rfq, RfqStatus
from workbench.store.queries import is_suppressed

CENT = Decimal("0.01")
_SEPARATORS = re.compile(r"[,，\s]")
_QUANTITY = re.compile(r"(\d[\d,，.]*)\s*(k|千|万|w)?", re.IGNORECASE)
_MULTIPLIERS = {"k": 1000, "千": 1000, "万": 10000, "w": 10000}


class QuoteAction(StrEnum):
    SAVE = "save"
    MARK_SENT = "mark_sent"
    ACCEPT = "accept"
    REJECT = "reject"
    REVISE = "revise"
    DISCARD = "discard"


class QuoteLine(BaseModel):
    """报价单的一行。单价按 unit 计，例如 unit 是 "1000 pcs" 时填每千件的价格。"""

    description: str
    spec: str = ""
    quantity: Decimal | None = None
    unit: str = "pcs"
    unit_price: Decimal | None = None
    unit_cost: Decimal | None = None

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
class QuoteTerms:
    currency: str
    incoterm: str
    validity_days: int
    lead_time: str
    payment_terms: str
    packing: str
    remarks: str


@dataclass(frozen=True)
class QuoteSheet:
    """报价单算出来的数：合计、毛利、报价日期和有效期。编辑页和打印页共用。"""

    lines: list[QuoteLine]
    total: Decimal
    margin: float | None
    issued: date
    valid_until: date
    expired: bool

    @classmethod
    def of(cls, quote: Quote, tz: str, now: datetime) -> "QuoteSheet":
        """毛利只在每一行都填了成本时才算；草稿按今天发出计算有效期。"""
        zone = ZoneInfo(tz)
        lines = [QuoteLine.model_validate(item) for item in quote.items]
        amounts = [a for line in lines if (a := line.amount) is not None]
        total = sum(amounts, Decimal(0))
        costs = [line.cost for line in lines if line.amount is not None]
        margin = None
        if amounts and total > 0 and all(c is not None for c in costs):
            margin = float((total - sum(c for c in costs if c is not None)) / total)
        issued = (quote.sent_at or now).astimezone(zone).date()
        valid_until = issued + timedelta(days=quote.validity_days)
        expired = quote.status is QuoteStatus.SENT and now.astimezone(zone).date() > valid_until
        return cls(lines, total, margin, issued, valid_until, expired)


def parse_number(text: str, label: str) -> Decimal | None:
    """ "20,000" → 20000；空着返回 None。"""
    cleaned = _SEPARATORS.sub("", text or "")
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{label}不是数字：{text}") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"{label}不能是负数：{text}")
    return value


def parse_lines(
    description: list[str],
    spec: list[str],
    quantity: list[str],
    unit: list[str],
    unit_price: list[str],
    unit_cost: list[str],
) -> list[QuoteLine]:
    """表单里按列提交的多行转成报价行；整行空着的跳过。"""
    lines = []
    rows = zip(description, spec, quantity, unit, unit_price, unit_cost, strict=True)
    for n, row in enumerate(rows, 1):
        desc, sp, qty, un, price, cost = (value.strip() for value in row)
        if not any((desc, sp, qty, price, cost)):
            continue
        lines.append(
            QuoteLine(
                description=desc,
                spec=sp,
                quantity=parse_number(qty, f"第 {n} 行数量"),
                unit=un or "pcs",
                unit_price=parse_number(price, f"第 {n} 行单价"),
                unit_cost=parse_number(cost, f"第 {n} 行成本"),
            )
        )
    return lines


def guess_quantity(text: str) -> Decimal | None:
    """从询价记录的数量里取数字预填第一行："200k" → 200000，"5万" → 50000。有多个数字（多种产品）时不猜。"""
    matches = _QUANTITY.findall(text or "")
    if len(matches) != 1:
        return None
    number, suffix = matches[0]
    try:
        value = Decimal(_SEPARATORS.sub("", number))
    except InvalidOperation:
        return None
    return value * _MULTIPLIERS.get(suffix.lower(), 1)


def missing_for_send(quote: Quote) -> list[str]:
    """发出前必须填好的项：每行的产品、数量、单价，以及贸易术语和交期。"""
    lines = [QuoteLine.model_validate(item) for item in quote.items]
    problems = [] if lines else ["至少要有一行产品"]
    for n, line in enumerate(lines, 1):
        if not line.description:
            problems.append(f"第 {n} 行没写产品")
        if not line.quantity:
            problems.append(f"第 {n} 行没写数量")
        if not line.unit_price:
            problems.append(f"第 {n} 行没写单价")
    if not quote.incoterm.strip():
        problems.append("没写贸易术语")
    if not quote.lead_time.strip():
        problems.append("没写交期（请先和工厂确认）")
    return problems


class Quoting:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def open_for(self, rfq_id: int, *, user: str = "") -> int:
        """询价的报价入口：已经有报价就打开最新一版，没有就按询价内容开第一版草稿。"""
        settings = self.ctx.settings
        with self.ctx.db.session() as session:
            rfq = session.get(Rfq, rfq_id)
            if rfq is None:
                raise LookupError("询价不存在")
            if rfq.quotes:
                return rfq.quotes[0].id
            self._check_contactable(session, rfq.company)
            now = self.ctx.clock()
            first_line = QuoteLine(description=rfq.products, quantity=guess_quantity(rfq.quantity))
            quote = Quote(
                rfq_id=rfq.id,
                company_id=rfq.company_id,
                version=1,
                currency=settings.quote_currency,
                incoterm=rfq.incoterm or settings.quote_incoterm,
                validity_days=settings.quote_validity_days,
                payment_terms=settings.quote_payment_terms,
                items=[first_line.model_dump(mode="json")],
                created_by=user,
                created_at=now,
                updated_at=now,
            )
            session.add(quote)
            session.flush()
            return quote.id

    def save(self, quote_id: int, terms: QuoteTerms, lines: list[QuoteLine]) -> None:
        with self.ctx.db.session() as session:
            quote = self._get(session, quote_id)
            if quote.status is not QuoteStatus.DRAFT:
                raise ValueError("发出过的报价不能再改，请改价出新版本")
            quote.currency = terms.currency.strip().upper()[:3] or self.ctx.settings.quote_currency
            quote.incoterm = terms.incoterm.strip()
            quote.validity_days = terms.validity_days
            quote.lead_time = terms.lead_time.strip()
            quote.payment_terms = terms.payment_terms.strip()
            quote.packing = terms.packing.strip()
            quote.remarks = terms.remarks.strip()
            quote.items = [line.model_dump(mode="json") for line in lines]
            quote.updated_at = self.ctx.clock()

    def mark_sent(self, quote_id: int, *, channel: Channel, user: str = "") -> None:
        """业务员把报价发给客户后登记：这一版锁定，之前发出的版本标为已替代，询价改为已报价，记一笔联系。"""
        with self.ctx.db.session() as session:
            quote = self._get(session, quote_id)
            if quote.status is not QuoteStatus.DRAFT:
                raise ValueError("这一版已经登记过了")
            self._check_contactable(session, quote.company)
            problems = missing_for_send(quote)
            if problems:
                raise ValueError("还不能登记发出：" + "；".join(problems))
            now = self.ctx.clock()
            for other in quote.rfq.quotes:
                if other.id != quote.id and other.status is QuoteStatus.SENT:
                    other.status = QuoteStatus.SUPERSEDED
            quote.status = QuoteStatus.SENT
            quote.sent_at = now
            quote.sent_by = user
            if quote.rfq.status in (RfqStatus.OPEN, RfqStatus.LOST):
                quote.rfq.status = RfqStatus.QUOTED
            sheet = QuoteSheet.of(quote, self.ctx.settings.timezone, now)
            session.add(
                Interaction(
                    company_id=quote.company_id,
                    channel=channel,
                    direction=Direction.OUT,
                    summary=(
                        f"已发出报价 {quote.number}：{quote.currency} {sheet.total:,.2f}，{quote.incoterm}，"
                        f"有效期至 {sheet.valid_until:%Y-%m-%d}"
                    ),
                    created_by=user,
                    happened_at=now,
                )
            )

    def record_outcome(self, quote_id: int, *, accepted: bool, note: str = "") -> None:
        """对方接受，询价记为成交；不接受要写原因，询价记为未成交（之后仍可改价出新版本）。"""
        with self.ctx.db.session() as session:
            quote = self._get(session, quote_id)
            if quote.status is not QuoteStatus.SENT:
                raise ValueError("只有已发出、还在有效的这一版可以记录结果")
            if not accepted and not note.strip():
                raise ValueError("请写一下对方不接受的原因")
            quote.status = QuoteStatus.ACCEPTED if accepted else QuoteStatus.REJECTED
            quote.outcome_note = note.strip()
            quote.rfq.status = RfqStatus.WON if accepted else RfqStatus.LOST

    def revise(self, quote_id: int, *, user: str = "") -> int:
        """在这一版的基础上出新版本草稿；同一询价已有草稿时直接返回那份草稿。"""
        with self.ctx.db.session() as session:
            quote = self._get(session, quote_id)
            existing = next((q for q in quote.rfq.quotes if q.status is QuoteStatus.DRAFT), None)
            if existing is not None:
                return existing.id
            self._check_contactable(session, quote.company)
            now = self.ctx.clock()
            draft = Quote(
                rfq_id=quote.rfq_id,
                company_id=quote.company_id,
                version=max(q.version for q in quote.rfq.quotes) + 1,
                currency=quote.currency,
                incoterm=quote.incoterm,
                validity_days=quote.validity_days,
                lead_time=quote.lead_time,
                payment_terms=quote.payment_terms,
                packing=quote.packing,
                remarks=quote.remarks,
                items=list(quote.items),
                created_by=user,
                created_at=now,
                updated_at=now,
            )
            session.add(draft)
            session.flush()
            return draft.id

    def discard(self, quote_id: int) -> int:
        """删除草稿，返回公司 id。发出过的版本不能删。"""
        with self.ctx.db.session() as session:
            quote = self._get(session, quote_id)
            if quote.status is not QuoteStatus.DRAFT:
                raise ValueError("发出过的报价不能删除")
            company_id = quote.company_id
            session.delete(quote)
            return company_id

    @staticmethod
    def _get(session: Session, quote_id: int) -> Quote:
        quote = session.get(Quote, quote_id)
        if quote is None:
            raise LookupError("报价单不存在")
        return quote

    @staticmethod
    def _check_contactable(session: Session, company: Company) -> None:
        if is_suppressed(session, company.domain):
            raise ValueError("这家公司在拒绝联系名单里，不能再报价")
