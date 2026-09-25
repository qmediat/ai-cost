"""What a user still has to set up: the next steps ``install --init-config`` prints and the doctor's setup lines.

The account facts a report depends on — which plan pays for which provider, how a CLI signs in — are the user's to
declare in the config; nothing here reads the network or guesses them. Each check names the key that is missing and
the value that fits, so a person and an agent can act on the line alone.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .collectors.claude import SOURCE_NAME as CLAUDE_SOURCE
from .collectors.claude import find_session_files
from .collectors.codex import SOURCE_NAME as CODEX_SOURCE
from .collectors.codex import rollout_files
from .collectors.gemini_cli import SOURCE_NAME as GEMINI_SOURCE
from .collectors.gemini_cli import session_files
from .collectors.grok_build import SOURCE_NAME as GROK_SOURCE
from .collectors.grok_build import usage_files
from .config import Budgets, Config, Paths, PriceBook
from .models import Billing, Skipped, UsageRow

SETUP_GUIDE = "https://github.com/qmediat/ai-cost/blob/main/docs/SETUP.md"
_USAGE_LOG_GUIDE = f"{SETUP_GUIDE}#4-count-your-own-apps-and-mcp-servers"
_RULES = ("api", "subscription")  # the billing rules that decide a row; "" (mixed) leaves it to the files


class Mark(Enum):
    """A doctor line's verdict: fine, a problem to act on (counted in the exit code), or information."""

    OK = "ok"
    PROBLEM = "!!"
    INFO = "--"


@dataclass(frozen=True)
class Check:
    """One doctor line."""

    mark: Mark
    text: str


class Found(Enum):
    """What a CLI's session directory held when the doctor looked."""

    FILES = "files"
    EMPTY = "empty"  # the directory exists and holds no session file
    ABSENT = "absent"  # no such directory: the CLI is not used here, or keeps its files elsewhere
    UNREADABLE = "unreadable"  # it exists but cannot be listed: its files would be missed in silence


@dataclass(frozen=True)
class SourceFiles:
    """One built-in CLI's session files on disk."""

    label: str
    provider: str  # the pricebook provider its rows carry
    count: int
    where: Path
    says_billing: bool  # whether the files tell a plan session from a pay-per-token one (Codex rollouts can)
    home_env: str = ""  # the variable that points the tool at the CLI's home
    found: Found = Found.FILES
    source: str = ""  # the name its collector puts on every row
    unlisted: int = 0  # directories under ``where`` that could not be listed: their files are missed


@dataclass(frozen=True)
class ProviderUse:
    """How one provider's rows of the doctor's window were billed."""

    provider: str
    on_plan: int = 0
    per_token: int = 0
    unknown: int = 0

    @property
    def rows(self) -> int:
        """Every row of the provider in the window."""
        return self.on_plan + self.per_token + self.unknown


@dataclass(frozen=True)
class _Layout:
    """A built-in CLI: where its session files live and how its collector lists them."""

    label: str
    provider: str
    root: Callable[[Paths], Path]
    files: Callable[[Paths, list[Skipped]], Sequence[object]]  # a subdirectory it cannot list: a skip
    says_billing: bool
    home_env: str
    source: str


_LAYOUTS = (
    _Layout(
        "Claude transcripts",
        "anthropic",
        lambda p: p.claude_home / "projects",
        lambda p, skipped: find_session_files(p.claude_home, None, None, all_projects=True, skipped=skipped),
        False,
        "CLAUDE_CONFIG_DIR",
        CLAUDE_SOURCE,
    ),
    _Layout(
        "Codex rollouts",
        "openai",
        lambda p: p.codex_home / "sessions",
        lambda p, _: rollout_files(p.codex_home),
        True,
        "CODEX_HOME",
        CODEX_SOURCE,
    ),
    _Layout(
        "Gemini CLI sessions",
        "google",
        lambda p: p.gemini_home / "tmp",
        lambda p, _: session_files(p.gemini_home),
        False,
        "GEMINI_CLI_HOME",
        GEMINI_SOURCE,
    ),
    _Layout(
        "Grok Build sessions",
        "xai",
        lambda p: p.grok_home / "sessions",
        lambda p, _: usage_files(p.grok_home),
        False,
        "GROK_HOME",
        GROK_SOURCE,
    ),
)

