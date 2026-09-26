"""Operations: the report assembly, doctor, monitor, install/schedule. Commands return exit codes; nothing here exits."""

from __future__ import annotations

import html
import json
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, cast

from . import __version__
from .attribution import attribution_group, parse_rules
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
    probe_bill,
    settled_by,
)
from .collectors.usage_log import log_files
from .config import MAX_WINDOW_HOURS, Config, Paths, PriceBook
from .errors import ToolError, UsageError
from .groups import (
    INVOICE_ATTRIBUTION,
    RealGroup,
    Report,
    api_group,
    needs_list_price,
    real_group,
    vendor_group,
)
from .models import (
    Billing,
    BillSummary,
    Collected,
    Provider,
    RowKind,
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
    Check,
    Mark,
    billing_hint,
    github_checks,
    init_config_lines,
    outside_scope,
    paid_for,
    provider_use,
    setup_checks,
    source_checks,
    source_files,
)
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
from .prices_check import auto_check, load_result, state_file
from .pricing import entry_for
from .timeutil import iso, minutes, now, parse_cli_ts
from .values import merge_skips

Emit = Callable[[str], None]


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
    unpriced: str = "fail"  # fail (exit 5, the default) or skip (count the rows and go on)


def resolve_window(request: ReportRequest, span: Window | None, default_hours: float) -> Window:
    """``--since/--until`` win; else the session span (a minute before, five after); else the last N hours."""
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
        return Window(padded.start - minutes(1), padded.end + minutes(5))
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
    gathered = _Gathered(
        window,
        [],
        [],
        [f"claude transcripts ×{len({p for _, p in files})}"],
        [],
        [*unlisted, *claude.skipped],
        book,
    )
    if not files:
        gathered.warnings.append(
            f"no Claude transcripts matched (project {project or Path.cwd()}, session {request.session})"
        )
    gathered.rows += [row for row in claude.rows if window.contains(row.at)]
    if request.github:  # before the plugins, so their enrichers see every row — the live GitHub rows included
        _gather_github(request, config, book, gathered)
    bill = _gather_bill(request, paths, config, gathered)
    _collect_sources(request, paths, config, loaded, gathered)
    gathered.rows = _inherit_scope_by_ref(gathered.rows)
    per_project = False
    if project is not None and not all_projects:  # the sources' raw working directories, before any enricher
        _keep_project_rows(gathered, project)
        per_project = True
    _absorb_bill(gathered, bill, per_project, config)
    _enrich_rows(request, paths, config, loaded, gathered)
    gathered.rows = _with_billing_rules(gathered.rows, config)
    return gathered


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
    """The configured usage report, read now; ``repos`` (``--github``) are the ones whose Actions lines count."""
    if config.github.bill is None:
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


def _counts(by_source: Mapping[str, int]) -> str:
    return ", ".join(f"{name} ×{count}" for name, count in sorted(by_source.items()))


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
        f"--project {project}: {sum(by_source.values())} {texts[group]} ({_counts(by_source)})"
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
        paths, config, gathered.window, request, settings, gathered.skipped, gathered.warnings, gathered.book
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
    name = _piece_name(source)  # a nameless source fails here, before anything of it is in the report
    writers = sorted(
        {r.source for r in rows if isinstance(r.source, str) and r.source} - {name}
    )  # ADR-0005: the programs a log names
    gathered.rows += rows
    gathered.items += items
    merge_skips(gathered.skipped, skips)
    if rows or items:  # the checked lists: a generator behind collected.rows has been consumed by now
        suffix = f" ({', '.join(writers)})" if writers else ""
        suffix += f", {len(items)} work items" if items else ""
        gathered.sources.append(f"{name} ×{len(rows)}{suffix}")


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


def _role_pieces(plugin: Plugin, role: str) -> tuple[Any, ...]:
    """A plugin's sources or enrichers as a tuple; a ``TypeError`` naming the role when it is not a sequence."""
    try:
        return tuple(getattr(plugin, role))
    except TypeError as exc:
        raise TypeError(f"{role} is not a sequence ({exc})") from exc


