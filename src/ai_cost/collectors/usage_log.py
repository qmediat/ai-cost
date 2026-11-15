"""The usage log (ADR-0005): one JSON line per LLM request, written by any program, priced like every other source.

A line names ``provider`` and ``model`` and carries the counters the provider's formula reads (``pricing.counters_of``)
and / or ``cost`` in USD. Every value is validated: a boolean, a negative, fractional or unbounded count, a counter the
provider would not price, a currency other than USD, a malformed or non-string id or a missing required key makes
the line a counted skip. A well-formed provider or model the pricebook does not list is an
unpriced row when the line carries no cost — the report's ``--unpriced`` rule decides (fail by default, ``skip``
counts it). A repeated ``event_id`` within one file is read once; an instance's monthly segments are one log, so
there an ``event_id`` is read once across the segments a report reads.
"""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ConfigError
from ..models import (
    Billing,
    Collected,
    PriceEntry,
    Provider,
    RowKind,
    Scope,
    Skipped,
    Tokens,
    UsageRow,
    Window,
)
from ..origin import origin_of
from ..pricing import as_tier_family, counters_of, folds_to_tiers
from ..timeutil import parse_ts
from ..values import count, labels, money, optional_text

if TYPE_CHECKING:
    from ..config import Config, Instance, Paths, PriceBook
    from ..plugins import Context

SCHEMA = 1
REQUIRED = ("schema", "at", "provider", "model")
COUNTERS = frozenset(f.name for f in fields(Tokens) if f.type in ("int", int))
ALIASES = {
    "thoughts": "output"
}  # a Gemini "thoughts" counter is output, as the Gemini CLI collector counts it
_NOT_PER_REQUEST = frozenset({"requests", "reviews"})  # counted by the tool, never logged by a caller
PER_REQUEST = (
    COUNTERS - _NOT_PER_REQUEST
)  # what a line may carry: a tool-counted key is unknown to the reader
LOG_COUNTERS = tuple(f.name for f in fields(Tokens) if f.name in PER_REQUEST) + tuple(ALIASES)
_BILLING = {"api": Billing.API, "subscription": Billing.SUBSCRIPTION}


def _counts(raw: Any, unknown: set[str]) -> dict[str, Any]:
    """The counters of one line by the reader's names; unknown keys are collected (one warning per file), wrong values are errors."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"tokens must be an object, got {type(raw).__name__}")
    counts: dict[str, Any] = (
        {}
    )  # Any: Tokens mixes int, float and Mapping fields, only int ones are accepted here
    for key, value in raw.items():
        name = ALIASES.get(str(key), str(key))
        if name not in PER_REQUEST:
            unknown.add(str(key))
            continue
        if value is None:  # a present null is not "absent": the writer meant something and it is not a count
            raise ValueError(f"{key} is null: a counter is a whole number or absent")
        counts[name] = counts.get(name, 0) + count(
            value, str(key)
        )  # exact, non-negative, bounded: the writer's rule
    return counts


def _priced_counters(provider: Provider, entry: PriceEntry | None, given: frozenset[str]) -> None:
    """A counter the row's pricing never reads is a writer's error: said, never silently priced at zero."""
    consumed = counters_of(provider, entry)
    if consumed is None:
        return
    stray = sorted(given - consumed)
    if stray:
        reads = ", ".join(sorted(consumed)) or "no token counter (give a cost)"
        raise ValueError(f"{provider.value} prices {reads}: {', '.join(stray)} would not be priced")


def _cost(value: Any) -> float | None:
    """The line's own cost: a finite non-negative number (a huge integer or a string is a ``ValueError``)."""
    if isinstance(value, str):
        raise ValueError(f"cost must be a number, got {value!r}")
    amount = money(
        value, "cost"
    )  # bools and non-numbers: ValueError; an integer beyond float range: ValueError
    if amount is not None and amount < 0:
        raise ValueError(f"cost must be non-negative, got {value!r}")
    return amount


