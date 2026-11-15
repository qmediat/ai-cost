"""``ai-cost doctor``: the sources, the config, the prices' age, the state and the scheduled jobs, one line each."""

from __future__ import annotations

import os
import shutil
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from . import __version__
from .collectors.github_bill import (
    probe_bill,
)
from .collectors.usage_log import instance_log_files, log_files, scan, unread_files
from .config import Config, Paths, PriceBook
from .daily import newest_index_file, read_index
from .errors import ToolError
from .groups import (
    Report,
)
from .models import (
    UsageRow,
    Window,
)
from .onboarding import (
    REPORT_CHECK_DAYS,
    Check,
    Mark,
    SourceFiles,
    github_checks,
    instance_checks,
    provider_report_checks,
    provider_use,
    setup_checks,
    source_checks,
    source_files,
)
from .ops import (
    ACCOUNTS_UNREAD,
    ReportRequest,
    bill_request,
    build_report,
    piece_name,
    plugins_for,
    read_context,
    reads_clis,
    role_pieces,
)
from .output import Emit
from .plugins import (
    Context,
    Plugin,
)
from .prices_check import CheckResult, load_result, state_file
from .providers import read_reports
from .reconcile import last_reconciliation
from .schedules import DAILY_JOB, job_has_path, schedule_installed
from .timeutil import now

Line = Callable[[bool, str], None]  # a plugin's doctor hook: (fine, text)


def _verdict(good: bool, text: str) -> Check:
    """A line that is fine or a problem to act on."""
    return Check(Mark.OK if good else Mark.PROBLEM, text)


def _present(good: bool, text: str) -> Check:
    """A line that is fine or information: something absent that is allowed to be."""
    return Check(Mark.OK if good else Mark.INFO, text)


def _doctor_window(paths: Paths, config: Config, book: PriceBook, checks: list[Check]) -> Report | None:
    """The last 24 h, collected once: the skipped records and the setup lines read the same rows."""
    try:
        return build_report(ReportRequest(all_projects=True, hours=24.0, groups=()), paths, config, book)
    except ToolError as exc:
        checks.append(_verdict(False, f"collection over the last 24 h failed: {exc}"))
        return None


def _doctor_skipped(report: Report) -> Check:
    """What the collectors could not use in the last 24 h, per source (the counted records of Invariant #14d)."""
    by_source = Counter(item.source for item in report.skipped)
    detail = ", ".join(f"{name} {count}" for name, count in sorted(by_source.items())) or "none"
    return _verdict(
        True,
        f"skipped records in the last 24 h: {sum(by_source.values())} ({detail}) — `report --detail` lists them",
    )


def doctor_checks(paths: Paths, config: Config, book: PriceBook) -> list[Check]:
    """Every doctor line, typed: sources, config, price freshness, state, schedules."""
    checks: list[Check] = []
    _doctor_sources(paths, config, book, checks)
    _doctor_prices(paths, book, checks)
    _doctor_state(paths, checks, config.github.bill is not None, book)
    return checks


def doctor(paths: Paths, config: Config, book: PriceBook, emit: Emit) -> int:
    """Sources, config, price freshness, schedule; exit 1 on any problem."""
    emit(f"ai-cost {__version__} doctor")
    checks = doctor_checks(paths, config, book)
    for check in checks:
        emit(f"  {check.mark.value}  {check.text}")
    problems = sum(check.mark is Mark.PROBLEM for check in checks)
    emit(f"doctor: {'all good' if problems == 0 else f'{problems} issue(s)'}")
    return 0 if problems == 0 else 1


def _doctor_sources(paths: Paths, config: Config, book: PriceBook, checks: list[Check]) -> None:
    """Python, the files of every source, the window's skipped records, the setup lines, plugins, user files."""
    checks.append(_verdict(sys.version_info >= (3, 9), f"python {sys.version.split()[0]}"))
    found = source_files(paths) if reads_clis(paths) else []  # an instance's own lines only
    report = _doctor_window(paths, config, book, checks)
    rows = list(report.rows) if report else []
    checks += _usage_checks(paths, config, found, len(rows) if report else None)
    if report is not None:
        checks.append(_doctor_skipped(report))
    checks += setup_checks(found, config, book, rows)
    checks += _account_checks(paths, config, rows)
    _doctor_plugins(
        paths,
        config,
        lambda good, text: checks.append(_verdict(good, text)),
        lambda text: checks.append(Check(Mark.INFO, text)),
    )
    checks += _user_files(paths)


def _usage_checks(
    paths: Paths, config: Config, found: Sequence[SourceFiles], window_rows: int | None
) -> list[Check]:
    """The CLIs' files and the usage logs; an instance: its own directory, and the CLIs' files when it reads them."""
    if paths.instance is None:
        return source_checks(found, log_files(paths, config), window_rows)
    instance = paths.instance  # ``found`` is empty unless the instance reads the CLIs (``reads_clis``)
    walked = scan(instance)  # one walk of usage/ for every line below
    logs = instance_log_files(instance, config, walked)
    return instance_checks(instance, found, logs, unread_files(walked), window_rows)


