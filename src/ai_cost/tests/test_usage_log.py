"""The usage log: the schema, provider-native counters, validation as counted skips, event ids, windows."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from unittest import mock

from ..collectors.usage_log import collect_usage_log
from ..models import Billing, Provider, RowKind
from ..timeutil import iso
from .fixtures import BASE, WINDOW


def _line(**fields: object) -> str:
    entry: dict[str, object] = {
        "schema": 1,
        "at": iso(BASE + timedelta(minutes=3)),
        "provider": "openai",
        "model": "gpt-5.5",
    }
    entry.update(fields)
    return json.dumps(entry)


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _isolated(tmp_path: Path) -> mock._patch_dict:
    """Every path the CLI reads under ``tmp_path``: the developer's own config and prices never decide a test."""
    env = {
        "AI_COST_CONFIG_DIR": str(tmp_path / "cfg"),
        "AI_COST_CONFIG": str(tmp_path / "cfg" / "config.json"),
        "AI_COST_PRICES": str(tmp_path / "cfg" / "prices.json"),
        "AI_COST_STATE_DIR": str(tmp_path / "state"),
        "AI_COST_USAGE_LOG": str(tmp_path / "default-usage.jsonl"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
        "CODEX_HOME": str(tmp_path / ".codex"),
        "GEMINI_CLI_HOME": str(tmp_path / ".gemini"),
        "AI_COST_OFFLINE": "1",
    }
    return mock.patch.dict(os.environ, env)


def test_lines_become_rows_with_their_providers_counters_billing_and_scope(tmp_path: Path) -> None:
    log = _write(
        tmp_path / "usage.jsonl",
        [
            _line(
                tokens={"input": 1200, "cached_input": 800, "output": 300},
                ref="job-42",
                branch="feat/x",
                pr="17",
                tags=["ci"],
            ),
            _line(
                provider="anthropic",
                model="claude-sonnet-5",
                tokens={"input": 10, "cache_read": 5},
                billing="subscription",
            ),
            _line(provider="google", model="gemini-3.8-flash", cost=0.0123, source="my-app"),
            _line(at=iso(BASE + timedelta(hours=5)), tokens={"input": 1}),  # outside the window
        ],
    )
    collected, warnings = collect_usage_log([log, tmp_path / "missing.jsonl"], WINDOW)
    assert warnings == [] and collected.skipped == []
    openai, anthropic, google = collected.rows
    assert openai.kind is RowKind.LOG and openai.provider is Provider.OPENAI
    assert openai.tokens.input == 1200 and openai.tokens.cached_input == 800 and openai.tokens.output == 300
    assert openai.billing is Billing.API and openai.ref == "job-42"
    assert openai.scope.branch == "feat/x" and openai.scope.pr == "17" and openai.scope.paths == ("ci",)
    assert anthropic.billing is Billing.SUBSCRIPTION and anthropic.tokens.cache_read == 5
    assert google.cost_reported == 0.0123 and google.billing is Billing.API and google.ref == "usage"


def test_bad_lines_are_counted_skips_and_unknown_keys_one_warning(tmp_path: Path) -> None:
    log = _write(
        tmp_path / "usage.jsonl",
        [
            "not json",
            json.dumps([1, 2]),
            _line(schema=2, tokens={"input": 1}),
            _line(at="yesterday", tokens={"input": 1}),
            _line(tokens={"input": -1}),
            _line(tokens={"input": True}),
            _line(tokens="x"),
            _line(cost=-0.5),
            _line(billing="free", tokens={"input": 1}),
            _line(provider="Acme Corp", tokens={"input": 1}),  # not a provider id; "acme" itself would be one
            _line(),  # neither tokens nor cost
            _line(tokens={"input": 1, "sparkles": 3}),  # unknown key: ignored, warned once
            _line(tokens={"input": 2, "sparkles": 4}),
        ],
    )
    collected, warnings = collect_usage_log([log], None)
    assert [r.tokens.input for r in collected.rows] == [1, 2]
    assert len(collected.skipped) == 11
    reasons = " | ".join(s.reason for s in collected.skipped)
    for word in (
        "not an object",
        "schema",
        "timestamp",
        "must be non-negative",
        "must be a number",
        "tokens must be an object",
        "cost",
        "billing",
        "neither",
        "not a provider id",
    ):
        assert word in reasons, word
    assert len(warnings) == 1 and "sparkles" in warnings[0]


def test_a_repeated_event_id_counts_once(tmp_path: Path) -> None:
    log = _write(
        tmp_path / "u.jsonl",
        [_line(event_id="e1", tokens={"input": 1})] * 3 + [_line(tokens={"input": 1})] * 2,
    )
    collected, _ = collect_usage_log([log], None)
    assert len(collected.rows) == 3, "one for e1, two without an id"


def test_a_huge_cost_an_empty_model_and_a_plugin_provider_are_handled_as_documented(tmp_path: Path) -> None:
    log = _write(
        tmp_path / "usage.jsonl",
        [
            _line(cost=10**400),  # a JSON integer beyond any float: a counted skip, never an OverflowError
            _line(model="", tokens={"input": 1}),  # an empty model is missing, never an unpriced row
            _line(
                provider="acme", tokens={"input": 3}
            ),  # any well-formed id is a provider (the pricebook decides)
        ],
    )
    collected, _ = collect_usage_log([log], None)
    assert [r.provider.value for r in collected.rows] == ["acme"] and collected.rows[0].tokens.input == 3
    reasons = [s.reason for s in collected.skipped]
    assert any("cost" in r and "finite" in r for r in reasons), reasons
    assert any("missing model" in r for r in reasons), reasons


def test_the_default_log_named_again_in_the_config_is_read_once(tmp_path: Path) -> None:
    from ..collectors.usage_log import _unique

    default = tmp_path / "data" / "ai-cost" / "usage.jsonl"
    same = tmp_path / "data" / ".." / "data" / "ai-cost" / "usage.jsonl"
    assert _unique([default, same, tmp_path / "other.jsonl"], []) == [default, tmp_path / "other.jsonl"]


def test_log_command_read_errors_and_bad_counters_are_usage_errors(tmp_path: Path) -> None:
    with _isolated(tmp_path):
        _run_test_log_command_read_errors_and_bad_counters_are_usage_errors(tmp_path)


def _run_test_log_command_read_errors_and_bad_counters_are_usage_errors(tmp_path: Path) -> None:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from .. import cli

    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        assert (
            cli.main(["log", "--from-response", str(tmp_path / "missing.json")]) == 2
        ), "no file: a UsageError"
    assert "cannot read" in err.getvalue()
    bad = tmp_path / "inf.json"
    bad.write_text('{"usage": {"input_tokens": 1e999, "output_tokens": 1}}')
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        assert cli.main(["log", "--from-response", str(bad), "--provider", "anthropic", "--model", "m"]) == 2
    assert "usage counter" in err.getvalue(), err.getvalue()


def test_log_command_has_a_flag_for_every_documented_counter_and_names_an_unwritable_log(
    tmp_path: Path,
) -> None:
    with _isolated(tmp_path):
        _run_test_log_command_has_a_flag_for_every_documented_counter_and_names_an_unwritable_log(tmp_path)


def _run_test_log_command_has_a_flag_for_every_documented_counter_and_names_an_unwritable_log(
    tmp_path: Path,
) -> None:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from .. import cli

    target = tmp_path / "logs" / "u.jsonl"
    flags = ["--thoughts", "5", "--cache-hit", "2", "--cache-miss", "1", "--cache-write-unsplit", "3"]
    with redirect_stdout(
        io.StringIO()
    ):  # a provider without a formula: every counter is accepted, a cost is required
        assert (
            cli.main(
                ["log", "--provider", "acme", "--model", "m", "--cost", "0.01", *flags, "--log", str(target)]
            )
            == 0
        )
    written = json.loads(target.read_text().splitlines()[-1])
    assert written["tokens"] == {"thoughts": 5, "cache_hit": 2, "cache_miss": 1, "cache_write_unsplit": 3}
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the log directory should be")
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = cli.main(
            [
                "log",
                "--provider",
                "google",
                "--model",
                "gemini-3.8-flash",
                "--prompt",
                "1",
                "--log",
                str(blocked / "u.jsonl"),
            ]
        )
    assert code == 1 and "cannot write" in err.getvalue(), "an unwritable log path is a ToolError"


def test_log_command_refuses_counter_flags_next_to_a_response(tmp_path: Path) -> None:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from .. import cli

    response = tmp_path / "r.json"
    response.write_text(
        json.dumps(
            {"type": "message", "model": "claude-sonnet-5", "usage": {"input_tokens": 1, "output_tokens": 1}}
        )
    )
    err = io.StringIO()
    with _isolated(tmp_path), redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = cli.main(
            ["log", "--from-response", str(response), "--input", "5", "--log", str(tmp_path / "u.jsonl")]
        )
    assert code == 2 and "drop --input" in err.getvalue(), err.getvalue()


def test_log_command_refuses_what_the_reader_would_skip(tmp_path: Path) -> None:
    with _isolated(tmp_path):
        _run_test_log_command_refuses_what_the_reader_would_skip(tmp_path)


def _run_test_log_command_refuses_what_the_reader_would_skip(tmp_path: Path) -> None:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from .. import cli

    target = str(tmp_path / "u.jsonl")
    cases = {
        "--cost inf": ["--provider", "openai", "--model", "m", "--cost", "inf"],
        "negative counter": ["--provider", "openai", "--model", "m", "--input", "-5"],
        "all-zero counters": ["--provider", "openai", "--model", "m", "--input", "0", "--output", "0"],
        "malformed provider": ["--provider", "Acme Corp", "--model", "m", "--input", "1"],
        "unlisted provider without a cost": ["--provider", "acme", "--model", "m", "--input", "1"],
    }
    for name, flags in cases.items():
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            assert cli.main(["log", *flags, "--log", target]) == 2, name
        assert err.getvalue().strip(), name
    with redirect_stdout(io.StringIO()):
        assert (
            cli.main(
                [
                    "log",
                    "--provider",
                    "acme",
                    "--model",
                    "m",
                    "--input",
                    "1",
                    "--cost",
                    "0.1",
                    "--log",
                    target,
                ]
            )
            == 0
        )
    assert (
        json.loads(Path(target).read_text().splitlines()[-1])["cost"] == 0.1
    ), "a cost makes any provider loggable"


def test_thoughts_count_as_output_and_the_cli_offers_every_logged_counter(tmp_path: Path) -> None:
    with _isolated(tmp_path):
        _run_test_thoughts_count_as_output_and_the_cli_offers_every_logged_counter(tmp_path)


def _run_test_thoughts_count_as_output_and_the_cli_offers_every_logged_counter(tmp_path: Path) -> None:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from .. import cli
    from ..collectors.usage_log import COUNTERS, LOG_COUNTERS

    log = _write(
        tmp_path / "usage.jsonl", [_line(provider="google", tokens={"prompt": 5, "output": 2, "thoughts": 7})]
    )
    collected, warnings = collect_usage_log([log], None)
    assert (
        collected.rows[0].tokens.output == 9 and not warnings
    ), "thoughts fold into output, no unknown-key warning"
    assert (
        set(LOG_COUNTERS) >= COUNTERS - {"requests", "web_search", "reviews"} and "thoughts" in LOG_COUNTERS
    )
    target = str(tmp_path / "u.jsonl")
    with redirect_stdout(io.StringIO()):
        for name in LOG_COUNTERS:  # every counter the reader knows has a flag the parser accepts
            flags = [
                "--provider",
                "acme",  # no formula of its own: any counter, with a cost
                "--model",
                "m",
                "--cost",
                "0.01",
                f"--{name.replace('_', '-')}",
                "1",
            ]
            assert cli.main(["log", *flags, "--log", target]) == 0, name
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = cli.main(
            ["log", "--provider", "openai", "--model", "gpt-99-unlisted", "--input", "1", "--log", target]
        )
    assert (
        code == 2 and "no list price" in err.getvalue()
    ), "a listed provider with an unpriced model is refused too"


def test_record_validates_like_the_cli_and_a_duplicate_outside_the_window_keeps_no_id(tmp_path: Path) -> None:
    from ..log import record
    from .fixtures import defaults, paths_in

    _, book = defaults(paths_in(tmp_path))
    log = tmp_path / "u.jsonl"
    refusals: list[tuple[str, dict[str, int], float | None]] = [
        ("openai", {"input": -1}, None),  # a negative counter
        ("openai", {"input": 0}, None),  # nothing positive and no cost
        ("openai", {"input": 1}, float("inf")),  # a cost that is not an amount
        ("Acme Corp", {"input": 1}, None),  # not a provider id
    ]
    for provider, tokens, cost in refusals:
        try:
            record(provider, "gpt-5.5", tokens, cost, path=log)
        except ValueError:
            continue
        raise AssertionError(f"{provider} {tokens} {cost}: record() must refuse what the reader would skip")
    try:
        record("openai", "gpt-99-unlisted", {"input": 1}, path=log, book=book)
    except ValueError as exc:
        assert "no list price" in str(exc)
    else:
        raise AssertionError("with a pricebook an unpriced model needs a cost")
    assert not log.exists(), "nothing was written"
    early, late = iso(BASE - timedelta(days=2)), iso(BASE + timedelta(minutes=5))
    lines = [
        _line(at=early, event_id="e1", tokens={"input": 1}),
        _line(at=late, event_id="e1", tokens={"input": 2}),
    ]
    collected, _ = collect_usage_log([_write(tmp_path / "w.jsonl", lines)], WINDOW)
    assert [r.tokens.input for r in collected.rows] == [2], "the copy outside the window consumed no id"


def test_validated_refuses_boolean_and_fractional_counters_and_a_cost_beyond_float_range() -> None:
    from ..log import Usage, validated

    for tokens, cost in (({"input": True}, None), ({"input": 1.5}, None), ({"input": 1}, 10**400)):
        try:
            validated(Usage("openai", "gpt-5.5", tokens, cost))  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"{tokens} {cost}: the reader would skip this line; validated() must refuse it")


