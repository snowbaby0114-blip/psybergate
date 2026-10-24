"""advisory_key, the 64-bit key behind every advisory lock. It is pure, so no database is needed;
the locks themselves are tested against PostgreSQL in tests/test_locking.py."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.locking import advisory_key

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

parts = st.lists(st.one_of(st.text(max_size=8), st.integers(0, 10**12)), max_size=3)


class TestKnownValues:
    """Values worked out with hashlib.blake2b(name, digest_size=8), name being the namespace and
    the parts joined by the unit separator. They never change: a release that computed another key
    for the same pair would stop excluding the instances still running the previous release."""

    @pytest.mark.parametrize(
        ("namespace", "key_parts", "expected"),
        [
            ("idem", ("abc",), -3675527241415653118),
            ("idem", ("",), -1825574783283376031),
            ("idem", (), 84969342855986938),
            ("idem", ("kéy",), 9060475819366580580),
            ("pair", (1, "PT-1"), 8970263771744122699),
            ("pair", (2, "PT-1"), -662164116306338692),
            ("pair", (1, "PT-2"), 7407871211008365028),
        ],
    )
    def test_are_pinned(
        self, namespace: str, key_parts: tuple[str | int, ...], expected: int
    ) -> None:
        assert advisory_key(namespace, *key_parts) == expected


class TestSeparation:
    def test_the_namespace_keeps_lock_families_apart(self) -> None:
        assert advisory_key("idem", 1, "PT-1") == -6155586341099691948
        assert advisory_key("idem", 1, "PT-1") != advisory_key("pair", 1, "PT-1")

    def test_parts_cannot_be_shifted_across_their_boundary(self) -> None:
        assert advisory_key("pair", 12, 3) != advisory_key("pair", 1, 23)
        assert advisory_key("idem", "a", "b") != advisory_key("idem", "ab")

    def test_no_part_is_not_the_same_as_an_empty_part(self) -> None:
        assert advisory_key("idem") != advisory_key("idem", "")

    def test_every_part_matters(self) -> None:
        assert advisory_key("pair", 1, "PT-1") != advisory_key("pair", 2, "PT-1")
        assert advisory_key("pair", 1, "PT-1") != advisory_key("pair", 1, "PT-2")

    def test_a_part_counts_as_the_text_it_is_written_as(self) -> None:
        assert advisory_key("pair", 7, "PT-1") == advisory_key("pair", "7", "PT-1")


class TestFitsABigint:
    @given(st.text(max_size=8), parts)
    def test_is_always_a_signed_64_bit_integer(
        self, namespace: str, key_parts: list[str | int]
    ) -> None:
        assert INT64_MIN <= advisory_key(namespace, *key_parts) <= INT64_MAX

    def test_uses_the_whole_signed_range(self) -> None:
        keys = [advisory_key("idem", f"key-{number}") for number in range(200)]

        assert min(keys) < -(2**62)
        assert max(keys) > 2**62
