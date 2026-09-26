"""Provider day reports (ADR-0008): each configured source reads the days of a window, exact, with what it lacks.

A source is named under ``providers.<name>.report`` in the config; without it nothing is read. A reader never raises
past the report: what it cannot read comes back as a missing or unreadable span with the reason, never as a zero.
Keys come from the environment only; a config holds no secret.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from ..models import Provider, ProviderDays, Span, Window
from . import alibaba_bills, deepseek_export, google_billing, xai_usage
from .common import redact


@dataclass(frozen=True)
class ReportSource:
    """One ``providers.<name>.report``: the source, and the settings its reader takes (checked by ``SETTINGS``)."""

    provider: str
    source: str
    settings: Mapping[str, str | tuple[str, ...]] = field(default_factory=dict)

    def text(self, key: str, default: str = "") -> str:
        """A text setting."""
        value = self.settings.get(key, default)
        return value if isinstance(value, str) else default

    def names(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        """A list setting."""
        value = self.settings.get(key, default)
        return value if isinstance(value, tuple) else default


@dataclass(frozen=True)
class ReadContext:
    """What every reader of one report shares: the state directory, the window, the time, the network, the keys."""

    state_dir: Path
    window: Window
    read_at: datetime
    offline: bool
    environ: Mapping[str, str]


Reader = Callable[[ReportSource, ReadContext], ProviderDays]
SECRET_ENVS = (xai_usage.KEY_ENV, alibaba_bills.KEY_ID_ENV, alibaba_bills.SECRET_ENV)  # never in a message
TEXT, NAMES = "text", "names"


def _deepseek(source: ReportSource, ctx: ReadContext) -> ProviderDays:
    return deepseek_export.read_days(ctx.state_dir, ctx.window)


def _xai(source: ReportSource, ctx: ReadContext) -> ProviderDays:
    key = ctx.environ.get(xai_usage.KEY_ENV, "")
    request = xai_usage.XaiRequest(source.text("team"), key, ctx.window, ctx.read_at, ctx.offline)
    return xai_usage.read_days(request)


def _google(source: ReportSource, ctx: ReadContext) -> ProviderDays:
    services = source.names("services", google_billing.DEFAULT_SERVICES)
    request = google_billing.GoogleRequest(
        source.text("table"), services, ctx.window, ctx.read_at, ctx.offline
    )
    return google_billing.read_days(request)


def _alibaba(source: ReportSource, ctx: ReadContext) -> ProviderDays:
    request = alibaba_bills.AlibabaRequest(
        key_id=ctx.environ.get(alibaba_bills.KEY_ID_ENV, ""),
        secret=ctx.environ.get(alibaba_bills.SECRET_ENV, ""),
        window=ctx.window,
        read_at=ctx.read_at,
        endpoint=source.text("endpoint", alibaba_bills.ENDPOINT),
        products=source.names("products"),
        offline=ctx.offline,
    )
    return alibaba_bills.read_days(request)


READERS: Mapping[tuple[str, str], Reader] = {
    ("deepseek", "import"): _deepseek,
    ("xai", xai_usage.SOURCE): _xai,
    ("google", google_billing.SOURCE): _google,
    ("alibaba", alibaba_bills.SOURCE): _alibaba,
}
# the sources whose days are UTC days whatever they answer, a failed read included (a DeepSeek import states its own)
UTC_DAY_SOURCES = frozenset({("xai", xai_usage.SOURCE), ("google", google_billing.SOURCE)})
# the settings each source takes, with their kind and whether they are required; any other key is a ConfigError
SETTINGS: Mapping[tuple[str, str], Mapping[str, tuple[str, bool]]] = {
    ("deepseek", "import"): {},
    ("xai", xai_usage.SOURCE): {"team": (TEXT, True)},
    ("google", google_billing.SOURCE): {"table": (TEXT, True), "services": (NAMES, False)},
    ("alibaba", alibaba_bills.SOURCE): {"endpoint": (TEXT, False), "products": (NAMES, False)},
}


def sources_of(name: str) -> tuple[str, ...]:
    """The sources a provider can name (for the config's error message)."""
    return tuple(source for provider, source in READERS if provider == name)


def read_reports(reports: Mapping[str, ReportSource], ctx: ReadContext) -> list[ProviderDays]:
    """Every configured provider's days for the window, in the config's order."""
    return [_guarded(source, ctx) for source in reports.values()]


def _guarded(source: ReportSource, ctx: ReadContext) -> ProviderDays:
    """One reader, which never raises past the report: whatever it could not handle is an unreadable span, said.

    The readers catch the failures they know; this is the last line for the ones nobody foresaw (a transport error of
    another class, a malformed answer deep in a parser). An exception can quote what it refused — ``http.client``
    quotes a header value with a line break, the bearer key in it — so every key the readers use is masked first. A
    source of ``UTC_DAY_SOURCES`` keeps its UTC days when it fails, so ``daily`` reads that day again.
    """
    key = (source.provider, source.source)
    try:
        found = READERS[key](source, ctx)
    except Exception as exc:  # said as an unreadable span, never swallowed
        secrets = [ctx.environ.get(name, "") for name in SECRET_ENVS]
        why = redact(f"the {source.source} reader failed: {exc.__class__.__name__}: {exc}", secrets)
        span = Span(ctx.window.start, ctx.window.end, why)
        found = ProviderDays(Provider.of(source.provider), source.source, unreadable=(span,))
    return replace(found, utc_days=True) if key in UTC_DAY_SOURCES else found
