"""Every email address in the tree is in ``qmediat.io`` or on the short allowed list.

    python3 scripts/check_addresses.py [--walk] [ROOT]

An address is every match of one pattern, the same in every implementation of this check: a local part of Unicode
letters, digits and ``._%+-``; an at-sign; a domain of dot-separated labels of Unicode letters, digits, ``-`` and
``_`` whose last label is two or more letters; not followed by a letter or a digit, nor by a dot and a letter or digit
(a ``_`` or ``-`` after the last label ends the address).
No shape is exempt: a git remote in scp form, a version or a file name written that way is an address like any other.
One is allowed only when its domain is, or is a subdomain of, ``qmediat.io``, ``users.noreply.github.com``,
``example.com``, ``example.org`` or ``example.net``; when it is the no-reply address of ``anthropic.com``; or when the
exact (file, address) pair is in ``REPOSITORY_ADDRESSES`` — a file that must show a remote or other address-shaped
text adds one entry there. ``src/ai_cost/tests/data/address-corpus.json`` is the shared corpus that pins the pattern.

Not read, by design: an address not written plainly (a quoted local part, a percent- or entity-encoded at-sign, a
fullwidth at-sign, an ``[at]`` form, an address split across lines); a last label of one letter, which is not read as
an address; a file with a NUL byte in its first 8192 bytes (binary, UTF-16 text, image or PDF metadata); in the
tracked-files mode, build output git does not track in the private tree (``dist/``) — the bundle in ``bin/`` is tracked
but binary. ``scripts/release.sh`` walks the unpacked artefacts themselves, so what a build publishes is read. An address with a quoted or otherwise unusual local part is reported as its
address-shaped tail, and an allowlist entry uses the reported form. Allowed domains, allowed addresses and allowlist
entries compare case-insensitively; an entry's file matches the reported path or the path's tail after a ``/``, so one
entry covers the package, the export and each unpacked artefact; an entry no file needs any more is not reported.

The files read (ROOT defaults to this script's package):

* ``--walk``: every file below ROOT except ``__pycache__`` directories — a ``.git`` directory is read like any
  other; git is never asked, so what a ``.gitignore`` ignores is read too. ``scripts/release.sh`` checks what it publishes this way: the tree it exports,
  and every member of the artefacts it builds, unpacked, before anything is uploaded.
* without it: the files git tracks under ROOT when ROOT is inside a work tree; a walk when it is not, or when none of
  the tracked files is there, that walk skipping ``.git`` directories too. CI checks the repository this way, and ``scripts/release.sh`` checks the package this
  way first, a fast refusal before it builds or exports (the private tree holds untracked build output).

Exit 0 with one line naming the number of files read; exit 1 with one ``path:line: address`` line per address that
is not allowed; exit 2 with one line per file that cannot be read (or a symbolic link that does not lead to a regular
file inside ROOT), when git fails, or on a usage error: the check cannot tell.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "check_addresses"
PREFIX_BYTES = 8192
WALK_SKIPPED = frozenset({"__pycache__"})
FALLBACK_SKIPPED = WALK_SKIPPED | {".git"}
WALK = "--walk"
USAGE = f"usage: python3 scripts/{NAME}.py [{WALK}] [ROOT]"
ADDRESS = re.compile(r"(?<![\w.%+-])([\w.%+-]+@(?:[\w-]+\.)+[^\W\d_]{2,})(?![^\W_])(?!\.[^\W_])")


@dataclass(frozen=True)
class RepositoryAddress:
    """An address allowed in one file only: text the file must show."""

    file: str
    address: str


ALLOWED_DOMAINS: tuple[str, ...] = (
    "qmediat.io",
    "users.noreply.github.com",
    "example.com",
    "example.org",
    "example.net",
)
ALLOWED_ADDRESSES: tuple[str, ...] = ("noreply@anthropic.com",)
REPOSITORY_ADDRESSES: tuple[RepositoryAddress, ...] = ()


class Listing(Enum):
    """How the files to read are found."""

    WALK = "walk"
    TRACKED = "tracked"


@dataclass(frozen=True)
class Request:
    """What the command line asks for."""

    root: Path
    listing: Listing


@dataclass(frozen=True)
class Finding:
    """An address that is not allowed, where it stands."""

    path: str
    line: int
    address: str


@dataclass(frozen=True)
class Unreadable:
    """A file the check could not read, and why."""

    path: str
    reason: str


@dataclass(frozen=True)
class Verdict:
    """What the tree holds: the findings, the files that could not be read, the number of files read."""

    findings: tuple[Finding, ...]
    unreadable: tuple[Unreadable, ...]
    files_read: int


class GitFailed(Exception):
    """``git ls-files`` failed; the message is its first stderr line or its exit status."""


def _in_domain(domain: str, allowed: str) -> bool:
    return domain == allowed or domain.endswith("." + allowed)


def is_allowed(path: str, address: str) -> bool:
    """Whether ADDRESS may stand in the file at PATH (relative to ROOT, with "/"); addresses compare case-insensitively."""
    lowered = address.lower()
    domain = lowered.rsplit("@", 1)[1]
    if any(_in_domain(domain, allowed.lower()) for allowed in ALLOWED_DOMAINS):
        return True
    if lowered in (allowed.lower() for allowed in ALLOWED_ADDRESSES):
        return True
    return any(
        _same_file(entry.file, path) and entry.address.lower() == lowered for entry in REPOSITORY_ADDRESSES
    )


def _same_file(entry: str, path: str) -> bool:
    """Whether the reported PATH is the entry's file, in this tree or below a prefix (an export, an artefact)."""
    wanted, reported = entry.replace("\\", "/").strip("/"), path.replace("\\", "/")
    return reported == wanted or reported.endswith("/" + wanted)


