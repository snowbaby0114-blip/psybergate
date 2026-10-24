"""Dispenses and the dispense attempt log.

dispenses holds what was actually handed to a patient; dispense_attempts records every business
evaluation, accepted or rejected, so rejections are persisted with their reasons while leaving
no dispense row behind. Its outcome-shape CHECK lets the schema itself guarantee that a rejection
has violations and no dispense, and that an acceptance has a dispense and no violations.

dispenses has no link to the rule that governed it yet; migration 0003 adds that, back-filling
rows that already exist.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dispenses",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("medicine_id", sa.BigInteger(), nullable=False),
        sa.Column("patient_ref", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("dispensed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorisation_ref", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dispenses"),
        sa.ForeignKeyConstraint(["medicine_id"], ["medicines.id"], name="fk_dispenses_medicine_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_dispenses_idempotency_key"),
        sa.CheckConstraint("quantity > 0", name="ck_dispenses_quantity_positive"),
        sa.CheckConstraint("btrim(patient_ref) <> ''", name="ck_dispenses_patient_ref_not_blank"),
        sa.CheckConstraint(
            "authorisation_ref IS NULL OR btrim(authorisation_ref) <> ''",
            name="ck_dispenses_authorisation_ref_not_blank",
        ),
    )
    # Quota/window read under the lock, and the patient ledger.
    op.create_index(
        "ix_dispenses_patient_medicine_time",
        "dispenses",
        ["patient_ref", "medicine_id", sa.text("dispensed_at DESC"), sa.text("id DESC")],
    )
    # Medicine filter, history-immutability check and foreign-key support.
    op.create_index(
        "ix_dispenses_medicine_time",
        "dispenses",
        ["medicine_id", sa.text("dispensed_at DESC"), sa.text("id DESC")],
    )
    # Unfiltered newest-first listing.
    op.create_index(
        "ix_dispenses_time", "dispenses", [sa.text("dispensed_at DESC"), sa.text("id DESC")]
    )

    op.create_table(
        "dispense_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("medicine_code", sa.Text(), nullable=False),
        sa.Column("medicine_id", sa.BigInteger(), nullable=True),
        sa.Column("patient_ref", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("dispensed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorisation_ref", sa.Text(), nullable=True),
        sa.Column(
            "violations",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("dispense_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dispense_attempts"),
        sa.ForeignKeyConstraint(
            ["medicine_id"], ["medicines.id"], name="fk_dispense_attempts_medicine_id"
        ),
        sa.ForeignKeyConstraint(
            ["dispense_id"], ["dispenses.id"], name="fk_dispense_attempts_dispense_id"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_dispense_attempts_idempotency_key"),
        sa.UniqueConstraint("dispense_id", name="uq_dispense_attempts_dispense_id"),
        sa.CheckConstraint("quantity > 0", name="ck_dispense_attempts_quantity_positive"),
        sa.CheckConstraint(
            "(outcome = 'accepted' AND dispense_id IS NOT NULL"
            " AND jsonb_array_length(violations) = 0)"
            " OR (outcome = 'rejected' AND dispense_id IS NULL"
            " AND jsonb_array_length(violations) > 0)",
            name="ck_dispense_attempts_outcome_shape",
        ),
    )


def downgrade() -> None:
    # Dropping a table drops its indexes and constraints. The attempt log references dispenses,
    # so it goes first.
    op.drop_table("dispense_attempts")
    op.drop_table("dispenses")
