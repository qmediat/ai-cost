"""The shipped selftest runs every test without pytest: a test may take only the fixtures it provides."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def test_every_shipped_test_takes_only_the_fixtures_the_selftest_provides() -> None:
    from .. import tests
    from ..selftest import unsupported_fixtures

    found = unsupported_fixtures(tests)
    assert not found, f"pytest provides them, `ai-cost selftest` does not: {found}"


def test_a_test_asking_for_another_fixture_is_named(tmp_path: Path) -> None:
    from ..selftest import unsupported_fixtures

    package = tmp_path / "fixture_probe"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "test_probe.py").write_text(
        "def test_prints(capsys):\n    pass\n\n\ndef test_writes(tmp_path):\n    pass\n\n\n"
        "def test_defaults(x=1, *args, **kwargs):\n    pass\n\n\ntest_cases = [1, 2]\n\n\n"
        "class TestGroup:\n    def test_in_a_class(self):\n        pass\n\n\n"
        "def test_marked():\n    pass\n\n\ntest_marked.pytestmark = ['parametrize']\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        probe = importlib.import_module("fixture_probe")
        assert unsupported_fixtures(probe) == [
            "test_probe.TestGroup (a test class)",
            "test_probe.test_marked (pytest marks)",
            "test_probe.test_prints (capsys)",
        ], "a default, *args, a list: no fixture"
    finally:
        sys.path.remove(str(tmp_path))
        for name in [n for n in sys.modules if n.startswith("fixture_probe")]:
            del sys.modules[name]


def test_the_runner_names_a_fixture_it_does_not_provide() -> None:
    from ..selftest import _call

    def test_prints(capsys: object) -> None:
        pass

    try:
        _call(test_prints)
    except TypeError as exc:
        assert "asks for capsys, which the selftest does not provide (tmp_path only)" in str(exc)
    else:
        raise AssertionError("a test asking for capsys cannot run here")
