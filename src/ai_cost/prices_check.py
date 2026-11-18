"""Price drift: fetch each vendor page, find every model by its id or display name, compare the amounts that follow.

Never rewrites a price on its own: ``check`` reports, ``update`` writes only token-tier models with an unambiguous
input/output pair to the USER file (ADR-0002: the shape decides what may be written). A background check is started
detached when the last one is older than ``auto_check_days``; reports never wait for the network.
"""

from __future__ import annotations

import bisect
import contextlib
import dataclasses
import functools
import html
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import SAME_MODEL_TAIL, Paths, PriceBook, builtin_prices, deep_merge
from .errors import ConfigError, ToolError, UsageError
from .models import CheckStatus, PeakOffpeakPrice, PriceEntry, Provider, TokenTierPrice
from .netread import TRANSPORT_ERRORS, read_body
from .pricing import current_price
from .process import self_command
from .timeutil import iso, now, parse_ts
from .values import money

# The checker says who it is. A vendor page that refuses this agent is reported as fetch-failed; the checker never
# presents itself as a browser to get past that.
USER_AGENT = f"ai-cost/{__version__} (+https://github.com/qmediat/ai-cost)"
AMOUNT = re.compile(r"\$\s?(\d+(?:[.,]\d+)?)")
SCAN_SPAN = 900
LOCK_SECONDS = 600


@dataclass
class ModelCheck:
    """Outcome for one model."""

    status: CheckStatus
    expected: list[float]
    seen: list[float] = field(default_factory=list)
    # the expected amounts the model's nearest row lacks ("cache_read 1"): what a CHANGED status is about
    missing: list[str] = field(default_factory=list)
    # cache and long-context rates of a vendor that lists them apart from the model's row, absent from the confirming
    # row: said (confirm by hand), never a CHANGED status — the page may list them elsewhere or not at all
    unchecked: list[str] = field(default_factory=list)


@dataclass
class ProviderCheck:
    """Outcome for one provider page."""

    url: str
    status: str
    models: dict[str, ModelCheck] = field(default_factory=dict)
    # model ids the vendor's page names that the price book neither prices nor lists as no_row: new models
    unlisted: list[str] = field(default_factory=list)
    # when this entry was read, where that is not the saved file's checked_at: an entry kept from an earlier check, or
    # one read by a check limited by --providers; empty = read at the file's checked_at
    read_at: str = field(default="", metadata={"internal": True})  # written to the state file by ``to_json``
    # this check could not read the vendor's page or its twin: a missing observation, never a state of the prices
    unread: bool = field(default=False, metadata={"internal": True})

    def kept(self) -> bool:
        """An unread vendor whose saved entry stays: it holds models or unlisted ids (a fresh unread entry holds none)."""
        return self.unread and bool(self.models or self.unlisted)

    def read_when(self) -> str:
        """When the entry was read, in the words every line about it uses."""
        return f"read at {self.read_at}" if self.read_at else "of an earlier check"


def _amount(item: Any) -> float:
    """One state-file amount; a null, a boolean, a non-number or a value no float holds is a ``ValueError``."""
    amount = money(item, "amount")
    if amount is None:
        raise ValueError("amount is null")
    return amount


def _amounts(value: Any) -> list[float]:
    """The amounts of a state-file entry; a corrupt entry (not a list, or any item not an amount) is an empty list."""
    if not isinstance(value, list):
        return []
    try:
        return [_amount(item) for item in value]
    except ValueError:
        return []


def _labels(value: Any) -> list[str]:
    """A state-file list of labelled amounts; anything else (an older file, a corrupt entry) is an empty list."""
    return [str(item) for item in value if isinstance(item, str)] if isinstance(value, list) else []


