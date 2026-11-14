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
import http.client
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
from .errors import ConfigError, ToolError
from .models import CheckStatus, PeakOffpeakPrice, PriceEntry, Provider, TokenTierPrice
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
                str(prov.get("url", "")), str(prov.get("status", "")), checks, _labels(prov.get("unlisted"))
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
                return str(response.read(2_000_000).decode("utf-8", "replace"))
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
    """Visible text of an HTML page, whitespace collapsed; a heading is kept as a `## ` marker."""
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    # a heading stays a section marker (`## Batch ¶`), as in a twin: where it starts and where its own text ends
    raw = re.sub(r"(?i)<h[1-4](?:\s[^>]*)?>", " ## ", raw)
    raw = re.sub(r"(?i)</h[1-4]>", f" {HEADING_END} ", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text)


MARKDOWN_TWIN = ".md"
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
    texts = [page_text(fetch(url, timeout))]
    # no twin: the status line says "1 page variant"
    with contextlib.suppress(urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        texts.append(twin_text(fetch(url + MARKDOWN_TWIN, timeout)))
    return texts


def twin_text(raw: str) -> str:
    """A markdown twin's text, whitespace collapsed — its ``<= 200k … >`` is prose, never a tag to strip.

    A vendor that answers ``<url>.md`` with an HTML page (no twin, a catch-all route) is read as the page it is.
    """
    if raw.lstrip("\ufeff \t\r\n")[:1] == "<":
        return page_text(raw)
    marked = re.sub(r"(?m)^(#{1,4} [^\n]*)$", rf"\1 {HEADING_END}", _CALLOUT.sub(" ", raw))
    return re.sub(r"\s+", " ", html.unescape(marked))


# a notice box of a markdown page: it names the model that replaces a deprecated one before that model's own table,
# and a row ends at the next model name — the notice is prose about the row, never part of it
_CALLOUT = re.compile(r"\{%\s*callout\b.*?\{%\s*/callout\s*%\}", re.S)


# ---- matching -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rows:
    """Where a model's row ends on a vendor's page: at the next model name the page shows (``SCAN_SPAN`` at most).

    ``heads`` is ``None`` for a page laid out in columns (every model's figures after all their names: DeepSeek), where
    only the span bounds what belongs to a name, and a name is found inside a compound label (``DeepSeek-V4-Pro-0813``).
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


# vendors whose page is laid out in columns, not rows (measured on the live pages 2026-09-30)
COLUMN_PAGES = frozenset({Provider.DEEPSEEK})
WHOLE_SPAN = Rows(None)


def page_rows(provider: Provider, names: Sequence[str], models: Sequence[re.Pattern[str]] = ()) -> Rows:
    """How the page's rows end: at the next model name, in either form the vendor writes one.

    A name starts with the first word of a display name (``Claude ``, ``Gemini ``, ``Grok ``, case as shown) or with
    the letters of an id (``claude``, ``gpt``, ``qwen``, any case) — a page may list its rows under either.
    """
    if provider in COLUMN_PAGES:
        return WHOLE_SPAN
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


def occurrences(
    text: str,
    needle: str,
    span: int = SCAN_SPAN,
    rows: Rows = WHOLE_SPAN,
    own: Sequence[str] = (),
    excluded: list[int] | None = None,
) -> list[list[float]]:
    """Dollar amounts in the row after EVERY occurrence of ``needle`` as a whole name (menus, prose, tables).

    On a page in rows a name inside a longer one is none (``Claude Fable 5`` in ``Claude Fable 5.1``, ``gpt-5.6`` in
    ``gpt-5.6-sol``) and a row ends where the page's next model name starts, ``span`` characters after the name at most.
    Another name of the model itself (``own``: its id after its display name) does not end its row while no amount has
    come yet, nor does one of its endpoints (``gemini-3.1-pro-preview-customtools``); another version
    (``claude-opus-5-5``) does, and so does the needle itself again (the model's next row, or an example).
    """
    name = re.compile((_WHOLE if rows.heads else "{}").format(re.escape(needle)), re.I)
    own_names = [rf"{re.escape(n)}{SAME_MODEL_TAIL}(?![A-Za-z0-9.-])" for n in own]
    if (
        rows.heads
    ):  # the needle with a same-model tail (its dated snapshot, its -preview twin): the same row's model
        own_names.append(rf"{re.escape(needle)}(?=[-\[]){SAME_MODEL_TAIL}(?![A-Za-z0-9.-])")
    mine = re.compile("|".join(own_names), re.I) if own_names else None
    out = []
    tiers = _tier_starts(text)
    for found in name.finditer(text):
        standard = _standard_row(text, found.start(), found.end(), tiers)
        if standard is None:
            if (
                excluded is not None
            ):  # a row of another tier: the text prices the model, in a table that never confirms
                excluded.append(found.start())
            continue
        row = standard[: found.start() + span - found.end()]
        out.append(
            [float(m.replace(",", "")) for m in AMOUNT.findall(row[: _row_end(row, rows, mine, name)])]
        )
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


def numbers_near(text: str, needle: str, span: int = SCAN_SPAN) -> list[float] | None:
    """The richest occurrence, or ``None`` when the needle is absent."""
    found = occurrences(text, needle, span)
    return max(found, key=len) if found else None


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
) -> Iterator[list[float]]:
    """The model's rows, from the variant that prices it, in the order that decides.

    The variant: the markdown twin when it prices the model — a row with amounts, or a row excluded as another tier
    (the twin says which tier a table belongs to; the page's own text does not, so it never overrides an exclusion) —
    else the page. The order: the id first, then its aliases, then its display name, each in page order.
    """
    names = name_variants(provider, model, entry)
    for text in reversed(texts):
        excluded: list[int] = []
        found = [
            amounts
            for name in names  # the model's OTHER names: its own again starts another of its rows
            for amounts in occurrences(
                text, name, rows=rows, own=[n for n in names if n.lower() != name.lower()], excluded=excluded
            )
        ]
        yield from found
        if any(found) or excluded:
            return


def match_model(
    texts: Sequence[str], provider: Provider, model: str, entry: PriceEntry, rows: Rows | None = None
) -> ModelCheck:
    """Confirmed when the model's FIRST row with amounts carries every expected amount (``_occurrences``'s order).

    One row decides: a later table of the page (another tier, another region, an example) can neither confirm a stale
    price nor be mistaken for a change. ``rows`` says where a row ends (``page_rows`` over every model of the vendor;
    by default over this model's names). A CHANGED check names what the row lacks; a confirmed one names the rates
    listed apart that its row does not show.
    """
    wanted, apart = wanted_rates(entry, provider)
    expected = [rate for _, rate in wanted]
    # without the vendor's other rows nothing says which names are models: every head then ends a row, as before
    rows = rows or dataclasses.replace(page_rows(provider, name_variants(provider, model, entry)), known=None)
    decides = next(
        (amounts for amounts in _occurrences(texts, provider, model, entry, rows) if amounts), None
    )
    if decides is None:
        return ModelCheck(CheckStatus.NOT_FOUND, expected)
    missing, unchecked = _lacks(wanted, apart, decides)
    status = CheckStatus.CHANGED if missing else CheckStatus.CONFIRMED
    return ModelCheck(status, expected, decides, missing=missing, unchecked=unchecked)


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


def check_prices(book: PriceBook, providers: Sequence[str] | None = None, timeout: int = 15) -> CheckResult:
    """Fetch every source page and match every model; never writes."""
    result = CheckResult(checked_at=iso(now()))
    for provider in book.sources:  # the pricebook is the registry
        if providers and provider.value not in providers:
            continue
        url = book.sources.get(provider, "")
        entry = ProviderCheck(url=url, status="skipped")
        result.providers[provider.value] = entry
        if not url:
            continue
        try:
            texts = fetch_variants(url, timeout)
        except urllib.error.HTTPError as exc:
            entry.status = f"fetch-failed HTTP {exc.code} (verify manually at {url})"
            continue
        except (urllib.error.URLError, OSError, ValueError) as exc:
            entry.status = f"fetch-failed {exc.__class__.__name__}"
            continue
        entry.status = f"fetched ({len(texts)} page variant{'s' if len(texts) != 1 else ''})"
        models = book.models.get(provider, {})
        rule = discovery(book, provider)
        rows = vendor_rows(book, provider, rule)
        for model, price_entry in models.items():
            entry.models[model] = match_model(texts, provider, model, price_entry, rows)
        known = [name for m, e in models.items() for name in _ids(m, e)]
        found = unlisted_models(texts, known, rule)
        # priced as the pricer decides it: a same-model id (a `-preview` twin, a dated snapshot) has its row
        entry.unlisted = [name for name in found if book.entry(provider, name, None) is None]
    return result


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


def save_result(paths: Paths, result: CheckResult) -> None:
    """Persist a result (atomic); an unwritable state directory is a ``ToolError``, never a traceback."""
    _write_json(state_file(paths), result.to_json())


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
