"""The origin keys of a usage-log line: who launched the work — one validator for ``ai-cost log``, ``record()`` and the reader."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
from datetime import timedelta
from pathlib import Path
from typing import Any

from .. import cli
from ..collectors.usage_log import collect_usage_log
from ..log import record
from ..origin import Origin, origin_of
from ..timeutil import iso
from .fixtures import BASE, WINDOW
from .test_usage_log import _isolated, _line, _write

SESSION = "11111111-2222-4333-8444-555555555555"
STAMP: dict[str, Any] = {
    "origin_session": SESSION,
    "origin_repo": "acme/widget",
    "origin_pr": 7,
    "run_id": "r1-reviewer-1",
}


def _refused(keys: dict[str, Any]) -> str:
    try:
        origin_of(keys)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"{keys!r} must be refused")


def test_record_writes_the_origin_keys_and_the_reader_counts_the_line_without_a_warning(
    tmp_path: Path,
) -> None:
    log = tmp_path / "usage.jsonl"
    line = record("openai", "gpt-5.5", {"input": 10, "output": 2}, path=log, pr="PR-7 draft", **STAMP)
    assert {key: line[key] for key in STAMP} == STAMP and line["pr"] == "PR-7 draft"
    written = json.loads(log.read_text())
    assert origin_of(written) == Origin(SESSION, "acme/widget", 7, "r1-reviewer-1")
    collected, warnings = collect_usage_log([log], None)
    assert len(collected.rows) == 1 and collected.skipped == [] and warnings == []
    assert collected.rows[0].scope.pr == "PR-7 draft", "the schema-1 pr stays free text scope"


def test_a_line_without_origin_keys_writes_none_of_them(tmp_path: Path) -> None:
    line = record("openai", "gpt-5.5", {"input": 1}, path=tmp_path / "u.jsonl", ref="job-1")
    assert not set(line) & {"origin_session", "origin_repo", "origin_pr", "run_id"}


def test_a_pull_request_number_must_be_a_positive_integer_beside_a_repository() -> None:
    for value in (True, 0, -3, "7", 7.0, 2**53 + 1):
        assert "origin_pr must be a positive integer" in _refused(
            {"origin_repo": "acme/widget", "origin_pr": value}
        )
    assert "origin_pr needs origin_repo" in _refused({"origin_pr": 7})


def test_ids_and_the_repository_have_one_shape() -> None:
    assert "origin_repo must be owner/name" in _refused({"origin_repo": "widget"})
    assert "origin_repo must be owner/name" in _refused({"origin_repo": "acme/widget/extra"})
    for path_shaped in ("../..", "acme/..", "acme/.", "-x/widget"):
        assert "origin_repo must be owner/name" in _refused({"origin_repo": path_shaped}), path_shaped
    assert (
        origin_of({"origin_repo": "acme/.github"}).repo == "acme/.github"
    ), "a dot-led name is a real repository"
    assert "origin_session must be letters" in _refused({"origin_session": "two words"})
    assert "run_id must be letters" in _refused({"run_id": "x" * 201})
    assert "run_id must be a string" in _refused({"run_id": 5})


def test_the_writer_refuses_a_malformed_origin_and_writes_nothing(tmp_path: Path) -> None:
    log = tmp_path / "u.jsonl"
    try:
        record("openai", "gpt-5.5", {"input": 1}, path=log, origin_pr=7)
    except ValueError as exc:
        assert "origin_pr needs origin_repo" in str(exc), str(exc)
    else:
        raise AssertionError("a PR number without its repository must be refused by the writer")
    assert not log.exists()


def test_a_malformed_origin_keeps_the_lines_usage_and_is_one_counted_warning(tmp_path: Path) -> None:
    log = _write(
        tmp_path / "usage.jsonl",
        [
            _line(tokens={"input": 100}, origin_pr=7),
            _line(tokens={"input": 50}, origin_repo="acme/widget", origin_pr="8"),
            _line(tokens={"input": 25}, **STAMP),
        ],
    )
    collected, warnings = collect_usage_log([log], WINDOW)
    assert sum(row.tokens.input for row in collected.rows) == 175 and collected.skipped == []
    assert len(warnings) == 1 and "2 line(s) with a malformed origin" in warnings[0], warnings
    assert "line 1: origin_pr needs origin_repo" in warnings[0], warnings


def test_a_wrappers_lines_read_without_a_warning_before_and_after_the_repository_rule(
    tmp_path: Path,
) -> None:
    run = "reviewer:raw:20260929T100000Z:4242"  # a wrapper's run id: tool, command, UTC time, pid
    written = {
        "billing": "api",
        "source": "reviewer",
        "ref": run,
        "run_id": run,
        "event_id": "resp-1",
        "pr": "145",
        "branch": "qmt/x",
        "tokens": {"cache_hit": 3, "cache_miss": 7, "output": 5},
    }
    now = _line(provider="deepseek", model="deepseek-flash", **{**written, **STAMP, "run_id": run})
    before = _line(
        provider="deepseek", model="deepseek-flash", **{**written, "event_id": "resp-2"}, origin_pr=145
    )
    collected, warnings = collect_usage_log([_write(tmp_path / "now.jsonl", [now])], WINDOW)
    assert len(collected.rows) == 1 and collected.skipped == [] and warnings == []
    collected, warnings = collect_usage_log([_write(tmp_path / "before.jsonl", [before])], WINDOW)
    assert (
        len(collected.rows) == 1 and collected.skipped == []
    ), "a line an older wrapper wrote (a PR number, no repository) keeps its usage"
    assert len(warnings) == 1 and "origin_pr needs origin_repo" in warnings[0], warnings


def test_a_malformed_origin_outside_the_window_is_not_this_windows_warning(tmp_path: Path) -> None:
    later = iso(BASE + timedelta(days=3))
    log = _write(tmp_path / "usage.jsonl", [_line(tokens={"input": 1}, at=later, origin_pr=7)])
    collected, warnings = collect_usage_log([log], WINDOW)
    assert collected.rows == [] and warnings == []


def _refused_by_cli(argv: list[str]) -> str:
    err = io.StringIO()
    with redirect_stderr(err):
        assert cli.main(argv) == 2, "a malformed origin is a usage error"
    return err.getvalue()


def test_cli_log_takes_the_origin_flags_and_refuses_a_malformed_one(tmp_path: Path) -> None:
    with _isolated(tmp_path):
        log = tmp_path / "usage.jsonl"
        base = ["log", "--provider", "openai", "--model", "gpt-5.5", "--input", "3", "--log", str(log)]
        stamp = [
            "--origin-session",
            SESSION,
            "--origin-repo",
            "acme/widget",
            "--origin-pr",
            "7",
            "--run-id",
            "r1",
        ]
        assert cli.main([*base, *stamp]) == 0
        line = json.loads(log.read_text())
        assert (line["origin_session"], line["origin_repo"], line["origin_pr"], line["run_id"]) == (
            SESSION,
            "acme/widget",
            7,
            "r1",
        )
        assert "origin_pr needs origin_repo" in _refused_by_cli([*base, "--origin-pr", "7"])
        assert "origin_repo must be owner/name" in _refused_by_cli([*base, "--origin-repo", "widget"])
        assert len(log.read_text().splitlines()) == 1, "a refused line is never written"