def _pieces(gathered: _Gathered, plugin: Plugin, role: str) -> tuple[Any, ...]:
    """A plugin's sources or enrichers for the report; one that is not a sequence (``None``) costs the plugin that role only."""
    try:
        return _role_pieces(plugin, role)
    except TypeError as exc:
        _plugin_failed(gathered, plugin.name, plugin.name, role, exc)
        return ()


def _guarded_source(gathered: _Gathered, plugin: str, source: Source, ctx: Context) -> None:
    """A plugin source that raises (or returns the wrong shape) costs its own rows, never the report."""
    try:
        _absorb(gathered, source, source.collect(ctx))  # a wrong shape fails inside _absorb: guarded as well
    except Exception as exc:
        _plugin_failed(gathered, plugin, _piece_name(source), "source", exc)


def _guarded_enricher(gathered: _Gathered, plugin: str, enricher: Enricher, ctx: Context) -> None:
    """A plugin enricher that raises leaves the rows as they were: one counted skip and one warning."""
    try:
        gathered.rows = _enriched(
            gathered.rows, enricher, ctx
        )  # a copy goes in: a raise leaves the rows as they were
    except Exception as exc:
        _plugin_failed(gathered, plugin, _piece_name(enricher), "enricher", exc)


def _enriched(rows: list[UsageRow], enricher: Enricher, ctx: Context) -> list[UsageRow]:
    """The enricher's result, checked: rows only, and exactly one per input row — replacements, never drops or copies."""
    result = _rows_only(enricher.enrich(list(rows), ctx))
    if len(result) != len(rows):
        raise TypeError(f"enrich() must return one row per input row, got {len(result)} for {len(rows)}")
    return result


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
    return rows


def _piece_name(piece: object) -> str:
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
        unpriced = entry_for(row, book) is None and row.cost_reported is None
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


def _config_warnings(paths: Paths, config: Config, book: PriceBook) -> list[str]:
    """The retired keys, then the config: without a user one the package defaults; with one, a plan per plan rule."""
    return [*_retired(paths, config, book), *_config_state(paths, config, book)]


def _config_state(paths: Paths, config: Config, book: PriceBook) -> list[str]:
    if paths.user_config_file().exists():
        on_plan = sorted(name for name, rule in config.billing_rules.items() if rule == "subscription")
        return [
            f"providers.{name}.billing = subscription but no subscription covers {name}: those plan rows cost "
            f"nothing here — add the plan to {paths.user_config_file()}"
            for name in on_plan
            if not paid_for(name, config, book)
        ]
    if not config.subscriptions:
        return [
            "no user config — no subscriptions configured (the package ships none); run `ai-cost install --init-config` and add yours"
        ]
    plans = ", ".join(s.plan for s in config.subscriptions)
    return [f"no user config — subscriptions are the defaults ({plans}); run `ai-cost install --init-config`"]


def _retired(paths: Paths, config: Config, book: PriceBook) -> list[str]:
    """The keys the user's files still set that 2.6 no longer reads: a Copilot review has no price (ADR-0007)."""
    said = [
        f"{key} is no longer read (a Copilot review has no price since 2.6): {BILL_HINT}"
        for key in config.github.retired
    ]
    return said + [
        f"{key} in {paths.user_prices_file()} is no longer read (a Copilot review has no price since 2.6) — remove it"
        for key in book.retired
    ]


def _billing_warnings(real: RealGroup | None, rows: Sequence[UsageRow], config: Config) -> list[str]:
    """Copilot counts without a usage report, and rows of unknown billing (priced by the API group only).

    Those of clients outside scope are said as such; the rest are counted per provider with the rule to set.
    """
    counted = _copilot_counted(rows, config) + _plans_beside_seats(real)
    if real is None or not real.unknown_billing:
        return counted
    return counted + _outside_warning(outside_scope(rows, config)) + _unknown_warning(real, rows, config)