def test_validated_normalises_refuses_unknown_keys_and_an_empty_model_and_the_source_key_survives(
    tmp_path: Path,
) -> None:
    from ..log import Usage, validated

    normalised = validated(Usage("openai", "gpt-5.5", {"input": 3.0}, 1.0))  # type: ignore[dict-item]
    assert normalised.tokens == {"input": 3} and normalised.cost == 1.0, "what the reader will read: ints"
    for usage in (Usage("openai", "gpt-5.5", {"sparkles": 1}), Usage("openai", "", {"input": 1})):
        try:
            validated(usage)
        except ValueError:
            continue
        raise AssertionError(f"{usage}: the reader would skip or ignore this line")
    log = _write(tmp_path / "usage.jsonl", [_line(tokens={"input": 1}, source="my-app")])
    collected, _ = collect_usage_log([log], None)
    assert collected.rows[0].source == "my-app", "the reporting program's name is kept"


def test_the_report_reads_the_usage_log_of_its_paths_not_the_process_environment(tmp_path: Path) -> None:
    from ai_cost.tests.fixtures import defaults, paths_in, write_claude_session

    from ..ops import ReportRequest, build_report

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    assert paths.usage_log is not None
    _write(paths.usage_log, [_line(tokens={"input": 7}, ref="\u017cyrafa")])  # non-ASCII survives as written
    elsewhere = _write(tmp_path / "elsewhere.jsonl", [_line(tokens={"input": 99})])
    config, book = defaults(paths)
    request = ReportRequest(project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end))
    with mock.patch.dict(os.environ, {"AI_COST_USAGE_LOG": str(elsewhere)}):
        report = build_report(request, paths, config, book)
    logged = [r for r in report.rows if r.source == "usage-log"]
    assert [r.tokens.input for r in logged] == [7], "the Paths decide, the environment does not"
    assert logged[0].ref == "\u017cyrafa", "read as UTF-8, like the writer wrote it"


