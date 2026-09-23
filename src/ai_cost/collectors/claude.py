"""Claude Code transcripts: ``<claude_home>/projects/<project>/<session>.jsonl`` plus the session's subagent files.

Every assistant record carries ``message.usage``; a streamed message appears once per content block with the same
``message.id``, so ids are de-duplicated. Malformed lines are counted, never silently dropped (consult C5).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import Billing, Collected, Provider, RowKind, Scope, Skipped, Tokens, UsageRow, Window
from ..timeutil import parse_ts
from ..values import as_object, count

_TS_RE = re.compile(r'"timestamp":\s*"([^"]+)"')

SOURCE_NAME = "claude-transcripts"  # the one name of this source on every row


def project_dir(claude_home: Path, project_path: Path) -> Path:
    """Claude Code encodes a project path by replacing ``/`` and ``.`` with ``-``."""
    return claude_home / "projects" / re.sub(r"[/\\.]", "-", str(project_path.resolve()))


def _mtime(path: Path) -> float:
    """A transcript's mtime for ordering; a file deleted mid-listing or a broken symlink sorts last, never raises."""
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


def find_session_files(
    claude_home: Path,
    project_path: Path | None,
    session: str | None,
    all_projects: bool,
) -> list[tuple[str, Path]]:
    """``(session_id, path)`` for every transcript requested; a session's ``subagents`` files ride along."""
    if session and Path(session).is_file():
        path = Path(session)
        return [
            (path.stem, path),
            *((path.stem, sub) for sub in sorted((path.parent / path.stem).rglob("*.jsonl"))),
        ]
    roots = [project_dir(claude_home, project_path or Path.cwd())]
    if all_projects:
        roots = sorted(p for p in (claude_home / "projects").glob("*") if p.is_dir())
    files: list[tuple[str, Path]] = []
    for root in roots:
        candidates = sorted(root.glob("*.jsonl"), key=_mtime, reverse=True)
        if session == "latest":
            candidates = candidates[:1]
        elif session and session != "all":
            candidates = [c for c in candidates if c.name.startswith(session)]
        for candidate in candidates:
            files.append((candidate.stem, candidate))
            files.extend((candidate.stem, sub) for sub in sorted((root / candidate.stem).rglob("*.jsonl")))
    return files


def _tokens(usage: Any) -> Tokens:
    usage = as_object(usage, "usage")
    creation = as_object(usage.get("cache_creation"), "cache_creation")
    tools = as_object(usage.get("server_tool_use"), "server_tool_use")
    split = bool(creation)
    return Tokens(
        input=count(usage.get("input_tokens"), "input_tokens"),
        output=count(usage.get("output_tokens"), "output_tokens"),
        cache_read=count(usage.get("cache_read_input_tokens"), "cache_read_input_tokens"),
        cache_write_5m=count(creation.get("ephemeral_5m_input_tokens"), "ephemeral_5m_input_tokens"),
        cache_write_1h=count(creation.get("ephemeral_1h_input_tokens"), "ephemeral_1h_input_tokens"),
        cache_write_unsplit=(
            0 if split else count(usage.get("cache_creation_input_tokens"), "cache_creation_input_tokens")
        ),
        web_search=count(tools.get("web_search_requests"), "web_search_requests"),
    )


def _records(path: Path) -> Iterator[tuple[int, Mapping[str, Any] | None, datetime | None]]:
    """Yield ``(line_no, record_or_None, timestamp)``.

    A record is parsed only when it may carry usage (the literal ``"usage"``); a non-empty line that is not even a
    JSON object is malformed whatever it says.
    """
    with path.open(errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            if not line.lstrip().startswith("{"):
                yield line_no, {"__malformed__": True}, None
                continue
            if '"usage"' not in line:
                match = _TS_RE.search(line)
                yield line_no, None, parse_ts(match.group(1)) if match else None
                continue
            try:
                yield line_no, json.loads(line), None
            except ValueError:
                yield line_no, {"__malformed__": True}, None


def _assistant_usage(record: Mapping[str, Any]) -> tuple[Mapping[str, Any], Any] | None:
    """``(message, usage)`` of an assistant record that carries a usage key (any shape); ``None`` otherwise."""
    message = record.get("message") or {}
    usage = message.get("usage") if isinstance(message, Mapping) else None
    if record.get("type") != "assistant" or usage is None:
        return None
    return (
        message,
        usage,
    )  # a present but malformed usage (0, "", []) reaches _tokens and becomes a counted skip


def _billable(tokens: Tokens) -> bool:
    """Claude Code's ``<synthetic>`` turns carry no tokens at all: there is nothing to price."""
    counted = (
        tokens.input,
        tokens.output,
        tokens.cache_read,
        tokens.cache_write_5m,
        tokens.cache_write_1h,
        tokens.cache_write_unsplit,
        tokens.web_search,
    )
    return any(counted)


_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\n)\s*")
_MAX_SEGMENTS = 200


def _segments(message: Mapping[str, Any]) -> list[str]:
    """What a turn touched: a Bash command split into its statements, a file path, or the tool input as text."""
    out: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            continue
        args = block.get("input")
        if not isinstance(args, Mapping):
            continue
        if "command" in args:
            out += [part for part in _SEGMENT_SPLIT.split(str(args["command"])) if part]
        elif "file_path" in args:
            out.append(str(args["file_path"]))
        else:
            out.append(json.dumps(args, ensure_ascii=False)[:200])
    return out[:_MAX_SEGMENTS]


