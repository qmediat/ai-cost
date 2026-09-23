"""The shared value rules: exact counts, finite amounts, object guards, skip merging."""

from __future__ import annotations

from ..models import Skipped
from ..values import MAX_COUNT, as_object, count, merge_skips, money, object_at


def test_count_is_exact_and_rejects_fractions_booleans_and_junk() -> None:
    assert count(None) == 0 and count(12) == 12 and count(2.0) == 2 and count(" 7 ") == 7
    for bad, error in (
        (True, TypeError),
        ([1], TypeError),
        (1.5, ValueError),
        ("1.5", ValueError),
        ("x", ValueError),
    ):
        try:
            count(bad, "n")
        except error:
            continue
        raise AssertionError(f"{bad!r} must raise {error.__name__}")
    try:
        count(float("inf"), "n")
    except ValueError as exc:
        assert "finite" in str(exc)
    else:
        raise AssertionError("an infinite count must raise ValueError")


def test_count_is_bounded_so_nothing_downstream_overflows() -> None:
    assert count(MAX_COUNT) == MAX_COUNT and count(str(MAX_COUNT)) == MAX_COUNT and count(0) == 0
    for negative in (-1, -1e6, "-3"):
        try:
            count(negative, "n")
        except ValueError as exc:
            assert "non-negative" in str(exc), str(exc)
            continue
        raise AssertionError(f"{negative!r} must be rejected: a negative count would price negative USD")
    for huge in (MAX_COUNT + 1, 10**400, "9" * 400, 1e300):
        try:
            count(huge, "n")
        except ValueError as exc:
            assert "at most" in str(exc), str(exc)
            continue
        raise AssertionError(f"{huge!r} must be rejected")


def test_money_accepts_numbers_and_numeric_strings_only() -> None:
    assert money(None, "c") is None and money("0.25", "c") == 0.25 and money(3, "c") == 3.0
    for bad in (True, "n/a", float("nan"), [1], 10**400):
        try:
            money(bad, "c")
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} must be a ValueError")


def test_object_guards() -> None:
    assert as_object(None, "x") == {} and as_object({"a": 1}, "x") == {"a": 1}
    try:
        as_object([1], "x")
    except TypeError as exc:
        assert "x must be an object" in str(exc)
    else:
        raise AssertionError("a list is not an object: TypeError expected")
    assert object_at({"a": {"b": None}}, "a", "b", "c") == {} and object_at(
        {"a": {"b": {"c": 1}}}, "a", "b"
    ) == {"c": 1}


def test_merge_skips_dedupes_inside_the_fresh_list_too() -> None:
    into = [Skipped("s", "p", "r")]
    merge_skips(into, [Skipped("s", "p", "r"), Skipped("s", "q", "r"), Skipped("s", "q", "r")])
    assert [(s.path, s.reason) for s in into] == [("p", "r"), ("q", "r")]
