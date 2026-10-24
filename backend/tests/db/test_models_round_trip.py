"""The ORM models write and read the real schema, with the types and constraints it declares."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from psycopg import errors
from sqlalchemy import exc
from sqlalchemy.orm import Session, sessionmaker

from app.models import Dispense, DispenseAttempt, FormularyRule, Medicine

SAST = timezone(timedelta(hours=2))
REASONS = [
    {
        "code": "later_dispense_limit_exceeded",
        "field": "quantity",
        "message": "Would push a later dispense over its limit ✓",
        "context": {"affected": [{"business_date": "2026-03-20", "dispense_ids": [4, 9]}]},
    }
]


@dataclass(frozen=True)
class Stored:
    medicine: int
    rule: int
    dispense: int
    accepted: int
    rejected: int


@pytest.fixture
def stored(session_factory: sessionmaker[Session], clean_db: None) -> Stored:
    """One row of every model, committed through the ORM."""
    with session_factory() as session:
        medicine = Medicine(
            code="MED-1",
            name="Amoxil",
            form="capsule",
            strength_value=Decimal("0.250"),
            strength_unit="mg",
        )
        session.add(medicine)
        session.flush()
        rule = FormularyRule(
            medicine_id=medicine.id,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            max_quantity_per_dispense=10,
            max_quantity_per_30_days=30,
        )
        session.add(rule)
        session.flush()
        dispense = Dispense(
            medicine_id=medicine.id,
            rule_id=rule.id,
            patient_ref="PT-1",
            quantity=5,
            dispensed_at=datetime(2026, 3, 15, 12, 0, tzinfo=SAST),
            idempotency_key="key-accepted",
        )
        session.add(dispense)
        session.flush()
        accepted = DispenseAttempt(
            idempotency_key="key-accepted",
            request_hash="a" * 64,
            outcome="accepted",
            medicine_code="MED-1",
            medicine_id=medicine.id,
            patient_ref="PT-1",
            quantity=5,
            dispensed_at=dispense.dispensed_at,
            dispense_id=dispense.id,
            violations=[],
        )
        rejected = DispenseAttempt(
            idempotency_key="key-rejected",
            request_hash="b" * 64,
            outcome="rejected",
            medicine_code="MED-1",
            medicine_id=medicine.id,
            patient_ref="PT-1",
            quantity=50,
            dispensed_at=dispense.dispensed_at,
            violations=REASONS,
        )
        session.add_all([accepted, rejected])
        session.commit()
        return Stored(medicine.id, rule.id, dispense.id, accepted.id, rejected.id)


def test_values_survive_a_round_trip(
    session_factory: sessionmaker[Session], stored: Stored
) -> None:
    with session_factory() as session:
        medicine = session.get_one(Medicine, stored.medicine)
        rule = session.get_one(FormularyRule, stored.rule)
        dispense = session.get_one(Dispense, stored.dispense)
        accepted = session.get_one(DispenseAttempt, stored.accepted)
        rejected = session.get_one(DispenseAttempt, stored.rejected)

        assert str(medicine.strength_value) == "0.250"
        assert medicine.is_active is True
        assert medicine.created_at.utcoffset() == timedelta(0)
        assert (rule.effective_from, rule.effective_to) == (date(2026, 1, 1), None)
        assert rule.requires_authorisation is False
        # Stored from 12:00 at +02:00, read back as the same instant in UTC.
        assert dispense.dispensed_at == datetime(2026, 3, 15, 10, 0, tzinfo=UTC)
        assert dispense.dispensed_at.utcoffset() == timedelta(0)
        assert dispense.authorisation_ref is None
        assert accepted.dispense_id == dispense.id
        assert accepted.violations == []
        assert rejected.dispense_id is None
        assert rejected.violations == REASONS


def test_the_composite_foreign_key_holds_through_the_orm(
    session_factory: sessionmaker[Session], stored: Stored
) -> None:
    with session_factory() as session:
        other = Medicine(
            code="MED-2", name="Brufen", form="tablet", strength_value=200, strength_unit="mg"
        )
        session.add(other)
        session.flush()
        session.add(
            Dispense(
                medicine_id=other.id,
                rule_id=stored.rule,  # a rule of MED-1
                patient_ref="PT-1",
                quantity=1,
                dispensed_at=datetime(2026, 3, 15, 10, 0, tzinfo=UTC),
                idempotency_key="key-other",
            )
        )

        with pytest.raises(exc.IntegrityError) as raised:
            session.flush()

    assert isinstance(raised.value.orig, errors.ForeignKeyViolation)
    assert raised.value.orig.diag.constraint_name == "fk_dispenses_rule_id_medicine_id"
