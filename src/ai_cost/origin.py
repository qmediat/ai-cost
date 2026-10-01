"""Who launched the work a usage-log line records: the origin keys, one validator for the writer and the reader.

Any program may stamp its lines: ``origin_session`` (the session that launched the work, e.g. a Claude Code session
id), ``origin_repo`` (``owner/name``), ``origin_pr`` (a positive number, only beside a repository) and ``run_id`` (the
invocation's own id). Period reports read the line as before; the schema-1 ``session``, ``ref`` and ``pr`` keep their
meaning (``pr`` is free text scope, never ``origin_pr``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .values import MAX_COUNT, optional_text

ID_MAX = 200
_ID = re.compile(r"[A-Za-z0-9._:-]+")
_REPO = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+"
)  # a GitHub owner, then a name ("." and ".." are not)


@dataclass(frozen=True)
class Origin:
    """The origin keys of one line; empty text and ``None`` mean absent."""

    session: str = ""
    repo: str = ""
    pr: int | None = None
    run_id: str = ""

    def __post_init__(self) -> None:
        """Every key as the reader will accept it; a malformed one is a ``ValueError`` naming the key."""
        _check_id("origin_session", self.session)
        _check_id("run_id", self.run_id)
        _check_repo(self.repo)
        _check_pr(self.pr, self.repo)

    def line_keys(self) -> dict[str, Any]:
        """The keys that carry a value, as a log line writes them."""
        line: dict[str, Any] = {
            "origin_session": self.session,
            "origin_repo": self.repo,
            "origin_pr": self.pr,
            "run_id": self.run_id,
        }
        return {key: value for key, value in line.items() if value}


def origin_of(entry: Mapping[str, Any]) -> Origin:
    """A line's origin keys as an ``Origin``; a malformed key is a ``ValueError`` naming it."""
    return Origin(
        session=optional_text(entry, "origin_session"),
        repo=optional_text(entry, "origin_repo"),
        pr=entry.get("origin_pr"),
        run_id=optional_text(entry, "run_id"),
    )


def _check_id(key: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {type(value).__name__}")
    if value and (len(value) > ID_MAX or not _ID.fullmatch(value)):
        raise ValueError(f"{key} must be letters, digits and . _ : - (at most {ID_MAX}), got {value[:40]!r}")


def _check_repo(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(f"origin_repo must be a string, got {type(value).__name__}")
    if value and (len(value) > ID_MAX or not _REPO.fullmatch(value) or value.split("/")[1] in (".", "..")):
        raise ValueError(f"origin_repo must be owner/name, got {value[:40]!r}")


def _check_pr(value: object, repo: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_COUNT
    ):  # True == 1, not here
        raise ValueError(f"origin_pr must be a positive integer up to {MAX_COUNT}, got {str(value)[:40]}")
    if not repo:
        raise ValueError("origin_pr needs origin_repo: a number alone names no pull request")
