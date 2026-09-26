"""Live GitHub data through ``gh``: Copilot reviews submitted inside the window and Actions billable minutes.

Minutes come from ``/actions/runs/{id}/timing`` (what GitHub invoices, per runner OS); a run without it keeps its
wall-clock under ``elapsed``, which is shown and never priced (it is not what GitHub bills). Public repositories are
free and say so.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from ..models import ELAPSED_RUNNER, Billing, Collected, Provider, RowKind, Skipped, Tokens, UsageRow, Window
from ..timeutil import parse_ts

OS_RUNNER = {"UBUNTU": "linux", "WINDOWS": "windows", "MACOS": "macos"}

SOURCE_NAME = "github"  # the one name of the live GitHub source on every row


@dataclass(frozen=True)
class GhCall:
    """What one ``gh`` run returned; ``failure`` is empty when it succeeded, else why it did not."""

    stdout: str
    failure: str


def run_gh(args: list[str], timeout: int = 60) -> GhCall:
    """Run ``gh``; a missing binary, a timeout and a non-zero exit each come back as a ``failure`` text."""
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        failure = "gh is not installed"
    except subprocess.TimeoutExpired:
        failure = f"gh did not answer within {timeout} s"
    except (OSError, subprocess.SubprocessError) as exc:
        failure = f"gh could not run: {exc}"
    else:
        return _outcome(result)
    return GhCall("", failure)


def _outcome(result: subprocess.CompletedProcess[str]) -> GhCall:
    """Its output, or the last line it said on failure (``gh`` puts the HTTP status there)."""
    if result.returncode == 0:
        return GhCall(result.stdout, "")
    said = (result.stderr or result.stdout).strip().splitlines()
    return GhCall("", said[-1] if said else f"gh exited {result.returncode}")


def gh(args: list[str], timeout: int = 60) -> str | None:
    """Run ``gh`` and return stdout, or ``None`` when it is missing or fails (the caller records why)."""
    call = run_gh(args, timeout)
    return None if call.failure else call.stdout


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


_MAX_RUN_MS = 10**13  # ~317 years: beyond any run, and far below what float arithmetic keeps exact


def _timing_minutes(billable: dict[str, Any]) -> dict[str, float]:
    """Minutes per runner OS from a ``/timing`` billable block; a value that is not a numeric ``total_ms`` is a ``ValueError``."""
    minutes: dict[str, float] = {}
    for os_key, value in billable.items():
        if not isinstance(value, dict):
            raise ValueError(f"billable.{os_key} is not an object")
        total = value.get("total_ms")
        total = (
            0 if total is None else total
        )  # absent is zero; anything else a finite, non-negative, bounded number
        if isinstance(total, bool) or not isinstance(total, (int, float)) or not 0 <= total <= _MAX_RUN_MS:
            raise ValueError(  # NaN fails the chained comparison too: it is neither ≥ 0 nor ≤ the bound
                f"billable.{os_key}.total_ms is not a non-negative number below {_MAX_RUN_MS}"
            )
        name = OS_RUNNER.get(str(os_key).upper(), str(os_key).lower())
        minutes[name] = minutes.get(name, 0.0) + float(total) / 60000.0
    return minutes


def _minutes(
    repo: str, runs: list[dict[str, Any]], window: Window, skipped: list[Skipped]
) -> dict[date, dict[str, float]]:
    """Billable minutes per UTC day of a run's start and per runner: the usage report bills Actions per day."""
    days: dict[date, dict[str, float]] = {}
    for run in runs:
        minutes = _run_minutes(repo, run, window, skipped)
        started = parse_ts(run.get("createdAt"))
        if not minutes or started is None:
            continue
        by_os = days.setdefault(started.astimezone(timezone.utc).date(), {})
        for name, value in minutes.items():
            by_os[name] = by_os.get(name, 0.0) + value
    return days


def _run_minutes(repo: str, run: dict[str, Any], window: Window, skipped: list[Skipped]) -> dict[str, float]:
    """One run's billable minutes per OS from ``/timing``; its elapsed time under ``elapsed`` when that is missing."""
    started = parse_ts(run.get("createdAt"))
    if started is None or not window.contains(started):
        if started is None:  # a run nobody can place in time: counted as lost, never dropped in silence
            skipped.append(
                Skipped("github", f"{repo} run {run.get('databaseId')}", "no createdAt — run not counted")
            )
        return {}
    timing = _json(gh(["api", f"repos/{repo}/actions/runs/{run.get('databaseId')}/timing"]))
    billable = (timing or {}).get("billable") if isinstance(timing, dict) else None
    reason = "no /timing — elapsed time shown, not priced"
    if isinstance(billable, dict) and billable:
        try:
            return _timing_minutes(billable)
        except (
            ValueError
        ) as exc:  # a /timing block that is not what the API documents: said, elapsed time shown
            reason = f"malformed /timing ({exc}) — elapsed time shown, not priced"
    finished = parse_ts(run.get("updatedAt"))
    label = f"{repo} run {run.get('databaseId')}"
    if not (
        started and finished
    ):  # neither /timing nor an elapsed time: the run is counted as lost, never silently
        skipped.append(
            Skipped("github", label, f"{reason.split(' — ')[0]} and no updatedAt — run not counted")
        )
        return {}
    skipped.append(Skipped("github", label, reason))
    return {ELAPSED_RUNNER: max(0.0, (finished - started).total_seconds() / 60.0)}


