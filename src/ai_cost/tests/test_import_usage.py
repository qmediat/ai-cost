"""``ai-cost --instance DIR import usage FILE|-`` and ``Engine.import_usage``: a log stream's usage lines, replay-safe."""

from __future__ import annotations

import io
import json
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest import mock

from .. import api
from ..collectors import usage_log
from ..timeutil import iso
from .engine_fixtures import BOUNDARY, WHOLE, engine, log_line, raises_api, write_segment
from .fixtures import BASE, run_cli

AT = BASE + timedelta(minutes=10)
BOM = b"\xef\xbb\xbf"
_TRUE: Any = True  # a boolean where a time belongs, past the type checker


def _stream(tmp: Path, lines: list[str]) -> Path:
    path = tmp / "stream.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _import(tmp: Path, *argv: str, root: str = "app") -> tuple[int, str, str]:
    return run_cli(["--instance", str(tmp / root), "import", "usage", *argv])


def _from_stdin(tmp: Path, data: bytes, root: str) -> tuple[int, str, str]:
    """The command fed real bytes on stdin, as a pipe gives them."""
    with mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(data), encoding="ascii")):
        return _import(tmp, "-", root=root)


def _from_file(tmp: Path, data: bytes, root: str) -> tuple[int, str, str]:
    path = tmp / f"{root}.log"
    path.write_bytes(data)
    return _import(tmp, str(path), root=root)


def _timed(at: Any) -> str:
    """A usage line whose ``at`` is the value given."""
    return json.dumps({**json.loads(log_line(AT, event_id="a")), "at": at})


