"""ADR-0003: rows attributed whole, identity keys before paths, mixed and unattributed reported, sums preserved."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from .. import cli
from ..attribution import MIXED, UNATTRIBUTED, attribution_group, label_for, parse_rules
from ..collectors import collect_claude
from ..collectors.claude import find_session_files
from ..config import Config, PriceBook
from ..errors import UsageError
from ..groups import Report, subscription_shares
from ..models import Scope
from ..ops import ReportRequest, build_report
from ..render import render_json, render_markdown
from ..timeutil import iso
from .fixtures import (
    WINDOW,
    defaults,
    paths_in,
    write_attribution_session,
    write_claude_session,
    write_codex,
)

THRESHOLDS = ((40, 150, 400, 1000), (15000, 50000, 120000, 300000))
RULES = ("ai-cost=pkg/cost|ws-d|^14$", "lint=lint-tree|feat/lint", "api=feat/api")


def test_rules_parse_and_reject() -> None:
    rules = parse_rules(["a=x.*y"])
    assert rules[0].label == "a" and rules[0].pattern.search("xzy")
    for bad in (["nolabel"], ["a=("], ["mixed=x"], ["a=x", "a=y"], ["=x"]):
        try:
            parse_rules(bad)
        except UsageError as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"{bad} must be a usage error")


def test_label_for_identity_keys_decide_before_paths() -> None:
    rules = parse_rules(RULES)
    assert label_for(Scope(), rules) == UNATTRIBUTED
    assert label_for(Scope(pr="14"), rules) == "ai-cost"
    assert (
        label_for(Scope(branch="feat/lint", paths=("pkg/cost/z.py",)), rules) == "lint"
    ), "a key beats paths"
    assert label_for(Scope(branch="feat/lint", workspace="ws-d"), rules) == MIXED
    assert label_for(Scope(paths=("cp ws-d/a b", "ls lint-tree/c")), rules) == MIXED
    assert (
        label_for(Scope(paths=("sed pkg/cost/x.py", "git add pkg/cost/x.py", "ls lint-tree")), rules)
        == "ai-cost"
    )


def test_claude_rows_carry_branch_and_every_streamed_entrys_segments(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_attribution_session(paths, tmp_path / "proj")
    rows = {
        r.at.minute if r.at else -1: r
        for r in collect_claude(
            find_session_files(paths.claude_home, tmp_path / "proj", "sess-attr", False, []), None
        ).rows
    }
    assert rows[1].scope == Scope(paths=("sed -i x pkg/cost/x.py", "git add pkg/cost/x.py"))
    assert rows[3].scope == Scope(), "a text-only turn has no scope"
    assert rows[4].scope.paths == ("/w/pkg/cost/y.py",), "the tool_use of a later streamed entry counts"
    assert rows[5].scope.branch == "feat/lint" and rows[6].scope.branch == "", "HEAD is no branch"
    labels = {minute: label_for(row.scope, parse_rules(RULES)) for minute, row in rows.items()}
    assert labels == {1: "ai-cost", 2: MIXED, 3: UNATTRIBUTED, 4: "ai-cost", 5: "lint", 6: "lint"}


def _report(tmp_path: Path, rules: tuple[str, ...]) -> tuple[Report, tuple[Config, PriceBook]]:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    write_attribution_session(paths, tmp_path / "proj")
    write_codex(paths)
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj",
        since=iso(WINDOW.start),
        until=iso(WINDOW.end),
        groups=("real", "api"),
        attribute=rules,
    )
    return build_report(request, paths, config, book), (config, book)


def test_attribution_sums_to_the_group_totals_and_reports_the_buckets(tmp_path: Path) -> None:
    report, _ = _report(tmp_path, RULES)
    group = report.attribution
    assert report.api is not None and report.real is not None and group is not None
    labels = [line.label for line in group.lines]
    assert labels[-2:] == [MIXED, UNATTRIBUTED] and {"ai-cost", "api"} <= set(
        labels
    ), "buckets last, labels first"
    assert abs(group.total_api_usd - report.api.total_usd) < 1e-6
    assert abs(sum(line.cash_usd for line in group.lines) - report.real.cash_usd) < 1e-6
    assert abs(sum(line.subscription_usd for line in group.lines) - report.real.subscription_usd) < 1e-6
    by_label = {line.label: line for line in group.lines}
    assert by_label["api"].keys == ("feat/api",), "a Codex rollout is labelled by its branch"
    assert (
        by_label[MIXED].calls >= 1 and by_label[UNATTRIBUTED].calls >= 1
    ), "the buckets are real, not decorative"
    assert 0 < by_label["ai-cost"].usd_context < by_label["ai-cost"].api_usd, "cache reads are shown apart"


def test_subscription_weights_sum_every_model_of_a_provider(tmp_path: Path) -> None:
    """A label with two Claude models must weigh both, not the last one the dict saw."""
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")  # fable in the session, sonnet in the subagent
    config, book = defaults(paths)
    rows = collect_claude(
        find_session_files(paths.claude_home, tmp_path / "proj", "sess-1", False, []), None
    ).rows
    assert {r.model for r in rows} >= {"claude-fable-5-1", "claude-sonnet-5"}
    tagged = [replace(r, scope=Scope(branch="fable" if "fable" in r.model else "sonnet")) for r in rows]
    group = attribution_group(tagged, parse_rules(("fable=^fable$", "sonnet=^sonnet$")), book, config, WINDOW)
    fable, sonnet = (next(line for line in group.lines if line.label == name) for name in ("fable", "sonnet"))
    anthropic = sum(s.usd for s in subscription_shares(config, book, WINDOW) if s.provider == "anthropic")
    assert fable.subscription_usd > 0 and sonnet.subscription_usd > 0, "every model of the provider weighs"
    assert abs(fable.subscription_usd + sonnet.subscription_usd - anthropic) < 1e-9
    assert abs(fable.subscription_usd * sonnet.api_usd - sonnet.subscription_usd * fable.api_usd) < 1e-9


def test_a_later_streamed_entry_with_the_usage_is_kept(tmp_path: Path) -> None:
    from ..collectors.claude import project_dir
    from .fixtures import USAGE_SMALL, _assistant

    paths = paths_in(tmp_path)
    root = project_dir(paths.claude_home, tmp_path / "p2")
    root.mkdir(parents=True)
    zero = {"input_tokens": 0, "output_tokens": 0}
    (root / "s.jsonl").write_text(
        _assistant("z1", 1, zero, tools=[{"file_path": "/w/pkg/cost/a.py"}])
        + "\n"
        + _assistant("z1", 1, USAGE_SMALL, tools=[{"file_path": "/w/pkg/cost/b.py"}])
        + "\n"
    )
    rows = collect_claude([("s", root / "s.jsonl")], None).rows
    assert len(rows) == 1 and rows[0].tokens.input == 1000
    assert rows[0].scope.paths == (
        "/w/pkg/cost/a.py",
        "/w/pkg/cost/b.py",
    ), "both entries' segments survive"


def test_subscription_share_goes_to_unattributed_when_nobody_used_the_provider(tmp_path: Path) -> None:
    report, (config, book) = _report(tmp_path, ("nothing=^zzz$",))
    group = attribution_group(report.rows, parse_rules(("nothing=^zzz$",)), book, config, report.window)
    by_label = {line.label: line for line in group.lines}
    assert by_label["nothing"].subscription_usd == 0 and by_label[UNATTRIBUTED].subscription_usd > 0
    assert by_label[UNATTRIBUTED].keys, "the keys column names what nobody labelled"


def test_rendered_section_and_json(tmp_path: Path) -> None:
    report, _ = _report(tmp_path, RULES)
    text = render_markdown(report)
    assert "## 4. Attribution" in text and "| mixed |" in text and "| unattributed |" in text
    data = json.loads(render_json(report, detail=False))
    assert data["attribution"]["lines"][0]["real_usd"] >= 0
    plain, _ = _report(tmp_path / "again", ())
    assert "## 4. Attribution" not in render_markdown(plain)


def test_cli_attribute_is_repeatable_and_a_bad_regex_exits_2(tmp_path: Path) -> None:
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        code = cli.main(
            ["report", "--project", str(tmp_path), "--attribute", "a=x", "--attribute", "b=(", "--hours", "1"]
        )
    assert code == 2 and "--attribute b" in err.getvalue()
