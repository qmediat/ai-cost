"""The shipped tests without pytest: every ``test_*`` function of every ``ai_cost.tests.test_*`` module, one line each.

The only fixture the runner provides is ``tmp_path`` (a temporary directory per test); ``unsupported_fixtures`` names a
test that asks for another one, which pytest would provide in CI and this runner cannot.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import tempfile
from contextlib import AbstractContextManager, ExitStack
from pathlib import Path
from types import ModuleType
from typing import Callable

from . import tests as tests_package

Emit = Callable[[str], None]


def _tmp_path() -> AbstractContextManager[str]:
    return tempfile.TemporaryDirectory(prefix="ai-cost-selftest-")


FIXTURES: dict[str, Callable[[], AbstractContextManager[str]]] = {"tmp_path": _tmp_path}  # name → a fresh one


def _test_modules(package: ModuleType) -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules(package.__path__) if m.name.startswith("test_"))


def _tests(module: ModuleType) -> list[tuple[str, Callable[..., None]]]:
    """The module's tests as pytest collects them: callable ``test_*`` attributes."""
    found = [(name, getattr(module, name)) for name in sorted(dir(module)) if name.startswith("test_")]
    return [(name, test) for name, test in found if callable(test)]


def _asked(test: Callable[..., None]) -> list[str]:
    """The fixtures a test asks for: its parameters without a default (not ``*args`` / ``**kwargs``).

    A callable whose signature cannot be read asks for none (the runner then calls it bare, as pytest would fail it).
    """
    kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    try:
        parameters = inspect.signature(test).parameters.values()
    except (TypeError, ValueError):
        return []
    return [p.name for p in parameters if p.kind in kinds and p.default is inspect.Parameter.empty]


def _uncollected(package: ModuleType, module: ModuleType, module_name: str) -> list[str]:
    """What pytest would collect in the module and this runner would not: a ``Test*`` class, a marked test."""
    names = sorted(dir(module))
    classes = [
        f"{module_name}.{n} (a test class)"
        for n in names
        if n.startswith("Test") and inspect.isclass(getattr(module, n))
    ]
    marked = [
        f"{module_name}.{n} (pytest marks)" for n, test in _tests(module) if hasattr(test, "pytestmark")
    ]
    return classes + marked


def unsupported_fixtures(package: ModuleType) -> list[str]:
    """``module.test (fixture)`` for every test of the package that asks for what the runner cannot provide.

    pytest provides every fixture, marks and test classes, so such a test passes in CI and fails (or is never run) in
    the shipped selftest; this is the check that says so in CI. A test subpackage is named too: the runner reads the
    package's own modules only.
    """
    found = [f"{m.name} (a test subpackage)" for m in pkgutil.iter_modules(package.__path__) if m.ispkg]
    for module_name in _test_modules(package):
        module = importlib.import_module(f"{package.__name__}.{module_name}")
        found += _uncollected(package, module, module_name)
        for name, test in _tests(module):
            found += [
                f"{module_name}.{name} ({fixture})" for fixture in _asked(test) if fixture not in FIXTURES
            ]
    return found


def _call(check: Callable[..., None]) -> None:
    asked = _asked(check)
    missing = [name for name in asked if name not in FIXTURES]
    if missing:
        raise TypeError(
            f"asks for {', '.join(missing)}, which the selftest does not provide ({', '.join(FIXTURES)} only)"
        )
    with ExitStack() as stack:
        check(**{name: Path(stack.enter_context(FIXTURES[name]())) for name in asked})


def _run_module(package: ModuleType, module_name: str, emit: Emit, verbose: bool) -> tuple[int, int]:
    """Run every ``test_*`` of one module; a crashing test is a failed test and the run continues."""
    module = importlib.import_module(f"{package.__name__}.{module_name}")
    passed = failed = 0
    for name, test in _tests(module):
        try:
            _call(test)
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
    for module_name in _test_modules(package):
        ok, bad = _run_module(package, module_name, emit, verbose)
        passed += ok
        failed += bad
    emit(f"{label}: {passed} passed, {failed} failed")
    return 1 if failed else 0


def run_selftest(emit: Emit, verbose: bool = False) -> int:
    """The package's own tests."""
    return run_package(tests_package, emit, verbose)