def _plans_beside_seats(real: RealGroup | None) -> list[str]:
    """A configured GitHub plan still booked while the usage report states seats: said, the user decides."""
    shares = real.subscriptions if real is not None else ()
    seats = sorted({s.plan for s in shares if s.attribution == INVOICE_ATTRIBUTION})
    kept = sorted(
        s.plan for s in shares if s.provider == "github" and s.attribution != INVOICE_ATTRIBUTION and s.usd
    )
    if not seats or not kept:
        return []
    return [
        f"subscription {', '.join(kept)} stays booked beside the seats the GitHub usage report bills "
        f"({', '.join(seats)}) — remove it from subscriptions if those seats replaced it"
    ]


def _copilot_counted(rows: Sequence[UsageRow], config: Config) -> list[str]:
    """Copilot reviews without a usage report: counted, never priced — said, so a total is never read as complete."""
    reviews = sum(row.tokens.reviews for row in rows if row.kind is RowKind.COPILOT)
    if not reviews or config.github.bill is not None:
        return []
    return [f"{reviews} Copilot review(s) counted, not priced (a review has no price) — {BILL_HINT}"]


def _outside_warning(clients: Mapping[str, int]) -> list[str]:
    if not clients:
        return []
    return [
        f"{sum(clients.values())} usage row(s) from clients outside scope ({', '.join(sorted(clients))}) have "
        "unknown billing and stay out of the real group on purpose (outside_scope_clients); the API-only group "
        "still prices their tokens"
    ]


def _unknown_warning(real: RealGroup, rows: Sequence[UsageRow], config: Config) -> list[str]:
    """The unknown rows a rule or a plugin could place, per provider, and the ledger rows without a figure."""
    outside = {use.provider: use.outside for use in provider_use(rows, config.outside_scope_clients)}
    left = {name: n - outside.get(name, 0) for name, n in sorted(real.unknown_by_provider.items())}
    left = {name: n for name, n in left.items() if n}
    unfigured = real.unfigured_ledger
    if not left and not unfigured:
        return []
    counts = [f"{name} {n}" for name, n in left.items()]
    counts += [f"{unfigured} ledger row(s) without a figure, counted nowhere else"] if unfigured else []
    rules = "; ".join(f"providers.{name}.billing — {billing_hint(name)}" for name in left)
    fix = f"set {rules}; or add" if left else "add"
    return [
        f"{sum(left.values()) + unfigured} usage row(s) with unknown billing are left out of the real group "
        f"({', '.join(counts)}; the API group prices the ones with tokens) — {fix} a plugin that knows how those "
        "sessions were paid"
    ]


def _configured_or_warned(config: Config) -> Loaded:
    """The configured plugins; a loader that fails (a broken bundle marker) is one header warning, not an abort."""
    try:
        return load_configured_plugins(config)
    except ToolError as exc:
        return Loaded(warnings=[f"plugins: {exc}"])


def build_report(
    request: ReportRequest, paths: Paths, config: Config, book: PriceBook, loaded: Loaded | None = None
) -> Report:
    """Collect every source for the window and build the requested groups.

    ``loaded`` is the plugin set to run (tests pass one); by default the configured plugins are loaded here.
    """
    rules = parse_rules(request.attribute)
    if loaded is None:
        loaded = _configured_or_warned(config)
    gathered = _gather(request, paths, config, loaded, book)
    gathered.warnings += loaded.warnings
    if request.unpriced == "skip":
        list_priced = "api" in request.groups or bool(request.attribute)
        _drop_unpriced(gathered, book, config, paths.user_prices_file(), list_priced=list_priced)
    gathered.warnings += _config_warnings(paths, config, book)
    items = _vendor_items(request, gathered, config)
    window, rows = gathered.window, gathered.rows
    real = real_group(rows, book, config, window) if "real" in request.groups else None
    gathered.warnings += _billing_warnings(real, rows, config)
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
        attribution=attribution_group(rows, rules, book, config, window) if rules else None,
        github_bill=gathered.bill,
        api=api_group(rows, book, config) if "api" in request.groups else None,
        vendor=(
            vendor_group(items, config, request.vendor_profile)
            if "vendor" in request.groups and items
            else None
        ),
    )