def _reviews(repo: str, window: Window) -> int | None:
    """Copilot reviews submitted inside the window; ``None`` when ``gh pr list`` failed (not the same as zero)."""
    since = window.start.strftime("%Y-%m-%d")
    listing = gh(
        [
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "all",
            "--limit",
            "100",
            "--search",
            f"updated:>={since}",
            "--json",
            "number",
        ]
    )
    prs = _json(listing) if listing is not None else None
    if not isinstance(prs, list) or any(not isinstance(pr, dict) or "number" not in pr for pr in prs):
        return None  # a failed or misshapen listing: unknown, not zero
    count = 0
    for pr in prs:
        text = gh(
            [
                "api",
                f"repos/{repo}/pulls/{pr['number']}/reviews",
                "--jq",
                '[.[] | select(.user.login | test("copilot"; "i")) | .submitted_at]',
            ]
        )
        stamps = _json(text)
        if text is None or not isinstance(stamps, list):
            return None  # one PR's reviews unknown → the repository's count is unknown, not smaller
        count += sum(1 for stamp in stamps if stamp and window.contains(parse_ts(stamp)))
    return count


def collect_github(repos: list[str], window: Window) -> Collected:
    """Per repository: Actions minutes per UTC day and one Copilot review count; one ``gh`` cannot see is skipped."""
    rows: list[UsageRow] = []
    skipped: list[Skipped] = []
    for repo in repos:
        private = gh(["repo", "view", repo, "--json", "isPrivate", "--jq", ".isPrivate"])
        if private is None:
            skipped.append(
                Skipped("github", repo, "gh repo view failed (missing gh, no auth, or unknown repo)")
            )
            continue
        since = window.start.strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = gh(
            [
                "run",
                "list",
                "-R",
                repo,
                "--limit",
                "200",
                "--created",
                f">={since}",
                "--json",
                "databaseId,createdAt,updatedAt,status",
            ]
        )
        runs = _json(listing) if listing is not None else None
        if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
            skipped.append(
                Skipped("github", repo, "gh run list failed or misshapen — Actions minutes unknown, no row")
            )
        else:
            rows += _actions_rows(repo, window, skipped, runs, private)
        reviews = _reviews(repo, window)
        if reviews is None:
            skipped.append(Skipped("github", repo, "gh pr list failed — Copilot reviews unknown, no row"))
        else:
            rows.append(_copilot_row(repo, window, reviews))
    return Collected(rows=rows, skipped=skipped)


def _actions_rows(
    repo: str, window: Window, skipped: list[Skipped], runs: list[dict[str, Any]], private: str
) -> list[UsageRow]:
    """One row per UTC day with runs; a repository without any keeps one empty row.

    A day's minutes are settled by that day's month of the usage report, never by another's (ADR-0007).
    """
    days = _minutes(repo, runs, window, skipped) or {window.start.astimezone(timezone.utc).date(): {}}
    return [_actions_row(repo, day, by_os, window, private) for day, by_os in sorted(days.items())]


def _actions_row(repo: str, day: date, by_os: dict[str, float], window: Window, private: str) -> UsageRow:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return UsageRow(
        provider=Provider.GITHUB,
        model="actions",
        kind=RowKind.ACTIONS,
        source=SOURCE_NAME,
        at=max(window.start, start),
        ref=repo,
        billing=Billing.SUBSCRIPTION,
        tokens=Tokens(
            minutes=round(sum(by_os.values()), 1),
            by_os={k: round(v, 1) for k, v in by_os.items()},
            billable=private.strip() == "true",
        ),
    )


def _copilot_row(repo: str, window: Window, reviews: int) -> UsageRow:
    return UsageRow(
        provider=Provider.GITHUB,
        model="copilot-code-review",
        kind=RowKind.COPILOT,
        source=SOURCE_NAME,
        at=window.start,
        ref=repo,
        billing=Billing.SUBSCRIPTION,
        tokens=Tokens(reviews=reviews),
    )
