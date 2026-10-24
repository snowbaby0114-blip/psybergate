"""ORM description of the schema as it stands after migration 0003.

Alembic migrations are the only thing that creates the schema (never ``metadata.create_all``);
tests/db proves that this module and the migrations describe the same database. Every
constraint and index is named explicitly so migrations, models and error mapping agree.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[type, Any]] = {
        str: Text,
        datetime: DateTime(timezone=True),
    }


class Medicine(Base):
    __tablename__ = "medicines"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str]
    name: Mapped[str]
    form: Mapped[str]
    strength_value: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    strength_unit: Mapped[str]
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_medicines"),
        UniqueConstraint("code", name="uq_medicines_code"),
        CheckConstraint("code = btrim(code) AND code <> ''", name="ck_medicines_code_trimmed"),
        CheckConstraint("btrim(name) <> ''", name="ck_medicines_name_not_blank"),
        CheckConstraint("btrim(form) <> ''", name="ck_medicines_form_not_blank"),
        CheckConstraint("btrim(strength_unit) <> ''", name="ck_medicines_strength_unit_not_blank"),
        CheckConstraint("strength_value > 0", name="ck_medicines_strength_value_positive"),
        # Keyset ordering of the catalogue listing.
        Index("ix_medicines_name_code", "name", "code"),
        # Trigram indexes serve the case-insensitive substring search (ILIKE '%q%').
        Index(
            "ix_medicines_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_medicines_code_trgm",
            "code",
            postgresql_using="gin",
            postgresql_ops={"code": "gin_trgm_ops"},
        ),
    )


class FormularyRule(Base):
    __tablename__ = "formulary_rules"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    medicine_id: Mapped[int] = mapped_column(BigInteger)
    # effective_to is exclusive and NULL means open-ended, so a period is the range
    # [effective_from, effective_to). Both are assigned mapped_column() so the exclusion
    # constraint below can refer to them.
    effective_from: Mapped[date] = mapped_column()
    effective_to: Mapped[date | None] = mapped_column()
    max_quantity_per_dispense: Mapped[int]
    max_quantity_per_30_days: Mapped[int]
    requires_authorisation: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_formulary_rules"),
        ForeignKeyConstraint(
            ["medicine_id"], ["medicines.id"], name="fk_formulary_rules_medicine_id"
        ),
        # Target of the composite foreign key from dispenses.
        UniqueConstraint("id", "medicine_id", name="uq_formulary_rules_id_medicine_id"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_formulary_rules_period_ordered",
        ),
        CheckConstraint(
            "max_quantity_per_dispense > 0", name="ck_formulary_rules_max_per_dispense_positive"
        ),
        CheckConstraint(
            "max_quantity_per_30_days > 0", name="ck_formulary_rules_max_per_30_days_positive"
        ),
        # Two rule periods of one medicine can never overlap, whatever the application does.
        ExcludeConstraint(
            ("medicine_id", "="),
            (func.daterange(effective_from, effective_to, "[)"), "&&"),
            name="ex_formulary_rules_no_overlap",
            using="gist",
        ),
        Index("ix_formulary_rules_medicine_id_effective_from", "medicine_id", "effective_from"),
    )


class Dispense(Base):
    __tablename__ = "dispenses"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    medicine_id: Mapped[int] = mapped_column(BigInteger)
    patient_ref: Mapped[str]
    quantity: Mapped[int]
    dispensed_at: Mapped[datetime] = mapped_column()
    authorisation_ref: Mapped[str | None]
    idempotency_key: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    rule_id: Mapped[int] = mapped_column(BigInteger)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_dispenses"),
        ForeignKeyConstraint(["medicine_id"], ["medicines.id"], name="fk_dispenses_medicine_id"),
        # The governing rule must belong to the dispensed medicine.
        ForeignKeyConstraint(
            ["rule_id", "medicine_id"],
            ["formulary_rules.id", "formulary_rules.medicine_id"],
            name="fk_dispenses_rule_id_medicine_id",
        ),
        UniqueConstraint("idempotency_key", name="uq_dispenses_idempotency_key"),
        CheckConstraint("quantity > 0", name="ck_dispenses_quantity_positive"),
        CheckConstraint("btrim(patient_ref) <> ''", name="ck_dispenses_patient_ref_not_blank"),
        CheckConstraint(
            "authorisation_ref IS NULL OR btrim(authorisation_ref) <> ''",
            name="ck_dispenses_authorisation_ref_not_blank",
        ),
        # Quota/window read under the lock, and the patient ledger.
        Index(
            "ix_dispenses_patient_medicine_time",
            "patient_ref",
            "medicine_id",
            dispensed_at.desc(),
            id.desc(),
        ),
        # Medicine filter, history-immutability check and foreign-key support.
        Index("ix_dispenses_medicine_time", "medicine_id", dispensed_at.desc(), id.desc()),
        # Unfiltered newest-first listing.
        Index("ix_dispenses_time", dispensed_at.desc(), id.desc()),
    )


class DispenseAttempt(Base):
    __tablename__ = "dispense_attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    idempotency_key: Mapped[str]
    request_hash: Mapped[str]
    outcome: Mapped[str]
    medicine_code: Mapped[str]
    medicine_id: Mapped[int | None] = mapped_column(BigInteger)
    patient_ref: Mapped[str]
    quantity: Mapped[int]
    dispensed_at: Mapped[datetime]
    authorisation_ref: Mapped[str | None]
    violations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    dispense_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_dispense_attempts"),
        ForeignKeyConstraint(
            ["medicine_id"], ["medicines.id"], name="fk_dispense_attempts_medicine_id"
        ),
        ForeignKeyConstraint(
            ["dispense_id"], ["dispenses.id"], name="fk_dispense_attempts_dispense_id"
        ),
        UniqueConstraint("idempotency_key", name="uq_dispense_attempts_idempotency_key"),
        UniqueConstraint("dispense_id", name="uq_dispense_attempts_dispense_id"),
        CheckConstraint("quantity > 0", name="ck_dispense_attempts_quantity_positive"),
        # The schema itself guarantees that a rejection has reasons and no dispense, and that
        # an acceptance has a dispense and no reasons.
        CheckConstraint(
            "(outcome = 'accepted' AND dispense_id IS NOT NULL"
            " AND jsonb_array_length(violations) = 0)"
            " OR (outcome = 'rejected' AND dispense_id IS NULL"
            " AND jsonb_array_length(violations) > 0)",
            name="ck_dispense_attempts_outcome_shape",
        ),
    )