# ---- doctor -----------------------------------------------------------------------------------------------------


Line = Callable[[bool, str], None]


def _doctor_window(paths: Paths, config: Config, book: PriceBook, line: Line) -> Report | None:
    """The last 24 h, collected once: the skipped records and the setup lines read the same rows."""
    try:
        return build_report(ReportRequest(all_projects=True, hours=24.0, groups=()), paths, config, book)
    except ToolError as exc:
        line(False, f"collection over the last 24 h failed: {exc}")
        return None


def _doctor_skipped(report: Report, line: Line) -> None:
    """What the collectors could not use in the last 24 h, per source (the counted records of Invariant #14d)."""
    by_source = Counter(item.source for item in report.skipped)
    detail = ", ".join(f"{name} {count}" for name, count in sorted(by_source.items())) or "none"
    line(
        True,
        f"skipped records in the last 24 h: {sum(by_source.values())} ({detail}) — `report --detail` lists them",
    )


def _render(checks: Iterable[Check], line: Line, emit: Emit) -> None:
    """A verdict goes through ``line`` (a problem counts); information is printed and never counted."""
    for check in checks:
        if check.mark is Mark.INFO:
            emit(f"  --  {check.text}")
        else:
            line(check.mark is Mark.OK, check.text)


def doctor(paths: Paths, config: Config, book: PriceBook, emit: Emit) -> int:
    """Sources, config, price freshness, schedule; exit 1 on any problem."""
    problems = 0

    def line(good: bool, text: str) -> None:
        nonlocal problems
        emit(("  ok  " if good else "  !!  ") + text)
        problems += 0 if good else 1

    emit(f"ai-cost {__version__} doctor")
    _doctor_sources(paths, config, book, line, emit)
    _doctor_prices(book, line)
    _doctor_state(paths, line, emit, config.github.bill is not None)
    emit(f"doctor: {'all good' if problems == 0 else f'{problems} issue(s)'}")
    return 0 if problems == 0 else 1


def _doctor_sources(paths: Paths, config: Config, book: PriceBook, line: Line, emit: Emit) -> None:
    """Python, the files of every source, the window's skipped records, the setup lines, plugins, user files."""
    line(sys.version_info >= (3, 9), f"python {sys.version.split()[0]}")
    found = source_files(paths)
    report = _doctor_window(paths, config, book, line)
    rows = list(report.rows) if report else []
    _render(source_checks(found, log_files(paths, config), len(rows) if report else None), line, emit)
    if report is not None:
        _doctor_skipped(report, line)
    _render(setup_checks(found, config, book, rows), line, emit)
    request = bill_request(paths, config)
    probe = probe_bill(request) if request is not None else None
    _render(github_checks(config, provider_use(rows, config.outside_scope_clients), probe), line, emit)
    _doctor_plugins(paths, config, line, emit)
    user_config = paths.user_config_file()
    emit(
        ("  ok  " if user_config.exists() else "  --  ")
        + f"user config {user_config} "
        + (
            "present"
            if user_config.exists()
            else "absent → defaults (ai-cost install --init-config writes it)"
        )
    )
    user_prices = paths.user_prices_file()
    emit(
        ("  ok  " if user_prices.exists() else "  --  ")
        + f"user prices {user_prices} {'present' if user_prices.exists() else 'absent → built-in registry'}"
    )


