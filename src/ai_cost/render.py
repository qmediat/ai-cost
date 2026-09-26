"""Reports as markdown (for people), JSON (for machines) or plain text; every line of output is produced here."""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
from collections.abc import Callable, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from .groups import (
    INVOICE_ATTRIBUTION,
    ApiGroup,
    AttributionGroup,
    Line,
    RealGroup,
    Report,
    SubscriptionShare,
    VendorGroup,
)
from .models import BillSubtotal, BillSummary, Provider, Tokens
from .timeutil import iso


def _fmt_usd(value: float) -> str:
    return f"{value:.2f}"


def _fmt_int(value: float) -> str:
    return f"{int(value):,}".replace(",", " ")


def _millions(value: float) -> str:
    return f"{value / 1e6:.2f} M"


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """A GFM table with no pipes inside cells."""
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell).replace("|", "/") for cell in row) + " |")
    return "\n".join(lines)


def tokens_brief(tokens: Tokens) -> str:
    """The non-zero counters of a line, in millions where they are tokens."""
    parts = []
    for name in (
        "input",
        "output",
        "cache_read",
        "cache_write_5m",
        "cache_write_1h",
        "cached_input",
        "prompt",
        "cached",
        "cache_hit",
        "cache_miss",
    ):
        value = getattr(tokens, name)
        if value:
            parts.append(f"{name} {_millions(value)}")
    if tokens.reviews:
        parts.append(f"reviews {tokens.reviews}")
    if tokens.minutes:
        parts.append(f"minutes {tokens.minutes}")
    return ", ".join(parts)


def _share_cells(share: SubscriptionShare) -> list[Any]:
    """A share's row; one the usage report states has no seat count and, after a price change, no single price."""
    invoice = share.attribution == INVOICE_ATTRIBUTION
    monthly = "—" if invoice and not share.monthly_usd else _fmt_usd(share.monthly_usd)
    return [share.plan, "—" if invoice else share.seats, monthly, share.attribution, _fmt_usd(share.usd)]


def _real_section(group: RealGroup, hours: float) -> list[str]:
    lines = [
        "",
        f"## 1. Real cost (what you paid): {_fmt_usd(group.total_usd)} USD",
        "",
        f"Subscriptions prorated to the window ({hours:.1f} h of a 730 h month) + pay-per-token API keys at list price. No promos, no negotiated discounts.",
        "",
        md_table(
            ["Subscription", "Seats", "Monthly USD", "Attribution", "Share USD"],
            [_share_cells(s) for s in group.subscriptions],
        ),
        "",
        md_table(
            ["Provider", "Model / line", "Runs", "USD", "Note"],
            [
                [
                    line.provider.value,
                    line.label,
                    line.calls,
                    _fmt_usd(line.usd),
                    "; ".join(sorted(line.notes)),
                ]
                for line in group.usage
            ],
        ),
        "",
        f"Cash (API keys): **{_fmt_usd(group.cash_usd)} USD** · subscriptions share: **{_fmt_usd(group.subscription_usd)} USD** · total: **{_fmt_usd(group.total_usd)} USD**",
    ]
    return lines


def _api_section(group: ApiGroup) -> list[str]:
    return [
        "",
        f"## 2. API-only cost (as if no subscription existed): {_fmt_usd(group.total_usd)} USD",
        "",
        "Every token at the provider's pay-per-use list price (cache tiers applied, since that is how the APIs bill). GitHub: the usage report's gross amounts (before included credits and minutes); without one a Copilot review is a count and Actions billable minutes are at the per-minute list price.",
        "",
        md_table(
            ["Provider", "Model", "Runs", "Model calls", "Tokens", "USD", "Notes"],
            [
                [
                    line.provider.value,
                    line.label,
                    line.calls,
                    line.model_calls or "",
                    tokens_brief(line.tokens),
                    _fmt_usd(line.usd),
                    "; ".join(sorted(line.notes)),
                ]
                for line in group.lines
            ],
        ),
    ]


