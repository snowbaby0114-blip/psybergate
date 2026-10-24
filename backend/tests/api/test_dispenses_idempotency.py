"""POST /api/v1/dispenses is idempotent on idempotency_key: the same key with the same payload gives
the original outcome again and creates nothing; the same key with another payload is an error."""

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app import repositories
from app.models import Dispense, FormularyRule, Medicine
from app.seed import TABLES, run_seed
from tests.api.dispense_support import DISPENSES, OMIT, REPLAYED, Ledger, post_dispense
from tests.api.support import FakeClock, assert_problem, violation_pairs

MedicineFactory = Callable[..., Medicine]
RuleFactory = Callable[..., FormularyRule]
DispenseFactory = Callable[..., Dispense]

ATTEMPT_ID = frozenset({"attempt_id"})


@pytest.fixture
def ledger(session_factory: sessionmaker[Session]) -> Ledger:
    return Ledger(session_factory)


@pytest.fixture
def medicine(make_medicine: MedicineFactory) -> Medicine:
    return make_medicine(code="MED-0001")


@pytest.fixture
def rule(medicine: Medicine, make_rule: RuleFactory) -> FormularyRule:
    """At most 10 per dispense and 30 per 30 days, in force since 1 January 2026."""
    return make_rule(medicine, date(2026, 1, 1))


def without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    """A problem names the request it answers, which is a different one for a replay."""
    return {name: value for name, value in body.items() if name != "request_id"}


def assert_identical_but_for_the_request_id(original: Response, replay: Response) -> None:
    """The bytes of a replayed rejection are those of the original, apart from the id of the
    request it answers, so even the order of the keys in a violation's context is the same."""
    original_id, replay_id = original.json()["request_id"], replay.json()["request_id"]
    assert replay_id != original_id
    assert replay.text.replace(replay_id, original_id) == original.text