def _doctor_plugins(paths: Paths, config: Config, line: Line, emit: Emit) -> None:
    """One line per loaded plugin (its sources), its own doctor lines, and every module that failed to load."""
    try:
        loaded = load_configured_plugins(config)
    except ToolError as exc:  # a broken bundle marker: one red line, the rest of the doctor still runs
        line(False, f"plugins: {exc}")
        return
    for warning in loaded.warnings:
        line(False, warning)
    if not loaded.plugins:
        emit("  --  plugins: none (built-in sources only)")
    end = now()
    window = Window(end - timedelta(hours=config.window_default_hours), end)
    for plugin in loaded.plugins:
        try:
            names = (
                ", ".join(_piece_name(source) for source in _role_pieces(plugin, "sources")) or "no sources"
            )
        except TypeError as exc:  # sources=None: diagnosed, the other plugins still listed
            line(False, f"plugin {plugin.name}: {exc}")
            continue
        line(True, f"plugin {plugin.name}: {names}")
        if plugin.doctor is not None:
            settings = config.plugin_settings.get(plugin.name, {})
            try:
                plugin.doctor(Context(paths, config, window, ReportRequest(), settings), line)
            except Exception as exc:
                line(False, f"plugin {plugin.name}: doctor failed ({exc.__class__.__name__}: {exc})")


def _doctor_prices(book: PriceBook, line: Line) -> None:
    age = (now().date() - book.checked_at).days
    if book.auto_check_days > 0:
        line(
            age <= book.auto_check_days * 3,
            f"prices checked_at {book.checked_at} ({age} days ago; auto-check every {book.auto_check_days} days)",
        )
    else:
        line(
            True,
            f"prices checked_at {book.checked_at} ({age} days ago; the drift check is off: auto_check_days 0)",
        )
    today = now().date()
    for provider, table in book.models.items():
        for model, entry in table.items():
            until = getattr(entry, "valid_until", None)
            if until:
                line(
                    until >= today,
                    f"{provider.value}/{model} price valid until {until}"
                    + (
                        ""
                        if until >= today
                        else " — expired: "
                        + (
                            "the next block applies"
                            if getattr(entry, "next", None)
                            else "NO next block, update prices.json"
                        )
                    ),
                )


def _doctor_state(paths: Paths, line: Line, emit: Emit, bill: bool = False) -> None:
    """The last price check, the state directory, the tools and the scheduled jobs."""
    last = load_result(paths)
    emit(
        ("  ok  " if last else "  --  ")
        + f"last price check: {last.checked_at if last else 'never'} ({state_file(paths)})"
    )
    state = paths.state_dir
    if _writable(state):
        line(
            True, f"state dir {state} writable" + ("" if state.exists() else " (created on the first write)")
        )
    else:
        line(False, f"state dir {state} is not writable — AI_COST_STATE_DIR, or fix the permissions")
    _doctor_tools(emit)
    _doctor_reports(paths, line, emit, bill)


def _doctor_tools(emit: Emit) -> None:
    """Informational only: neither tool is required, so nothing here counts as a problem."""
    has_gh = shutil.which("gh") is not None
    emit(
        ("  ok  " if has_gh else "  --  ")
        + f"gh {'present' if has_gh else 'absent'} (needed only for --github and providers.github.bill)"
    )
    emit(
        ("  ok  " if schedule_installed() else "  --  ")
        + f"scheduled price check: {'installed' if schedule_installed() else 'not installed (ai-cost install --schedule 3)'}"
    )


def _doctor_daily_job(line: Line, emit: Emit, bill: bool) -> None:
    """The daily-report job: absent is information; one installed before 2.6 cannot find gh for the usage report."""
    installed = schedule_installed(DAILY_JOB)
    if installed and bill and not job_has_path(DAILY_JOB):
        line(
            False,
            "scheduled daily reports: installed without a PATH — the job cannot find gh to read the "
            "GitHub usage report; run ai-cost install --schedule-reports again",
        )
        return
    emit(
        ("  ok  " if installed else "  --  ")
        + "scheduled daily reports: "
        + ("installed" if installed else "not installed (ai-cost install --schedule-reports)")
    )


