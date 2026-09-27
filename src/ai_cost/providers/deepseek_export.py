"""DeepSeek's usage export as a provider day report (ADR-0008, decision 8): DeepSeek offers no usage API.

``ai-cost import deepseek <export>`` reads the export (a ZIP of ``cost-<from>_<to>.csv`` and ``amount-<from>_<to>.csv``,
or the two CSV files), checks it, and merges its days into ``<state_dir>/providers/deepseek.json``; every report then
reads the days of its window from there. The store keeps amounts, counters and times — never the account id, a key or
a key's name.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..errors import ToolError, UsageError
from ..models import Provider, ProviderDay, ProviderDays, ProviderLine, Span, Window

SOURCE = "deepseek-export"
STORE = "providers/deepseek.json"
SCHEMA = 1
COST_COLUMNS = ("user_id", "start_time_iso", "end_time_iso", "model", "wallet_type", "cost", "currency")
AMOUNT_COLUMNS = (
    "user_id",
    "start_time_iso",
    "end_time_iso",
    "model",
    "api_key_name",
    "api_key",
    "type",
    "price",
    "amount",
)
COUNTERS = ("input_cache_hit_tokens", "input_cache_miss_tokens", "output_tokens", "request_count")
PAID_WALLET = (
    "Paid"  # the wallet the account paid into; any other wallet (a grant) is a discount on the usage
)
EASTERNMOST = timezone(
    timedelta(hours=14)
)  # a zone-less ZIP time read here is the earliest instant it can denote
_MEMBER = re.compile(r"(?:^|/)(cost|amount)-(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.csv$")


@dataclass(frozen=True)
class Export:
    """The two files of one export, their declared range and the earliest instant they can have been produced."""

    cost: list[dict[str, str]]
    amount: list[dict[str, str]]
    first: date
    last: date
    captured: datetime


@dataclass(frozen=True)
class ImportResult:
    """What an import changed: days by their start (``added``, ``replaced``), days left as they were, dates to rebuild."""

    added: tuple[str, ...]
    replaced: tuple[str, ...]
    kept: int
    dates: tuple[date, ...]  # the UTC dates whose `daily` files can use the new days (whole UTC days only)


@dataclass
class _DayTotals:
    """One day × model while it is summed: exact amounts and the counters, per wallet."""

    gross: Decimal = Decimal(0)
    net: Decimal = Decimal(0)
    priced: Decimal = Decimal(0)  # Σ price × amount of the amount file: must equal gross
    requests: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    currency: set[str] = field(default_factory=set)


# ---- reading an export ----------------------------------------------------------------------------------------


def read_export(path: Path, captured: datetime | None = None) -> Export:
    """The export at ``path``: a ZIP (its capture time from the members), or a directory / CSV with ``captured`` given."""
    if not path.exists():
        raise UsageError(f"{path}: no such file or directory")
    if path.is_file() and zipfile.is_zipfile(path):
        if captured is not None:
            raise UsageError(f"{path}: a ZIP carries its own time — --captured is for CSV files")
        texts, stamp = _zip_members(path)
    else:
        if captured is None:
            raise UsageError(
                f"{path}: a CSV export carries no capture time — pass --captured <when the export was taken>"
            )
        texts, stamp = _csv_members(path), captured
    kinds = {kind for kind, _ in texts}
    if kinds != {"cost", "amount"}:
        raise UsageError(
            f"{path}: needs cost-<from>_<to>.csv and amount-<from>_<to>.csv, found {sorted(kinds)}"
        )
    ranges = {rng for _, rng in texts}
    if len(ranges) != 1:
        raise UsageError(f"{path}: the cost and amount files declare different ranges {sorted(ranges)}")
    first, last = ranges.pop()
    if last < first:
        raise UsageError(f"{path}: the declared range {first}..{last} ends before it starts")
    if stamp < datetime.combine(first, time.min, EASTERNMOST):  # the earliest instant the range can start
        raise UsageError(
            f"{path}: taken {stamp.isoformat()}, before its own range {first}..{last} — not a real time"
        )
    return Export(
        cost=_rows(texts[("cost", (first, last))], COST_COLUMNS, "cost"),
        amount=_rows(texts[("amount", (first, last))], AMOUNT_COLUMNS, "amount"),
        first=first,
        last=last,
        captured=stamp,
    )


_Texts = dict[tuple[str, tuple[date, date]], str]


def _zip_members(path: Path) -> tuple[_Texts, datetime]:
    texts: _Texts = {}
    stamps: list[datetime] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info, key in _export_members(archive, path):
                texts[key] = archive.read(info).decode("utf-8-sig")
                stamps.append(datetime(*info.date_time, tzinfo=EASTERNMOST))
    except (
        OSError,
        zipfile.BadZipFile,
        UnicodeDecodeError,
        RuntimeError,
        NotImplementedError,
    ) as exc:  # encrypted, odd codec
        raise UsageError(f"{path}: not a readable export ZIP ({exc})") from exc
    if not stamps:
        raise UsageError(f"{path}: no cost-/amount- CSV inside")
    return texts, min(stamps).astimezone(timezone.utc)


def _export_members(
    archive: zipfile.ZipFile, path: Path
) -> list[tuple[zipfile.ZipInfo, tuple[str, tuple[date, date]]]]:
    """The archive's cost-/amount- members with their kind and declared range; two of one kind cannot both be it."""
    keyed = [(info, key) for info in archive.infolist() if (key := _member_key(info.filename)) is not None]
    kinds = [key for _, key in keyed]
    for key in {k for k in kinds if kinds.count(k) > 1}:
        raise UsageError(
            f"{path}: two {key[0]} files for {key[1][0]}..{key[1][1]} — which one is the export?"
        )
    return keyed


