"""The plugin boundary: discovery from names, failures counted, duplicates skipped, api_version checked."""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import sys
import textwrap
import types
from pathlib import Path

from .. import plugins as plugins_module
from ..plugins import API_VERSION, bundled_modules, configured_modules, load_module, load_plugins


def _module(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(body))
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))


def test_a_plugin_module_is_loaded_once_and_a_bad_one_is_a_counted_warning(tmp_path: Path) -> None:
    _module(
        tmp_path,
        "good_plugin",
        """
        from ai_cost.plugins import Plugin
        PLUGIN = Plugin(name="good")
        """,
    )
    _module(tmp_path, "no_export", "x = 1\n")
    _module(tmp_path, "broken_plugin", "raise RuntimeError('boom')\n")
    loaded = load_plugins(["good_plugin", "good_plugin", "no_export", "broken_plugin", "missing_plugin"])
    assert [p.name for p in loaded.plugins] == ["good"], "loaded once despite being listed twice"
    assert len(loaded.warnings) == 3 and all("plugin " in w for w in loaded.warnings)
    assert any("no PLUGIN export" in w for w in loaded.warnings)
    assert any("boom" in w for w in loaded.warnings)


def test_an_api_version_mismatch_is_refused_with_a_message(tmp_path: Path) -> None:
    _module(
        tmp_path,
        "old_plugin",
        f"""
        from ai_cost.plugins import Plugin
        PLUGIN = Plugin(name="old", api_version={API_VERSION + 1})
        """,
    )
    result = load_module("old_plugin")
    assert isinstance(result, str) and f"speaks {API_VERSION}" in result


def test_configured_modules_join_the_config_list_and_the_environment() -> None:
    names = configured_modules(["a", "b"], {"AI_COST_PLUGINS": " c , ,d"})
    assert names == ["a", "b", "c", "d"]
    assert configured_modules([], {}) == []


def test_bundled_modules_come_from_the_build_marker_next_to_the_core_only(tmp_path: Path) -> None:
    assert bundled_modules("ai_cost_bundle_that_does_not_exist") == []
    core_root = Path(plugins_module.__file__).resolve().parent.parent
    linked_root = tmp_path / "core-link"
    # The same directory spelled through a symlink: the marker check compares physical paths on both sides.
    os.symlink(core_root, linked_root, target_is_directory=True)
    cases: tuple[tuple[str, Path, list[str]], ...] = (
        ("ai_cost_bundle_fake", core_root / "ai_cost_bundle_fake.py", ["acme.cost", "acme_extras"]),
        ("ai_cost_bundle_linked", linked_root / "ai_cost_bundle_linked.py", ["acme.cost", "acme_extras"]),
        (
            "ai_cost_bundle_alien",
            tmp_path / "ai_cost_bundle_alien.py",
            [],
        ),  # the cwd or PYTHONPATH: somebody else's
    )
    for name, origin, expected in cases:
        module = types.ModuleType(name)
        module.PLUGINS = ("acme.cost", "acme_extras")  # type: ignore[attr-defined]
        module.__spec__ = importlib.machinery.ModuleSpec(name, None, origin=str(origin))
        module.__file__ = str(origin)
        sys.modules[name] = module
        try:
            assert bundled_modules(name) == expected, f"{name} at {origin}"
        finally:
            del sys.modules[name]


def test_a_bundle_marker_that_fails_to_import_is_a_tool_error() -> None:
    from unittest import mock

    from ..errors import ToolError

    core_root = Path(plugins_module.__file__).resolve().parent.parent
    spec = importlib.machinery.ModuleSpec(
        "ai_cost_bundle_broken", None, origin=str(core_root / "ai_cost_bundle_broken.py")
    )
    with (
        mock.patch.object(importlib.util, "find_spec", return_value=spec),
        mock.patch.object(importlib, "import_module", side_effect=RuntimeError("boom")),
    ):
        try:
            bundled_modules("ai_cost_bundle_broken")
        except ToolError as exc:
            assert "ai_cost_bundle_broken" in str(exc) and "boom" in str(exc)
        else:
            raise AssertionError("a broken marker next to the core must be a ToolError, never a traceback")


def test_a_module_whose_plugin_attribute_raises_is_a_counted_warning() -> None:
    module = types.ModuleType("ai_cost_plugin_moody")

    def moody(name: str) -> object:
        raise RuntimeError("not today")

    module.__dict__["__getattr__"] = moody  # a module-level __getattr__, set the way a module would
    sys.modules["ai_cost_plugin_moody"] = module
    try:
        result = load_module("ai_cost_plugin_moody")
    finally:
        del sys.modules["ai_cost_plugin_moody"]
    assert isinstance(result, str) and "cannot import" in result and "not today" in result, result