@dataclass
class CheckResult:
    """Everything one run learned; serialised to the state file."""

    checked_at: str
    providers: dict[str, ProviderCheck] = field(default_factory=dict)
    # limited by --providers to some of the registry's vendors: saved, it keeps the others and the saved checked_at
    limited: bool = field(default=False, metadata={"internal": True})

    def drift(self) -> list[tuple[str, str]]:
        """``(provider, model)`` pairs whose amounts did not match."""
        return [
            (p, m)
            for p, prov in self.providers.items()
            for m, check in prov.models.items()
            if check.status is CheckStatus.CHANGED
        ]

    def to_json(self) -> dict[str, Any]:
        """Plain data for the state file."""
        return {
            "checked_at": self.checked_at,
            "providers": {
                name: {
                    "url": prov.url,
                    "status": prov.status,
                    "models": {
                        m: {
                            "status": c.status.value,
                            "expected": c.expected,
                            "seen": c.seen[:12],
                            "missing": c.missing,
                            "unchecked": c.unchecked,
                        }
                        for m, c in prov.models.items()
                    },
                    "unlisted": prov.unlisted,
                    "read_at": prov.read_at,
                    "unread": prov.unread,
                }
                for name, prov in self.providers.items()
            },
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CheckResult:
        """The state file back into a result (unknown statuses count as changed)."""
        result = cls(checked_at=str(data.get("checked_at", "")))
        providers = data.get("providers")
        for name, prov in (providers.items() if isinstance(providers, Mapping) else ()):
            if not isinstance(prov, Mapping):
                continue  # a corrupt entry is dropped: the next check rewrites the file
            checks = {}
            models = prov.get("models")
            for model, check in (models.items() if isinstance(models, Mapping) else ()):
                if not isinstance(check, Mapping):
                    continue
                try:
                    status = CheckStatus(check.get("status"))
                except ValueError:
                    status = CheckStatus.CHANGED
                checks[model] = ModelCheck(
                    status,
                    _amounts(check.get("expected")),
                    _amounts(check.get("seen")),
                    _labels(check.get("missing")),
                    _labels(check.get("unchecked")),
                )
            result.providers[name] = ProviderCheck(
                str(prov.get("url", "")),
                str(prov.get("status", "")),
                checks,
                _labels(prov.get("unlisted")),
                str(prov.get("read_at") or ""),
                unread=prov.get("unread") is True,
            )
        return result


# ---- fetching -----------------------------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every 3xx surfaces as an ``HTTPError``: ``fetch`` decides itself where it may go."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http_only(url: str) -> None:
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise urllib.error.URLError(f"{url!r} refused: only http(s) pages are read")


_REDIRECTS = (301, 302, 303, 307, 308)


def _redirect(exc: urllib.error.HTTPError, current: str) -> str | None:
    """The target of a redirect response (http(s) only), ``None`` when the response is not a redirect."""
    location = exc.headers.get("Location") if exc.headers else None
    if exc.code not in _REDIRECTS or not location:
        return None
    target = urllib.parse.urljoin(current, location)
    _http_only(target)
    return target


def _get(url: str, agent: str, hops: int, timeout: int, opener: Callable[..., Any]) -> str:
    """One user agent: follow redirects by hand; a loop or too many hops raises a 310, everything else its own error."""
    current = url
    for _ in range(hops):
        headers = {
            "User-Agent": agent,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "en",
        }
        request = urllib.request.Request(current, headers=headers)
        try:
            with opener(request, timeout=timeout) as response:
                return str(read_body(response, PAGE_CAP).decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            target = _redirect(exc, current)
            if target is None:
                raise
            if target == current:
                raise urllib.error.HTTPError(current, 310, "redirect loop", None, None) from exc  # type: ignore[arg-type]
            current = target
    raise urllib.error.HTTPError(current, 310, "too many redirects", None, None)  # type: ignore[arg-type]


def fetch(
    url: str,
    timeout: int = 15,
    hops: int = 5,
    agent: str = USER_AGENT,
    opener: Callable[..., Any] = _OPENER.open,
) -> str:
    """GET following redirects by hand (3.9's urllib ignores 308), as the one agent the checker is.

    Only http(s) targets are followed: a page must not redirect the checker to a local file.
    """
    _http_only(url)
    return _get(url, agent, hops, timeout, opener)


def page_text(raw: str) -> str:
    """Visible text of an HTML page, whitespace collapsed; a heading is kept as a `## ` marker, a table as markdown."""
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    raw = _html_tables(raw)
    # a heading stays a section marker (`## Batch ¶`), as in a twin: where it starts and where its own text ends
    raw = re.sub(r"(?i)<h[1-4](?:\s[^>]*)?>", " ## ", raw)
    raw = re.sub(r"(?i)</h[1-4]>", f" {HEADING_END} ", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return _collapse(text)


_SPACE = re.compile(r"\s+")


def _collapse(text: str) -> str:
    """Whitespace collapsed to one character: a space, or a line end where a table row ends or starts.

    A table row is one line, so the reader finds a table's rows by their lines (``page_tables``); the line end takes the
    place of one space, so every other reading sees the same positions.
    """

    def one(found: re.Match[str]) -> str:
        at_bar = text[found.start() - 1 : found.start()] == "|" or text[found.end() : found.end() + 1] == "|"
        return "\n" if at_bar and "\n" in found.group(0) else " "

    return _SPACE.sub(one, text)


def _flat(text: str) -> str:
    """Whitespace collapsed to one space: the text of one cell or one name."""
    return _SPACE.sub(" ", text)


_TABLE_TAG = re.compile(r"(?i)<(/?)table\b[^>]*>")
_HTML_ROW = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr>")
_HTML_CELL = re.compile(r"(?is)<t([dh])\b([^>]*)>(.*?)</t\1>")
_HTML_SPAN = re.compile(r"(?i)\b(col|row)span\s*=\s*[\"'{]?\s*(\d+)")  # quoted, bare or JSX (rowSpan={2})
MAX_SPAN = 50  # columns or rows one HTML cell may cover: a larger span is markup noise, not a layout


@dataclass(frozen=True)
class HtmlCell:
    """One cell of an HTML table: its text, and the columns and rows it covers."""

    text: str
    cols: int = 1
    rows: int = 1


def _html_cells(row: str) -> list[HtmlCell]:
    """The cells of one ``<tr>``: their tags dropped, their entities read, a ``|`` escaped as a markdown table writes it.

    An entity bar (``&#124;``) is read before the bar is escaped, so it stays in its cell; the rest is escaped again for
    the page's own unescape.
    """
    cells = []
    for found in _HTML_CELL.finditer(row):
        spans = {
            kind.lower(): min(max(int(count), 1), MAX_SPAN)
            for kind, count in _HTML_SPAN.findall(found.group(2))
        }
        text = _flat(html.unescape(re.sub(r"(?s)<[^>]+>", " ", found.group(3)))).strip()
        text = html.escape(text.replace("|", "\\|"), quote=False)
        cells.append(HtmlCell(text, spans.get("col", 1), spans.get("row", 1)))
    return cells


def _held(line: list[str], below: dict[int, int]) -> None:
    """Empty cells where a cell of an earlier row still spans down (``below``: column → rows still covered)."""
    while below.get(len(line), 0) > 0:
        below[len(line)] -= 1
        line.append("")


def _html_grid(table: str) -> list[list[str]]:
    """The table's rows, every cell at the column it stands in.

    A cell spanning columns repeats its text in each, as the vendor's markdown twin writes it (a price two models share,
    a model's name over its sub-columns). A cell spanning rows holds only its first: a model's name over its two tier
    rows (Anthropic, Alibaba) would read as two of its rows, and a twin writes such rows each with its own label.
    """
    grid: list[list[str]] = []
    below: dict[int, int] = {}
    for row in _HTML_ROW.findall(table):
        line: list[str] = []
        for cell in _html_cells(row):
            _held(line, below)
            for _ in range(cell.cols):
                below[len(line)] = cell.rows - 1
                line.append(cell.text)
        _held(line, below)
        grid.append(line)
    return grid


@dataclass(frozen=True)
class HtmlTable:
    """Where one HTML table stands in a text: the whole element, and its body between its own two tags."""

    start: int
    end: int
    body: tuple[int, int]


def _leaf_tables(raw: str) -> list[HtmlTable]:
    """The closed HTML tables of ``raw`` that hold no other table, in page order.

    A table that holds another is layout: its tags stay text and the tag strip drops them, so the table inside keeps its
    own rows and columns. A ``</table>`` without its ``<table`` closes nothing; a ``<table`` never closed stays text.
    """
    leaves: list[HtmlTable] = []
    open_tags: list[tuple[re.Match[str], bool]] = []  # each table still open, and whether it holds another
    for tag in _TABLE_TAG.finditer(raw):
        if not tag.group(1):
            if open_tags:
                open_tags[-1] = (open_tags[-1][0], True)
            open_tags.append((tag, False))
            continue
        if not open_tags:
            continue
        opened, holds = open_tags.pop()
        if not holds:
            leaves.append(HtmlTable(opened.start(), tag.end(), (opened.end(), tag.start())))
    return leaves  # disjoint, so the order they close in is the order they start in


def _html_tables(raw: str) -> str:
    """``raw`` with every HTML table that holds no other written as a markdown table (``_markdown_table``)."""
    parts, done = [], 0
    for table in _leaf_tables(raw):
        parts += [
            raw[done : table.start],
            _markdown_table(raw[table.start : table.end], raw[slice(*table.body)]),
        ]
        done = table.end
    return "".join(parts) + raw[done:]


def _markdown_table(element: str, body: str) -> str:
    """An HTML table as a markdown table (a header row, a separator, a line per row): the page and its twin read alike."""
    grid = _html_grid(body)
    width = max((len(line) for line in grid), default=0)
    if not width:
        return element  # no rows the reader knows: the table stays text, as before
    lines = ["| " + " | ".join(line + [""] * (width - len(line))) + " |" for line in grid]
    lines.insert(1, "|" + " --- |" * width)
    return "\n" + "\n".join(lines) + "\n"


# what a page read can raise: every transport failure (netread.TRANSPORT_ERRORS, an HTTP status included) and a URL
# urllib cannot parse (ValueError, a page over PAGE_CAP — ``PageTooLarge`` — included) — the vendor is unread and the check goes on with the next source
PAGE_UNREADABLE = (*TRANSPORT_ERRORS, ValueError)
PAGE_CAP = 2_000_000  # bytes of a page the check reads; a longer page is unread (``PageTooLarge``)
MARKDOWN_TWIN = ".md"
NO_TWIN = (404, 410)  # the statuses of ``<url>.md`` that say the vendor serves no markdown twin
HEADING_END = (
    "\u00b6"  # where a heading's own text ends once whitespace is collapsed: its body is not its title
)


def fetch_variants(url: str, timeout: int = 15) -> list[str]:
    """The page text as the checker sees it, as the list of variants the matching step walks.

    The page itself, then its markdown twin (``<url>.md``) where the vendor serves one: a page rendered by script
    answers with an empty shell, and its twin carries the whole table. A vendor without a twin gives one variant —
    the provider's status line counts them.
    """
    if url.endswith(MARKDOWN_TWIN):  # a source that is the markdown itself: read as one, it has no twin
        return [twin_text(fetch(url, timeout))]
    return [page_text(fetch(url, timeout)), *_twin(url + MARKDOWN_TWIN, timeout)]


class TwinUnread(Exception):
    """The page was read, its markdown twin was not: without the twin the page's matching is not to be trusted."""

    def __init__(self, url: str, cause: BaseException) -> None:
        super().__init__(f"{url}: {cause.__class__.__name__}")
        self.url = url
        self.cause = cause


def _twin(url: str, timeout: int) -> list[str]:
    """The twin's text as a one-item list; empty only when the vendor answers that it has none (``NO_TWIN``).

    Any other failure is a ``TwinUnread``: a twin that timed out or arrived cut off is not "no twin".
    """
    try:
        return [twin_text(fetch(url, timeout))]
    except urllib.error.HTTPError as exc:
        if exc.code in NO_TWIN:
            return []  # no twin: the status line says "1 page variant"
        raise TwinUnread(url, exc) from exc
    except PAGE_UNREADABLE as exc:
        raise TwinUnread(url, exc) from exc


# what leaves a vendor unread — its page or its twin; a cut-off body is an unread page, never a crash of the check
VENDOR_UNREAD = (*PAGE_UNREADABLE, TwinUnread)


def twin_text(raw: str) -> str:
    """A markdown twin's text, whitespace collapsed — its ``<= 200k … >`` is prose, never a tag to strip.

    A vendor that answers ``<url>.md`` with an HTML page (no twin, a catch-all route) is read as the page it is.
    """
    if raw.lstrip("\ufeff \t\r\n")[:1] == "<":
        return page_text(raw)
    # an HTML table inside the markdown is read as the page reads one; the prose around it stays as written
    marked = re.sub(r"(?m)^(#{1,4} [^\n]*)$", rf"\1 {HEADING_END}", _html_tables(_CALLOUT.sub(" ", raw)))
    return _collapse(html.unescape(_BAR_ENTITY.sub(r"\\|", marked)))


# a bar written as an entity: text inside a table cell, never a cell border — read as the escaped bar it stands for
_BAR_ENTITY = re.compile(r"&(?:#124|#[xX]0*7[cC]|vert|verbar|VerticalLine);")


# a notice box of a markdown page: it names the model that replaces a deprecated one before that model's own table,
# and a row ends at the next model name — the notice is prose about the row, never part of it
_CALLOUT = re.compile(r"\{%\s*callout\b.*?\{%\s*/callout\s*%\}", re.S)


# ---- matching -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rows:
    """Where a model's row ends on a vendor's page: at the next model name the page shows (``SCAN_SPAN`` at most).

    ``heads`` is ``None`` when nothing names the vendor's models: only the span bounds what belongs to a name, and a
    name is found inside a compound label. A table that names its models in its head is read by its columns
    (``occurrences``), at the table's place in the page.
    """

    heads: re.Pattern[str] | None
    # the names the price book has rows for: before a row's first amount only one of THESE ends it — the vendor's
    # family name in a sentence (`Our efficient Gemini 4 frontier reasoning model`) is prose, not the next row
    known: re.Pattern[str] | None = None
    # the vendor lists a model's long-prompt rates as part of what its row confirms (``CACHE_IN_ROW``): the model's
    # own long-labelled row (`Claude Haiku 5.5 (for prompts over 100,000 tokens)`) then continues its standard row
    long_in_row: bool = False
    # how the vendor writes a model (the price book's ``model_ids`` / ``model_names``): a head that matches is a model,
    # priced or not, and ends a row; a head that matches none and has no row is the family name in a sentence
    models: tuple[re.Pattern[str], ...] = ()


WHOLE_SPAN = Rows(None)


def page_rows(provider: Provider, names: Sequence[str], models: Sequence[re.Pattern[str]] = ()) -> Rows:
    """How the page's rows end: at the next model name, in either form the vendor writes one.

    A name starts with the first word of a display name (``Claude ``, ``Gemini ``, ``Grok ``, case as shown) or with
    the letters of an id (``claude``, ``gpt``, ``qwen``, any case) — a page may list its rows under either.
    """
    shown = sorted({name.split(" ")[0] + " " for name in names if " " in name})
    ids = sorted(
        {found.group(0) for name in names if " " not in name and (found := re.match(r"[A-Za-z]+", name))}
    )
    forms = [
        f"(?:{'|'.join(map(re.escape, shown))})" if shown else "",
        f"(?i:{'|'.join(map(re.escape, ids))})" if ids else "",
    ]
    alternatives = "|".join(form for form in forms if form)
    if not alternatives:
        return WHOLE_SPAN
    exact = "|".join(re.escape(name) for name in sorted(set(names), key=len, reverse=True))
    return Rows(
        re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives})(?=[\s-]?[A-Za-z0-9])"),
        re.compile(rf"(?i:{exact})(?![A-Za-z0-9]|[.-][A-Za-z0-9])"),
        provider in CACHE_IN_ROW,
        tuple(models),
    )


_WHOLE = r"(?<![A-Za-z0-9.-]){}(?![A-Za-z0-9]|[.-][A-Za-z0-9])"  # a name, not part of a longer one

# A markdown page says which service tier a table belongs to: a heading (`### Batch pricing data`) or a tab
# (`{% tab title="Flex" %}`). The rates of another tier are a fixed multiple of the standard ones (batch 0.5x, fast 2x),
# so such a row would "confirm" a price that halved or doubled; it never confirms, and it ends a standard row.
_SECTION = re.compile(
    rf'(?:(?:\b(Standard|Batch|Flex|Fast|Ultrafast|Priority) )?#{{2,4}} |tab title=")([^|#"%{HEADING_END}]{{0,60}})'
)
_OTHER_TIER = re.compile(r"\b(?:batch|flex|fast|ultrafast|priority|long[- ]context)\b", re.I)
# a row the vendor labels as its long-prompt tier (`grok-4.7 (≥ 200k prompt tokens)`): the same rule
_LONG_ROW = re.compile(r"\s*\(\s*(?:≥|>=|>|for prompts over)")


@functools.lru_cache(maxsize=4)  # one scan per page text, not per model name
def _tier_starts(text: str) -> tuple[list[int], list[bool]]:
    """Where each section of ``text`` starts, and whether it is another service tier than the standard one."""
    found = list(_SECTION.finditer(text))
    return [m.start() for m in found], [_other_tier(m.group(1), m.group(2)) for m in found]


def _other_tier(label: str | None, title: str) -> bool:
    """Whether a section is another service tier than the standard one.

    By the tab label a page prints before a heading (`Fast ### Grouped Pricing Table data`, `Standard ### …`) when it
    is a tier word, else by the heading itself.
    """
    if label:
        return label != "Standard"
    return bool(_OTHER_TIER.search(title))


def _standard_row(text: str, start: int, end: int, tiers: tuple[list[int], list[bool]]) -> str | None:
    """The row ``text[end:]`` of a name at ``start`` cut at the next other-tier section; ``None`` inside one."""
    starts, other = tiers
    index = bisect.bisect_right(starts, start) - 1
    if (index >= 0 and other[index]) or _LONG_ROW.match(text, end):
        return None
    cut = next((starts[i] for i in range(index + 1, len(starts)) if other[i]), len(text))
    return text[end:cut]


_SEPARATOR_ROW = re.compile(r"\|(?:\s*:?-{3,}:?\s*\|)+")
_CELL_BORDER = re.compile(r"(?<!\\)\|")
_LINE = re.compile(r"[^\n]+")
# what a header cell shows beside a name: a markdown link's address, an html tag (`<sup>1</sup>`)
_LINK_TEXT = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TAG = re.compile(r"<[^>]+>")
# a footnote marker after a name in a header cell (`deepseek-flash(1)`, `deepseek-flash¹`, `deepseek-flash 1`)
_FOOTNOTE = re.compile(r"\s*(?:\(\d{1,2}\)|\[\^?\d{1,2}\]|[*†‡¹²³⁴⁵⁶⁷⁸⁹]+)$|\s\d{1,2}$")


@dataclass(frozen=True)
class Table:
    """A markdown table of a page's text, one line per row: where each row's line stands, and its cells.

    ``rows[0]`` is the header row. The rows before the first row with an amount are the table's head (a caption row, the
    row that names the models, a row of sub-labels): a model named in a head cell is priced in that cell's column. A
    table without an amount (features, context windows) has no head: it prices nothing, so it decides nothing.
    """

    lines: tuple[tuple[int, int], ...]
    rows: tuple[tuple[str, ...], ...]
    head: int


@dataclass(frozen=True)
class HeadCell:
    """A place in a table's head, in a column after the first (the first column labels the rows)."""

    table: Table
    row: int
    column: int


def _is_row(line: str) -> bool:
    """Whether a line is a table row: it starts and ends with a cell border."""
    stripped = line.strip()
    return len(stripped) > 1 and stripped[0] == "|" and stripped[-1] == "|"


def _cells(line: str, width: int) -> tuple[str, ...]:
    """A row's cells, as many as the table's columns: a short row's missing cells are empty, a long row's extra go."""
    cells = [cell.strip() for cell in _CELL_BORDER.split(line.strip())[1:-1]]
    return tuple(cells[:width] + [""] * (width - len(cells)))


def _table(text: str, lines: list[tuple[int, int]], separator: int, width: int) -> Table:
    """The table whose separator is ``lines[separator]``: its header line, then every row line after the separator.

    A separator line met in the body is the next table's: the row before it is that table's header.
    """
    spans = [lines[separator - 1]]
    for start, end in lines[separator + 1 :]:
        line = text[start:end]
        if _SEPARATOR_ROW.fullmatch(line.strip()):
            if len(spans) > 1:
                spans.pop()
            break
        if not _is_row(line):
            break
        spans.append((start, end))
    rows = tuple(_cells(text[start:end], width) for start, end in spans)
    head = next((index for index, row in enumerate(rows) if any(AMOUNT.search(cell) for cell in row)), 0)
    return Table(tuple(spans), rows, head)


@functools.lru_cache(maxsize=4)  # one parse per page text, not per model name
def page_tables(text: str) -> tuple[Table, ...]:
    """Every markdown table of ``text``: a row line right before a separator line, and the row lines after it."""
    lines = [found.span() for found in _LINE.finditer(text)]
    found = []
    for index in range(1, len(lines)):
        separator = text[slice(*lines[index])].strip()
        if _SEPARATOR_ROW.fullmatch(separator) and _is_row(text[slice(*lines[index - 1])]):
            found.append(_table(text, lines, index, separator.count("|") - 1))
    return tuple(found)


def _head_cell(tables: Sequence[Table], text: str, at: int) -> HeadCell | None:
    """The head cell where ``text[at]`` stands, ``None`` outside every table's head and in a first column."""
    for table in tables:
        for row, (start, end) in enumerate(table.lines[: table.head]):
            if start <= at < end:
                column = len(_CELL_BORDER.findall(text, start, at)) - 1
                return HeadCell(table, row, column) if column > 0 else None
    return None


def _names_model(cell: str, wanted: set[str]) -> bool:
    """Whether a header cell is one of the model's names: link text and tags read, emphasis and a footnote left out."""
    plain = _flat(_TAG.sub(" ", _LINK_TEXT.sub(r"\1", cell))).strip("*_` ").lower()
    return plain in wanted or _FOOTNOTE.sub("", plain).strip("*_` ") in wanted


def _column_reading(head: HeadCell, wanted: set[str]) -> Reading | None:
    """The amounts under every cell of ``head``'s row that names the model, row by row.

    ``None`` when ``head``'s own cell names the model only inside a longer text (two models, a sentence): that is no
    column of the model, and no row either.
    """
    line = head.table.rows[head.row]
    columns = [index for index, cell in enumerate(line) if index and _names_model(cell, wanted)]
    if head.column not in columns:
        return None
    below = head.table.rows[head.row + 1 :]
    return Reading(
        [amount for row in below for index in columns for amount in _figures(row[index])], column=True
    )


def _figures(text: str) -> list[float]:
    """The dollar amounts of ``text``, in order."""
    return [float(found.replace(",", "")) for found in AMOUNT.findall(text)]


@dataclass(frozen=True)
class Reading:
    """What one place that names the model reads: its amounts — a row's, or a table column's.

    A column decides even when it holds no amount: its table prices other models in its columns, and a column the
    reader cannot read is no price — never the figures of another column or of the rows after it. So a model named in
    an earlier pricing table's head with no price there is not found, never priced from a later table or section; a
    table with no amount at all has no head and decides nothing. A head cell that holds more than the model's name
    (``deepseek-v4-pro (128K)``) gives no reading, never the row after it: the model is read where else the page names
    it, not found when nowhere — the price book lists the cell's text as one of the model's names (``display``, an
    alias) to read that column.
    """

    amounts: list[float]
    column: bool = False

    @property
    def decides(self) -> bool:
        """Whether this reading is the model's price (an empty one: not found)."""
        return self.column or bool(self.amounts)


def _own_pattern(needle: str, own: Sequence[str], rows: Rows) -> re.Pattern[str] | None:
    """The names that stand for the model in its own row: its other names, and its tail-extended needle.

    On a page in rows the needle with a same-model tail (its dated snapshot, its -preview twin) is the same row's model.
    """
    own_names = [rf"{re.escape(n)}{SAME_MODEL_TAIL}(?![A-Za-z0-9.-])" for n in own]
    if rows.heads:
        own_names.append(rf"{re.escape(needle)}(?=[-\[]){SAME_MODEL_TAIL}(?![A-Za-z0-9.-])")
    return re.compile("|".join(own_names), re.I) if own_names else None


def occurrences(
    text: str,
    needle: str,
    span: int = SCAN_SPAN,
    rows: Rows = WHOLE_SPAN,
    own: Sequence[str] = (),
    excluded: list[int] | None = None,
) -> list[Reading]:
    """What EVERY occurrence of ``needle`` as a whole name reads (menus, prose, tables), in page order.

    In a table's head the name reads its column (``_column_reading``), once per table; anywhere else the row after it.
    On a page in rows a name inside a longer one is none (``Claude Fable 5`` in ``Claude Fable 5.1``, ``gpt-5.6`` in
    ``gpt-5.6-sol``) and a row ends where the page's next model name starts, ``span`` characters after the name at most.
    Another name of the model itself (``own``: its id after its display name) does not end its row while no amount has
    come yet, nor does one of its endpoints (``gemini-3.1-pro-preview-customtools``); another version
    (``claude-opus-5-5``) does, and so does the needle itself again (the model's next row, or an example).
    """
    name = re.compile((_WHOLE if rows.heads else "{}").format(re.escape(needle)), re.I)
    mine = _own_pattern(needle, own, rows)
    wanted = {n.lower() for n in (needle, *own)}
    tables, tiers = page_tables(text), _tier_starts(text)
    out: list[Reading] = []
    read: set[tuple[int, int]] = set()  # the tables whose columns the name has read, by their header line
    for found in name.finditer(text):
        standard = _standard_row(text, found.start(), found.end(), tiers)
        # a row of another tier: the text prices the model, in a table that never confirms
        if standard is None:
            if excluded is not None:
                excluded.append(found.start())
            continue
        head = _head_cell(tables, text, found.start())
        if head is None:
            row = standard[: found.start() + span - found.end()]
            out.append(Reading(_figures(row[: _row_end(row, rows, mine, name)])))
        elif head.table.lines[0] not in read:
            column = _column_reading(head, wanted)
            if column is not None:
                read.add(head.table.lines[0])
                out.append(column)
    return out


def _row_end(row: str, rows: Rows, mine: re.Pattern[str] | None, name: re.Pattern[str] | None = None) -> int:
    """Where the next model's name starts in ``row`` (its end when none does).

    The model's other names are passed over only in the row's header, before its first amount (Google prints the id
    and its endpoints after the display name); after an amount, any name starts another row — the model's own batch
    row too, so two of its rows never pool their figures. A stray amount before the id ends the header early: the row
    is then cut at the id, a check that can only turn louder (changed? or not found), never quieter. A name that extends
    the model's own with a word (``…-customtools``) in the header is read as one of its endpoints — without that,
    gemini-3.1-pro-preview is not found on the live Google page (measured 2026-09-30).
    """
    links = [found.span() for found in _PROSE_LINK.finditer(row)] if rows.heads else []
    pooled = 0  # the end of the model's own long-row label: a head inside it (`Haiku 5.5` in its name) is that label
    for head in rows.heads.finditer(row) if rows.heads else ():
        if head.start() < pooled:
            continue
        in_header = not AMOUNT.search(row, 0, head.start())
        own = bool(mine and mine.match(row, head.start()))
        linked = any(start <= head.start() < end for start, end in links)
        prose = bool(
            rows.known
            and not rows.known.match(row, head.start())
            and not any(model.match(row, head.start()) for model in rows.models)
        )
        again = name.match(row, head.start()) if name and rows.long_in_row else None
        if again and _LONG_ROW.match(row, again.end()):
            pooled = (
                again.end()
            )  # the model's own long-prompt row: its rates belong to what this row confirms
            continue
        if not (in_header and (own or linked or prose)):
            return head.start()
    return len(row)


# another model named as a markdown link in a row's header (`compared to [Gemini 4 Carbon](/docs/pricing#…)`, the
# link's address included): prose
# about the model, before its table — passed over like the model's own names; after an amount it starts a row
_PROSE_LINK = re.compile(r"\[[^\]\[|]{1,60}\]\([^)\s]{0,200}\)")


def name_variants(provider: Provider, model: str, entry: PriceEntry) -> list[str]:
    """Names a vendor page may use: the id, configured aliases, a derived display name."""
    names = [model]
    parts = model.split("-")
    if isinstance(entry, TokenTierPrice):
        names.extend(alias.id for alias in entry.aliases)  # every window: a page may name any of them
    if provider == Provider.ANTHROPIC and len(parts) >= 3:
        family = parts[1].capitalize()
        version = ".".join(parts[2:4]) if len(parts) >= 4 and parts[3].isdigit() else parts[2]
        names += [f"Claude {family} {version}", f"{family} {version}"]
    elif provider in (Provider.GOOGLE, Provider.XAI):
        names.append(" ".join(p.capitalize() for p in parts))
    elif isinstance(entry, PeakOffpeakPrice) and entry.display:
        names.append(entry.display)
    return names


# vendors whose page lists a model's cache and long-context rates in the model's own row, beside input and output
# (measured on the live pages 2026-09-30: every Anthropic, Google and OpenAI rate in the confirming row; 2026-10-19: Xiaomi's
# cache-hit rate in the model's row; Alibaba shows cached input as a share in prose, xAI's models page none for
# grok-4.6/4.7) — only there those rates are part of what confirms the price
CACHE_IN_ROW = frozenset({Provider.ANTHROPIC, Provider.GOOGLE, Provider.OPENAI, Provider.XIAOMI})
CACHE_RATES = ("cached_input", "cache_read", "cache_write_5m", "cache_write_1h")
LONG_RATES = ("input", "output", *CACHE_RATES)
Labelled = list[tuple[str, float]]


def wanted_rates(entry: PriceEntry, provider: Provider) -> tuple[Labelled, Labelled]:
    """What confirms the price, and the rates listed apart from the row — each named, for today's price.

    Confirming: the pair that prices rows TODAY (an active ``next`` block, not the expired top-level pair), with its
    cache and long-context rates where the vendor lists them in the model's row (``CACHE_IN_ROW``) — or every DeepSeek
    tariff figure. Apart: those rates at every other vendor.
    """
    if isinstance(entry, PeakOffpeakPrice):
        tariffs = (("peak", entry.peak), ("offpeak", entry.offpeak))
        rates = ("cache_hit", "cache_miss", "output")
        return [(f"{name} {rate}", getattr(tariff, rate)) for name, tariff in tariffs for rate in rates], []
    if not isinstance(entry, TokenTierPrice):
        return [], []
    current = current_price(entry, now())
    pair: Labelled = [("input", current.input), ("output", current.output)]
    extras = list(extra_rates(current).items())
    return (pair + extras, []) if provider in CACHE_IN_ROW else (pair, extras)


def extra_rates(entry: TokenTierPrice) -> dict[str, float]:
    """The rates a price entry bills with beside input and output: its cache rates, its long-context tier's."""
    rates = {name: float(getattr(entry, name)) for name in CACHE_RATES}
    if entry.long is not None:
        rates.update({f"long {name}": float(getattr(entry.long, name)) for name in LONG_RATES})
    return {name: rate for name, rate in rates.items() if rate}


def _claim(rates: Labelled, pool: list[float]) -> list[str]:
    """Each rate takes one equal figure out of ``pool`` (two equal rates need it twice); the ones none answers."""
    missing = []
    for name, rate in rates:
        match = next((index for index, seen in enumerate(pool) if abs(rate - seen) < 1e-9), None)
        if match is None:
            missing.append(f"{name} {rate:g}")
        else:
            pool.pop(match)
    return missing


def _lacks(wanted: Labelled, apart: Labelled, amounts: Sequence[float]) -> tuple[list[str], list[str]]:
    """What the row lacks of the confirming rates, and of the rates listed apart among the figures those left.

    A figure that answered the output never answers a long-context input of the same value too.
    """
    pool = list(amounts)
    return _claim(wanted, pool), _claim(apart, pool)


def _occurrences(
    texts: Sequence[str], provider: Provider, model: str, entry: PriceEntry, rows: Rows
) -> Iterator[Reading]:
    """The model's readings — its rows and its table columns — from the variant that prices it, in the order that decides.

    The variant: the markdown twin when it prices the model — a reading that decides, or a row excluded as another tier
    (the twin says which tier a table belongs to; the page's own text does not, so it never overrides an exclusion) —
    else the page. The order: the id first, then its aliases, then its display name, each in page order (a column at
    its table's place).
    """
    names = name_variants(provider, model, entry)
    for text in reversed(texts):
        excluded: list[int] = []
        found = [
            reading
            for name in names  # the model's OTHER names: its own again starts another of its rows
            for reading in occurrences(
                text, name, rows=rows, own=[n for n in names if n.lower() != name.lower()], excluded=excluded
            )
        ]
        yield from found
        if any(reading.decides for reading in found) or excluded:
            return


def match_model(
    texts: Sequence[str], provider: Provider, model: str, entry: PriceEntry, rows: Rows | None = None
) -> ModelCheck:
    """Confirmed when the model's FIRST reading that decides carries every expected amount (``_occurrences``'s order).

    A reading decides when it is a row with amounts or a table column, empty or not; an empty column is not found.
    One reading decides: a later table of the page (another tier, another region, an example) can neither confirm a stale
    price nor be mistaken for a change. ``rows`` says where a row ends (``page_rows`` over every model of the vendor;
    by default over this model's names). A CHANGED check names what the row lacks; a confirmed one names the rates
    listed apart that its row does not show.
    """
    wanted, apart = wanted_rates(entry, provider)
    expected = [rate for _, rate in wanted]
    # without the vendor's other rows nothing says which names are models: every head then ends a row, as before
    rows = rows or dataclasses.replace(page_rows(provider, name_variants(provider, model, entry)), known=None)
    decides = next(
        (found for found in _occurrences(texts, provider, model, entry, rows) if found.decides), None
    )
    if decides is None or not decides.amounts:
        return ModelCheck(CheckStatus.NOT_FOUND, expected)
    missing, unchecked = _lacks(wanted, apart, decides.amounts)
    status = CheckStatus.CHANGED if missing else CheckStatus.CONFIRMED
    return ModelCheck(status, expected, decides.amounts, missing=missing, unchecked=unchecked)


# ---- new models -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Discovery:
    """How one vendor writes a model id, and the ids its page lists that the price book leaves no_row by design."""

    ids: re.Pattern[str] | None
    no_row: tuple[re.Pattern[str], ...] = ()
    # a vendor that writes display names, not ids (``Claude Mythos 5.2``): the name's groups fill ``name_id``
    names: re.Pattern[str] | None = None
    name_id: str = ""


def _patterns(raw: Any, where: str, flags: int = re.IGNORECASE) -> tuple[re.Pattern[str], ...]:
    """Regexes of the price book, compiled; one that is not a regex is a ``ConfigError`` naming its place."""
    items = [raw] if isinstance(raw, str) else [] if raw is None else raw
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise ConfigError(f"{where}: a regular expression, or a list of them")
    try:
        return tuple(re.compile(item, flags) for item in items)
    except re.error as exc:
        raise ConfigError(f"{where}: not a regular expression ({exc})") from exc


def _one(raw: Any, where: str) -> re.Pattern[str] | None:
    """A key that holds ONE regular expression, read as written: a vendor writes its ids and names in one case."""
    found = _patterns(raw, where, 0)
    if len(found) > 1:
        raise ConfigError(f"{where}: one regular expression (alternatives go inside it)")
    return found[0] if found else None


def _section_of(raw: Mapping[str, Any], provider: Provider) -> Mapping[str, Any]:
    providers = raw.get("providers")
    section = providers.get(provider.value) if isinstance(providers, Mapping) else None
    return section if isinstance(section, Mapping) else {}


def discovery(book: PriceBook, provider: Provider) -> Discovery:
    """The provider's ``model_ids`` / ``model_names`` / ``no_row`` keys of the price book (absent: nothing is looked for).

    ``no_row`` is the shipped list AND the user's: a user file that names one id keeps every shipped exclusion.
    """
    section, where = _section_of(book.raw, provider), f"prices: providers.{provider.value}"
    shipped = _section_of(builtin_prices(), provider).get("no_row")
    name_id = section.get("model_name_id", "")
    if not isinstance(name_id, str):
        raise ConfigError(f"{where}.model_name_id: a template such as claude-{{1}}-{{2}}")
    names = _one(section.get("model_names"), f"{where}.model_names")
    slots = [int(slot) for slot in _SLOT.findall(name_id)]
    if names is not None and any(slot == 0 or slot > names.groups for slot in slots):
        raise ConfigError(f"{where}.model_name_id: names a group model_names does not have")
    return Discovery(
        _one(section.get("model_ids"), f"{where}.model_ids"),
        _no_row(shipped, section.get("no_row"), f"{where}.no_row"),
        names,
        name_id,
    )


def _no_row(shipped: Any, merged: Any, where: str) -> tuple[re.Pattern[str], ...]:
    """The shipped exclusions and the price book's own, each once (a user file without the key leaves the shipped)."""
    both = _patterns(shipped, f"{where} (shipped)") + _patterns(merged, where)
    return tuple({pattern.pattern: pattern for pattern in both}.values())


_SLOT = re.compile(r"\{(\d+)\}")  # a group of ``model_names`` in a ``model_name_id`` template


def _named_id(found: re.Match[str], template: str) -> str | None:
    """The id a display name stands for: each ``{n}`` of the template is group n, lower case, dots as dashes.

    ``None`` when a group the template names took no part in this match (an optional group): no id is made up.
    """
    groups = {int(slot): found.group(int(slot)) for slot in _SLOT.findall(template)}
    if any(text is None for text in groups.values()):
        return None
    return _SLOT.sub(lambda slot: groups[int(slot.group(1))].lower().replace(".", "-"), template)


def unlisted_models(texts: Sequence[str], known: Sequence[str], rule: Discovery) -> list[str]:
    """Model ids the page names that are neither priced (``known``: rows and their aliases) nor listed as no_row."""
    priced = {name.lower() for name in known}
    seen: set[str] = set()
    for text in texts:
        if rule.ids is not None:
            seen.update(match.group(0).lower().rstrip(".-") for match in rule.ids.finditer(text))
        if rule.names is not None and rule.name_id:
            named = (_named_id(match, rule.name_id) for match in rule.names.finditer(text))
            seen.update(name for name in named if name)
    return sorted(
        name for name in seen if name not in priced and not any(skip.search(name) for skip in rule.no_row)
    )


def vendor_rows(book: PriceBook, provider: Provider, rule: Discovery | None = None) -> Rows:
    """The rows of a vendor's page as the check reads them: every priced model's names, and how the vendor writes one."""
    rule = rule or discovery(book, provider)
    models = book.models.get(provider, {})
    names = [name for m, e in models.items() for name in name_variants(provider, m, e)]
    return page_rows(provider, names, [pattern for pattern in (rule.ids, rule.names) if pattern is not None])


def check_provider_names(book: PriceBook, providers: Sequence[str] | None) -> None:
    """An unknown name in ``--providers`` is a ``UsageError``: raised before a check holds anything."""
    names = [provider.value for provider in book.sources]
    unknown = [name for name in providers or () if name not in names]
    if unknown:
        raise UsageError(
            f"unknown provider(s) for --providers: {', '.join(unknown)} (known: {', '.join(names)})"
        )


def check_prices(book: PriceBook, providers: Sequence[str] | None = None, timeout: int = 15) -> CheckResult:
    """Fetch every source page and match every model; never writes. An unknown name in ``providers`` is a usage error."""
    check_provider_names(book, providers)
    asked = [provider for provider in book.sources if not providers or provider.value in providers]
    result = CheckResult(checked_at=iso(now()), limited=len(asked) < len(book.sources))
    for provider in asked:  # the pricebook is the registry
        url = book.sources.get(provider, "")
        entry = ProviderCheck(url=url, status="skipped")
        result.providers[provider.value] = entry
        if not url:
            continue
        try:
            texts = fetch_variants(url, timeout)
        except VENDOR_UNREAD as exc:
            entry.status = f"fetch-failed {read_failure(exc, url)}"
            entry.unread = True
            continue
        entry.status = f"fetched ({len(texts)} page variant{'s' if len(texts) != 1 else ''})"
        models = book.models.get(provider, {})
        rule = discovery(book, provider)
        rows = vendor_rows(book, provider, rule)
        for model, price_entry in models.items():
            entry.models[model] = match_model(texts, provider, model, price_entry, rows)
        priced_ids = [name for m, e in models.items() for name in _ids(m, e)]
        found = unlisted_models(texts, priced_ids, rule)
        # priced as the pricer decides it: a same-model id (a `-preview` twin, a dated snapshot) has its row
        entry.unlisted = [name for name in found if book.entry(provider, name, None) is None]
    return result


def read_failure(exc: BaseException, url: str) -> str:
    """What a vendor's status line says of a read that failed: the twin named, an HTTP status with where to look."""
    if isinstance(exc, TwinUnread):
        return f"twin {read_failure(exc.cause, exc.url)}"
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} (verify manually at {url})"
    return exc.__class__.__name__