_HINTS = {
    "anthropic": '"subscription" on a Claude plan (and the plan under subscriptions), "api" on an API key',
    "openai": '"api" when Codex runs on an API key, "subscription" when it runs on a ChatGPT plan (and the plan '
    "under subscriptions) — a turn whose rollout names its plan needs no rule, but some sessions name none",
    "google": '"api" on an API key, "subscription" when you sign in with a Google account (and that plan '
    "under subscriptions; a plan the registry lacks, a free tier too, goes into prices.json plans)",
}
_DEFAULT_HINT = '"subscription" on a plan (and the plan under subscriptions), "api" when you pay per token'

_DOCTOR_TODO = "every !! line names what is still missing"
START_HERE = (  # the two commands a new user runs first, as ``--help`` and the next steps print them
    "  ai-cost install --init-config   writes your config and prints what to put in it",
    f"  ai-cost doctor                  {_DOCTOR_TODO}",
)
NEXT_STEPS = (
    "next — the file ships no plans on purpose (an example plan would be counted as money you paid):",
    '  1. "subscriptions": one entry per plan you pay, e.g. {"plan": "claude-max-20x", "seats": 1, '
    '"covers": ["anthropic"], "attribution": "time"} — the plan names: ai-cost prices show',
    '  2. "providers": <name>.billing wherever the session files cannot say how you paid — Claude Code always '
    '("anthropic": "subscription" on a Claude plan, "api" on an API key), Gemini CLI and Grok Build when they '
    "run on an account plan instead of a key",
    f"  3. ai-cost doctor — {_DOCTOR_TODO}",
    f"guide: {SETUP_GUIDE}",
)


def billing_hint(provider: str) -> str:
    """The values of ``providers.<provider>.billing`` and when each one fits."""
    return _HINTS.get(provider, _DEFAULT_HINT)


def init_config_lines(target: Path, written: bool) -> list[str]:
    """What ``install --init-config`` prints: the file, then what to put in it (or where to look when it exists)."""
    if not written:
        return [
            f"exists: {target} (use --force to overwrite) — ai-cost doctor says what is still missing; "
            f"guide: {SETUP_GUIDE}"
        ]
    return [f"written {target}", *NEXT_STEPS]


def source_files(paths: Paths) -> list[SourceFiles]:
    """The session files of every built-in CLI, listed by its own collector."""
    return [_source_files(layout, paths) for layout in _LAYOUTS]


def _source_files(layout: _Layout, paths: Paths) -> SourceFiles:
    where = layout.root(paths)
    skipped: list[Skipped] = []
    try:
        count = len(layout.files(paths, skipped))
        found = Found.FILES if count else (Found.EMPTY if os.path.isdir(where) else Found.ABSENT)
    except OSError:  # the collectors' listing: the directory exists and cannot be listed
        count, found = 0, Found.UNREADABLE
    found = Found.UNREADABLE if skipped and not count else found
    return SourceFiles(
        layout.label,
        layout.provider,
        count,
        where,
        layout.says_billing,
        layout.home_env,
        found,
        layout.source,
        len(skipped),
    )


def provider_use(rows: Iterable[UsageRow]) -> list[ProviderUse]:
    """Rows per provider by billing: on a plan, per token (settled on a ledger too), or unknown."""
    counts: dict[str, Counter[Billing]] = {}
    for row in rows:
        counts.setdefault(row.provider.value, Counter())[row.billing] += 1
    return [
        ProviderUse(
            provider,
            on_plan=count[Billing.SUBSCRIPTION],
            per_token=count[Billing.API] + count[Billing.API_SETTLED],
            unknown=count[Billing.UNKNOWN],
        )
        for provider, count in sorted(counts.items())
    ]


