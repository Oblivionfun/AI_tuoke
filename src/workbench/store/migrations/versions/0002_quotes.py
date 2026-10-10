"""quotes

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10 19:40:50.103897
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quotes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rfq_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "sent",
                "accepted",
                "rejected",
                "superseded",
                name="quotestatus",
                native_enum=False,
                length=32,
            ),
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
        sa.Column("outcome_note", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("sent_by", sa.String(length=64), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["rfq_id"], ["rfqs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rfq_id", "version"),
    )
    with op.batch_alter_table("quotes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_quotes_company_id"), ["company_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_quotes_rfq_id"), ["rfq_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_quotes_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("quotes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_quotes_status"))
        batch_op.drop_index(batch_op.f("ix_quotes_rfq_id"))
        batch_op.drop_index(batch_op.f("ix_quotes_company_id"))

    op.drop_table("quotes")
