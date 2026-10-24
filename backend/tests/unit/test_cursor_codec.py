import base64
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.pagination import InvalidCursor, decode_cursor, encode_cursor, filter_fingerprint

FILTERS = {"q": "amox", "patient_ref": None}
KEY = ["2026-03-15T10:00:00.000000Z", 981]


def forge(payload: object) -> str:
    """A token carrying an arbitrary JSON document, spelled the way encode_cursor spells tokens."""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def valid_payload(**changes: Any) -> dict[str, Any]:
    return {"v": 1, "k": KEY, "f": filter_fingerprint(FILTERS)} | changes


class TestFilterFingerprint:
    def test_is_twelve_hex_characters_of_the_sha256_of_the_canonical_filters(self) -> None:
        canonical = b'{"patient_ref":null,"q":"amox"}'

        assert filter_fingerprint(FILTERS) == hashlib.sha256(canonical).hexdigest()[:12]
        assert re.fullmatch(r"[0-9a-f]{12}", filter_fingerprint(FILTERS))

    def test_is_stable_across_releases(self) -> None:
        assert filter_fingerprint(FILTERS) == "56411991982d"

    def test_does_not_depend_on_key_order(self) -> None:
        assert filter_fingerprint({"a": 1, "b": {"x": 1, "y": 2}}) == filter_fingerprint(
            {"b": {"y": 2, "x": 1}, "a": 1}
        )

    @pytest.mark.parametrize(
        "other",
        [
            pytest.param({"q": "amo", "patient_ref": None}, id="different value"),
            pytest.param({"q": "amox", "patient_ref": "PT-1"}, id="value instead of null"),
            pytest.param({"q": "amox"}, id="missing key"),
            pytest.param({"q": "amox", "patient_ref": None, "medicine": None}, id="extra key"),
            pytest.param({"query": "amox", "patient_ref": None}, id="different key name"),
        ],
    )
    def test_distinguishes_different_filters(self, other: dict[str, Any]) -> None:
        assert filter_fingerprint(other) != filter_fingerprint(FILTERS)

    def test_a_null_filter_and_an_absent_filter_are_different_filters(self) -> None:
        assert filter_fingerprint({"q": None}) != filter_fingerprint({})


class TestWireFormat:
    def test_a_token_is_unpadded_base64url_of_a_json_object(self) -> None:
        token = encode_cursor(KEY, FILTERS)

        document = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))

        assert document == {"v": 1, "k": KEY, "f": filter_fingerprint(FILTERS)}

    def test_a_token_issued_by_an_earlier_release_still_decodes(self) -> None:
        issued_earlier = (
            "eyJ2IjoxLCJrIjpbIjIwMjYtMDMtMTVUMTA6MDA6MDAuMDAwMDAwWiIs"
            "OTgxXSwiZiI6IjU2NDExOTkxOTgyZCJ9"
        )

        assert decode_cursor(issued_earlier, FILTERS) == KEY


class TestRoundTrip:
    def test_returns_the_key_that_was_encoded(self) -> None:
        token = encode_cursor(KEY, FILTERS)

        assert decode_cursor(token, FILTERS) == KEY

    def test_token_is_opaque_and_safe_to_put_in_a_url(self) -> None:
        token = encode_cursor(["Amoxil ? > ü", 7], FILTERS)

        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
        assert "Amoxil" not in token

    def test_the_token_for_a_given_page_is_always_the_same(self) -> None:
        assert encode_cursor(KEY, FILTERS) == encode_cursor(
            list(KEY), dict(reversed(FILTERS.items()))
        )

    def test_tuples_come_back_as_lists(self) -> None:
        assert decode_cursor(encode_cursor(("Amoxil", "MED-0001"), {}), {}) == [
            "Amoxil",
            "MED-0001",
        ]

    @settings(deadline=None)
    @given(
        key=st.lists(st.one_of(st.none(), st.integers(), st.text()), max_size=4),
        filters=st.dictionaries(
            st.text(max_size=5),
            st.one_of(st.none(), st.integers(), st.text(max_size=8)),
            max_size=4,
        ),
    )
    def test_round_trips_any_json_safe_key_and_filters(
        self, key: list[Any], filters: dict[str, Any]
    ) -> None:
        token = encode_cursor(key, filters)

        assert re.fullmatch(r"[A-Za-z0-9_-]*", token)
        assert decode_cursor(token, filters) == key


class TestFilterMismatch:
    @pytest.mark.parametrize(
        "other",
        [
            pytest.param({"q": "amo", "patient_ref": None}, id="changed value"),
            pytest.param({"q": "amox"}, id="dropped filter"),
            pytest.param(
                {"q": "amox", "patient_ref": None, "medicine": "MED-1"}, id="added filter"
            ),
            pytest.param({}, id="no filters"),
        ],
    )
    def test_a_cursor_cannot_be_used_with_different_filters(self, other: dict[str, Any]) -> None:
        token = encode_cursor(KEY, FILTERS)

        with pytest.raises(InvalidCursor, match="different filters"):
            decode_cursor(token, other)

    @settings(deadline=None)
    @given(
        issued=st.dictionaries(st.text(max_size=4), st.one_of(st.none(), st.text(max_size=6))),
        requested=st.dictionaries(st.text(max_size=4), st.one_of(st.none(), st.text(max_size=6))),
    )
    def test_only_the_filters_it_was_issued_for_are_accepted(
        self, issued: dict[str, Any], requested: dict[str, Any]
    ) -> None:
        assume(issued != requested)

        with pytest.raises(InvalidCursor):
            decode_cursor(encode_cursor(KEY, issued), requested)