def _csv_members(path: Path) -> _Texts:
    files = sorted(path.glob("*.csv")) if path.is_dir() else [path, *_sibling(path)]
    texts: _Texts = {}
    for file in files:
        key = _member_key(file.name)
        if key is not None:
            try:
                texts[key] = file.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                raise UsageError(f"{file}: unreadable ({exc})") from exc
    return texts


def _sibling(path: Path) -> list[Path]:
    """The other file of a pair named on the command line (``cost-…`` → ``amount-…``)."""
    swapped = re.sub(r"^(cost|amount)-", lambda m: "amount-" if m.group(1) == "cost" else "cost-", path.name)
    other = path.with_name(swapped)
    return [other] if other != path and other.exists() else []


def _member_key(name: str) -> tuple[str, tuple[date, date]] | None:
    match = _MEMBER.search(name)
    if match is None:
        return None
    try:
        return match.group(1), (date.fromisoformat(match.group(2)), date.fromisoformat(match.group(3)))
    except ValueError:
        raise UsageError(f"{name}: its range is not two dates") from None


_PRIVATE = ("user_id", "api_key", "api_key_name")  # the export's identities: never stored, never said
_EXTRA = "\x00extra"  # where csv puts the fields of a row longer than its header


def _rows(text: str, columns: Sequence[str], kind: str) -> list[dict[str, str]]:
    """The rows of one file; a header or a row that is not exactly the known columns refuses the import."""
    reader = csv.DictReader(io.StringIO(text), restkey=_EXTRA)
    if tuple(reader.fieldnames or ()) != tuple(columns):
        raise UsageError(f"the {kind} file's columns are {reader.fieldnames}, expected {list(columns)}")
    rows = []
    for row in reader:
        if _EXTRA in row or any(value is None for value in row.values()):
            raise UsageError(f"the {kind} file's line {reader.line_num} is not {len(columns)} fields")
        rows.append(dict(row))
    return rows


# ---- checking it ----------------------------------------------------------------------------------------------


def fingerprint(export: Export) -> str:
    """The account as an opaque 16-hex fingerprint; an export of two accounts is refused."""
    ids = {row["user_id"] for row in [*export.cost, *export.amount]}
    if len(ids) != 1:
        raise UsageError(f"the export holds {len(ids)} account ids — one export, one account")
    return hashlib.sha256(ids.pop().encode()).hexdigest()[:16]


def day_totals(export: Export) -> dict[tuple[str, str, str], _DayTotals]:
    """Per (start, end, model): the exact amounts, reconciled in both directions, with every key unique."""
    _unique(export.cost, ("start_time_iso", "end_time_iso", "model", "wallet_type", "currency"), "cost")
    _unique(export.amount, ("start_time_iso", "end_time_iso", "model", "api_key", "type", "price"), "amount")
    totals: dict[tuple[str, str, str], _DayTotals] = defaultdict(_DayTotals)
    for row in export.cost:
        _add_cost(totals[_key(row)], row)
    for row in export.amount:
        _add_amount(totals[_key(row)], row)
    for key, day in totals.items():
        if day.gross != day.priced:
            raise UsageError(
                f"{key}: cost {day.gross} ≠ Σ price × amount {day.priced} — not a consistent export"
            )
        if len(day.currency) > 1:
            raise UsageError(f"{key}: more than one currency {sorted(day.currency)}")
    return totals


def _key(row: Mapping[str, str]) -> tuple[str, str, str]:
    return row["start_time_iso"], row["end_time_iso"], row["model"]


