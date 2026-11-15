"""``ai-cost collect``: an instance's usage segments on another machine, pulled over ssh into one local file each.

The remote side runs plain ``sh``; no ai-cost is needed there. A pull is written to a temporary file in the target
directory and moved over the old file only when ssh succeeded, so a reader never sees half a file and a failed pull
leaves the previous one in place.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import shlex
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .errors import UsageError
from .output import Emit

# Every file of <PATH>/usage named YYYY-MM.jsonl (the reader's SEGMENT_NAME), in name order, each ended by a newline.
# Nothing is skipped silently: a linked root is followed (it is what the user named); a linked usage/ fails the pull
# (exit 5: it may lead to another instance's records); a month entry that is a link (exit 6) or not a regular file
# (exit 7, tested before any read, so a fifo never blocks) fails the pull too. Names that are no month (-00, -13…-19)
# are not segments and are passed over; a glob that matches nothing is no entry: an instance without segments. $1 is the path, passed as an argument so the remote shell
# never interprets it; CDPATH is unset, cd prints nothing into the stream and is never given a bare "-" (a relative
# root is entered as ./ROOT). Each segment is read once by awk, which ends its last line with a newline; it gets no
# "--" (the BSD awk reads it as a file) and needs none: the names are digits, "-", ".jsonl" — never an option or a
# VAR=value assignment. The root is entered first, then usage/ tested from inside it: a root that cannot be entered,
# or a usage/ that cannot be entered or listed, fails the pull. The local side keeps the stronger rule (``pull``): a
# pull never shrinks the file.
REMOTE_SCRIPT = (
    "unset CDPATH; "
    "case $1 in /*) r=$1;; *) r=./$1;; esac; "
    'cd -- "$r" >/dev/null 2>&1 || { echo "no such directory or cannot enter: $1" >&2; exit 3; }; '
    "[ -e usage ] || [ -L usage ] || exit 0; "
    '[ -L usage ] && { echo "$1/usage is a link: not pulled" >&2; exit 5; }; '
    'cd -- ./usage >/dev/null 2>&1 && [ -r . ] || { echo "cannot read $1/usage" >&2; exit 4; }; '
    "for f in [0-9][0-9][0-9][0-9]-[01][0-9].jsonl; do "
    "case $f in *-00.jsonl|*-1[3-9].jsonl) continue;; esac; "
    '[ -e "$f" ] || [ -L "$f" ] || continue; '
    '[ -L "$f" ] && { echo "$1/usage/$f is a link: not pulled" >&2; exit 6; }; '
    '[ -f "$f" ] || { echo "$1/usage/$f is not a regular file: not pulled" >&2; exit 7; }; '
    'awk 1 "$f" || exit 1; done'
)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_HOST_CHARS, _PATH_CHARS = 40, 80  # the readable parts of a file name; the 64-bit digest keeps it unique
_FILE_MODE = 0o644  # a new pulled file: what the umask leaves of it, as the tool's other files
_CHUNK = 1 << 20


@dataclass(frozen=True)
class Target:
    """One remote instance: ``host`` as ssh takes it, ``path`` its root on that host."""

    host: str
    path: str

    @property
    def remote(self) -> str:
        """The path as the remote shell gets it (``remote_path``): one spelling per remote directory."""
        return remote_path(self.path)

    @property
    def file_name(self) -> str:
        """``<host>-<path>-<hash>.jsonl`` of the normalised path: safe characters, bounded, distinct per instance."""
        digest = hashlib.sha256(f"{self.host}:{self.remote}".encode()).hexdigest()[:16]
        return f"{_safe(self.host)[:_HOST_CHARS]}-{_safe(self.remote)[:_PATH_CHARS]}-{digest}.jsonl"


@dataclass(frozen=True)
class Ssh:
    """The ssh executable and its connect timeout in seconds."""

    program: str
    timeout: int


class Outcome(Enum):
    """How one pull ended."""

    PULLED = "pulled"
    SSH_FAILED = "ssh failed"
    LOCAL_ERROR = "local error"
    SMALLER = "smaller pull"  # fewer bytes than the file holds: segments only grow, so the remote lost sight of some


@dataclass(frozen=True)
class Size:
    """A file's lines and bytes."""

    lines: int = 0
    size: int = 0

    def __str__(self) -> str:
        return f"{self.lines} lines ({self.size} bytes)"


@dataclass(frozen=True)
class Pulled:
    """What one pull did: the file and what it holds now — or the failure, with its cause."""

    target: Target
    file: Path
    outcome: Outcome = Outcome.PULLED
    pulled: Size = Size()
    held: Size = Size()  # what the previous file holds (SMALLER)
    status: int = 0  # ssh's exit status (SSH_FAILED)
    error: str = ""
    kept: bool = False  # a failed pull left an earlier file in place

    @property
    def ok(self) -> bool:
        """Whether the file now holds the remote segments."""
        return self.outcome is Outcome.PULLED

    def line(self) -> str:
        """The command's line for this pull."""
        name = f"{self.target.host}:{self.target.path}"
        if self.ok:
            return f"{name} → {self.file}: {self.pulled}"
        if self.outcome is Outcome.SMALLER:
            return (
                f"{name} FAILED (smaller pull): pulled {self.pulled} where {self.file} holds {self.held} — kept; "
                f"delete {self.file} to accept a smaller pull"
            )
        cause = f"ssh exit {self.status}" if self.outcome is Outcome.SSH_FAILED else self.outcome.value
        after = f"{self.file} kept" if self.kept else "no file written"
        return f"{name} FAILED ({cause}): {self.error or 'no message'} — {after}"


