"""The scheduled jobs of this program (launchd on macOS, cron elsewhere): install, describe, remove."""

from __future__ import annotations

import html
import os
import platform
import re
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import Paths
from .errors import ToolError
from .output import Emit, ensure_dir


@dataclass(frozen=True)
class Job:
    """One scheduled command of this program: launchd label ``ai-cost.<label>``, cron tag ``# ai-cost:<label>``."""

    label: str
    argv_tail: tuple[str, ...]  # after the command that starts this program
    log_name: str  # its log file, under the state directory


@dataclass(frozen=True)
class Cadence:
    """When a job runs: every ``days`` days, or (``days`` 0) every day at ``hour``:``minute`` local time."""

    days: int = 0
    hour: int = 6
    minute: int = 17


PRICES_JOB = Job("prices-check", ("prices", "check", "--quiet"), "prices-check.log")
DAILY_JOB = Job("daily-report", ("daily",), "daily-report.log")
DAILY_CADENCE = Cadence(hour=6, minute=40)


def plist_path(job: Job = PRICES_JOB) -> Path:
    """MacOS launchd agent file."""
    return Path.home() / "Library" / "LaunchAgents" / f"ai-cost.{job.label}.plist"


def plist_paths(job: Job = PRICES_JOB) -> list[Path]:
    """The agent file plus any earlier label of the same job (``<prefix>.ai-cost.<label>.plist``) still on disk."""
    current = plist_path(job)
    earlier = sorted(p for p in current.parent.glob(f"*.ai-cost.{job.label}.plist") if p != current)
    return [current, *earlier]


def is_macos() -> bool:
    """A runtime check (a ``sys.platform`` literal would let the type checker prune the other branch)."""
    return platform.system() == "Darwin"


CRON_TAG = "# ai-cost:prices-check"  # the price check's tag (``cron_tag(PRICES_JOB)``), kept by name
_LEGACY_CRON = re.compile(
    r"ai[-_]cost['\"]?\s+prices\s+check"
)  # price-check lines written before the tag, quoted paths included


def cron_tag(job: Job) -> str:
    """The comment that marks a job's crontab line whatever the command looks like."""
    return f"# ai-cost:{job.label}"


def _is_our_cron_line(line: str, job: Job) -> bool:
    legacy = job is PRICES_JOB and _LEGACY_CRON.search(line) is not None
    return cron_tag(job) in line or legacy


def cron_has_entry(crontab: str, job: Job = PRICES_JOB) -> bool:
    """Whether a crontab text carries the job (tagged, or a pre-tag price-check line)."""
    return any(_is_our_cron_line(line, job) for line in crontab.splitlines())


def cron_without_entry(crontab: str, job: Job = PRICES_JOB) -> list[str]:
    """The crontab lines minus the job's (tagged or legacy)."""
    return [line for line in crontab.splitlines() if not _is_our_cron_line(line, job)]


def _cron_when(cadence: Cadence) -> str:
    """The five cron fields: every N days at 06:17, or every day at the given time."""
    if cadence.days:
        return f"17 6 */{cadence.days} * *"
    return f"{cadence.minute} {cadence.hour} * * *"


def cron_line(days: int, argv: Sequence[str], log: Path) -> str:
    """The price check's crontab line: every ``days`` days at 06:17, quoted for the shell, tagged."""
    return job_cron_line(PRICES_JOB, Cadence(days=days), argv, log)


def job_cron_line(job: Job, cadence: Cadence, argv: Sequence[str], log: Path, search_path: str = "") -> str:
    """A job's crontab line, quoted for the shell and tagged so it can be found and removed.

    ``search_path`` is the PATH of the shell that installed it: cron's own finds neither ``gh`` nor a Homebrew tool.
    """
    command = shlex.join(list(argv))
    env = f"PATH={shlex.quote(search_path)} " if search_path else ""
    return f"{_cron_when(cadence)} {env}{command} >> {shlex.quote(str(log))} 2>&1 {cron_tag(job)}"


def _crontab_write(text: str) -> bool:
    try:
        return (
            subprocess.run(
                ["crontab", "-"], input=text, text=True, capture_output=True, check=False
            ).returncode
            == 0
        )
    except OSError:
        return False


def _crontab() -> str:
    try:
        return subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return ""


def schedule_installed(job: Job = PRICES_JOB) -> bool:
    """Whether the job is registered (launchd or cron)."""
    if is_macos():
        return any(p.exists() for p in plist_paths(job))
    return cron_has_entry(_crontab(), job)


def job_has_path(job: Job) -> bool:
    """Whether the registered job carries a PATH of its own (installed by 2.6 or later)."""
    if is_macos():
        try:
            return "<key>PATH</key>" in plist_path(job).read_text()
        except OSError:
            return False
    return any(" PATH=" in line for line in _crontab().splitlines() if _is_our_cron_line(line, job))


