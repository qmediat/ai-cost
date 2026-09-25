"""Grok Build CLI sessions: one row per (turn, model) of every session with turns inside the window.

The CLI writes ``<grok_home>/sessions/<working directory, URL-encoded>/<session id>/usage.json``: the session's
totals and one entry per turn (``turns[]`` — ``endedAt``, the counters, a ``modelUsage`` object per model). Read as
verified on Grok Build 1.0.25 (2026-09-21): ``inputTokens`` INCLUDES ``cachedReadTokens`` (the OpenAI-family shape
the pricer reads), ``outputTokens`` EXCLUDES ``reasoningTokens``, which xAI bills as output, ``costUsdTicks`` is the
CLI's own price estimate in 1e-10 USD (the ``total_cost_usd`` a headless run prints, to the cent), ``modelCalls``
the API requests of the turn (the long-context tier applies only to a single request). The directory name is the
working directory the CLI ran in: the row's workspace (ADR-0003, ADR-0006). Nothing in the file says how the
session was paid, so the row takes ``providers.xai.billing`` and stays ``UNKNOWN`` without a rule.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from ..models import Billing, Collected, Provider, RowKind, Scope, Skipped, Tokens, UsageRow, Window
from ..timeutil import parse_ts
from ..values import as_object, count, money
from .files import listing, or_skip

if TYPE_CHECKING:
    from ..plugins import Context

SOURCE_NAME = "grok-build"  # the one name of this source: on every row, on the Source, in every skip
TICKS_PER_USD = 1e10  # costUsdTicks → USD: 3564980000 ticks were the CLI's 0.356498 USD (2026-09-21)


def usage_files(grok_home: Path) -> list[Path]:
    """Every ``usage.json`` under ``<grok_home>/sessions/<cwd>/<session>/``, in directory order."""
    return listing(grok_home / "sessions", "*/*/usage.json")


def _tokens(usage: Mapping[str, Any]) -> Tokens:
    """The counters as the OpenAI-family pricer reads them: input includes the cached reads, output the reasoning."""
    inputs = count(usage.get("inputTokens"), "inputTokens")
    cached = count(usage.get("cachedReadTokens"), "cachedReadTokens")
    if (
        cached > inputs
    ):  # the CLI counts cached reads inside the input: more cached than input is not this shape
        raise ValueError(f"cachedReadTokens {cached} exceed inputTokens {inputs}")
    output = count(usage.get("outputTokens"), "outputTokens")
    reasoning = count(usage.get("reasoningTokens"), "reasoningTokens")
    return Tokens(
        input=inputs,
        cached_input=cached,
        output=output + reasoning,
        requests=count(usage.get("modelCalls"), "modelCalls"),
    )


def _cost(usage: Mapping[str, Any]) -> float | None:
    """``costUsdTicks`` as USD; absent or zero (the CLI did not price the turn) is ``None``: the list price applies."""
    ticks = money(usage.get("costUsdTicks"), "costUsdTicks")
    return ticks / TICKS_PER_USD if ticks is not None and ticks > 0 else None


def _model_usages(turn: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """``(model, counters)`` per model of a turn; a turn without ``modelUsage`` is one entry under its primary model."""
    by_model = as_object(turn.get("modelUsage"), "modelUsage")
    if by_model:
        return [(str(name), as_object(entry, f"modelUsage.{name}")) for name, entry in by_model.items()]
    return [(str(turn.get("primaryModelId") or "unknown"), turn)]


def _turn_rows(turn: Mapping[str, Any], session: str, scope: Scope, billing: Billing) -> list[UsageRow]:
    """The rows of one turn, one per model — all of them or none: a bad counter anywhere is the caller's counted skip.

    A turn nobody can place in time (no ``endedAt``) is a ``ValueError`` as well: it is never guessed into a day.
    """
    at = parse_ts(turn.get("endedAt"))
    if at is None:
        raise ValueError(f"endedAt missing or not a time: {turn.get('endedAt')!r}")
    return [
        UsageRow(
            provider=Provider.XAI,
            model=model,
            kind=RowKind.SESSION,
            source=SOURCE_NAME,
            at=at,
            ref=session,
            billing=billing,
            tokens=_tokens(usage),
            cost_reported=_cost(usage),
            scope=scope,
        )
        for model, usage in _model_usages(turn)
    ]


def _turns(document: Mapping[str, Any], path: Path) -> list[Any]:
    """The ``turns`` list; a file without turns but with a ``session`` block is one turn ended at ``updatedAt``."""
    turns = document.get("turns")
    if turns is None:
        turns = []
    if not isinstance(turns, list):
        raise ValueError(f"turns must be a list, got {type(turns).__name__}")
    if turns:
        return turns
    session = as_object(document.get("session"), "session")
    if not session:
        return []
    stamp = document.get("updatedAt")  # the file's own stamp when it is one, else when the file was written
    ended = stamp if parse_ts(stamp) is not None else path.stat().st_mtime
    return [{**session, "endedAt": ended}]


def _collect_file(path: Path, window: Window, billing: Billing, skipped: list[Skipped]) -> list[UsageRow]:
    """The rows of one session file inside the window; a bad turn is a counted skip, the other turns still count."""
    document = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(document, Mapping):
        raise ValueError("not a JSON object")
    session = str(document.get("sessionId") or path.parent.name)
    scope = Scope(workspace=unquote(path.parent.parent.name))  # the working directory the CLI ran in
    rows: list[UsageRow] = []
    for index, turn in enumerate(_turns(document, path), 1):
        try:
            turn_rows = _turn_rows(as_object(turn, f"turn {index}"), session, scope, billing)
        except (TypeError, ValueError) as exc:  # one bad turn is one counted skip; the rest still counts
            skipped.append(Skipped(SOURCE_NAME, str(path), f"turn {index}: {exc}"))
            continue
        rows += [row for row in turn_rows if window.contains(row.at)]
    return rows


def collect_grok_build(grok_home: Path, window: Window, billing: Billing = Billing.UNKNOWN) -> Collected:
    """Rows of every Grok Build session with turns inside the window; a file last written before it is not read."""
    rows: list[UsageRow] = []
    skipped: list[Skipped] = []
    min_mtime = (window.start - timedelta(minutes=1)).timestamp()
    for path in or_skip(lambda: usage_files(grok_home), SOURCE_NAME, grok_home / "sessions", skipped):
        try:
            if path.stat().st_mtime < min_mtime:
                continue
            rows += _collect_file(path, window, billing, skipped)
        except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
            skipped.append(
                Skipped(SOURCE_NAME, str(path), f"unusable: {exc}")
            )  # unreadable, not JSON, no object
    return Collected(rows=rows, skipped=skipped)


class GrokBuildSource:
    """The sessions under ``<grok_home>/sessions``; ``providers.xai.billing`` is the rule (the file says nothing)."""

    name = SOURCE_NAME

    def collect(self, ctx: Context) -> Collected:
        """Grok Build rows inside the window, billed by the configured rule."""
        return collect_grok_build(ctx.paths.grok_home, ctx.window, Billing.rule(ctx.config.xai_billing))
