"""The usage log (ADR-0005): one JSON line per LLM request, written by any program, priced like every other source.

A line names ``provider`` and ``model`` and carries the counters the provider's formula reads (``pricing.counters_of``)
and / or ``cost`` in USD. Every value is validated: a boolean, a negative, fractional or unbounded count, a counter the
provider would not price, a currency other than USD, a malformed or non-string id or a missing required key makes
the line a counted skip. A well-formed provider or model the pricebook does not list is an
unpriced row when the line carries no cost — the report's ``--unpriced`` rule decides (fail by default, ``skip``
counts it). A repeated ``event_id`` within one file is read once.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models import (
    Billing,
    Collected,
    PriceEntry,
    Provider,
    RowKind,
    Scope,
    Skipped,
    Tokens,
    UsageRow,
    Window,
)
from ..pricing import as_tier_family, counters_of, folds_to_tiers
from ..timeutil import parse_ts
from ..values import count, money

if TYPE_CHECKING:
    from ..config import PriceBook
    from ..plugins import Context

SCHEMA = 1
REQUIRED = ("schema", "at", "provider", "model")
COUNTERS = frozenset(f.name for f in fields(Tokens) if f.type in ("int", int))
ALIASES = {
    "thoughts": "output"
}  # a Gemini "thoughts" counter is output, as the Gemini CLI collector counts it
_NOT_PER_REQUEST = frozenset({"requests", "reviews"})  # counted by the tool, never logged by a caller
PER_REQUEST = (
    COUNTERS - _NOT_PER_REQUEST
)  # what a line may carry: a tool-counted key is unknown to the reader
LOG_COUNTERS = tuple(f.name for f in fields(Tokens) if f.name in PER_REQUEST) + tuple(ALIASES)
_BILLING = {"api": Billing.API, "subscription": Billing.SUBSCRIPTION}


def _counts(raw: Any, unknown: set[str]) -> dict[str, Any]:
    """The counters of one line by the reader's names; unknown keys are collected (one warning per file), wrong values are errors."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"tokens must be an object, got {type(raw).__name__}")
    counts: dict[str, Any] = (
        {}
    )  # Any: Tokens mixes int, float and Mapping fields, only int ones are accepted here
    for key, value in raw.items():
        name = ALIASES.get(str(key), str(key))
        if name not in PER_REQUEST:
            unknown.add(str(key))
            continue
        if value is None:  # a present null is not "absent": the writer meant something and it is not a count
            raise ValueError(f"{key} is null: a counter is a whole number or absent")
        counts[name] = counts.get(name, 0) + count(
            value, str(key)
        )  # exact, non-negative, bounded: the writer's rule
    return counts


def _priced_counters(provider: Provider, entry: PriceEntry | None, given: frozenset[str]) -> None:
    """A counter the row's pricing never reads is a writer's error: said, never silently priced at zero."""
    consumed = counters_of(provider, entry)
    if consumed is None:
        return
    stray = sorted(given - consumed)
    if stray:
        reads = ", ".join(sorted(consumed)) or "no token counter (give a cost)"
        raise ValueError(f"{provider.value} prices {reads}: {', '.join(stray)} would not be priced")


def _cost(value: Any) -> float | None:
    """The line's own cost: a finite non-negative number (a huge integer or a string is a ``ValueError``)."""
    if isinstance(value, str):
        raise ValueError(f"cost must be a number, got {value!r}")
    amount = money(
        value, "cost"
    )  # bools and non-numbers: ValueError; an integer beyond float range: ValueError
    if amount is not None and amount < 0:
        raise ValueError(f"cost must be non-negative, got {value!r}")
    return amount


def _billing(entry: Mapping[str, Any]) -> Billing:
    raw = entry.get("billing")
    if raw is None:
        return (
            Billing.API
        )  # a line without a billing key: pay-per-token (parse_line refused one with nothing to price)
    if (
        not isinstance(raw, str) or raw not in _BILLING
    ):  # a list or an object is unhashable: said as a billing error
        raise ValueError(f"billing must be api or subscription, got {raw!r}")
    return _BILLING[raw]


def _scope(entry: Mapping[str, Any]) -> Scope:
    tags = entry.get("tags")
    tags = () if tags is None else tags
    if not isinstance(tags, (list, tuple)) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a list of strings")  # as the writer's Attribution requires
    return Scope(
        branch=_optional_text(entry, "branch"),
        pr=_optional_text(entry, "pr"),
        paths=tuple(tags),
    )