def test_record_checks_its_optional_keys_and_the_cli_writes_to_the_paths_it_is_given(tmp_path: Path) -> None:
    from ai_cost.tests.fixtures import paths_in

    from .. import cli
    from ..log import record

    log = tmp_path / "u.jsonl"
    for keys in ({"tags": "ci"}, {"billing": "free"}):
        try:
            record("openai", "gpt-5.5", {"input": 1}, path=log, **keys)  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"{keys}: record() must refuse a key the reader would misread or skip")
    line = record("openai", "gpt-5.5", {"input": 1}, path=log, tags=["ci"], billing="api")
    assert line["tags"] == ["ci"] and line["billing"] == "api"
    paths = paths_in(tmp_path)
    args = cli.build_parser().parse_args(
        ["log", "--provider", "openai", "--model", "gpt-5.5", "--input", "2"]
    )
    with mock.patch.dict(
        os.environ, {"AI_COST_USAGE_LOG": str(tmp_path / "elsewhere.jsonl"), "AI_COST_OFFLINE": "1"}
    ):
        assert cli.cmd_log(args, paths) == 0
    assert paths.usage_log is not None and paths.usage_log.exists(), "written where the Paths say"
    assert not (tmp_path / "elsewhere.jsonl").exists(), "not where the process environment says"


