"""Codex CLI rollouts: one row per (model, UTC day) of every session with turns inside the window.

Usage is summed per ``token_count`` event whose timestamp falls inside the window, so a session resumed from before
the window or running past it contributes only the turns in between. Rollout files sit in the directory of their
creation day; a file is parsed only when its mtime is not older than the window start.

Billing evidence the file itself carries: a ``token_count`` event names the ChatGPT plan the session ran on
(``payload.rate_limits.plan_type``, ``team``/``plus``; null when no plan was involved — verified on the CLI's own
rollouts, 2026-09-20). A turn that names a plan is a plan turn — a session may switch to the API key half-way (an exhausted plan), so billing
is per turn; the other turns take the provider's configured
billing and stay ``UNKNOWN`` without it. What an API key was actually charged is not in the rollout: a plugin that
knows (a ledger, a proxy log) settles the rows afterwards and splits a session cost by each row's ``share``.
The header names the working directory the session ran in (``session_meta.payload.cwd``, verified 2026-09-21): it
is the row's workspace, so ``--project`` and ``--attribute`` place a rollout by directory (ADR-0006).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models import Billing, Collected, Provider, RowKind, Scope, Skipped, Tokens, UsageRow, Window
from ..timeutil import parse_ts
from ..values import as_object, count

if TYPE_CHECKING:
    from ..plugins import Context

SOURCE_NAME = (
    "codex-rollouts"  # the one name of this source: on every row, on the Source, and what the extras key on
)
_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")
_WEIGHT_KEYS = ("input_tokens", "output_tokens")  # cached input is a subset of input: it must not weigh twice


def _sum(usage: Any, into: dict[str, int]) -> None:
    if not isinstance(usage, Mapping):
        raise TypeError(f"token usage must be an object, got {type(usage).__name__}")
    for key in _USAGE_KEYS:
        into[key] += count(usage.get(key), key)


@dataclass
class _Rollout:
    """What one rollout file says before windowing."""

    session: str = ""
    branch: str = ""
    cwd: str = (
        ""  # the working directory of the session (session_meta.payload.cwd); empty when the header lacks it
    )
    model: str = ""  # the model of the CURRENT turn while reading; a session may switch models
    plan: str = ""  # the ChatGPT plan named by any token_count event; empty when none was
    first_ts: str = ""
    total: Mapping[str, Any] | None = None
    turns_seen: bool = (
        False  # a per-turn event was there, valid or not: the cumulative total is never the fallback
    )
    events: list[tuple[datetime, Mapping[str, Any], str, bool]] = field(
        default_factory=list
    )  # (stamp, usage, model, on a plan)


def _read_rollout(path: Path, skipped: list[Skipped]) -> _Rollout:
    """The session id, model, plan, start and every ``token_count`` event of a rollout; a bad line or event is counted."""
    rollout = _Rollout()
    with path.open(errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            if not any(marker in line for marker in ('"session_meta"', '"turn_context"', '"token_count"')):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                skipped.append(Skipped("codex", str(path), f"line {line_no}: not JSON"))
                continue
            if not isinstance(record, Mapping):
                skipped.append(Skipped("codex", str(path), f"line {line_no}: not an object"))
                continue
            try:
                _absorb(rollout, record)
            except (TypeError, ValueError) as exc:  # one bad event is one counted skip; the rest still counts
                skipped.append(Skipped("codex", str(path), f"line {line_no}: {exc}"))
    return rollout


def _absorb(rollout: _Rollout, record: Mapping[str, Any]) -> None:
    payload = as_object(record.get("payload"), "payload")
    kind = record.get("type")
    if kind == "session_meta":
        rollout.session = str(payload.get("session_id") or payload.get("id") or "")
        rollout.first_ts = str(record.get("timestamp") or "")
        git = payload.get("git")  # metadata only: a wrong shape costs the branch, never the usage
        rollout.branch = str(git.get("branch") or "") if isinstance(git, Mapping) else ""
        cwd = payload.get("cwd")  # metadata as well: anything but text costs the workspace, never the usage
        rollout.cwd = cwd if isinstance(cwd, str) else ""
    elif kind == "turn_context":
        rollout.model = str(payload.get("model") or rollout.model)
    elif payload.get("type") == "token_count":
        _absorb_token_count(rollout, record, payload)


def _absorb_token_count(rollout: _Rollout, record: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    """One ``token_count`` event, whole or not at all: a bad counter or a lost timestamp leaves the rollout as it was.

    Either is a counted skip in the caller; the turn still marks the rollout as per-turn, because falling back to
    the cumulative total would place its usage at the session start.
    """
    info = as_object(payload.get("info"), "info")
    last: Mapping[str, Any] | None = None
    if info.get("last_token_usage") is not None:  # present, whatever its shape: this event is a turn
        rollout.turns_seen = (
            True  # even a rejected turn proves the rollout is per-turn: no cumulative fallback
        )
        last = as_object(
            info["last_token_usage"], "last_token_usage"
        )  # 0, "" or [] raise here: a counted skip
        _turn_usage(last)  # a counter that is not a count raises here, before anything of the event is kept
    if info.get("total_token_usage"):
        rollout.total = as_object(info["total_token_usage"], "total_token_usage")
    stamp = parse_ts(record.get("timestamp"))
    if last is not None and stamp is None:  # a turn nobody can place in time: a counted skip, never silent
        raise ValueError(f"timestamp missing or not a time: {record.get('timestamp')!r}")
    plan = _plan_of(payload)
    if last is not None and stamp:
        rollout.events.append((stamp, last, rollout.model, bool(plan)))  # this turn's own evidence
    if plan:
        rollout.plan = plan  # any plan seen: the rule for an old rollout that only has a cumulative total


def _plan_of(payload: Mapping[str, Any]) -> str:
    """The ChatGPT plan a ``token_count`` event names (``rate_limits.plan_type``), ``""`` when none or malformed."""
    limits = payload.get(
        "rate_limits"
    )  # metadata only: a wrong shape costs the plan evidence, never the usage
    plan = limits.get("plan_type") if isinstance(limits, Mapping) else None
    return plan if isinstance(plan, str) else ""


@dataclass
class _Bucket:
    """One (model, UTC day) of a session inside the window: first stamp, summed usage, non-empty turns."""

    first_in: datetime
    usage: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_USAGE_KEYS, 0))
    turns: int = 0


def _turn_usage(last: Mapping[str, Any]) -> dict[str, int] | None:
    """A turn's counters as ints (validated); ``None`` for a turn that reports no tokens at all."""
    usage = dict.fromkeys(_USAGE_KEYS, 0)
    _sum(last, usage)
    return usage if any(usage.values()) else None


