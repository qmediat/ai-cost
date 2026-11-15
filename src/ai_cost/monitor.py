"""``ai-cost monitor``: rolling-window totals, the budgets, the history file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config, Paths, PriceBook
from .groups import (
    Report,
)
from .models import (
    BillSummary,
)
from .ops import ReportRequest, build_report, provider_outside
from .output import Emit, append_json_line
from .timeutil import iso, now, parse_cli_ts


@dataclass(frozen=True)
class MonitorEntry:
    """One history line; ``outside`` names the amounts the totals leave out (GitHub and provider days, unread spans)."""

    ts: str
    window: tuple[str, str]
    hours: float
    real_usd: float
    cash_usd: float
    api_usd: float
    by_provider_api_usd: dict[str, float]
    outside: tuple[str, ...] = ()


def bill_outside(bill: BillSummary | None) -> tuple[str, ...]:
    """The GitHub amounts a window's totals leave out, exact: the days it only touches and the months not read."""
    if bill is None:
        return ()
    days = tuple(
        f"github {day.day.isoformat()} ({day.why}): net {day.net:f} USD, gross {day.gross:f}"
        for day in bill.outside
    )
    return days + tuple(f"github {month}" for month in bill.missing)


def month_to_date(paths: Paths, config: Config, book: PriceBook, at: datetime) -> float:
    """API-equivalent cost from the first of ``at``'s month to ``at``.

    One report over the month, so overlapping monitor windows never double count.
    """
    start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if at == start:  # the month has just begun: nothing to sum, and no empty window to refuse
        return 0.0
    request = ReportRequest(all_projects=True, since=iso(start), until=iso(at), groups=("api",))
    report = build_report(request, paths, config, book)
    return report.api.total_usd if report.api else 0.0


def _breaches(config: Config, entry: MonitorEntry, month_total: float, scale: float) -> list[str]:
    """Every budget the window exceeds; ``scale`` = exact window hours / 24 (the entry's hours are rounded)."""
    by_provider = entry.by_provider_api_usd
    found = []
    if config.budgets.monthly_usd and month_total > config.budgets.monthly_usd:
        found.append(
            f"api-equivalent month-to-date {month_total:.2f} > monthly budget {config.budgets.monthly_usd:.2f}"
        )
    if config.budgets.daily_usd and entry.api_usd > config.budgets.daily_usd * scale:
        found.append(
            f"api-equivalent {entry.api_usd:.2f} > daily budget {config.budgets.daily_usd:.2f} × {scale:.2f} days"
        )
    for provider, limit in config.budgets.per_provider_daily_usd.items():
        spent = by_provider.get(provider, 0.0)
        if spent > limit * scale:
            found.append(f"{provider} {spent:.2f} > {limit * scale:.2f}")
    return found


def _monitor_entry(report: Report) -> MonitorEntry:
    assert report.real is not None and report.api is not None
    by_provider: dict[str, float] = {}
    for line in report.api.lines:
        by_provider[line.provider.value] = by_provider.get(line.provider.value, 0.0) + line.usd
    return MonitorEntry(
        ts=report.generated_at,
        window=report.window_iso,
        hours=round(report.window.hours(), 2),
        real_usd=round(report.real.total_usd, 4),
        cash_usd=round(report.real.cash_usd, 4),
        api_usd=round(report.api.total_usd, 4),
        by_provider_api_usd={k: round(v, 4) for k, v in by_provider.items()},
        outside=bill_outside(report.github_bill) + provider_outside(report.provider_reports),
    )


def _append_history(path: Path, entry: Any) -> None:
    """One JSON line per monitor run; a state directory that cannot be written is a ``ToolError``, not a traceback."""
    append_json_line(path, entry.__dict__)


def monitor(
    request: ReportRequest,
    paths: Paths,
    config: Config,
    book: PriceBook,
    append: bool,
    emit: Emit,
) -> int:
    """Rolling-window totals → history; exit 3 when a budget is breached."""
    report = build_report(request, paths, config, book)
    entry = _monitor_entry(report)
    if append:
        _append_history(paths.state_dir / "history.jsonl", entry)
    month_end = parse_cli_ts(request.until, "--until") or now()  # the month of the window's end
    month_total = month_to_date(paths, config, book, month_end) if config.budgets.monthly_usd else 0.0
    hours = report.window.hours()
    breaches = _breaches(config, entry, month_total, hours / 24.0 if hours else 1.0)
    emit(json.dumps(entry.__dict__))
    for breach in breaches:
        emit("BUDGET BREACH: " + breach)
    return 3 if breaches else 0


def history(paths: Paths, count: int, emit: Emit) -> int:
    """The last ``count`` history lines."""
    path = paths.state_dir / "history.jsonl"
    if not path.exists():
        emit(f"no history yet ({path})")
        return 0
    entries = path.read_text().splitlines()
    for line in entries[-count:] if count > 0 else []:
        emit(line)
    return 0