def _safe(text: str) -> str:
    """``text`` with every run of characters outside letters, digits, ``.``, ``_``, ``-`` as one ``-`` (``_`` when nothing is left)."""
    return _UNSAFE.sub("-", text).strip("-.") or "_"


def parse_target(spec: str) -> Target:
    """``HOST:PATH``; no ``:``, an empty host or path, or a host that ssh would read as an option is a UsageError."""
    host, colon, path = spec.partition(":")
    if not colon or not host or not path:
        raise UsageError(f"collect: expected HOST:PATH, got {spec!r}")
    if host.startswith("-"):
        raise UsageError(f"collect: a host never starts with '-' (ssh would read an option), got {host!r}")
    if ".." in path.split("/"):
        raise UsageError(
            f"collect: {path!r} has a '..' — only the remote can resolve it; give the path without it"
        )
    return Target(host, path)


def remote_path(path: str) -> str:
    """The path as the remote shell gets it, normalised lexically (``..`` is refused by ``parse_target``).

    Empty and ``.`` components dropped (``//``, ``/./``, a trailing ``/`` or ``/.``), one leading ``~`` dropped (the
    path is then relative to the home: quoted, ``~`` is not expanded there); ``.`` for the home itself.
    """
    parts = path.split("/")
    if parts[0] == "~":
        parts = parts[1:]
    kept = "/".join(part for part in parts if part not in ("", "."))
    if path.startswith("/"):
        return "/" + kept
    return kept or "."


def ssh_argv(ssh: Ssh, target: Target) -> list[str]:
    """One non-interactive call: ``sh -c SCRIPT ai-cost PATH``, every word quoted for the remote shell."""
    command = " ".join(shlex.quote(word) for word in ("sh", "-c", REMOTE_SCRIPT, "ai-cost", target.remote))
    options = (
        "BatchMode=yes",
        f"ConnectTimeout={ssh.timeout}",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=3",
    )
    return [ssh.program, *(word for option in options for word in ("-o", option)), target.host, command]


def _temporary(into: Path, final: Path) -> Path:
    """A new, writable file beside ``final``, ``_FILE_MODE`` under the umask (``_keep_mode`` sets it after the write)."""
    temp = into / f".{final.name}.{secrets.token_hex(6)}.tmp"
    os.close(os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE))
    return temp


def _keep_mode(temp: Path, final: Path) -> None:
    """The written ``temp`` takes ``final``'s mode when ``final`` exists: a re-pull never changes who can read it."""
    if final.exists():
        os.chmod(temp, stat.S_IMODE(final.stat().st_mode))


def _counted(file: Path) -> Size:
    """Lines (a last line without its newline counts) and bytes of a pulled file, read in chunks."""
    lines, size, last = 0, 0, b"\n"
    with file.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            lines, size, last = lines + chunk.count(b"\n"), size + len(chunk), chunk[-1:]
    return Size(lines + (0 if last == b"\n" else 1), size)


def _ssh_into(ssh: Ssh, target: Target, temp: Path) -> tuple[int, str]:
    """Ssh's exit status and the first line of its stderr, its stdout written to ``temp``."""
    with temp.open("wb") as handle:
        done = subprocess.run(
            ssh_argv(ssh, target),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.PIPE,
            check=False,
        )
    said = done.stderr.decode("utf-8", "replace").strip().splitlines()
    return done.returncode, said[0] if said else ""


def pull(ssh: Ssh, target: Target, into: Path) -> Pulled:
    """One target into ``into``; the old file is replaced only by a whole, successful pull that is not smaller."""
    final, temp = into / target.file_name, None
    try:
        temp = _temporary(into, final)
        status, error = _ssh_into(ssh, target, temp)
        if status != 0:
            return Pulled(target, final, Outcome.SSH_FAILED, status=status, error=error, kept=final.exists())
        pulled = _counted(temp)  # counted before the replace: nothing after it can fail the pull
        # segments only grow: a pull with fewer bytes lost sight of some, whatever the remote's reason
        if final.exists() and pulled.size < final.stat().st_size:
            return Pulled(target, final, Outcome.SMALLER, pulled, _counted(final), kept=True)
        _keep_mode(temp, final)
        os.replace(temp, final)
    except OSError as exc:
        # the ssh executable, the directory or a file failed: said as this pull's failure
        error = f"{exc.__class__.__name__}: {exc}"
        return Pulled(target, final, Outcome.LOCAL_ERROR, error=error, kept=final.exists())
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):  # a clean-up never hides the pull's result
                temp.unlink()
    return Pulled(target, final, pulled=pulled)


def collect(ssh: Ssh, targets: Sequence[Target], into: Path, emit: Emit) -> int:
    """Every target pulled in turn, one line each; 0 when all succeeded, 1 when any failed (counted on a last line)."""
    try:  # not output.ensure_dir: its ToolError is exit 1, a --into that cannot be made is a usage error (exit 2)
        into.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(f"--into: cannot create {into} ({exc.__class__.__name__})") from exc
    if not os.access(into, os.W_OK | os.X_OK):
        raise UsageError(f"--into: cannot write in {into}")
    failed = 0
    for target in targets:
        result = pull(ssh, target, into)
        failed += 0 if result.ok else 1
        emit(result.line())
    if failed:
        emit(f"{failed} of {len(targets)} pulls failed")
    return 1 if failed else 0