def _billing(entry: Mapping[str, Any]) -> Billing:
    raw = entry.get("billing")
    if raw is None:
        return (
            Billing.API
        )  # a line without a billing key: pay-per-token (parse_line refused one with nothing to price)
    if (
        not isinstance(raw, str) or raw not in _BILLING
    ):  # a list or an object is unhashable: said as a billing error
        raise ValueError(f"billing must be api or subscription, got {raw!r}")
    return _BILLING[raw]


def _scope(entry: Mapping[str, Any]) -> Scope:
    tags = entry.get("tags")
    tags = () if tags is None else tags
    if not isinstance(tags, (list, tuple)) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a list of strings")  # as the writer's Attribution requires
    return Scope(
        branch=optional_text(entry, "branch"),
        pr=optional_text(entry, "pr"),
        paths=tuple(tags),
    )


def _text(entry: Mapping[str, Any], key: str) -> str:
    """A required id as the string it must be: a number is not a provider or a model, however it would print."""
    value = entry[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {type(value).__name__}")
    return value


def line_header(entry: Mapping[str, Any]) -> datetime:
    """The required keys, the exact schema, USD only; the line's timestamp."""
    missing = [key for key in REQUIRED if entry.get(key) is None or entry.get(key) == ""]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    schema = entry["schema"]
    if (
        isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA
    ):  # true == 1 in Python, not here
        raise ValueError(f"schema {schema!r} is not {SCHEMA}")
    currency = entry.get("currency")
    if currency is not None and currency != "USD":
        raise ValueError(f"currency {currency!r} is not USD")
    at = parse_ts(entry["at"])
    if at is None:
        raise ValueError(f"at is not a timestamp: {entry['at']!r}")
    return at


def parse_line(
    entry: Any,
    source_name: str,
    unknown: set[str],
    book: PriceBook | None = None,
    read_labels: tuple[dict[str, str], bool] | None = None,
) -> UsageRow:
    """One log line as a row; every shape problem is a ``ValueError`` naming the key.

    With the pricebook, the counters are checked against the model's own price shape; without it, against the
    provider's built-in formula.
    """
    if not isinstance(entry, Mapping):
        raise ValueError("not an object")
    at = line_header(entry)
    provider = Provider.of(_text(entry, "provider"))
    cost = _cost(entry.get("cost"))
    counts = _counts(entry.get("tokens"), unknown)
    if cost is None and not any(counts.values()):
        raise ValueError("neither tokens nor cost")
    model = _text(entry, "model")
    priced_as = book.entry(provider, model, at) if book is not None else None
    if folds_to_tiers(provider, priced_as):
        counts = as_tier_family(
            counts
        )  # DeepSeek's own cache counters of a tiers-priced model: the writer's rule is the reader's
    _priced_counters(provider, priced_as, frozenset(counts))
    tokens = Tokens(**counts)
    return UsageRow(
        provider=provider,
        model=model,
        kind=RowKind.LOG,
        source=optional_text(entry, "source")
        or "usage-log",  # the reporting program's own name when it gave one
        at=at,
        ref=optional_text(entry, "ref") or optional_text(entry, "session") or source_name,
        billing=_billing(entry),
        tokens=tokens,
        cost_reported=cost,
        scope=_scope(entry),
        origin_session=_origin_session(entry),
        labels=(read_labels or line_labels(entry))[0],  # the caller's parse when it made one
    )


def line_labels(entry: Mapping[str, Any]) -> tuple[dict[str, str], bool]:
    """The line's labels, and whether its ``labels`` key is another program's (a shape the writer never writes).

    Such a key costs the line its labels only: the usage is real (``_Read`` counts the lines once per file).
    """
    try:
        return labels(entry.get("labels")), False
    except ValueError:
        return {}, True


def _origin_session(entry: Mapping[str, Any]) -> str:
    """The line's ``origin_session``; a malformed origin stamps nothing (``_OriginNotes`` counts it per file)."""
    try:
        return origin_of(entry).session
    except ValueError:
        return ""


def line_event_id(entry: Mapping[str, Any]) -> str:
    """The writer's id for this line; absent is ``""``; anything but a string is an error (5 and "5" would collide)."""
    event = entry.get("event_id")
    if event is None:
        return ""
    if not isinstance(event, str):
        raise ValueError(f"event_id must be a string, got {type(event).__name__}")
    return event


def _outside(entry: Any, window: Window | None) -> bool:
    """Whether a line that could not be read says, by its ``at``, that it belongs to another window."""
    if window is None or not isinstance(entry, Mapping):
        return False
    at = parse_ts(entry.get("at"))
    return at is not None and not window.contains(at)


def _lines(path: Path) -> Iterator[tuple[int, str]]:
    with path.open(
        encoding="utf-8", errors="replace"
    ) as handle:  # the writer's encoding, whatever the locale
        for line_no, line in enumerate(handle, 1):
            if line.strip():
                yield line_no, line


def event_ids(path: Path, book: PriceBook | None = None) -> set[str]:
    """The event ids of the rows a report counts in the segment ``path``; an absent file holds none.

    The reader's own decision (``_Read.take``, the segment's month included) with the report's price book: a line it
    skips — counted by every report — reserves no id, so a valid copy of that call can still be written. Only the ids
    are kept: the reader's rows and skips are emptied after every line, so memory grows with the ids, not the file.
    """
    found: set[str] = set()
    if not path.exists():
        return found
    read = _Read(None, book, [], ref=path.parent.name, segmented=True)
    for line_no, line in _lines(path):
        read.take(path, line_no, line)
        found.update(read.events.at)
        read.rows.clear()
        read.skipped.clear()
        read.events = _Events()
    return found


@dataclass
class _Events:
    """One file's event ids: the first copy of an event counts, once.

    A later copy that carries the session stamp the kept one lacks lends the kept copy its stamp (a session report
    selects by the stamp; the first copy's time and figures stay); a later copy stamped for another session is a
    conflict, counted, and the first copy stays as it was.
    """

    at: dict[str, int] = field(default_factory=dict)
    conflicts: int = 0

    def keep(self, rows: list[UsageRow], row: UsageRow, event: str) -> bool:
        """Whether ``row`` joined ``rows`` (appended, or in its event's place)."""
        index = self.at.get(event) if event else None
        if index is None:
            if event:
                self.at[event] = len(rows)
            rows.append(row)
            return True
        kept = rows[index].origin_session
        if not kept and row.origin_session:
            rows[index] = replace(
                rows[index], origin_session=row.origin_session
            )  # the stamp, never other figures
            return True
        self.conflicts += bool(kept and row.origin_session and row.origin_session != kept)
        return False


@dataclass
class _OriginNotes:
    """Malformed origins of one file's counted lines: how many and the first reason (the reader streams, keeps no list).

    A malformed origin never costs the line its usage: the tokens are real, only the stamp is unusable.
    """

    count: int = 0
    first: str = ""

    def note(self, entry: Mapping[str, Any], line_no: int) -> None:
        try:
            origin_of(entry)
        except ValueError as exc:
            self.count += 1
            self.first = self.first or f"line {line_no}: {exc}"


@dataclass
class _Read:
    """What one log's read accumulates: rows (with their event ids), skips and the per-log notes."""

    window: Window | None
    book: PriceBook | None
    skipped: list[Skipped]
    ref: str = ""  # the default ref of a line that names none: the log's name ("" = each file's stem)
    segmented: bool = False  # monthly segments: a line belongs to the month its file names
    rows: list[UsageRow] = field(default_factory=list)
    foreign_labels: int = 0  # counted lines whose ``labels`` key is not this reader's
    events: _Events = field(default_factory=_Events)
    unknown: set[str] = field(default_factory=set)
    origins: _OriginNotes = field(default_factory=_OriginNotes)

    def take(self, path: Path, line_no: int, line: str) -> None:
        """One line: a row inside the window (once per event id), a counted skip, or nothing (another window)."""
        entry: Any = None  # this line's, never the previous one's
        try:
            entry = json.loads(line)
            read = line_labels(entry) if isinstance(entry, Mapping) else None  # parsed once, handed on
            row = parse_line(entry, self.ref or path.stem, self.unknown, self.book, read)
            event = line_event_id(entry)
        except (ValueError, TypeError, RecursionError) as exc:  # RecursionError: nested beyond any reason
            if not _outside(entry, self.window):  # a bad line of another window is not this window's skip
                self.skipped.append(Skipped("usage-log", str(path), f"line {line_no}: {exc}"))
            return
        if self.segmented and row.at is not None and _month(row.at) != path.stem:
            reason = (
                f"line {line_no}: at {row.at:%Y-%m-%dT%H:%M:%SZ} is outside the segment's month {path.stem}"
            )
            self.skipped.append(Skipped("usage-log", str(path), reason))
            return
        if self.window is not None and not self.window.contains(row.at):
            return  # outside the window: neither counted nor remembered, so it never consumes an id
        before = len(self.rows)
        if self.events.keep(self.rows, row, event):
            self.origins.note(entry, line_no)
        if (
            len(self.rows) > before and read is not None
        ):  # a new row; a copy lending its stamp is not counted twice
            self.foreign_labels += read[1]

    def notes(self, name: object) -> list[str]:
        """The log's warnings: unknown keys, conflicting events, other programs' `labels` keys, malformed origins."""
        warnings = []
        if self.unknown:
            warnings.append(f"{name}: unknown token keys ignored: {', '.join(sorted(self.unknown))}")
        if self.events.conflicts:
            warnings.append(
                f"{name}: {self.events.conflicts} repeated event(s) stamped for another session: first copy kept"
            )
        if self.foreign_labels:
            warnings.append(
                f"{name}: {self.foreign_labels} line(s) carry a `labels` key the reader does not use: "
                "usage counted, labels ignored"
            )
        if self.origins.count:
            warnings.append(
                f"{name}: {self.origins.count} line(s) with a malformed origin: usage counted, origin ignored"
                f" (first: {self.origins.first})"
            )
        return warnings


def _collect_file(
    path: Path,
    window: Window | None,
    skipped: list[Skipped],
    warnings: list[str],
    book: PriceBook | None = None,
) -> list[UsageRow]:
    """One file's rows inside the window, read line by line; its notes join ``warnings``."""
    read = _Read(window, book, skipped)
    for line_no, line in _lines(path):
        read.take(path, line_no, line)
    warnings.extend(read.notes(path))
    return read.rows


def collect_usage_log(
    paths: list[Path], window: Window | None, book: PriceBook | None = None
) -> tuple[Collected, list[str]]:
    """Rows of every configured usage log inside the window, plus one warning per file with unknown keys."""
    rows: list[UsageRow] = []
    skipped: list[Skipped] = []
    warnings: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            rows += _collect_file(path, window, skipped, warnings, book)
        except OSError as exc:
            skipped.append(Skipped("usage-log", str(path), f"cannot read: {exc}"))
    return Collected(rows=rows, skipped=skipped), warnings


def _readable_key(path: Path) -> Path:
    """The resolved location of ``path``; raises for a symlink loop in any component or a dangling link.

    Python 3.13+ resolves a loop without raising, but ``stat()`` still fails on it (ELOOP). A merely absent file
    below real directories is normal for a configured log and is returned as is; a dangling link in any component
    of the path is unreadable and raises.
    """
    key = path.resolve()
    try:
        path.stat()
    except FileNotFoundError:
        # A dangling link somewhere in the path is unreadable; a link to an existing directory (macOS /var, /tmp)
        # above a merely absent file is not.
        if leads_nowhere(path):
            raise
    return key


def leads_nowhere(path: Path) -> bool:
    """Whether a link in ``path`` or any of its parents leads nowhere (dangling or a loop): the path cannot be read.

    One rule for the reader (a counted skip) and the doctor (a problem line); a merely absent file is not this.
    """
    return any(part.is_symlink() and not part.exists() for part in (path, *path.parents))


def _unique(paths: list[Path], skipped: list[Skipped]) -> list[Path]:
    """The paths in order, each resolved location once; a symlink that leads nowhere (a loop, a dangling link) is a skip."""
    seen: set[Path] = set()
    kept: list[Path] = []
    for path in paths:
        try:
            key = _readable_key(path)
        except (OSError, RuntimeError) as exc:  # RuntimeError: a symlink loop on Python < 3.13
            skipped.append(Skipped("usage-log", str(path), f"cannot resolve: {exc}"))
            continue
        if key not in seen:
            seen.add(key)
            kept.append(path)
    return kept


# ---- an instance's log: monthly segments ---------------------------------------------------------------------------

SEGMENT_NAME = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])\.jsonl")