def _unique(rows: Iterable[Mapping[str, str]], columns: Sequence[str], kind: str) -> None:
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        key = tuple(row[c] for c in columns)
        if (
            key in seen
        ):  # named by its day, model and counter: the key, even masked, stays out of every message
            said = {c: v for c, v in zip(columns, key) if c not in _PRIVATE}
            raise UsageError(f"the {kind} file repeats {said} — a duplicated row")
        seen.add(key)


def _decimal(text: str, what: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise UsageError(f"{what}: {text!r} is not a number") from None
    if not value.is_finite() or value < 0:
        raise UsageError(f"{what}: {text!r} is not a non-negative amount")
    return value


def _add_cost(day: _DayTotals, row: Mapping[str, str]) -> None:
    cost = _decimal(row["cost"], f"cost of {_key(row)}")
    day.gross += cost
    day.net += cost if row["wallet_type"] == PAID_WALLET else Decimal(0)
    day.currency.add(row["currency"])


def _add_amount(day: _DayTotals, row: Mapping[str, str]) -> None:
    counter = row["type"]
    if counter not in COUNTERS:
        raise UsageError(f"{_key(row)}: unknown counter {counter!r} (known: {', '.join(COUNTERS)})")
    amount = _decimal(row["amount"], f"{counter} of {_key(row)}")
    if amount != amount.to_integral_value():
        raise UsageError(f"{_key(row)}: {counter} {amount} is not a whole count")
    if counter == "request_count":
        day.requests += int(amount)
        return
    day.tokens[counter] = day.tokens.get(counter, 0) + int(amount)
    day.priced += _decimal(row["price"], f"price of {counter} of {_key(row)}") * amount


# ---- the days of an export ------------------------------------------------------------------------------------


DAY_LENGTHS = (
    timedelta(hours=23),
    timedelta(hours=24),
    timedelta(hours=25),
)  # a daylight-saving day is 23 or 25 h


def export_days(export: Export) -> list[dict[str, Any]]:
    """The export's days as the store keeps them (amounts as text).

    Every day a row names keeps the interval the export states, so a daylight-saving change (a 23 h or 25 h day, the
    offset moving) is stored as it is; the time between two such days is a zero interval (the export covers it and
    billed nothing). Dates of the declared range before the first row or after the last have no boundary the export
    states: they are not stored, and a later export with a row there covers them.
    """
    by_day = _row_days(export)
    days: list[dict[str, Any]] = []
    intervals = sorted(by_day)
    for (start, end), following in zip(intervals, [*intervals[1:], None]):
        days.append(_stored_day(start, end, by_day[(start, end)], export.captured))
        if following is not None and following[0] < end:
            raise UsageError(
                f"days overlap: {start.isoformat()}..{end.isoformat()} and {following[0].isoformat()}"
            )
        if following is not None and end < following[0]:
            gap = _stored_day(end, following[0], [], export.captured)
            days.append(gap | {"currency": days[-1]["currency"]})  # the account's currency, not a default
    return days


def _row_days(export: Export) -> dict[tuple[datetime, datetime], list[dict[str, Any]]]:
    """The lines of every day a row names, by its own interval; a day outside the range or of a strange length refuses."""
    by_day: dict[tuple[datetime, datetime], list[dict[str, Any]]] = defaultdict(list)
    for (first, last, model), day in sorted(day_totals(export).items()):
        start, end = _instant(first), _instant(last)
        if end - start not in DAY_LENGTHS:
            raise UsageError(f"{first}..{last} is not a day (23, 24 or 25 hours)")
        if not export.first <= start.date() <= export.last:
            raise UsageError(f"{first} is outside the declared range {export.first}..{export.last}")
        by_day[(start, end)].append(_line(model, day))
    if not by_day:
        raise UsageError(f"export {export.first}..{export.last}: no row carries its days' boundaries")
    return by_day


def _instant(text: str) -> datetime:
    """A row's day boundary: an ISO time with its offset, as the export writes it."""
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise UsageError(f"{text!r} is not an ISO time with an offset") from None
    if value.utcoffset() is None:
        raise UsageError(f"{text!r} carries no offset — the day boundary is not an instant")
    return value


def _line(model: str, day: _DayTotals) -> dict[str, Any]:
    return {
        "label": model,
        "gross": str(day.gross),
        "net": str(day.net),
        "requests": day.requests,
        "tokens": dict(sorted(day.tokens.items())),
        "currency": next(iter(day.currency), "USD"),
    }


def _stored_day(
    start: datetime, end: datetime, lines: list[dict[str, Any]], captured: datetime
) -> dict[str, Any]:
    currencies = {line.pop("currency") for line in lines}
    if len(currencies) > 1:
        raise UsageError(f"{start.isoformat()}: several currencies {sorted(currencies)}")
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "captured": captured.isoformat(),
        "currency": currencies.pop() if currencies else "USD",
        "lines": lines,
    }


