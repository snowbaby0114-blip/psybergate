"""The dispense queries of app.repositories, against PostgreSQL: what pair_history hands the rule
engine, and how the listing is ordered and cut into pages."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app import repositories
from app.domain.dispense import PriorDispense
from app.models import Dispense, FormularyRule, Medicine

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]
DispenseFactory = Callable[..., Dispense]


def at(day: int, hour: int = 10, minute: int = 0, second: int = 0, micro: int = 0) -> datetime:
    """An instant in March 2026, in UTC."""
    return datetime(2026, 3, day, hour, minute, second, micro, tzinfo=UTC)


@pytest.fixture
def medicine(make_medicine: MedicineFactory) -> Medicine:
    return make_medicine(code="MED-0001")


@pytest.fixture
def rule(medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
    return make_rule(medicine, date(2026, 1, 1))


class TestPairHistory:
    @pytest.fixture
    def history(
        self, session_factory: sessionmaker[Session], medicine: Medicine
    ) -> Callable[..., list[PriorDispense]]:
        def read(start: datetime, end: datetime, patient_ref: str = "PT-1") -> list[PriorDispense]:
            with session_factory() as session, session.begin():
                return repositories.pair_history(
                    session,
                    medicine_id=medicine.id,
                    patient_ref=patient_ref,
                    start=start,
                    end=end,
                )

        return read

    def test_returns_what_the_rule_engine_takes_oldest_first(
        self,
        history: Callable[..., list[PriorDispense]],
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        newer = make_dispense(medicine, rule, quantity=4, dispensed_at=at(10))
        older = make_dispense(medicine, rule, quantity=7, dispensed_at=at(5))

        assert history(at(1), at(20)) == [
            PriorDispense(id=older.id, dispensed_at=at(5), quantity=7),
            PriorDispense(id=newer.id, dispensed_at=at(10), quantity=4),
        ]

    def test_the_range_includes_its_start_and_excludes_its_end(
        self,
        history: Callable[..., list[PriorDispense]],
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        make_dispense(medicine, rule, dispensed_at=at(5) - timedelta(microseconds=1))
        starting = make_dispense(medicine, rule, dispensed_at=at(5))
        last = make_dispense(medicine, rule, dispensed_at=at(10) - timedelta(microseconds=1))
        make_dispense(medicine, rule, dispensed_at=at(10))

        found = history(at(5), at(10))

        assert [prior.id for prior in found] == [starting.id, last.id]

    def test_dispenses_at_the_same_instant_come_in_the_order_they_were_recorded(
        self,
        history: Callable[..., list[PriorDispense]],
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        recorded = [make_dispense(medicine, rule, dispensed_at=at(5)) for _ in range(3)]

        assert [prior.id for prior in history(at(1), at(20))] == [d.id for d in recorded]

    def test_it_is_by_the_date_of_the_dispense_not_by_when_it_was_recorded(
        self,
        history: Callable[..., list[PriorDispense]],
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        late = make_dispense(medicine, rule, dispensed_at=at(5), created_at=at(25))
        make_dispense(medicine, rule, dispensed_at=at(25), created_at=at(5))

        assert [prior.id for prior in history(at(1), at(10))] == [late.id]

    def test_leaves_out_other_patients_and_other_medicines(
        self,
        history: Callable[..., list[PriorDispense]],
        medicine: Medicine,
        rule: FormularyRule,
        make_medicine: MedicineFactory,
        make_rule: RuleFactory,
        make_dispense: DispenseFactory,
    ) -> None:
        other = make_medicine(code="MED-0002")
        other_rule = make_rule(other, date(2026, 1, 1))
        mine = make_dispense(medicine, rule, dispensed_at=at(5))
        make_dispense(medicine, rule, patient_ref="PT-2", dispensed_at=at(5))
        make_dispense(medicine, rule, patient_ref="pt-1", dispensed_at=at(5))
        make_dispense(other, other_rule, dispensed_at=at(5))

        assert [prior.id for prior in history(at(1), at(20))] == [mine.id]

    def test_is_empty_when_there_is_nothing(
        self, history: Callable[..., list[PriorDispense]]
    ) -> None:
        assert history(at(1), at(20)) == []

    def test_keeps_microseconds(
        self,
        history: Callable[..., list[PriorDispense]],
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        make_dispense(medicine, rule, dispensed_at=at(5, 10, 0, 0, 123456))

        [prior] = history(at(1), at(20))

        assert prior.dispensed_at == at(5, 10, 0, 0, 123456)


class TestDispenseList:
    @pytest.fixture
    def listing(
        self, session_factory: sessionmaker[Session]
    ) -> Callable[..., list[tuple[int, str, str]]]:
        def read(
            patient_ref: str | None = None,
            medicine_id: int | None = None,
            after: tuple[datetime, int] | None = None,
            limit: int = 10,
        ) -> list[tuple[int, str, str]]:
            with session_factory() as session, session.begin():
                rows = repositories.list_dispenses(session, patient_ref, medicine_id, after, limit)
                return [(dispense.id, code, name) for dispense, code, name in rows]

        return read

    def test_newest_first_by_date_then_by_id_with_the_medicine_code_and_name(
        self,
        listing: Callable[..., list[tuple[int, str, str]]],
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        oldest = make_dispense(medicine, rule, dispensed_at=at(1))
        tied_first = make_dispense(medicine, rule, dispensed_at=at(5))
        tied_second = make_dispense(medicine, rule, dispensed_at=at(5))
        newest = make_dispense(medicine, rule, dispensed_at=at(9))

        assert listing() == [
            (newest.id, "MED-0001", "Amoxil"),
            (tied_second.id, "MED-0001", "Amoxil"),
            (tied_first.id, "MED-0001", "Amoxil"),
            (oldest.id, "MED-0001", "Amoxil"),
        ]

    def test_reads_one_row_more_than_the_page_to_tell_whether_there_is_another(
        self,
        listing: Callable[..., list[tuple[int, str, str]]],
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        for day in range(1, 6):
            make_dispense(medicine, rule, dispensed_at=at(day))

        assert len(listing(limit=3)) == 4
        assert len(listing(limit=5)) == 5

    def test_resumes_strictly_after_the_position_of_a_cursor(
        self,
        listing: Callable[..., list[tuple[int, str, str]]],
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        first = make_dispense(medicine, rule, dispensed_at=at(5))
        second = make_dispense(medicine, rule, dispensed_at=at(5))
        third = make_dispense(medicine, rule, dispensed_at=at(5))
        oldest = make_dispense(medicine, rule, dispensed_at=at(1))
        newest = make_dispense(medicine, rule, dispensed_at=at(9))

        after_second = listing(after=(at(5), second.id))
        after_newest = listing(after=(at(9), newest.id))

        assert [row[0] for row in after_second] == [first.id, oldest.id]
        assert [row[0] for row in after_newest] == [third.id, second.id, first.id, oldest.id]

    def test_filters_by_patient_and_by_medicine_id(
        self,
        listing: Callable[..., list[tuple[int, str, str]]],
        medicine: Medicine,
        rule: FormularyRule,
        make_medicine: MedicineFactory,
        make_rule: RuleFactory,
        make_dispense: DispenseFactory,
    ) -> None:
        other = make_medicine(code="MED-0002", name="Panado")
        other_rule = make_rule(other, date(2026, 1, 1))
        a = make_dispense(medicine, rule, patient_ref="PT-1", dispensed_at=at(1))
        b = make_dispense(other, other_rule, patient_ref="PT-1", dispensed_at=at(2))
        c = make_dispense(medicine, rule, patient_ref="PT-2", dispensed_at=at(3))

        assert [row[0] for row in listing(patient_ref="PT-1")] == [b.id, a.id]
        assert [row[0] for row in listing(medicine_id=medicine.id)] == [c.id, a.id]
        assert [row[0] for row in listing(patient_ref="PT-1", medicine_id=other.id)] == [b.id]
        assert listing(patient_ref="PT-3") == []


class TestAttemptsAndDispenses:
    def test_what_is_inserted_can_be_read_back_by_key_and_by_id(
        self,
        session_factory: sessionmaker[Session],
        medicine: Medicine,
        rule: FormularyRule,
    ) -> None:
        with session_factory() as session, session.begin():
            dispense = repositories.insert_dispense(
                session,
                medicine_id=medicine.id,
                rule_id=rule.id,
                patient_ref="PT-1",
                quantity=5,
                dispensed_at=at(5),
                authorisation_ref=None,
                idempotency_key="key-1",
            )
            attempt = repositories.insert_attempt(
                session,
                idempotency_key="key-1",
                request_hash="h" * 64,
                outcome="accepted",
                medicine_code="MED-0001",
                medicine_id=medicine.id,
                patient_ref="PT-1",
                quantity=5,
                dispensed_at=at(5),
                authorisation_ref=None,
                violations=[],
                dispense_id=dispense.id,
            )
            # The server-side defaults come back with the INSERT: reading them needs no query.
            assert dispense.created_at is not None
            assert attempt.created_at is not None

        with session_factory() as session, session.begin():
            found = repositories.find_attempt_by_key(session, "key-1")
            stored, code, name = repositories.get_dispense_with_medicine(session, dispense.id)

            assert found is not None
            assert (found.id, found.dispense_id, found.outcome) == (
                attempt.id,
                dispense.id,
                "accepted",
            )
            assert (stored.id, code, name) == (dispense.id, "MED-0001", "Amoxil")
            assert repositories.find_attempt_by_key(session, "other") is None

    def test_violations_are_stored_as_given(
        self, session_factory: sessionmaker[Session], medicine: Medicine
    ) -> None:
        violations = [
            {
                "code": "later_dispense_limit_exceeded",
                "field": "quantity",
                "message": "m",
                "context": {"affected": [{"business_date": "2026-03-20", "dispense_ids": [1, 2]}]},
            }
        ]
        with session_factory() as session, session.begin():
            repositories.insert_attempt(
                session,
                idempotency_key="key-1",
                request_hash="h" * 64,
                outcome="rejected",
                medicine_code="MED-0001",
                medicine_id=medicine.id,
                patient_ref="PT-1",
                quantity=5,
                dispensed_at=at(5),
                authorisation_ref=None,
                violations=violations,
                dispense_id=None,
            )

        with session_factory() as session, session.begin():
            found = repositories.find_attempt_by_key(session, "key-1")

        assert found is not None
        assert found.violations == violations