def _month(at: datetime) -> str:
    return f"{at.astimezone(timezone.utc):%Y-%m}"


def segment_of(usage_dir: Path, at: datetime) -> Path:
    """The segment a line belongs to: ``<usage_dir>/YYYY-MM.jsonl`` by the UTC month of its ``at``."""
    return usage_dir / f"{_month(at)}.jsonl"


NOT_A_SEGMENT = "not a monthly segment"
A_DIRECTORY = "a directory named like a segment"
NOT_A_FILE = "not a regular file"
LEADS_OUT = "a link that leads outside the instance"
LEADS_NOWHERE = "a link that leads nowhere"


@dataclass(frozen=True)
class Unread:
    """An entry of the segments' directory a report does not read, and why."""

    path: Path
    why: str

    @property
    def name(self) -> str:
        """The entry's name in ``usage/``."""
        return self.path.name


@dataclass(frozen=True)
class Scan:
    """One walk of an instance's ``usage/``, oldest first.

    The readable segments, the month-named entries a report cannot read (each with why), and the names that are no
    month (a writer's dotfiles and ``*.tmp`` left out).
    """

    readable: tuple[Path, ...]
    unread: tuple[Unread, ...]
    strays: tuple[Unread, ...]

    def in_window(self, window: Window | None) -> tuple[list[Path], list[Unread]]:
        """The readable segments and the unreadable month entries the window touches (every one without a window)."""
        return (
            [path for path in self.readable if _touches(path, window)],
            [item for item in self.unread if _touches(item.path, window)],
        )


