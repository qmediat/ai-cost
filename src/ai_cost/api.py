"""The library interface: an engine an app embeds, bound to one installation of the app (docs/EMBED.md).

``__all__`` is the interface and ``tests/data/api-surface.json`` pins it. The engine adapts these types to the tool's
own functions and keeps no logic of its own; ``ai-cost --instance DIR …`` is the same engine for every other language
and prints the same JSON.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .collectors.usage_log import (
    REQUIRED,
    event_ids,
    line_event_id,
    line_header,
    parse_line,
    writable_segment,
)
from .config import Config, Instance, Paths, PriceBook, load_config, load_pricebook
from .doctor import doctor_checks
from .errors import ApiError, ToolError
from .groups import Report
from .log import MAX_LINE_BYTES, Attribution, Usage, append, entry, validated
from .models import Provider
from .onboarding import Mark
from .ops import INSTANCE_GROUPS, ReportRequest, build_report
from .pricing import how_priced
from .pricing import price as list_price
from .render import render_json
from .timeutil import iso, now, zoned

__all__ = ["ApiError", "Call", "Engine", "Instance", "Period", "Price", "Report", "SkipClass", "UsageImport"]

REPORTED = "reported by the caller"
GROUPS = frozenset({"real", "api", "vendor"})
_KEYS = frozenset(f.name for f in fields(Attribution)) - {"labels"}  # Call.labels carries those


def _utc(at: Any, what: str) -> datetime:
    """An aware time in UTC at second precision; a naive one or another type is refused, naming the field."""
    if not isinstance(at, datetime):
        raise ApiError(f"{what} must be a datetime, got {type(at).__name__}")
    if at.tzinfo is None or at.utcoffset() is None:
        raise ApiError(f"{what} must be timezone-aware, got {at.isoformat()}")
    return at.astimezone(timezone.utc).replace(microsecond=0)


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ApiError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class Period:
    """What a report covers: ``since`` and ``until`` (aware, the app's own boundaries), or the last ``hours``."""

    since: datetime | None = None
    until: datetime | None = None
    hours: float | None = None

    def __post_init__(self) -> None:
        bounds = (self.since, self.until)
        if (self.hours is None) == (bounds == (None, None)):
            raise ApiError("a period is since + until, or hours: exactly one of the two forms")
        if self.hours is None and None in bounds:
            raise ApiError("a period by dates needs both since and until")
        hours = self.hours
        if hours is not None and (isinstance(hours, bool) or not isinstance(hours, (int, float))):
            raise ApiError(f"Period.hours must be a number, got {type(hours).__name__}")
        if hours is not None and not math.isfinite(hours):
            raise ApiError(f"Period.hours must be finite, got {hours!r}")
        for name in ("since", "until"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value, f"Period.{name}"))


@dataclass(frozen=True)
class Call:
    """One request an app made, in the provider's own counter names (docs/USAGE-LOG.md)."""

    provider: str
    model: str
    tokens: Mapping[str, int] = field(default_factory=dict)
    cost: float | None = None  # what the request was charged, when the app knows
    at: datetime | None = None  # aware; default: now
    labels: Mapping[str, str] = field(default_factory=dict)  # exact names and values: tenant, feature, …
    keys: Mapping[str, Any] = field(
        default_factory=dict
    )  # the line's optional keys: ref, source, event_id, …

    def __post_init__(self) -> None:
        for name in ("provider", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ApiError(f"Call.{name} must be a non-empty string, got {value!r}")
        for name in ("tokens", "labels", "keys"):
            _mapping(getattr(self, name), f"Call.{name}")
        if (
            self.at is not None
        ):  # the line is built in UTC from it (``_when``); the call keeps the app's value
            _utc(self.at, "Call.at")


@dataclass(frozen=True)
class Price:
    """A call's price; ``usd`` is None when nothing prices it, and ``note`` says how or why not."""

    usd: float | None
    note: str


@contextmanager
def _said() -> Iterator[None]:
    """The tool's own errors (a config, a window, an unpriced model) as the interface's one error type."""
    try:
        yield
    except ToolError as exc:
        raise ApiError(str(exc)) from exc


def _provider(call: Call) -> Provider:
    try:
        return Provider.of(call.provider)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"invalid call: provider {call.provider!r}: {exc}") from exc


def _when(call: Call) -> datetime:
    """The call's time in UTC at second precision; now when the call gives none."""
    return _utc(call.at, "Call.at") if call.at is not None else now().replace(microsecond=0)


def _validated(call: Call, book: PriceBook | None, at: datetime) -> Usage:
    """The usage as the reader accepts it on its day; ``ApiError`` when the line would be skipped."""
    try:
        return validated(Usage(call.provider, call.model, dict(call.tokens), call.cost), book, at)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"invalid call: {exc}") from exc