def test_attribution_treats_missing_tags_as_none_and_a_non_iterable_as_a_value_error() -> None:
    from ..log import Attribution

    assert Attribution(tags=None).tags == ()  # type: ignore[arg-type]
    try:
        Attribution(tags=7)  # type: ignore[arg-type]
    except ValueError as exc:
        assert "list of strings" in str(exc)
    else:
        raise AssertionError("a non-iterable is a ValueError, as record() promises")


def test_an_unhashable_billing_value_is_the_documented_skip(tmp_path: Path) -> None:
    log = _write(tmp_path / "usage.jsonl", [_line(billing=["api"], tokens={"input": 1})])
    collected, _ = collect_usage_log([log], None)
    assert not collected.rows and any(
        "billing must be api or subscription" in s.reason for s in collected.skipped
    )


def test_what_the_pricing_would_hide_or_python_would_let_through_is_a_counted_skip(tmp_path: Path) -> None:
    lines = [
        _line(tokens={"input": 10**400}),  # beyond any float: pricing would overflow
        _line(schema=True, tokens={"input": 1}),  # True == 1 in Python, not in the schema
        _line(schema=1.0, tokens={"input": 1}),
        _line(cost=1.5, currency="EUR"),
        _line(provider=1, tokens={"input": 1}),  # a number is not a provider id, however it prints
        _line(model=["gpt-5.5"], tokens={"input": 1}),
        _line(provider="deepseek", model="deepseek-chat", tokens={"input": 5, "output": 1}),  # priced at zero
        _line(
            tokens={"input": 1, "requests": 5}
        ),  # a tool-counted key: ignored with a warning, the line counts
    ]
    collected, warnings = collect_usage_log([_write(tmp_path / "u.jsonl", lines)], WINDOW)
    reasons = [s.reason for s in collected.skipped]
    for word in (
        "at most",
        "schema True",
        "schema 1.0",
        "currency 'EUR'",
        "provider must be a string",
        "model must be a string",
        "would not be priced",
    ):
        assert any(word in r for r in reasons), (word, reasons)
    assert len(collected.rows) == 1 and collected.rows[0].tokens.input == 1
    assert warnings and "requests" in warnings[0], "a tool-counted key is unknown to the reader"