def _usage_in_window(rollout: _Rollout, path: Path, window: Window) -> dict[tuple[str, str, bool], _Bucket]:
    """Per (model, UTC day): first stamp inside the window, the summed per-turn usage and the turn count.

    A day boundary keeps a price change (``next`` blocks) from straddling one row. A turn that reports no tokens is
    nothing: it makes no bucket and counts in no divisor. Old rollouts without per-turn events count whole, under
    their last model, by their start; a rollout whose every turn was rejected counts nothing (its skips say so) —
    its cumulative total would place out-of-window usage at the session start.
    """
    buckets: dict[tuple[str, str, bool], _Bucket] = {}
    if rollout.events:
        for stamp, last, model, on_plan in rollout.events:
            usage = _turn_usage(last)
            if usage is None or not window.contains(stamp):
                continue
            bucket = buckets.setdefault((model, stamp.date().isoformat(), on_plan), _Bucket(stamp))
            for key in _USAGE_KEYS:
                bucket.usage[key] += usage[key]
            bucket.turns += 1
    elif rollout.total is not None and not rollout.turns_seen:
        started = parse_ts(rollout.first_ts) or datetime.fromtimestamp(
            path.stat().st_mtime, tz=window.start.tzinfo
        )
        if window.contains(started):
            usage = _turn_usage(rollout.total)
            if usage is not None:
                buckets[(rollout.model, started.date().isoformat(), bool(rollout.plan))] = _Bucket(
                    started, usage, 1
                )
    return buckets


_Windowed = dict[tuple[str, str, bool], _Bucket]  # (model, UTC day, on a plan) → its bucket