# ---- the store --------------------------------------------------------------------------------------------------


def store_path(state_dir: Path) -> Path:
    """Where the imported days live."""
    return state_dir / STORE


def load_store(path: Path) -> dict[str, Any]:
    """The store, or an empty one when none exists yet; an unreadable store is a ``ToolError`` naming it."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SCHEMA, "account": "", "days": []}
    except (OSError, ValueError, RecursionError) as exc:
        raise ToolError(f"{path}: the DeepSeek store cannot be read ({exc.__class__.__name__})") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("days"), list):
        raise ToolError(f"{path}: not a DeepSeek store of schema {SCHEMA}")
    return data


def merge(
    store: Mapping[str, Any], account: str, days: list[dict[str, Any]]
) -> tuple[dict[str, Any], ImportResult]:
    """The store with the new days: every stored day that intersects the new range is replaced.

    An older capture or another account is refused, so import order never regresses data and accounts never mix.
    """
    if store.get("account") and store["account"] != account:
        raise UsageError(
            "this export belongs to another DeepSeek account than the stored days — one account per store"
        )
    start, end = _parse(days[0]["start"]), _parse(days[-1]["end"])
    hit = [d for d in store["days"] if _parse(d["start"]) < end and _parse(d["end"]) > start]
    newer = [d["start"] for d in hit if _parse(d["captured"]) > _parse(days[0]["captured"])]
    if newer:
        raise UsageError(
            f"an export taken later is already stored for {newer[:3]}… — this one is older, not imported"
        )
    kept = [d for d in store["days"] if d not in hit]
    merged = sorted([*kept, *days], key=lambda d: _parse(d["start"]))
    replaced = {d["start"] for d in hit}
    result = ImportResult(
        added=tuple(d["start"] for d in days if d["start"] not in replaced),
        replaced=tuple(sorted(replaced)),
        kept=len(kept),
        dates=_utc_dates(days),
    )
    return {"schema": SCHEMA, "account": account, "days": merged}, result


def _utc_dates(days: Sequence[Mapping[str, Any]]) -> tuple[date, ...]:
    """The UTC dates whose `daily` files can use these days: only a day that is a whole UTC day fits one."""
    dates = set()
    for day in days:
        start = _parse(day["start"])
        if start.utcoffset() == timedelta(0) and _parse(day["end"]) - start == timedelta(days=1):
            dates.add(start.date())
    return tuple(sorted(dates))


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _take_over_a_dead_lock(lock: Path) -> None:
    """Remove a lock whose process no longer runs (POSIX: signal 0 asks without touching the process)."""
    if os.name != "posix":
        return
    try:
        pid = int(lock.read_text().strip())
        os.kill(pid, 0)
    except FileNotFoundError:
        return
    except ProcessLookupError:
        lock.unlink(missing_ok=True)
    except (OSError, ValueError):
        return  # alive but not ours, or not a pid: the refusal names the file


def write_store(path: Path, store: Mapping[str, Any]) -> None:
    """Atomically, through a file of this process's own: a reader never sees half a store."""
    written: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as tmp:
            written = Path(tmp.name)
            tmp.write(json.dumps(store, indent=1) + "\n")
        written.replace(path)
    except OSError as exc:
        if written is not None:
            written.unlink(missing_ok=True)  # a failed write leaves no copy behind
        raise ToolError(f"cannot write {path}: {exc}") from exc


@contextlib.contextmanager
def store_lock(path: Path) -> Iterator[None]:
    """One import at a time: the lock file is created atomically and removed at the end, whatever happens.

    A second import refuses while it exists. A lock whose process is gone (a crash, a kill) is taken over on POSIX,
    where that can be told; elsewhere it names itself so the user can remove it.
    """
    lock = path.with_name(path.name + ".lock")
    _take_over_a_dead_lock(lock)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise UsageError(
            f"another import is writing {path} ({lock} exists; remove it if none is running)"
        ) from None
    except OSError as exc:
        raise ToolError(f"cannot lock {path}: {exc}") from exc
    try:
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        yield
    finally:
        lock.unlink(missing_ok=True)