def _ids(model: str, entry: PriceEntry) -> list[str]:
    """A row's id and the alias ids it prices."""
    aliases = [alias.id for alias in entry.aliases] if isinstance(entry, TokenTierPrice) else []
    return [model, *aliases]


# ---- applying -----------------------------------------------------------------------------------------------------


def apply_check(result: CheckResult, book: PriceBook, user_file: Path) -> list[tuple[str, str, float, float]]:
    """Write unambiguous input/output pairs of TOKEN-TIER models to the user file; everything else stays for a hand edit."""
    user = _read_user_prices(user_file)
    applied = []
    for provider_name, prov in result.providers.items():
        for model, check in prov.models.items():
            if check.status is not CheckStatus.CHANGED or len(check.seen) < 2 or len(check.expected) != 2:
                continue  # more than a pair expected: its row holds cache rates too, a hand edit
            try:
                provider = Provider.of(provider_name)
            except ValueError:
                continue
            entry = book.models.get(provider, {}).get(model)
            if not isinstance(entry, TokenTierPrice) or current_price(entry, now()) is not entry:
                continue  # an active next block: the pair belongs in `next`, a hand edit
            new_in, new_out = check.seen[0], check.seen[-1] if len(check.seen) == 2 else None
            if (
                new_out is None or new_in >= new_out
            ):  # a page listing output first would swap the pair: hand edit
                continue
            _model_slot(user, provider_name, model).update({"input": new_in, "output": new_out})
            check.status = CheckStatus.APPLIED
            check.expected = [new_in, new_out]
            check.missing = []
            applied.append((provider_name, model, new_in, new_out))
    if applied:
        if (
            not result.limited
        ):  # only a check of every vendor moves the registry date ``check_due`` falls back to
            user["checked_at"] = result.checked_at[:10]
        _write_json(user_file, user)
    return applied