def _line(call: Call, at: datetime, book: PriceBook) -> dict[str, Any]:
    """The usage-log line of a call; a model without a list price needs the call's cost."""
    provider = _provider(call)
    if call.cost is None and book.entry(provider, call.model, at) is None:
        raise ApiError(
            f"no price: {call.provider}/{call.model} has no list price on {at:%Y-%m-%d} — give the call's cost, "
            "or add the model to the instance's prices.json"
        )
    usage = _validated(call, book, at)
    unknown = sorted(set(call.keys) - _KEYS)
    if unknown:
        raise ApiError(
            f"invalid call: unknown key(s) {', '.join(unknown)}; the keys are {', '.join(sorted(_KEYS))}"
        )
    try:
        return entry(usage, at, Attribution(**call.keys, labels=call.labels))
    except (TypeError, ValueError) as exc:
        raise ApiError(f"invalid call: {exc}") from exc


def price_call(call: Call, book: PriceBook, config: Config) -> Price:
    """A call's price: the cost the caller reported, else the list price the report gives its line.

    A model priced per unit (GitHub) or without a row in the price book has no list price here.
    """
    at = _when(call)
    provider = _provider(call)
    if call.cost is not None:
        return Price(_validated(call, book, at).cost, REPORTED)
    found = book.entry(provider, call.model, at)
    if found is None:
        _validated(call, None, at)  # a malformed call is said as such, not as a missing price
        return Price(None, f"no list price for {call.provider}/{call.model} and no cost given")
    usage = _validated(call, book, at)
    row = parse_line(entry(usage, at), "price", set(), book)
    with _said():
        money = list_price(row, book, config)
    return Price(money.usd, how_priced(row, found, money))


def _names(value: Any, what: str) -> tuple[str, ...]:
    """A list or tuple of strings (one string alone would be read letter by letter)."""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ApiError(f"{what} must be a list or a tuple of strings, got {type(value).__name__}")
    if not all(isinstance(item, str) for item in value):
        raise ApiError(f"{what} must hold strings, got {value!r}")
    return tuple(value)


def _request(period: Any, groups: Any, attribute: Any, where: Any, by: Any) -> ReportRequest:
    """The report request of the interface's arguments, each checked and named when it is the wrong type."""
    if not isinstance(period, Period):
        raise ApiError(f"period must be a Period, got {type(period).__name__}")
    chosen = _names(groups, "groups")
    if set(chosen) - GROUPS:
        raise ApiError(
            f"groups: {', '.join(sorted(set(chosen) - GROUPS))} — the groups are real, api, vendor"
        )
    filters = dict(_mapping(where or {}, "where"))  # the label rule itself: ops._checked, for both doors
    strings = all(isinstance(name, str) and isinstance(value, str) for name, value in filters.items())
    if not strings or not isinstance(by or "", str):
        raise ApiError("where maps label names to strings; by is one label name")
    return ReportRequest(
        all_projects=True,
        since=iso(period.since) if period.since else None,
        until=iso(period.until) if period.until else None,
        hours=period.hours,
        groups=chosen,
        attribute=_names(attribute, "attribute"),
        where=filters,
        by=by or "",
    )


class SkipClass(Enum):
    """Why an import skipped a line; the value is the words the command prints."""

    NOT_A_USAGE_LINE = "not a usage line"  # the stream's other lines: counted, never a failure, never quoted
    UNREADABLE = "a usage line that cannot be read"  # it names "schema", yet no JSON object parses
    INVALID = "invalid usage line"  # one ``record`` would refuse: its example names the key
    NO_EVENT_ID = "no event_id"  # an import is replay-safe only by the line's id
    LINE_TOO_LONG = "line too long"

    @property
    def fails(self) -> bool:
        """Whether the app must fix such a line: every class but the stream's other lines, which are never quoted."""
        return self is not SkipClass.NOT_A_USAGE_LINE


_USAGE_HEADER = frozenset(REQUIRED) - {"schema"}  # the reader's required keys beside the version
_CALL_FIELDS = frozenset({"schema", "at", "currency", "provider", "model", "tokens", "cost", "labels"})
_RELATIVE = frozenset({"now"})  # a time the reader takes that an import cannot replay


class _TooLong(ApiError):
    """A line over the instance's limit (``MAX_LINE_BYTES``): nothing was written."""


def _segment(instance: Instance, at: datetime) -> Path:
    """The month's segment of ``at``; one that leads out of the instance is refused."""
    try:
        return writable_segment(instance, at)
    except ValueError as exc:
        raise ApiError(f"cannot write: {exc}") from exc


