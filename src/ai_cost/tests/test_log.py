"""``ai-cost log``: response shapes → provider-native counters, the line layout, the default path, appending."""

from __future__ import annotations

import json
from pathlib import Path

from ..collectors.usage_log import collect_usage_log
from ..errors import UsageError
from ..log import Attribution, Usage, append, default_path, entry, from_response, record
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