class TestReplay:
    def test_an_accepted_request_returns_the_original_response_and_creates_nothing(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        first = post_dispense(client)
        replays = [post_dispense(client) for _ in range(3)]

        assert first.status_code == 201
        assert REPLAYED not in first.headers
        for replay in replays:
            assert replay.status_code == 201
            assert replay.headers[REPLAYED] == "true"
            assert replay.json() == first.json()
        assert len(ledger.dispenses()) == 1
        assert len(ledger.attempts()) == 1

    def test_a_rejected_request_returns_the_original_422_and_creates_nothing(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        first = post_dispense(client, quantity=40)
        replay = post_dispense(client, quantity=40)

        original = assert_problem(first, 422, "dispense_rejected", extra_keys=ATTEMPT_ID)
        replayed = assert_problem(replay, 422, "dispense_rejected", extra_keys=ATTEMPT_ID)
        assert REPLAYED not in first.headers
        assert replay.headers[REPLAYED] == "true"
        assert without_request_id(replayed) == without_request_id(original)
        assert replayed["request_id"] != original["request_id"]
        assert [v["code"] for v in replayed["violations"]] == [
            "max_quantity_per_dispense_exceeded",
            "max_quantity_per_30_days_exceeded",
        ]
        assert ledger.dispenses() == []
        assert [attempt.id for attempt in ledger.attempts()] == [original["attempt_id"]]

    def test_an_accepted_replay_is_identical_to_the_original_to_the_byte(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        first = post_dispense(client, authorisation_ref="AUTH-1")

        replay = post_dispense(client, authorisation_ref="AUTH-1")

        assert replay.content == first.content

    def test_a_rejected_replay_is_identical_to_the_original_to_the_byte_but_for_the_request_id(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        # The context of the second violation has six keys, which jsonb would return reordered.
        first = post_dispense(client, quantity=40)

        replay = post_dispense(client, quantity=40)

        assert [v["code"] for v in first.json()["violations"]][1] == (
            "max_quantity_per_30_days_exceeded"
        )
        assert_identical_but_for_the_request_id(first, replay)

    def test_a_replay_keeps_the_nested_context_of_a_later_dispense_violation_in_order(
        self,
        client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
    ) -> None:
        make_dispense(
            medicine, rule, quantity=20, dispensed_at=datetime(2026, 3, 20, 10, tzinfo=UTC)
        )
        first = post_dispense(client, quantity=15, dispensed_at="2026-03-10T10:00:00Z")

        replay = post_dispense(client, quantity=15, dispensed_at="2026-03-10T10:00:00Z")

        assert [v["code"] for v in first.json()["violations"]] == [
            "max_quantity_per_dispense_exceeded",
            "later_dispense_limit_exceeded",
        ]
        assert_identical_but_for_the_request_id(first, replay)

    @pytest.mark.parametrize(
        "changes",
        [
            pytest.param({"quantity": 40}, id="over the limits"),
            pytest.param({"medicine_code": "NOPE"}, id="unknown medicine"),
            pytest.param({"dispensed_at": "2025-06-01T10:00:00Z"}, id="no rule in force"),
        ],
    )
    def test_every_kind_of_rejection_replays_identically(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger, changes: dict[str, Any]
    ) -> None:
        first = post_dispense(client, **changes)
        replay = post_dispense(client, **changes)

        assert first.status_code == replay.status_code == 422
        assert replay.headers[REPLAYED] == "true"
        assert without_request_id(replay.json()) == without_request_id(first.json())
        assert len(ledger.attempts()) == 1

    def test_the_replay_is_the_original_outcome_even_if_the_rules_have_since_changed(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        refused = post_dispense(client, idempotency_key="old", quantity=11)
        relaxed = client.post(
            "/api/v1/medicines/MED-0001/rules",
            json={
                "effective_from": "2026-02-01",
                "max_quantity_per_dispense": 50,
                "max_quantity_per_30_days": 100,
            },
        )

        replay = post_dispense(client, idempotency_key="old", quantity=11)
        fresh = post_dispense(client, idempotency_key="new", quantity=11)

        assert (refused.status_code, relaxed.status_code) == (422, 201)
        assert replay.status_code == 422
        assert replay.headers[REPLAYED] == "true"
        assert fresh.status_code == 201

    def test_the_replay_of_an_accepted_request_is_not_judged_again(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        first = post_dispense(client, idempotency_key="first", quantity=10)
        # The patient's 30 days are used up by these, so the first request would now be refused.
        assert post_dispense(client, idempotency_key="second", quantity=10).status_code == 201
        assert post_dispense(client, idempotency_key="third", quantity=10).status_code == 201

        replay = post_dispense(client, idempotency_key="first", quantity=10)

        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert len(ledger.dispenses()) == 3

    def test_the_payload_may_be_spelt_differently_and_still_be_the_same_payload(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        first = post_dispense(client)

        replay = post_dispense(
            client,
            medicine_code=" MED-0001 ",
            patient_ref="PT-1\t",
            authorisation_ref="   ",
            dispensed_at="2026-03-15T12:00:00+02:00",
        )

        assert replay.status_code == 201
        assert replay.headers[REPLAYED] == "true"
        assert replay.json() == first.json()
        assert len(ledger.dispenses()) == 1

    def test_without_a_dispensed_at_the_replay_does_not_move_the_dispense_to_a_later_now(
        self, client: TestClient, rule: FormularyRule, clock: FakeClock, ledger: Ledger
    ) -> None:
        first = post_dispense(client, dispensed_at=OMIT)
        clock.now = datetime(2026, 6, 16, 8, 30, tzinfo=UTC)

        replay = post_dispense(client, dispensed_at=OMIT)

        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert replay.json()["dispensed_at"] == "2026-06-15T10:00:00Z"


class TestKeyReuse:
    """The same key with another payload cannot be a retry, and retrying it cannot succeed: it is
    a 422 of its own, told apart from a rejection by the rules and from a state conflict."""

    @pytest.mark.parametrize(
        "changes",
        [
            pytest.param({"quantity": 6}, id="quantity"),
            pytest.param({"patient_ref": "PT-2"}, id="patient"),
            pytest.param({"medicine_code": "MED-0002"}, id="medicine"),
            pytest.param({"dispensed_at": "2026-03-15T10:00:01Z"}, id="a second later"),
            pytest.param({"dispensed_at": "2026-03-15T10:00:00.000001Z"}, id="a microsecond"),
            pytest.param({"authorisation_ref": "AUTH-1"}, id="authorisation"),
        ],
    )
    def test_another_payload_under_the_same_key_is_refused_and_creates_nothing(
        self,
        client: TestClient,
        rule: FormularyRule,
        make_medicine: MedicineFactory,
        ledger: Ledger,
        changes: dict[str, Any],
    ) -> None:
        make_medicine(code="MED-0002")
        first = post_dispense(client)

        response = post_dispense(client, **changes)

        body = assert_problem(response, 422, "idempotency_key_reuse")
        assert violation_pairs(body) == [("idempotency_key_reuse", "idempotency_key")]
        assert REPLAYED not in response.headers
        assert len(ledger.dispenses()) == 1
        assert len(ledger.attempts()) == 1
        again = post_dispense(client)
        assert again.status_code == 201
        assert again.json() == first.json()

    def test_a_key_used_by_a_rejection_cannot_be_reused_for_another_payload_either(
        self, client: TestClient, rule: FormularyRule, ledger: Ledger
    ) -> None:
        post_dispense(client, quantity=40)

        response = post_dispense(client, quantity=5)

        assert_problem(response, 422, "idempotency_key_reuse")
        assert ledger.dispenses() == []
        assert len(ledger.attempts()) == 1

    def test_an_omitted_dispensed_at_is_not_the_same_payload_as_the_instant_it_resolved_to(
        self, client: TestClient, rule: FormularyRule, clock: FakeClock, ledger: Ledger
    ) -> None:
        first = post_dispense(client, dispensed_at=OMIT)
        assert first.json()["dispensed_at"] == "2026-06-15T10:00:00Z"

        explicit = post_dispense(client, dispensed_at="2026-06-15T10:00:00Z")

        assert_problem(explicit, 422, "idempotency_key_reuse")
        assert len(ledger.dispenses()) == 1

    def test_an_explicit_dispensed_at_is_not_the_same_payload_as_none_given(
        self, client: TestClient, rule: FormularyRule, clock: FakeClock, ledger: Ledger
    ) -> None:
        post_dispense(client, dispensed_at="2026-06-15T10:00:00Z")

        omitted = post_dispense(client, dispensed_at=OMIT)

        assert_problem(omitted, 422, "idempotency_key_reuse")
        assert len(ledger.dispenses()) == 1

    def test_the_key_belongs_to_the_whole_system_not_to_a_patient(
        self, client: TestClient, rule: FormularyRule
    ) -> None:
        post_dispense(client, patient_ref="PT-1")

        response = post_dispense(client, patient_ref="PT-2")

        assert_problem(response, 422, "idempotency_key_reuse")


class TestSeededKeys:
    """The seed writes an accepted attempt for every dispense it creates. Replaying one of those
    keys must be a replay, which only works if the seed fingerprints a request the way the API
    does."""

    @pytest.fixture
    def seeded(self, engine: Engine) -> Iterator[None]:
        run_seed(
            engine,
            medicines=30,
            dispenses=90,
            patients=40,
            years=1,
            random_seed=20260901,
            reset=True,
            today=date(2026, 6, 15),
        )
        yield
        with engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))

    @staticmethod
    def request_of(item: dict[str, Any]) -> dict[str, Any]:
        """The request that would have created a listed dispense, instant given as listed."""
        return {
            "idempotency_key": item["idempotency_key"],
            "medicine_code": item["medicine_code"],
            "patient_ref": item["patient_ref"],
            "quantity": item["quantity"],
            "dispensed_at": item["dispensed_at"],
            "authorisation_ref": item["authorisation_ref"],
        }

    @pytest.mark.usefixtures("seeded")
    def test_a_seeded_dispense_is_replayed_with_the_body_the_listing_shows(
        self, client: TestClient
    ) -> None:
        items = client.get(DISPENSES, params={"limit": 25}).json()["items"]
        demo = client.get(DISPENSES, params={"patient_ref": "PT-DEMO"}).json()["items"]
        assert len(items) == 25
        assert any(item["authorisation_ref"] is not None for item in demo)

        for item in [*items, *demo]:
            replay = client.post(DISPENSES, json=self.request_of(item))

            assert replay.status_code == 201, replay.text
            assert replay.headers[REPLAYED] == "true"
            assert replay.json() == item

    @pytest.mark.usefixtures("seeded")
    def test_another_payload_under_a_seeded_key_is_a_key_reuse(self, client: TestClient) -> None:
        item = client.get(DISPENSES, params={"limit": 1}).json()["items"][0]

        response = client.post(DISPENSES, json=self.request_of(item) | {"quantity": 1000})

        assert_problem(response, 422, "idempotency_key_reuse")


class TestKeyConflictBackstop:
    """The key lock makes a duplicate wait for the first request and then see its attempt. If a
    request still gets as far as inserting although another one with its key has been committed,
    the unique constraints refuse the insert and the request is answered from the first one."""

    @pytest.fixture
    def stale_lookup(self, monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[str]]:
        """Make the next request's lookup of its attempt miss, as it would if the first request
        had committed just after it. The lookups of the request are recorded."""

        def install() -> list[str]:
            lookups: list[str] = []
            real = repositories.find_attempt_by_key

            def missing_the_first_time(session: Any, idempotency_key: str) -> Any:
                lookups.append(idempotency_key)
                return None if len(lookups) == 1 else real(session, idempotency_key)

            monkeypatch.setattr(repositories, "find_attempt_by_key", missing_the_first_time)
            return lookups

        return install

    def test_a_request_that_would_have_been_accepted_again_gets_the_original_response(
        self,
        client: TestClient,
        rule: FormularyRule,
        stale_lookup: Callable[[], list[str]],
        ledger: Ledger,
    ) -> None:
        first = post_dispense(client, quantity=10)
        lookups = stale_lookup()

        # 10 already dispensed, so 10 more still fit and the loser reaches insert_dispense.
        response = post_dispense(client, quantity=10)

        assert response.status_code == 201
        assert response.headers[REPLAYED] == "true"
        assert response.json() == first.json()
        assert lookups == ["key-1", "key-1"]
        assert len(ledger.dispenses()) == 1
        assert len(ledger.attempts()) == 1

    def test_a_request_that_would_have_been_rejected_gets_the_original_response_too(
        self,
        client: TestClient,
        rule: FormularyRule,
        stale_lookup: Callable[[], list[str]],
        ledger: Ledger,
    ) -> None:
        first = post_dispense(client, idempotency_key="first", quantity=10)
        post_dispense(client, idempotency_key="second", quantity=10)
        post_dispense(client, idempotency_key="third", quantity=10)
        stale_lookup()

        # The patient's 30 days are used up, so judged afresh the request is refused and the loser
        # reaches insert_attempt. The outcome of the first request stands instead.
        response = post_dispense(client, idempotency_key="first", quantity=10)

        assert response.status_code == 201
        assert response.headers[REPLAYED] == "true"
        assert response.json() == first.json()
        assert len(ledger.dispenses()) == 3
        assert len(ledger.attempts()) == 3

    def test_a_rejection_that_won_is_replayed_as_a_rejection(
        self,
        client: TestClient,
        rule: FormularyRule,
        stale_lookup: Callable[[], list[str]],
        ledger: Ledger,
    ) -> None:
        first = post_dispense(client, quantity=40)
        stale_lookup()

        response = post_dispense(client, quantity=40)

        assert response.status_code == 422
        assert response.headers[REPLAYED] == "true"
        assert without_request_id(response.json()) == without_request_id(first.json())
        assert len(ledger.attempts()) == 1

    def test_another_payload_is_a_key_reuse_when_it_loses_the_race(
        self,
        client: TestClient,
        rule: FormularyRule,
        stale_lookup: Callable[[], list[str]],
        ledger: Ledger,
    ) -> None:
        post_dispense(client, quantity=10)
        stale_lookup()

        response = post_dispense(client, quantity=5)

        assert_problem(response, 422, "idempotency_key_reuse")
        assert len(ledger.dispenses()) == 1
        assert len(ledger.attempts()) == 1

    def test_a_dispense_with_the_key_but_no_attempt_is_not_papered_over(
        self,
        error_client: TestClient,
        medicine: Medicine,
        rule: FormularyRule,
        make_dispense: DispenseFactory,
        ledger: Ledger,
    ) -> None:
        # The two rows are always written together, so this cannot happen. If it ever does, the
        # constraint's error must surface instead of an answer made up from nothing.
        make_dispense(medicine, rule, idempotency_key="key-1")

        response = post_dispense(error_client)

        assert_problem(response, 500, "internal_error")
        assert len(ledger.dispenses()) == 1
        assert ledger.attempts() == []

    def test_an_integrity_error_on_another_constraint_is_not_taken_for_a_key_conflict(
        self,
        error_client: TestClient,
        rule: FormularyRule,
        monkeypatch: pytest.MonkeyPatch,
        ledger: Ledger,
    ) -> None:
        real = repositories.insert_dispense

        def without_quantity(session: Any, **fields: Any) -> Dispense:
            # Violates ck_dispenses_quantity_positive, which no request can reach.
            return real(session, **{**fields, "quantity": 0})

        monkeypatch.setattr(repositories, "insert_dispense", without_quantity)

        response = post_dispense(error_client)

        assert_problem(response, 500, "internal_error")
        assert (ledger.dispenses(), ledger.attempts()) == ([], [])
