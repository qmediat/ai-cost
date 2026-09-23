"""``ai-cost log`` and ``ai_cost.log.record``: append one usage-log line (ADR-0005).

The line comes from explicit counters or from a raw API response. Response shapes are recognised by their keys
(a dispatch table): Anthropic ``usage`` (cache fields, or a ``type: message`` response), Google ``usageMetadata``,
DeepSeek ``usage`` (``prompt_cache_hit_tokens``), OpenAI Responses ``usage`` (``input_tokens`` / ``output_tokens``) and
OpenAI Chat Completions ``usage`` (``prompt_tokens``). The two OpenAI shapes are shared by other vendors, so they need
an explicit provider; the Anthropic, Google and DeepSeek shapes name theirs.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PriceBook, usage_log_path
from .errors import UsageError
from .models import Provider
from .timeutil import iso, now
from .values import count, money

SCHEMA = 1


@dataclass(frozen=True)
class Usage:
    """What one request consumed: the provider's own counters, and what it was charged when known."""

    provider: str
    model: str
    tokens: Mapping[str, int] = field(default_factory=dict)
    cost: float | None = None


@dataclass(frozen=True)
class Shape:
    """One recognised response layout."""

    name: str
    provider: str | None  # None: the shape is shared by several vendors, the caller must say which
    detect: Callable[[Mapping[str, Any]], bool]
    extract: Callable[[Mapping[str, Any]], Mapping[str, int]]


