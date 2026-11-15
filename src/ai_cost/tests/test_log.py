"""``ai-cost log``: response shapes → provider-native counters, the line layout, the default path, appending."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from ..collectors.usage_log import collect_usage_log
from ..errors import UsageError
from ..log import (
    MAX_LINE_BYTES,
    Attribution,
    Usage,
    append,
    default_path,
    encoded,
    entry,
    from_response,
    record,
)
from .fixtures import BASE

ANTHROPIC = {
    "model": "claude-sonnet-5",
    "usage": {
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 7,
    },
}
GOOGLE = {
    "modelVersion": "gemini-3.8-flash",
    "usageMetadata": {
        "promptTokenCount": 50,
        "cachedContentTokenCount": 20,
        "candidatesTokenCount": 5,
        "thoughtsTokenCount": 9,
    },
}
OPENAI_CHAT = {
    "model": "gpt-5.5",
    "usage": {"prompt_tokens": 30, "completion_tokens": 6, "prompt_tokens_details": {"cached_tokens": 12}},
}
OPENAI_RESPONSES = {
    "model": "gpt-5.5",
    "usage": {"input_tokens": 31, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 13}},
}
DEEPSEEK = {  # the chat-completions shape plus DeepSeek's own cache counters
    "model": "deepseek-chat",
    "usage": {
        "prompt_tokens": 19,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 4},
        "prompt_cache_hit_tokens": 4,
        "prompt_cache_miss_tokens": 15,
    },
}
ANTHROPIC_PLAIN = {
    "type": "message",
    "model": "claude-sonnet-5",
    "usage": {"input_tokens": 10, "output_tokens": 4},
}


def test_the_response_shapes_map_to_their_providers_counters() -> None:
    anthropic = from_response(ANTHROPIC, None)
    assert anthropic.provider == "anthropic" and anthropic.model == "claude-sonnet-5"
    assert anthropic.tokens == {"input": 10, "output": 4, "cache_read": 100, "cache_write_unsplit": 7}
    plain = from_response(ANTHROPIC_PLAIN, None)  # no cache counters: the response's type says who it is
    assert plain.provider == "anthropic" and plain.tokens == {"input": 10, "output": 4}
    searched = {
        "type": "message",
        "model": "claude-sonnet-5",
        "usage": {"input_tokens": 1, "output_tokens": 1, "server_tool_use": {"web_search_requests": 3}},
    }
    assert from_response(searched, None).tokens == {"input": 1, "output": 1, "web_search": 3}
    google = from_response(GOOGLE, None)
    assert google.provider == "google" and google.model == "gemini-3.8-flash", "Google names it modelVersion"
    assert google.tokens == {"prompt": 50, "cached": 20, "output": 14}
    chat = from_response(OPENAI_CHAT, "openai")
    assert chat.tokens == {"input": 30, "cached_input": 12, "output": 6}
    responses = from_response(OPENAI_RESPONSES, "xai")
    assert responses.provider == "xai" and responses.tokens == {"input": 31, "cached_input": 13, "output": 7}
    deepseek = from_response(DEEPSEEK, None)
    assert deepseek.provider == "deepseek" and deepseek.tokens == {
        "cache_hit": 4,
        "cache_miss": 15,
        "output": 10,
    }


def test_the_writer_refuses_a_counter_the_provider_would_not_price() -> None:
    from ..log import validated

    try:
        validated(Usage("deepseek", "deepseek-chat", {"input": 31, "output": 7}))
    except ValueError as exc:
        assert "would not be priced" in str(exc) and "input" in str(exc), str(exc)
    else:
        raise AssertionError(
            "deepseek prices cache_hit/cache_miss, an input counter would be charged at zero"
        )
    google = validated(Usage("google", "gemini-3.8-flash", {"prompt": 5, "thoughts": 2}))
    assert google.tokens == {"prompt": 5, "thoughts": 2}, "an alias of a priced counter is fine"
    for bad in (Usage("openai", 5, {"input": 1}), Usage(7, "gpt-5.5", {"input": 1}), Usage("openai", "", {"input": 1})):  # type: ignore[arg-type]
        try:
            validated(bad)
        except ValueError as exc:
            assert "must be a non-empty string" in str(exc), str(exc)
        else:
            raise AssertionError(
                "a non-string or empty id must be a ValueError: the reader would skip the line"
            )
    try:
        validated(Usage("openai", "gpt-5.5", ["input", 3]))  # type: ignore[arg-type]
    except ValueError as exc:
        assert "tokens must be an object" in str(exc)
    else:
        raise AssertionError("tokens that are not a mapping are a ValueError, never a TypeError")


def test_shared_shapes_need_a_provider_and_unknown_shapes_are_usage_errors() -> None:
    for payload, provider, word in (
        (OPENAI_CHAT, None, "--provider"),
        ({"usageMetadata": GOOGLE["usageMetadata"]}, None, "--model"),
        ({"usage": {"weird": 1}}, "openai", "known shape"),
        ([1, 2], "openai", "not a JSON object"),
        ({"model": "m", "usage": {"prompt_tokens": 0, "completion_tokens": 0}}, "openai", "no counters"),
    ):
        try:
            from_response(payload, provider)
        except UsageError as exc:
            assert word in str(exc), (word, str(exc))
        else:
            raise AssertionError(f"expected a UsageError mentioning {word}")


def test_a_line_carries_required_keys_first_and_only_the_optional_keys_with_values() -> None:
    meta = Attribution(ref="job-42", branch="feat/x", tags=("ci", "nightly"), billing="api")
    line = entry(Usage("openai", "gpt-5.5", {"input": 3}, 0.01), at=BASE, meta=meta)
    assert list(line)[:4] == ["schema", "at", "provider", "model"]
    assert line["tokens"] == {"input": 3} and line["cost"] == 0.01 and line["tags"] == ["ci", "nightly"]
    assert "pr" not in line and "session" not in line and line["billing"] == "api"
    assert "tokens" not in entry(Usage("openai", "gpt-5.5", cost=0.5), at=BASE)


def test_default_path_honours_the_environment(tmp_path: Path) -> None:
    assert default_path({"AI_COST_USAGE_LOG": "~/x.jsonl"}, tmp_path) == Path("~/x.jsonl").expanduser()
    assert (
        default_path({"XDG_DATA_HOME": str(tmp_path / "xdg")}, tmp_path)
        == tmp_path / "xdg" / "ai-cost" / "usage.jsonl"
    )
    assert default_path({}, tmp_path) == tmp_path / ".local" / "share" / "ai-cost" / "usage.jsonl"


def test_record_appends_lines_the_reader_prices(tmp_path: Path) -> None:
    log = tmp_path / "deep" / "usage.jsonl"
    record("openai", "gpt-5.5", {"input": 1200, "output": 300}, path=log, ref="job-1")
    append(log, entry(from_response(ANTHROPIC, None), meta=Attribution(source="my-app")))
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [line["provider"] for line in lines] == ["openai", "anthropic"] and lines[1]["source"] == "my-app"
    collected, warnings = collect_usage_log([log], None)
    assert len(collected.rows) == 2 and collected.skipped == [] and warnings == []


def test_record_names_an_unknown_key_as_a_value_error(tmp_path: Path) -> None:
    try:
        record("openai", "gpt-5.5", {"input": 1}, path=tmp_path / "u.jsonl", tagz=["ci"])
    except ValueError as exc:
        assert "unknown key(s) tagz" in str(exc) and "tags" in str(exc), str(exc)
    else:
        raise AssertionError("an unknown optional key must be a ValueError, never a TypeError")
    assert not (tmp_path / "u.jsonl").exists(), "nothing was written"


def test_record_refuses_tokens_that_are_not_an_object(tmp_path: Path) -> None:
    try:
        record("openai", "gpt-5.5", ["input", 3], path=tmp_path / "u.jsonl")  # type: ignore[arg-type]
    except ValueError as exc:
        assert "tokens must be an object" in str(exc), str(exc)
    else:
        raise AssertionError("a list of tokens must be a ValueError, never a TypeError")


def test_record_refuses_a_non_string_optional_key(tmp_path: Path) -> None:
    from typing import Any

    for key, value in (("event_id", 5), ("ref", ["a"]), ("pr", 12)):
        keys: dict[str, Any] = {key: value}
        try:
            record("openai", "gpt-5.5", {"input": 1}, path=tmp_path / "u.jsonl", **keys)
        except ValueError as exc:
            assert f"{key} must be a string" in str(exc), str(exc)
        else:
            raise AssertionError(f"{key}={value!r} must be a ValueError: the reader would skip the line")
    assert not (tmp_path / "u.jsonl").exists()


def test_a_tilde_in_the_data_home_variable_is_the_users_home(tmp_path: Path) -> None:
    import os
    from unittest import mock

    from ..config import usage_log_path

    with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
        assert (
            usage_log_path({"XDG_DATA_HOME": "~/data"}, tmp_path)
            == tmp_path / "data" / "ai-cost" / "usage.jsonl"
        )
        assert usage_log_path({"AI_COST_USAGE_LOG": "~/u.jsonl"}, tmp_path) == tmp_path / "u.jsonl"


def test_a_numeric_counter_key_is_named_and_a_tiers_priced_deepseek_model_reads_the_openai_family(
    tmp_path: Path,
) -> None:
    from ..config import load_pricebook
    from ..log import validated
    from .fixtures import paths_in

    try:
        validated(Usage("openai", "gpt-5.5", {1: 3}))  # type: ignore[dict-item]
    except ValueError as exc:
        assert "unknown counter(s) 1" in str(exc), str(exc)
    else:
        raise AssertionError("a numeric key must be a ValueError naming it, never a TypeError from join")
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
    usage = validated(
        Usage("deepseek", "deepseek-tiers", {"cache_hit": 4, "cache_miss": 15, "output": 10}), book
    )
    assert usage.tokens == {"output": 10, "input": 19, "cached_input": 4}, usage.tokens
    native = validated(
        Usage("deepseek", "deepseek-flash", {"cache_hit": 4, "cache_miss": 15, "output": 10}), book
    )
    assert native.tokens == {
        "cache_hit": 4,
        "cache_miss": 15,
        "output": 10,
    }, "a peak/off-peak model keeps its own"


def test_both_spellings_of_deepseek_cache_usage_on_one_line_are_refused(tmp_path: Path) -> None:
    """input/cached_input and cache_hit/cache_miss describe the same usage: keeping one would drop the other's tokens."""
    from ..config import load_pricebook
    from ..log import validated
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
    mixed = Usage(
        "deepseek", "deepseek-tiers", {"input": 1000, "cache_hit": 100, "cache_miss": 900, "output": 1}
    )
    try:
        validated(mixed, book)
    except ValueError as exc:
        assert "two spellings" in str(exc), str(exc)
    else:
        raise AssertionError(
            "a line with both spellings must be refused, never folded over the writer's input"
        )


