"""The shipped tests without pytest: every ``test_*`` function of every ``ai_cost.tests.test_*`` module, one line each.

Fixtures are the same the pytest run uses (``tmp_path`` is replaced by a temporary directory per test).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Callable

from . import tests as tests_package

Emit = Callable[[str], None]


def _call(check: Callable[..., None]) -> None:
    parameters = inspect.signature(check).parameters
    if "tmp_path" in parameters:
        with tempfile.TemporaryDirectory(prefix="ai-cost-selftest-") as tmp:
            check(tmp_path=Path(tmp))
    else:
        check()


def _run_module(package: ModuleType, module_name: str, emit: Emit, verbose: bool) -> tuple[int, int]:
    """Run every ``test_*`` of one module; a crashing test is a failed test and the run continues."""
    module = importlib.import_module(f"{package.__name__}.{module_name}")
    passed = failed = 0
    for name in sorted(n for n in dir(module) if n.startswith("test_")):
        try:
            _call(getattr(module, name))
        except Exception as exc:
            failed += 1
            emit(f"  FAIL {module_name}.{name}: {exc.__class__.__name__}: {exc}")
            continue
        passed += 1
        if verbose:
            emit(f"  PASS {module_name}.{name}")
    return passed, failed


def run_package(
    package: ModuleType, emit: Emit, verbose: bool = False, label: str = "ai-cost selftest"
) -> int:
    """Run every ``test_*`` module of a tests package; exit 1 on any failure. ``verbose`` prints the passes too."""
    passed = failed = 0
    for module_info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if not module_info.name.startswith("test_"):
            continue
        ok, bad = _run_module(package, module_info.name, emit, verbose)
        passed += ok
        failed += bad
    emit(f"{label}: {passed} passed, {failed} failed")
    return 1 if failed else 0


def run_selftest(emit: Emit, verbose: bool = False) -> int:
    """The package's own tests."""
    return run_package(tests_package, emit, verbose)
