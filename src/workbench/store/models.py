"""工作台数据表（最小集）：产品/SKU、公司、运行与步骤、草稿、联系记录、RFQ、拒绝联系名单、用户。"""

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """SQLite 不存时区：写入时转 UTC，读出时补上 UTC。"""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


def enum_col(enum_cls: type[StrEnum]) -> Enum:
    return Enum(enum_cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class CompanyStatus(StrEnum):
    NEW = "new"
    RESEARCHING = "researching"
    QUEUED = "queued"
    ACCEPTED = "accepted"
    NEEDS_EVIDENCE = "needs_evidence"
    CONTACTED = "contacted"
    REPLIED = "replied"
    ARCHIVED = "archived"
    FAILED = "failed"


class Queue(StrEnum):
    FIRST_TOUCH = "first_touch"
    QUALIFIED = "qualified"
    UNFIT = "unfit"

    @property
    def label(self) -> str:
        return {"first_touch": "可首触", "qualified": "合格买家", "unfit": "不合适"}[self.value]


class RunKind(StrEnum):
    PROSPECT = "prospect"
    RESEARCH = "research"
    WRITE = "write"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DraftStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    DISCARDED = "discarded"


class Channel(StrEnum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    PHONE = "phone"
    LINKEDIN = "linkedin"
    PORTAL = "portal"
    OTHER = "other"


class Direction(StrEnum):
    OUT = "out"
    IN = "in"


class RfqStatus(StrEnum):
    OPEN = "open"
    QUOTED = "quoted"
    SAMPLE = "sample"
    WON = "won"
    LOST = "lost"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    name_en: Mapped[str] = mapped_column(String(200))
    name_zh: Mapped[str] = mapped_column(String(200), default="")
    material_declared: Mapped[str] = mapped_column(String(200), default="")
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_issue: Mapped[str] = mapped_column(Text, default="")
    duplicate_group: Mapped[str] = mapped_column(String(64), default="")
    sales_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    variants: Mapped[list["SkuVariant"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class SkuVariant(Base):
    """工厂确认过的规格。文案只引用确认过的 SKU。"""

    __tablename__ = "sku_variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    standard: Mapped[str] = mapped_column(String(100), default="")
    material: Mapped[str] = mapped_column(String(200), default="")
    sizes: Mapped[str] = mapped_column(String(300), default="")
    finish: Mapped[str] = mapped_column(String(200), default="")
    moq: Mapped[str] = mapped_column(String(100), default="")
    lead_time: Mapped[str] = mapped_column(String(100), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    confirmed_by: Mapped[str] = mapped_column(String(64), default="")
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    product: Mapped[Product] = relationship(back_populates="variants")


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    domain: Mapped[str] = mapped_column(String(253), unique=True)
    website: Mapped[str] = mapped_column(String(500), default="")
    country: Mapped[str] = mapped_column(String(2), index=True)
    source: Mapped[str] = mapped_column(String(32), default="prospect")
    status: Mapped[CompanyStatus] = mapped_column(enum_col(CompanyStatus), default=CompanyStatus.NEW, index=True)
    queue: Mapped[Queue | None] = mapped_column(enum_col(Queue), index=True)
    score: Mapped[int | None] = mapped_column(Integer)
    prospect_evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    dossier: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    review: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    revisions: Mapped[int] = mapped_column(Integer, default=0)
    sanctions_hint: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    queued_on: Mapped[date | None] = mapped_column(Date, index=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(64), default="")
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    drafts: Mapped[list["Draft"]] = relationship(back_populates="company", order_by="Draft.id.desc()")
    interactions: Mapped[list["Interaction"]] = relationship(
        back_populates="company", order_by="Interaction.happened_at.desc()"
    )
    rfqs: Mapped[list["Rfq"]] = relationship(back_populates="company", order_by="Rfq.id.desc()")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[RunKind] = mapped_column(enum_col(RunKind), index=True)
    status: Mapped[RunStatus] = mapped_column(enum_col(RunStatus), default=RunStatus.QUEUED, index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), index=True)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    agent_calls: Mapped[list[Any]] = mapped_column(default=list)
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    company: Mapped[Company | None] = relationship()


class RunEvent(Base):
    """运行页的实时步骤（SSE 推送来源）。"""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))
    kind: Mapped[str] = mapped_column(String(32))
    to_address: Mapped[str] = mapped_column(String(320), default="")
    subject_en: Mapped[str] = mapped_column(String(500), default="")
    body_en: Mapped[str] = mapped_column(Text, default="")
    subject_zh: Mapped[str] = mapped_column(String(500), default="")
    body_zh: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[DraftStatus] = mapped_column(enum_col(DraftStatus), default=DraftStatus.DRAFT)
    sent_by: Mapped[str] = mapped_column(String(64), default="")
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    company: Mapped[Company] = relationship(back_populates="drafts")


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    channel: Mapped[Channel] = mapped_column(enum_col(Channel))
    direction: Mapped[Direction] = mapped_column(enum_col(Direction))
    summary: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    happened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    company: Mapped[Company] = relationship(back_populates="interactions")


class Rfq(Base):
    __tablename__ = "rfqs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    products: Mapped[str] = mapped_column(Text)
    quantity: Mapped[str] = mapped_column(String(200), default="")
    target_price: Mapped[str] = mapped_column(String(200), default="")
    incoterm: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[RfqStatus] = mapped_column(enum_col(RfqStatus), default=RfqStatus.OPEN)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    company: Mapped[Company] = relationship(back_populates="rfqs")


class Suppression(Base):
    """拒绝联系名单：按域名，寻客和去重都会排除。"""

    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(253), unique=True)
    company_name: Mapped[str] = mapped_column(String(300), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
