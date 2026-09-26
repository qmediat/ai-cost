"""Reconcile the local count of one provider with the figure its console or export shows for the same window.

``ai-cost reconcile --provider xai --usd 156.11 [--tokens 80200000] --hours 24`` compares the cash the real
group attributes to the provider (its API rows: a CLI's own figure where the config trusts it, else the list price)
and the tokens it billed with what the person read off the provider's console; the gap in percent is judged
against ``reconcile.tolerance_pct`` (5 by default) and appended to ``<state dir>/reconcile.jsonl``, so ``doctor``
shows the last one. A gap above the tolerance is exit 1: the window, a source the tool does not read or a price it
applies differently — something to look at, never a silent number. Nothing is fetched: no provider offers one
public spend endpoint every user could call, so the figure comes from the person (or a script that reads an export).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace

from .config import Config, Paths, PriceBook
from .errors import ToolError
from .models import Billing, RowKind, Tokens, UsageRow, client_outside
from .ops import Emit, ReportRequest, append_json_line, build_report
from .render import plain
from .timeutil import iso, now

HISTORY = "reconcile.jsonl"


@dataclass(frozen=True)
class Reported:
    """What the provider's console or export shows for the window: the amount, and the tokens when it shows them."""

    provider: str
    usd: float
    tokens: int | None = None


@dataclass(frozen=True)
class Reconciliation:
    """The local count of one provider against the figure reported for the same window."""

    provider: str
    window: tuple[str, str]
    hours: float
    rows: int
    local_usd: float
    local_tokens: int
    reported_usd: float
    reported_tokens: int | None
    tolerance_pct: float
    checked_at: str = ""
    unknown_billing: int = 0  # rows of the provider the real group could not attribute to a key or a plan
    outside_scope: int = (
        0  # of those, rows of clients the config puts outside the tracked work (never a rule's)
    )
    plan_rows: int = 0  # rows a subscription paid: not in a console's API figure, so not in the local count
    skipped: int = 0  # records the collectors could not use in the window (doctor lists them)
    warnings: tuple[str, ...] = ()  # the report's own header warnings: what the local count is built on

    @property
    def usd_gap_pct(self) -> float:
        """``local − reported`` as a percentage (``gap_pct``)."""
        return gap_pct(self.local_usd, self.reported_usd)

    @property
    def tokens_gap_pct(self) -> float | None:
        """The same for the tokens, when a figure was reported."""
        return None if self.reported_tokens is None else gap_pct(self.local_tokens, self.reported_tokens)

    @property
    def within_tolerance(self) -> bool:
        """Whether the USD gap, either way, is at most the configured tolerance."""
        return abs(self.usd_gap_pct) <= self.tolerance_pct


def gap_pct(local: float, reported: float) -> float:
    """``(local − reported)`` as a percentage of the reported figure; of the local one when nothing was reported."""
    base = reported if reported > 0 else local
    return 0.0 if base <= 0 else (local - reported) / base * 100.0


def billed_tokens(tokens: Tokens) -> int:
    """Every token a provider bills, counted once.

    ``cached_input`` sits inside ``input`` and ``cached`` inside ``prompt`` (the OpenAI and Google families), so
    neither is added; Anthropic's cache reads and writes and DeepSeek's hits and misses are their own counters.
    """
    return (
        tokens.input
        + tokens.output
        + tokens.cache_read
        + tokens.cache_write_5m
        + tokens.cache_write_1h
        + tokens.cache_write_unsplit
        + tokens.prompt
        + tokens.cache_hit
        + tokens.cache_miss
    )


def reconcile(
    request: ReportRequest, paths: Paths, config: Config, book: PriceBook, reported: Reported
) -> Reconciliation:
    """The provider's local count for the request's window against the reported figures; nothing is written."""
    whole = replace(request, all_projects=True, groups=("real", "api"), unpriced="skip")
    report = build_report(whole, paths, config, book)
    provider = reported.provider
    lines = [line for line in (report.real.usage if report.real else []) if line.provider.value == provider]
    rows = [row for row in report.rows if row.provider.value == provider]
    api_rows = [row for row in rows if _console_bills(row)]
    return Reconciliation(
        provider=provider,
        window=report.window_iso,
        hours=report.window.hours(),
        rows=len(api_rows),
        local_usd=sum(line.usd for line in lines),
        local_tokens=sum(billed_tokens(row.tokens) for row in api_rows),
        reported_usd=reported.usd,
        reported_tokens=reported.tokens,
        tolerance_pct=config.reconcile_tolerance_pct,
        checked_at=iso(now()),
        unknown_billing=sum(row.billing is Billing.UNKNOWN for row in rows),
        outside_scope=sum(
            row.billing is Billing.UNKNOWN and client_outside(row.client, config.outside_scope_clients)
            for row in rows
        ),
        plan_rows=sum(row.billing is Billing.SUBSCRIPTION for row in rows),
        skipped=len(report.skipped),
        warnings=tuple(report.warnings),
    )