def test_append_writes_one_line_in_one_write_call(tmp_path: Path) -> None:
    line = entry(Usage("openai", "gpt-6-sol", {"input": 5}), BASE)
    with mock.patch("os.write", wraps=os.write) as write:
        append(tmp_path / "deep" / "usage.jsonl", line)
        append(tmp_path / "deep" / "usage.jsonl", line)
    assert write.call_count == 2, "one write per line: a buffered write may split a line between writers"
    assert (tmp_path / "deep" / "usage.jsonl").read_text("utf-8") == (json.dumps(line) + "\n") * 2


def test_append_with_a_limit_refuses_a_line_that_leaves_no_byte_for_a_newline(tmp_path: Path) -> None:
    def line(size: int) -> dict[str, object]:
        filler = {"ref": ""}
        fixed = len((json.dumps(filler) + "\n").encode("utf-8"))
        return {
            "ref": "é" * ((size - fixed) // 2) + "x" * ((size - fixed) % 2)
        }  # é: two bytes, one character

    append(tmp_path / "usage.jsonl", line(MAX_LINE_BYTES - 1), MAX_LINE_BYTES)
    try:
        append(tmp_path / "usage.jsonl", line(MAX_LINE_BYTES), MAX_LINE_BYTES)
    except ValueError as exc:
        assert "4096 bytes" in str(exc), exc
    else:
        raise AssertionError("a 4096-byte line was written")
    written = (tmp_path / "usage.jsonl").read_bytes()
    assert (
        len(written) == MAX_LINE_BYTES - 1
    ), "the encoded size counts, not the characters; the long line wrote nothing"


def test_labels_are_written_only_when_given_and_checked_like_the_reader() -> None:
    assert "labels" not in entry(Usage("openai", "gpt-6-sol", {"input": 5}), BASE)
    labelled = entry(Usage("openai", "gpt-6-sol", {"input": 5}), BASE, Attribution(labels={"tenant": "acme"}))
    assert labelled["labels"] == {"tenant": "acme"}
    for bad in ({"a b": "x"}, {"tenant": ""}, {"tenant": 5}, {"t": "x" * 257}):
        try:
            Attribution(labels=bad)  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"labels {bad!r} accepted")