def import_export(path: Path, state_dir: Path, captured: datetime | None = None) -> ImportResult:
    """Read, check and merge one export into the store."""
    export = read_export(path, captured)
    account = fingerprint(export)
    days = export_days(export)
    target = store_path(state_dir)
    with store_lock(target):
        try:
            store, result = merge(load_store(target), account, days)
        except (KeyError, TypeError, ValueError) as exc:  # a stored day without its times: nothing is written
            raise ToolError(
                f"{target}: a stored day cannot be read ({exc.__class__.__name__} {exc}) — not imported"
            ) from exc
        write_store(target, store)
    return result


# ---- reading the store for a window -----------------------------------------------------------------------------


def read_days(state_dir: Path, window: Window) -> ProviderDays:
    """The stored days that touch the window, and the parts of the window no import covers (never a zero).

    An absent or unreadable store has no zone to state: its days are not UTC days, and the import that repairs it names
    the ``daily`` dates to rebuild.
    """
    path = store_path(state_dir)
    try:
        store = load_store(path)
    except ToolError as exc:
        return ProviderDays(Provider.DEEPSEEK, SOURCE, unreadable=(Span(window.start, window.end, str(exc)),))
    days, broken = _stored_days(store["days"], window)
    touching = tuple(d for d in days if d.start < window.end and d.end > window.start)
    captured = max((d.captured for d in days), default=None)
    unreadable = tuple(Span(span.start, span.end, f"{path}: {span.why}") for span in broken)
    why = "not in any readable stored day" if broken else "not in any imported export"
    return ProviderDays(
        Provider.DEEPSEEK,
        SOURCE,
        touching,
        tuple(_gaps(days, window, why)),
        captured,
        unreadable,
        utc_days=_utc_store(days),
    )


def _utc_store(days: Sequence[ProviderDay]) -> bool:
    """Whether the store's days are UTC days (an export taken in UTC).

    The whole store answers, not only the window's days, so a day the export has not reached yet stays pending in
    ``daily`` until an import brings it.
    """
    return bool(days) and all(day.start.utcoffset() == timedelta(0) for day in days)


def _stored_days(stored: Sequence[Any], window: Window) -> tuple[list[ProviderDay], list[Span]]:
    """The days the store holds, and a span for each one that cannot be read and touches the window.

    A broken day whose own interval can be read costs only that interval; one whose interval cannot be read could be
    anywhere, so it costs the whole window. Never a zero.
    """
    days, broken = [], []
    for index, entry in enumerate(stored):
        try:
            days.append(_day(entry))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            span = _broken_span(
                entry, window, f"stored day #{index + 1} cannot be read ({exc.__class__.__name__} {exc})"
            )
            broken += [span] if span is not None else []
    return days, broken


def _broken_span(entry: Any, window: Window, why: str) -> Span | None:
    """Where an unreadable stored day lies within the window: its own interval, the whole window, or nowhere."""
    try:
        start, end = _aware(entry["start"]), _aware(entry["end"])
    except (KeyError, TypeError, ValueError):
        return Span(window.start, window.end, why)
    if end <= window.start or start >= window.end:
        return None
    return Span(max(start, window.start), min(end, window.end), why)


def _checked_amount(text: Any) -> Decimal:
    amount = Decimal(str(text))
    if not amount.is_finite():
        raise ValueError(f"{text!r} is not a finite amount")
    return amount


def _aware(text: Any) -> datetime:
    value = datetime.fromisoformat(str(text))
    if value.utcoffset() is None:
        raise ValueError(f"{text!r} carries no offset")
    return value


def _day(stored: Mapping[str, Any]) -> ProviderDay:
    start, end, captured = _aware(stored["start"]), _aware(stored["end"]), _aware(stored["captured"])
    lines = tuple(
        ProviderLine(
            label=str(line["label"]),
            gross=_checked_amount(line["gross"]),
            net=_checked_amount(line["net"]),
            requests=int(line["requests"]),
        )
        for line in stored["lines"]
    )
    return ProviderDay(
        provider=Provider.DEEPSEEK,
        start=start,
        end=end,
        lines=lines,
        closed=end <= captured,
        captured=captured,
        source=SOURCE,
        currency=stored.get("currency", "USD"),
        counts_requests=True,  # every line of the export states its requests
    )


def _gaps(days: Sequence[ProviderDay], window: Window, why: str) -> Iterable[Span]:
    """The parts of the window no stored day covers (days never overlap: every import replaces what it intersects)."""
    cursor = window.start
    for day in sorted(days, key=lambda d: d.start):
        if day.end <= cursor or day.start >= window.end:
            continue
        if day.start > cursor:
            yield Span(cursor, day.start, why)
        cursor = max(cursor, day.end)
    if cursor < window.end:
        yield Span(cursor, window.end, why)