def _doctor_reports(paths: Paths, line: Line, emit: Emit, bill: bool = False) -> None:
    """The daily-report job and what it last wrote, the last reconciliation: absent is informational, torn is red."""
    from .daily import newest_index_file, read_index  # both modules build on this one: imported here only
    from .reconcile import last_reconciliation

    _doctor_daily_job(line, emit, bill)
    index = newest_index_file(paths.reports_dir)
    if index is None:
        emit(f"  --  daily reports: none yet in {paths.reports_dir} (ai-cost daily)")
    else:
        try:
            last = read_index(index)
            failed = f", {len(last.failures)} project(s) FAILED — see its index.json" if last.failures else ""
            line(
                not last.failures,
                f"daily reports: last {last.day} ({len(last.projects)} project(s){failed}) in {paths.reports_dir}",
            )
        except ToolError as exc:
            line(False, f"daily reports: {exc}")
    try:
        check = last_reconciliation(paths)
    except ToolError as exc:
        line(False, f"last reconcile: {exc}")
        return
    if check is None:
        emit("  --  last reconcile: never (ai-cost reconcile --provider <id> --usd <console figure>)")
    else:
        verdict = "within tolerance" if check.within_tolerance else "ABOVE TOLERANCE"
        line(
            check.within_tolerance,
            f"last reconcile: {check.provider} {check.window[0]} → {check.window[1]}: {verdict}",
        )


def _writable(path: Path) -> bool:
    """A directory that can be written, or created: its nearest existing ancestor is a writable directory."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        if probe.is_symlink():  # a dangling link: the first write would fail on it, not create a directory
            return False
        probe = probe.parent
    return probe.is_dir() and os.access(probe, os.W_OK)


# ---- monitor ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorEntry:
    """One history line; ``outside`` names the amounts the totals leave out (GitHub days, unread months)."""

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
        outside=bill_outside(report.github_bill),
    )


def _append_history(path: Path, entry: Any) -> None:
    """One JSON line per monitor run; a state directory that cannot be written is a ``ToolError``, not a traceback."""
    append_json_line(path, entry.__dict__)


def append_json_line(path: Path, data: Mapping[str, Any]) -> None:
    """One JSON object per line, appended; a path that cannot be written is a ``ToolError``, never a traceback."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(data) + "\n")
    except OSError as exc:
        raise ToolError(f"cannot write {path}: {exc}") from exc


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


# ---- install / schedule -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One scheduled command of this program: launchd label ``ai-cost.<label>``, cron tag ``# ai-cost:<label>``."""

    label: str
    argv_tail: tuple[str, ...]  # after the command that starts this program
    log_name: str  # its log file, under the state directory


@dataclass(frozen=True)
class Cadence:
    """When a job runs: every ``days`` days, or (``days`` 0) every day at ``hour``:``minute`` local time."""

    days: int = 0
    hour: int = 6
    minute: int = 17


PRICES_JOB = Job("prices-check", ("prices", "check", "--quiet"), "prices-check.log")
DAILY_JOB = Job("daily-report", ("daily",), "daily-report.log")
DAILY_CADENCE = Cadence(hour=6, minute=40)


def plist_path(job: Job = PRICES_JOB) -> Path:
    """MacOS launchd agent file."""
    return Path.home() / "Library" / "LaunchAgents" / f"ai-cost.{job.label}.plist"


def plist_paths(job: Job = PRICES_JOB) -> list[Path]:
    """The agent file plus any earlier label of the same job (``<prefix>.ai-cost.<label>.plist``) still on disk."""
    current = plist_path(job)
    earlier = sorted(p for p in current.parent.glob(f"*.ai-cost.{job.label}.plist") if p != current)
    return [current, *earlier]


def is_macos() -> bool:
    """A runtime check (a ``sys.platform`` literal would let the type checker prune the other branch)."""
    return platform.system() == "Darwin"


CRON_TAG = "# ai-cost:prices-check"  # the price check's tag (``cron_tag(PRICES_JOB)``), kept by name
_LEGACY_CRON = re.compile(
    r"ai[-_]cost['\"]?\s+prices\s+check"
)  # price-check lines written before the tag, quoted paths included


