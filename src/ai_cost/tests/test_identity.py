"""A session report: the rows stamped with the session, from every project; the rest counted, never summed."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from ..errors import UsageError
from ..identity import inherit_origin_by_ref, resolve_session, select_session, selection_warnings
from ..models import Billing, Provider, RowKind, Tokens, UsageRow, Window
from ..ops import ReportRequest, build_report
from ..render import render_json, render_markdown
from ..timeutil import iso
from .fixtures import BASE, defaults, paths_in, write_claude_session, write_codex

SESSION = "sess-1"  # the fixture transcript's id


def _log(paths_usage: Path | None, *lines: dict[str, object]) -> None:
    assert paths_usage is not None, "the fixture paths name a usage log"
    paths_usage.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "schema": 1,
        "provider": "openai",
        "model": "gpt-5.5",
        "billing": "api",
        "tokens": {"input": 1000},
    }
    paths_usage.write_text("".join(json.dumps({**base, **line}) + "\n" for line in lines), encoding="utf-8")


def _at(minutes: int) -> str:
    return iso(BASE + timedelta(minutes=minutes))


def _report(tmp_path: Path, **request: object):  # type: ignore[no-untyped-def]
    paths = paths_in(tmp_path)
    config, book = defaults(paths)
    fields: dict[str, object] = {"session": SESSION, "groups": ("real", "api")}
    return build_report(ReportRequest(**{**fields, **request}), paths, config, book)  # type: ignore[arg-type]


def _row(ref: str, source: str, origin: str = "", kind: RowKind = RowKind.LOG, minutes: int = 0) -> UsageRow:
    return UsageRow(
        provider=Provider.OPENAI,
        model="gpt-5.5",
        kind=kind,
        at=BASE + timedelta(minutes=minutes),
        ref=ref,
        billing=Billing.API_SETTLED if kind is RowKind.SESSION else Billing.API,
        tokens=Tokens(input=10),
        source=source,
        origin_session=origin,
    )


def test_a_session_report_sums_only_the_rows_stamped_with_it(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    _log(
        paths.usage_log,
        {"at": _at(3), "ref": "mine", "origin_session": SESSION, "cost": 0.5},
        {"at": _at(4), "ref": "theirs", "origin_session": "sess-other", "cost": 7.0},
        {"at": _at(5), "ref": "nobody", "cost": 3.0},
    )
    report = _report(tmp_path)
    logged = [row for row in report.rows if row.kind is RowKind.LOG]
    assert [row.ref for row in logged] == [
        "mine"
    ], "another session's and the unstamped line are not in the sum"
    assert report.selection and report.selection.other_sessions == {"usage-log": 1}
    assert [(u.source, u.rows) for u in report.selection.unstamped] == [("usage-log", 1)]
    assert report.real and abs(report.real.total_usd - 0.5) < 1e-9, "cash of the session's own line only"


def test_claude_rows_carry_the_transcript_id_and_subagents_the_parents(tmp_path: Path) -> None:
    write_claude_session(paths_in(tmp_path), tmp_path / "proj")
    report = _report(tmp_path, groups=("api",))
    claude = [row for row in report.rows if row.kind is RowKind.TRANSCRIPT]
    assert claude and {row.origin_session for row in claude} == {SESSION}
    assert any(row.model == "claude-sonnet-5" for row in claude), "the subagent's turn is the session's"


def test_rows_of_the_span_without_a_stamp_are_listed_not_summed(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    write_codex(paths)  # rollouts nobody stamped: in the span, not provably the session's
    report = _report(tmp_path)
    assert not [row for row in report.rows if row.provider is Provider.OPENAI]
    assert report.selection and report.selection.unstamped_rows > 0
    text = render_markdown(report)
    assert f"# AI cost report — session {SESSION} — turns" in text
    assert "## Not in this session's sum" in text and "codex" in text
    assert "no subscription fee is allocated to it" in text and "prorated to the window" not in text


def test_a_period_report_is_unchanged(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    _log(paths.usage_log, {"at": _at(4), "ref": "theirs", "origin_session": "sess-other", "cost": 7.0})
    report = _report(tmp_path, session=None, since=_at(0), until=_at(30))
    assert report.selection is None and [row.ref for row in report.rows] == ["theirs"]


def test_a_stamped_line_hours_after_the_last_turn_is_counted(tmp_path: Path) -> None:
    """Collection runs a day past the last turn: a round with a longer cap, or a late charge, is in."""
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")  # the last stamped turn is at +55 min
    _log(paths.usage_log, {"at": _at(55 + 5 * 60), "ref": "late", "origin_session": SESSION, "cost": 1.0})
    report = _report(tmp_path, groups=("api",))
    assert [row.ref for row in report.rows if row.kind is RowKind.LOG] == ["late"]


def test_no_subscription_share_is_allocated_to_a_session(tmp_path: Path) -> None:
    write_claude_session(paths_in(tmp_path), tmp_path / "proj")
    report = _report(tmp_path)
    assert report.real is not None
    assert all(share.usd == 0 and share.attribution == "none" for share in report.real.subscriptions)
    data = json.loads(render_json(report, detail=False))
    assert data["selection"]["session"] == SESSION and data["real"]["total_usd"] == 0


def test_since_and_until_narrow_a_session(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    _log(
        paths.usage_log,
        {"at": _at(3), "ref": "early", "origin_session": SESSION},
        {"at": _at(40), "ref": "later", "origin_session": SESSION},
        {"at": _at(41), "ref": "theirs", "origin_session": "sess-other"},
    )
    report = _report(tmp_path, groups=("api",), since=_at(20), until=_at(50))
    assert [row.ref for row in report.rows if row.kind is RowKind.LOG] == [
        "later"
    ], "the dates, then the stamp"


def test_the_span_bounds_what_is_counted_and_a_day_bounds_what_is_read(tmp_path: Path) -> None:
    """The last turn is at +55 min: another session's row at +115 min is in the span, one at +125 min is not; a row
    of the session a day and a half later was never read."""
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    _log(
        paths.usage_log,
        {"at": _at(115), "ref": "near", "origin_session": "sess-other"},
        {"at": _at(125), "ref": "far", "origin_session": "sess-other"},
        {"at": _at(55 + 36 * 60), "ref": "much-later", "origin_session": SESSION},
    )
    report = _report(tmp_path, groups=("api",))
    assert report.selection and report.selection.other_sessions == {"usage-log": 1}
    assert not [row for row in report.rows if row.kind is RowKind.LOG]


def test_the_title_names_the_turns_and_project_is_refused(tmp_path: Path) -> None:
    write_claude_session(paths_in(tmp_path), tmp_path / "proj")
    text = render_markdown(_report(tmp_path, groups=("api",)))
    assert f"session {SESSION} — turns {_at(0)} → {_at(55)}, read {_at(-1)}" in text
    said = _usage_error(lambda: _report(tmp_path, groups=("api",), project=tmp_path / "proj"))
    assert "--project/--all-projects with --session" in said


def test_without_a_transcript_the_dates_are_required(tmp_path: Path) -> None:
    (paths_in(tmp_path).claude_home / "projects").mkdir(parents=True)
    said = _usage_error(lambda: _report(tmp_path, session="sess-gone"))
    assert "no transcript says when it ran" in said


def test_github_with_a_session_is_a_usage_error(tmp_path: Path) -> None:
    write_claude_session(paths_in(tmp_path), tmp_path / "proj")
    assert "names no session" in _usage_error(lambda: _report(tmp_path, github=("acme/www",)))


def test_a_repeated_event_keeps_its_stamped_copy(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    _log(
        paths.usage_log,
        {"at": _at(3), "ref": "plain", "event_id": "e1", "cost": 0.25},
        {"at": _at(9), "ref": "stamped", "event_id": "e1", "origin_session": SESSION, "cost": 9.0},
        {"at": _at(4), "ref": "mine", "event_id": "e2", "origin_session": SESSION},
        {"at": _at(4), "ref": "theirs", "event_id": "e2", "origin_session": "sess-other"},
    )
    report = _report(tmp_path, groups=("api",))
    logged = {row.ref: row for row in report.rows if row.kind is RowKind.LOG}
    assert sorted(logged) == ["mine", "plain"], "the first copy stays, stamped"
    assert logged["plain"].cost_reported == 0.25 and logged["plain"].at == BASE + timedelta(minutes=3)
    assert any("stamped for another session: first copy kept" in w for w in report.warnings)


def test_a_full_id_without_a_transcript_selects_by_stamp_and_says_so(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    (paths.claude_home / "projects").mkdir(parents=True)
    _log(paths.usage_log, {"at": _at(3), "ref": "mine", "origin_session": "sess-gone"})
    report = _report(tmp_path, session="sess-gone", groups=("api",), since=_at(0), until=_at(30))
    assert [row.ref for row in report.rows] == ["mine"]
    assert any("no transcript for --session sess-gone" in w for w in report.warnings)


def test_a_malformed_origin_stamps_nothing(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    _log(paths.usage_log, {"at": _at(3), "ref": "bad", "origin_session": "no spaces allowed"})
    report = _report(tmp_path, groups=("api",))
    assert report.selection and [(u.source, u.rows) for u in report.selection.unstamped] == [("usage-log", 1)]


def _usage_error(call: Callable[[], object]) -> str:
    try:
        call()
    except UsageError as exc:
        return str(exc)
    raise AssertionError("a usage error was expected")


def test_a_prefix_of_two_sessions_is_a_usage_error() -> None:
    files = [("sess-1a", Path("a.jsonl")), ("sess-1b", Path("b.jsonl"))]
    assert "names 2 sessions" in _usage_error(lambda: resolve_session("sess-1", files))
    assert resolve_session("sess-1a", files[:1]) == "sess-1a"


def test_a_subagent_transcript_is_a_usage_error(tmp_path: Path) -> None:
    sub = tmp_path / "sess-1" / "subagents" / "agent-a.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text("{}\n")
    assert "pass its parent session" in _usage_error(lambda: resolve_session(str(sub), [("agent-a", sub)]))


SESSION_ROW, LEDGER_ROW = RowKind.SESSION, RowKind.LEDGER


def test_a_ledger_row_takes_the_stamp_of_the_session_it_charges() -> None:
    rows = [
        _row("cx-1", "codex-rollouts", SESSION, SESSION_ROW),
        _row("cx-1", "ledger", kind=LEDGER_ROW),
        _row("cx-2", "ledger", kind=LEDGER_ROW),
    ]
    assert [row.origin_session for row in inherit_origin_by_ref(rows)] == [SESSION, SESSION, ""]


def test_equal_free_text_refs_lend_no_stamp() -> None:
    """Two writers may reuse a ref: only a ledger row inherits, and only from a session row."""
    rows = [_row("job-42", "writer-a", SESSION), _row("job-42", "writer-b")]
    assert inherit_origin_by_ref(rows)[1].origin_session == ""
    from_log = [_row("cx", "writer-a", SESSION), _row("cx", "ledger", kind=LEDGER_ROW)]
    assert inherit_origin_by_ref(from_log)[1].origin_session == "", "a log row is not the session it names"
    two = [
        _row("r", "a", SESSION, SESSION_ROW),
        _row("r", "b", "sess-2", SESSION_ROW),
        _row("r", "c", kind=LEDGER_ROW),
    ]
    assert (
        inherit_origin_by_ref(two)[2].origin_session == ""
    ), "a ref two sessions stamped is proof of neither"


def test_a_settled_run_without_its_charge_is_said() -> None:
    settled = _row("cx-1", "codex-rollouts", SESSION, SESSION_ROW)
    _, alone = select_session([settled], SESSION)
    assert alone.unsettled == ("cx-1",)
    assert any("ledger charge this report does not hold" in w for w in selection_warnings(alone))
    charge = _row("cx-1", "ledger", SESSION, LEDGER_ROW)
    assert select_session([settled, charge], SESSION)[1].unsettled == ()


def test_only_the_span_is_counted_around_the_session() -> None:
    span = Window(BASE, BASE + timedelta(minutes=30))
    rows = [
        _row("in", "x", "sess-2", minutes=10),
        _row("after", "x", "sess-2", minutes=600),
        _row("late", "x", SESSION, minutes=600),
    ]
    kept, selection = select_session(rows, SESSION, span)
    assert [row.ref for row in kept] == ["late"], "the session's own rows count from the whole collection"
    assert selection.other_sessions == {"x": 1}, "another session's rows only within the span"


def test_selection_counts_what_it_leaves_out() -> None:
    rows = [_row("a", "x", SESSION), _row("b", "x", "sess-2"), _row("c", "y"), _row("d", "y")]
    kept, selection = select_session(rows, SESSION)
    assert [row.ref for row in kept] == ["a"] and selection.kept == 1
    assert selection.other_sessions == {"x": 1} and selection.unstamped_rows == 2
    assert replace(selection, unstamped=()).unstamped_rows == 0


def test_a_session_report_quotes_no_vendor_work_from_the_sources(tmp_path: Path) -> None:
    """A work item names no session: the quote needs --items."""
    write_claude_session(paths_in(tmp_path), tmp_path / "proj")
    report = _report(tmp_path, groups=("api", "vendor"))
    assert report.vendor is None
    assert any("the sources' work items name no session" in w for w in report.warnings)


def test_the_header_counts_the_rows_left_after_unpriced_ones_are_skipped(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    _log(paths.usage_log, {"at": _at(3), "ref": "odd", "origin_session": SESSION, "model": "no-such-model"})
    report = _report(tmp_path, groups=("api",), unpriced="skip")
    assert report.selection and report.selection.kept == len(report.rows)
    assert any(f"the {len(report.rows)} row(s) stamped with it" in w for w in report.warnings)


def test_the_window_never_ends_past_now_and_always_after_the_last_turn(tmp_path: Path) -> None:
    from ..ops import resolve_window
    from ..timeutil import now

    last = now() - timedelta(minutes=2)
    window = resolve_window(ReportRequest(session=SESSION), Window(last - timedelta(hours=1), last), 24)
    assert last < window.end <= now() + timedelta(seconds=1)


def test_copilot_reviews_of_a_session_are_said_unpriced_even_with_a_usage_report(tmp_path: Path) -> None:
    """The usage report bills a repository's day: a session report does not read it, and says so."""
    from ..ops import _copilot_counted
    from .fixtures import defaults as fixture_defaults

    config, _ = fixture_defaults(paths_in(tmp_path))
    review = replace(_row("ws", "review-workspaces", SESSION), kind=RowKind.COPILOT, tokens=Tokens(reviews=2))
    said = _copilot_counted([review], config, one_session=True)
    assert said and "bills a repository's day, not a session" in said[0]