def _read_user_prices(user_file: Path) -> dict[str, Any]:
    """The user prices file as an object (absent = empty); one that cannot be read or is not an object is a ``ToolError``."""
    if not user_file.exists():
        return {}
    try:
        with user_file.open() as handle:
            user = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ToolError(f"cannot read {user_file}: {exc}") from exc
    if not isinstance(user, dict):
        raise ToolError(f"cannot read {user_file}: not a JSON object")
    return user


def _model_slot(user: dict[str, Any], provider: str, model: str) -> dict[str, Any]:
    """The user file's object for a model, created on the way: a null on the path (no override) becomes an object.

    Any other non-object on the path is the user's own malformed structure — a ``ToolError``, never overwritten.
    """
    node, walked = user, "prices file"
    for key in ("providers", provider, "models", model):
        child = node.get(key)
        walked = f"{walked}.{key}"
        if child is None:
            child = node[key] = {}
        elif not isinstance(child, dict):
            raise ToolError(
                f"cannot update {walked}: {type(child).__name__} where an object belongs — fix it by hand"
            )
        node = child
    return node


def _write_json(path: Path, payload: Any) -> None:
    """Write ``payload`` to ``path`` atomically; a directory that cannot be made or written is a ``ToolError``."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(path)
    except OSError as exc:
        raise ToolError(f"cannot write {path}: {exc}") from exc


def exit_code(unresolved: int) -> int:
    """0 when no suspected change is left after the apply, 4 otherwise."""
    return 4 if unresolved > 0 else 0


# ---- state + auto-check -------------------------------------------------------------------------------------------


def state_file(paths: Paths) -> Path:
    """Where the last check result lives."""
    return paths.state_dir / "prices-check.json"


def save_result(paths: Paths, result: CheckResult) -> CheckResult:
    """Persist a result (atomic) laid over the saved one (``to_save``) and return what was saved.

    An unwritable state directory is a ``ToolError``.
    """
    saved = to_save(result, load_result(paths))
    _write_json(state_file(paths), saved.to_json())
    return saved


def to_save(result: CheckResult, previous: CheckResult | None) -> CheckResult:
    """The result to save: what this check read, laid over what the saved file knew.

    A vendor this check could not read keeps its saved entry — a failed read is a missing observation, never a state:
    its models, unlisted ids and drift stay, the status names the failed read and when the kept entry was read. A check
    limited by ``--providers`` keeps every vendor it did not ask for as saved, and the saved ``checked_at`` that
    ``check_due`` counts from: only a check of every vendor moves it.
    """
    base = previous or CheckResult("")
    saved = CheckResult(result.checked_at, dict(result.providers))
    if result.limited:
        read = {
            name: dataclasses.replace(entry, read_at="" if entry.unread else result.checked_at)
            for name, entry in result.providers.items()
        }
        saved = CheckResult(base.checked_at, {**base.providers, **read})
    for name, entry in result.providers.items():
        before = base.providers.get(name)
        if entry.unread and before is not None and (before.models or before.unlisted):
            saved.providers[name] = _kept(entry, before, before.read_at or base.checked_at)
    return saved


def _kept(unread: ProviderCheck, before: ProviderCheck, read_at: str) -> ProviderCheck:
    """An unread vendor's entry: this check's failed read in the status, the saved entry's models and unlisted ids."""
    kept = ProviderCheck(unread.url, unread.status, before.models, before.unlisted, read_at, unread=True)
    return dataclasses.replace(kept, status=f"{unread.status}; kept the entry {kept.read_when()}")


