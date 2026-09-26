"""How this program starts a copy of itself: the zipapp by its path, a source checkout through ``-m``."""

from __future__ import annotations

import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path

_TRANSIENT = ("/_npx/", "\\_npx\\")  # npx's cache (POSIX, Windows): npm may prune it at any time


def self_command(argv0: str | None = None) -> list[str]:
    """``[python, <archive>]`` when running from the zipapp, else ``[python, -m, ai_cost]``."""
    entry = Path(argv0 if argv0 is not None else (sys.argv[0] if sys.argv else ""))
    if entry.name and entry.is_file() and zipfile.is_zipfile(entry):
        return [sys.executable, str(entry.resolve())]
    return [sys.executable, "-m", "ai_cost"]


def transient_warning(command: Sequence[str]) -> str:
    """Why a scheduled job must not point at this copy (npx's cache, which npm may prune); empty when it may."""
    program = command[1] if len(command) > 1 else ""
    if not any(marker in program for marker in _TRANSIENT):
        return ""
    return (
        f"this copy runs from npx's cache ({program}), which npm may prune at any time: the scheduled job would "
        "point at a file that can vanish — install it globally (npm install -g ai-costs) and schedule from there"
    )