def addresses(line: str) -> list[str]:
    """Every address of one line."""
    return [match.group(1) for match in ADDRESS.finditer(line)]


def findings_in(path: str, text: str) -> list[Finding]:
    """Every address of TEXT that may not stand in PATH, with its line number."""
    return [
        Finding(path, number, address)
        for number, line in enumerate(text.splitlines(), start=1)
        for address in addresses(line)
        if not is_allowed(path, address)
    ]


def _inside_git_work_tree(root: Path) -> bool:
    try:
        run = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except FileNotFoundError:  # no git on this machine: the tree is walked
        return False
    return run.returncode == 0 and run.stdout.strip() == "true"


def _present(path: Path) -> bool:
    """Whether PATH is in the work tree: missing when it or a parent does not exist, present when it cannot be told."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:  # a parent that cannot be searched: kept, and reading it says why it cannot be read
        return True
    return True


def tracked_files(root: Path) -> list[str] | None:
    """The files git tracks under ROOT that are there, relative to it; None when none of them is there.

    A tracked file deleted from the work tree is not in the tree any more.
    """
    run = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False, timeout=60
    )
    if run.returncode != 0:
        lines = run.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitFailed(lines[0] if lines else f"exit {run.returncode}")
    names = [name for name in run.stdout.decode("utf-8", "surrogateescape").split("\0") if name]
    present = [name for name in names if _present(root / name)]
    return present or None


def _raise(error: OSError) -> None:
    raise error


def walked_files(root: Path, skipped: frozenset[str]) -> list[str]:
    """Every file and symbolic link below ROOT, relative to it, outside directories named in SKIPPED."""
    found: list[str] = []
    for current, dirs, files in os.walk(root, onerror=_raise):
        base = Path(current)
        kept = [name for name in dirs if name not in skipped]
        links = [name for name in kept if (base / name).is_symlink()]
        dirs[:] = sorted(name for name in kept if name not in links)
        found += [(base / name).relative_to(root).as_posix() for name in sorted(files + links)]
    return found


def listed_files(request: Request) -> list[str]:
    """The files to read: a walk, or git's tracked files with a walk where git has none there."""
    if request.listing is Listing.WALK:
        return walked_files(request.root, WALK_SKIPPED)
    tracked = tracked_files(request.root) if _inside_git_work_tree(request.root) else None
    return walked_files(request.root, FALLBACK_SKIPPED) if tracked is None else tracked


def _check_link(root: Path, path: Path) -> None:
    """Raise OSError unless the symbolic link PATH leads to a regular file inside ROOT (a loop included)."""
    try:
        target = path.resolve()
        inside = target.is_file() and target.is_relative_to(root.resolve())
    except (
        RuntimeError
    ) as exc:  # a loop before Python 3.13; from 3.13 resolve() returns it and is_file() is False
        raise OSError(str(exc)) from exc
    if not inside:
        raise OSError(f"a symbolic link that does not lead to a regular file inside {root}")


def _text(root: Path, name: str) -> str | None:
    """The file's text; None for a binary file. Raises OSError for a file that cannot be read."""
    path = root / name
    if path.is_symlink():
        _check_link(root, path)
    with path.open("rb") as handle:
        prefix = handle.read(PREFIX_BYTES)
        if b"\0" in prefix:
            return None
        return (prefix + handle.read()).decode("utf-8", "replace")


def judge(root: Path, names: Iterable[str]) -> Verdict:
    """Read every named file of ROOT and collect the findings and the files that could not be read."""
    findings: list[Finding] = []
    unreadable: list[Unreadable] = []
    read = 0
    for name in names:
        try:
            text = _text(root, name)
        except OSError as exc:
            unreadable.append(Unreadable(name, exc.strerror or str(exc)))
            continue
        if text is None:
            continue
        read += 1
        findings += findings_in(name, text)
    return Verdict(tuple(findings), tuple(unreadable), read)


def _report(verdict: Verdict) -> int:
    for finding in verdict.findings:
        print(f"{finding.path}:{finding.line}: {finding.address}", file=sys.stderr)
    for file in verdict.unreadable:
        print(f"{NAME}: cannot read {file.path}: {file.reason}", file=sys.stderr)
    if verdict.unreadable:
        return 2
    if verdict.findings:
        print(f"{NAME}: {len(verdict.findings)} address(es) not allowed", file=sys.stderr)
        return 1
    print(f"{NAME}: {verdict.files_read} files read, every address allowed")
    return 0


def _request(args: Sequence[str]) -> Request | None:
    """The command line as a Request, or None after one line on stderr says why it is not one."""
    listing = Listing.WALK if WALK in args else Listing.TRACKED
    rest = [arg for arg in args if arg != WALK]
    if len(rest) > 1 or any(arg.startswith("-") for arg in rest):
        print(USAGE, file=sys.stderr)
        return None
    root = Path(rest[0]) if rest else ROOT
    if not root.is_dir():
        print(f"{NAME}: cannot read {root}: not a directory", file=sys.stderr)
        return None
    return Request(root, listing)


def _names(request: Request) -> list[str] | None:
    """The files to read, or None after one line on stderr says why they cannot be listed."""
    try:
        return listed_files(request)
    except (GitFailed, subprocess.SubprocessError) as exc:
        print(f"{NAME}: git ls-files failed: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"{NAME}: cannot read {exc.filename}: {exc.strerror}", file=sys.stderr)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Check the tree; exit 0, 1 (an address that is not allowed) or 2 (cannot tell)."""
    request = _request(list(sys.argv[1:] if argv is None else argv))
    names = None if request is None else _names(request)
    if request is None or names is None:
        return 2
    return _report(judge(request.root, names))


if __name__ == "__main__":
    sys.exit(main())
