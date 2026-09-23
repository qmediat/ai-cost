"""Pack ``src/`` into one executable zipapp with fixed timestamps, so the same sources give the same bytes.

``python -m zipapp`` stamps its generated ``__main__.py`` with the current time; this builder writes every entry —
the package files and the entry point — with one constant date, sorted, so ``build.sh --check`` can compare a
rebuild byte for byte.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

STAMP = (2026, 1, 1, 0, 0, 0)
ENTRY = "import ai_cost.__main__\nai_cost.__main__.run()\n"
SKIP_DIRS = {"__pycache__"}


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=STAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def _packed(path: Path) -> bool:
    """A source file that belongs in the archive: no caches, no build metadata (``*.egg-info``), no bytecode."""
    parts = set(path.parts)
    return (
        path.is_file()
        and not (SKIP_DIRS & parts)
        and path.suffix != ".pyc"
        and not any(part.endswith(".egg-info") for part in parts)
    )


def _files(src: Path) -> list[Path]:
    return sorted(p for p in src.rglob("*") if _packed(p))


def _bundle_marker(extra: list[Path]) -> str:
    """``ai_cost_bundle.py``: the plugin modules packed next to the core (their top-level packages)."""
    names = sorted({p.name for src in extra for p in src.iterdir() if (p / "__init__.py").is_file()})
    return "PLUGINS = " + repr(tuple(names)) + "\n"


def build(src: Path, out: Path, extra: list[Path] | None = None) -> None:
    """Write ``out`` = shebang + zip of every file under ``src`` and ``extra`` (caches skipped) + the entry point.

    With ``extra`` trees the archive also carries ``ai_cost_bundle.py`` naming their packages, so the core loads
    them as plugins without any configuration; without, the marker is absent and nothing is loaded.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        handle.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(handle, "w") as archive:
            _fill(archive, [src, *(extra or [])], extra or [])
    out.chmod(0o755)


def _fill(archive: zipfile.ZipFile, trees: list[Path], extra: list[Path]) -> None:
    """Every file of every tree, then the bundle marker (bundled builds only), then the entry point."""
    for tree in trees:
        for path in _files(tree):
            archive.writestr(_entry(path.relative_to(tree).as_posix()), path.read_bytes())
    if extra:
        archive.writestr(_entry("ai_cost_bundle.py"), _bundle_marker(extra))
    archive.writestr(_entry("__main__.py"), ENTRY)


def main(argv: list[str]) -> int:
    """``build.py <src> <out> [--with <dir>]...``: pack the trees into ``out``."""
    if len(argv) < 3:
        sys.stderr.write("usage: build.py <src-dir> <out-file> [--with <src-dir>]...\n")
        return 2
    extra = [Path(argv[i + 1]) for i, arg in enumerate(argv) if arg == "--with" and i + 1 < len(argv)]
    build(Path(argv[1]), Path(argv[2]), extra)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