def _account_checks(paths: Paths, config: Config, rows: Sequence[UsageRow]) -> list[Check]:
    """The GitHub usage report and the provider day reports; an instance asks neither."""
    if paths.instance is not None:
        return [
            Check(
                Mark.INFO,
                f"account sources (GitHub usage report, provider day reports): not read — {ACCOUNTS_UNREAD}",
            )
        ]
    request = bill_request(paths, config)
    probe = probe_bill(request) if request is not None else None
    uses = provider_use(rows, config.outside_scope_clients)
    week = Window(now() - timedelta(days=REPORT_CHECK_DAYS), now())
    reports = read_reports(config.provider_reports, read_context(paths, week))
    return github_checks(config, uses, probe) + provider_report_checks(config, reports, uses, now())


def _user_files(paths: Paths) -> list[Check]:
    """The config and prices files: present, or absent and what writes the config."""
    user_config, user_prices = paths.user_config_file(), paths.user_prices_file()
    writer = (
        f"ai-cost instance init {paths.instance.root}" if paths.instance else "ai-cost install --init-config"
    )
    config_state = "present" if user_config.exists() else f"absent → defaults ({writer} writes it)"
    prices_state = "present" if user_prices.exists() else "absent → built-in registry"
    return [
        _present(user_config.exists(), f"user config {user_config} {config_state}"),
        _present(user_prices.exists(), f"user prices {user_prices} {prices_state}"),
    ]


def _doctor_plugins(paths: Paths, config: Config, line: Line, info: Callable[[str], None]) -> None:
    """One line per loaded plugin (its sources), its own doctor lines, and every module that failed to load.

    ``line`` takes a verdict (a plugin's own doctor writes through it too), ``info`` a line of information.
    """
    try:
        loaded = plugins_for(paths, config)
    except ToolError as exc:  # a broken bundle marker: one red line, the rest of the doctor still runs
        line(False, f"plugins: {exc}")
        return
    for warning in loaded.warnings:
        line(False, warning)
    if not loaded.plugins:
        info("plugins: none (built-in sources only)")
    end = now()
    window = Window(end - timedelta(hours=config.window_default_hours), end)
    for plugin in loaded.plugins:
        _doctor_plugin(paths, config, plugin, window, line)


def _doctor_plugin(paths: Paths, config: Config, plugin: Plugin, window: Window, line: Line) -> None:
    """A plugin's sources, then its own doctor lines; a plugin that fails its doctor is one red line."""
    try:
        names = ", ".join(piece_name(source) for source in role_pieces(plugin, "sources")) or "no sources"
    except TypeError as exc:  # sources=None: diagnosed, the other plugins still listed
        line(False, f"plugin {plugin.name}: {exc}")
        return
    line(True, f"plugin {plugin.name}: {names}")
    if plugin.doctor is None:
        return
    settings = config.plugin_settings.get(plugin.name, {})
    try:
        plugin.doctor(Context(paths, config, window, ReportRequest(), settings), line)
    except Exception as exc:
        line(False, f"plugin {plugin.name}: doctor failed ({exc.__class__.__name__}: {exc})")


def _doctor_prices(paths: Paths, book: PriceBook, checks: list[Check]) -> None:
    """The price book's age and every row's ``valid_until``; an instance's age is information, never a problem."""
    checks.append(_price_age(book, paths.instance is not None))
    today = now().date()
    for provider, table in book.models.items():
        for model, entry in table.items():
            until = getattr(entry, "valid_until", None)
            if until:
                checks.append(
                    _verdict(until >= today, _validity(f"{provider.value}/{model}", until, entry, today))
                )


def _price_age(book: PriceBook, on_an_instance: bool) -> Check:
    """How old the price book's check is: a problem past three check periods on this machine, a fact on an instance.

    An instance's remedy would be ``prices check``, which works on this machine's own files only; its app ships its
    prices.
    """
    age = (now().date() - book.checked_at).days
    said = f"prices checked_at {book.checked_at} ({age} days ago"
    if on_an_instance:
        return Check(Mark.INFO, f"{said}; an instance's prices are its own prices.json, kept by its app)")
    if book.auto_check_days <= 0:
        return _verdict(True, f"{said}; the drift check is off: auto_check_days 0)")
    return _verdict(age <= book.auto_check_days * 3, f"{said}; auto-check every {book.auto_check_days} days)")


def _validity(name: str, until: date, entry: object, today: date) -> str:
    """A price row's validity line on ``today``, the verdict's own day; an expired one names its next block."""
    if until >= today:
        return f"{name} price valid until {until}"
    after = "the next block applies" if getattr(entry, "next", None) else "NO next block, update prices.json"
    return f"{name} price valid until {until} — expired: {after}"


def _doctor_state(
    paths: Paths, checks: list[Check], bill: bool = False, book: PriceBook | None = None
) -> None:
    """The last price check, the state directory, the tools and the scheduled jobs (not an instance's concern)."""
    if paths.instance is None:
        checks += _price_check_state(paths, book)
    state = paths.state_dir
    remedy = "fix the permissions" if paths.instance else "AI_COST_STATE_DIR, or fix the permissions"
    if _writable(state):
        made = "" if state.exists() else " (created on the first write)"
        checks.append(_verdict(True, f"state dir {state} writable{made}"))
    else:
        checks.append(_verdict(False, f"state dir {state} is not writable — {remedy}"))
    if paths.instance is None:
        checks += _doctor_tools()
        _doctor_reports(paths, checks, bill)


