"""Operations: the report assembly and the commands around it. Commands return exit codes; nothing here exits.

The doctor, the monitor, the scheduled jobs and the header's warnings live in ``doctor.py``, ``monitor.py``,
``schedules.py`` and ``report_warnings.py``.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from . import __version__
from .attribution import Rule, attribution_group, label_group, parse_rules
from .collectors import BUILTIN, collect_claude, collect_github, find_session_files
from .collectors.claude import SOURCE_NAME as CLAUDE_SOURCE
from .collectors.files import or_skip
from .collectors.github_bill import (
    BillError,
    BillRequest,
    bill_source,
    bill_warnings,
    collect_bill,
    fetch_month,
    settled_by,
)
from .config import MAX_WINDOW_HOURS, Config, Instance, Paths, PriceBook
from .errors import ToolError, UsageError
from .estimates import Estimates, left_out_warnings, scoped, valid
from .groups import (
    ApiGroup,
    AttributionGroup,
    RealGroup,
    Report,
    api_group,
    needs_list_price,
    real_group,
    vendor_group,
)
from .identity import (
    COLLECT_TAIL,
    SessionSelection,
    inherit_origin_by_ref,
    is_identity,
    resolve_session,
    select_session,
    selection_warnings,
    session_span,
    source_counts,
    without_shares,
)
from .models import (
    Billing,
    BillSummary,
    Collected,
    Estimate,
    Provider,
    ProviderSummary,
    Scope,
    Size,
    Skipped,
    UsageRow,
    Window,
    WorkItem,
    client_outside,
)
from .onboarding import (
    BILL_HINT,
    init_config_lines,
    paid_for,
)
from .output import Emit, ensure_dir
from .permodel import model_lines
from .plugins import (
    Context,
    Enricher,
    Loaded,
    Plugin,
    Source,
    bundled_modules,
    configured_modules,
    entry_point_modules,
    load_plugins,
)
from .prices_check import auto_check
from .pricing import entry_for
from .providers import ReadContext, read_reports
from .remainder import compare, interval, span_text
from .report_warnings import billing_warnings, config_warnings
from .timeutil import iso, minutes, now, parse_cli_ts, zoned
from .values import checked_label, merge_skips
from .values import labels as labels_rule

ACCOUNTS_UNREAD = "an instance reads no account source"
CLIS_UNREAD = "coding-CLI files: not read — an instance reads them only with read_clis"
FILTERED = (
    "the real group is their cash only, no plan share; a filter selects usage rows: work items and estimates are "
    "left out"
)
FILTERED_ACCOUNT = "a report filtered by labels holds part of the account"


def reads_clis(paths: Paths) -> bool:
    """Whether a run reads the machine user's coding-CLI files: always, except an instance without ``read_clis``."""
    return paths.instance is None or paths.instance.read_clis


@dataclass(frozen=True)
class ReportRequest:
    """Everything ``report`` needs, parsed from the CLI."""

    session: str | None = None
    project: Path | None = None
    all_projects: bool = False
    since: str | None = None
    until: str | None = None
    hours: float | None = None
    settings: Mapping[str, Mapping[str, str]] = field(
        default_factory=dict
    )  # --setting PLUGIN.KEY=VALUE: per-plugin overrides of config plugin_settings
    github: Sequence[str] = ()
    groups: Sequence[str] = ("real", "api", "vendor")
    items: Path | None = None
    vendor_profile: str | None = None
    detail: bool = False
    attribute: Sequence[str] = ()  # LABEL=REGEX specs (ADR-0003)
    # fail (exit 5) or skip (count the rows and go on); "" = the run's default, ``unpriced_rule``
    unpriced: str = ""
    where: Mapping[str, str] = field(default_factory=dict)  # only rows whose labels hold every pair, exactly
    by: str = ""  # one label name: the attribution group lists one line per value of it


def resolve_window(request: ReportRequest, span: Window | None, default_hours: float) -> Window:
    """``--since/--until`` win; else the session span; else the last N hours.

    The span runs from a minute before the first turn to five minutes after the last. One session reads on for a
    day (``COLLECT_TAIL``, never past now): a round it launched may settle, and a ledger charge land, after its last
    turn, and only the rows stamped with it are kept.
    """
    if request.since and request.hours is not None:
        raise UsageError("--hours cannot be combined with --since: both would set the window start")
    if request.hours is not None:
        _window_hours(request.hours, default_hours)  # validated whenever given, even where a span wins
    if request.since or request.until:
        end = parse_cli_ts(request.until, "--until") or now()
        start = parse_cli_ts(request.since, "--since") or _hours_before(end, request.hours, default_hours)
        if (
            start >= end
        ):  # an inverted or empty window would filter every source away and report 0 rows with exit 0
            raise UsageError(f"--since {iso(start)} is not before --until {iso(end)}: the window is empty")
        return _with_room(Window(start, end), "--since/--until")
    if span and request.session:
        padded = _with_room(span, "--session: the session's timestamps")
        end = padded.end + minutes(5)
        if is_identity(
            request.session
        ):  # a day on, never past now, always past the last turn (end is exclusive)
            end = max(min(padded.end + COLLECT_TAIL, now()), padded.end + timedelta(seconds=1))
        return Window(padded.start - minutes(1), end)
    end = now()
    return Window(_hours_before(end, request.hours, default_hours), end)


def _with_room(window: Window, what: str) -> Window:
    """The window as given, if the sources' padding around it (a day each side) can exist; else a ``UsageError``."""
    try:
        window.start - timedelta(days=1)
        window.end + timedelta(days=1)
    except OverflowError as exc:
        raise UsageError(f"{what}: the window sits at the edge of representable time") from exc
    return window


def _hours_before(end: datetime, hours: float | None, default_hours: float) -> datetime:
    """``end`` minus the window length; an ``--until`` so early that the start cannot exist is a ``UsageError``."""
    try:
        return end - timedelta(hours=_window_hours(hours, default_hours))
    except OverflowError as exc:
        raise UsageError(
            f"--until {end.isoformat()}: the window would start before the earliest time"
        ) from exc


