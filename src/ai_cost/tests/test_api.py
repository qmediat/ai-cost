"""The library interface: its declared surface, instance isolation, record / price / report, segments, labels."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from functools import partial
from importlib import resources
from pathlib import Path
from typing import Any
from unittest import mock

from .. import api
from ..log import MAX_LINE_BYTES, record
from .engine_fixtures import BOUNDARY, WHOLE, doctor_on, engine, leaky_home, log_line, surface, write_segment
from .engine_fixtures import raises_api as _raises
from .fixtures import BASE

AT = BASE + timedelta(minutes=10)
LEADS_OUT = "a link that leads outside the instance"  # the words a report and the doctor say


def _refs(report: api.Report) -> list[str]:
    return sorted(row.ref for row in report.rows)


def test_the_interface_matches_its_declared_surface() -> None:
    declared = json.loads(resources.files(__package__).joinpath("data/api-surface.json").read_text("utf-8"))
    found = surface()
    assert (
        found == declared
    ), "ai_cost.api changed — update tests/data/api-surface.json on purpose:\n" + json.dumps(
        found, indent=2, sort_keys=True
    )


def test_an_instance_report_reads_only_the_instance(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2026-09", [log_line(AT, ref="app-call")])
    (root / "config.json").write_text(json.dumps({"plugins": ["leak_config"]}), encoding="utf-8")
    with leaky_home(tmp_path, "leak_config"):
        report = engine(root).report(WHOLE)
    assert _refs(report) == ["app-call"], "no transcript, no default log, no plugin row"
    assert not any("plugin" in source for source in report.sources), report.sources


def test_read_clis_adds_the_machine_users_cli_rows_but_still_no_plugin(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2026-09", [log_line(AT, ref="app-call")])
    with leaky_home(tmp_path, "leak_clis"):
        refs = _refs(engine(root, read_clis=True).report(WHOLE))
    assert "app-call" in refs and any(ref.startswith("sess-1") for ref in refs), refs
    assert "plugin-row" not in refs and "machine-log" not in refs, refs


def test_a_plugin_the_instance_names_runs(tmp_path: Path) -> None:
    root = tmp_path / "app"
    with leaky_home(tmp_path, "leak_named"):
        refs = _refs(engine(root, plugins=("leak_named",)).report(WHOLE))
    assert refs == ["plugin-row"], refs


def test_two_instances_do_not_see_each_other(tmp_path: Path) -> None:
    first, second = engine(tmp_path / "one"), engine(tmp_path / "two")
    first.record(api.Call("openai", "gpt-6-sol", {"input": 10}, at=AT, keys={"ref": "one"}))
    second.record(api.Call("openai", "gpt-6-sol", {"input": 10}, at=AT, keys={"ref": "two"}))
    assert _refs(first.report(WHOLE)) == ["one"] and _refs(second.report(WHOLE)) == ["two"]


def test_an_instance_reads_no_account_source(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    bill = {
        "github": {"bill": {"scope": "organization", "name": "acme"}},
        "deepseek": {"report": {"source": "import"}},
    }
    (root / "config.json").write_text(json.dumps({"providers": bill}), encoding="utf-8")
    sources = engine(root).report(WHOLE).sources
    assert "github usage report: not read — an instance reads no account source" in sources, sources
    assert "provider reports: not read — an instance reads no account source" in sources, sources


def _round_trip(tmp_path: Path, provider: str, model: str, tokens: dict[str, int]) -> None:
    """record → price → report: the call's list price is the API group's figure for its one row."""
    app = engine(tmp_path / provider)
    call = api.Call(provider, model, tokens, at=AT)
    line = app.record(call)
    assert line["tokens"] == tokens and line["provider"] == provider
    quote = app.price(call)
    report = app.report(WHOLE)
    assert quote.usd is not None and quote.usd > 0 and report.api is not None
    assert abs(report.api.total_usd - quote.usd) < 1e-12, (report.api.total_usd, quote)


def test_an_anthropic_call_round_trips(tmp_path: Path) -> None:
    tokens = {"input": 1000, "output": 200, "cache_read": 5000, "cache_write_5m": 300}
    _round_trip(tmp_path, "anthropic", "claude-fable-5-2", tokens)


def test_an_openai_call_round_trips(tmp_path: Path) -> None:
    _round_trip(tmp_path, "openai", "gpt-6-sol", {"input": 1000, "cached_input": 400, "output": 200})


def test_a_google_call_round_trips(tmp_path: Path) -> None:
    _round_trip(tmp_path, "google", "gemini-3.8-flash", {"prompt": 1000, "cached": 400, "output": 200})


def test_a_deepseek_call_round_trips(tmp_path: Path) -> None:
    _round_trip(
        tmp_path, "deepseek", "deepseek-v4-pro", {"cache_hit": 1000, "cache_miss": 400, "output": 200}
    )


def test_a_refused_call_writes_nothing(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    _raises(lambda: app.record(api.Call("openai", "gpt-6-sol", {"cache_read": 5}, at=AT)), "invalid call")
    _raises(lambda: app.record(api.Call("nobody!", "m", {"input": 5}, at=AT)), "invalid call")
    _raises(lambda: app.record(api.Call("openai", "gpt-6-sol", {"input": 5}, at=AT, keys={"who": 1})), "who")
    assert not (tmp_path / "app" / "usage").exists()


def test_a_model_without_a_price_needs_the_calls_cost(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    unknown = api.Call("openai", "gpt-nowhere", {"input": 5}, at=AT)
    _raises(lambda: app.record(unknown), "no price", "give the call's cost")
    assert app.price(unknown) == api.Price(None, "no list price for openai/gpt-nowhere and no cost given")
    charged = api.Call("openai", "gpt-nowhere", {"input": 5}, cost=0.25, at=AT)
    assert app.record(charged)["cost"] == 0.25
    assert app.price(charged) == api.Price(0.25, "reported by the caller")


def test_a_call_a_provider_bills_per_unit_needs_its_cost_as_record_does(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    counted = api.Call("github", "actions", {"input": 1}, at=AT)
    _raises(lambda: app.price(counted), "invalid call", "github prices no token counter (give a cost)")
    _raises(lambda: app.record(counted), "no price", "give the call's cost")
    _raises(lambda: app.price(api.Call("github", "actions", {"bogus": 1}, at=AT)), "invalid call", "bogus")
    _raises(lambda: app.price(api.Call("github", "actions", at=AT)), "invalid call", "positive counter")


def test_a_calls_own_cost_is_its_price_for_every_provider(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    for provider, model in (("github", "actions"), ("openai", "gpt-6-sol"), ("openai", "gpt-nowhere")):
        assert app.price(api.Call(provider, model, cost=0.5, at=AT)) == api.Price(
            0.5, "reported by the caller"
        )


def test_segments_are_written_by_the_calls_utc_month(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    late = datetime(2026, 11, 1, 1, 30, tzinfo=timezone(timedelta(hours=2)))  # 2026-10-31 23:30 UTC
    app.record(api.Call("openai", "gpt-6-sol", {"input": 10}, at=late))
    app.record(api.Call("openai", "gpt-6-sol", {"input": 10}, at=BOUNDARY + timedelta(minutes=30)))
    names = sorted(path.name for path in (tmp_path / "app" / "usage").iterdir())
    assert names == ["2026-10.jsonl", "2026-11.jsonl"], names
    stored = json.loads((tmp_path / "app" / "usage" / "2026-10.jsonl").read_text("utf-8"))
    assert stored["at"] == "2026-10-31T23:30:00Z", "an aware call of another zone is stored in UTC"


def test_a_report_reads_only_the_segments_its_period_touches(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2026-10", ["not json: read only when October is asked for"])
    write_segment(root, "2026-11", [log_line(BOUNDARY + timedelta(hours=1), ref="november")])
    november = api.Period(since=BOUNDARY, until=BOUNDARY + timedelta(days=2))
    report = engine(root).report(november)
    assert _refs(report) == ["november"] and list(report.skipped) == [], report.skipped
    both = engine(root).report(api.Period(since=BOUNDARY - timedelta(days=1), until=november.until))
    assert [skip.path for skip in both.skipped] == [str(root / "usage" / "2026-10.jsonl")]


def test_an_event_id_repeated_across_segments_is_read_once(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2026-10", [log_line(BOUNDARY - timedelta(hours=1), event_id="e-1", ref="first")])
    write_segment(root, "2026-11", [log_line(BOUNDARY + timedelta(hours=1), event_id="e-1", ref="again")])
    period = api.Period(since=BOUNDARY - timedelta(days=1), until=BOUNDARY + timedelta(days=1))
    assert _refs(engine(root).report(period)) == ["first"]


def test_labels_filter_by_exact_equality_and_group_by_one_label(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    for tenant, feature in (("acme", "chat"), ("acme", "search"), ("Acme", "chat")):
        app.record(
            api.Call("openai", "gpt-6-sol", {"input": 1000}, at=AT, labels={"tenant": tenant, "f": feature})
        )
    app.record(api.Call("openai", "gpt-6-sol", {"input": 1000}, at=AT))
    assert len(app.report(WHOLE, where={"tenant": "acme"}).rows) == 2
    group = app.report(WHOLE, where={"tenant": "acme"}, by="f").attribution
    assert group is not None and [(line.label, line.calls) for line in group.lines] == [
        ("chat", 1),
        ("search", 1),
        ("(no f)", 0),
    ]
    grouped = app.report(WHOLE, by="tenant").attribution
    assert grouped is not None and [line.label for line in grouped.lines] == ["Acme", "acme", "(no tenant)"]


def test_a_malformed_label_is_refused(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    _raises(
        lambda: app.record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=AT, labels={"a b": "x"})), "a b"
    )
    _raises(
        lambda: app.record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=AT, labels={"t": ""})), "label"
    )
    _raises(lambda: app.report(WHOLE, by="no spaces please"), "label name")
    _raises(lambda: app.report(WHOLE, by="t", attribute=["x=y"]), "give one")


def test_a_line_over_the_atomic_write_limit_is_refused(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    probe = _encoded(_with_ref(app, "x"))  # a one-character ref: every other byte of the line is fixed
    fits = "x" * (MAX_LINE_BYTES - probe)  # one byte stays free for the newline that may precede the line
    assert _encoded(_with_ref(app, fits)) == MAX_LINE_BYTES - 1
    _raises(
        lambda: _with_ref(app, fits + "x"),
        "line too long",
        f"{MAX_LINE_BYTES} bytes",
        f"more than {MAX_LINE_BYTES}",
    )
    lines = (tmp_path / "app" / "usage" / "2026-09.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 2, "the probe and the line that fits; the longer one was never written"


def _with_ref(app: api.Engine, ref: str) -> object:
    return app.record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=AT, keys={"ref": ref}))


def _encoded(line: object) -> int:
    return len((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))


def test_naive_datetimes_are_refused(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    naive = datetime(2026, 9, 19, 15, 0)
    _raises(lambda: app.record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=naive)), "timezone-aware")
    _raises(lambda: app.price(api.Call("openai", "gpt-6-sol", {"input": 1}, at=naive)), "timezone-aware")
    _raises(lambda: api.Period(since=naive, until=naive + timedelta(hours=1)), "timezone-aware")
    assert not (tmp_path / "app" / "usage").exists()


def test_an_aware_time_is_recorded_in_utc_at_second_precision(tmp_path: Path) -> None:
    local = datetime(2026, 11, 16, 10, 30, 15, 999_000, tzinfo=timezone(timedelta(hours=8)))
    line = engine(tmp_path / "app").record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=local))
    assert line["at"] == "2026-11-16T02:30:15Z"


def test_a_deepseek_call_is_priced_by_its_utc_hour(tmp_path: Path) -> None:
    peak_in_utc = datetime(2026, 11, 16, 10, 30, tzinfo=timezone(timedelta(hours=8)))  # Monday 02:30 UTC
    quote = engine(tmp_path / "app").price(
        api.Call("deepseek", "deepseek-v4-pro", {"output": 1000}, at=peak_in_utc)
    )
    assert quote.note == "list price, peak", quote


def test_a_period_is_one_of_two_forms(tmp_path: Path) -> None:
    _raises(api.Period, "exactly one")
    _raises(lambda: api.Period(since=BASE, until=BASE + timedelta(hours=1), hours=1.0), "exactly one")
    _raises(lambda: api.Period(since=BASE), "both since and until")
    _raises(lambda: engine(tmp_path / "app").report(api.Period(hours=-1.0)), "--hours")


def test_a_missing_instance_reports_nothing_with_one_warning(tmp_path: Path) -> None:
    report = engine(tmp_path / "nowhere").report(WHOLE)
    assert report.rows == [] and list(report.warnings) == [
        f"instance {tmp_path / 'nowhere'} does not exist yet: nothing is recorded (ai-cost instance init)"
    ]


def test_usage_logs_outside_the_instance_need_its_permission(tmp_path: Path) -> None:
    root, elsewhere = tmp_path / "app", tmp_path / "elsewhere.jsonl"
    elsewhere.write_text(log_line(AT, ref="outside") + "\n", encoding="utf-8")
    write_segment(root, "2026-09", [])
    (root / "inside.jsonl").write_text(log_line(AT, ref="inside") + "\n", encoding="utf-8")
    (root / "config.json").write_text(json.dumps({"usage_logs": ["inside.jsonl", str(elsewhere)]}), "utf-8")
    _raises(lambda: engine(root).report(WHOLE), "outside the instance", "outside_logs")
    assert _refs(engine(root, outside_logs=(elsewhere,)).report(WHOLE)) == ["inside", "outside"]


def test_doctor_names_a_plugin_the_instance_cannot_load(tmp_path: Path) -> None:
    app = engine(tmp_path / "app", plugins=("ai_cost_no_such_plugin",))
    app.record(api.Call("openai", "gpt-6-sol", {"input": 1}))
    with doctor_on():
        problems, none = app.doctor(), engine(tmp_path / "app").doctor()
    assert len(problems) == 1 and "ai_cost_no_such_plugin: cannot import" in problems[0], problems
    assert none == []


def test_a_config_that_does_not_parse_is_an_api_error(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.json").write_text("[1]", encoding="utf-8")
    _raises(lambda: engine(tmp_path / "app"), "expected a JSON object")


def test_the_machine_users_prices_never_reach_an_instance(tmp_path: Path) -> None:
    call = api.Call("openai", "gpt-6-sol", {"input": 1_000_000}, at=AT)
    clean = engine(tmp_path / "app").price(call)
    with leaky_home(tmp_path, "leak_prices"):
        assert engine(tmp_path / "app").price(call) == clean == api.Price(2.0, "list price, standard tier")


def test_a_long_line_is_refused_by_an_instance_only(tmp_path: Path) -> None:
    long_ref = "x" * 5000
    line = record("openai", "gpt-6-sol", {"input": 1}, path=tmp_path / "user.jsonl", ref=long_ref)
    assert len((json.dumps(line) + "\n").encode("utf-8")) == 5123
    assert (tmp_path / "user.jsonl").read_text("utf-8") == json.dumps(line) + "\n", "the user's log keeps it"
    call = api.Call("openai", "gpt-6-sol", {"input": 1}, at=AT, keys={"ref": long_ref})
    _raises(lambda: engine(tmp_path / "app").record(call), "5123 bytes", "more than 4096")


def test_writes_open_the_file_for_appending(tmp_path: Path) -> None:
    opened = []
    real_open = os.open

    def spy(path: object, flags: int, *rest: object) -> int:
        opened.append(flags)
        return real_open(path, flags, *rest)  # type: ignore[arg-type]

    with mock.patch("os.open", side_effect=spy):
        engine(tmp_path / "app").record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=AT))
    assert opened and all(
        flags & os.O_APPEND and flags & (os.O_WRONLY | os.O_RDWR) for flags in opened
    ), opened


def test_a_usage_logs_entry_naming_an_own_segment_is_read_once(tmp_path: Path) -> None:
    root = tmp_path / "app"
    engine(root).record(api.Call("openai", "gpt-6-sol", {"input": 10}, at=AT))
    (root / "config.json").write_text(json.dumps({"usage_logs": ["usage/2026-09.jsonl"]}), "utf-8")
    assert len(engine(root).report(WHOLE).rows) == 1
    for entry_name in ("", "."):
        (root / "config.json").write_text(json.dumps({"usage_logs": [entry_name]}), "utf-8")
        _raises(lambda: engine(root).report(WHOLE), "usage_logs entry", "names no log file")


def test_a_filtered_report_carries_no_plan_share_and_names_none_of_the_other_rows(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    plans = [{"plan": "chatgpt-plus", "seats": 1, "covers": ["openai"]}]
    (root / "config.json").write_text(json.dumps({"subscriptions": plans}), "utf-8")
    app = engine(root)
    for tenant in ("a", "b", "b"):
        app.record(api.Call("openai", "gpt-6-sol", {"input": 1000}, at=AT, labels={"tenant": tenant}))
    whole, one = app.report(WHOLE), app.report(WHOLE, where={"tenant": "a"})
    assert whole.real is not None and whole.real.subscription_usd > 0
    assert one.real is not None and one.real.subscription_usd == 0 and one.real.total_usd == one.real.cash_usd
    assert app.report(WHOLE, where={"tenant": "zzz"}).real.total_usd == 0  # type: ignore[union-attr]
    assert one.sources == [
        "coding-CLI files: not read — an instance reads them only with read_clis",
        "usage-log ×1",
        "labels tenant=a: 1 row(s) — the real group is their cash only, no plan share; a filter selects usage "
        "rows: work items and estimates are left out",
    ], one.sources


def test_a_segment_named_no_month_and_a_line_of_another_month_are_said(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2027-13", [log_line(AT, ref="never")])
    write_segment(root, "2026-09", [log_line(AT, ref="here"), log_line(BOUNDARY, ref="november")])
    report = engine(root).report(WHOLE)
    assert _refs(report) == ["here"]
    assert [skip.reason for skip in report.skipped] == [
        "line 2: at 2026-11-01T00:00:00Z is outside the segment's month 2026-09"
    ]
    assert f"{root / 'usage'}: not a monthly segment, not read: 2027-13.jsonl" in report.warnings
    assert f"{root / 'usage'}/2027-13.jsonl: not a monthly segment, not read" in engine(root).doctor()


def test_a_segment_that_links_out_of_the_instance_is_not_read(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2026-08", [])
    outside = tmp_path / "outside.jsonl"
    outside.write_text(log_line(AT, ref="outside") + "\n", encoding="utf-8")
    (root / "usage" / "2026-09.jsonl").symlink_to(outside)
    report = engine(root).report(WHOLE)
    assert report.rows == [] and [(s.path, s.reason) for s in report.skipped] == [
        (str(root / "usage" / "2026-09.jsonl"), f"{LEADS_OUT}: not read")
    ]
    assert (
        f"{root / 'usage'}/2026-09.jsonl: {LEADS_OUT}, not read" in engine(root).doctor()
    ), "doctor and reader agree"


def test_a_period_across_a_year_reads_both_years_segments(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    new_year = datetime(2027, 1, 1, tzinfo=timezone.utc)
    for at in (new_year - timedelta(minutes=5), new_year + timedelta(minutes=5)):
        app.record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=at))
    period = api.Period(since=new_year - timedelta(hours=1), until=new_year + timedelta(hours=1))
    assert len(app.report(period).rows) == 2
    assert sorted(p.name for p in (tmp_path / "app" / "usage").iterdir()) == [
        "2026-12.jsonl",
        "2027-01.jsonl",
    ]


def test_a_root_given_as_text_relative_through_dots_or_a_link_is_the_same_instance(tmp_path: Path) -> None:
    real = tmp_path / "app"
    engine(real).record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=AT))
    (tmp_path / "link").symlink_to(real)
    with mock.patch.object(os, "getcwd", return_value=str(tmp_path)):
        relative = api.Instance("app")  # type: ignore[arg-type]
    assert relative.root == real and relative.root.is_absolute()
    for root in (str(real), real.parent / "x" / ".." / "app", tmp_path / "link"):
        assert len(api.Engine(api.Instance(root)).report(WHOLE).rows) == 1, root  # type: ignore[arg-type]


def test_wrong_types_at_the_interface_are_api_errors_naming_the_field(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    (tmp_path / "file").write_text("", encoding="utf-8")
    make_call: Any = api.Call  # the wrong types an app may hand over at run time
    cases = (
        (lambda: api.Instance(5), "Instance.root"),  # type: ignore[arg-type]
        (lambda: api.Instance(tmp_path, plugins="mod"), "Instance.plugins"),  # type: ignore[arg-type]
        (lambda: api.Engine(api.Instance(tmp_path / "file")), "not a directory"),
        (lambda: api.Period(hours="5"), "Period.hours"),  # type: ignore[arg-type]
        (lambda: api.Period(since="2026-01-01", until="2026-01-02"), "Period.since"),  # type: ignore[arg-type]
        (lambda: api.Call("openai", "gpt-6-sol", at="2026-01-01"), "Call.at"),  # type: ignore[arg-type]
        (lambda: api.Call("openai", "gpt-6-sol", tokens=[1]), "Call.tokens"),  # type: ignore[arg-type]
        (lambda: make_call("openai", [], {"input": 1}), "Call.model"),
        (lambda: make_call("openai", None, {"input": 1}), "Call.model"),
        (lambda: make_call("openai", 5, {"input": 1}), "Call.model"),
        (lambda: make_call("openai", "", {"input": 1}), "Call.model"),
        (lambda: make_call(None, "gpt-6-sol", {"input": 1}), "Call.provider"),
        (lambda: make_call(5, "gpt-6-sol", {"input": 1}), "Call.provider"),
        (lambda: app.report(WHOLE, groups="real"), "groups"),
        (lambda: app.report(WHOLE, groups=("bogus",)), "bogus"),
        (lambda: app.report("today"), "period"),  # type: ignore[arg-type]
    )
    for call, words in cases:
        _raises(call, words)


def test_the_doctor_of_a_missing_instance_names_the_instance_command(tmp_path: Path) -> None:
    root = tmp_path / "nowhere"
    assert engine(root).doctor() == [f"instance {root} does not exist yet — ai-cost instance init {root}"]


def test_a_detailed_row_shows_labels_only_when_it_has_some(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    app.record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=AT, labels={"tenant": "a"}))
    app.record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=AT))
    rows = json.loads(app.report_json(WHOLE, detail=True))["rows"]
    assert [row.get("labels") for row in rows] == [{"tenant": "a"}, None]


def test_an_instance_root_that_names_nothing_is_refused_for_text_and_paths_alike(tmp_path: Path) -> None:
    make: Any = api.Instance  # a str is taken too: the type says Path
    for unnamed in ("", " ", ".", "./", "a/..", Path(""), Path("."), Path("a/..")):
        _raises(partial(make, unnamed), "must name a directory", "Path.cwd()")
    assert make(Path.cwd()).root == Path.cwd(), "the current directory, named"


def test_the_library_expands_no_home_and_names_a_log_file_for_outside_logs(tmp_path: Path) -> None:
    make: Any = api.Instance
    _raises(partial(make, "~/app"), "Instance.root", "never expanded")
    _raises(partial(make, tmp_path, outside_logs=("~/log.jsonl",)), "Instance.outside_logs", "never expanded")
    for unnamed in ("", Path("")):
        _raises(partial(make, tmp_path, outside_logs=(unnamed,)), "names a usage log file")


def test_a_linked_root_with_a_usage_link_inside_its_real_root_is_read_and_written(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "data").mkdir(parents=True)
    (real / "usage").symlink_to(real / "data")
    (tmp_path / "linked").symlink_to(real)
    app = engine(tmp_path / "linked")
    app.record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=AT, keys={"ref": "kept"}))
    assert (real / "data" / "2026-09.jsonl").is_file()
    report = app.report(WHOLE)
    assert _refs(report) == ["kept"] and report.skipped == [], report.skipped


def test_the_doctor_names_a_configured_log_that_leads_nowhere(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2026-09", [])
    (root / "loop-a.jsonl").symlink_to(root / "loop-b.jsonl")
    (root / "loop-b.jsonl").symlink_to(root / "loop-a.jsonl")
    (root / "config.json").write_text(json.dumps({"usage_logs": ["loop-a.jsonl"]}), "utf-8")
    with doctor_on():
        problems = engine(root).doctor()
    assert (
        f"usage log {root / 'loop-a.jsonl'}: a link that leads nowhere — its lines are never read" in problems
    )


def test_an_instance_that_reads_the_clis_still_names_its_unread_month_entries(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2026-09", [])
    (root / "usage" / "2026-10.jsonl").mkdir()
    with doctor_on():
        problems = engine(root, read_clis=True).doctor()
    assert f"{root / 'usage'}/2026-10.jsonl: a directory named like a segment, not read" in problems, problems


def _record_at(root: Path) -> object:
    return engine(root).record(api.Call("openai", "gpt-6-sol", {"input": 1}, at=AT))


def test_record_refuses_a_segment_or_a_usage_directory_that_links_out(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "2026-09.jsonl").write_text("kept\n", "utf-8")
    segment_link = tmp_path / "one" / "usage"
    segment_link.mkdir(parents=True)
    (segment_link / "2026-09.jsonl").symlink_to(outside / "2026-09.jsonl")
    (tmp_path / "two").mkdir()
    (tmp_path / "two" / "usage").symlink_to(outside)
    for root in (tmp_path / "one", tmp_path / "two"):
        _raises(partial(_record_at, root), "cannot write", LEADS_OUT)
    assert (outside / "2026-09.jsonl").read_text("utf-8") == "kept\n", "the target outside is never written"


def test_a_dangling_and_a_looping_configured_log_are_skipped_beside_a_healthy_segment(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2026-09", [log_line(AT, ref="healthy")])
    (root / "dangling.jsonl").symlink_to(root / "gone.jsonl")
    (root / "loop-a.jsonl").symlink_to(root / "loop-b.jsonl")
    (root / "loop-b.jsonl").symlink_to(root / "loop-a.jsonl")
    config = {"usage_logs": ["dangling.jsonl", "loop-a.jsonl"]}
    (root / "config.json").write_text(json.dumps(config), "utf-8")
    report = engine(root).report(WHOLE)
    assert _refs(report) == ["healthy"]
    named = sorted(skip.path for skip in report.skipped)
    assert named == [str(root / "dangling.jsonl"), str(root / "loop-a.jsonl")], report.skipped


def test_a_writers_own_files_are_not_named_and_a_directory_like_a_segment_is(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2026-09", [log_line(AT, ref="here")])
    (root / "usage" / ".DS_Store").write_text("", "utf-8")
    (root / "usage" / "2026-09.jsonl.tmp").write_text("", "utf-8")
    (root / "usage" / "2026-10.jsonl").mkdir()  # outside the period: the doctor names it, the report does not
    report = engine(root).report(api.Period(since=BASE, until=datetime(2026, 10, 2, tzinfo=timezone.utc)))
    assert _refs(report) == ["here"]
    assert [(s.path, s.reason) for s in report.skipped] == [
        (str(root / "usage" / "2026-10.jsonl"), "a directory named like a segment: not read")
    ]
    assert engine(root).report(WHOLE).skipped == [], "a month outside the period is not the report's concern"
    assert not any("DS_Store" in w or "jsonl.tmp" in w for w in report.warnings), report.warnings
    doctor = engine(root).doctor()
    assert f"{root / 'usage'}/2026-10.jsonl: a directory named like a segment, not read" in doctor
    said = "\n".join(doctor)
    assert "DS_Store" not in said and "jsonl.tmp" not in said, said


def test_a_month_entry_that_leads_nowhere_is_a_counted_skip_never_a_silent_month(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2026-08", [])
    (root / "usage" / "2026-09.jsonl").symlink_to(root / "usage" / "gone.jsonl")
    (root / "usage" / "2026-10.jsonl").symlink_to(root / "usage" / "2026-10.jsonl")
    report = engine(root).report(api.Period(since=BASE, until=datetime(2026, 10, 2, tzinfo=timezone.utc)))
    assert [(s.path, s.reason) for s in report.skipped] == [
        (str(root / "usage" / "2026-09.jsonl"), "a link that leads nowhere: not read"),
        (str(root / "usage" / "2026-10.jsonl"), "a link that leads nowhere: not read"),
    ], report.skipped
    doctor = engine(root).doctor()
    for month in ("2026-09", "2026-10"):
        assert f"{root / 'usage'}/{month}.jsonl: a link that leads nowhere, not read" in doctor, doctor


def test_a_where_name_is_checked_by_the_label_rule(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    _raises(lambda: app.report(WHOLE, where={"bad name": "a"}), "where", "bad name")
    _raises(lambda: app.report(WHOLE, where={"t": "a\x01"}), "where")
    _raises(lambda: app.report(WHOLE, where={1: "a"}), "where maps label names to strings")  # type: ignore[dict-item]


def test_the_doctor_names_a_log_below_a_directory_link_that_leads_nowhere(tmp_path: Path) -> None:
    root = tmp_path / "app"
    write_segment(root, "2026-09", [])
    (root / "alias").symlink_to(root / "gone")
    (root / "config.json").write_text(json.dumps({"usage_logs": ["alias/events.jsonl"]}), "utf-8")
    with doctor_on():
        problems = engine(root).doctor()
    said = (
        f"usage log {root / 'alias' / 'events.jsonl'}: a link that leads nowhere — its lines are never read"
    )
    assert said in problems, problems
    assert [s.path for s in engine(root).report(WHOLE).skipped] == [str(root / "alias" / "events.jsonl")]


def test_a_period_is_one_form_hours_beside_a_date_included(tmp_path: Path) -> None:
    make: Any = api.Period
    for period in (
        {"since": BASE, "hours": 1},
        {"until": BASE, "hours": 1},
        {"since": BASE, "until": BASE + timedelta(hours=1), "hours": 1},
    ):
        _raises(partial(make, **period), "exactly one of the two forms")


def test_one_doctor_run_walks_the_usage_directory_once_for_its_checks(tmp_path: Path) -> None:
    from ..collectors import usage_log

    root = tmp_path / "app"
    write_segment(root, "2026-09", [])
    with doctor_on(), mock.patch("ai_cost.doctor.scan", wraps=usage_log.scan) as walked:
        engine(root).doctor()
    assert walked.call_count == 1


def test_an_instance_that_reads_the_clis_says_no_usage_found_once_over_both(tmp_path: Path) -> None:
    root = tmp_path / "app"
    (root / "usage").mkdir(parents=True)
    empty = {
        name: str(tmp_path / name)
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME", "GROK_HOME")
    }
    with doctor_on(), mock.patch.dict(os.environ, empty):
        with_clis = engine(root, read_clis=True).doctor()
        alone = engine(root).doctor()
    assert [p for p in with_clis if p.startswith("no usage found")], with_clis
    assert not [
        p for p in alone if p.startswith("no usage found")
    ], "without the CLIs an instance names no CLI"


def test_the_zone_rule_says_the_same_at_both_doors(tmp_path: Path) -> None:
    from .. import ops
    from .fixtures import run_cli

    naive = "2026-01-01T00:00:00"
    library = _raises(lambda: _instance_checked(ops, naive), "give the zone")
    code, _, err = run_cli(["--instance", str(tmp_path / "app"), "report", "--since", naive])
    assert code == 2 and library in err, (library, err)


def _instance_checked(ops: Any, since: str) -> None:
    with api._said():  # the library's translation of the tool's own errors
        ops._instance_checked(ops.ReportRequest(since=since), api.Instance(Path("/app")))
