"""A CLI's session files, listed the same way on every Python.

``Path.glob`` raises ``PermissionError`` for a root under an unreadable directory up to Python 3.12 and returns
nothing from 3.13 on, so the same unreadable home crashed one run and emptied another in silence. Here an absent
directory holds no files, and one that exists but cannot be listed is an ``OSError`` its caller counts.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from ..models import Skipped

T = TypeVar("T")


def listing(root: Path, pattern: str) -> list[Path]:
    """The paths under ``root`` matching ``pattern``, sorted; none when ``root`` is absent, ``OSError`` when unlistable."""
    try:
        mode = os.stat(root).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return []
    if not stat.S_ISDIR(mode):
        raise NotADirectoryError(errno.ENOTDIR, "not a directory", str(root))
    if not os.access(root, os.R_OK | os.X_OK):
        raise PermissionError(errno.EACCES, "cannot list the directory", str(root))
    return sorted(root.glob(pattern))


def or_skip(find: Callable[[], list[T]], source: str, where: Path, skipped: list[Skipped]) -> list[T]:
    """What ``find`` lists, or nothing and one counted skip when the directory cannot be listed."""
    try:
        return find()
    except OSError as exc:
        skipped.append(Skipped(source, str(where), f"cannot list: {exc.strerror or exc}"))
        return []
