"""Gemini CLI sessions: one row per model message, tokens as the CLI counts them.

The CLI writes ``~/.gemini/tmp/<project>/chats/session-<stamp>-<id>.jsonl`` (older versions: ``.json`` with a
``messages`` list). A model message is ``{"type": "gemini", "id", "timestamp", "model", "tokens": {input, cached,
output, thoughts, tool, total}}``; streamed updates repeat the same ``id`` with the same counts, so each id counts
once. ``input`` includes ``cached``; ``thoughts`` are billed as output. A ``{"$set": …}`` line is a patch record and
carries no usage. Nothing in the file says how the session was paid, so the row's billing is ``UNKNOWN`` until the
provider's configured billing (or a plugin) decides.

``<project>`` is the basename of the working directory the CLI ran in, or (older versions) the sha256 of its
path; ``<gemini_home>/projects.json`` maps every path to its name, so a row's workspace is the directory itself
whenever the file knows it (``--project`` and ``--attribute`` then place the row, ADR-0006) and the bare directory
name otherwise. The ref stays ``<project dir>/<session>``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models import Billing, Collected, Provider, RowKind, Scope, Skipped, Tokens, UsageRow, Window
from ..timeutil import parse_ts
from ..values import count

if TYPE_CHECKING:
    from ..plugins import Context

SOURCE_NAME = "gemini-cli"  # the one name of this source: on every row and on the Source


def session_files(gemini_home: Path) -> list[Path]:
    """Every session file under ``<gemini_home>/tmp/*/chats``, oldest first."""
    return sorted((gemini_home / "tmp").glob("*/chats/session-*.json*"))


def project_paths(gemini_home: Path, skipped: list[Skipped]) -> dict[str, str]:
    """The working directory behind each ``tmp/<dir>`` name.

    By the CLI's ``projects.json`` (path → name) and by the sha256 of the path (the hashed names older versions
    used); empty when the file is absent, and a counted skip when it is present but not what it should be.
    """
    file = gemini_home / "projects.json"
    if not file.exists():
        return {}
    try:
        document = json.loads(file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, RecursionError) as exc:
        skipped.append(Skipped(SOURCE_NAME, str(file), f"unusable: {exc.__class__.__name__}"))
        return {}
    projects = document.get("projects") if isinstance(document, Mapping) else None
    if not isinstance(projects, Mapping):
        skipped.append(Skipped(SOURCE_NAME, str(file), "no projects object"))
        return {}
    out: dict[str, str] = {}
    for path, name in projects.items():
        if isinstance(path, str) and path and isinstance(name, str) and name:
            out[name] = path
            out[hashlib.sha256(path.encode("utf-8")).hexdigest()] = path
    return out


def _lines(path: Path, skipped: list[Skipped]) -> Iterator[tuple[int, Any]]:
    """The records of a ``.jsonl``; a line that is not JSON is one counted skip, the other lines still count."""
    with path.open(errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield line_no, json.loads(line)
            except ValueError:
                skipped.append(Skipped("gemini-cli", str(path), f"line {line_no}: not JSON"))


def _records(path: Path, skipped: list[Skipped]) -> Iterator[tuple[int, Any]]:
    """``(line_no, record)`` for a ``.jsonl`` (one object per line) or a ``.json`` (its ``messages`` list)."""
    if path.suffix == ".jsonl":
        yield from _lines(path, skipped)
        return
    document = json.loads(path.read_text(errors="replace"))
    messages = document.get("messages") if isinstance(document, Mapping) else None
    if not isinstance(messages, list):
        raise ValueError("no messages list")
    yield 0, {
        "sessionId": document.get("sessionId")
    }  # the document's header, as line 1 of a .jsonl carries it
    yield from enumerate(messages, 1)


def _tokens(raw: Any) -> Tokens:
    if not isinstance(raw, Mapping):
        raise TypeError(f"tokens must be an object, got {type(raw).__name__}")
    return Tokens(
        prompt=count(raw.get("input"), "input"),
        cached=count(raw.get("cached"), "cached"),
        output=count(raw.get("output"), "output") + count(raw.get("thoughts"), "thoughts"),
    )


def _row(record: Any, session: str, project: str, billing: Billing, workspace: str) -> UsageRow | None:
    """A model message as a row; ``None`` for user turns, patch records and messages without usage."""
    if not isinstance(record, Mapping) or record.get("type") != "gemini" or record.get("tokens") is None:
        return None
    at = parse_ts(record.get("timestamp"))
    if at is None:
        raise ValueError("no timestamp")
    return UsageRow(
        provider=Provider.GOOGLE,
        model=str(record.get("model") or "unknown"),
        kind=RowKind.CHAT,
        source=SOURCE_NAME,
        at=at,
        ref=f"{project}/{session}",
        billing=billing,  # the configured providers.google.billing rule; the session log itself says nothing
        tokens=_tokens(record.get("tokens")),
        scope=Scope(workspace=workspace),
    )


def _is_header(record: Any) -> bool:
    """The session header names the session and has no ``type``; a message that also carries the id is a message."""
    return (
        isinstance(record, Mapping)
        and "type" not in record
        and bool(record.get("sessionId"))
        and isinstance(record["sessionId"], str)
    )


def _collect_file(
    path: Path, window: Window | None, skipped: list[Skipped], billing: Billing, projects: Mapping[str, str]
) -> list[UsageRow]:
    rows: list[UsageRow] = []
    seen: set[str] = set()
    session = path.stem.split("-")[
        -1
    ]  # the file name: the first 8 characters of the id, until the header says it whole
    project = path.parent.parent.name
    workspace = projects.get(
        project, project
    )  # the directory itself when projects.json knows it, else the name
    for line_no, record in _records(path, skipped):
        if _is_header(record):
            session = str(record["sessionId"])  # the session header (line 1 of a .jsonl, the .json document)
            continue
        try:
            row = _row(record, session, project, billing, workspace)
        except (TypeError, ValueError) as exc:
            skipped.append(Skipped("gemini-cli", str(path), f"line {line_no}: {exc}"))
            continue
        message_id = str(record.get("id") or line_no) if isinstance(record, Mapping) else str(line_no)
        if row is None or message_id in seen:
            continue
        seen.add(message_id)
        if window is None or window.contains(row.at):
            rows.append(row)
    return rows


def collect_gemini_cli(
    gemini_home: Path, window: Window | None, billing: Billing = Billing.UNKNOWN
) -> Collected:
    """Rows of every Gemini CLI session inside the window (paid as ``billing`` says); a bad file is a counted skip."""
    rows: list[UsageRow] = []
    skipped: list[Skipped] = []
    projects = project_paths(gemini_home, skipped)
    for path in session_files(gemini_home):
        try:
            rows += _collect_file(path, window, skipped, billing, projects)
        except (OSError, ValueError) as exc:  # ValueError: not JSON, or a .json without messages
            skipped.append(Skipped("gemini-cli", str(path), f"unusable: {exc}"))
    return Collected(rows=rows, skipped=skipped)


class GeminiCliSource:
    """The Gemini CLI sessions under ``<gemini_home>/tmp/*/chats``."""

    name = SOURCE_NAME

    def collect(self, ctx: Context) -> Collected:
        """Gemini CLI rows inside the window."""
        return collect_gemini_cli(ctx.paths.gemini_home, ctx.window, Billing.rule(ctx.config.google_billing))