def _vendor_scope(group: VendorGroup) -> str:
    bands = ", ".join(
        f"{size.value}×{count}"
        for size, count in sorted(group.band_counts.items(), key=lambda kv: kv[0].value)
    )
    return (
        f"Scope: {len(group.items)} items ({bands}) → {group.base_hours[0]:.0f}–{group.base_hours[1]:.0f} base hours; "
        f"integration/review/release allowance +{group.integration_pct:.0f}%; package minimum "
        f"{group.package_min_hours:.0f} h, rounded to {group.package_round_hours:.0f} h. Junior/mid/senior differ in "
        "rate AND in time (time factor); juniors' work is reviewed by a senior (senior review)."
    )


def _vendor_table(group: VendorGroup) -> str:
    header = ["Staffing", f"Rate {group.currency}/h", "Time factor", "Senior review", "Hours", "Working days"]
    rows = [
        [
            o.staffing,
            o.rate,
            o.time_factor,
            f"{o.senior_review_pct:.0f}%",
            f"{o.hours[0]}–{o.hours[1]}",
            f"{o.days[0]}–{o.days[1]}",
            f"{_fmt_int(o.cost[0])}–{_fmt_int(o.cost[1])}",
        ]
        for o in group.options
    ]
    return md_table([*header, f"Quote {group.currency}"], rows)


def _vendor_section(group: VendorGroup) -> list[str]:
    title = f"## 3. Vendor quote (what an outside firm would charge): profile `{group.profile}`"
    lines = ["", title, "", group.description, "", _vendor_scope(group), "", _vendor_table(group)]
    if 0 < len(group.items) <= 60:
        items = md_table(["ID", "Size", "Title"], [[i.id, i.size.value, i.title] for i in group.items])
        lines += ["", "<details><summary>Items</summary>", "", items, "", "</details>"]
    return lines


def _attribution_section(group: AttributionGroup) -> list[str]:
    rows = [
        [
            line.label,
            line.calls,
            _fmt_usd(line.api_usd),
            _fmt_usd(line.usd_context),
            _fmt_usd(line.cash_usd),
            _fmt_usd(line.subscription_usd),
            _fmt_usd(line.real_usd),
            ", ".join(line.keys[:8]) + (f" +{len(line.keys) - 8} more" if len(line.keys) > 8 else ""),
        ]
        for line in group.lines
    ]
    return [
        "",
        "## 4. Attribution (by branch / workspace / PR, then by the paths a turn touched)",
        "",
        f"Policy: {group.policy}.",
        "",
        md_table(
            [
                "Label",
                "Runs",
                "API USD",
                "of which context",
                "Cash USD",
                "Subscription USD",
                "Real USD",
                "Keys",
            ],
            rows,
        ),
        "",
        f"Sum of the lines: API-only **{_fmt_usd(group.total_api_usd)} USD**, real **{_fmt_usd(group.total_real_usd)} USD** (the window's API-only and real totals).",
    ]


def _exact(value: Decimal | None) -> str:
    return "?" if value is None else format(value, "f")


def _subtotal_rows(sums: Sequence[BillSubtotal]) -> list[list[Any]]:
    return [
        [
            s.product,
            s.sku,
            s.unit,
            s.lines,
            _exact(s.quantity),
            _exact(s.gross),
            _exact(s.discount),
            _exact(s.net),
        ]
        for s in sums
    ]


_SUBTOTAL_HEADERS = ["Product", "SKU", "Unit", "Lines", "Quantity", "Gross USD", "Included USD", "Net USD"]


def _bill_section(bill: BillSummary) -> list[str]:
    """The usage report's own figures, exact: what the GitHub lines above sum, what they leave out, what is missing."""
    span = (
        f"{bill.days[0].isoformat()}..{bill.days[-1].isoformat()} ({len(bill.days)})" if bill.days else "none"
    )
    out = [
        "",
        f"## GitHub usage report ({bill.account})",
        "",
        f"Read {iso(bill.read_at)}. Whole UTC days in the totals: {span}. The report's own figures, exact; "
        "the API-only group sums the gross amounts, the real group the net (seats as subscription shares).",
    ]
    if bill.counted:
        out += ["", md_table(_SUBTOTAL_HEADERS, _subtotal_rows(bill.counted))]
    if bill.left_out:
        out += [
            "",
            "Left out of the whole days (not AI-assisted work, or Actions of a repository not named with --github):",
        ]
        out += ["", md_table(_SUBTOTAL_HEADERS, _subtotal_rows(bill.left_out))]
    if bill.outside:
        rows = [
            [d.day.isoformat(), d.why, d.lines, _exact(d.gross), _exact(d.discount), _exact(d.net)]
            for d in bill.outside
        ]
        out += ["", "Days the window only touches — their counted lines, not in the totals:", ""]
        out += [md_table(["Day", "Why", "Lines", "Gross USD", "Included USD", "Net USD"], rows)]
    out += ["", *(f"- not read: {month}" for month in bill.missing)] if bill.missing else []
    return out