def _window_hours(hours: float | None, default_hours: float) -> float:
    """``--hours`` (the config default when absent): positive, finite and at most ``MAX_WINDOW_HOURS``."""
    chosen = default_hours if hours is None else hours
    if not math.isfinite(chosen) or chosen <= 0 or chosen > MAX_WINDOW_HOURS:
        raise UsageError(f"--hours must be > 0 and at most {MAX_WINDOW_HOURS}, got {chosen}")
    return chosen


def load_items(path: Path) -> list[WorkItem]:
    """Hand-sized scope: JSON ``[{id,title,size}]`` or a Markdown table with a ``size``/``effort`` column."""
    if not path.is_file():
        raise UsageError(f"--items: no such file {path}")
    try:
        text = path.read_text()
    except OSError as exc:
        raise UsageError(f"--items: cannot read {path} ({exc.__class__.__name__})") from exc
    try:
        entries = json.loads(text)
    except ValueError:
        items, found = _table_items(text)
        if not found:
            raise UsageError(
                f"--items: {path} is neither a JSON list of objects nor a Markdown table"
            ) from None
        return items  # a header-only table is an empty scope, as a JSON [] is
    if not isinstance(entries, list):
        raise UsageError(f"--items: {path} holds JSON but not a list of objects")
    return _json_items(entries)


def _json_items(entries: list[Any]) -> list[WorkItem]:
    """Every element must be an object; anything else names its position in a ``UsageError``."""
    items = []
    for n, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise UsageError(f"--items: entry {n + 1} is not an object ({type(entry).__name__})")
        size = _size(str(entry.get("size", "S")))
        items.append(WorkItem(id=str(entry.get("id", n + 1)), title=str(entry.get("title", "")), size=size))
    return items


def _table_items(text: str) -> tuple[list[WorkItem], bool]:
    """The items of the first Markdown table, and whether a table header was found at all."""
    items: list[WorkItem] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        row = dict(zip(header, cells))
        items.append(
            WorkItem(
                id=row.get("id") or cells[0],
                title=row.get("title") or row.get("item") or "",
                size=_size(row.get("size") or row.get("effort") or "S"),
            )
        )
    return items, header is not None


def _size(value: str) -> Size:
    try:
        return Size(value.upper())
    except ValueError:
        return Size.S


@dataclass(frozen=True)
class _Count:
    """A sources line that counts rows, as a filtered report says it: its label, and whether it counts sessions."""

    label: str
    # the Claude transcripts' line counts files; filtered, it names the sessions of the rows it kept
    sessions: bool = False

    def recount(self, rows: Sequence[UsageRow], index: int) -> str:
        """The line for the kept rows this line took (``UsageRow.intake``), naming only their writers; "" for none."""
        kept = [row for row in rows if row.intake == index]
        if not kept:
            return ""
        if self.sessions:
            return f"{self.label}: {len({row.ref for row in kept})} session(s) kept"
        writers = sorted({row.source for row in kept} - {self.label})
        return f"{self.label} ×{len(kept)}" + (f" ({', '.join(writers)})" if writers else "")


@dataclass
class _Gathered:
    """Everything collected for a window, before grouping."""

    window: Window
    rows: list[UsageRow]
    items: list[WorkItem]
    sources: list[str]
    warnings: list[str]
    skipped: list[Skipped]
    book: PriceBook | None = None
    bill: BillSummary | None = None  # the GitHub usage report's exact figures, when one was read
    selection: SessionSelection | None = None  # a session report: what it kept and what it left out
    session: str = ""  # a session report's resolved id, known before any source runs
    estimates: list[Estimate] = field(default_factory=list)  # never priced, never summed (ADR-0010)
    project: str = ""  # a per-project report's directory: it shows no estimate (ADR-0010)
    # the lines of ``sources`` that count rows, by index: a filtered report counts them again from the rows it kept
    # (None: a line it drops, a source's estimates)
    counts: dict[int, _Count | None] = field(default_factory=dict)
    filtered: bool = False  # a label filter: every row is tagged with the sources line that took it


def _default_project(request: ReportRequest, paths: Paths) -> tuple[Path | None, bool]:
    """Where to look for transcripts when the request does not say.

    A session id is unique, so it is searched in every project; ``latest``/``all`` or no session mean the cwd's
    project, or every project when the cwd has none.
    """
    if request.project or request.all_projects:
        return request.project, request.all_projects
    if request.session and request.session not in ("latest", "all"):
        return None, True
    from .collectors.claude import project_dir

    if project_dir(paths.claude_home, Path.cwd()).is_dir():
        return Path.cwd(), False
    return None, True


def _gather(
    request: ReportRequest, paths: Paths, config: Config, loaded: Loaded, book: PriceBook | None = None
) -> _Gathered:
    project, all_projects = _default_project(request, paths)
    unlisted: list[Skipped] = []
    files = or_skip(
        lambda: find_session_files(paths.claude_home, project, request.session, all_projects, unlisted),
        "claude",  # the name the Claude collector's own skips carry
        paths.claude_home / "projects",
        unlisted,
    )
    claude = collect_claude(files, None, Billing.rule(config.anthropic_billing))
    window = resolve_window(request, claude.span, config.window_default_hours)
    said = f"claude transcripts ×{len({p for _, p in files})}" if reads_clis(paths) else CLIS_UNREAD
    gathered = _Gathered(
        window, [], [], [said], [], [*unlisted, *claude.skipped], book, filtered=bool(request.where)
    )
    if reads_clis(paths):  # a line that counts rows; an instance's "not read" counts none
        gathered.counts[0] = _Count("claude transcripts", sessions=True)
    session = _open_session(gathered, request, files, project, reads_clis(paths))
    gathered.rows += _taken(gathered, [row for row in claude.rows if window.contains(row.at)], 0)
    if request.github:  # before the plugins, so their enrichers see every row — the live GitHub rows included
        _gather_github(request, config, book, gathered)
    bill = _bill_unless_part(request, paths, config, gathered, session)
    _collect_sources(request, paths, config, loaded, gathered)
    gathered.rows = _inherit_scope_by_ref(gathered.rows)
    per_project = False
    if project is not None and not all_projects:  # the sources' raw working directories, before any enricher
        _keep_project_rows(gathered, project)
        per_project = True
        gathered.project = str(project)
    _absorb_bill(gathered, bill, per_project, config)
    _enrich_rows(request, paths, config, loaded, gathered)
    gathered.rows = _with_billing_rules(gathered.rows, config)
    _select(gathered, session, claude.span)
    _keep_labelled(gathered, request.where)
    return gathered


