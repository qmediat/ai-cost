"""Instances for the engine's tests: a fixed month boundary, a call per provider family, a machine user's leaky HOME."""

from __future__ import annotations

import inspect
import json
import os
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from unittest import mock

from .. import api
from ..timeutil import iso
from .fixtures import BASE, paths_in, write_claude_session

BOUNDARY = datetime(2026, 11, 1, tzinfo=timezone.utc)  # a Sunday: DeepSeek off-peak either side
WHOLE = api.Period(
    since=BASE - timedelta(days=1), until=BASE + timedelta(days=1)
)  # the fixture session's day


def surface() -> dict[str, Any]:
    """Every public name of ``ai_cost.api`` with its signature as text (methods and properties of its classes)."""
    found: dict[str, Any] = {}
    for name in sorted(api.__all__):
        thing = getattr(api, name)
        entry: dict[str, Any] = {"bases": [base.__name__ for base in thing.__bases__]}
        if issubclass(
            thing, Enum
        ):  # its members are the contract; an enum's signature differs by Python version
            found[name] = {**entry, "members": {member.name: member.value for member in thing}}
            continue
        if not issubclass(thing, BaseException):
            entry["signature"] = str(inspect.signature(thing))
        members = sorted(vars(thing).items())
        entry["methods"] = {
            key: str(inspect.signature(value))
            for key, value in members
            if inspect.isfunction(value) and not key.startswith("_")
        }
        entry["properties"] = [key for key, value in members if isinstance(value, property)]
        found[name] = entry
    return found


def raises_api(call: object, *words: str) -> str:
    """Run ``call``; it must raise ``ApiError`` whose message holds every word."""
    try:
        call()  # type: ignore[operator]
    except api.ApiError as exc:
        message = str(exc)
        assert all(word in message for word in words), message
        return message
    raise AssertionError("no ApiError")


def engine(root: Path, **options: Any) -> api.Engine:
    return api.Engine(api.Instance(root, **options))


def write_segment(root: Path, month: str, lines: list[str]) -> Path:
    path = root / "usage" / f"{month}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def log_line(at: datetime, **keys: Any) -> str:
    line = {"schema": 1, "at": iso(at), "provider": "openai", "model": "gpt-6-sol", "tokens": {"input": 1000}}
    line.update(keys)
    return json.dumps(line)


LEAKY_PRICE = {
    "providers": {"openai": {"models": {"gpt-6-sol": {"input": 999}}}}
}  # a price no instance may see


@contextmanager
def doctor_on(day: datetime = BASE) -> Iterator[None]:
    """The doctor's clock pinned to ``day``: its price-age and ``valid_until`` verdicts never follow today's date."""
    with mock.patch("ai_cost.doctor.now", return_value=day):
        yield


@contextmanager
def leaky_home(tmp: Path, module: str) -> Iterator[None]:
    """A machine user's environment holding what an instance must never read: every variable ``Paths.from_env``
    reads, set to a tree with Claude Code transcripts, a default usage log, a user config naming a plugin, a user
    prices file with another price, the network allowed — and ``AI_COST_PLUGINS`` naming the plugin module."""
    home = tmp / "home"
    write_claude_session(paths_in(home), home / "proj")
    default_log = home / "default-usage.jsonl"
    default_log.write_text(log_line(BASE + timedelta(minutes=5), ref="machine-log") + "\n", encoding="utf-8")
    user = home / ".config" / "ai-cost"
    user.mkdir(parents=True)
    (user / "config.json").write_text(json.dumps({"plugins": [module]}), encoding="utf-8")
    (user / "prices.json").write_text(json.dumps(LEAKY_PRICE), encoding="utf-8")
    env = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
        "GEMINI_CLI_HOME": str(home / ".gemini"),
        "GROK_HOME": str(home / ".grok"),
        "AI_COST_STATE_DIR": str(home / "state"),
        "AI_COST_REPORTS_DIR": str(home / "reports"),
        "AI_COST_CONFIG_DIR": str(user),
        "AI_COST_CONFIG": str(user / "config.json"),
        "AI_COST_PRICES": str(user / "prices.json"),
        "AI_COST_OFFLINE": "0",
        "AI_COST_USAGE_LOG": str(default_log),
        "XDG_DATA_HOME": str(home / "data"),
        "AI_COST_PLUGINS": module,
    }
    with plugin_module(tmp, module), mock.patch.dict(os.environ, env):
        yield


@contextmanager
def plugin_module(tmp: Path, module: str) -> Iterator[None]:
    """A plugin whose one source adds a row named ``plugin-row``: present in a report means the plugin ran."""
    body = f"""
        from ai_cost.models import Billing, Collected, Provider, RowKind, Tokens, UsageRow
        from ai_cost.plugins import Plugin


        class Leak:
            name = "{module}"

            def collect(self, ctx):
                row = UsageRow(Provider.OPENAI, "gpt-6-sol", RowKind.LOG, ctx.window.start, "plugin-row",
                               Billing.API, Tokens(input=1000), source="{module}")
                return Collected(rows=[row])


        PLUGIN = Plugin(name="{module}", sources=(Leak(),))
        """
    (tmp / f"{module}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    sys.path.insert(0, str(tmp))
    try:
        yield
    finally:  # the next test imports from a clean path: neither the folder nor the module stays
        sys.path.remove(str(tmp))
        sys.modules.pop(module, None)
