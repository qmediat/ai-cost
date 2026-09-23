"""Value rules every collector shares: token counts, amounts of money, and skip bookkeeping.

A counter or an amount that is not what it claims to be (a boolean, a non-finite float, a string that is not a
number) is a ``ValueError``/``TypeError`` for the caller to turn into a counted skip — never a silent 0 or 1.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .models import Skipped

MAX_COUNT = 2**53  # the largest count float arithmetic keeps exact; 9e15 tokens is beyond any real usage


def count(value: Any, name: str = "token count") -> int:
    """A token counter as an exact int; absent is 0.

    A boolean or a non-numeric shape is a ``TypeError``; a fraction, a non-finite float, a string that is not an
    integer, a negative or a value above ``MAX_COUNT`` is a ``ValueError`` — counts are discrete and bounded, nothing is
    truncated or rounded, and nothing can overflow the float arithmetic of pricing or rendering later.
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite, got {value!r}")
        if not value.is_integer():
            raise ValueError(f"{name} must be a whole number, got {value!r}")
    try:
        exact = int(value.strip()) if isinstance(value, str) else int(value)
    except ValueError as exc:  # "1.5" / "x": the message names the counter, not Python's parser
        raise ValueError(f"{name} must be a whole number, got {value!r}") from exc
    if exact < 0:
        raise ValueError(f"{name} must be non-negative, got {exact}")
    if exact > MAX_COUNT:
        raise ValueError(f"{name} must be at most {MAX_COUNT}, got {exact}")
    return exact


def as_object(value: Any, name: str) -> Mapping[str, Any]:
    """A JSON part as an object; absent (``None``) is an empty object, any other shape is a ``TypeError``."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object, got {type(value).__name__}")
    return value


def object_at(raw: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    """The nested object at ``keys`` for lenient readers; a missing, null or non-object step is an empty object."""
    node: Any = raw
    for key in keys:
        node = node.get(key) if isinstance(node, Mapping) else None
    return node if isinstance(node, Mapping) else {}


def money(value: Any, name: str) -> float | None:
    """An amount in USD: a number or a numeric string, finite; absent is ``None``; anything else a ``ValueError``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    try:
        amount = float(value)
    except OverflowError as exc:  # an integer too large for a float
        raise ValueError(f"{name} must be finite, got {value!r}") from exc
    except (
        ValueError
    ) as exc:  # a string that is not a number: the message names the field, not Python's parser
        raise ValueError(f"{name} must be a number, got {value!r}") from exc
    if not math.isfinite(amount):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return amount


def merge_skips(into: list[Skipped], fresh: list[Skipped]) -> None:
    """Add the skips another pass over the same files has not recorded yet (same path and reason = one skip)."""
    seen = {(s.path, s.reason) for s in into}
    for skip in fresh:
        key = (skip.path, skip.reason)
        if key not in seen:
            seen.add(key)
            into.append(skip)