def _session_totals(rollout: _Rollout) -> tuple[int, int]:
    """``(weight, turns)`` of the whole session, every non-empty turn in or out of the window.

    A cost reported once per session (by a plugin that knows what it cost) is split across the session's rows by
    ``share`` — each row's fraction of this weight, or of these turns when no turn has any weight — so a session
    that spans two report windows or two (model, day) rows is charged once in total, never once per row or window.
    """
    weight = turns = 0
    for _, last, _, _ in rollout.events:
        usage = _turn_usage(last)
        if usage is None:
            continue
        weight += _weight(usage)
        turns += 1
    return weight, turns


def _weight(usage: Mapping[str, int]) -> int:
    """Input + output tokens: cached input is a subset of input and must not weigh twice."""
    return sum(usage[key] for key in _WEIGHT_KEYS)


def _share(bucket: _Bucket, session: _Session) -> float:
    """This bucket's fraction of the session's usage: by weight, else by turns; a total-only session is one bucket.

    A cached-only bucket of a weighted session has a true 0.0 share; a session whose turns report only cached
    tokens has rows but no weight, so its shares come from the turn count — either way they sum to one and a
    per-session cost is charged once, never once per row.
    """
    if session.weight:
        return _weight(bucket.usage) / session.weight
    return bucket.turns / session.turns if session.turns else 1.0


@dataclass(frozen=True)
class _Session:
    """What every row of a session shares: its id, scope, billing evidence and the divisors of the cost split. ``billing`` is the rule for turns without plan evidence."""

    ref: str
    scope: Scope
    billing: Billing
    weight: int
    turns: int


def _session_rows(session: _Session, windowed: _Windowed, default_model: str) -> list[UsageRow]:
    """One row per (model, UTC day, plan or not) of a session: a plan turn is the plan's, the rest follow the rule."""
    return [
        UsageRow(
            provider=Provider.OPENAI,
            model=key[0] or default_model or "unknown",  # the public core ships no default model (ADR-0004)
            kind=RowKind.SESSION,
            source=SOURCE_NAME,
            at=bucket.first_in,
            ref=session.ref,
            billing=Billing.SUBSCRIPTION if key[2] else session.billing,
            tokens=Tokens(
                input=bucket.usage["input_tokens"],
                cached_input=bucket.usage["cached_input_tokens"],
                output=bucket.usage["output_tokens"],
            ),
            scope=session.scope,
            share=_share(bucket, session),
        )
        for key, bucket in windowed.items()
    ]


def collect_codex(
    codex_home: Path, window: Window, default_model: str, billing_default: Billing = Billing.UNKNOWN
) -> Collected:
    """Rows for every rollout with turns inside the window, one per (model, UTC day) the turns fall in.

    A rollout that names a plan is a subscription session; every other session gets ``billing_default`` (the
    provider's configured billing), ``UNKNOWN`` when nothing says how it was paid.
    """
    rows: list[UsageRow] = []
    skipped: list[Skipped] = []
    min_mtime = (window.start - timedelta(minutes=1)).timestamp()
    for path in sorted((codex_home / "sessions").glob("*/*/*/rollout-*.jsonl")):
        try:
            if path.stat().st_mtime < min_mtime:
                continue
            rollout = _read_rollout(path, skipped)
            windowed = _usage_in_window(rollout, path, window)
            weight, turns = _session_totals(rollout)  # every turn was validated when read
        except (
            OSError,
            OverflowError,
            TypeError,
            ValueError,
        ) as exc:  # unreadable file, a usage block that is not numbers, or an mtime no datetime can hold
            skipped.append(Skipped("codex", str(path), f"unusable: {exc}"))
            continue
        ref = rollout.session or path.name
        scope = Scope(branch=rollout.branch, workspace=rollout.cwd)
        session = _Session(ref, scope, billing_default, weight, turns)
        rows += _session_rows(session, windowed, default_model)
    return Collected(rows=rows, skipped=skipped)


class CodexSource:
    """The rollouts under ``<codex_home>/sessions``; ``providers.openai.billing`` is the rule for sessions without a plan."""

    name = SOURCE_NAME

    def collect(self, ctx: Context) -> Collected:
        """Codex rows inside the window, billed by plan evidence, else by the configured rule."""
        rule = Billing.rule(ctx.config.openai_billing)
        return collect_codex(ctx.paths.codex_home, ctx.window, ctx.config.openai_default_model, rule)
