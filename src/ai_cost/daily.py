"""Daily reports (ADR-0006): one run writes a day's global report and one per project touched that day.

``ai-cost daily`` — the job ``install --schedule-reports`` registers — prices the UTC day ``[00:00, 24:00)`` once
over every project and once per Claude project directory whose transcripts were written in it; each project report
is the ``--project`` scope over every source (rows of other working directories left out and counted). It writes
``<reports dir>/<date>/global.{md,json}``, ``<project dir name>.{md,json}`` and ``index.json`` (the totals per
file and the notes: a project whose working directory no transcript names, a report that failed). ``doctor`` shows
the newest index. Unpriced rows are skipped and counted, so a new model never fails the job.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Any

from .config import Config, Paths, PriceBook
from .errors import ToolError
from .groups import Report, SubscriptionShare
from .models import Window
from .ops import Emit, ReportRequest, build_report, ensure_dir
from .render import plain, render_json, render_markdown
from .timeutil import iso, now

HEAD_LINES = 500  # transcript lines read to find the working directory: every entry carries ``cwd``


@dataclass(frozen=True)
class ProjectReport:
    """One project's files of a daily run and its two totals."""

    project_dir: str  # the Claude project directory name
    path: str  # the working directory it stands for
    markdown: str
    json_file: str
    real_usd: float
    api_usd: float
    rows: int


@dataclass(frozen=True)
class DailyIndex:
    """What one run wrote: the ``index.json`` of the day's directory."""

    day: str
    generated_at: str
    window: tuple[str, str]
    directory: str
    real_usd: float
    api_usd: float
    cash_usd: float
    rows: int
    projects: tuple[ProjectReport, ...]
    notes: tuple[str, ...]
    failures: tuple[str, ...] = ()  # a project whose report or files failed: said on stderr, exit 1


@dataclass(frozen=True)
class Output:
    """Where a run speaks: ``emit`` for the summary and the notes (``--quiet`` drops them), ``err`` for failures."""

    emit: Emit
    err: Emit


@dataclass
class _Built:
    """One project's report before its files are written (the plan shares are split across all of them first)."""

    folder: Path
    path: Path
    report: Report


