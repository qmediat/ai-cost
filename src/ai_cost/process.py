"""How this program starts a copy of itself: the zipapp by its path, a source checkout through ``-m``."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path


def self_command(argv0: str | None = None) -> list[str]:
    """``[python, <archive>]`` when running from the zipapp, else ``[python, -m, ai_cost]``."""
    entry = Path(argv0 if argv0 is not None else (sys.argv[0] if sys.argv else ""))
    if entry.name and entry.is_file() and zipfile.is_zipfile(entry):
        return [sys.executable, str(entry.resolve())]
    return [sys.executable, "-m", "ai_cost"]