def _taken(gathered: _Gathered, rows: list[UsageRow], index: int) -> list[UsageRow]:
    """The rows a sources line takes; in a filtered report each is tagged with that line, to be counted by it later."""
    return [replace(row, intake=index) for row in rows] if gathered.filtered else rows


def _bill_unless_part(
    request: ReportRequest, paths: Paths, config: Config, gathered: _Gathered, session: str | None
) -> BillSummary | None:
    """The GitHub usage report of an account view; a session's or a label filter's report reads none, and says so."""
    if session:
        return None
    if request.where:
        if config.github.bill is not None:
            gathered.sources.append(f"GitHub usage report: not read — {FILTERED_ACCOUNT}")
        return None
    return _gather_bill(request, paths, config, gathered)


def _holds(row: UsageRow, where: Mapping[str, str]) -> bool:
    """Whether the row's labels hold every pair of the filter, exactly."""
    return all(row.labels.get(key) == value for key, value in where.items())


def _keep_labelled(gathered: _Gathered, where: Mapping[str, str]) -> None:
    """``where``: the rows whose labels hold every pair, after inheritance, scope, selection and enrichment.

    Every row took part in those steps (an unlabelled row can be the scope donor of a labelled one); the filter comes
    last; work items and estimates are left out. The real group is the selection's cash only. The sources lines are
    counted again once the rows are final (``_say_filter``).
    """
    if not where:
        return
    gathered.rows = [row for row in gathered.rows if _holds(row, where)]
    gathered.items, gathered.estimates = [], []


def _say_filter(gathered: _Gathered, where: Mapping[str, str]) -> None:
    """A filtered report's sources from its final rows: each line counted from the rows it took, the filter last.

    No line says how many rows the other selections have.
    """
    if not where:
        return
    lines = []
    for index, line in enumerate(gathered.sources):
        if index not in gathered.counts:
            lines.append(line)  # a line that counts no row stays as it is
            continue
        count = gathered.counts[index]
        text = count.recount(gathered.rows, index) if count else ""
        lines += [text] if text else []
    said = ", ".join(f"{key}={value}" for key, value in sorted(where.items()))
    gathered.sources = [*lines, f"labels {said}: {len(gathered.rows)} row(s) — {FILTERED}"]


def _open_session(
    gathered: _Gathered,
    request: ReportRequest,
    files: Sequence[tuple[str, Path]],
    project: Path | None,
    expected: bool = True,
) -> str | None:
    """The report's session (``None`` for a period), kept for the plugins; a report without a transcript says so.

    ``expected`` False: the run reads no CLI file (an instance), so no transcript is no news.
    """
    session = _session_of(request, files, gathered.sources)
    gathered.session = session or ""
    if not files and expected:
        gathered.warnings.append(_no_transcript(request, project, session))
    return session


def _session_of(request: ReportRequest, files: Sequence[tuple[str, Path]], sources: list[str]) -> str | None:
    """The one session a session report is about; ``None`` for a period report.

    Account-wide figures name no session: ``--github`` is refused and the usage report is not read (said in the
    sources line). Without a transcript nothing says when the session ran, so the dates must.
    """
    if request.session is None or not is_identity(request.session):
        return None
    if request.github:
        raise UsageError("--github with --session: a live GitHub count names no session (the review rows do)")
    if request.project or request.all_projects:
        raise UsageError(
            "--project/--all-projects with --session: a session report reads every project and keeps the rows "
            "stamped with the session"
        )
    if not files and not (request.since or request.until):
        raise UsageError(
            f"--session {request.session}: no transcript says when it ran — give --since/--until to read its rows"
        )
    sources.append("GitHub usage report: not read — a report of one session")
    return resolve_session(request.session, files)


def _select(gathered: _Gathered, session: str | None, turns: Window | None) -> None:
    """After every enricher, so a plugin's stamps are in: the session's rows stay, the rest of its span is counted.

    A work item names no session: a session's vendor quote comes from ``--items`` alone (said by ``_vendor_items``).
    """
    if session is None:  # a period report
        return
    span = session_span(turns) if turns else None
    gathered.rows, selection = select_session(inherit_origin_by_ref(gathered.rows), session, span)
    said = (iso(turns.start), iso(turns.end)) if turns else ("", "")
    gathered.selection = replace(selection, turns=said)
    gathered.items = []


def _priced_groups(
    request: ReportRequest, gathered: _Gathered, book: PriceBook, config: Config, priced: Config
) -> tuple[RealGroup | None, ApiGroup | None]:
    """The real group (with its billing warnings) and the API group, each only when asked for."""
    rows = gathered.rows
    real = real_group(rows, book, priced, gathered.window) if "real" in request.groups else None
    gathered.warnings += billing_warnings(real, rows, config, one_session=gathered.selection is not None)
    api = api_group(rows, book, config) if "api" in request.groups else None  # it prices every row at list
    return real, api


INSTANCE_GROUPS = ("real", "api")  # an instance's default groups, both doors: an app reads no vendor invoice