def _append(target: Path, line: Mapping[str, Any]) -> None:
    """One atomic append of the line within the instance's limit."""
    try:
        append(target, line, MAX_LINE_BYTES)
    except ValueError as exc:
        raise _TooLong(f"line too long: {exc}") from exc
    except OSError as exc:
        raise ApiError(f"cannot write {target}: {exc}") from exc


@dataclass(frozen=True)
class UsageImport:
    """What ``Engine.import_usage`` did: lines written, lines the instance already held, lines skipped by class."""

    imported: int
    already_there: int
    skipped: Mapping[SkipClass, int]  # class → lines, in the order first met
    examples: Mapping[SkipClass, str]  # its first line, "line N: why"; none for the stream's other lines

    @property
    def failed(self) -> int:
        """The skipped lines the app must fix: every class but a line that is no usage line at all."""
        return sum(lines for reason, lines in self.skipped.items() if reason.fails)


class _Skip(Exception):
    """A line the import skips: its class and why, for the class's example."""

    def __init__(self, reason: SkipClass, detail: str = "") -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _candidates(text: str) -> Iterator[str]:
    """The whole line, then — behind a log prefix (a timestamp, a unit name) — the text from its first ``{``."""
    line = text.lstrip("\ufeff")
    yield line
    start = line.find("{")
    if start > 0:
        yield line[start:]


def _a_usage_object(value: Any) -> bool:
    """A JSON object with ``schema`` and at least one other key of the reader's header.

    An app's own JSON records that carry a ``schema`` version of their own are the stream's other lines.
    """
    return isinstance(value, Mapping) and "schema" in value and any(key in value for key in _USAGE_HEADER)


def _usage_object(text: str) -> Mapping[str, Any] | None:
    """The line's usage object (a JSON object with a ``schema`` key); None for any other line of the stream."""
    for candidate in _candidates(text):
        try:
            value = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        return value if _a_usage_object(value) else None
    if '"schema"' in text:
        raise _Skip(
            SkipClass.UNREADABLE, "no JSON object parses (a torn line, or a prefix after its first {)"
        )
    return None


def _event(line: Mapping[str, Any]) -> str:
    """The line's ``event_id``, by the reader's rule; without one a line cannot be imported safely twice."""
    try:
        event = line_event_id(line)
    except ValueError as exc:
        raise _Skip(SkipClass.INVALID, f"invalid call: {exc}") from exc
    if not event:
        raise _Skip(SkipClass.NO_EVENT_ID)
    return event


def _imported_at(line: Mapping[str, Any]) -> datetime:
    """The line's time: the reader's header rule, a zone (the library's rule), never a word relative to the import."""
    try:
        at = line_header(line)
        zoned(str(line["at"]), "at")
    except (ValueError, ToolError) as exc:
        raise _Skip(SkipClass.INVALID, f"invalid call: {exc}") from exc
    if str(line["at"]).strip() in _RELATIVE:
        raise _Skip(
            SkipClass.INVALID, f"invalid call: at {line['at']!r}: an imported line needs the time of its call"
        )
    return at


def _call_of(line: Mapping[str, Any], at: datetime) -> Call:
    """The call a usage line records, its optional keys as the line has them (a null one is absent)."""
    tokens, labels = line.get("tokens"), line.get("labels")
    return Call(
        provider=line["provider"],
        model=line["model"],
        tokens={} if tokens is None else tokens,
        cost=line.get("cost"),
        at=at,
        labels={} if labels is None else labels,
        keys={key: value for key, value in line.items() if key not in _CALL_FIELDS and value is not None},
    )