class TestTamperedTokens:
    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(lambda token: token[:-1], id="truncated by one"),
            pytest.param(lambda token: token[: len(token) // 2], id="truncated by half"),
            pytest.param(lambda token: token + "A", id="extra character"),
            pytest.param(lambda token: token + "=", id="padding added"),
            pytest.param(lambda token: token + "\n", id="newline appended"),
            pytest.param(lambda token: "!" + token, id="junk prefix"),
            pytest.param(lambda token: token[:10] + "*" + token[11:], id="character replaced"),
            pytest.param(lambda token: token[:10] + "é" + token[11:], id="non-ascii character"),
            pytest.param(lambda token: token[:10] + " " + token[10:], id="space inserted"),
            pytest.param(lambda token: token.swapcase(), id="case changed"),
            pytest.param(lambda token: "", id="empty"),
            pytest.param(lambda token: "   ", id="blank"),
        ],
    )
    def test_a_damaged_token_is_rejected(self, damage: Callable[[str], str]) -> None:
        token = encode_cursor(KEY, FILTERS)

        with pytest.raises(InvalidCursor):
            decode_cursor(damage(token), FILTERS)

    def test_the_standard_base64_alphabet_is_not_accepted(self) -> None:
        raw = json.dumps(valid_payload(k=["???>>>???>>>", 1]), separators=(",", ":")).encode()
        standard = base64.b64encode(raw).decode().rstrip("=")
        assert "+" in standard or "/" in standard

        with pytest.raises(InvalidCursor):
            decode_cursor(standard, FILTERS)

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(b"not json at all", id="not json"),
            pytest.param(b'{"v":1,"k":[1,"a"', id="truncated json"),
            pytest.param(b"\xff\xfe\x00garbage", id="not text"),
            pytest.param(b"", id="nothing"),
            pytest.param(
                b"[" * 100_000 + b"]" * 100_000, id="nested deeper than the parser allows"
            ),
        ],
    )
    def test_valid_base64_that_is_not_a_json_document_is_rejected(self, raw: bytes) -> None:
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        with pytest.raises(InvalidCursor):
            decode_cursor(token, FILTERS)


class TestWrongVersionOrShape:
    @pytest.mark.parametrize(
        "version",
        [0, 2, -1, "1", 1.0, True, None, [1]],
        ids=["zero", "two", "negative", "string", "float", "bool", "null", "list"],
    )
    def test_an_unsupported_version_is_rejected(self, version: Any) -> None:
        with pytest.raises(InvalidCursor, match="version"):
            decode_cursor(forge(valid_payload(v=version)), FILTERS)

    def test_the_current_version_is_accepted(self) -> None:
        assert decode_cursor(forge(valid_payload()), FILTERS) == KEY

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(None, id="null"),
            pytest.param(1, id="number"),
            pytest.param("cursor", id="string"),
            pytest.param([1, KEY, "x"], id="list"),
            pytest.param({}, id="empty object"),
            pytest.param({"k": KEY, "f": filter_fingerprint(FILTERS)}, id="no version"),
            pytest.param({"v": 1, "f": filter_fingerprint(FILTERS)}, id="no key"),
            pytest.param({"v": 1, "k": KEY}, id="no filter fingerprint"),
            pytest.param(valid_payload() | {"extra": 1}, id="extra field"),
            pytest.param(valid_payload(k="Amoxil"), id="key is a string"),
            pytest.param(valid_payload(k={"name": "Amoxil"}), id="key is an object"),
            pytest.param(valid_payload(k=None), id="key is null"),
            pytest.param(valid_payload(f=123), id="fingerprint is a number"),
            pytest.param(valid_payload(f=None), id="fingerprint is null"),
        ],
    )
    def test_a_document_of_the_wrong_shape_is_rejected(self, payload: object) -> None:
        with pytest.raises(InvalidCursor):
            decode_cursor(forge(payload), FILTERS)


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.sampled_from(["v", "k", "f", "x"]), children, max_size=4)
    ),
    max_leaves=12,
)


class TestNeverRaisesAnythingElse:
    @settings(deadline=None)
    @given(st.text())
    def test_arbitrary_text(self, token: str) -> None:
        with pytest.raises(InvalidCursor):
            decode_cursor(token, FILTERS)

    @settings(deadline=None)
    @given(st.binary())
    def test_base64_of_arbitrary_bytes(self, raw: bytes) -> None:
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        with pytest.raises(InvalidCursor):
            decode_cursor(token, FILTERS)

    @settings(deadline=None)
    @given(
        st.fixed_dictionaries(
            {
                "v": st.one_of(st.just(1), json_values),
                "k": json_values,
                "f": st.one_of(st.just(filter_fingerprint(FILTERS)), json_values),
            }
        )
    )
    def test_a_document_is_accepted_exactly_when_it_is_well_formed(
        self, document: dict[str, Any]
    ) -> None:
        well_formed = (
            type(document["v"]) is int
            and document["v"] == 1
            and isinstance(document["k"], list)
            and document["f"] == filter_fingerprint(FILTERS)
        )

        if well_formed:
            assert decode_cursor(forge(document), FILTERS) == document["k"]
        else:
            with pytest.raises(InvalidCursor):
                decode_cursor(forge(document), FILTERS)