def unpriced_rule(request: ReportRequest, paths: Paths) -> str:
    """The unpriced rule a run follows: what the request says, else its default.

    An instance skips and counts (an operator's screen never fails on a new model); the machine user's report fails.
    """
    return request.unpriced or ("skip" if paths.instance is not None else "fail")


def _skip_unpriced(
    request: ReportRequest, paths: Paths, config: Config, book: PriceBook, gathered: _Gathered
) -> None:
    """``--unpriced skip``: rows without a price leave, counted (``_drop_unpriced``)."""
    if unpriced_rule(request, paths) == "skip":
        list_priced = "api" in request.groups or bool(request.attribute) or bool(request.by)
        _drop_unpriced(gathered, book, config, paths.user_prices_file(), list_priced=list_priced)


def _say_selection(gathered: _Gathered) -> None:
    """After ``--unpriced skip``: a session report's header counts the rows its groups price."""
    if gathered.selection:
        gathered.selection = replace(gathered.selection, kept=len(gathered.rows))
        gathered.warnings += selection_warnings(gathered.selection)


def _no_transcript(request: ReportRequest, project: Path | None, session: str | None) -> str:
    if session is not None:
        return f"no transcript for --session {session}: only rows stamped with exactly that id are counted"
    return f"no Claude transcripts matched (project {project or Path.cwd()}, session {request.session})"


def _gather_github(
    request: ReportRequest, config: Config, book: PriceBook | None, gathered: _Gathered
) -> None:
    """The live GitHub counts of ``--github``, and a warning when nothing says how they are paid."""
    live = collect_github(list(request.github), gathered.window)
    gathered.rows += live.rows
    if live.rows and not paid_for("github", config, book):
        gathered.warnings.append(
            "live GitHub rows are counts: a Copilot review has no price and Actions minutes of a plan cost nothing "
            f"here — {BILL_HINT}"
        )
    merge_skips(gathered.skipped, list(live.skipped))
    gathered.sources.append(f"github ({', '.join(request.github)})")


def bill_request(paths: Paths, config: Config, repos: Sequence[str] = ()) -> BillRequest | None:
    """The configured usage report, read now; ``repos`` (``--github``) are the ones whose Actions lines count.

    An instance reads no account source: ``None`` for it whatever its config says.
    """
    if config.github.bill is None or paths.instance is not None:
        return None
    return BillRequest(config.github.bill, now(), paths.offline, frozenset(repo.lower() for repo in repos))


def bill_unreadable(paths: Paths, config: Config, day: date) -> str:
    """Why the usage report of ``day``'s month cannot be read now ("" when it can, or when none is configured)."""
    reading = bill_request(paths, config)
    if reading is None:
        return ""
    try:
        fetch_month(
            reading, day.year, day.month
        )  # kept for the process: the build that follows reads it again free
    except BillError as exc:
        return str(exc)
    return ""


def _gather_bill(
    request: ReportRequest, paths: Paths, config: Config, gathered: _Gathered
) -> BillSummary | None:
    """The usage report's rows of the window's whole days; its exact summary for the header (ADR-0007)."""
    if paths.instance is not None and config.github.bill is not None:
        gathered.sources.append(f"github usage report: not read — {ACCOUNTS_UNREAD}")
        return None
    reading = bill_request(paths, config, request.github)
    if reading is None:
        return None
    bill = collect_bill(reading, gathered.window)
    gathered.rows += bill.rows
    merge_skips(gathered.skipped, bill.skipped)
    return bill.summary


def _absorb_bill(gathered: _Gathered, bill: BillSummary | None, per_project: bool, config: Config) -> None:
    """The header lines, the sources line and the section.

    A per-project report keeps only the report's seats: its usage lines name repositories, not directories, so the
    account's figures belong to an --all-projects report.
    """
    if bill is None or config.github.bill is None:
        return
    account = config.github.bill
    gathered.rows = [
        replace(row, billing=Billing.API_SETTLED) if settled_by(row, bill, account) else row
        for row in gathered.rows
    ]
    gathered.warnings += bill_warnings(bill, with_section=not per_project)
    gathered.sources.append(bill_source(bill, per_project))
    gathered.bill = None if per_project else bill


# ---- provider day reports (ADR-0008) ------------------------------------------------------------------------------


def _compare_providers(
    request: ReportRequest, paths: Paths, config: Config, book: PriceBook, gathered: _Gathered
) -> tuple[ProviderSummary, ...]:
    """Each configured provider's days against the local rows, before anything is dropped as unpriced.

    Only an account view compares: a per-project or one-session report holds part of the account's records, so a
    difference there would book the rest of the account as untracked. Such a report reads no provider at all — a live
    source is a request per report, and `daily` writes one per project — and its sources line says why.
    """
    if not config.provider_reports:
        return ()
    view_why = _view_why(request, paths)
    if view_why:
        gathered.sources.append(f"provider reports: not read — {view_why}")
        return ()
    reports = read_reports(config.provider_reports, read_context(paths, gathered.window))
    done = compare(reports, gathered.rows, gathered.window, book, config)
    gathered.rows += list(done.rows)
    gathered.warnings += list(done.warnings)
    gathered.sources += [_report_source(summary) for summary in done.summaries]
    return done.summaries


def read_context(paths: Paths, window: Window) -> ReadContext:
    """What the provider readers of one report share; the keys are the process environment's."""
    return ReadContext(paths.state_dir, window, now(), paths.offline, os.environ)


def _view_why(request: ReportRequest, paths: Paths) -> str:
    """Why this report compares no provider day ("" when it is an account view)."""
    if paths.instance is not None:
        return ACCOUNTS_UNREAD
    return _part_of_the_account(request, paths)


def _part_of_the_account(request: ReportRequest, paths: Paths) -> str:
    """Why the report holds part of the account's records ("" when it holds them all)."""
    if request.where:
        return FILTERED_ACCOUNT
    project, all_projects = _default_project(request, paths)
    if project is not None and not all_projects:
        return "a per-project report holds part of the account"
    if request.session and request.session != "all":
        return "a report of one session holds part of the account"
    return ""


