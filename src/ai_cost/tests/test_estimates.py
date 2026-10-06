"""A source's estimates (ADR-0010): shown in a cell of their own, in an object of their own, never in any cost; checked
field by field; shown by a report of every project only."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..estimates import Estimates, amount, left_out_warnings, scoped, valid
from ..groups import Report
from ..models import Collected, Estimate, Provider
from ..ops import ReportRequest, build_report
from ..plugins import Context, Loaded, Plugin
from ..render import render_json, render_markdown
from ..timeutil import iso
from .fixtures import BASE, WINDOW, defaults, paths_in, write_claude_session

WHAT = "runs nobody could meter"
WHY = "counted by a budget, not a cost"
CELL = f"Estimated, not measured (acme: {WHAT}): 2 run(s), 7.5 USD — {WHY}; in no sum of this report"


def guess(ref: str, usd: str, session: str = "") -> Estimate:
    return Estimate(Provider("acme"), ref, BASE, Decimal(usd), WHAT, WHY, session)


class Guessing:
    """A source with no row: two estimates, the same one said twice."""

    name = "guessing"

    def __init__(self, found: tuple[Estimate, ...]) -> None:
        self.found = found

    def collect(self, ctx: Context) -> Collected:
        return Collected(estimates=self.found)


FOUND = (guess("r1", "5.000000000"), guess("r2", "2.5"), guess("r1", "5.000000000"))


def report(
    tmp_path: Path,
    found: tuple[Estimate, ...],
    groups: tuple[str, ...] = ("real", "api"),
    project: bool = False,
) -> Report:
    """A report of every project (``project``: of the transcript's project) with the source of ``found``."""
    paths = paths_in(tmp_path)
    if not (paths.claude_home / "projects").is_dir():
        write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    loaded = Loaded(plugins=[Plugin(name="acme", sources=(Guessing(found),))])
    scope = {"project": tmp_path / "proj"} if project else {"all_projects": True}
    request = ReportRequest(since=iso(WINDOW.start), until=iso(WINDOW.end), groups=groups, **scope)
    return replace(build_report(request, paths, config, book, loaded), generated_at="then")


def test_an_estimate_enters_no_cost_and_is_shown_once_under_the_real_cost_table(tmp_path: Path) -> None:
    plain, guessed = report(tmp_path, ()), report(tmp_path, FOUND)
    assert (guessed.real, guessed.api, guessed.models) == (plain.real, plain.api, plain.models)
    assert guessed.estimates == Estimates(FOUND[:2])
    lines = render_markdown(guessed).splitlines()
    assert lines.count(CELL) == 1
    cash = next(i for i, line in enumerate(lines) if line.startswith("Cash (API keys)"))
    assert lines[cash + 1 : cash + 3] == ["", CELL], "directly under the real cost table"


def test_without_a_real_group_the_cell_is_under_the_api_table(tmp_path: Path) -> None:
    lines = render_markdown(report(tmp_path, FOUND, groups=("api",))).splitlines()
    total = next(i for i, line in enumerate(lines) if line.startswith("## 2."))
    assert lines.count(CELL) == 1 and lines.index(CELL) > total


def test_a_report_without_an_estimate_reads_as_it_always_did(tmp_path: Path) -> None:
    plain = report(tmp_path, ())
    assert plain.estimates is None
    assert "estimates" not in json.loads(render_json(plain, detail=False))
    assert "Estimated" not in render_markdown(plain)


def test_the_json_report_holds_the_estimates_in_an_object_of_their_own(tmp_path: Path) -> None:
    plain, guessed = report(tmp_path, ()), report(tmp_path, FOUND)
    data, before = json.loads(render_json(guessed, detail=False)), json.loads(
        render_json(plain, detail=False)
    )
    said = data.pop("estimates")
    assert said["shown_usd"] == "7.500000000" and said["left_out"] == [] and said["left_out_usd"] == "0"
    assert [(item["ref"], item["usd"], item["provider"]) for item in said["shown"]] == [
        ("r1", "5.000000000", "acme"),
        ("r2", "2.5", "acme"),
    ]
    data["sources"], before["sources"] = [], []  # the source line names the estimates' source
    assert data == before, "every other field is what it was without them"


def test_a_session_keeps_the_estimates_of_its_own_runs_and_lists_the_rest_with_their_amount() -> None:
    mine, other, nobody = guess("a", "1", "sess-1"), guess("b", "2", "sess-2"), guess("c", "4")
    found = scoped((mine, other, nobody), "sess-1")
    assert found == Estimates((mine,), (other, nobody))
    assert left_out_warnings(found) == [
        "2 estimated run(s) in the span not tied to this session (1 of other sessions, 1 tied to none): 6 USD — "
        "an estimate, not a cost; in no sum of this report"
    ]
    assert scoped((mine, other), "") == Estimates((mine, other)), "a period report shows every one"
    assert scoped((), "sess-1") is None and left_out_warnings(None) == []


def test_an_amount_is_exact_text_without_trailing_zeros() -> None:
    assert [amount(Decimal(text)) for text in ("5.000000000", "2.750", "10", "0E-9")] == [
        "5",
        "2.75",
        "10",
        "0",
    ]


def test_a_source_that_returns_anything_else_as_an_estimate_costs_its_own_piece(tmp_path: Path) -> None:
    broken = report(tmp_path, ("5 USD",))  # type: ignore[arg-type]
    assert broken.estimates is None
    assert any("source guessing failed" in warning for warning in broken.warnings)


def test_a_project_report_shows_no_estimate_and_names_its_amount_as_left_out(tmp_path: Path) -> None:
    scoped_report, plain = report(tmp_path, FOUND, project=True), report(tmp_path, (), project=True)
    assert scoped_report.estimates == Estimates((), FOUND[:2])
    assert "Estimated, not measured" not in render_markdown(scoped_report)
    assert (
        f"2 estimated run(s) in the span not tied to this project (--project {tmp_path / 'proj'}): 7.5 USD — "
        "an estimate, not a cost; in no sum of this report" in scoped_report.warnings
    )
    assert (scoped_report.real, scoped_report.api) == (plain.real, plain.api)


def test_a_report_of_every_project_shows_an_estimate_once(tmp_path: Path) -> None:
    shown = report(tmp_path, FOUND)
    assert render_markdown(shown).splitlines().count(CELL) == 1
    assert not [warning for warning in shown.warnings if "estimated run(s)" in warning]


def test_an_estimate_with_a_wrong_field_costs_its_source_never_the_report(tmp_path: Path) -> None:
    good = guess("r1", "5")
    wrong: tuple[tuple[str, dict[str, Any]], ...] = (
        ("float usd", {"usd": 1.5}),
        ("negative usd", {"usd": Decimal("-1")}),
        ("NaN usd", {"usd": Decimal("NaN")}),
        ("naive time", {"at": datetime(2026, 9, 19, 15, 0)}),
        ("provider text", {"provider": "acme"}),
        ("session not text", {"session": None}),
    )
    for name, field in wrong:
        broken = report(tmp_path / name.replace(" ", "-"), (good, replace(good, **field)))
        assert broken.estimates is None, name
        assert any("source guessing failed" in warning for warning in broken.warnings), name
        assert render_markdown(broken) and render_json(broken, detail=False), "the report renders"
    assert valid(good) is good