@dataclass
class _Message:
    """One assistant API call: the fields a row needs (never the record body) plus every entry's segments."""

    session_id: str
    path: Path
    line_no: int
    timestamp: Any
    model: str
    branch: str
    tokens: Tokens
    segments: list[str]

    @classmethod
    def read(
        cls,
        session_id: str,
        path: Path,
        line_no: int,
        record: Mapping[str, Any],
        message: Mapping[str, Any],
        usage: Mapping[str, Any],
    ) -> _Message:
        return cls(
            session_id,
            path,
            line_no,
            record.get("timestamp"),
            str(message.get("model") or "unknown"),
            str(record.get("gitBranch") or ""),
            _tokens(usage),
            _segments(message),
        )


@dataclass
class _Span:
    """The earliest and latest timestamp seen, whatever the record."""

    first: datetime | None = None
    last: datetime | None = None

    def extend(self, stamp: datetime | None) -> None:
        if stamp is None:
            return
        self.first = stamp if self.first is None or stamp < self.first else self.first
        self.last = stamp if self.last is None or stamp > self.last else self.last

    def window(self) -> Window | None:
        return Window(self.first, self.last) if self.first and self.last else None


def _read_file(
    messages: dict[str, _Message], session_id: str, path: Path, skipped: list[Skipped], seen: _Span
) -> None:
    """One transcript, streamed line by line: usage into ``messages``, every timestamp into ``seen``."""
    for line_no, record, stamp in _records(path):
        if record is None:
            seen.extend(stamp)
            continue
        if record.get("__malformed__"):
            skipped.append(Skipped("claude", str(path), f"line {line_no}: not JSON"))
            continue
        seen.extend(
            parse_ts(record.get("timestamp"))
        )  # widened even when the usage block turns out malformed
        _absorb(messages, session_id, path, line_no, record, skipped)


def _read_messages(
    files: list[tuple[str, Path]], skipped: list[Skipped]
) -> tuple[dict[str, _Message], _Span]:
    """Every assistant message keyed by id (streamed entries merged) and the span of all timestamps seen."""
    messages: dict[str, _Message] = {}
    seen = _Span()
    for session_id, path in files:
        try:
            _read_file(messages, session_id, path, skipped, seen)
        except OSError as exc:
            skipped.append(Skipped("claude", str(path), f"cannot open: {exc}"))
    return messages, seen


def _absorb(
    messages: dict[str, _Message],
    session_id: str,
    path: Path,
    line_no: int,
    record: Mapping[str, Any],
    skipped: list[Skipped],
) -> None:
    """Merge one assistant entry into its message; a usage block that does not parse is a counted skip."""
    message = record.get("message")
    if record.get("type") == "assistant" and message is not None and not isinstance(message, Mapping):
        skipped.append(Skipped("claude", str(path), f"line {line_no}: message is not an object"))
        return
    found = _assistant_usage(record)
    if found is None:
        return
    message, usage = found
    message_id = str(message.get("id") or f"{path}:{record.get('uuid')}")
    known = messages.get(message_id)
    try:
        fresh = _Message.read(session_id, path, line_no, record, message, usage)
    except (TypeError, ValueError) as exc:
        skipped.append(Skipped("claude", str(path), f"line {line_no}: bad usage: {exc}"))
        return
    if known is None:
        messages[message_id] = fresh
    elif not _billable(known.tokens) and _billable(fresh.tokens):  # the usage arrived in a later entry
        fresh.segments = known.segments + fresh.segments
        messages[message_id] = fresh
    else:
        known.segments += fresh.segments


def collect_claude(
    files: list[tuple[str, Path]], window: Window | None, billing: Billing = Billing.UNKNOWN
) -> Collected:
    """Rows for every assistant message (inside ``window`` when given) and the span of all timestamps seen.

    A transcript says nothing about how it was paid: every row is billed as ``billing`` says (the configured
    ``providers.anthropic.billing``) and a caller that does not say leaves it ``UNKNOWN`` — never a guessed plan.
    """
    rows: list[UsageRow] = []
    skipped: list[Skipped] = []
    messages, seen = _read_messages(files, skipped)
    for msg in messages.values():
        tokens = msg.tokens
        at = parse_ts(msg.timestamp)
        if at is None:
            if _billable(tokens):
                skipped.append(Skipped("claude", str(msg.path), f"line {msg.line_no}: no timestamp"))
            continue
        seen.extend(at)  # every stamped turn widens the span, priced or not
        if not _billable(tokens) or (window and not window.contains(at)):
            continue
        rows.append(_row(msg, at, tokens, billing))
    span = seen.window()
    return Collected(rows=rows, skipped=skipped, span=span)


def _row(msg: _Message, at: datetime, tokens: Tokens, billing: Billing) -> UsageRow:
    branch = msg.branch
    return UsageRow(
        provider=Provider.ANTHROPIC,
        model=msg.model,
        kind=RowKind.TRANSCRIPT,
        source=SOURCE_NAME,
        at=at,
        ref=msg.session_id,
        billing=billing,
        tokens=tokens,
        scope=Scope(branch="" if branch == "HEAD" else branch, paths=tuple(msg.segments[:_MAX_SEGMENTS])),
    )