def _price_check_state(paths: Paths, book: PriceBook | None) -> list[Check]:
    """The last price check and what it found that the price book does not show."""
    last = load_result(paths)
    checks = [
        _present(
            bool(last), f"last price check: {last.checked_at if last else 'never'} ({state_file(paths)})"
        )
    ]
    checks += [
        Check(
            Mark.INFO,
            f"{provider}/{model}: listed apart from its row on the vendor page — confirm by hand: {rates}",
        )
        for provider, model, rates in _unchecked_rates(last)
    ]
    checks += [
        Check(
            Mark.INFO,
            f"{provider}/{model}: on the vendor's page, not in the price book — add its row, or name it under the "
            "provider's no_row",
        )
        for provider, model in _unlisted(last, book)
    ]
    return checks


def _unlisted(last: CheckResult | None, book: PriceBook | None) -> list[tuple[str, str]]:
    """The models the last price check found on a vendor's page without a row — less those priced since."""
    if last is None:
        return []
    priced = (
        {provider.value: provider for provider in book.models} if book else {}
    )  # a state file may name any id

    def has_row(provider: str, model: str) -> bool:
        return (
            book is not None and provider in priced and book.entry(priced[provider], model, None) is not None
        )

    return [
        (provider, model)
        for provider, prov in last.providers.items()
        for model in prov.unlisted
        if not has_row(provider, model)
    ]


def _unchecked_rates(last: CheckResult | None) -> list[tuple[str, str, str]]:
    """The rates the last price check found listed apart from a model's row (its --quiet run prints none)."""
    if last is None:
        return []
    return [
        (provider, model, ", ".join(check.unchecked))
        for provider, prov in last.providers.items()
        for model, check in prov.models.items()
        if check.unchecked
    ]


def _doctor_tools() -> list[Check]:
    """Informational only: neither tool is required, so nothing here counts as a problem."""
    has_gh = shutil.which("gh") is not None
    scheduled = schedule_installed()
    return [
        _present(
            has_gh,
            f"gh {'present' if has_gh else 'absent'} (needed only for --github and providers.github.bill)",
        ),
        _present(
            scheduled,
            f"scheduled price check: {'installed' if scheduled else 'not installed (ai-cost install --schedule 3)'}",
        ),
    ]


def _daily_job_check(bill: bool) -> Check:
    """The daily-report job: absent is information; one installed before 2.6 cannot find gh for the usage report."""
    installed = schedule_installed(DAILY_JOB)
    if installed and bill and not job_has_path(DAILY_JOB):
        return _verdict(
            False,
            "scheduled daily reports: installed without a PATH — the job cannot find gh to read the "
            "GitHub usage report; run ai-cost install --schedule-reports again",
        )
    state = "installed" if installed else "not installed (ai-cost install --schedule-reports)"
    return _present(installed, f"scheduled daily reports: {state}")


def _doctor_reports(paths: Paths, checks: list[Check], bill: bool = False) -> None:
    """The daily-report job and what it last wrote, the last reconciliation: absent is informational, torn is red."""
    checks.append(_daily_job_check(bill))
    index = newest_index_file(paths.reports_dir)
    if index is None:
        checks.append(Check(Mark.INFO, f"daily reports: none yet in {paths.reports_dir} (ai-cost daily)"))
    else:
        try:
            last = read_index(index)
            failed = f", {len(last.failures)} project(s) FAILED — see its index.json" if last.failures else ""
            checks.append(
                _verdict(
                    not last.failures,
                    f"daily reports: last {last.day} ({len(last.projects)} project(s){failed}) in {paths.reports_dir}",
                )
            )
        except ToolError as exc:
            checks.append(_verdict(False, f"daily reports: {exc}"))
    checks.append(_last_reconcile(paths))


def _last_reconcile(paths: Paths) -> Check:
    """The last reconciliation: never (information), within tolerance, above it, or unreadable."""
    try:
        check = last_reconciliation(paths)
    except ToolError as exc:
        return _verdict(False, f"last reconcile: {exc}")
    if check is None:
        return Check(
            Mark.INFO, "last reconcile: never (ai-cost reconcile --provider <id> --usd <console figure>)"
        )
    verdict = "within tolerance" if check.within_tolerance else "ABOVE TOLERANCE"
    return _verdict(
        check.within_tolerance,
        f"last reconcile: {check.provider} {check.window[0]} → {check.window[1]}: {verdict}",
    )


def _writable(path: Path) -> bool:
    """A directory that can be written, or created: its nearest existing ancestor is a writable directory."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        if probe.is_symlink():  # a dangling link: the first write would fail on it, not create a directory
            return False
        probe = probe.parent
    return probe.is_dir() and os.access(probe, os.W_OK)