def test_the_counters_are_checked_against_the_models_own_price_shape_when_the_book_is_known(
    tmp_path: Path,
) -> None:
    from ..models import PeakOffpeakPrice, TokenTierPrice
    from ..pricing import (
        ANTHROPIC_COUNTERS,
        GOOGLE_COUNTERS,
        OPENAI_COUNTERS,
        PEAK_OFFPEAK_COUNTERS,
        TOKEN_TIER_COUNTERS,
        counters_of,
    )
    from .fixtures import defaults, paths_in

    _, book = defaults(paths_in(tmp_path))
    tiers = book.entry(Provider.OPENAI, "gpt-5.5")
    peak = book.entry(Provider.DEEPSEEK, "deepseek-flash")
    assert isinstance(tiers, TokenTierPrice) and isinstance(peak, PeakOffpeakPrice)
    assert (
        counters_of(Provider.DEEPSEEK, tiers) == OPENAI_COUNTERS
    ), "a DeepSeek model priced by tiers reads input"
    assert counters_of(Provider.OPENAI, peak) == PEAK_OFFPEAK_COUNTERS
    assert counters_of(Provider.GOOGLE, tiers) == GOOGLE_COUNTERS
    assert (
        counters_of(Provider.ANTHROPIC, tiers) == ANTHROPIC_COUNTERS
        and "cached_input" not in ANTHROPIC_COUNTERS
    )
    assert (
        counters_of(Provider.of("acme"), tiers) == TOKEN_TIER_COUNTERS
    ), "a user-added provider: either family"
    assert (
        counters_of(Provider.GITHUB, tiers) == frozenset()
    ), "GitHub is billed per unit whatever an entry says"
    assert (
        counters_of(Provider.of("acme")) is None and counters_of(Provider.DEEPSEEK) == PEAK_OFFPEAK_COUNTERS
    )