def _report_source(summary: ProviderSummary) -> str:
    untracked = sum((c.real_diff or 0) > 0 or (c.api_diff or 0) > 0 for c in summary.compared)
    return (
        f"provider report {summary.provider.value} ({summary.source}): {len(summary.compared)} day(s) compared, "
        f"{untracked} with untracked usage, {len(summary.not_compared)} not compared"
    )


def provider_outside(summaries: Sequence[ProviderSummary]) -> tuple[str, ...]:
    """The provider days a window's totals leave out, exact, and the spans without data."""
    days = tuple(
        f"{s.provider.value} {interval(c.day)} ({c.why_not}): gross {c.day.gross:f} {c.day.currency}, "
        f"net {c.day.net:f} {c.day.currency}"
        for s in summaries
        for c in s.not_compared
    )
    spans = tuple(
        f"{s.provider.value} {span_text(span)}" for s in summaries for span in (*s.missing, *s.unreadable)
    )
    return days + spans


# ---- per-project scope (ADR-0006) -------------------------------------------------------------------------------


def _inherit_scope_by_ref(rows: list[UsageRow]) -> list[UsageRow]:
    """A row without any scope key takes the scope of a row of another source that shares its ref (ADR-0006).

    The ref is a CLI session's id, or what a program wrote as ``ref`` / ``session`` in a usage-log line: a ledger
    line, a proxy log or an app's own record of a CLI session is placed with that session. Only an empty scope is
    filled, only from another source, and the row's own paths stay.
    """
    scopes: dict[str, tuple[str, Scope]] = {}
    for row in rows:
        if row.ref and row.scope.identity() and row.ref not in scopes:
            scopes[row.ref] = (row.source, row.scope)
    out: list[UsageRow] = []
    for row in rows:
        found = scopes.get(row.ref)
        if found is None or row.scope.identity() or found[0] == row.source:
            out.append(row)
        else:
            out.append(replace(row, scope=replace(found[1], paths=row.scope.paths)))
    return out


def _is_under(workspace: str, project: Path) -> bool:
    """Whether an absolute working directory is the project or lies below it; symlinks of either side resolved."""
    given = os.path.expanduser(str(project))
    roots = {os.path.normpath(given), os.path.realpath(given)}
    dirs = {os.path.normpath(workspace)}
    if os.path.exists(workspace):
        dirs.add(os.path.realpath(workspace))
    return any(d == root or d.startswith(root.rstrip(os.sep) + os.sep) for d in dirs for root in roots)


def _keep_project_rows(gathered: _Gathered, project: Path) -> None:
    """``--project`` (or the cwd's project): rows that name another scope leave, counted per source.

    Exclusion needs evidence (ADR-0006). A row that names an absolute working directory outside the project is
    left out; a row that names a workspace which is not a directory (a review workspace, a hashed session folder
    nobody can map) is left out too — it belongs somewhere, just not provably here; a row that names nothing (a
    usage-log line, a GitHub row) stays. Each group is counted in its own header warning, so the reader sees how
    much of the total is placed by evidence. The Claude rows are the project's already: their transcripts were
    chosen by it.
    """
    kept: list[UsageRow] = []
    counts: dict[str, dict[str, int]] = {"outside": {}, "named": {}, "unplaced": {}}
    for row in gathered.rows:
        workspace = os.path.expanduser(row.scope.workspace)  # a source may say ~/…: still a directory
        if row.source == CLAUDE_SOURCE or (os.path.isabs(workspace) and _is_under(workspace, project)):
            kept.append(row)
            continue
        group = "outside" if os.path.isabs(workspace) else "named" if workspace else "unplaced"
        counts[group][row.source] = counts[group].get(row.source, 0) + 1
        if group == "unplaced":
            kept.append(row)
    gathered.rows = kept
    gathered.warnings += _scope_warnings(project, counts)


def _scope_warnings(project: Path, counts: Mapping[str, Mapping[str, int]]) -> list[str]:
    """One header line per non-empty group of the per-project filter, with the count per source."""
    texts = {
        "outside": "row(s) of other working directories left out",
        "named": "row(s) of named workspaces that are not directories left out (an --all-projects report with "
        "--attribute places them by regex)",
        "unplaced": "row(s) naming no working directory are included",
    }
    return [
        f"--project {project}: {sum(by_source.values())} {texts[group]} ({source_counts(by_source)})"
        + (" — --all-projects counts them" if group != "unplaced" else "")
        for group, by_source in counts.items()
        if by_source
    ]


def _with_billing_rules(rows: list[UsageRow], config: Config) -> list[UsageRow]:
    """A row without billing evidence of its own takes ``providers.<name>.billing`` when the config names a rule.

    The built-in sources apply their provider's rule themselves; this is how a plugin's rows (xai, deepseek, a
    user-added provider) follow the same config keys instead of staying unknown. A row of a client outside scope
    keeps its unknown billing: the rules are for the tracked work.
    """
    rules = {name: Billing.rule(rule) for name, rule in config.billing_rules.items() if rule}
    outside = config.outside_scope_clients
    return [
        (
            replace(row, billing=rules[row.provider.value])
            if row.billing is Billing.UNKNOWN
            and row.provider.value in rules
            and not client_outside(row.client, outside)
            else row
        )
        for row in rows
    ]


def _context(
    request: ReportRequest, paths: Paths, config: Config, gathered: _Gathered, plugin: Plugin | None = None
) -> Context:
    settings = (
        {**config.plugin_settings.get(plugin.name, {}), **request.settings.get(plugin.name, {})}
        if plugin
        else {}
    )
    return Context(
        paths,
        config,
        gathered.window,
        request,
        settings,
        gathered.skipped,
        gathered.warnings,
        gathered.book,
        gathered.session,
    )