def render_markdown(report: Report) -> str:
    """The human report."""
    hours = report.window.hours()
    out = [
        f"# AI cost report — {report.window_iso[0]} → {report.window_iso[1]} ({hours:.1f} h)",
        "",
        f"Sources: {', '.join(report.sources) or 'nothing found'}. Prices checked {report.prices_checked_at}. Generated by ai-cost {report.version} on {report.generated_at}.",
    ]
    if report.skipped:
        out += [
            "",
            f"- ⚠ {len(report.skipped)} file(s) or record(s) skipped (see `doctor` / `--format json` for the list)",
        ]
    out += [f"- ⚠ {warning}" for warning in report.warnings]
    if report.real:
        out += _real_section(report.real, hours)
    if report.api:
        out += _api_section(report.api)
    if report.vendor:
        out += _vendor_section(report.vendor)
    if report.attribution:
        out += _attribution_section(report.attribution)
    if report.github_bill:
        out += _bill_section(report.github_bill)
    if report.real and report.api:
        rows: list[list[Any]] = [
            ["Real (subscriptions share + API keys)", _fmt_usd(report.real.total_usd)],
            ["API-only equivalent", _fmt_usd(report.api.total_usd)],
        ]
        if report.vendor:
            rows += [
                [
                    f"Vendor quote, {o.staffing} option",
                    f"{_fmt_int(o.cost[0])}–{_fmt_int(o.cost[1])} {report.vendor.currency}",
                ]
                for o in report.vendor.options
            ]
        out += ["", "## Summary", "", md_table(["Group", "USD"], rows)]
    return "\n".join(out) + "\n"


def render_text(report: Report) -> str:
    """Markdown without the pipes: readable in a terminal that drops tables."""
    text = render_markdown(report)
    return re.sub(r"^\|", "", text, flags=re.M).replace(" | ", "  ").replace("|", "")


_SCALAR_CONVERTERS: list[tuple[type, Callable[[Any], Any]]] = [
    (Decimal, lambda v: format(v, "f")),  # an amount of a usage report, exact as text
    (Enum, lambda v: v.value),
    (datetime, lambda v: v.isoformat()),
    (date, lambda v: v.isoformat()),
]


def _properties(value: Any) -> list[str]:
    return [name for name, attr in inspect.getmembers(type(value)) if isinstance(attr, property)]


def plain(value: Any) -> Any:
    """Dataclasses (fields AND properties, so computed totals survive), enums, dates and containers → JSON-ready."""
    result: Any = value
    if isinstance(value, Provider):  # a dataclass, but serialised as its id like the enum it replaced
        result = value.value
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        names = [f.name for f in dataclasses.fields(value)] + _properties(value)
        result = {name: plain(getattr(value, name)) for name in names}
    elif isinstance(value, dict):
        result = {str(k.value if isinstance(k, (Enum, Provider)) else k): plain(v) for k, v in value.items()}
    elif isinstance(value, set):
        result = [plain(v) for v in sorted(value, key=str)]  # a set has no order: sorted, so two runs match
    elif isinstance(value, (list, tuple)):
        result = [plain(v) for v in value]
    else:
        for kind, convert in _SCALAR_CONVERTERS:
            if isinstance(value, kind):
                result = convert(value)
                break
    return result


def render_json(report: Report, detail: bool) -> str:
    """The machine report; ``detail`` adds every usage row."""
    data = plain(report)
    if not detail:
        data.pop("rows", None)
    return json.dumps(data, indent=2)


def line_summary(line: Line) -> str:
    """One-line form used by ``monitor``."""
    return f"{line.provider.value}/{line.label}: {_fmt_usd(line.usd)}"