def load_result(paths: Paths) -> CheckResult | None:
    """The last saved result, if any."""
    path = state_file(paths)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return CheckResult.from_json(data) if isinstance(data, Mapping) else None


def check_due(book: PriceBook, last: CheckResult | None) -> bool:
    """Older than ``auto_check_days`` (from the last saved check, else the registry's ``checked_at``); 0 = never."""
    if book.auto_check_days <= 0:
        return False
    stamp = parse_ts(last.checked_at) if last and last.checked_at else None
    reference = stamp or parse_ts(book.checked_at.isoformat())
    return reference is None or now() - reference >= timedelta(days=book.auto_check_days)


def _reclaim(lock: Path) -> bool:
    """``True`` when the stale lock was removed here or is gone; ``False`` when it is fresh or reclaimed elsewhere.

    One process at a time looks at the lock and removes it: the reclaim marker is created with ``O_EXCL``, and the
    stat and the unlink of a stale lock happen under it, so a lock another process has just recreated is never the
    one deleted (the marker holder sees it fresh). A marker a crash left behind expires like the lock itself.
    """
    marker = lock.with_name(f"{lock.name}.reclaim")
    try:
        handle = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except (
        FileExistsError
    ):  # another process is reclaiming right now: yield to it, unless its marker is a crash's
        return _clear_expired(marker)
    except OSError:
        return False
    os.close(handle)
    try:
        return _remove_if_expired(lock)
    finally:
        with contextlib.suppress(OSError):
            marker.unlink()


