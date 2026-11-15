"""The README's ``## Supports · version X.Y.Z`` heading and the code's ``__version__`` name the package version.

    python3 scripts/check_supports.py [ROOT]

The package version is ``version`` of pyproject.toml's ``[project]`` table. Exit 0 when the README's one Supports
heading names it and, where ROOT holds ``src/ai_cost/__init__.py``, its ``__version__`` does too; exit 1 with one
line naming both numbers when one differs, or when the heading (exactly one) or the package version is missing. CI
runs it on every push and the release paths run it before anything is published, so a version bump that leaves the
README's "Supports" list or the code behind never ships.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = Path("src/ai_cost/__init__.py")
HEADING = re.compile(r"^## Supports · version (\S+)[ \t]*$", re.M)
TABLE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")
VERSION = re.compile(r'^\s*version\s*=\s*"([^"]+)"\s*(#.*)?$')
CODE_VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.M)
NAME = "check_supports"


@dataclass(frozen=True)
class Verdict:
    """Whether the README's Supports heading and the code carry the package version, and the line that says so."""

    ok: bool
    message: str


def project_version(pyproject: str) -> str | None:
    """``version`` of the ``[project]`` table — tomllib where the interpreter has it, a table-scoped scan before 3.11."""
    if sys.version_info >= (3, 11):
        import tomllib

        project = tomllib.loads(pyproject).get("project", {})
        version = project.get("version") if isinstance(project, dict) else None
        return version if isinstance(version, str) else None
    table = ""
    for line in pyproject.splitlines():
        header = TABLE.match(line)
        if header:
            table = header.group(1).strip()
            continue
        found = VERSION.match(line) if table == "project" else None
        if found:
            return found.group(1)
    return None


def _heading_verdict(readme: str, package: str) -> Verdict | None:
    """The failure of the README's Supports heading against the package version; None when it names it."""
    headings = HEADING.findall(readme)
    if len(headings) != 1:
        return Verdict(
            False,
            f"{NAME}: README.md has {len(headings)} '## Supports · version …' headings, needs exactly one"
            f" (the package is version {package})",
        )
    if headings[0] != package:
        return Verdict(
            False,
            f"{NAME}: README.md 'Supports' says version {headings[0]}, the package is version {package}",
        )
    return None


def _code_version(init: str | None) -> str | None:
    """``__version__`` of ``__init__.py``; None when there is no such file, ``"(none)"`` when it names none."""
    if init is None:
        return None
    found = CODE_VERSION.search(init)
    return found.group(1) if found else "(none)"


def judge(readme: str, pyproject: str, init: str | None = None) -> Verdict:
    """Compare the README heading and, when given, ``__init__.py``'s ``__version__`` with the [project] version."""
    package = project_version(pyproject)
    if package is None:
        return Verdict(False, f'{NAME}: pyproject.toml declares no [project] version = "…"')
    failed = _heading_verdict(readme, package)
    if failed is not None:
        return failed
    code = _code_version(init)
    if code is not None and code != package:
        return Verdict(False, f"{NAME}: {INIT} says __version__ {code}, the package is version {package}")
    return Verdict(True, f"{NAME}: README.md 'Supports' names version {package}, as the package does")


def main(argv: Sequence[str] | None = None) -> int:
    """Check the tree at ROOT (default: this script's package); print the verdict, exit 0 or 1."""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else ROOT
    try:
        readme = (root / "README.md").read_text(encoding="utf-8")
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        init = (root / INIT).read_text(encoding="utf-8") if (root / INIT).is_file() else None
    except OSError as exc:
        print(f"{NAME}: cannot read {exc.filename}: {exc.strerror}", file=sys.stderr)
        return 1
    verdict = judge(readme, pyproject, init)
    print(verdict.message, file=sys.stdout if verdict.ok else sys.stderr)
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
