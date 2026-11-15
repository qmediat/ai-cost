"""What a command writes: the lines it prints (``Emit``) and the state files it creates or appends to."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from .errors import ToolError

Emit = Callable[[str], None]


def ensure_dir(path: Path, what: str) -> None:
    """Create ``path`` with its parents; a file in the way or a permission is a ToolError, never a traceback."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"cannot create the {what} {path}: {exc}") from exc


def append_json_line(path: Path, data: Mapping[str, Any]) -> None:
    """One JSON object per line, appended; a path that cannot be written is a ``ToolError``, never a traceback."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(data) + "\n")
    except OSError as exc:
        raise ToolError(f"cannot write {path}: {exc}") from exc
