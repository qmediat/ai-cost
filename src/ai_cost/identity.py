"""One session's own usage: the rows stamped with the session, nothing chosen by time.

A period report counts what happened in a window; a session report counts what the session launched. Every row
carries the session that launched it (``UsageRow.origin_session``): a Claude transcript's id, a usage-log line's
``origin_session``, or what a plugin stamps. A session report keeps the rows stamped with the requested session; rows
of its span stamped for another session and rows without a stamp are counted and said, never summed. A session is not
a period, so no subscription fee is allocated to it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .config import Config
from .errors import UsageError
from .models import Billing, RowKind, UsageRow, Window
from .timeutil import minutes

# --session values that name a period, not a session
PERIODS = ("latest", "all")
# the span: a round launched at the last turn settles within its default 60-minute cap
SPAN_TAIL = minutes(65)
# collection reaches further (a longer cap, a late charge); only the stamped rows of that stretch stay
COLLECT_TAIL = minutes(24 * 60)


@dataclass(frozen=True)
class Unstamped:
    """Rows of the session's span that carry no session stamp, per source and model: counted, never summed."""

    source: str
    provider: str
    model: str
    rows: int


@dataclass(frozen=True)
class SessionSelection:
    """What a session report kept and what it left out."""

    session: str  # the resolved transcript id
    kept: int  # rows stamped with it: the report's rows
    other_sessions: Mapping[str, int]  # rows of the span stamped for another session, per source
    unstamped: tuple[Unstamped, ...]  # rows of the span without a stamp
    # kept runs settled by a ledger charge the report does not hold (their refs)
    unsettled: tuple[str, ...] = ()
    turns: tuple[str, str] = ("", "")  # the session's first and last turn (ISO), "" without a transcript

    @property
    def unstamped_rows(self) -> int:
        """How many rows of the span carry no stamp."""
        return sum(item.rows for item in self.unstamped)


def is_identity(session: str | None) -> bool:
    """Whether ``--session`` names one session (an id, a prefix, a transcript path) rather than a period."""
    return bool(session) and session not in PERIODS


def resolve_session(requested: str, files: Sequence[tuple[str, Path]]) -> str:
    """The one session ``--session`` names: its transcript id.

    A prefix behind two transcripts and a subagent transcript are usage errors; with no transcript the id is taken
    as given, and its rows are found by their stamp alone (the caller asks for the dates to read).
    """
    if Path(requested).is_file() and Path(requested).parent.name == "subagents":
        raise UsageError(f"--session {requested}: a subagent transcript — pass its parent session")
    ids = sorted({session for session, _ in files})
    if len(ids) > 1:
        shown = ", ".join(ids[:4]) + (", …" if len(ids) > 4 else "")
        raise UsageError(f"--session {requested} names {len(ids)} sessions ({shown}) — give more of the id")
    return ids[0] if ids else requested


def session_span(span: Window) -> Window:
    """The stretch whose other rows a session report counts: its turns, a minute before, ``SPAN_TAIL`` after."""
    return Window(span.start - minutes(1), span.end + SPAN_TAIL)


def inherit_origin_by_ref(rows: Sequence[UsageRow]) -> list[UsageRow]:
    """An unstamped ledger row takes the stamp of the CLI session it charges: a session row with the same ``ref``.

    A ledger line names its session only by the session's id, which is the session row's ``ref``. Nothing else
    inherits: two writers may reuse a free-text ref, and equal text is no proof of one owner. A ref whose session
    rows carry two stamps lends none.
    """
    stamps: dict[str, set[str]] = {}
    for row in rows:
        if row.kind is RowKind.SESSION and row.ref and row.origin_session:
            stamps.setdefault(row.ref, set()).add(row.origin_session)
    return [_inherited(row, stamps) for row in rows]


def _inherited(row: UsageRow, stamps: Mapping[str, set[str]]) -> UsageRow:
    found = stamps.get(row.ref, set())
    if row.kind is not RowKind.LEDGER or row.origin_session or len(found) != 1:
        return row
    return replace(row, origin_session=next(iter(found)))


def select_session(
    rows: Sequence[UsageRow], session: str, span: Window | None = None
) -> tuple[list[UsageRow], SessionSelection]:
    """The rows stamped with ``session`` (from the whole collection), and the count of what ``span`` holds besides."""
    kept = [row for row in rows if row.origin_session == session]
    around = [row for row in rows if span is None or row.at is None or span.contains(row.at)]
    others = Counter(row.source for row in around if row.origin_session and row.origin_session != session)
    loose = Counter((row.source, row.provider.value, row.model) for row in around if not row.origin_session)
    unstamped = tuple(Unstamped(*key, count) for key, count in sorted(loose.items()))
    selection = SessionSelection(
        session, len(kept), dict(sorted(others.items())), unstamped, _unsettled(kept)
    )
    return kept, selection


def _unsettled(kept: Sequence[UsageRow]) -> tuple[str, ...]:
    """Runs whose cash a ledger charge carries (``API_SETTLED``) while no kept ledger row holds that charge."""
    charged = {row.ref for row in kept if row.kind is RowKind.LEDGER}
    return tuple(sorted({row.ref for row in kept if row.billing is Billing.API_SETTLED} - charged))


def selection_warnings(selection: SessionSelection) -> list[str]:
    """The header lines of a session report: what is counted, and what was left out and why."""
    said = [
        f"session {selection.session}: the {selection.kept} row(s) stamped with it are counted, from every project; "
        "no subscription fee is allocated to a session"
    ]
    if selection.other_sessions:
        said.append(
            f"{sum(selection.other_sessions.values())} row(s) of other sessions in the span left out "
            f"({source_counts(selection.other_sessions)})"
        )
    if selection.unstamped:
        by_source = Counter[str]()
        for item in selection.unstamped:
            by_source[item.source] += item.rows
        said.append(
            f"{selection.unstamped_rows} row(s) in the span carry no session stamp — not counted, listed under "
            f"the report ({source_counts(by_source)})"
        )
    if selection.unsettled:
        said.append(
            f"{len(selection.unsettled)} run(s) paid by a ledger charge this report does not hold "
            f"({', '.join(selection.unsettled[:3])}): their cash is missing from the total"
        )
    return said


def without_shares(config: Config) -> Config:
    """The config a session report prices with: every plan attributed ``none``, so no fee lands on the session."""
    return replace(
        config, subscriptions=tuple(replace(sub, attribution="none") for sub in config.subscriptions)
    )


def source_counts(by_source: Mapping[str, int]) -> str:
    """``name ×count`` per source, sorted: the one way a header lists counts."""
    return ", ".join(f"{name} ×{count}" for name, count in sorted(by_source.items()))