def _console_bills(row: UsageRow) -> bool:
    """A row whose tokens a console's API figure covers: paid per use, and not a ledger line.

    A ledger row carries the cash of calls the session rows already count (the API group skips it the same way);
    its counters, when it has any, would count those tokens twice.
    """
    return row.billing in (Billing.API, Billing.API_SETTLED) and row.kind is not RowKind.LEDGER


def _thousands(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def describe(outcome: Reconciliation) -> list[str]:
    """The comparison as lines: the window, both figures, the gap against the tolerance, the verdict."""
    tokens_gap = outcome.tokens_gap_pct
    reported = f"  reported  {outcome.reported_usd:.2f} USD"
    if outcome.reported_tokens is not None:
        reported += f"  {_thousands(outcome.reported_tokens)} tokens"
    gap = f"  gap       {outcome.usd_gap_pct:+.2f} % USD (tolerance {outcome.tolerance_pct:g} %)"
    if tokens_gap is not None:
        gap += f" · {tokens_gap:+.2f} % tokens"
    verdict = (
        "within tolerance"
        if outcome.within_tolerance
        else "ABOVE TOLERANCE — check the window and the sources"
    )
    lines = [
        f"reconcile {outcome.provider} {outcome.window[0]} → {outcome.window[1]} ({outcome.hours:.1f} h)",
        f"  local     {outcome.local_usd:.2f} USD  {_thousands(outcome.local_tokens)} tokens  "
        f"({outcome.rows} API row(s))",
        reported,
        gap,
    ]
    return [*lines, *_notes(outcome), f"reconcile: {verdict}"]


def _notes(outcome: Reconciliation) -> list[str]:
    """What the local count leaves out or is built on: plan rows, unknown rows, skipped records, the report's warnings."""
    notes: list[str] = []
    if outcome.plan_rows:
        notes.append(
            f"  note      {outcome.plan_rows} plan row(s) are not counted: a console shows API usage only"
        )
    in_scope = outcome.unknown_billing - outcome.outside_scope
    if in_scope:
        notes.append(
            f"  note      {in_scope} row(s) of unknown billing are neither in the local USD nor in "
            f"the tokens — set providers.{outcome.provider}.billing"
        )
    if outcome.outside_scope:
        notes.append(
            f"  note      {outcome.outside_scope} row(s) of clients outside scope are left unknown on purpose: "
            "neither in the local USD nor in the tokens"
        )
    if outcome.skipped:
        notes.append(
            f"  note      {outcome.skipped} record(s) were skipped while collecting — doctor lists them"
        )
    notes.extend(f"  warn      {text}" for text in outcome.warnings)
    return notes


def run_reconcile(
    request: ReportRequest, paths: Paths, config: Config, book: PriceBook, reported: Reported, emit: Emit
) -> int:
    """Print the comparison, append it to the history; exit 1 when the USD gap exceeds the tolerance."""
    outcome = reconcile(request, paths, config, book, reported)
    append_json_line(paths.state_dir / HISTORY, plain(outcome))
    for text in describe(outcome):
        emit(text)
    return 0 if outcome.within_tolerance else 1


def last_reconciliation(paths: Paths) -> Reconciliation | None:
    """The newest history line as a ``Reconciliation``; ``None`` without a history, a ``ToolError`` for a torn one."""
    path = paths.state_dir / HISTORY
    if not path.exists():
        return None
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return None
        data = json.loads(lines[-1])
        kept = {
            f.name: data[f.name] for f in fields(Reconciliation) if f.name in data
        }  # defaults fill the rest
        kept["window"] = tuple(kept["window"])
        kept["warnings"] = tuple(kept.get("warnings", ()))
        return Reconciliation(**kept)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ToolError(
            f"{path}: the last line is not a reconciliation: {exc.__class__.__name__}: {exc}"
        ) from exc