def cron_tag(job: Job) -> str:
    """The comment that marks a job's crontab line whatever the command looks like."""
    return f"# ai-cost:{job.label}"


def _is_our_cron_line(line: str, job: Job) -> bool:
    legacy = job is PRICES_JOB and _LEGACY_CRON.search(line) is not None
    return cron_tag(job) in line or legacy


def cron_has_entry(crontab: str, job: Job = PRICES_JOB) -> bool:
    """Whether a crontab text carries the job (tagged, or a pre-tag price-check line)."""
    return any(_is_our_cron_line(line, job) for line in crontab.splitlines())


def cron_without_entry(crontab: str, job: Job = PRICES_JOB) -> list[str]:
    """The crontab lines minus the job's (tagged or legacy)."""
    return [line for line in crontab.splitlines() if not _is_our_cron_line(line, job)]


def _cron_when(cadence: Cadence) -> str:
    """The five cron fields: every N days at 06:17, or every day at the given time."""
    if cadence.days:
        return f"17 6 */{cadence.days} * *"
    return f"{cadence.minute} {cadence.hour} * * *"


def cron_line(days: int, argv: Sequence[str], log: Path) -> str:
    """The price check's crontab line: every ``days`` days at 06:17, quoted for the shell, tagged."""
    return job_cron_line(PRICES_JOB, Cadence(days=days), argv, log)


def job_cron_line(job: Job, cadence: Cadence, argv: Sequence[str], log: Path, search_path: str = "") -> str:
    """A job's crontab line, quoted for the shell and tagged so it can be found and removed.

    ``search_path`` is the PATH of the shell that installed it: cron's own finds neither ``gh`` nor a Homebrew tool.
    """
    command = shlex.join(list(argv))
    env = f"PATH={shlex.quote(search_path)} " if search_path else ""
    return f"{_cron_when(cadence)} {env}{command} >> {shlex.quote(str(log))} 2>&1 {cron_tag(job)}"


def _crontab_write(text: str) -> bool:
    try:
        return (
            subprocess.run(
                ["crontab", "-"], input=text, text=True, capture_output=True, check=False
            ).returncode
            == 0
        )
    except OSError:
        return False


def _crontab() -> str:
    try:
        return subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return ""


def schedule_installed(job: Job = PRICES_JOB) -> bool:
    """Whether the job is registered (launchd or cron)."""
    if is_macos():
        return any(p.exists() for p in plist_paths(job))
    return cron_has_entry(_crontab(), job)


def job_has_path(job: Job) -> bool:
    """Whether the registered job carries a PATH of its own (installed by 2.6 or later)."""
    if is_macos():
        try:
            return "<key>PATH</key>" in plist_path(job).read_text()
        except OSError:
            return False
    return any(" PATH=" in line for line in _crontab().splitlines() if _is_our_cron_line(line, job))


def describe_cadence(cadence: Cadence) -> str:
    """``every 3 day(s)`` or ``daily at 06:40``."""
    if cadence.days:
        return f"every {cadence.days} day(s)"
    return f"daily at {cadence.hour:02d}:{cadence.minute:02d}"


def _launchd_when(cadence: Cadence) -> str:
    if cadence.days:
        return f"<key>StartInterval</key><integer>{cadence.days * 86400}</integer>"
    return (
        "<key>StartCalendarInterval</key><dict>"
        f"<key>Hour</key><integer>{cadence.hour}</integer><key>Minute</key><integer>{cadence.minute}</integer>"
        "</dict>"
    )


def _plist(job: Job, cadence: Cadence, argv: Sequence[str], log: Path, search_path: str = "") -> str:
    """The launchd agent; ``search_path`` (the installing shell's PATH) replaces launchd's bare /usr/bin:/bin one."""
    program = "".join(f"<string>{html.escape(arg)}</string>" for arg in argv)
    out = html.escape(str(log))
    env = (
        f"  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{html.escape(search_path)}</string></dict>\n"
        if search_path
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>Label</key><string>ai-cost.{job.label}</string>\n"
        f"  <key>ProgramArguments</key><array>{program}</array>\n"
        f"{env}"
        f"  {_launchd_when(cadence)}\n"
        f"  <key>StandardOutPath</key><string>{out}</string><key>StandardErrorPath</key><string>{out}</string>\n"
        "  <key>RunAtLoad</key><false/>\n"
        "</dict></plist>\n"
    )


