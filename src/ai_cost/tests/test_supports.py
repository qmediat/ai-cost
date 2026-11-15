"""The README's ``## Supports · version X.Y.Z`` heading and ``__version__`` carry the package version.

The check is ``scripts/check_supports.py``. A checkout carries it; an installed package, the zipapp and an unpacked
sdist do not: there every test here returns after one line on stderr says the check was not run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_supports.py"
PROJECT = '[project]\nname = "fixture"\nversion = "1.2.3"\n'
_SAID: list[bool] = []  # the "not run" line is said once per run, not once per test


def _script() -> Path | None:
    """The check, or None — said on stderr — where this tree carries no ``scripts/check_supports.py``."""
    if (ROOT / "scripts").is_dir():
        assert SCRIPT.is_file(), "a checkout's scripts/ carries the Supports check"
    if SCRIPT.is_file():
        return SCRIPT
    if not _SAID:
        _SAID.append(True)
        print(
            "test_supports: no scripts/check_supports.py in this tree: the Supports check is not run",
            file=sys.stderr,
        )
    return None


def _check(root: Path) -> subprocess.CompletedProcess[str] | None:
    """Run the check on ROOT; None where there is no check to run."""
    script = _script()
    if script is None:
        return None
    return subprocess.run(
        [sys.executable, str(script), str(root)], capture_output=True, text=True, check=False, timeout=60
    )


def _tree(tmp_path: Path, readme: str, pyproject: str = PROJECT, init: str | None = None) -> Path:
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    if init is not None:
        (tmp_path / "src" / "ai_cost").mkdir(parents=True)
        (tmp_path / "src" / "ai_cost" / "__init__.py").write_text(init, encoding="utf-8")
    return tmp_path


def test_a_supports_heading_with_the_package_version_passes(tmp_path: Path) -> None:
    run = _check(_tree(tmp_path, "# x\n\n## Supports · version 1.2.3\n\n| Reads | … |\n"))
    if run is None:
        return
    assert run.returncode == 0, run.stderr
    assert "1.2.3" in run.stdout


def test_a_supports_heading_with_another_version_fails_naming_both(tmp_path: Path) -> None:
    run = _check(_tree(tmp_path, "# x\n\n## Supports · version 1.2.2\n"))
    if run is None:
        return
    assert run.returncode == 1
    assert "1.2.2" in run.stderr and "1.2.3" in run.stderr, run.stderr


def test_a_readme_without_the_supports_heading_fails(tmp_path: Path) -> None:
    run = _check(_tree(tmp_path, "# x\n\n## Supports\n\nversion 1.2.3\n"))
    if run is None:
        return
    assert run.returncode == 1
    assert "0 '## Supports · version …' headings" in run.stderr, run.stderr


def test_the_package_version_is_the_project_tables_not_an_earlier_tables(tmp_path: Path) -> None:
    pyproject = '[tool.other]\nversion = "9.9.9"\n\n' + PROJECT + '\n[tool.later]\nversion = "8.8.8"\n'
    run = _check(_tree(tmp_path, "## Supports · version 1.2.3\n", pyproject))
    if run is None:
        return
    assert run.returncode == 0, run.stderr


def test_a_code_version_that_differs_from_the_package_fails_naming_both(tmp_path: Path) -> None:
    run = _check(_tree(tmp_path, "## Supports · version 1.2.3\n", init='__version__ = "1.2.2"\n'))
    if run is None:
        return
    assert run.returncode == 1
    assert "__version__ 1.2.2" in run.stderr and "1.2.3" in run.stderr, run.stderr


def test_a_code_version_equal_to_the_package_passes(tmp_path: Path) -> None:
    run = _check(_tree(tmp_path, "## Supports · version 1.2.3\n", init='"""x"""\n\n__version__ = "1.2.3"\n'))
    if run is None:
        return
    assert run.returncode == 0, run.stderr


def test_the_shipped_readme_and_code_name_the_package_version() -> None:
    run = _check(ROOT)
    if run is None:
        return
    assert run.returncode == 0, run.stderr