def source_checks(found: Sequence[SourceFiles], logs: Sequence[Path], window_rows: int | None) -> list[Check]:
    """One line per CLI and usage log; a CLI not used is information, finding no usage anywhere is a problem.

    ``window_rows`` is None when the doctor's collection failed, and a source or a log that could not be read may
    hold usage: in both cases "no usage" is not claimed.
    """
    checks = [_source_check(f) for f in found]
    logged = [_usage_log_check(path, first=index == 0) for index, path in enumerate(dict.fromkeys(logs))]
    checks += [check for check, _ in logged]
    has_log = any(present for _, present in logged)
    unread = any(f.found is Found.UNREADABLE or f.unlisted for f in found)
    unread = unread or any(c.mark is Mark.PROBLEM for c, _ in logged)
    if window_rows == 0 and not has_log and not unread and not any(f.count for f in found):
        checks.append(
            Check(
                Mark.PROBLEM,
                "no usage found: no session files of Claude Code, Codex CLI, Gemini CLI or Grok Build, no usage log, "
                "no plugin rows in the last 24 h — a CLI that keeps its files elsewhere: CLAUDE_CONFIG_DIR, "
                "CODEX_HOME, GEMINI_CLI_HOME, GROK_HOME",
            )
        )
    return checks


def _source_check(found: SourceFiles) -> Check:
    if found.unlisted:
        return Check(
            Mark.PROBLEM,
            f"{found.label}: {found.count} under {found.where}, but {found.unlisted} directory(ies) under it cannot "
            "be read — their files would be missed; fix the permissions",
        )
    elsewhere = f"used here? {found.home_env} names its home when it keeps the files elsewhere"
    by_state = {
        Found.FILES: (Mark.OK, f"{found.label}: {found.count} under {found.where}"),
        Found.UNREADABLE: (
            Mark.PROBLEM,
            f"{found.label}: {found.where} cannot be read — its files would be missed; fix the permissions",
        ),
        Found.EMPTY: (Mark.INFO, f"{found.label}: 0 under {found.where} — {elsewhere}"),
        Found.ABSENT: (Mark.INFO, f"{found.label}: none ({found.where} does not exist) — {elsewhere}"),
    }
    mark, text = by_state[found.found]
    return Check(mark, text)


def _usage_log_check(usage_log: Path, first: bool) -> tuple[Check, bool]:
    """A usage log: present, absent (information), or unreadable (a problem)."""
    if not usage_log.exists():
        text = (
            f"usage log: none yet (programs can write one: {_USAGE_LOG_GUIDE})"
            if first
            else f"usage log {usage_log}: absent (listed in usage_logs)"
        )
        return Check(Mark.INFO, text), False
    if not usage_log.is_file() or not os.access(usage_log, os.R_OK):
        return (
            Check(Mark.PROBLEM, f"usage log {usage_log}: not a readable file — its lines would be skipped"),
            False,
        )
    try:
        size = usage_log.stat().st_size
    except OSError as exc:
        return Check(Mark.PROBLEM, f"usage log {usage_log}: cannot read ({exc})"), False
    return Check(Mark.OK if size else Mark.INFO, f"usage log {usage_log}: {size} bytes"), size > 0


def covers(cover: str, provider: str) -> bool:
    """A plan cover names a provider exactly (``github``) or one of its products (``github-copilot``)."""
    return cover == provider or cover.startswith(f"{provider}-")


def _paid_names(config: Config, book: PriceBook | None) -> list[str]:
    """What the declared plans pay for: their ``covers``; a plan declared without them, the registry's provider."""
    names = [cover for plan in config.subscriptions for cover in plan.covers]
    if book is not None:
        bare = [sub.plan for sub in config.subscriptions if not sub.covers and sub.plan in book.plans]
        names += [book.plans[plan].provider for plan in bare]
    return names


def uncovered(providers: Iterable[str], config: Config, book: PriceBook | None = None) -> list[str]:
    """The providers no declared plan pays for, sorted."""
    paid = _paid_names(config, book)
    return sorted({name for name in providers if not any(covers(cover, name) for cover in paid)})


def paid_for(provider: str, config: Config, book: PriceBook | None) -> bool:
    """A plan pays for the provider — for GitHub also a switch saying the plan's included allowance is used up."""
    if not uncovered([provider], config, book):
        return True
    github = config.github
    return provider == "github" and (github.copilot_plan_exhausted or github.actions_plan_exhausted)