def test_the_reader_checks_counters_by_the_models_own_price_shape_when_it_has_the_book(
    tmp_path: Path,
) -> None:
    from .fixtures import defaults, paths_in

    _, book = defaults(paths_in(tmp_path))
    log = _write(
        tmp_path / "u.jsonl",
        [
            _line(
                tokens={"cache_hit": 3, "output": 1}
            ),  # openai/gpt-5.5 is priced by tiers: cache_hit is not read
            _line(provider="deepseek", model="deepseek-flash", tokens={"input": 3, "output": 1}),
            _line(provider="deepseek", model="deepseek-flash", tokens={"cache_miss": 3, "output": 1}),
        ],
    )
    collected, _ = collect_usage_log([log], None, book)
    assert [r.provider for r in collected.rows] == [Provider.DEEPSEEK] and len(collected.skipped) == 2
    assert all("would not be priced" in s.reason for s in collected.skipped), [
        s.reason for s in collected.skipped
    ]


def test_a_web_search_is_a_per_request_counter_of_anthropic_only(tmp_path: Path) -> None:
    log = _write(
        tmp_path / "u.jsonl",
        [
            _line(
                provider="anthropic",
                model="claude-sonnet-5",
                tokens={"input": 5, "output": 1, "web_search": 2},
            ),
            _line(tokens={"input": 5, "output": 1, "web_search": 2}),  # openai: its formula never reads it
            _line(
                tokens={"input": 5, "cache_read": 3}
            ),  # an Anthropic counter on an OpenAI line: a zero rate
        ],
    )
    collected, warnings = collect_usage_log([log], None)
    assert [r.tokens.web_search for r in collected.rows] == [2] and not warnings
    assert len(collected.skipped) == 2 and all("would not be priced" in s.reason for s in collected.skipped)


def test_a_nested_line_is_a_skip_and_an_event_id_is_a_string(tmp_path: Path) -> None:
    nested = "[" * 100_000 + "]" * 100_000
    log = _write(
        tmp_path / "u.jsonl",
        [
            nested,
            _line(tokens={"input": 1}, event_id="e0"),
            _line(tokens={"input": 1}, event_id="e0"),
            _line(tokens={"input": 2}, event_id=0),  # not a string: 0 and "0" would collide
            _line(tokens={"input": 3}, event_id=["x"]),
        ],
    )
    collected, _ = collect_usage_log([log], None)
    assert [r.tokens.input for r in collected.rows] == [
        1
    ], "a repeated id is read once; a non-string id is a skip"
    reasons = [s.reason for s in collected.skipped]
    assert len(reasons) == 3 and sum("event_id must be a string" in r for r in reasons) == 2, reasons