def real_root(instance: Instance) -> Path:
    """The instance root with its links followed: the one place every containment check compares against."""
    return instance.root.resolve()


def scan(instance: Instance) -> Scan:
    """``usage/`` walked once; every month-named entry judged by one rule (``_segment_why``) against the real root."""
    if not instance.usage_dir.is_dir():
        return Scan((), (), ())
    root = real_root(instance)
    readable: list[Path] = []
    unread: list[Unread] = []
    strays: list[Unread] = []
    for path in sorted(instance.usage_dir.iterdir()):
        if not SEGMENT_NAME.fullmatch(path.name):
            if not _a_writers_own(path):
                strays.append(Unread(path, NOT_A_SEGMENT))
            continue
        why = _segment_why(path, root)
        if why:
            unread.append(Unread(path, why))
        else:
            readable.append(path)
    return Scan(tuple(readable), tuple(unread), tuple(strays))


def _touches(path: Path, window: Window | None) -> bool:
    """Whether the segment named ``path`` holds a month the window touches."""
    if window is None:
        return True
    last = _month(max(window.start, window.end - timedelta(microseconds=1)))
    return _month(window.start) <= path.stem <= last


def unread_files(found: Scan) -> list[Unread]:
    """What a report leaves unread in ``usage/``, each with why: the doctor's view of the whole directory."""
    return sorted([*found.unread, *found.strays], key=lambda item: item.name)