def setup_checks(
    found: Sequence[SourceFiles], config: Config, book: PriceBook, rows: Sequence[UsageRow]
) -> list[Check]:
    """Plans, billing rules, the window's rows and the budgets: every gap that keeps real from being complete."""
    uses = provider_use(rows)
    placed = _placed_sources(rows)
    unruled = {f.provider for f in found if _needs_rule(f, config) and f.source not in placed}
    return [
        *plan_checks(config, book, uses),
        *(_rule_problem(f) for f in found if f.provider in unruled),
        *(_use_check(use, config, use.provider in unruled) for use in uses),
        budget_check(config.budgets),
    ]


def _placed_sources(rows: Sequence[UsageRow]) -> set[str]:
    """The sources whose every row of the window has a billing: their files, or a plugin, decided each one."""
    return {row.source for row in rows} - {row.source for row in rows if row.billing is Billing.UNKNOWN}


def plan_checks(config: Config, book: PriceBook, uses: Sequence[ProviderUse]) -> list[Check]:
    """Each declared plan, and every provider whose rows are on a plan that no declared plan pays for."""
    checks = [_plan_check(sub.plan, sub.seats, book) for sub in config.subscriptions]
    if not config.subscriptions:
        checks.append(
            Check(
                Mark.INFO,
                "subscriptions: none declared — each plan you pay goes under subscriptions (the names: "
                "ai-cost prices show); pay-per-token alone needs none",
            )
        )
    on_plan = {name for name, rule in config.billing_rules.items() if rule == "subscription"}
    on_plan |= {use.provider for use in uses if use.on_plan}
    for provider in sorted(name for name in on_plan if not paid_for(name, config, book)):
        checks.append(
            Check(
                Mark.PROBLEM,
                f"{provider}: used on a plan but no subscription covers {provider} — the plan's fee is missing "
                f'from real: add it under subscriptions with "covers": ["{provider}"]',
            )
        )
    return checks


def budget_check(budgets: Budgets) -> Check:
    """The budgets ``monitor`` checks — information either way, since none is a valid choice."""
    limits = [f"daily {budgets.daily_usd:g} USD"] if budgets.daily_usd else []
    limits += [f"monthly {budgets.monthly_usd:g} USD"] if budgets.monthly_usd else []
    limits += [
        f"{name} {limit:g} USD a day" for name, limit in sorted(budgets.per_provider_daily_usd.items())
    ]
    if not limits:
        return Check(Mark.INFO, "budgets for monitor: none (budgets.daily_usd / monthly_usd 0 = no check)")
    return Check(Mark.INFO, f"budgets for monitor: {', '.join(limits)} — ai-cost monitor exits 3 above one")


def _plan_check(plan: str, seats: int, book: PriceBook) -> Check:
    if plan in book.plans:
        return Check(Mark.OK, f"subscription {plan} ×{seats}")
    return Check(
        Mark.PROBLEM,
        f"subscription {plan} ×{seats} — unknown plan: a name from ai-cost prices show, or the plan in prices.json",
    )


def _needs_rule(found: SourceFiles, config: Config) -> bool:
    """A CLI whose files never say how a session was paid needs its provider's rule, or its rows stay unknown."""
    return (
        bool(found.count)
        and not found.says_billing
        and config.billing_rules.get(found.provider) not in _RULES
    )


def _rule_problem(found: SourceFiles) -> Check:
    return Check(
        Mark.PROBLEM,
        f"{found.label}: providers.{found.provider}.billing names no rule (mixed) and the files never say how a "
        f"session was paid, so its rows stay out of real — set it: {billing_hint(found.provider)}",
    )


def _use_check(use: ProviderUse, config: Config, already_said: bool) -> Check:
    """The window's rows of one provider; unknown ones are a problem unless the missing rule was said above."""
    rule = config.billing_rules.get(use.provider) or "mixed"
    text = (
        f"{use.provider}: {use.rows} row(s) in the last 24 h — {use.on_plan} on a plan, {use.per_token} per token"
        + (f", {use.unknown} of unknown billing" if use.unknown else "")
        + f" (providers.{use.provider}.billing = {rule})"
    )
    if not use.unknown:
        return Check(Mark.OK, text)
    if already_said:
        return Check(Mark.INFO, text)
    return Check(
        Mark.PROBLEM,
        f"{text} — the unknown ones stay out of real: set the rule, {billing_hint(use.provider)}",
    )
