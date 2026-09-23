"""The plugin boundary (ADR-0004): what a source is, what a plugin exports, how plugins are found.

Every input — the built-in ones included — is a ``Source`` listed by a ``Plugin``. The core discovers plugins through
the ``ai_cost.plugins`` entry-point group (pip / pipx installs) and through module names the user lists in the config
(``plugins``) or ``AI_COST_PLUGINS`` (zipapp bundles, ``PYTHONPATH``). A plugin that cannot be loaded is a counted
warning shown by ``doctor``; it never aborts a report.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .config import Config, Paths, PriceBook
from .errors import ToolError
from .models import Collected, Skipped, UsageRow, Window

if TYPE_CHECKING:
    from .ops import ReportRequest

API_VERSION = 1
ENTRY_POINT_GROUP = "ai_cost.plugins"
PLUGINS_ENV = "AI_COST_PLUGINS"
EXPORT = "PLUGIN"
BUNDLE_MODULE = "ai_cost_bundle"  # written by the build when plugin packages are packed into the zipapp


@dataclass(frozen=True)
class Context:
    """What every source and enricher sees for one report."""

    paths: Paths
    config: Config
    window: Window
    request: ReportRequest
    settings: Mapping[str, Any] = field(default_factory=dict)  # the plugin's own section of the config
    skipped: list[Skipped] = field(
        default_factory=list
    )  # shared sink: what a source or enricher could not read
    warnings: list[str] = field(
        default_factory=list
    )  # shared sink: report-header warnings a plugin wants shown
    book: PriceBook | None = None  # the run's pricebook: a source that validates against list prices reads it


class Source(Protocol):
    """One input: reads what is on disk (or reachable) for the window and returns rows."""

    name: str

    def collect(self, ctx: Context) -> Collected:
        """Rows, work items and skips for the context's window."""
        ...


class Enricher(Protocol):
    """An optional pass over all rows after collection (scope inheritance, ties); must be idempotent."""

    name: str

    def enrich(self, rows: list[UsageRow], ctx: Context) -> list[UsageRow]:
        """One output row per input row, in order: the same rows, some replaced (``dataclasses.replace``).

        Never a drop or a copy — the core checks the count, the order is the plugin's contract.
        """
        ...


Line = Callable[[bool, str], None]  # doctor: (ok, text)


@dataclass(frozen=True)
class Plugin:
    """What a plugin module exports as ``PLUGIN``."""

    name: str
    sources: tuple[Source, ...] = ()
    enrichers: tuple[Enricher, ...] = ()
    doctor: Callable[[Context, Line], None] | None = None
    tests_package: str = (
        ""  # a tests package (``pkg.tests``) that ``ai-cost selftest`` runs when the plugin is loaded
    )
    api_version: int = API_VERSION


@dataclass
class Loaded:
    """Plugins in load order plus every problem met on the way (each one also a ``doctor`` line)."""

    plugins: list[Plugin] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def entry_point_modules(group: str = ENTRY_POINT_GROUP) -> list[str]:
    """Module names registered under the entry-point group (Python 3.9-safe selection)."""
    found: Any = importlib.metadata.entry_points()  # 3.10+: EntryPoints with .select(); 3.9: a dict of lists
    points = found.select(group=group) if hasattr(found, "select") else found.get(group, [])
    return [str(point.value).split(":")[0] for point in points]


def _core_root() -> str:
    """The directory (or zipapp) holding the ``ai_cost`` package: the only place a bundle marker may come from.

    Symlinks are resolved on both sides of the comparison, so a zipapp reached through a linked directory
    (``/tmp`` on macOS, a linked ``~/bin``) still recognises its own marker.
    """
    return os.path.normpath(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def _packed_next_to_core(module: str) -> bool:
    """Whether ``module`` resolves to a file right next to the ``ai_cost`` package, without importing it."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):  # a missing parent package, or a sys.modules entry without a spec
        return False
    if spec is None or not spec.origin:
        return False
    return os.path.normpath(os.path.dirname(os.path.realpath(spec.origin))) == _core_root()


def bundled_modules(module: str = BUNDLE_MODULE) -> list[str]:
    """Plugin modules the build packed next to the core (``ai_cost_bundle.PLUGINS``); none outside such a build.

    A module of that name anywhere else (the working directory, ``PYTHONPATH``) is somebody else's: it is never
    imported, because importing it would run it.
    """
    if not _packed_next_to_core(module):
        return []
    try:
        bundle = importlib.import_module(module)
    except Exception as exc:  # the build's own marker is broken: said once, never a traceback
        raise ToolError(
            f"the plugin bundle marker {module} cannot be imported ({exc.__class__.__name__}: {exc})"
        ) from exc
    listed = getattr(bundle, "PLUGINS", ())
    return [str(name) for name in listed] if isinstance(listed, (list, tuple)) else []


def configured_modules(configured: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    """Module names from the config list and the ``AI_COST_PLUGINS`` variable (comma-separated)."""
    from_env = [name.strip() for name in environ.get(PLUGINS_ENV, "").split(",") if name.strip()]
    return [*configured, *from_env]


def load_module(name: str) -> Plugin | str:
    """The plugin a module exports, or the reason it could not be used."""
    try:
        module = importlib.import_module(name)
        plugin = getattr(
            module, EXPORT, None
        )  # a module-level __getattr__ that raises is counted like a failed import
    except Exception as exc:
        return f"plugin {name}: cannot import ({exc.__class__.__name__}: {exc})"
    if not isinstance(plugin, Plugin):
        return f"plugin {name}: no {EXPORT} export of type ai_cost.plugins.Plugin"
    if plugin.api_version != API_VERSION:
        return f"plugin {name}: api_version {plugin.api_version}, this ai-cost speaks {API_VERSION}"
    return plugin


def load_plugins(modules: Sequence[str]) -> Loaded:
    """Load each module once, in order; duplicates are skipped, failures counted."""
    loaded = Loaded()
    seen: set[str] = set()
    for name in modules:
        if name in seen:
            continue
        seen.add(name)
        result = load_module(name)
        if isinstance(result, Plugin):
            loaded.plugins.append(result)
        else:
            loaded.warnings.append(result)
    return loaded
