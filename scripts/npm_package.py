"""Assemble the npm package ``ai-costs``: the zipapp the Python distribution ships, started by a small Node launcher.

    python3 scripts/npm_package.py OUT --zipapp dist/ai-cost

writes OUT/package.json (every field from pyproject.toml, the one source: version, description, keywords, license,
author, homepage, repository, issues), OUT/bin/ai-cost.js, OUT/ai-cost.pyz (the zipapp, byte for byte), OUT/README.md
and OUT/LICENSE. The package has no install scripts and no dependencies: installing it runs nothing. ``npm pack`` in
OUT makes the tarball the release carries.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "ai-costs"  # as on PyPI; npm refuses `ai-cost` next to the existing `aicost` (punctuation is ignored)
ZIPAPP = "ai-cost.pyz"


@dataclass(frozen=True)
class Project:
    """What the package metadata takes from pyproject.toml."""

    version: str
    description: str
    keywords: tuple[str, ...]
    license: str
    author: str  # "name <email>"
    homepage: str
    repository: str
    issues: str


def read_project(pyproject: Path) -> Project:
    """The ``[project]`` fields the package needs; a missing one is a ``ValueError`` naming it."""
    text = pyproject.read_text(encoding="utf-8")
    author = re.search(r'name = "([^"]+)", email = "([^"]+)"', _raw(text, "authors", r"\[.*\]"))
    if author is None:
        raise ValueError("pyproject.toml authors has no name and email")
    return Project(
        version=_string(text, "version"),
        description=_string(text, "description"),
        keywords=tuple(re.findall(r'"([^"]+)"', _raw(text, "keywords", r"\[[^\]]*\]"))),
        license=_string(text, "license"),
        author=f"{author.group(1)} <{author.group(2)}>",
        homepage=_string(text, "Homepage"),
        repository=_string(text, "Repository"),
        issues=_string(text, "Issues"),
    )


def requires_python(pyproject: Path) -> tuple[int, int]:
    """The ``>=X.Y`` floor of ``requires-python`` — the launcher's minimum must equal it."""
    floor = re.fullmatch(r">=(\d+)\.(\d+)", _string(pyproject.read_text(encoding="utf-8"), "requires-python"))
    if floor is None:
        raise ValueError("pyproject.toml requires-python is not >=X.Y")
    return int(floor.group(1)), int(floor.group(2))


def _string(text: str, key: str) -> str:
    return _raw(text, key, r'"[^"]*"')[1:-1]


def _raw(text: str, key: str, value: str) -> str:
    """The value of a ``key = value`` line of pyproject.toml, as written."""
    match = re.search(rf"^{re.escape(key)} = ({value})$", text, re.M)
    if match is None:
        raise ValueError(f"pyproject.toml has no {key}")
    return match.group(1)


def manifest(project: Project) -> dict[str, object]:
    """``package.json``: one command, the files it needs, no scripts and no dependencies."""
    return {
        "name": NAME,
        "version": project.version,
        "description": project.description,
        "keywords": list(project.keywords),
        "homepage": project.homepage,
        "repository": {"type": "git", "url": f"git+{project.repository}.git"},
        "bugs": {"url": project.issues},
        "license": project.license,
        "author": project.author,
        "bin": {"ai-cost": "bin/ai-cost.js"},
        "files": ["bin/", ZIPAPP, "README.md", "LICENSE"],
        "engines": {"node": ">=18"},
    }


def assemble(out: Path, zipapp: Path, root: Path = ROOT) -> Project:
    """Write the package into ``out`` (absent or an empty directory; never overwritten); returns what it was built from."""
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        raise FileExistsError(f"{out} exists and is not an empty directory (a link is refused too)")
    sources = [root / "npm" / "bin" / "ai-cost.js", root / "npm" / "README.md", root / "LICENSE", zipapp]
    missing = [str(path) for path in sources if not path.is_file()]
    if (
        missing
    ):  # checked before anything is written: a refused package leaves no half-written directory behind
        raise ValueError(f"missing {', '.join(missing)}")
    project = read_project(root / "pyproject.toml")
    (out / "bin").mkdir(parents=True, exist_ok=True)
    (out / "package.json").write_text(json.dumps(manifest(project), indent=2) + "\n", encoding="utf-8")
    launcher = out / "bin" / "ai-cost.js"
    shutil.copyfile(root / "npm" / "bin" / "ai-cost.js", launcher)
    launcher.chmod(0o755)
    shutil.copyfile(zipapp, out / ZIPAPP)
    shutil.copyfile(root / "npm" / "README.md", out / "README.md")
    shutil.copyfile(root / "LICENSE", out / "LICENSE")
    return project


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 with the package written, 2 when the zipapp is missing, the destination is taken or pyproject lacks a key."""
    parser = argparse.ArgumentParser(prog="npm_package.py", description="Assemble the npm package ai-costs.")
    parser.add_argument("out", type=Path, help="destination directory (absent or empty)")
    parser.add_argument("--zipapp", type=Path, required=True, help="the zipapp scripts/build.py wrote")
    args = parser.parse_args(argv)
    if not args.zipapp.is_file():
        print(
            f"npm_package: no zipapp at {args.zipapp} (python3 scripts/build.py src dist/ai-cost)",
            file=sys.stderr,
        )
        return 2
    try:
        project = assemble(args.out, args.zipapp)
    except (OSError, ValueError) as exc:
        print(f"npm_package: {exc}", file=sys.stderr)
        return 2
    print(f"npm_package: {NAME}@{project.version} → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