def _remove_if_expired(path: Path) -> bool:
    """Remove ``path`` when it is older than ``LOCK_SECONDS`` or already gone; ``False`` when it is fresh."""
    try:
        if time.time() - path.stat().st_mtime < LOCK_SECONDS:
            return False
        path.unlink()
    except FileNotFoundError:  # gone meanwhile: nothing holds it
        return True
    except OSError:
        return False
    return True


def _clear_expired(marker: Path) -> bool:
    """A reclaim marker older than ``LOCK_SECONDS`` was left by a crash: remove it and try again; a live one wins."""
    return _remove_if_expired(marker)


def take_lock(lock: Path) -> bool:
    """Create ``lock`` atomically (``O_EXCL``); a stale one, older than ``LOCK_SECONDS``, is reclaimed and replaced."""
    for _ in range(
        3
    ):  # create, reclaim, create — one more when a crashed reclaim marker had to be cleared first
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if not _reclaim(lock):
                return False
            continue
        with os.fdopen(handle, "w") as out:
            out.write(str(os.getpid()))
        return True
    return False


def auto_check(
    paths: Paths, book: PriceBook, spawn: Any = subprocess.Popen
) -> tuple[list[tuple[str, str]], bool]:
    """Report the last saved drift at once; start a detached check when due (lock keeps it to one). Never waits."""
    last = load_result(paths)
    drift = last.drift() if last else []
    if paths.offline or not check_due(book, last):
        return drift, False
    lock = paths.state_dir / "prices-check.lock"
    try:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        if not take_lock(lock):
            return drift, False
        command = [*self_command(), "prices", "check", "--quiet"]
        with (paths.state_dir / "prices-check.log").open("a") as log:
            spawn(
                command,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        return drift, True
    except OSError:
        return drift, False


def merged_registry_json(book: PriceBook) -> dict[str, Any]:
    """The registry as merged with the user's overrides, JSON-shaped (``prices show --format json``)."""
    return deep_merge(book.raw, {})