def _line(tokens: int) -> dict[str, object]:
    return entry(Usage("openai", "gpt-6-sol", {"input": tokens}), BASE)


def _read(log: Path) -> tuple[list[int], int]:
    collected, _ = collect_usage_log([log], None)
    return [row.tokens.input for row in collected.rows], len(collected.skipped)


def test_a_line_after_a_torn_tail_starts_a_line_of_its_own(tmp_path: Path) -> None:
    log = tmp_path / "usage.jsonl"
    log.write_bytes(b'{"schema": 1, "at": "2026-09-19T15:')  # another writer's line, cut short
    append(log, _line(7))
    assert _read(log) == ([7], 1), "the fragment is one counted skip, the new line a row"


def test_two_lines_after_a_torn_tail_are_two_rows_and_a_blank_line_is_no_skip(tmp_path: Path) -> None:
    log = tmp_path / "usage.jsonl"
    log.write_bytes(b'{"schema": 1, "at": "2026-09-19T15:')
    append(log, _line(7))
    append(log, _line(8))
    with log.open(
        "a", encoding="utf-8"
    ) as handle:  # two writers that both saw a torn tail leave a blank line
        handle.write("\n")
    append(log, _line(9))
    assert _read(log) == ([7, 8, 9], 1)


def test_an_empty_a_missing_and_a_crlf_ended_file_get_no_newline_first(tmp_path: Path) -> None:
    empty, missing, crlf = tmp_path / "empty.jsonl", tmp_path / "missing.jsonl", tmp_path / "crlf.jsonl"
    empty.write_bytes(b"")
    crlf.write_bytes(json.dumps(_line(1)).encode() + b"\r\n")
    for log in (empty, missing, crlf):
        append(log, _line(7))
    assert empty.read_bytes() == missing.read_bytes() == json.dumps(_line(7)).encode() + b"\n"
    assert crlf.read_bytes().endswith(b"\r\n" + json.dumps(_line(7)).encode() + b"\n")