def _a_writers_own(path: Path) -> bool:
    return path.name.startswith(".") or path.name.endswith(".tmp")


def _segment_why(path: Path, root: Path) -> str:
    """Why a report does not read the month-named entry ``path`` ("" when it does): one rule for reader and doctor."""
    resolved = _resolved(path)
    if resolved is None:
        return LEADS_NOWHERE
    if not (resolved == root or root in resolved.parents):
        return LEADS_OUT
    return _kind_why(resolved)


def _kind_why(resolved: Path) -> str:
    """Why the place ``resolved`` inside the instance is no readable segment ("" for a regular file)."""
    try:
        mode = resolved.stat().st_mode
    except FileNotFoundError:
        return LEADS_NOWHERE
    except OSError as exc:
        return f"cannot read: {exc.strerror or exc}"
    if stat.S_ISDIR(mode):
        return A_DIRECTORY
    return "" if stat.S_ISREG(mode) else NOT_A_FILE


def _resolved(path: Path) -> Path | None:
    """Where ``path`` leads, links followed; ``None`` for a link loop (Python 3.13+ returns one, older ones raise)."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):  # RuntimeError: a link loop before Python 3.13
        return None
    try:
        resolved.stat()
    except FileNotFoundError:
        return resolved  # an absent file (or a dangling link's target): its place is known
    except OSError:  # ELOOP: a loop Python 3.13+ resolved without raising
        return None
    return resolved


def writable_segment(instance: Instance, at: datetime) -> Path:
    """The segment a line of ``at`` is written to, refused (``ValueError``) when it leads out of the instance.

    A report reads only what lies in the instance (``scan``, against the same real root), so a call written through
    a link that leads elsewhere would never be reported; a linked instance root is the instance itself.
    """
    target = segment_of(instance.usage_dir, at)
    root = real_root(instance)
    resolved = _resolved(target)
    if resolved is None or not (resolved == root or root in resolved.parents):
        raise ValueError(f"{target}: {LEADS_OUT} (or nowhere) — a call written there would never be reported")
    return target


Numbered = Iterable[
    tuple[Path, int, str]
]  # (file, line number, text): the segments of one log, read in order


def _numbered(path: Path) -> Numbered:
    return ((path, line_no, line) for line_no, line in _lines(path))


def _collect_lines(read: _Read, name: object, lines: Numbered, warnings: list[str]) -> list[UsageRow]:
    """The rows of one log read across several files; its notes join ``warnings`` once, under ``name``."""
    for path, line_no, line in lines:
        read.take(path, line_no, line)
    warnings.extend(read.notes(name))
    return read.rows


def _tolerant(files: Sequence[Path], skipped: list[Skipped]) -> Numbered:
    """The segments' lines in order; a segment that cannot be read is a counted skip, the others are still read."""
    for path in files:
        try:
            yield from _numbered(path)
        except OSError as exc:
            skipped.append(Skipped("usage-log", str(path), f"cannot read: {exc}"))