def test_the_sources_line_names_the_writers_of_the_log(tmp_path: Path) -> None:
    from ..ops import ReportRequest, build_report
    from .fixtures import defaults, paths_in

    paths = paths_in(tmp_path)
    _write(
        paths.usage_log or tmp_path / "usage.jsonl",
        [_line(tokens={"input": 1}, source="my-app"), _line(tokens={"input": 1}, source="ci")],
    )
    config, book = defaults(paths)
    report = build_report(
        ReportRequest(
            all_projects=True, since=iso(BASE), until=iso(BASE + timedelta(hours=1)), groups=("api",)
        ),
        paths,
        config,
        book,
    )
    assert any(s == "usage-log ×2 (ci, my-app)" for s in report.sources), report.sources


def test_a_github_line_needs_a_cost_and_a_symlink_loop_is_a_skip(tmp_path: Path) -> None:
    import os

    from ..collectors.usage_log import _unique
    from ..models import Skipped

    log = _write(
        tmp_path / "u.jsonl",
        [
            _line(provider="github", model="copilot-code-review", tokens={"input": 5}),
            _line(provider="github", model="copilot-code-review", cost=0.04),
        ],
    )
    collected, _ = collect_usage_log([log], None)
    assert [r.cost_reported for r in collected.rows] == [0.04]
    assert len(collected.skipped) == 1 and "no token counter" in collected.skipped[0].reason
    loop = tmp_path / "loop"
    os.symlink(loop, loop)  # a link to itself
    loop_dir = tmp_path / "loopdir"
    os.symlink(loop_dir, loop_dir)  # a looping DIRECTORY component: the file's own name is not a link
    skipped: list[Skipped] = []
    assert _unique([loop, loop_dir / "u.jsonl", log], skipped) == [log]
    assert len(skipped) == 2 and all("cannot resolve" in s.reason for s in skipped)
    absent: list[Skipped] = []
    assert _unique([tmp_path / "absent.jsonl"], absent) == [tmp_path / "absent.jsonl"] and not absent
    dangling = tmp_path / "dangling"
    os.symlink(tmp_path / "nowhere.jsonl", dangling)  # a link whose target does not exist is a counted skip
    missing_dir_link = tmp_path / "missing-link"
    # A dangling DIRECTORY link above the file's own name is unreadable too.
    os.symlink(tmp_path / "no-such-dir", missing_dir_link)
    gone: list[Skipped] = []
    assert _unique([dangling, missing_dir_link / "u.jsonl"], gone) == []
    assert len(gone) == 2 and all("cannot resolve" in s.reason for s in gone)


def test_a_cost_only_line_reaches_the_api_group_and_a_bad_line_of_another_window_is_not_this_windows_skip(
    tmp_path: Path,
) -> None:
    from ..groups import api_group
    from .fixtures import defaults, paths_in

    config, book = defaults(paths_in(tmp_path))
    log = _write(
        tmp_path / "u.jsonl",
        [
            _line(provider="github", model="copilot-code-review", cost=0.04),
            _line(
                tokens={"input": -1}, at=iso(BASE - timedelta(days=40))
            ),  # malformed, and of another window
            _line(tokens={"input": -1}),  # malformed, in this window
        ],
    )
    collected, _ = collect_usage_log([log], WINDOW, book)
    assert len(collected.rows) == 1 and len(collected.skipped) == 1, [s.reason for s in collected.skipped]
    assert round(api_group(collected.rows, book, config).total_usd, 4) == 0.04, "the reported charge, not 0"