class _FailsAfter(io.RawIOBase):
    """A pipe that gives ``data`` once, then fails the next read as a broken device would."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if not self.data:
            raise OSError(5, "Input/output error")
        data, self.data = self.data, b""
        buffer[: len(data)] = data
        return len(data)


def _segment(tmp: Path, month: str, root: str = "app") -> list[dict[str, Any]]:
    path = tmp / root / "usage" / f"{month}.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def test_a_stream_lands_in_the_segments_of_its_lines_own_months(tmp_path: Path) -> None:
    before, after = BOUNDARY - timedelta(hours=1), BOUNDARY + timedelta(hours=1)
    stream = _stream(
        tmp_path,
        [log_line(before, event_id="a", labels={"tenant": "acme"}), log_line(after, event_id="b")],
    )
    code, out, _ = _import(tmp_path, str(stream))
    october, november = _segment(tmp_path, "2026-10"), _segment(tmp_path, "2026-11")
    assert (code, out) == (0, "imported 2\n")
    assert [(line["event_id"], line["at"]) for line in october] == [("a", "2026-10-31T23:00:00Z")]
    assert [(line["event_id"], line["at"]) for line in november] == [("b", "2026-11-01T01:00:00Z")]
    assert october[0]["labels"] == {"tenant": "acme"}, "the line's labels are kept"


def test_the_same_stream_twice_imports_nothing_the_second_time(tmp_path: Path) -> None:
    stream = _stream(tmp_path, [log_line(AT, event_id="a"), log_line(AT, event_id="b")])
    _import(tmp_path, str(stream))
    written = _segment(tmp_path, "2026-09")
    code, out, _ = _import(tmp_path, str(stream))
    assert (code, out) == (0, "imported 0\nalready there 2\n")
    assert _segment(tmp_path, "2026-09") == written


def test_a_duplicate_inside_one_stream_is_written_once(tmp_path: Path) -> None:
    stream = _stream(tmp_path, [log_line(AT, event_id="a"), log_line(AT, event_id="a")])
    code, out, _ = _import(tmp_path, str(stream))
    assert (code, out) == (0, "imported 1\nalready there 1\n")
    assert len(_segment(tmp_path, "2026-09")) == 1


def test_an_empty_stream_says_it_imported_nothing(tmp_path: Path) -> None:
    assert _import(tmp_path, str(_stream(tmp_path, []))) == (0, "imported 0\n", "")


def test_a_line_without_an_event_id_is_skipped_and_fails_the_run(tmp_path: Path) -> None:
    stream = _stream(tmp_path, [log_line(AT, event_id="a"), log_line(AT)])
    code, out, _ = _import(tmp_path, str(stream))
    assert (code, out) == (1, "imported 1\nskipped 1 — no event_id: line 2\n")


def test_other_lines_of_the_stream_are_not_usage_lines_and_do_not_fail(tmp_path: Path) -> None:
    stream = _stream(tmp_path, ["GET /health 200", "", "[1, 2]", log_line(AT, event_id="a")])
    code, out, _ = _import(tmp_path, str(stream))
    assert (code, out) == (0, "imported 1\nskipped 2 — not a usage line\n")


def test_an_apps_own_json_log_records_are_not_usage_lines(tmp_path: Path) -> None:
    records = ['{"level": "info", "msg": "started"}', '{"level": "warn", "msg": "slow", "at": "2026-09-19"}']
    code, out, _ = _import(tmp_path, str(_stream(tmp_path, [*records, log_line(AT, event_id="a")])))
    assert (code, out) == (0, "imported 1\nskipped 2 — not a usage line\n")


def test_an_invalid_usage_line_is_one_class_with_the_validators_words(tmp_path: Path) -> None:
    bogus = log_line(AT, event_id="a", tokens={"bogus": 1})
    naive = log_line(AT, event_id="b").replace('Z"', '"')
    code, out, _ = _import(tmp_path, str(_stream(tmp_path, [bogus, naive])))
    call = api.Call("openai", "gpt-6-sol", {"bogus": 1}, at=AT)
    refused = raises_api(partial(engine(tmp_path / "other").record, call))
    assert (code, out) == (1, f"imported 0\nskipped 2 — invalid usage line: line 1: {refused}\n")
    assert not (tmp_path / "app" / "usage" / "2026-09.jsonl").exists()


def test_a_time_without_its_zone_is_named(tmp_path: Path) -> None:
    naive = log_line(AT, event_id="b").replace('Z"', '"')
    result = engine(tmp_path / "app").import_usage([naive])
    assert "give the zone" in result.examples[api.SkipClass.INVALID] and result.failed == 1, result


def test_a_systematic_error_is_one_line_however_many_lines_it_hits(tmp_path: Path) -> None:
    lines = [log_line(AT, event_id=f"e{n}", tokens={f"bogus{n}": 1}) for n in range(50)]
    result = engine(tmp_path / "app").import_usage(lines)
    assert (
        result.skipped == {api.SkipClass.INVALID: 50} and "bogus0" in result.examples[api.SkipClass.INVALID]
    ), result


def test_a_key_record_does_not_know_is_refused_by_name(tmp_path: Path) -> None:
    result = engine(tmp_path / "app").import_usage([log_line(AT, event_id="a", region="eu")])
    assert result.failed == 1 and "unknown key(s) region" in result.examples[api.SkipClass.INVALID], result


def test_a_line_over_the_instances_limit_is_skipped(tmp_path: Path) -> None:
    long = log_line(AT, event_id="a", ref="r" * 5000)
    code, out, _ = _import(tmp_path, str(_stream(tmp_path, [long])))
    assert code == 1 and out.startswith("imported 0\nskipped 1 — line too long: line 1: the line is "), out
    assert not (tmp_path / "app" / "usage" / "2026-09.jsonl").exists()


def test_a_usage_line_behind_a_log_prefix_is_imported(tmp_path: Path) -> None:
    lines = [
        f"2026-09-19T15:10:00.123456789Z stdout F {log_line(AT, event_id='a')}",
        f"Sep 19 15:10:00 box app[42]: {log_line(AT, event_id='b')}",
    ]
    assert _import(tmp_path, str(_stream(tmp_path, lines))) == (0, "imported 2\n", "")


def test_a_torn_usage_line_fails_the_run(tmp_path: Path) -> None:
    torn = log_line(AT, event_id="a")[:40]
    code, out, _ = _import(tmp_path, str(_stream(tmp_path, [torn])))
    assert code == 1 and out.startswith(
        "imported 0\nskipped 1 — a usage line that cannot be read: line 1: "
    ), out


def test_a_byte_order_mark_is_dropped_at_either_door(tmp_path: Path) -> None:
    data = BOM + (log_line(AT, event_id="a") + "\n").encode()
    assert (
        _from_file(tmp_path, data, "file") == _from_stdin(tmp_path, data, "stdin") == (0, "imported 1\n", "")
    )


def test_stdin_and_a_file_read_the_same_bytes_alike(tmp_path: Path) -> None:
    data = b"noise \xff\xfe\n" + "\n".join([log_line(AT, event_id="a"), log_line(AT)]).encode() + b"\n"
    from_file, from_stdin = _from_file(tmp_path, data, "file"), _from_stdin(tmp_path, data, "stdin")
    assert from_file == from_stdin and from_file[0] == 1, (from_file, from_stdin)
    assert _segment(tmp_path, "2026-09", "file") == _segment(tmp_path, "2026-09", "stdin")


def test_import_usage_without_an_instance_is_a_usage_error(tmp_path: Path) -> None:
    code, out, err = run_cli(["import", "usage", str(_stream(tmp_path, [log_line(AT, event_id="a")]))])
    assert (code, out) == (2, "") and "needs an instance" in err, err


def test_import_of_an_export_on_an_instance_is_refused_by_its_two_words(tmp_path: Path) -> None:
    code, _, err = run_cli(
        ["--instance", str(tmp_path / "app"), "import", "deepseek", str(tmp_path / "x.zip")]
    )
    assert code == 2 and err.startswith("ai-cost: import deepseek works on"), err


def test_a_missing_file_is_a_usage_error(tmp_path: Path) -> None:
    code, out, err = _import(tmp_path, str(tmp_path / "absent.log"))
    assert (code, out) == (2, "") and "cannot read" in err, err


def test_a_segment_that_links_out_of_the_instance_is_refused_as_record_refuses_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text("kept\n", encoding="utf-8")
    (tmp_path / "app" / "usage").mkdir(parents=True)
    (tmp_path / "app" / "usage" / "2026-09.jsonl").symlink_to(outside)
    code, _, err = _import(tmp_path, str(_stream(tmp_path, [log_line(AT, event_id="a")])))
    raises_api(partial(engine(tmp_path / "app").import_usage, [log_line(AT, event_id="a")]), "cannot write")
    assert code == 1 and usage_log.LEADS_OUT in err, err
    assert outside.read_text("utf-8") == "kept\n", "the target outside is never written"


def test_the_library_returns_the_counts_the_command_prints(tmp_path: Path) -> None:
    lines = [log_line(AT, event_id="a"), log_line(AT, event_id="a"), "noise", log_line(AT)]
    result = engine(tmp_path / "lib").import_usage(lines)
    _, out, _ = _import(tmp_path, str(_stream(tmp_path, lines)))
    assert result == api.UsageImport(
        1,
        1,
        {api.SkipClass.NOT_A_USAGE_LINE: 1, api.SkipClass.NO_EVENT_ID: 1},
        {api.SkipClass.NO_EVENT_ID: "line 4"},
    )
    assert (
        out == "imported 1\nalready there 1\nskipped 1 — not a usage line\nskipped 1 — no event_id: line 4\n"
    )
    assert result.failed == 1


def test_one_string_is_refused_as_the_lines(tmp_path: Path) -> None:
    raises_api(
        partial(engine(tmp_path / "app").import_usage, log_line(AT, event_id="a")), "iterable of lines"
    )


def test_an_item_that_is_no_string_is_refused_by_its_type(tmp_path: Path) -> None:
    items: list[Any] = [{"schema": 1}]
    raises_api(partial(engine(tmp_path / "app").import_usage, items), "got dict as line 1")


def test_each_target_segment_is_read_once_per_import(tmp_path: Path) -> None:
    lines = [log_line(AT + timedelta(seconds=n), event_id=f"e{n}") for n in range(5)]
    with mock.patch.object(api, "event_ids", wraps=usage_log.event_ids) as read:
        engine(tmp_path / "app").import_usage(lines)
    assert read.call_count == 1


def test_a_line_already_there_is_not_validated_again(tmp_path: Path) -> None:
    write_segment(tmp_path / "app", "2026-09", [log_line(AT, event_id="a")])
    stale = log_line(AT, event_id="a", tokens={"retired": 1})  # a rule that changed since the first import
    assert engine(tmp_path / "app").import_usage([stale]) == api.UsageImport(0, 1, {}, {})


def test_a_row_the_reader_skips_reserves_no_id(tmp_path: Path) -> None:
    line = {"schema": 1, "event_id": "e2", "at": iso(AT), "provider": "openai", "model": "gpt-6-sol"}
    usage = json.dumps(line)
    write_segment(tmp_path / "app", "2026-09", [usage])  # no tokens and no cost: a counted skip, never a row
    app = engine(tmp_path / "app")
    result = app.import_usage([log_line(AT, event_id="e2")])
    report = app.report(WHOLE)
    assert (result.imported, result.already_there) == (1, 0), result
    assert len(report.rows) == 1 and len(report.skipped) == 1, report


def test_a_time_relative_to_the_import_is_refused(tmp_path: Path) -> None:
    result = engine(tmp_path / "app").import_usage([_timed("now")])
    assert (
        result.imported == 0 and "needs the time of its call" in result.examples[api.SkipClass.INVALID]
    ), result


def test_a_boolean_time_is_refused_by_the_import(tmp_path: Path) -> None:
    result = engine(tmp_path / "app").import_usage([_timed(True)])
    assert (
        result.imported == 0 and "at is not a timestamp: True" in result.examples[api.SkipClass.INVALID]
    ), result


def test_a_boolean_time_in_a_segment_is_a_counted_skip(tmp_path: Path) -> None:
    write_segment(tmp_path / "app", "2026-09", [_timed(True)])
    report = engine(tmp_path / "app").report(WHOLE)
    assert report.rows == [] and ["at is not a timestamp: True" in s.reason for s in report.skipped] == [True]


def test_a_boolean_time_is_refused_by_record_and_by_log(tmp_path: Path) -> None:
    raises_api(partial(api.Call, "openai", "gpt-6-sol", {"input": 1}, at=_TRUE), "Call.at must be a datetime")
    argv = ["--instance", str(tmp_path / "app"), "log", "--provider", "openai", "--model", "gpt-6-sol"]
    code, _, err = run_cli([*argv, "--input", "1", "--at", "true"])
    assert code == 2 and "--at" in err, err


def test_the_segment_holds_each_imported_call_once(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    app.record(api.Call("openai", "gpt-6-sol", {"input": 1000}, at=AT, keys={"event_id": "a"}))
    stream = [log_line(AT, event_id=name) for name in ("a", "b", "b", "c")]
    first, second = app.import_usage(stream), app.import_usage(stream)
    assert (first.imported, first.already_there, second.imported) == (2, 2, 0)
    assert [line["event_id"] for line in _segment(tmp_path, "2026-09")] == ["a", "b", "c"]


def test_a_read_that_fails_midway_is_a_usage_error_and_a_replay_finds_what_was_written(
    tmp_path: Path,
) -> None:
    line = (log_line(AT, event_id="a") + "\n").encode()
    with mock.patch("sys.stdin", io.TextIOWrapper(io.BufferedReader(_FailsAfter(line)))):
        code, out, err = _import(tmp_path, "-")
    assert (code, out) == (2, "") and "cannot read stdin: Input/output error" in err, err
    assert _from_file(tmp_path, line, "app") == (0, "imported 0\nalready there 1\n", "")


def test_an_instance_config_that_does_not_parse_is_exit_2(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.json").write_text("[1]", encoding="utf-8")
    code, out, err = _import(tmp_path, str(_stream(tmp_path, [log_line(AT, event_id="a")])))
    assert (code, out) == (2, "") and err.startswith("ai-cost: import usage: "), err


def test_a_stream_without_one_usage_line_says_so(tmp_path: Path) -> None:
    lines = ["{'schema': 1, 'at': 'x'}", "GET / 200"]
    code, out, _ = _import(tmp_path, str(_stream(tmp_path, lines)))
    assert (code, out) == (0, "imported 0 — no usage line found in 2 lines\nskipped 2 — not a usage line\n")


def test_an_apps_records_with_their_own_schema_key_are_not_usage_lines(tmp_path: Path) -> None:
    records = ['{"schema": 1, "level": "info", "msg": "started"}', '{"schema": 2, "level": "warn"}']
    code, out, _ = _import(tmp_path, str(_stream(tmp_path, [*records, log_line(AT, event_id="a")])))
    assert (code, out) == (0, "imported 1\nskipped 2 — not a usage line\n")


def test_a_usage_line_with_a_header_key_but_no_event_id_still_fails(tmp_path: Path) -> None:
    code, out, _ = _import(tmp_path, str(_stream(tmp_path, ['{"schema": 1, "provider": "openai"}'])))
    assert (code, out) == (1, "imported 0\nskipped 1 — no event_id: line 1\n")


def test_the_ids_of_a_segment_are_read_without_keeping_its_rows(tmp_path: Path) -> None:
    valid = [log_line(AT + timedelta(seconds=n), event_id=f"e{n}") for n in range(20)]
    skipped = [log_line(AT, event_id=f"bad{n}", tokens={"bogus": 1}) for n in range(5)]
    path = write_segment(tmp_path / "app", "2026-09", [*valid[:10], *skipped, *valid[10:]])
    held: list[int] = []  # what the reader holds as each line arrives: rows, skips and ids of earlier lines
    real = usage_log._Read

    def watched(*args: Any, **keys: Any) -> Any:
        reader: Any = real(*args, **keys)  # its take is wrapped below
        take = reader.take

        def counted(*line: Any) -> None:
            held.append(len(reader.rows) + len(reader.skipped) + len(reader.events.at))
            take(*line)

        reader.take = counted
        return reader

    with mock.patch.object(usage_log, "_Read", side_effect=watched):
        ids = usage_log.event_ids(path)
    assert ids == {f"e{n}" for n in range(20)}
    assert len(held) == 25 and max(held) == 0, held


def test_no_skip_example_repeats_its_class(tmp_path: Path) -> None:
    lines = [
        log_line(AT, event_id="a")[:40],
        log_line(AT, event_id="b", tokens={"bogus": 1}),
        log_line(AT),
        log_line(AT, event_id="c", ref="r" * 5000),
    ]
    result = engine(tmp_path / "app").import_usage(lines)
    assert set(result.examples) == {reason for reason in api.SkipClass if reason.fails}, result
    assert [r for r, example in result.examples.items() if r.value in example] == [], result.examples