def _int(value: Any) -> int:
    """A counter of a raw response: absent or not a number is 0; a number must be a finite whole count."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return count(
        value, "a usage counter"
    )  # a non-finite or fractional number raises: the response is not usable


def _sub(block: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = block.get(key)
    return value if isinstance(value, Mapping) else {}


def _usage(response: Mapping[str, Any]) -> Mapping[str, Any]:
    return _sub(response, "usage")


def _anthropic(response: Mapping[str, Any]) -> Mapping[str, int]:
    usage = _usage(response)
    creation = _sub(usage, "cache_creation")
    counters = {
        "input": _int(usage.get("input_tokens")),
        "output": _int(usage.get("output_tokens")),
        "cache_read": _int(usage.get("cache_read_input_tokens")),
        "cache_write_5m": _int(creation.get("ephemeral_5m_input_tokens")),
        "cache_write_1h": _int(creation.get("ephemeral_1h_input_tokens")),
        "cache_write_unsplit": 0 if creation else _int(usage.get("cache_creation_input_tokens")),
        "web_search": _int(_sub(usage, "server_tool_use").get("web_search_requests")),  # billed per search
    }
    return {key: value for key, value in counters.items() if value}


def _google(response: Mapping[str, Any]) -> Mapping[str, int]:
    meta = _sub(response, "usageMetadata")
    counters = {
        "prompt": _int(meta.get("promptTokenCount")),
        "cached": _int(meta.get("cachedContentTokenCount")),
        "output": _int(meta.get("candidatesTokenCount")) + _int(meta.get("thoughtsTokenCount")),
    }
    return {key: value for key, value in counters.items() if value}


def _openai_responses(response: Mapping[str, Any]) -> Mapping[str, int]:
    usage = _usage(response)
    counters = {
        "input": _int(usage.get("input_tokens")),
        "cached_input": _int(_sub(usage, "input_tokens_details").get("cached_tokens")),
        "output": _int(usage.get("output_tokens")),
    }
    return {key: value for key, value in counters.items() if value}


def _deepseek(response: Mapping[str, Any]) -> Mapping[str, int]:
    usage = _usage(response)
    counters = {
        "cache_hit": _int(usage.get("prompt_cache_hit_tokens")),
        "cache_miss": _int(usage.get("prompt_cache_miss_tokens")),
        "output": _int(usage.get("completion_tokens")),
    }
    return {key: value for key, value in counters.items() if value}


def _openai_chat(response: Mapping[str, Any]) -> Mapping[str, int]:
    usage = _usage(response)
    counters = {
        "input": _int(usage.get("prompt_tokens")),
        "cached_input": _int(_sub(usage, "prompt_tokens_details").get("cached_tokens")),
        "output": _int(usage.get("completion_tokens")),
    }
    return {key: value for key, value in counters.items() if value}


_ANTHROPIC_KEYS = ("cache_read_input_tokens", "cache_creation_input_tokens", "cache_creation")
_DEEPSEEK_KEYS = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens")


def _is_anthropic(response: Mapping[str, Any]) -> bool:
    """A Messages API response says ``type: message``; a bare usage block is known by its cache counters."""
    return response.get("type") == "message" or any(k in _usage(response) for k in _ANTHROPIC_KEYS)


SHAPES: tuple[Shape, ...] = (
    Shape("anthropic", "anthropic", _is_anthropic, _anthropic),
    Shape("google", "google", lambda r: isinstance(r.get("usageMetadata"), Mapping), _google),
    Shape("deepseek", "deepseek", lambda r: any(k in _usage(r) for k in _DEEPSEEK_KEYS), _deepseek),
    Shape(
        "openai-responses",
        None,
        lambda r: "input_tokens" in _usage(r) and "output_tokens" in _usage(r),
        _openai_responses,
    ),
    Shape("openai-chat", None, lambda r: "prompt_tokens" in _usage(r), _openai_chat),
)


def from_response(response: Any, provider: str | None, model: str | None = None) -> Usage:
    """The usage of a raw API response; ``provider`` is required for the shapes several vendors share."""
    if not isinstance(response, Mapping):
        raise UsageError("--from-response: the response is not a JSON object")
    shape = next((s for s in SHAPES if s.detect(response)), None)
    if shape is None:
        raise UsageError(
            "--from-response: no usage block of a known shape (Anthropic, Google, OpenAI chat/responses)"
        )
    chosen = provider or shape.provider
    if chosen is None:
        raise UsageError(
            f"--from-response: the {shape.name} shape is shared by several vendors — pass --provider "
            "(a bare Anthropic usage block without cache counters needs --provider anthropic)"
        )
    named = model or response.get("model") or response.get("modelVersion")  # Google names it modelVersion
    if not isinstance(named, str) or not named:
        raise UsageError("--from-response: the response names no model — pass --model")
    tokens = shape.extract(response)
    if not tokens:
        raise UsageError("--from-response: the usage block has no counters")
    return Usage(chosen, named, tokens)


@dataclass(frozen=True)
class Attribution:
    """The optional keys of a log line (ADR-0003 scope, identity)."""

    ref: str = ""
    session: str = ""
    branch: str = ""
    pr: str = ""
    tags: Sequence[str] = ()
    source: str = ""
    event_id: str = ""
    billing: str = ""

    def __post_init__(self) -> None:
        """The keys as the reader will read them: tags a sequence of strings (never one string), billing known."""
        tags = () if self.tags is None else self.tags  # None is absent, like every other optional key
        try:
            listed = tuple(tags)
        except TypeError as exc:
            raise ValueError("tags must be a list of strings") from exc
        if isinstance(tags, str) or not all(isinstance(tag, str) for tag in listed):
            raise ValueError("tags must be a list of strings, not a string")
        if self.billing not in ("", "api", "subscription"):
            raise ValueError(f"billing must be api or subscription, got {self.billing!r}")
        for key in ("ref", "session", "branch", "pr", "source", "event_id"):  # the reader takes strings only
            if not isinstance(getattr(self, key), str):
                raise ValueError(f"{key} must be a string, got {type(getattr(self, key)).__name__}")
        object.__setattr__(self, "tags", listed)


def entry(usage: Usage, at: datetime | None = None, meta: Attribution | None = None) -> dict[str, Any]:
    """One log line as a dict: required keys first, only the optional keys that carry a value."""
    meta = meta or Attribution()
    line: dict[str, Any] = {
        "schema": SCHEMA,
        "at": iso(at or now()),
        "provider": usage.provider,
        "model": usage.model,
    }
    if usage.tokens:
        line["tokens"] = dict(usage.tokens)
    if usage.cost is not None:
        line["cost"] = usage.cost
    optional = {
        "billing": meta.billing,
        "ref": meta.ref,
        "session": meta.session,
        "branch": meta.branch,
        "pr": meta.pr,
        "source": meta.source,
        "event_id": meta.event_id,
    }
    line.update({key: value for key, value in optional.items() if value})
    if meta.tags:
        line["tags"] = list(meta.tags)
    return line


def default_path(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """``AI_COST_USAGE_LOG``, else ``$XDG_DATA_HOME/ai-cost/usage.jsonl`` (``~/.local/share`` by default)."""
    return usage_log_path(os.environ if environ is None else environ, home or Path.home())


def append(path: Path, line: Mapping[str, Any]) -> None:
    """Append one line; the directory is created, the write is a single call (atomic for one short line on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


from .collectors.usage_log import (  # noqa: E402 — the reader's counter set, one definition
    ALIASES,
    LOG_COUNTERS,
)
from .pricing import as_tier_family, counters_of, folds_to_tiers  # noqa: E402


def _well_formed(usage: Usage) -> None:
    """Tokens an object, provider and model non-empty strings, every counter one the reader knows."""
    if not isinstance(usage.tokens, Mapping):
        raise ValueError(f"tokens must be an object of counters, got {type(usage.tokens).__name__}")
    for key, value in (("provider", usage.provider), ("model", usage.model)):
        if (
            not isinstance(value, str) or not value
        ):  # the reader takes only strings: the writer must not write more
            raise ValueError(f"{key} must be a non-empty string, got {value!r}")
    unknown = sorted(
        str(key) for key in set(usage.tokens) - set(LOG_COUNTERS)
    )  # a numeric key is named, not a TypeError
    if unknown:
        raise ValueError(f"unknown counter(s) {', '.join(unknown)}: the reader would ignore them")


def _amounts(usage: Usage) -> tuple[dict[str, int], float | None]:
    """The counters and the cost as the reader's own rules take them: whole, bounded, finite, non-negative."""
    try:
        counters = {name: count(value, name) for name, value in usage.tokens.items()}
        amount = money(usage.cost, "cost")
    except (TypeError, ValueError) as exc:  # a boolean, a fraction, a negative, a string, an infinity
        raise ValueError(str(exc)) from exc
    if not any(counters.values()) and amount is None:
        raise ValueError("give at least one positive counter or a cost")
    if amount is not None and amount < 0:
        raise ValueError(f"cost must be non-negative, got {usage.cost!r}")
    return counters, amount


def _in_the_models_family(usage: Usage, counters: dict[str, int], book: PriceBook | None) -> dict[str, int]:
    """A DeepSeek response's cache counters for a model the user prices by token tiers: input = hit + miss, cached = hit.

    The API always answers with ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``; a tiers-priced model reads
    the OpenAI family, so the same usage is expressed in it instead of being refused (``pricing.as_tier_family``, the
    reader's rule too; both spellings on one line are refused there).
    """
    provider = Provider.of(usage.provider)
    entry = book.entry(provider, usage.model) if book is not None else None
    return as_tier_family(counters) if folds_to_tiers(provider, entry) else counters


def _priced(usage: Usage, counters: Mapping[str, int], book: PriceBook | None) -> None:
    """Every counter one the row's pricing reads; with a pricebook, an unlisted model needs a cost."""
    provider = Provider.of(usage.provider)  # raises for an id no pricebook could list
    entry = book.entry(provider, usage.model) if book is not None else None
    consumed = counters_of(provider, entry)  # by the model's own price shape when the book knows it
    stray = sorted({ALIASES.get(name, name) for name in counters} - consumed) if consumed is not None else []
    if stray:
        reads = ", ".join(sorted(consumed or ())) or "no token counter (give a cost)"
        raise ValueError(f"{usage.provider} prices {reads}: {', '.join(stray)} would not be priced")
    if book is not None and usage.cost is None and entry is None:
        raise ValueError(
            f"{usage.provider}/{usage.model} has no list price: give a cost, or add the model to the prices file"
        )


def validated(usage: Usage, book: PriceBook | None = None) -> Usage:
    """The usage as the reader will accept it, or a ``ValueError`` saying why the line would be skipped or unpriced.

    Counters are non-negative and at least one is positive unless a cost is given; a cost is finite and non-negative;
    the provider is a well-formed id and every counter is one its formula reads; with a pricebook, a model it does not
    list needs a cost.
    """
    _well_formed(usage)
    counters, amount = _amounts(usage)
    counters = _in_the_models_family(usage, counters, book)
    _priced(usage, counters, book)
    return Usage(usage.provider, usage.model, counters, amount)  # the normalised counters and amount


def record(
    provider: str,
    model: str,
    tokens: Mapping[str, int] | None = None,
    cost: float | None = None,
    path: Path | None = None,
    book: PriceBook | None = None,
    **keys: Any,
) -> dict[str, Any]:
    """In-process helper: ``record("openai", "gpt-5.5", {"input": 12, "output": 3}, ref="job-42")``.

    The line is validated like ``ai-cost log`` does (``ValueError`` names the problem), the optional keys included;
    pass ``book`` to refuse a model the pricebook does not list unless a cost is given.
    """
    if tokens is not None and not isinstance(tokens, Mapping):
        raise ValueError(f"tokens must be an object of counters, got {type(tokens).__name__}")
    usage = validated(Usage(provider, model, dict(tokens or {}), cost), book)
    unknown = sorted(set(keys) - {f.name for f in fields(Attribution)})
    if (
        unknown
    ):  # a typo or a key the line has no place for: said as the docstring promises, never a TypeError
        raise ValueError(
            f"unknown key(s) {', '.join(unknown)}: the optional keys are {', '.join(f.name for f in fields(Attribution))}"
        )
    line = entry(usage, meta=Attribution(**keys))  # the values are checked by Attribution itself
    append(path or default_path(), line)
    return line