@dataclass
class _Import:
    """One import's tallies and the event ids of every segment it writes to, each segment read once.

    A line whose id is held is not written, even when it carries a session stamp the held copy lacks: the reader lends
    a later copy's stamp only within one log, so an import keeps the first copy as it is.
    """

    instance: Instance
    book: PriceBook
    imported: int = 0
    already_there: int = 0
    skipped: dict[SkipClass, int] = field(default_factory=dict)
    examples: dict[SkipClass, str] = field(default_factory=dict)
    held: dict[Path, set[str]] = field(default_factory=dict)

    def take(self, number: int, text: str) -> None:
        """Line ``number`` of the stream: ignored (blank), skipped and counted, already there, or written."""
        if not isinstance(text, str):
            raise ApiError(f"lines must hold strings, got {type(text).__name__} as line {number}")
        if not text.strip():
            return
        try:
            self._put(text)
        except _Skip as skip:
            self._skip(skip, number)

    def _put(self, text: str) -> None:
        """The id is checked before the call is: a line already there is never validated or priced again."""
        line = _usage_object(text)
        if line is None:
            raise _Skip(SkipClass.NOT_A_USAGE_LINE)
        event = _event(line)
        at = _imported_at(line)
        target = _segment(self.instance, at)  # refused as record refuses it: the import stops
        held = self._held(target)
        if event in held:
            self.already_there += 1
            return
        self._write(target, line, at)
        held.add(event)
        self.imported += 1

    def _write(self, target: Path, line: Mapping[str, Any], at: datetime) -> None:
        """``record``'s validator and writer; a write that fails for another reason stops the import."""
        try:
            call = _call_of(line, at)
            written = _line(call, _when(call), self.book)
        except ApiError as exc:
            raise _Skip(SkipClass.INVALID, str(exc)) from exc
        try:
            _append(target, written)
        except _TooLong as exc:
            raise _Skip(
                SkipClass.LINE_TOO_LONG, str(exc.__cause__ or exc)
            ) from exc  # the class says "too long"

    def _held(self, target: Path) -> set[str]:
        if target not in self.held:
            try:
                self.held[target] = event_ids(target, self.book)
            except OSError as exc:
                raise ApiError(f"cannot read {target}: {exc}") from exc
        return self.held[target]

    def _skip(self, skip: _Skip, number: int) -> None:
        self.skipped[skip.reason] = self.skipped.get(skip.reason, 0) + 1
        if skip.reason.fails:  # the stream's other lines are never repeated: they may hold anything
            self.examples.setdefault(
                skip.reason, f"line {number}: {skip.detail}" if skip.detail else f"line {number}"
            )


class Engine:
    """ai-cost bound to one instance: record calls, price them, report them, check the instance.

    The config and the prices are read once, when the engine is made: build a new one after changing them.
    """

    def __init__(self, instance: Instance) -> None:
        """Reads the instance's config and prices once; a file that does not parse is an ``ApiError``."""
        if not isinstance(instance, Instance):
            raise ApiError(f"instance must be an Instance, got {type(instance).__name__}")
        if instance.root.exists() and not instance.root.is_dir():
            raise ApiError(f"Instance.root {instance.root} is not a directory")
        self.instance = instance
        self._paths = Paths.for_instance(instance)
        with _said():
            self._config = load_config(self._paths)
            self._book = load_pricebook(self._paths)

    def record(self, call: Call) -> Mapping[str, Any]:
        """Validate like ``ai-cost log``, append one line to the month's segment in one write, return the line."""
        at = _when(call)
        line = _line(call, at, self._book)
        _append(_segment(self.instance, at), line)
        return line

    def import_usage(self, lines: Iterable[str]) -> UsageImport:
        """Usage lines of a log stream into the instance, each ``event_id`` once: a stream fed twice adds nothing.

        Each line is validated and written as ``record`` writes a call, in the segment of its own ``at``; a line that
        is no JSON object with a ``schema`` key is another line of the stream, counted as such. Read as a stream: one
        line at a time. Each id is counted once by every report; two writers at once may store a line twice.
        """
        if isinstance(lines, (str, bytes)):
            raise ApiError("lines must be an iterable of lines, not one string")
        run = _Import(self.instance, self._book)
        for number, text in enumerate(lines, 1):
            run.take(number, text)
        return UsageImport(run.imported, run.already_there, dict(run.skipped), dict(run.examples))

    def price(self, call: Call) -> Price:
        """The price of one call; reads no file (the prices were read when the engine was made)."""
        return price_call(call, self._book, self._config)

    def report(
        self,
        period: Period,
        *,
        groups: Sequence[str] = INSTANCE_GROUPS,
        attribute: Sequence[str] = (),
        where: Mapping[str, str] | None = None,
        by: str | None = None,
    ) -> Report:
        """The report of ``docs/JSON.md`` for the period; an unpriced model is skipped and counted, never raised."""
        request = _request(period, groups, attribute, where, by)
        with _said():
            return build_report(request, self._paths, self._config, self._book)

    def report_json(
        self,
        period: Period,
        *,
        groups: Sequence[str] = INSTANCE_GROUPS,
        attribute: Sequence[str] = (),
        where: Mapping[str, str] | None = None,
        by: str | None = None,
        detail: bool = False,
    ) -> str:
        """The JSON ``ai-cost --instance DIR report --format json`` prints for the same period and options."""
        report = self.report(period, groups=groups, attribute=attribute, where=where, by=by)
        return render_json(report, detail)

    def doctor(self) -> Sequence[str]:
        """The instance's problems, one line each, every remedy an instance's own; empty when there is none."""
        with _said():
            checks = doctor_checks(self._paths, self._config, self._book)
        return [check.text for check in checks if check.mark is Mark.PROBLEM]