def _absorb(gathered: _Gathered, source: Source, collected: Collected) -> None:
    """Take a source's rows, items and skips; a wrong shape is a ``TypeError`` (a plugin's guard turns it into a skip)."""
    if not isinstance(collected, Collected):
        raise TypeError(f"collect() must return a Collected, got {type(collected).__name__}")
    items = list(collected.items)
    if not all(isinstance(item, WorkItem) for item in items):
        raise TypeError("collect() must return WorkItem items only")
    rows, skips = _rows_only(collected.rows, "collect()"), _skips_only(
        collected.skipped
    )  # every part checked before any is kept
    estimates = _estimates_only(collected.estimates)
    name = piece_name(source)  # a nameless source fails here, before anything of it is in the report
    writers = sorted(
        {r.source for r in rows if isinstance(r.source, str) and r.source} - {name}
    )  # ADR-0005: the programs a log names
    gathered.rows += _taken(gathered, rows, len(gathered.sources))  # the index of the line said below
    gathered.items += items
    gathered.estimates += estimates
    merge_skips(gathered.skipped, skips)
    if rows or items:  # the checked lists: a generator behind collected.rows has been consumed by now
        suffix = f" ({', '.join(writers)})" if writers else ""
        suffix += f", {len(items)} work items" if items else ""
        gathered.counts[len(gathered.sources)] = _Count(name)
        gathered.sources.append(f"{name} ×{len(rows)}{suffix}")
    if estimates:
        gathered.counts[len(gathered.sources)] = None
        gathered.sources.append(f"{name}: {len(estimates)} estimate(s), in no sum")


def _collect_sources(
    request: ReportRequest, paths: Paths, config: Config, loaded: Loaded, gathered: _Gathered
) -> None:
    """Every built-in source, then every plugin's sources, in load order."""
    for source in BUILTIN:
        _absorb(gathered, source, source.collect(_context(request, paths, config, gathered)))
    for plugin in loaded.plugins:
        ctx = _context(request, paths, config, gathered, plugin)
        for source in _pieces(gathered, plugin, "sources"):
            _guarded_source(gathered, plugin.name, source, ctx)


def _enrich_rows(
    request: ReportRequest, paths: Paths, config: Config, loaded: Loaded, gathered: _Gathered
) -> None:
    """Every plugin's enrichers, in load order — after the per-project filter, which reads the raw workspaces."""
    for plugin in loaded.plugins:
        ctx = _context(request, paths, config, gathered, plugin)
        for enricher in _pieces(gathered, plugin, "enrichers"):
            _guarded_enricher(gathered, plugin.name, enricher, ctx)


def role_pieces(plugin: Plugin, role: str) -> tuple[Any, ...]:
    """A plugin's sources or enrichers as a tuple; a ``TypeError`` naming the role when it is not a sequence."""
    try:
        return tuple(getattr(plugin, role))
    except TypeError as exc:
        raise TypeError(f"{role} is not a sequence ({exc})") from exc


def _pieces(gathered: _Gathered, plugin: Plugin, role: str) -> tuple[Any, ...]:
    """A plugin's sources or enrichers for the report; one that is not a sequence (``None``) costs the plugin that role only."""
    try:
        return role_pieces(plugin, role)
    except TypeError as exc:
        _plugin_failed(gathered, plugin.name, plugin.name, role, exc)
        return ()


def _guarded_source(gathered: _Gathered, plugin: str, source: Source, ctx: Context) -> None:
    """A plugin source that raises (or returns the wrong shape) costs its own rows, never the report."""
    try:
        _absorb(gathered, source, source.collect(ctx))  # a wrong shape fails inside _absorb: guarded as well
    except Exception as exc:
        _plugin_failed(gathered, plugin, piece_name(source), "source", exc)


def _guarded_enricher(gathered: _Gathered, plugin: str, enricher: Enricher, ctx: Context) -> None:
    """A plugin enricher that raises leaves the rows as they were: one counted skip and one warning."""
    try:
        gathered.rows = _enriched(
            gathered.rows, enricher, ctx
        )  # a copy goes in: a raise leaves the rows as they were
    except Exception as exc:
        _plugin_failed(gathered, plugin, piece_name(enricher), "enricher", exc)


def _enriched(rows: list[UsageRow], enricher: Enricher, ctx: Context) -> list[UsageRow]:
    """The enricher's result, checked: rows only, and exactly one per input row — replacements, never drops or copies."""
    result = _rows_only(enricher.enrich(list(rows), ctx))
    if len(result) != len(rows):
        raise TypeError(f"enrich() must return one row per input row, got {len(result)} for {len(rows)}")
    # one row per input row, in order: a row the enricher built afresh keeps the sources line that took its input
    return [
        new if new.intake == old.intake else replace(new, intake=old.intake) for old, new in zip(rows, result)
    ]


def _estimates_only(result: object) -> list[Estimate]:
    """The estimates a source returned, each checked field by field (``estimates.valid``).

    A ``TypeError`` or ``ValueError`` costs the source its piece: a wrong field would fail the report later, in its
    rendering.
    """
    return [valid(item) for item in cast("Iterable[object]", result)]


def _skips_only(result: object) -> list[Skipped]:
    """The skips a source returned, or a ``TypeError``: a string or a foreign item there would break the doctor later."""
    items = list(cast("Iterable[object]", result))
    if not all(isinstance(item, Skipped) for item in items):
        raise TypeError("collect() must return Skipped items only")
    return cast("list[Skipped]", items)


