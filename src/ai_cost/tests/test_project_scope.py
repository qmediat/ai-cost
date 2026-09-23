"""Per-project scope (ADR-0006): Codex and Gemini rows carry their working directory; --project keeps the project's."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from unittest import mock

from ..collectors import collect_codex, collect_gemini_cli
from ..collectors.gemini_cli import project_paths
from ..config import Paths
from ..models import Billing, Collected, Provider, RowKind, Scope, Skipped, Tokens, UsageRow
from ..ops import ReportRequest, build_report
from ..plugins import Context, Loaded, Plugin
from ..timeutil import iso
from .fixtures import BASE, WINDOW, defaults, paths_in, write_claude_session, write_grok


def _rollout(paths: Paths, session: str, cwd: str | None, minutes: int = 5) -> Path:
    """A per-turn rollout whose header names (or not) the working directory it ran in."""
    folder = paths.codex_home / "sessions" / "2026" / "09" / "19"
    folder.mkdir(parents=True, exist_ok=True)
    started = iso(BASE + timedelta(minutes=minutes))
    header = {"session_id": session, "git": {"branch": "feat/x"}, **({"cwd": cwd} if cwd is not None else {})}
    usage = {"input_tokens": 1_000, "cached_input_tokens": 0, "output_tokens": 100}
    event = {"type": "token_count", "info": {"last_token_usage": usage}, "rate_limits": {"plan_type": None}}
    lines = [
        json.dumps({"timestamp": started, "type": "session_meta", "payload": header}),
        json.dumps({"timestamp": started, "type": "turn_context", "payload": {"model": "gpt-6-astra"}}),
        json.dumps({"timestamp": started, "type": "event_msg", "payload": event}),
    ]
    path = folder / f"rollout-2026-09-19T15-05-00-{session}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_codex_rollout_carries_its_working_directory_and_branch(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    _rollout(paths, "sid-cwd", "/Users/me/Projects/app")
    _rollout(paths, "sid-none", None)
    _rollout(paths, "sid-bad", 42)  # type: ignore[arg-type]
    rows = {r.ref: r for r in collect_codex(paths.codex_home, WINDOW, "gpt-6-astra").rows}
    assert rows["sid-cwd"].scope == Scope(branch="feat/x", workspace="/Users/me/Projects/app")
    assert rows["sid-none"].scope == Scope(
        branch="feat/x"
    ), "no cwd in the header: no workspace, the usage counts"
    assert rows["sid-bad"].scope == Scope(branch="feat/x"), "a cwd that is not text costs the workspace only"


def _gemini_session(gemini_home: Path, folder: str, message_id: str) -> None:
    chats = gemini_home / "tmp" / folder / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    record = {
        "id": message_id,
        "timestamp": iso(BASE + timedelta(minutes=5)),
        "type": "gemini",
        "model": "gemini-3.8-flash",
        "tokens": {"input": 10, "output": 1, "cached": 0, "thoughts": 0},
    }
    (chats / f"session-2026-09-19T15-05-{message_id}.jsonl").write_text(
        json.dumps({"sessionId": f"s-{message_id}"}) + "\n" + json.dumps(record) + "\n"
    )


def test_gemini_rows_take_the_working_directory_projects_json_maps_to_their_folder(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    hashed = hashlib.sha256(b"/Users/me/old").hexdigest()
    paths.gemini_home.mkdir(parents=True)
    (paths.gemini_home / "projects.json").write_text(
        json.dumps({"projects": {"/Users/me/app": "app", "/Users/me/old": "old", 7: "n", "/x": None}})
    )
    _gemini_session(paths.gemini_home, "app", "a1")
    _gemini_session(paths.gemini_home, hashed, "h1")
    _gemini_session(paths.gemini_home, "stray", "s1")
    collected = collect_gemini_cli(paths.gemini_home, WINDOW)
    by_ref = {r.ref: r.scope.workspace for r in collected.rows}
    assert by_ref == {
        "app/s-a1": "/Users/me/app",
        f"{hashed}/s-h1": "/Users/me/old",
        "stray/s-s1": "stray",
    }, "a named folder and a hashed one map to their directory; an unmapped one keeps its name"
    assert not collected.skipped, "entries that are not text are ignored, never a skip of the whole map"


def test_a_malformed_projects_json_is_a_counted_skip_and_the_folder_names_stay(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.gemini_home.mkdir(parents=True)
    _gemini_session(paths.gemini_home, "app", "a1")
    skipped: list[Skipped] = []
    assert (
        project_paths(paths.gemini_home, skipped) == {} and not skipped
    ), "absent: older CLI, nothing to say"
    (paths.gemini_home / "projects.json").write_text("{not json")
    assert project_paths(paths.gemini_home, skipped) == {} and skipped[-1].reason.startswith("unusable")
    (paths.gemini_home / "projects.json").write_text(json.dumps({"projects": [1]}))
    assert project_paths(paths.gemini_home, skipped) == {} and skipped[-1].reason == "no projects object"
    rows = collect_gemini_cli(paths.gemini_home, WINDOW).rows
    assert [r.scope.workspace for r in rows] == ["app"]


def _row(source: str, ref: str, scope: Scope, kind: RowKind = RowKind.LOG) -> UsageRow:
    return UsageRow(
        provider=Provider.DEEPSEEK,
        model="deepseek-flash",
        kind=kind,
        source=source,
        at=BASE + timedelta(minutes=5),
        ref=ref,
        billing=Billing.API,
        tokens=Tokens(input=10, output=1),
        scope=scope,
    )


class _Named:
    """A source whose one row names a workspace that is not a directory (a review workspace, say)."""

    name = "named"

    def collect(self, ctx: Context) -> Collected:
        return Collected(
            rows=[_row(self.name, "ws-77/r1", Scope(workspace="ws-77", pr="77"), RowKind.REVIEW)]
        )


class _Tilde:
    """A source that names its directory with a leading ``~`` (Copilot, PR #90 r1): still a directory."""

    name = "tilde"

    def collect(self, ctx: Context) -> Collected:
        return Collected(rows=[_row(self.name, "tilde-1", Scope(workspace="~/proj"))])


class _ByRef:
    """A source whose rows name no scope but share the ref of a rollout (a ledger line, an app's log line)."""

    name = "byref"

    def collect(self, ctx: Context) -> Collected:
        return Collected(rows=[_row(self.name, "sid-in", Scope()), _row(self.name, "sid-out", Scope())])


class _Probe:
    """An enricher that records the workspaces it was given (the filter must have run before it)."""

    name = "probe"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def enrich(self, rows: list[UsageRow], ctx: Context) -> list[UsageRow]:
        self.seen = [row.scope.workspace for row in rows]
        return rows


def _report(
    paths: Paths, project: Path | None, probe: _Probe | None = None
) -> tuple[list[UsageRow], list[str]]:
    config, book = defaults(paths)
    request = ReportRequest(
        project=project,
        all_projects=project is None,
        since=iso(WINDOW.start),
        until=iso(WINDOW.end),
        groups=("api",),
    )
    sources = (_Named(), _ByRef(), _Tilde())
    loaded = (
        Loaded(plugins=[Plugin(name="probe", sources=sources, enrichers=(probe,))]) if probe else Loaded()
    )
    report = build_report(request, paths, config, book, loaded)
    return list(report.rows), list(report.warnings)


def _populated(tmp_path: Path) -> tuple[Paths, Path]:
    """A project with transcripts, rollouts inside / below / through a symlink / outside / without a directory, a Grok session elsewhere."""
    paths = paths_in(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    link = tmp_path / "link"
    os.symlink(project, link)
    write_claude_session(paths, project)
    _rollout(paths, "sid-in", str(project))
    _rollout(paths, "sid-sub", str(project / "packages" / "a"), minutes=6)
    _rollout(paths, "sid-link", str(link), minutes=7)
    _rollout(paths, "sid-out", str(tmp_path / "other"), minutes=8)
    _rollout(paths, "sid-none", None, minutes=9)
    write_grok(paths, cwd=str(tmp_path / "other"))
    return paths, project


def test_project_keeps_the_projects_rows_drops_other_scopes_and_says_what_it_cannot_place(
    tmp_path: Path,
) -> None:
    paths, project = _populated(tmp_path)
    probe = _Probe()
    with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):  # ~/proj is the project
        rows, warnings = _report(paths, project, probe)
    assert any(r.source == "tilde" for r in rows), "a ~ directory under the project is the project's"
    refs = sorted(r.ref for r in rows if r.provider.value == "openai")
    assert refs == ["sid-in", "sid-link", "sid-none", "sid-sub"], "inside, below, through a symlink, unplaced"
    assert not any(r.source == "grok-build" for r in rows), "the Grok session of the other directory is out"
    assert any(r.provider.value == "anthropic" for r in rows), "the project's own transcripts stay"
    assert not any(
        r.source == "named" for r in rows
    ), "a named workspace that is no directory is another scope"
    by_ref = {r.ref: r.scope.workspace for r in rows if r.source == "byref"}
    assert by_ref == {"sid-in": str(project)}, "a scopeless row takes the directory of the rollout it names"
    assert any(
        "3 row(s) of other working directories left out (byref ×1, codex-rollouts ×1, grok-build ×1)" in w
        for w in warnings
    )
    assert any(
        "1 row(s) of named workspaces that are not directories left out" in w and "(named ×1)" in w
        for w in warnings
    )
    assert any("1 row(s) naming no working directory are included (codex-rollouts ×1)" in w for w in warnings)
    assert sorted(probe.seen) == sorted(
        r.scope.workspace for r in rows
    ), "enrichers see the filtered rows only"


def test_all_projects_keeps_every_row_and_says_nothing_about_scopes(tmp_path: Path) -> None:
    paths, _ = _populated(tmp_path)
    rows, warnings = _report(paths, None)
    assert sorted(r.ref for r in rows if r.provider.value == "openai") == [
        "sid-in",
        "sid-link",
        "sid-none",
        "sid-out",
        "sid-sub",
    ], "--all-projects: every rollout"
    assert any(r.source == "grok-build" for r in rows) and not any("left out" in w for w in warnings)


def test_a_report_run_from_inside_a_project_scopes_the_other_sources_to_it(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    write_claude_session(paths, project)
    _rollout(paths, "sid-in", str(project))
    _rollout(paths, "sid-out", str(tmp_path / "elsewhere"), minutes=8)
    config, book = defaults(paths)
    cwd = os.getcwd()
    os.chdir(project)
    try:
        report = build_report(
            ReportRequest(since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)),
            paths,
            config,
            book,
        )
    finally:
        os.chdir(cwd)
    assert [r.ref for r in report.rows if r.provider.value == "openai"] == ["sid-in"]
    assert any("left out (codex-rollouts ×1)" in w for w in report.warnings)
