import hashlib
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.domain.idempotency import request_fingerprint

REQUEST: dict[str, Any] = {
    "medicine_code": "MED-0001",
    "patient_ref": "PT-7K2M9Q",
    "quantity": 20,
    "dispensed_at": datetime(2026, 3, 15, 10, 0, tzinfo=UTC),
    "authorisation_ref": "AUTH-123",
}


def fingerprint(**changes: Any) -> str:
    return request_fingerprint(**{**REQUEST, **changes})


class TestFingerprintContract:
    def test_is_the_sha256_of_the_canonical_json_of_the_normalised_payload(self) -> None:
        canonical = (
            '{"authorisation_ref":"AUTH-123","dispensed_at":"2026-03-15T10:00:00+00:00",'
            '"medicine_code":"MED-0001","patient_ref":"PT-7K2M9Q","quantity":20}'
        )

        assert fingerprint() == hashlib.sha256(canonical.encode()).hexdigest()

    def test_a_missing_dispensed_at_and_authorisation_ref_are_hashed_as_null(self) -> None:
        canonical = (
            '{"authorisation_ref":null,"dispensed_at":null,'
            '"medicine_code":"MED-0001","patient_ref":"PT-7K2M9Q","quantity":20}'
        )

        assert (
            fingerprint(dispensed_at=None, authorisation_ref=None)
            == hashlib.sha256(canonical.encode()).hexdigest()
        )

    def test_digests_never_change_because_stored_attempts_are_compared_against_them(self) -> None:
        assert fingerprint() == "392fadfc4e0eb44385bb9fd544b5efa400a80a11939a563744605e330be9017d"
        assert (
            fingerprint(dispensed_at=None, authorisation_ref=None)
            == "8bcca4e3d1c4dfc3b1777613527d80950c96641ef9ff70fc219ad4b063032e22"
        )

    def test_is_stable_and_independent_of_keyword_order(self) -> None:
        reordered = request_fingerprint(
            authorisation_ref="AUTH-123",
            dispensed_at=REQUEST["dispensed_at"],
            quantity=20,
            patient_ref="PT-7K2M9Q",
            medicine_code="MED-0001",
        )

        assert reordered == fingerprint() == fingerprint()
        assert len(reordered) == 64
        assert reordered == reordered.lower()

    def test_non_ascii_text_is_escaped_in_the_canonical_json(self) -> None:
        canonical = (
            '{"authorisation_ref":"AUTH-123","dispensed_at":"2026-03-15T10:00:00+00:00",'
            '"medicine_code":"MED-0001","patient_ref":"PT-\\u00e9\\u2603","quantity":20}'
        )

        assert fingerprint(patient_ref="PT-é☃") == hashlib.sha256(canonical.encode()).hexdigest()


class TestSensitivity:
    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"medicine_code": "MED-0002"}, id="medicine_code"),
            pytest.param({"patient_ref": "PT-OTHER"}, id="patient_ref"),
            pytest.param({"quantity": 21}, id="quantity"),
            pytest.param(
                {"dispensed_at": datetime(2026, 3, 15, 10, 0, 1, tzinfo=UTC)}, id="seconds"
            ),
            pytest.param(
                {"dispensed_at": datetime(2026, 3, 15, 10, 0, 0, 1, tzinfo=UTC)}, id="microsecond"
            ),
            pytest.param({"dispensed_at": None}, id="dispensed_at omitted"),
            pytest.param({"authorisation_ref": "AUTH-124"}, id="authorisation_ref"),
            pytest.param({"authorisation_ref": None}, id="authorisation_ref omitted"),
        ],
    )
    def test_changing_any_single_field_changes_the_fingerprint(
        self, change: dict[str, Any]
    ) -> None:
        assert fingerprint(**change) != fingerprint()

    def test_an_omitted_dispensed_at_differs_from_any_explicit_one(self) -> None:
        omitted = fingerprint(dispensed_at=None)

        assert omitted == fingerprint(dispensed_at=None)
        assert omitted != fingerprint(dispensed_at=datetime(2026, 3, 15, 10, 0, tzinfo=UTC))
        assert omitted != fingerprint(dispensed_at=datetime(2026, 3, 15, tzinfo=UTC))

    def test_text_fields_cannot_be_shifted_across_their_boundary(self) -> None:
        assert fingerprint(medicine_code="A", patient_ref="BC") != fingerprint(
            medicine_code="AB", patient_ref="C"
        )
        assert fingerprint(patient_ref="12", quantity=3) != fingerprint(
            patient_ref="1", quantity=23
        )


class TestEquivalentSpellings:
    @pytest.mark.parametrize("blank", [None, "", "   ", "\t \n"])
    def test_a_blank_authorisation_ref_means_none(self, blank: str | None) -> None:
        assert fingerprint(authorisation_ref=blank) == fingerprint(authorisation_ref=None)

    def test_authorisation_ref_is_trimmed(self) -> None:
        assert fingerprint(authorisation_ref="  AUTH-123\t") == fingerprint()

    def test_equal_instants_in_different_utc_offsets_are_the_same_request(self) -> None:
        same_moment = [
            datetime(2026, 3, 15, 12, 0, tzinfo=timezone(timedelta(hours=2))),
            datetime(2026, 3, 15, 5, 0, tzinfo=timezone(timedelta(hours=-5))),
            datetime(2026, 3, 15, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        ]

        for instant in same_moment:
            assert fingerprint(dispensed_at=instant) == fingerprint()

    def test_naive_dispensed_at_is_rejected_instead_of_guessed(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            fingerprint(dispensed_at=datetime(2026, 3, 15, 10, 0))


offsets = st.sampled_from(
    [
        UTC,
        timezone(timedelta(hours=2)),
        timezone(timedelta(hours=-5)),
        timezone(timedelta(hours=5.5)),
    ]
)
requests = st.fixed_dictionaries(
    {
        "medicine_code": st.text(max_size=6),
        "patient_ref": st.text(max_size=6),
        "quantity": st.integers(1, 1000),
        "dispensed_at": st.one_of(
            st.none(),
            st.datetimes(
                min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=offsets
            ),
        ),
        "authorisation_ref": st.one_of(st.none(), st.text(max_size=6)),
    }
)


def meaning(request: dict[str, Any]) -> tuple[Any, ...]:
    """What makes two requests the same request, spelled out without the fingerprint code."""
    moment = request["dispensed_at"]
    ref = request["authorisation_ref"]
    return (
        request["medicine_code"],
        request["patient_ref"],
        request["quantity"],
        None if moment is None else (moment - moment.utcoffset()).replace(tzinfo=None),
        None if ref is None else ref.strip() or None,
    )


@settings(deadline=None)
@given(requests, requests)
def test_requests_that_mean_different_things_never_share_a_fingerprint(
    first: dict[str, Any], second: dict[str, Any]
) -> None:
    assume(meaning(first) != meaning(second))

    assert request_fingerprint(**first) != request_fingerprint(**second)


@settings(deadline=None)
@given(requests, offsets, st.text(alphabet=" \t\n", max_size=3))
def test_respelling_a_request_never_changes_its_fingerprint(
    request: dict[str, Any], offset: timezone, padding: str
) -> None:
    respelled = dict(request)
    if request["dispensed_at"] is not None:
        respelled["dispensed_at"] = request["dispensed_at"].astimezone(offset)
    if request["authorisation_ref"] is not None:
        respelled["authorisation_ref"] = padding + request["authorisation_ref"] + padding

    assert request_fingerprint(**respelled) == request_fingerprint(**request)
