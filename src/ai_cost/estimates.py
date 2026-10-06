"""Amounts a source knows only as estimates (ADR-0010): kept apart from every cost of the report.

An estimate never enters a group, a total, a per-model line or an attribution line; the report shows it in a cell of
its own and the JSON in an object of its own (``estimates``). An estimate is tied to no project: only a report of every
project shows it, and a session report the estimates of runs that session launched; every other one is listed among
what the report left out, with its amount. A plugin's estimate is checked field by field before any report reads it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .models import Estimate, Provider


@dataclass(frozen=True)
class Estimates:
    """What a report read as estimates: the ones in its scope, and the ones a session report left out."""

    shown: tuple[Estimate, ...] = ()
    left_out: tuple[Estimate, ...] = ()  # a session report's: runs of another session, or of none known

    @property
    def shown_usd(self) -> Decimal:
        """The shown estimates' sum: an estimate, never a cost."""
        return sum((item.usd for item in self.shown), Decimal(0))

    @property
    def left_out_usd(self) -> Decimal:
        """The left-out estimates' sum."""
        return sum((item.usd for item in self.left_out), Decimal(0))


def valid(item: object) -> Estimate:
    """``item`` when it is an estimate a report can show and never sum; ``TypeError`` / ``ValueError`` otherwise.

    The amount is a finite ``Decimal`` not below zero, the time an aware ``datetime``, the provider a ``Provider``,
    every other field text: a wrong one would fail the report later, in its rendering.
    """
    if not isinstance(item, Estimate):
        raise TypeError(f"an estimate must be an Estimate, got {type(item).__name__}")
    if not isinstance(item.provider, Provider) or not isinstance(item.usd, Decimal):
        raise TypeError(f"estimate {item.ref!r}: provider must be a Provider and usd a Decimal")
    if not item.usd.is_finite() or item.usd < 0:
        raise ValueError(f"estimate {item.ref!r}: usd must be finite and not below zero, got {item.usd}")
    if not isinstance(item.at, datetime) or item.at.utcoffset() is None:
        raise TypeError(f"estimate {item.ref!r}: at must be a datetime with a time zone")
    if not all(isinstance(getattr(item, name), str) for name in ("ref", "what", "why", "session")):
        raise TypeError(f"estimate {item.ref!r}: ref, what, why and session must be text")
    return item


def scoped(found: Sequence[Estimate], session: str, project: str = "") -> Estimates | None:
    """The report's estimates, once each; ``session`` (a session report's id) keeps the runs it launched.

    ``project`` (a per-project report's directory) keeps none: an estimate names no working directory, so it belongs
    to no project. None when there is none: a report without an estimate reads as it always did.
    """
    unique = tuple(dict.fromkeys(found))
    if not unique:
        return None
    if project:
        return Estimates((), unique)
    if not session:
        return Estimates(unique)
    mine = tuple(item for item in unique if item.session == session)
    return Estimates(mine, tuple(item for item in unique if item.session != session))


def amount(value: Decimal) -> str:
    """An estimate's amount as text, exact, without trailing zeros (``5.000000000`` → ``5``)."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def left_out_warnings(estimates: Estimates | None, project: str = "") -> list[str]:
    """The header line for the estimates a report left out: how many, their amount, why they are not here.

    ``project``: a per-project report, which leaves every estimate out; otherwise a session report's.
    """
    if estimates is None or not estimates.left_out:
        return []
    count, total = len(estimates.left_out), amount(estimates.left_out_usd)
    tail = f": {total} USD — an estimate, not a cost; in no sum of this report"
    if project:
        return [f"{count} estimated run(s) in the span not tied to this project (--project {project}){tail}"]
    others = sum(1 for item in estimates.left_out if item.session)
    return [
        f"{count} estimated run(s) in the span not tied to this session ({others} of other "
        f"sessions, {count - others} tied to none){tail}"
    ]