def day_window(day: date) -> Window:
    """The UTC day, half-open: ``[00:00, 24:00)``."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return Window(start, start + timedelta(days=1))


def yesterday() -> date:
    """The default day of a run: the UTC day before today (a job in the morning prices the finished day)."""
    return (now() - timedelta(days=1)).date()


def _stamp(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


def projects_touched(claude_home: Path, window: Window) -> list[Path]:
    """Claude project directories with a transcript written at or after the window start (a session ran in it)."""
    since = (window.start - timedelta(minutes=1)).timestamp()
    folders = sorted(p for p in (claude_home / "projects").glob("*") if p.is_dir())
    return [folder for folder in folders if any(_stamp(f) >= since for f in folder.rglob("*.jsonl"))]


def _head(path: Path, limit: int) -> Iterator[str]:
    """The first ``limit`` lines of a file; an unreadable file is no lines."""
    try:
        with path.open(errors="replace") as handle:
            yield from islice(handle, limit)
    except OSError:
        return


def _cwd_of(line: str) -> str:
    """The ``cwd`` of one transcript entry, or ``""``."""
    if '"cwd"' not in line:
        return ""
    try:
        record = json.loads(line)
    except ValueError:
        return ""
    cwd = record.get("cwd") if isinstance(record, Mapping) else None
    return cwd if isinstance(cwd, str) else ""


def _encodes_to(cwd: str, folder: Path) -> bool:
    """Whether a recorded ``cwd`` is the one the project directory's name encodes (``/`` and ``.`` as ``-``).

    The raw string or its real path (a symlinked home) must match: a stray entry from another directory never
    scopes a project (Grok, PR #90 r1).
    """
    return any(re.sub(r"[/\\.]", "-", c) == folder.name for c in {cwd, os.path.realpath(cwd)})


def project_path(project_dir: Path) -> Path | None:
    """The working directory a Claude project directory stands for: the ``cwd`` its newest transcripts record.

    The directory name encodes the path with every ``/`` and ``.`` as ``-``, which cannot be reversed; the entries
    inside carry it verbatim, and only a ``cwd`` that encodes back to this directory's name counts. ``None`` when
    none of the three newest transcripts says.
    """
    transcripts = sorted(project_dir.glob("*.jsonl"), key=_stamp, reverse=True)
    for transcript in transcripts[:3]:
        for line in _head(transcript, HEAD_LINES):
            cwd = _cwd_of(line)
            if cwd and _encodes_to(cwd, project_dir):
                return Path(cwd)
    return None


def _request(window: Window, project: Path | None) -> ReportRequest:
    """The day's request: real and api groups; unpriced rows skipped and counted (a job never fails on a new model)."""
    return ReportRequest(
        project=project,
        all_projects=project is None,
        since=iso(window.start),
        until=iso(window.end),
        groups=("real", "api"),
        unpriced="skip",
    )


def _write_pair(report: Report, base: Path) -> tuple[Path, Path]:
    """``<base>.md`` and ``<base>.json``; a directory that cannot be written is a ``ToolError``."""
    markdown, machine = base.with_suffix(".md"), base.with_suffix(".json")
    try:
        markdown.write_text(render_markdown(report), encoding="utf-8")
        machine.write_text(render_json(report, detail=False), encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"cannot write {base}.md / .json: {exc}") from exc
    return markdown, machine


def _totals(report: Report) -> tuple[float, float, float]:
    """``(real, cash, api)`` of a report; 0 for a group that was not built."""
    real = report.real.total_usd if report.real else 0.0
    cash = report.real.cash_usd if report.real else 0.0
    api = report.api.total_usd if report.api else 0.0
    return real, cash, api


def _empty_reason(report: Report) -> str:
    """Why a project's day has no rows: nothing was used, or everything it used has no price."""
    if any(skip.source == "pricing" for skip in report.skipped):
        return (
            "every row of the day is unpriced (add the models to your prices file) — not reported per project"
        )
    return "no usage rows in the day — not reported per project"


def _build_projects(
    paths: Paths, config: Config, book: PriceBook, window: Window
) -> tuple[list[_Built], list[str], list[str]]:
    """One report per project touched in the window: built, or a note (nothing to report), or a failure (an error)."""
    built: list[_Built] = []
    notes: list[str] = []
    failures: list[str] = []
    for folder in projects_touched(paths.claude_home, window):
        path = project_path(folder)
        if path is None:
            notes.append(
                f"{folder.name}: no transcript names its working directory — not reported per project"
            )
            continue
        try:
            report = build_report(_request(window, path), paths, config, book)
        except ToolError as exc:
            failures.append(f"{folder.name}: {exc}")
            continue
        if not report.rows:
            notes.append(f"{folder.name}: {_empty_reason(report)}")
            continue
        built.append(_Built(folder, path, report))
    return built, notes, failures


def _api_by_provider(report: Report) -> dict[str, float]:
    """The project's API-equivalent cost per provider: its weight in the split of that provider's plan."""
    usd: dict[str, float] = {}
    for line in report.api.lines if report.api else []:
        usd[line.provider.value] = usd.get(line.provider.value, 0.0) + line.usd
    return usd


def _scaled(share: SubscriptionShare, weight: float, total: float) -> SubscriptionShare:
    """The share's part for one project; the share's own note (an unknown plan, say) stays in front of the split's."""
    if total <= 0:
        text = "no project used this provider that day: the share is in global.md"
        return replace(share, usd=0.0, note=_joined(share.note, text))
    text = "the project's part, by its API-equivalent share"
    return replace(share, usd=share.usd * weight / total, note=_joined(share.note, text))


def _joined(first: str, second: str) -> str:
    return f"{first}; {second}" if first else second


def _share_plans(built: list[_Built]) -> None:
    """Split each plan's day share between the projects by their API-equivalent cost of the plan's provider.

    ADR-0003's policy, applied across the day's projects so their files sum to the global share instead of each
    repeating it (DeepSeek, PR #90 r1). A provider no project used keeps its share in the global report only.
    """
    weights = [_api_by_provider(item.report) for item in built]
    totals: dict[str, float] = {}
    for by_provider in weights:
        for provider, usd in by_provider.items():
            totals[provider] = totals.get(provider, 0.0) + usd
    note = "subscription shares here are this project's part of the day's plans (split by API-equivalent share, ADR-0003); the whole shares are in global.md"
    for item, by_provider in zip(built, weights):
        real = item.report.real
        if real is None:
            continue
        shares = [
            _scaled(s, by_provider.get(s.provider, 0.0), totals.get(s.provider, 0.0))
            for s in real.subscriptions
        ]
        item.report = replace(
            item.report, real=replace(real, subscriptions=shares), warnings=[*item.report.warnings, note]
        )


def _write_projects(built: list[_Built], directory: Path) -> tuple[list[ProjectReport], list[str]]:
    """The files of every built project; a project whose files cannot be written is a failure, the others still land."""
    reports: list[ProjectReport] = []
    failures: list[str] = []
    for item in built:
        try:
            markdown, machine = _write_pair(item.report, directory / item.folder.name)
        except ToolError as exc:
            failures.append(f"{item.folder.name}: {exc}")
            continue
        real, _, api = _totals(item.report)
        reports.append(
            ProjectReport(
                item.folder.name,
                str(item.path),
                str(markdown),
                str(machine),
                real,
                api,
                len(item.report.rows),
            )
        )
    return reports, failures


def daily_reports(paths: Paths, config: Config, book: PriceBook, day: date, out: Path, output: Output) -> int:
    """Write the day's global report, one per project and the index; exit 1 when a project failed (said on ``err``)."""
    emit, err = output.emit, output.err
    window = day_window(day)
    directory = out / day.isoformat()
    ensure_dir(directory, "reports directory")
    overall = build_report(_request(window, None), paths, config, book)
    _write_pair(overall, directory / "global")
    built, notes, failures = _build_projects(paths, config, book, window)
    _share_plans(built)
    projects, write_failures = _write_projects(built, directory)
    failures += write_failures
    real, cash, api = _totals(overall)
    index = DailyIndex(
        day=day.isoformat(),
        generated_at=iso(now()),
        window=overall.window_iso,
        directory=str(directory),
        real_usd=real,
        api_usd=api,
        cash_usd=cash,
        rows=len(overall.rows),
        projects=tuple(projects),
        notes=tuple(notes),
        failures=tuple(failures),
    )
    _write_index(directory / "index.json", index)
    emit(
        f"daily {day}: real {real:.2f} USD, api-only {api:.2f} USD, {len(overall.rows)} row(s), "
        f"{len(projects)} project(s) → {directory}"
    )
    for note in notes:
        emit(f"  note: {note}")
    for failure in failures:
        err(f"daily {day}: FAIL {failure}")
    return 1 if failures else 0


def _write_index(path: Path, index: DailyIndex) -> None:
    try:
        path.write_text(json.dumps(plain(index), indent=2), encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"cannot write {path}: {exc}") from exc


def newest_index_file(reports_dir: Path) -> Path | None:
    """The ``index.json`` of the latest day directory (dates sort as names), or ``None`` when there is none."""
    files = sorted(reports_dir.glob("*/index.json")) if reports_dir.is_dir() else []
    return files[-1] if files else None


def read_index(path: Path) -> DailyIndex:
    """An ``index.json`` back as a ``DailyIndex``; a file that is not one is a ``ToolError`` naming it."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        projects = tuple(ProjectReport(**_fields(ProjectReport, item)) for item in data["projects"])
        own = {**_fields(DailyIndex, data), "window": tuple(data["window"]), "notes": tuple(data["notes"])}
        own["failures"] = tuple(data.get("failures", ()))
        return DailyIndex(**{**own, "projects": projects})
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ToolError(f"{path} is not a daily index: {exc.__class__.__name__}: {exc}") from exc


def _fields(kind: type[Any], data: Any) -> dict[str, Any]:
    """The dataclass's own keys of a JSON object (``plain`` also wrote the properties); a non-object is a TypeError.

    A key the file lacks is left to the dataclass: a default fills it, a required one is the constructor's TypeError.
    """
    if not isinstance(data, Mapping):
        raise TypeError(f"expected an object, got {type(data).__name__}")
    return {field.name: data[field.name] for field in fields(kind) if field.name in data}
