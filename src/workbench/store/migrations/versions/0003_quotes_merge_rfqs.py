"""报价单并入询价，报价版本快照，产品报价字段

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10 20:20:00

旧结构：rfqs（询价）+ quotes（每个版本一行，草稿/已发出/已替代…）。
新结构：quotes 一行就是一次询价到成交的报价单，quote_versions 存每次生成的快照。
每条旧询价变成一张报价单（沿用 id，单号不变），最新一版的明细作为当前明细，发出过的版本变成快照。
"""

import json
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

COUNTRY_EN = {"AE": "United Arab Emirates", "ZA": "South Africa", "SA": "Saudi Arabia"}
RFQ_STATUS = {"open": "pending", "quoted": "quoted", "sample": "quoted", "won": "won", "lost": "lost"}
SENT = {"sent", "accepted", "rejected", "superseded"}


def _total(items: list[dict]) -> str:
    total = Decimal(0)
    for item in items:
        try:
            total += Decimal(item.get("quantity") or 0) * Decimal(item.get("unit_price") or 0)
        except InvalidOperation:
            continue
    return str(total.quantize(Decimal("0.01")))


def upgrade() -> None:
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.add_column(sa.Column("spec_en", sa.String(length=300), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("price_unit", sa.String(length=32), nullable=False, server_default="1000 pcs"))
        batch_op.add_column(sa.Column("ref_price", sa.String(length=32), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("cost", sa.String(length=32), nullable=False, server_default=""))

    conn = op.get_bind()
    companies = {row.id: row for row in conn.execute(sa.text("SELECT id, name, country FROM companies"))}
    rfqs = conn.execute(sa.text("SELECT * FROM rfqs ORDER BY id")).mappings().all()
    old_quotes = conn.execute(sa.text("SELECT * FROM quotes ORDER BY rfq_id, version")).mappings().all()

    op.drop_table("quotes")
    op.drop_table("rfqs")
    op.create_table(
        "quotes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("customer_name", sa.String(length=300), nullable=False),
        sa.Column("customer_country", sa.String(length=100), nullable=False),
        sa.Column("customer_contact", sa.String(length=300), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "quoted", "won", "lost", name="quotestatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("incoterm", sa.String(length=64), nullable=False),
        sa.Column("validity_days", sa.Integer(), nullable=False),
        sa.Column("lead_time", sa.String(length=100), nullable=False),
        sa.Column("payment_terms", sa.String(length=200), nullable=False),
        sa.Column("packing", sa.String(length=300), nullable=False),
        sa.Column("remarks", sa.Text(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("inquiry", sa.Text(), nullable=False),
        sa.Column("lost_reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("quotes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_quotes_company_id"), ["company_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_quotes_customer_name"), ["customer_name"], unique=False)
        batch_op.create_index(batch_op.f("ix_quotes_status"), ["status"], unique=False)
    op.create_table(
        "quote_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("quote_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("total", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quote_id", "version"),
    )
    with op.batch_alter_table("quote_versions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_quote_versions_quote_id"), ["quote_id"], unique=False)

    insert_quote = sa.text(
        "INSERT INTO quotes (id, company_id, customer_name, customer_country, customer_contact, status, currency,"
        " incoterm, validity_days, lead_time, payment_terms, packing, remarks, items, inquiry, lost_reason,"
        " created_by, created_at, updated_at) VALUES (:id, :company_id, :customer_name, :customer_country, '',"
        " :status, :currency, :incoterm, :validity_days, :lead_time, :payment_terms, :packing, :remarks, :items,"
        " '', :lost_reason, :created_by, :created_at, :updated_at)"
    )
    insert_version = sa.text(
        "INSERT INTO quote_versions (quote_id, version, snapshot, total, created_by, created_at)"
        " VALUES (:quote_id, :version, :snapshot, :total, :created_by, :created_at)"
    )
    for rfq in rfqs:
        mine = [q for q in old_quotes if q["rfq_id"] == rfq["id"]]
        latest = mine[-1] if mine else None
        company = companies.get(rfq["company_id"])
        customer = {
            "name": company.name if company else "",
            "country": COUNTRY_EN.get(company.country, company.country) if company else "",
            "contact": "",
        }

        def items_of(quote) -> list[dict]:
            return [{"product_id": None, "size": "", **item} for item in json.loads(quote["items"] or "[]")]

        if latest is not None:
            items = items_of(latest)
            terms = {k: latest[k] for k in ("currency", "incoterm", "validity_days", "lead_time", "payment_terms")}
            terms |= {"packing": latest["packing"], "remarks": latest["remarks"]}
        else:
            line = {"product_id": None, "description": rfq["products"], "size": "", "spec": "", "quantity": None}
            items = [line | {"unit": "pcs", "unit_price": None, "unit_cost": None}]
            terms = {
                "currency": "USD",
                "incoterm": rfq["incoterm"],
                "validity_days": 30,
                "lead_time": "",
                "payment_terms": "",
                "packing": "",
                "remarks": rfq["notes"] or "",
            }
        sent = [q for q in mine if q["status"] in SENT]
        status = RFQ_STATUS.get(rfq["status"], "pending")
        if status == "quoted" and not sent:
            status = "pending"
        conn.execute(
            insert_quote,
            {
                "id": rfq["id"],
                "company_id": rfq["company_id"],
                "customer_name": customer["name"],
                "customer_country": customer["country"],
                "status": status,
                **terms,
                "items": json.dumps(items, ensure_ascii=False),
                "lost_reason": next((q["outcome_note"] for q in reversed(mine) if q["outcome_note"]), ""),
                "created_by": rfq["created_by"],
                "created_at": rfq["created_at"],
                "updated_at": latest["updated_at"] if latest else rfq["created_at"],
            },
        )
        for n, quote in enumerate(sent, 1):
            version_items = items_of(quote)
            version_terms = {
                k: quote[k]
                for k in ("currency", "incoterm", "validity_days", "lead_time", "payment_terms", "packing", "remarks")
            }
            snapshot = {"customer": customer, "terms": version_terms, "items": version_items}
            conn.execute(
                insert_version,
                {
                    "quote_id": rfq["id"],
                    "version": n,
                    "snapshot": json.dumps(snapshot, ensure_ascii=False),
                    "total": _total(version_items),
                    "created_by": quote["sent_by"] or quote["created_by"],
                    "created_at": quote["sent_at"] or quote["created_at"],
                },
            )


def downgrade() -> None:
    raise NotImplementedError("报价单结构改动不能自动回退，请用升级前的数据库备份恢复")