def collect_segments(
    found: Scan, usage_dir: Path, window: Window | None, book: PriceBook | None = None
) -> tuple[Collected, list[str]]:
    """The rows of the segments the window touches, read as ONE log: an ``event_id`` counts once across them.

    A line whose ``at`` is in another month than its segment's is a counted skip: no period would read it; so is a
    month-named entry the window touches that is no readable segment (``Scan``), never a month dropped unsaid.
    """
    skipped: list[Skipped] = []
    warnings: list[str] = []
    read = _Read(window, book, skipped, ref=usage_dir.name, segmented=True)
    files, unread = found.in_window(window)
    skipped += [Skipped("usage-log", str(item.path), f"{item.why}: not read") for item in unread]
    rows = _collect_lines(read, usage_dir, _tolerant(files, skipped), warnings)
    if found.strays:
        names = ", ".join(item.name for item in found.strays)
        warnings.append(f"{usage_dir}: {NOT_A_SEGMENT}, not read: {names}")
    return Collected(rows=rows, skipped=skipped), warnings


OUTSIDE_REMEDY = "name it in Instance.outside_logs (the command: --outside-log PATH)"


def instance_logs(instance: Instance, listed: Sequence[str], own: Sequence[Path]) -> list[Path]:
    """An instance config's ``usage_logs`` beside its segments ``own``, as the config names them below the root.

    The containment rule reads each entry's resolved place: a file below the root is read; one outside it only when
    ``Instance.outside_logs`` names it; a ``~`` entry (it depends on the environment), the root itself (``""``,
    ``"."``) and a directory are a ``ConfigError``; an entry that is one of the segments is dropped. The named paths
    are returned: one that resolves nowhere (a loop, a dangling link) is kept, so the reader counts it (``_unique``)
    and the doctor names it.
    """
    root = real_root(instance)
    allowed = {path.resolve() for path in instance.outside_logs}
    segments = {_resolved(path) for path in own}
    found = []
    for name in listed:
        named = instance.root / name
        path = _resolved(named)
        if path is not None:
            _contained(name, path, instance, root, allowed)
        if path is None or path not in segments:
            found.append(named)
    return found


