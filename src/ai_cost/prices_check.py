"""Price drift: fetch each vendor page, find every model by its id or display name, compare the amounts that follow.

Never rewrites a price on its own: ``check`` reports, ``update`` writes only token-tier models with an unambiguous
input/output pair to the USER file (ADR-0002: the shape decides what may be written). A background check is started
detached when the last one is older than ``auto_check_days``; reports never wait for the network.
"""

from __future__ import annotations

import contextlib
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

from .config import Paths, PriceBook, deep_merge
from .errors import ToolError
from .models import CheckStatus, PeakOffpeakPrice, PriceEntry, Provider, TokenTierPrice
from .pricing import current_price
from .process import self_command
from .timeutil import iso, now, parse_ts
from .values import money

UA_PLAIN = "Mozilla/5.0"
UA_BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
AMOUNT = re.compile(r"\$\s?(\d+(?:[.,]\d+)?)")
SCAN_SPAN = 600
LOCK_SECONDS = 600


@dataclass
class ModelCheck:
    """Outcome for one model."""

    status: CheckStatus
    expected: list[float]
    seen: list[float] = field(default_factory=list)


@dataclass
class ProviderCheck:
    """Outcome for one provider page."""

    url: str
    status: str
    models: dict[str, ModelCheck] = field(default_factory=dict)


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
                        m: {"status": c.status.value, "expected": c.expected, "seen": c.seen[:12]}
                        for m, c in prov.models.items()
                    },
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
                    status, _amounts(check.get("expected")), _amounts(check.get("seen"))
                )
            result.providers[name] = ProviderCheck(
                str(prov.get("url", "")), str(prov.get("status", "")), checks
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
_RETRY_WITH_NEXT_AGENT = (403, 310)  # forbidden for this UA, or a redirect loop / too many hops


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
    agents: Sequence[str] = (UA_PLAIN, UA_BROWSER),
    opener: Callable[..., Any] = _OPENER.open,
) -> str:
    """GET following redirects by hand (3.9's urllib ignores 308); the plain UA first, the browser UA on 403/loop.

    Only http(s) targets are followed: a page must not redirect the checker to a local file.
    """
    _http_only(url)
    if not agents:
        raise ValueError("fetch: at least one user agent is required")
    *retried, final = agents
    for agent in retried:
        try:
            return _get(url, agent, hops, timeout, opener)
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRY_WITH_NEXT_AGENT:
                raise
    return _get(
        url, final, hops, timeout, opener
    )  # the last agent's error is the caller's, whatever its code


def page_text(raw: str) -> str:
    """Visible text of an HTML page, whitespace collapsed."""
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text)


def fetch_variants(url: str, timeout: int = 15) -> list[str]:
    """The page as the plain and the browser UA see it (some sites serve different tables to each)."""
    texts: list[str] = []
    errors: list[BaseException] = []
    for agent in (UA_PLAIN, UA_BROWSER):
        try:
            text = page_text(fetch(url, timeout, agents=(agent,)))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(exc)
            continue
        if text not in texts:
            texts.append(text)
    if not texts:
        raise errors[0]
    return texts


# ---- matching -----------------------------------------------------------------------------------------------------


def occurrences(text: str, needle: str, span: int = SCAN_SPAN) -> list[list[float]]:
    """Dollar amounts after EVERY occurrence of ``needle`` (menus, prose, standard and batch tables)."""
    out: list[list[float]] = []
    low, key, pos = text.lower(), needle.lower(), 0
    while True:
        index = low.find(key, pos)
        if index < 0:
            return out
        out.append([float(m.replace(",", "")) for m in AMOUNT.findall(text[index : index + span])])
        pos = index + 1


def numbers_near(text: str, needle: str, span: int = SCAN_SPAN) -> list[float] | None:
    """The richest occurrence, or ``None`` when the needle is absent."""
    found = occurrences(text, needle, span)
    return max(found, key=len) if found else None