def test_a_bad_line_after_one_of_another_window_is_still_this_windows_skip(tmp_path: Path) -> None:
    log = _write(
        tmp_path / "u.jsonl",
        [
            _line(tokens={"input": -1}, at=iso(BASE - timedelta(days=40))),
            "not json at all",
            _line(tokens={"input": 2}),
        ],
    )
    collected, _ = collect_usage_log([log], WINDOW)
    assert [r.tokens.input for r in collected.rows] == [2]
    assert [s.reason for s in collected.skipped] == ["line 2: not JSON"] or (
        len(collected.skipped) == 1 and "line 2" in collected.skipped[0].reason
    ), [s.reason for s in collected.skipped]


def test_a_list_ref_or_a_numeric_source_is_a_named_skip(tmp_path: Path) -> None:
    log = _write(
        tmp_path / "u.jsonl", [_line(tokens={"input": 1}, ref=["a"]), _line(tokens={"input": 2}, source=7)]
    )
    collected, _ = collect_usage_log([log], None)
    assert not collected.rows and sorted(s.reason for s in collected.skipped) == [
        "line 1: ref must be a string, got list",
        "line 2: source must be a string, got int",
    ]


def test_the_log_command_notes_a_target_no_report_reads(tmp_path: Path) -> None:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from .. import cli

    err = io.StringIO()
    with _isolated(tmp_path), redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = cli.main(
            [
                "log",
                "--provider",
                "openai",
                "--model",
                "gpt-5.5",
                "--input",
                "1",
                "--log",
                str(tmp_path / "elsewhere.jsonl"),
            ]
        )
    assert code == 0 and "not a file reports read" in err.getvalue(), err.getvalue()
    err = io.StringIO()
    with _isolated(tmp_path), redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = cli.main(
            [
                "log",
                "--provider",
                "openai",
                "--model",
                "gpt-5.5",
                "--input",
                "1",
                "--log",
                str(tmp_path / "default-usage.jsonl"),
            ]
        )
    assert code == 0 and "not a file reports read" not in err.getvalue(), "the default log needs no note"


def test_a_null_counter_and_a_non_string_tag_are_named_skips(tmp_path: Path) -> None:
    log = _write(
        tmp_path / "u.jsonl",
        [_line(tokens={"input": None, "output": 5}), _line(tokens={"input": 1}, tags=[7])],
    )
    collected, _ = collect_usage_log([log], None)
    assert not collected.rows
    reasons = sorted(s.reason for s in collected.skipped)
    assert reasons == [
        "line 1: input is null: a counter is a whole number or absent",
        "line 2: tags must be a list of strings",
    ], reasons


def test_the_reader_folds_native_deepseek_counters_of_a_tiers_priced_model_like_the_writer(
    tmp_path: Path,
) -> None:
    """A line an app wrote without the book still counts: hit + miss is input, hit is cached; both spellings are a skip."""
    from ..config import load_pricebook
    from .fixtures import paths_in

    paths = paths_in(tmp_path)
    paths.user_prices_file().parent.mkdir(parents=True, exist_ok=True)
    paths.user_prices_file().write_text(
        json.dumps(
            {
                "schema": 1,
                "providers": {"deepseek": {"models": {"deepseek-tiers": {"input": 1.0, "output": 2.0}}}},
            }
        )
    )
    book = load_pricebook(paths)
    log = _write(
        tmp_path / "u.jsonl",
        [
            _line(
                provider="deepseek",
                model="deepseek-tiers",
                tokens={"cache_hit": 4, "cache_miss": 15, "output": 10},
            ),
            _line(
                provider="deepseek",
                model="deepseek-tiers",
                tokens={"input": 19, "cache_hit": 4, "output": 10},
            ),
            _line(provider="acme", model="m1", tokens={"cached_input": 5, "output": 1}, cost=0.01),
        ],
    )
    collected, _ = collect_usage_log([log], None, book)
    assert [(r.tokens.input, r.tokens.cached_input, r.tokens.cache_hit) for r in collected.rows] == [
        (19, 4, 0),
        (0, 5, 0),
    ], "folded like the writer; an unlisted provider keeps every known counter"
    assert [s.reason for s in collected.skipped] == [
        "line 2: cache_hit/cache_miss and input/cached_input are two spellings of the same usage: give one"
    ], [s.reason for s in collected.skipped]