def test_a_short_write_raises_and_writes_nothing_more(tmp_path: Path) -> None:
    real = os.write
    with mock.patch("os.write", side_effect=lambda fd, data: real(fd, data[:10])) as write:
        try:
            append(tmp_path / "usage.jsonl", _line(7))
        except OSError as exc:
            assert "a fragment may be left; the next line starts on a new line" in str(exc), exc
        else:
            raise AssertionError("a short write was not said")
    assert (
        write.call_count == 1
        and (tmp_path / "usage.jsonl").read_bytes() == json.dumps(_line(7)).encode()[:10]
    )


def test_a_line_within_the_limit_is_written_whatever_the_tail_is(tmp_path: Path) -> None:
    log = tmp_path / "usage.jsonl"
    log.write_bytes(b"torn")
    line = {"ref": "x" * (MAX_LINE_BYTES - len(json.dumps({"ref": ""}).encode()) - 2)}
    assert len(encoded(line)) == MAX_LINE_BYTES - 1, "the longest line the limit takes"
    append(log, line, MAX_LINE_BYTES)
    assert log.read_bytes() == b"torn\n" + encoded(line), "one write of exactly the limit"


def test_a_log_this_writer_cannot_read_is_appended_to_without_the_check(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        return  # root reads every file: the refusal this test needs cannot happen
    log = tmp_path / "usage.jsonl"
    log.write_bytes(b'{"torn"')
    log.chmod(0o200)
    try:
        append(log, _line(7))
    finally:
        log.chmod(0o600)
    assert (
        log.read_bytes() == b'{"torn"' + json.dumps(_line(7)).encode() + b"\n"
    ), "no check: no newline first"


def test_a_pipe_delivers_the_whole_line_to_a_reader_that_opens_later(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    received: list[bytes] = []

    def read_later() -> None:
        time.sleep(0.3)
        with open(fifo, "rb") as stream:
            received.append(stream.read())

    reader = threading.Thread(target=read_later, daemon=True)
    reader.start()
    append(fifo, _line(7))  # waits for the reader: a write-only open of a pipe
    reader.join(5)
    assert received == [json.dumps(_line(7)).encode() + b"\n"]


def test_a_device_is_appended_to_without_the_check() -> None:
    append(Path(os.devnull), _line(7))


def _swapping_open(log: Path, swap: Callable[[], None]) -> Callable[..., int]:
    """``os.open`` that runs ``swap`` once, just before the tail check's (read-only) open of ``log``."""
    real_open, state = os.open, {"swapped": False}

    def opener(path: object, flags: int, *more: int, **named: int) -> int:
        if not flags & os.O_APPEND and not state["swapped"]:
            state["swapped"] = True
            swap()
        return real_open(path, flags, *more, **named)  # type: ignore[arg-type]

    return opener


def test_a_log_rotated_between_the_open_and_the_check_gets_the_line_in_the_new_file(tmp_path: Path) -> None:
    log, moved = tmp_path / "usage.jsonl", tmp_path / "moved.jsonl"
    log.write_bytes(b'{"torn"')

    def rotate() -> None:
        os.replace(log, moved)
        log.write_bytes(b'{"also torn"')

    with mock.patch("os.open", _swapping_open(log, rotate)):
        append(log, _line(7))
    assert moved.read_bytes() == b'{"torn"'
    assert log.read_bytes() == b'{"also torn"\n' + json.dumps(_line(7)).encode() + b"\n"


def test_a_pipe_put_under_the_path_after_the_open_gets_the_line_when_its_reader_opens(tmp_path: Path) -> None:
    log, moved = tmp_path / "usage.jsonl", tmp_path / "moved.jsonl"
    log.write_bytes(b'{"torn"')
    received: list[bytes] = []
    reader = threading.Thread(target=lambda: received.append(log.read_bytes()), daemon=True)

    def to_a_pipe() -> None:
        os.replace(log, moved)
        os.mkfifo(log)
        reader.start()

    def run() -> None:
        with mock.patch("os.open", _swapping_open(log, to_a_pipe)):
            append(log, _line(7))

    writer = threading.Thread(target=run, daemon=True)
    writer.start()
    writer.join(5)
    assert not writer.is_alive(), "the append waited at the tail check's open"
    reader.join(5)
    assert moved.read_bytes() == b'{"torn"'
    assert received == [json.dumps(_line(7)).encode() + b"\n"]