def _rows_only(result: object, what: str = "enrich()") -> list[UsageRow]:
    """A plugin call's rows as a list; ``None``, a non-iterable or a foreign item is a ``TypeError`` naming the call."""
    if result is None or isinstance(result, (str, bytes, Mapping)):
        raise TypeError(f"{what} must return rows, got {type(result).__name__}")
    items = list(cast("Iterable[object]", result))
    if not all(isinstance(item, UsageRow) for item in items):
        raise TypeError(f"{what} must return UsageRow items only")
    rows = cast("list[UsageRow]", items)  # proven above
    for row in rows:  # the fields every group reads: a str provider or billing would fail far from the plugin
        if not isinstance(row.provider, Provider) or not isinstance(row.billing, Billing):
            raise TypeError(
                f"{what} must return rows with a Provider and a Billing, got {row.provider!r} / {row.billing!r}"
            )
        if (
            not isinstance(row.source, str) or not row.source
        ):  # the boundary's promise: every row names its source
            raise TypeError(f"{what} must return rows that name their source, got {row.source!r}")
        _labels_checked(row, what)
    return rows


def _labels_checked(row: UsageRow, what: str) -> None:
    """A row's labels by the writer's and the reader's one rule; a plugin's other shape fails its own piece."""
    try:
        labels_rule(row.labels)
    except ValueError as exc:
        raise TypeError(f"{what} must return rows whose labels follow the label rule: {exc}") from exc


def piece_name(piece: object) -> str:
    """A source's or enricher's ``name``, or its type when a plugin forgot to give it one."""
    name = getattr(piece, "name", None)
    return name if isinstance(name, str) and name else type(piece).__name__


def _plugin_failed(gathered: _Gathered, plugin: str, name: str, role: str, exc: Exception) -> None:
    gathered.skipped.append(
        Skipped(f"plugin {plugin}", name, f"{role} failed: {exc.__class__.__name__}: {exc}")
    )
    gathered.warnings.append(
        f"plugin {plugin}: {role} {name} failed ({exc.__class__.__name__}: {exc}) — its rows are missing"
    )


def load_configured_plugins(config: Config, environ: Mapping[str, str] | None = None) -> Loaded:
    """Bundled modules, then entry points, then ``plugins`` from the config and ``AI_COST_PLUGINS``.

    A module is loaded once however many lists name it; a failure to import is a warning, never an error.
    """
    env = os.environ if environ is None else environ
    return load_plugins(
        configured_modules([*bundled_modules(), *entry_point_modules(), *config.plugins], env)
    )


def _vendor_items(request: ReportRequest, gathered: _Gathered, config: Config) -> list[WorkItem]:
    if request.items:
        return load_items(request.items) if "vendor" in request.groups else []
    if gathered.selection and "vendor" in request.groups:
        gathered.warnings.append(
            "vendor: no quote — the sources' work items name no session; pass --items with the session's scope"
        )
        return []
    if gathered.items and "vendor" in request.groups:
        loc = config.sizing.loc
        gathered.warnings.append(
            f"vendor items = {len(gathered.items)} (one per scope with sizing evidence), sized automatically from the sources' "
            f"line counts (XS≤{loc[0]}, S≤{loc[1]}, M≤{loc[2]}, L≤{loc[3]}) — pass --items for hand-sized scope"
        )
    return gathered.items


def _drop_unpriced(
    gathered: _Gathered, book: PriceBook, config: Config, prices_file: Path, list_priced: bool
) -> None:
    """``--unpriced skip``: a row with neither a list price nor a reported cost leaves the report.

    Such rows are not in ``report.rows`` (so not in any group, ``--detail`` or ``row_count``): each one is a counted
    ``Skipped`` and the header carries one warning per run naming the models — nothing is silently dropped. When no
    requested group prices at list (``real`` alone), only a row the real rules would price at list leaves: a plan row
    (priced by its plan) and an unknown-billing row (listed unpriced by the real group's ``unknown_billing``) stay.
    """
    kept: list[UsageRow] = []
    models: dict[str, int] = {}
    for row in gathered.rows:
        own = row.cost_reported is not None or row.untracked is not None  # a figure of its own is its price
        unpriced = entry_for(row, book) is None and not own
        if unpriced and (list_priced or needs_list_price(row, book, config)):
            name = f"{row.provider.value}/{row.model}"
            models[name] = models.get(name, 0) + 1
            gathered.skipped.append(
                Skipped("pricing", f"{name} ({row.ref})", "no price and no reported cost (--unpriced skip)")
            )
            continue
        kept.append(row)
    if models:
        listing = ", ".join(f"{name} ×{count}" for name, count in sorted(models.items()))
        gathered.warnings.append(f"unpriced rows skipped: {listing} — add them to {prices_file}")
    gathered.rows = kept


def plugins_for(paths: Paths, config: Config) -> Loaded:
    """The plugins a run loads: an instance's own list and nothing else; otherwise the configured discovery."""
    if paths.instance is not None:
        return load_plugins(paths.instance.plugins)
    return load_configured_plugins(config)


def _configured_or_warned(paths: Paths, config: Config) -> Loaded:
    """The run's plugins; a loader that fails (a broken bundle marker) is one header warning, not an abort."""
    try:
        return plugins_for(paths, config)
    except ToolError as exc:
        return Loaded(warnings=[f"plugins: {exc}"])


def build_report(
    request: ReportRequest, paths: Paths, config: Config, book: PriceBook, loaded: Loaded | None = None
) -> Report:
    """Collect every source for the window and build the requested groups.

    ``loaded`` is the plugin set to run (tests pass one); by default the configured plugins are loaded here.
    """
    rules = parse_rules(request.attribute)
    _checked(request, rules, paths)
    if loaded is None:
        loaded = _configured_or_warned(paths, config)
    gathered = _gather(request, paths, config, loaded, book)
    gathered.warnings += loaded.warnings
    compared = _compare_providers(request, paths, config, book, gathered)
    _skip_unpriced(request, paths, config, book, gathered)
    _say_selection(gathered)
    _say_filter(gathered, request.where)
    gathered.warnings += config_warnings(paths, config, book)
    items = _vendor_items(request, gathered, config)
    window, rows = gathered.window, gathered.rows
    # a session is not a period, a label filter not the installation: neither carries a plan share
    priced = without_shares(config) if gathered.selection or request.where else config
    real, api = _priced_groups(request, gathered, book, config, priced)
    estimates = _scoped_estimates(gathered)
    return Report(
        version=__version__,
        generated_at=iso(now()),
        window=window,
        window_iso=(iso(window.start), iso(window.end)),
        sources=gathered.sources,
        warnings=gathered.warnings,
        skipped=gathered.skipped,
        prices_checked_at=book.checked_at.isoformat(),
        rows=rows,
        real=real,
        attribution=_attribution(request, rows, rules, book, priced, window),
        github_bill=gathered.bill,
        provider_reports=compared,
        selection=gathered.selection,
        where=dict(request.where),
        estimates=estimates,
        api=api,
        models=model_lines(rows, api, book, priced) if api else (),
        vendor=vendor_group(items, config, request.vendor_profile) if _vendor_asked(request, items) else None,
    )


