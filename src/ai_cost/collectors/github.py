"""Live GitHub data through ``gh``: Copilot reviews submitted inside the window and Actions billable minutes.

Minutes come from ``/actions/runs/{id}/timing`` (what GitHub invoices, per runner OS); the run's wall-clock is the
fallback and lands on the configured runner. Public repositories are free and say so.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from ..models import Billing, Collected, Provider, RowKind, Skipped, Tokens, UsageRow, Window
from ..timeutil import parse_ts

OS_RUNNER = {"UBUNTU": "linux", "WINDOWS": "windows", "MACOS": "macos"}

SOURCE_NAME = "github"  # the one name of the live GitHub source on every row


def gh(args: list[str], timeout: int = 60) -> str | None:
    """Run ``gh`` and return stdout, or ``None`` when it is missing or fails (the caller records why)."""
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


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
    repo: str, runs: list[dict[str, Any]], window: Window, runner: str, skipped: list[Skipped]
) -> dict[str, float]:
    by_os: dict[str, float] = {}
    for run in runs:
        for name, minutes in _run_minutes(repo, run, window, runner, skipped).items():
            by_os[name] = by_os.get(name, 0.0) + minutes
    return by_os


def _run_minutes(
    repo: str, run: dict[str, Any], window: Window, runner: str, skipped: list[Skipped]
) -> dict[str, float]:
    """One run's billable minutes per OS from ``/timing``; its elapsed time under ``runner`` when that is missing or malformed."""
    started = parse_ts(run.get("createdAt"))
    if started is None or not window.contains(started):
        if started is None:  # a run nobody can place in time: counted as lost, never dropped in silence
            skipped.append(
                Skipped("github", f"{repo} run {run.get('databaseId')}", "no createdAt — run not counted")
            )
        return {}
    timing = _json(gh(["api", f"repos/{repo}/actions/runs/{run.get('databaseId')}/timing"]))
    billable = (timing or {}).get("billable") if isinstance(timing, dict) else None
    reason = "no /timing — elapsed time used"
    if isinstance(billable, dict) and billable:
        try:
            return _timing_minutes(billable)
        except (
            ValueError
        ) as exc:  # a /timing block that is not what the API documents: said, elapsed time used
            reason = f"malformed /timing ({exc}) — elapsed time used"
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
    return {runner: max(0.0, (finished - started).total_seconds() / 60.0)}


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


def collect_github(repos: list[str], window: Window, runner: str) -> Collected:
    """Two rows per repository: Actions minutes and Copilot reviews; a repo ``gh`` cannot see is a skipped entry."""
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
            rows.append(_actions_row(repo, window, runner, skipped, runs, private))
        reviews = _reviews(repo, window)
        if reviews is None:
            skipped.append(Skipped("github", repo, "gh pr list failed — Copilot reviews unknown, no row"))
        else:
            rows.append(_copilot_row(repo, window, reviews))
    return Collected(rows=rows, skipped=skipped)


def _actions_row(
    repo: str, window: Window, runner: str, skipped: list[Skipped], runs: list[dict[str, Any]], private: str
) -> UsageRow:
    by_os = _minutes(repo, runs, window, runner, skipped)
    return UsageRow(
        provider=Provider.GITHUB,
        model="actions",
        kind=RowKind.ACTIONS,
        source=SOURCE_NAME,
        at=window.start,
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