def name_variants(provider: Provider, model: str, entry: PriceEntry) -> list[str]:
    """Names a vendor page may use: the id, configured aliases, a derived display name."""
    names = [model]
    parts = model.split("-")
    if isinstance(entry, TokenTierPrice):
        names.extend(entry.aliases)
    if provider == Provider.ANTHROPIC and len(parts) >= 3:
        family = parts[1].capitalize()
        version = ".".join(parts[2:4]) if len(parts) >= 4 and parts[3].isdigit() else parts[2]
        names += [f"Claude {family} {version}", f"{family} {version}"]
    elif provider in (Provider.GOOGLE, Provider.XAI):
        names.append(" ".join(p.capitalize() for p in parts))
    elif isinstance(entry, PeakOffpeakPrice) and entry.display:
        names.append(entry.display)
    return names


def expected_amounts(entry: PriceEntry) -> list[float]:
    """The amounts a page must show for the price to count as confirmed.

    The pair that prices rows TODAY (an active ``next`` block, not the expired top-level pair), or every DeepSeek
    tariff figure.
    """
    if isinstance(entry, TokenTierPrice):
        current = current_price(entry, now())
        return [current.input, current.output]
    if isinstance(entry, PeakOffpeakPrice):
        return [
            rate
            for tariff in (entry.peak, entry.offpeak)
            for rate in (tariff.cache_hit, tariff.cache_miss, tariff.output)
        ]
    return []


def _occurrences(
    texts: Sequence[str], provider: Provider, model: str, entry: PriceEntry
) -> Iterator[list[float]]:
    for text in texts:
        for name in name_variants(provider, model, entry):
            yield from occurrences(text, name)


def match_model(texts: Sequence[str], provider: Provider, model: str, entry: PriceEntry) -> ModelCheck:
    """Confirmed when any occurrence of any name carries every expected amount."""
    expected = expected_amounts(entry)
    best: list[float] | None = None
    for amounts in _occurrences(texts, provider, model, entry):
        if not amounts:
            continue
        if all(any(abs(seen - want) < 1e-9 for seen in amounts) for want in expected):
            return ModelCheck(CheckStatus.CONFIRMED, expected, amounts)
        if best is None or len(amounts) > len(best):
            best = amounts
    return ModelCheck(CheckStatus.NOT_FOUND if best is None else CheckStatus.CHANGED, expected, best or [])


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
        for model, price_entry in book.models.get(provider, {}).items():
            entry.models[model] = match_model(texts, provider, model, price_entry)
        if provider == Provider.GITHUB:
            entry.models["copilot-overage"] = _copilot_check(texts, book)
    return result


def _copilot_check(texts: Sequence[str], book: PriceBook) -> ModelCheck:
    want = book.github.overage_usd_per_unit
    source = book.github.copilot_source  # validated as text by the registry parser, like every other source
    pages = list(texts)
    if source:
        try:
            pages = fetch_variants(source)
        except (urllib.error.URLError, OSError, ValueError):
            pages = []
    seen: list[float] = []
    for text in pages:
        seen = (
            numbers_near(text, "additional premium request", 300)
            or numbers_near(text, "additional", 300)
            or []
        )
        if want in seen:
            return ModelCheck(CheckStatus.CONFIRMED, [want], seen)
    return ModelCheck(CheckStatus.NOT_FOUND if not seen else CheckStatus.CHANGED, [want], seen)


# ---- applying -----------------------------------------------------------------------------------------------------


def apply_check(result: CheckResult, book: PriceBook, user_file: Path) -> list[tuple[str, str, float, float]]:
    """Write unambiguous input/output pairs of TOKEN-TIER models to the user file; everything else stays for a hand edit."""
    user = _read_user_prices(user_file)
    applied = []
    for provider_name, prov in result.providers.items():
        for model, check in prov.models.items():
            if check.status is not CheckStatus.CHANGED or model == "copilot-overage" or len(check.seen) < 2:
                continue
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