def describe_cadence(cadence: Cadence) -> str:
    """``every 3 day(s)`` or ``daily at 06:40``."""
    if cadence.days:
        return f"every {cadence.days} day(s)"
    return f"daily at {cadence.hour:02d}:{cadence.minute:02d}"


def _launchd_when(cadence: Cadence) -> str:
    if cadence.days:
        return f"<key>StartInterval</key><integer>{cadence.days * 86400}</integer>"
    return (
        "<key>StartCalendarInterval</key><dict>"
        f"<key>Hour</key><integer>{cadence.hour}</integer><key>Minute</key><integer>{cadence.minute}</integer>"
        "</dict>"
    )


def _plist(job: Job, cadence: Cadence, argv: Sequence[str], log: Path, search_path: str = "") -> str:
    """The launchd agent; ``search_path`` (the installing shell's PATH) replaces launchd's bare /usr/bin:/bin one."""
    program = "".join(f"<string>{html.escape(arg)}</string>" for arg in argv)
    out = html.escape(str(log))
    env = (
        f"  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{html.escape(search_path)}</string></dict>\n"
        if search_path
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>Label</key><string>ai-cost.{job.label}</string>\n"
        f"  <key>ProgramArguments</key><array>{program}</array>\n"
        f"{env}"
        f"  {_launchd_when(cadence)}\n"
        f"  <key>StandardOutPath</key><string>{out}</string><key>StandardErrorPath</key><string>{out}</string>\n"
        "  <key>RunAtLoad</key><false/>\n"
        "</dict></plist>\n"
    )


def install_schedule(paths: Paths, days: int, command: Sequence[str], emit: Emit) -> int:
    """Register the periodic price check (launchd on macOS, cron elsewhere); ``command`` starts this program."""
    return install_job(paths, PRICES_JOB, Cadence(days=days), command, emit)


def install_job(paths: Paths, job: Job, cadence: Cadence, command: Sequence[str], emit: Emit) -> int:
    """Register one job (launchd on macOS, cron elsewhere); ``command`` starts this program."""
    ensure_dir(paths.state_dir, "state directory")
    log = paths.state_dir / job.log_name
    argv = [*command, *job.argv_tail]
    search_path = os.environ.get("PATH", "")  # the job finds what this shell finds (gh for the usage report)
    if is_macos():
        return _install_agent(
            _plist(job, cadence, argv, log, search_path), describe_cadence(cadence), emit, job
        )
    lines = [*cron_without_entry(_crontab(), job), job_cron_line(job, cadence, argv, log, search_path)]
    if not _crontab_write("\n".join(lines) + "\n"):
        emit("crontab write failed — is crontab installed and allowed for this user?")
        return 1
    emit(f"cron entry installed ({describe_cadence(cadence)})")
    return 0


def _install_agent(plist: str, when: str, emit: Emit, job: Job = PRICES_JOB) -> int:
    """Write the launchd agent, retire every earlier label, load it: 0 only when it loads AND every earlier label is gone."""
    target = plist_path(job)
    ensure_dir(target.parent, "LaunchAgents directory")
    try:
        target.write_text(plist)
    except OSError as exc:
        raise ToolError(f"cannot write the launchd agent {target}: {exc}") from exc
    retired = [_remove_plist(path, emit) for path in plist_paths(job) if path != target]
    subprocess.run(["launchctl", "unload", str(target)], capture_output=True, check=False)
    result = subprocess.run(["launchctl", "load", str(target)], capture_output=True, text=True, check=False)
    state = "loaded" if result.returncode == 0 else f"written (load failed: {result.stderr.strip()})"
    emit(f"launchd agent {state} {when}: {target}")
    return 0 if result.returncode == 0 and False not in retired else 1  # a label that stayed is said above


def _remove_plist(path: Path, emit: Emit) -> bool | None:
    """Unload and delete one agent file: ``None`` when there was none, ``False`` when it could not be removed."""
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
    if not path.exists():
        return None
    try:
        path.unlink()
    except OSError as exc:  # a directory in its place, a permission: said, never a traceback
        emit(f"cannot remove {path}: {exc}")
        return False
    emit(f"removed {path}")
    return True


def remove_schedule(emit: Emit) -> int:
    """Unregister the periodic price check."""
    return remove_job(PRICES_JOB, emit)


def remove_job(job: Job, emit: Emit) -> int:
    """Unregister one job; every label it ever had goes, none keeps running."""
    if is_macos():
        outcomes = [_remove_plist(path, emit) for path in plist_paths(job)]
        if not any(outcome is not None for outcome in outcomes):
            emit("not installed")
        return 1 if False in outcomes else 0
    crontab = _crontab()
    if not cron_has_entry(crontab, job):
        emit("not installed")
        return 0
    kept = "\n".join(cron_without_entry(crontab, job)) + "\n"
    if not _crontab_write(kept):
        emit("crontab write failed — is crontab installed and allowed for this user?")
        return 1
    emit("cron entry removed")
    return 0