def _contained(name: str, path: Path, instance: Instance, root: Path, allowed: set[Path]) -> None:
    """A ``ConfigError`` for an entry that names no log file of the instance or leaves it without permission."""
    where = f"{instance.config_file}: usage_logs entry {name!r}"
    if name.startswith("~") or path == root or path.is_dir():
        raise ConfigError(f"{where} names no log file of the instance (a file below {instance.root})")
    if root not in path.parents and path not in allowed:
        raise ConfigError(f"{where} is outside the instance {instance.root} — {OUTSIDE_REMEDY}")


def log_files(paths: Paths, config: Config) -> list[Path]:
    """The default usage log (the ``Paths`` given, else the environment's) and every file in config ``usage_logs``.

    An instance has no default log: its segments, then its config's ``usage_logs`` (``instance_logs``).
    """
    from ..log import default_path

    if paths.instance is not None:
        return instance_log_files(paths.instance, config, scan(paths.instance))
    return [paths.usage_log or default_path(), *(Path(p).expanduser() for p in config.usage_logs)]


def instance_log_files(instance: Instance, config: Config, found: Scan) -> list[Path]:
    """An instance's logs from one walk of ``usage/``: its readable segments, then its config's ``usage_logs``."""
    return [*found.readable, *instance_logs(instance, config.usage_logs, found.readable)]


class UsageLogSource:
    """The default usage log (``AI_COST_USAGE_LOG`` / the XDG data dir) plus every file in config ``usage_logs``.

    For an instance: its monthly segments that the window touches, plus its config's ``usage_logs``.
    """

    name = "usage-log"

    def collect(self, ctx: Context) -> Collected:
        """Rows of every usage log inside the window; unknown counter keys become report warnings."""
        if ctx.paths.instance is not None:
            return _collect_instance(ctx.paths.instance, ctx)
        listed = log_files(ctx.paths, ctx.config)  # the Paths given to the report, never the process env
        paths = _unique(
            listed, ctx.skipped
        )  # the default file named again in the config is read once, not twice
        collected, warnings = collect_usage_log(paths, ctx.window, ctx.book)
        ctx.warnings.extend(warnings)
        return collected


def _collect_instance(instance: Instance, ctx: Context) -> Collected:
    """The instance's segments, then the files its config lists; never a file of the environment."""
    found = scan(instance)
    own, warnings = collect_segments(found, instance.usage_dir, ctx.window, ctx.book)
    listed = _unique(instance_logs(instance, ctx.config.usage_logs, found.readable), ctx.skipped)
    more, more_warnings = collect_usage_log(listed, ctx.window, ctx.book)
    ctx.warnings.extend([*warnings, *more_warnings])
    return Collected(rows=[*own.rows, *more.rows], skipped=[*own.skipped, *more.skipped])