def install_schedule(paths: Paths, days: int, command: Sequence[str], emit: Emit) -> int:
    """Register the periodic price check (launchd on macOS, cron elsewhere); ``command`` starts this program."""
    return install_job(paths, PRICES_JOB, Cadence(days=days), command, emit)


def install_job(paths: Paths, job: Job, cadence: Cadence, command: Sequence[str], emit: Emit) -> int:
    """Register one job (launchd on macOS, cron elsewhere); ``command`` starts this program."""
    ensure_dir(paths.state_dir, "state directory")
    log = paths.state_dir / job.log_name
    argv = [*command, *job.argv_tail]
    search_path = os.environ.get("PATH", "")  # the job finds what this shell finds (gh for the usage report)
    if is_macos():
        return _install_agent(
            _plist(job, cadence, argv, log, search_path), describe_cadence(cadence), emit, job
        )
    lines = [*cron_without_entry(_crontab(), job), job_cron_line(job, cadence, argv, log, search_path)]
    if not _crontab_write("\n".join(lines) + "\n"):
        emit("crontab write failed — is crontab installed and allowed for this user?")
        return 1
    emit(f"cron entry installed ({describe_cadence(cadence)})")
    return 0


def ensure_dir(path: Path, what: str) -> None:
    """Create ``path`` with its parents; a file in the way or a permission is a ToolError, never a traceback."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"cannot create the {what} {path}: {exc}") from exc


def _install_agent(plist: str, when: str, emit: Emit, job: Job = PRICES_JOB) -> int:
    """Write the launchd agent, retire every earlier label, load it: 0 only when it loads AND every earlier label is gone."""
    target = plist_path(job)
    ensure_dir(target.parent, "LaunchAgents directory")
    try:
        target.write_text(plist)
    except OSError as exc:
        raise ToolError(f"cannot write the launchd agent {target}: {exc}") from exc
    retired = [_remove_plist(path, emit) for path in plist_paths(job) if path != target]
    subprocess.run(["launchctl", "unload", str(target)], capture_output=True, check=False)
    result = subprocess.run(["launchctl", "load", str(target)], capture_output=True, text=True, check=False)
    state = "loaded" if result.returncode == 0 else f"written (load failed: {result.stderr.strip()})"
    emit(f"launchd agent {state} {when}: {target}")
    return 0 if result.returncode == 0 and False not in retired else 1  # a label that stayed is said above


def _remove_plist(path: Path, emit: Emit) -> bool | None:
    """Unload and delete one agent file: ``None`` when there was none, ``False`` when it could not be removed."""
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
    if not path.exists():
        return None
    try:
        path.unlink()
    except OSError as exc:  # a directory in its place, a permission: said, never a traceback
        emit(f"cannot remove {path}: {exc}")
        return False
    emit(f"removed {path}")
    return True


def remove_schedule(emit: Emit) -> int:
    """Unregister the periodic price check."""
    return remove_job(PRICES_JOB, emit)


def remove_job(job: Job, emit: Emit) -> int:
    """Unregister one job; every label it ever had goes, none keeps running."""
    if is_macos():
        outcomes = [_remove_plist(path, emit) for path in plist_paths(job)]
        if not any(outcome is not None for outcome in outcomes):
            emit("not installed")
        return 1 if False in outcomes else 0
    crontab = _crontab()
    if not cron_has_entry(crontab, job):
        emit("not installed")
        return 0
    kept = "\n".join(cron_without_entry(crontab, job)) + "\n"
    if not _crontab_write(kept):
        emit("crontab write failed — is crontab installed and allowed for this user?")
        return 1
    emit("cron entry removed")
    return 0


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