def _attribution(
    request: ReportRequest,
    rows: Sequence[UsageRow],
    rules: Sequence[Rule],
    book: PriceBook,
    config: Config,
    window: Window,
) -> AttributionGroup | None:
    """The attribution group: by the ``--attribute`` rules, or by the app's label ``by``; never both."""
    if request.by:
        return label_group(rows, request.by, book, config, window)
    return attribution_group(rows, rules, book, config, window) if rules else None


def _checked(request: ReportRequest, rules: Sequence[Rule], paths: Paths) -> None:
    """The label options by the writer's rule, and the combinations a report refuses (one line each).

    ``--by`` with ``--attribute`` would build one group twice; ``--where`` selects usage rows, so it reads no live
    GitHub count (``--github``) and quotes no hand-sized scope (``--items``) — neither belongs to a selection; and
    ``--github`` reads the machine user's GitHub account over the network: never on an instance.
    """
    if paths.instance is not None:
        _instance_checked(request, paths.instance)
    for name, value in request.where.items():
        checked_label("--where", name, value)
    if request.by:
        checked_label("--by", request.by)
    if request.by and rules:
        raise UsageError("--by and --attribute both build the attribution group: give one")
    for flag, given in (("--github", request.github), ("--items", request.items)):
        if request.where and given:
            raise UsageError(
                f"--where with {flag}: a label filter selects usage rows, {flag} is not one of them"
            )


def _instance_checked(request: ReportRequest, instance: Instance) -> None:
    """What an instance refuses: the machine's GitHub account, a transcript it does not read, a zone-less time."""
    if request.session and not instance.read_clis:  # a session names a coding-CLI transcript, a path included
        raise UsageError(
            "--session reads a coding-CLI transcript: an instance reads one only with --read-clis (Instance.read_clis)"
        )
    if request.github:
        raise UsageError(
            f"--github reads this machine's GitHub account: not on an instance ({ACCOUNTS_UNREAD})"
        )
    zoned(request.since, "--since")
    zoned(request.until, "--until")


def _vendor_asked(request: ReportRequest, items: Sequence[WorkItem]) -> bool:
    """Whether the vendor group is built: asked for, and there is a work item to quote."""
    return "vendor" in request.groups and bool(items)


def _scoped_estimates(gathered: _Gathered) -> Estimates | None:
    """The report's estimates and the header line for what it left out.

    A session report's are split by the session; a per-project report leaves every one out.
    """
    session = gathered.selection.session if gathered.selection else ""
    estimates = scoped(gathered.estimates, session, gathered.project)
    gathered.warnings += left_out_warnings(estimates, gathered.project)
    return estimates


def init_config(paths: Paths, force: bool, emit: Emit) -> int:
    """Write a starter user config from the built-in defaults."""
    from .config import builtin_config

    target = paths.user_config_file()
    if target.exists() and not force:
        for text in init_config_lines(target, written=False):
            emit(text)
        return 0
    defaults = builtin_config()
    ensure_dir(target.parent, "config directory")
    starter = json.dumps({key: defaults[key] for key in ("subscriptions", "providers", "budgets")}, indent=2)
    try:
        target.write_text(starter + "\n")
    except OSError as exc:
        raise ToolError(f"cannot write {target}: {exc}") from exc
    for text in init_config_lines(target, written=True):
        emit(text)
    return 0


# what an installation's operators set most: their plans and their budgets
INSTANCE_CONFIG: Mapping[str, Any] = {
    "subscriptions": [],
    "budgets": {"daily_usd": 0, "monthly_usd": 0, "per_provider_daily_usd": {}},
}


def init_instance(instance: Instance, emit: Emit) -> int:
    """The instance's directory, its usage segments directory and a starting config; what exists is kept as it is."""
    try:
        instance.usage_dir.mkdir(parents=True, exist_ok=True)
        instance.state_dir.mkdir(parents=True, exist_ok=True)
        written = _create(instance.config_file, json.dumps(INSTANCE_CONFIG, indent=2) + "\n")
    except OSError as exc:
        raise ToolError(f"instance init: cannot write {instance.root}: {exc}") from exc
    emit(f"config  {instance.config_file} ({'written' if written else 'kept'})")
    emit(f"usage   {instance.usage_dir}/YYYY-MM.jsonl")
    emit(f"state   {instance.state_dir}")
    return 0


def _create(path: Path, text: str) -> bool:
    """Write ``text`` to a file that does not exist yet, in one exclusive create; one that exists is kept (False)."""
    try:
        with path.open("x", encoding="utf-8") as created:
            created.write(text)
    except FileExistsError:
        return False
    return True


def run_auto_check(paths: Paths, book: PriceBook, quiet: bool, emit_err: Emit) -> None:
    """Report saved drift and start a background check when due."""
    drift, started = auto_check(paths, book)
    if drift and not quiet:
        emit_err(
            "price drift suspected ("
            + ", ".join(f"{p}/{m}" for p, m in drift[:6])
            + ") — run `ai-cost prices check`"
        )
    if started and not quiet:
        emit_err(
            f"price registry last checked {book.checked_at} — a background check was started; its result shows in the next report"
        )