def _text(entry: Mapping[str, Any], key: str) -> str:
    """A required id as the string it must be: a number is not a provider or a model, however it would print."""
    value = entry[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {type(value).__name__}")
    return value


def _optional_text(entry: Mapping[str, Any], key: str) -> str:
    """An optional key as the writer writes it: a string, or absent; a list or a number is a ``ValueError``."""
    value = entry.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {type(value).__name__}")
    return value


def _header(entry: Mapping[str, Any]) -> datetime:
    """The required keys, the exact schema, USD only; the line's timestamp."""
    missing = [key for key in REQUIRED if entry.get(key) is None or entry.get(key) == ""]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    schema = entry["schema"]
    if (
        isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA
    ):  # true == 1 in Python, not here
        raise ValueError(f"schema {schema!r} is not {SCHEMA}")
    currency = entry.get("currency")
    if currency is not None and currency != "USD":
        raise ValueError(f"currency {currency!r} is not USD")
    at = parse_ts(entry["at"])
    if at is None:
        raise ValueError(f"at is not a timestamp: {entry['at']!r}")
    return at


def parse_line(entry: Any, source_name: str, unknown: set[str], book: PriceBook | None = None) -> UsageRow:
    """One log line as a row; every shape problem is a ``ValueError`` naming the key.

    With the pricebook, the counters are checked against the model's own price shape; without it, against the
    provider's built-in formula.
    """
    if not isinstance(entry, Mapping):
        raise ValueError("not an object")
    at = _header(entry)
    provider = Provider.of(_text(entry, "provider"))
    cost = _cost(entry.get("cost"))
    counts = _counts(entry.get("tokens"), unknown)
    if cost is None and not any(counts.values()):
        raise ValueError("neither tokens nor cost")
    model = _text(entry, "model")
    priced_as = book.entry(provider, model) if book is not None else None
    if folds_to_tiers(provider, priced_as):
        counts = as_tier_family(
            counts
        )  # DeepSeek's own cache counters of a tiers-priced model: the writer's rule is the reader's
    _priced_counters(provider, priced_as, frozenset(counts))
    tokens = Tokens(**counts)
    return UsageRow(
        provider=provider,
        model=model,
        kind=RowKind.LOG,
        source=_optional_text(entry, "source")
        or "usage-log",  # the reporting program's own name when it gave one
        at=at,
        ref=_optional_text(entry, "ref") or _optional_text(entry, "session") or source_name,
        billing=_billing(entry),
        tokens=tokens,
        cost_reported=cost,
        scope=_scope(entry),
    )


def _event_id(entry: Mapping[str, Any]) -> str:
    """The writer's id for this line; absent is ``""``; anything but a string is an error (5 and "5" would collide)."""
    event = entry.get("event_id")
    if event is None:
        return ""
    if not isinstance(event, str):
        raise ValueError(f"event_id must be a string, got {type(event).__name__}")
    return event


def _outside(entry: Any, window: Window | None) -> bool:
    """Whether a line that could not be read says, by its ``at``, that it belongs to another window."""
    if window is None or not isinstance(entry, Mapping):
        return False
    at = parse_ts(entry.get("at"))
    return at is not None and not window.contains(at)


def _lines(path: Path) -> Iterator[tuple[int, str]]:
    with path.open(
        encoding="utf-8", errors="replace"
    ) as handle:  # the writer's encoding, whatever the locale
        for line_no, line in enumerate(handle, 1):
            if line.strip():
                yield line_no, line


def _collect_file(
    path: Path,
    window: Window | None,
    skipped: list[Skipped],
    warnings: list[str],
    book: PriceBook | None = None,
) -> list[UsageRow]:
    rows: list[UsageRow] = []
    seen: set[str] = set()
    unknown: set[str] = set()
    for line_no, line in _lines(path):
        entry: Any = None  # this line's, never the previous one's
        try:
            entry = json.loads(line)
            row = parse_line(entry, path.stem, unknown, book)
            event = _event_id(entry)
        except (ValueError, TypeError, RecursionError) as exc:  # RecursionError: nested beyond any reason
            if not _outside(entry, window):  # a bad line of another window is not this window's skip
                skipped.append(Skipped("usage-log", str(path), f"line {line_no}: {exc}"))
            continue
        if window is not None and not window.contains(row.at):
            continue  # outside the window: neither counted nor remembered, so it never consumes an id
        if event and event in seen:
            continue
        if event:
            seen.add(event)
        rows.append(row)
    if unknown:
        warnings.append(f"{path}: unknown token keys ignored: {', '.join(sorted(unknown))}")
    return rows


def collect_usage_log(
    paths: list[Path], window: Window | None, book: PriceBook | None = None
) -> tuple[Collected, list[str]]:
    """Rows of every configured usage log inside the window, plus one warning per file with unknown keys."""
    rows: list[UsageRow] = []
    skipped: list[Skipped] = []
    warnings: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            rows += _collect_file(path, window, skipped, warnings, book)
        except OSError as exc:
            skipped.append(Skipped("usage-log", str(path), f"cannot read: {exc}"))
    return Collected(rows=rows, skipped=skipped), warnings


def _readable_key(path: Path) -> Path:
    """The resolved location of ``path``; raises for a symlink loop in any component or a dangling link.

    Python 3.13+ resolves a loop without raising, but ``stat()`` still fails on it (ELOOP). A merely absent file
    below real directories is normal for a configured log and is returned as is; a dangling link in any component
    of the path is unreadable and raises.
    """
    key = path.resolve()
    try:
        path.stat()
    except FileNotFoundError:
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise  # a dangling link somewhere in the path: unreadable, never a plain absent file
    return key


def _unique(paths: list[Path], skipped: list[Skipped]) -> list[Path]:
    """The paths in order, each resolved location once; a symlink that leads nowhere (a loop, a dangling link) is a skip."""
    seen: set[Path] = set()
    kept: list[Path] = []
    for path in paths:
        try:
            key = _readable_key(path)
        except (OSError, RuntimeError) as exc:  # RuntimeError: a symlink loop on Python < 3.13
            skipped.append(Skipped("usage-log", str(path), f"cannot resolve: {exc}"))
            continue
        if key not in seen:
            seen.add(key)
            kept.append(path)
    return kept


class UsageLogSource:
    """The default usage log (``AI_COST_USAGE_LOG`` / the XDG data dir) plus every file in config ``usage_logs``."""

    name = "usage-log"

    def collect(self, ctx: Context) -> Collected:
        """Rows of every usage log inside the window; unknown counter keys become report warnings."""
        from ..log import default_path

        default = (
            ctx.paths.usage_log or default_path()
        )  # the Paths given to the report, never the process env
        listed = [default, *(Path(p).expanduser() for p in ctx.config.usage_logs)]
        paths = _unique(
            listed, ctx.skipped
        )  # the default file named again in the config is read once, not twice
        collected, warnings = collect_usage_log(paths, ctx.window, ctx.book)
        ctx.warnings.extend(warnings)
        return collected
