"""Medicines and versioned formulary rules.

Creates the btree_gist and pg_trgm extensions (both are "trusted" since PostgreSQL 13, so no
superuser is needed), the medicines catalogue and the formulary rule periods.

The exclusion constraint is what makes overlapping rule periods for one medicine impossible,
including under concurrent writes. It needs btree_gist so that the equality on medicine_id and
the overlap of the half-open date ranges [effective_from, effective_to) can share one GiST index.
pg_trgm backs the substring search on medicine name and code.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "medicines",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("form", sa.Text(), nullable=False),
        sa.Column("strength_value", sa.Numeric(12, 3), nullable=False),
        sa.Column("strength_unit", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_medicines"),
        sa.UniqueConstraint("code", name="uq_medicines_code"),
        sa.CheckConstraint("code = btrim(code) AND code <> ''", name="ck_medicines_code_trimmed"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_medicines_name_not_blank"),
        sa.CheckConstraint("btrim(form) <> ''", name="ck_medicines_form_not_blank"),
        sa.CheckConstraint(
            "btrim(strength_unit) <> ''", name="ck_medicines_strength_unit_not_blank"
        ),
        sa.CheckConstraint("strength_value > 0", name="ck_medicines_strength_value_positive"),
    )
    op.create_index("ix_medicines_name_code", "medicines", ["name", "code"])
    op.create_index(
        "ix_medicines_name_trgm",
        "medicines",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_medicines_code_trgm",
        "medicines",
        ["code"],
        postgresql_using="gin",
        postgresql_ops={"code": "gin_trgm_ops"},
    )

    op.create_table(
        "formulary_rules",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("medicine_id", sa.BigInteger(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("max_quantity_per_dispense", sa.Integer(), nullable=False),
        sa.Column("max_quantity_per_30_days", sa.Integer(), nullable=False),
        sa.Column(
            "requires_authorisation", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_formulary_rules"),
        sa.ForeignKeyConstraint(
            ["medicine_id"], ["medicines.id"], name="fk_formulary_rules_medicine_id"
        ),
        # Target of the composite foreign key that dispenses gain in migration 0003.
        sa.UniqueConstraint("id", "medicine_id", name="uq_formulary_rules_id_medicine_id"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_formulary_rules_period_ordered",
        ),
        sa.CheckConstraint(
            "max_quantity_per_dispense > 0", name="ck_formulary_rules_max_per_dispense_positive"
        ),
        sa.CheckConstraint(
            "max_quantity_per_30_days > 0", name="ck_formulary_rules_max_per_30_days_positive"
        ),
    )
    op.execute(
        "ALTER TABLE formulary_rules ADD CONSTRAINT ex_formulary_rules_no_overlap "
        "EXCLUDE USING gist ("
        "medicine_id WITH =, daterange(effective_from, effective_to, '[)') WITH &&)"
    )
    op.create_index(
        "ix_formulary_rules_medicine_id_effective_from",
        "formulary_rules",
        ["medicine_id", "effective_from"],
    )


def downgrade() -> None:
    # Dropping a table drops its indexes and constraints (including the exclusion constraint).
    op.drop_table("formulary_rules")
    op.drop_table("medicines")
    # Only after the tables: the GiST and GIN indexes depend on the extensions' operator classes.
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
