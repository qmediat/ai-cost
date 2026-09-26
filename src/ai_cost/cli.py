"""Command-line surface: parse, dispatch, return an exit code. ``__main__.run()`` is the only ``sys.exit``."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import __version__
from .collectors.usage_log import LOG_COUNTERS
from .config import Paths, PriceBook, builtin_prices, load_config, load_pricebook
from .daily import Output, daily_reports, yesterday
from .errors import ToolError, UsageError
from .log import (
    Attribution,
    Usage,
    from_response,
    validated,
)
from .log import (
    append as append_log,
)
from .log import (
    default_path as default_log_path,
)
from .log import (
    entry as log_entry,
)
from .models import CheckStatus, Provider
from .onboarding import SETUP_GUIDE, START_HERE
from .ops import (
    DAILY_CADENCE,
    DAILY_JOB,
    Cadence,
    Emit,
    ReportRequest,
    build_report,
    doctor,
    history,
    init_config,
    install_job,
    install_schedule,
    monitor,
    remove_job,
    remove_schedule,
    run_auto_check,
)
from .prices_check import apply_check, check_prices, exit_code, merged_registry_json, save_result
from .process import self_command, transient_warning
from .reconcile import Reported, run_reconcile
from .render import plain, render_json, render_markdown, render_text
from .timeutil import parse_cli_ts


def emit(text: str) -> None:
    """Every human-readable line of stdout goes through here."""
    sys.stdout.write(text + "\n")


def emit_err(text: str) -> None:
    """Warnings and progress go to stderr."""
    sys.stderr.write("ai-cost: " + text + "\n")


def _add_window(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", help="window start (ISO-8601, date or epoch)")
    parser.add_argument("--until", help="window end (default now)")
    parser.add_argument(
        "--hours",
        type=float,
        help="window = last N hours, N > 0 (default: the config's window_default_hours, 24; with --until: the N hours before it; not with --since; a --session span wins)",
    )


def _report_sources(report: argparse.ArgumentParser) -> None:
    report.add_argument(
        "--session", help="Claude Code session id, prefix, 'latest', 'all' or a transcript path"
    )
    report.add_argument(
        "--project", type=Path, help="project directory whose transcripts to read (default: cwd)"
    )
    report.add_argument(
        "--all-projects", action="store_true", help="every project's transcripts inside the window"
    )
    _add_window(report)
    report.add_argument(
        "--setting",
        action="append",
        default=[],
        metavar="PLUGIN.KEY=VALUE",
        help="a plugin setting for this run (overrides config plugin_settings); repeatable",
    )
    report.add_argument(
        "--github", help="owner/repo[,owner/repo]: live Copilot reviews and Actions minutes via gh"
    )


def _report_output(report: argparse.ArgumentParser) -> None:
    report.add_argument("--group", default="real,api,vendor", help="real,api,vendor")
    report.add_argument(
        "--items",
        type=Path,
        help="work items for the vendor group: JSON or a Markdown table with a size column",
    )
    report.add_argument("--vendor-profile", help="profile name from config.vendor.profiles")
    report.add_argument("--format", choices=["md", "json", "table"], default="md")
    report.add_argument("--out", type=Path)
    report.add_argument("--detail", action="store_true", help="include every usage row in JSON")
    report.add_argument(
        "--unpriced",
        choices=["fail", "skip"],
        default="fail",
        help="a model without a price and without a reported cost: fail (exit 5, default) or skip it, counted",
    )
    report.add_argument(
        "--attribute",
        action="append",
        default=[],
        metavar="LABEL=REGEX",
        help="attribute rows to LABEL when REGEX matches their branch / workspace / PR, else the paths the turn "
        "touched (prices every row like --group api); "
        "(repeatable; mixed and unattributed are always reported)",
    )
    report.add_argument("--no-auto-check", action="store_true")
    report.add_argument("--quiet", action="store_true")


def _report_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    report = sub.add_parser("report", help="the three groups for a window (default command)")
    _report_sources(report)
    _report_output(report)


def _prices_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    prices = sub.add_parser(
        "prices", help="show | check (drift vs vendor pages) | update (check + apply to your prices file)"
    )
    prices.add_argument("action", choices=["show", "check", "update"])
    prices.add_argument("--providers", help="comma-separated subset")
    prices.add_argument("--format", choices=["text", "json"], default="text")
    prices.add_argument(
        "--snapshot", action="store_true", help="with show --format json: the built-in registry only"
    )
    prices.add_argument("--quiet", action="store_true")


def _doctor_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sub.add_parser("doctor", help="diagnostics: sources, config, price freshness, schedules, last daily run")


def _daily_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    daily = sub.add_parser(
        "daily", help="write a day's global report and one per project touched (default: yesterday, UTC)"
    )
    daily.add_argument("--date", metavar="YYYY-MM-DD", help="the UTC day to report (default: yesterday)")
    daily.add_argument(
        "--out", type=Path, help="reports directory (default: AI_COST_REPORTS_DIR or the XDG data dir)"
    )
    daily.add_argument("--quiet", action="store_true", help="print nothing but errors")


def _reconcile_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    rec = sub.add_parser(
        "reconcile",
        help="compare the local count of one provider with its console figure for a window (exit 1 above tolerance)",
    )
    rec.add_argument("--provider", required=True, help="pricebook provider id (xai, openai, …)")
    rec.add_argument(
        "--usd", type=float, required=True, help="what the provider's console shows for the window"
    )
    rec.add_argument("--tokens", type=int, help="the console's token total for the window, when it shows one")
    _add_window(rec)


def _monitor_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    mon = sub.add_parser("monitor", help="rolling window totals → history + budget check (exit 3 on breach)")
    _add_window(mon)
    mon.add_argument("--append", action="store_true", help="append to history.jsonl")
    mon.add_argument("--history", nargs="?", const=20, type=int, help="print the last N history entries")
    mon.add_argument("--quiet", action="store_true")


def _install_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    install = sub.add_parser(
        "install",
        help="--init-config writes the user config; --schedule N installs a price check every N days",
        description="Set the tool up: the user config (start here), the periodic price check, the daily reports.",
    )
    install.add_argument(
        "--init-config",
        action="store_true",
        help="write ~/.config/ai-cost/config.json from the defaults and print what to put in it",
    )
    install.add_argument(
        "--force", action="store_true", help="with --init-config: overwrite an existing file"
    )
    install.add_argument(
        "--schedule", metavar="DAYS", type=int, help="check the vendors' price pages every DAYS days"
    )
    install.add_argument("--unschedule", action="store_true", help="remove the price-check job")
    install.add_argument(
        "--schedule-reports",
        action="store_true",
        help="run `ai-cost daily` every day (launchd on macOS, cron)",
    )
    install.add_argument(
        "--at", metavar="HH:MM", help="local time of the daily run (default 06:40; with --schedule-reports)"
    )
    install.add_argument("--unschedule-reports", action="store_true", help="remove the daily-report job")


def _log_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    log = sub.add_parser("log", help="append one usage-log line: counters from flags or a raw API response")
    log.add_argument("--provider", help="pricebook provider id (required for OpenAI-shaped responses)")
    log.add_argument("--model", help="model id as the API reports it (taken from the response when present)")
    log.add_argument("--from-response", metavar="FILE", help="raw API response JSON; '-' reads stdin")
    for counter in _LOG_COUNTERS:
        log.add_argument(f"--{counter.replace('_', '-')}", type=int, metavar="N")
    log.add_argument("--cost", type=float, help="what the request was charged, USD")
    log.add_argument("--billing", choices=["api", "subscription"])
    for key in ("ref", "session", "branch", "pr", "source", "event-id"):
        log.add_argument(f"--{key}")
    log.add_argument("--tag", action="append", default=[], metavar="TAG")
    log.add_argument("--at", help="ISO-8601 timestamp of the request (default: now)")
    log.add_argument("--log", type=Path, help="the log file (default: AI_COST_USAGE_LOG or the XDG data dir)")


def _selftest_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    selftest = sub.add_parser("selftest", help="run the shipped tests without pytest")
    selftest.add_argument("-v", "--verbose", action="store_true", help="print every passing test too")


_LOG_COUNTERS = LOG_COUNTERS  # one list: the usage log's provider-native counters (+ the thoughts alias)
_COMMANDS = (
    _report_command,
    _prices_command,
    _doctor_command,
    _monitor_command,
    _daily_command,
    _reconcile_command,
    _install_command,
    _log_command,
    _selftest_command,
)


def build_parser() -> argparse.ArgumentParser:
    """The CLI; ``report`` is the default subcommand."""
    parser = argparse.ArgumentParser(
        prog="ai-cost",
        description="Cost of AI-assisted work: real (subscriptions + API keys), API-only equivalent, vendor quote.",
        epilog="\n".join(("start here:", *START_HERE, f"guide: {SETUP_GUIDE}")),
        formatter_class=argparse.RawDescriptionHelpFormatter,  # the epilog keeps its lines: the URL never wraps
    )
    parser.add_argument("--version", action="version", version=f"ai-cost {__version__}")
    sub = parser.add_subparsers(dest="command")
    for add_command in _COMMANDS:
        add_command(sub)
    return parser


def parse_settings(specs: Sequence[str]) -> dict[str, dict[str, str]]:
    """``PLUGIN.KEY=VALUE`` specs as ``{plugin: {key: value}}``; anything else is a ``UsageError``."""
    settings: dict[str, dict[str, str]] = {}
    for spec in specs:
        name, eq, value = spec.partition("=")
        plugin, dot, key = name.partition(".")
        if not (eq and dot and plugin and key):
            raise UsageError(f"--setting: expected PLUGIN.KEY=VALUE, got {spec!r}")
        settings.setdefault(plugin, {})[key] = value
    return settings


def _check_interval(book: PriceBook) -> str:
    return (
        f"auto-check every {book.auto_check_days} days"
        if book.auto_check_days > 0
        else "drift check off (auto_check_days 0)"
    )


def _request(args: argparse.Namespace) -> ReportRequest:
    return ReportRequest(
        session=args.session,
        project=args.project,
        all_projects=args.all_projects,
        since=args.since,
        until=args.until,
        hours=args.hours,
        settings=parse_settings(args.setting),
        github=tuple(g.strip() for g in (args.github or "").split(",") if g.strip()),
        groups=tuple(g.strip() for g in args.group.split(",") if g.strip()),
        items=args.items,
        vendor_profile=args.vendor_profile,
        detail=args.detail,
        attribute=tuple(args.attribute),
        unpriced=args.unpriced,
    )


def cmd_report(args: argparse.Namespace, paths: Paths) -> int:
    """Collect, group, render."""
    config, book = load_config(paths), load_pricebook(paths)
    if not args.no_auto_check:
        run_auto_check(paths, book, args.quiet, emit_err)
    report = build_report(_request(args), paths, config, book)
    if args.format == "json":
        text = render_json(report, args.detail)
    elif args.format == "table":
        text = render_text(report)
    else:
        text = render_markdown(report)
    if args.out:
        try:
            args.out.write_text(text)
        except OSError as exc:
            raise UsageError(f"--out: cannot write {args.out} ({exc.__class__.__name__})") from exc
        emit(f"written {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def _show_prices(args: argparse.Namespace, paths: Paths) -> int:
    book = load_pricebook(paths)
    if args.format == "json":
        emit(json.dumps(builtin_prices() if args.snapshot else merged_registry_json(book), indent=2))
        return 0
    user = paths.user_prices_file()
    emit(
        f"prices checked_at {book.checked_at} · {_check_interval(book)} · user file {user}{'' if user.exists() else ' (absent)'}"
    )
    for provider, table in book.models.items():
        emit(f"\n{provider.value}  {book.sources.get(provider, '')}")
        for model, entry in table.items():
            emit(f"  {model:<26} {entry}")
    emit(f"  github: {book.github}")
    emit("\nplans:")
    for name, plan in book.plans.items():
        emit(f"  {name:<24} {plan.monthly_usd:>7.2f} USD/month{'  per seat' if plan.per_seat else ''}")
    return 0


def cmd_prices(args: argparse.Namespace, paths: Paths) -> int:
    """Show / check / update."""
    if args.action == "show":
        return _show_prices(args, paths)
    if paths.offline:
        raise ToolError("AI_COST_OFFLINE=1 — price check disabled", code=2)
    book = load_pricebook(paths)
    providers = [p.strip() for p in args.providers.split(",")] if args.providers else None
    result = check_prices(book, providers)
    applied = apply_check(result, book, paths.user_prices_file()) if args.action == "update" else []
    save_result(paths, result)
    (paths.state_dir / "prices-check.lock").unlink(missing_ok=True)
    unresolved = sum(
        check.status is CheckStatus.CHANGED
        for prov in result.providers.values()
        for check in prov.models.values()
    )
    if args.format == "json":
        emit(
            json.dumps(
                {"providers": plain(result.providers), "applied": applied, "unresolved": unresolved}, indent=2
            )
        )
        return exit_code(unresolved)
    for name, prov in result.providers.items():
        emit(f"{name:<10} {prov.status}")
        for model, check in prov.models.items():
            if not args.quiet or check.status.value != "confirmed":
                flag = {"confirmed": "ok ", "changed?": "!! ", "not-found": "?? ", "applied": "++ "}.get(
                    check.status.value, "   "
                )
                emit(f"   {flag}{model:<26} expected {check.expected} seen {check.seen[:12]}")
    for provider, model, new_in, new_out in applied:
        emit(f"applied {provider}/{model} → input {new_in} output {new_out} ({paths.user_prices_file()})")
    code = exit_code(unresolved)
    if code:
        emit(
            f"\n{unresolved} suspected change(s) not applied. Verify on the vendor page, then `ai-cost prices update` or edit {paths.user_prices_file()}."
        )
    return code


def cmd_monitor(args: argparse.Namespace, paths: Paths) -> int:
    """History or a rolling-window check."""
    if args.history is not None:
        return history(paths, args.history, emit)
    config, book = load_config(paths), load_pricebook(paths)
    request = ReportRequest(
        all_projects=True, since=args.since, until=args.until, hours=args.hours, groups=("real", "api")
    )
    return monitor(request, paths, config, book, args.append, emit if not args.quiet else _quiet_emit)


def _quiet_emit(text: str) -> None:
    if text.startswith("BUDGET BREACH"):
        emit(text)


def cmd_install(args: argparse.Namespace, paths: Paths) -> int:
    """User config, the periodic price check and the daily-report job."""
    _check_install_args(args)
    code = 0
    if args.init_config:
        code = init_config(paths, args.force, emit)
    if args.unschedule:
        code = remove_schedule(emit) or code
    if args.unschedule_reports:
        code = remove_job(DAILY_JOB, emit) or code
    refusal = transient_warning(self_command()) if args.schedule or args.schedule_reports else ""
    if (
        refusal
    ):  # only the jobs are refused: one registered here would stop in silence the day npm prunes the file
        emit_err(f"not scheduled: {refusal}")
        return 2
    if args.schedule:
        code = install_schedule(paths, args.schedule, self_command(), emit) or code
    if args.schedule_reports:
        cadence = _cadence(args.at) if args.at else DAILY_CADENCE
        code = install_job(paths, DAILY_JOB, cadence, self_command(), emit) or code
    return code


def _check_install_args(args: argparse.Namespace) -> None:
    """Each modifier with its action, a whole-day interval, at least one action — else a ``UsageError``."""
    if args.schedule is not None and args.schedule < 1:
        raise UsageError(f"--schedule: the interval is in whole days, at least 1 (got {args.schedule})")
    if args.at and not args.schedule_reports:
        raise UsageError("--at sets the time of --schedule-reports; give both")
    if args.force and not args.init_config:
        raise UsageError("--force overwrites the file of --init-config; give both")
    actions = (
        args.init_config,
        args.schedule is not None,
        args.unschedule,
        args.schedule_reports,
        args.unschedule_reports,
    )
    if not any(actions):
        raise UsageError(
            "install: nothing to do — start with --init-config, or give --schedule DAYS, --schedule-reports, "
            "--unschedule, --unschedule-reports (ai-cost install --help)"
        )


def _cadence(text: str) -> Cadence:
    """``HH:MM`` (24 h) as the daily cadence; anything else is a ``UsageError`` naming the flag."""
    hour, sep, minute = text.partition(":")
    if not (sep and hour.isdigit() and minute.isdigit() and 0 <= int(hour) < 24 and 0 <= int(minute) < 60):
        raise UsageError(f"--at: expected HH:MM (24-hour), got {text!r}")
    return Cadence(hour=int(hour), minute=int(minute))


def cmd_daily(args: argparse.Namespace, paths: Paths) -> int:
    """The day's reports: global and per project."""
    try:
        day = date.fromisoformat(args.date) if args.date else yesterday()
    except ValueError as exc:
        raise UsageError(f"--date: expected YYYY-MM-DD, got {args.date!r}") from exc
    config, book = load_config(paths), load_pricebook(paths)
    out = args.out or paths.reports_dir
    return daily_reports(paths, config, book, day, out, Output(_nothing if args.quiet else emit, emit_err))


def _nothing(text: str) -> None:
    """``--quiet``: the summary lines are dropped; a failed project still reaches stderr and the exit code."""


def cmd_reconcile(args: argparse.Namespace, paths: Paths) -> int:
    """The local count of one provider against its console figure."""
    try:
        provider = Provider.of(args.provider).value
    except ValueError as exc:
        raise UsageError(f"--provider: {exc}") from exc
    if not math.isfinite(args.usd) or args.usd < 0:
        raise UsageError(f"--usd: expected a finite amount ≥ 0, got {args.usd!r}")
    if args.tokens is not None and args.tokens < 0:
        raise UsageError(f"--tokens: expected a count ≥ 0, got {args.tokens}")
    config, book = load_config(paths), load_pricebook(paths)
    request = ReportRequest(all_projects=True, since=args.since, until=args.until, hours=args.hours)
    return run_reconcile(request, paths, config, book, Reported(provider, args.usd, args.tokens), emit)


def cmd_doctor(args: argparse.Namespace, paths: Paths) -> int:
    """Diagnostics."""
    return doctor(paths, load_config(paths), load_pricebook(paths), emit)


def _log_usage(args: argparse.Namespace) -> Usage:
    """The usage of one ``log`` invocation: from a response file, else from the counter flags."""
    if args.from_response:
        given = [name for name in _LOG_COUNTERS if getattr(args, name) is not None]
        if given:  # both would be a silent choice: the response wins and the flags vanish
            raise UsageError(
                f"log: --from-response takes the counters from the response; drop --{given[0].replace('_', '-')}"
            )
        try:
            text = (
                sys.stdin.read()
                if args.from_response == "-"
                else Path(args.from_response).read_text(encoding="utf-8")
            )
            payload = json.loads(text)
            return from_response(payload, args.provider, args.model)
        except OSError as exc:  # a missing or unreadable response file
            raise UsageError(f"--from-response: cannot read {args.from_response}: {exc}") from exc
        except (
            ValueError,
            RecursionError,
        ) as exc:  # not JSON (or nested beyond reason), or a counter no count can hold
            raise UsageError(f"--from-response: {exc}") from exc
    if not args.provider or not args.model:
        raise UsageError("log: --provider and --model are required without --from-response")
    tokens = {name: getattr(args, name) for name in _LOG_COUNTERS if getattr(args, name) is not None}
    return Usage(args.provider, args.model, tokens, args.cost)


def _checked_usage(usage: Usage, cost: float | None, book: PriceBook) -> Usage:
    """The line as the reader will accept it (``validated``), with ``--cost`` filled in; a problem is a UsageError."""
    if cost is not None and usage.cost is None:
        usage = Usage(usage.provider, usage.model, usage.tokens, cost)
    try:
        return validated(usage, book)
    except ValueError as exc:
        raise UsageError(f"log: {exc}") from exc


def cmd_log(args: argparse.Namespace, paths: Paths) -> int:
    """Append one usage line and echo it."""
    usage = _checked_usage(_log_usage(args), args.cost, load_pricebook(paths))
    meta = Attribution(
        ref=args.ref or "",
        session=args.session or "",
        branch=args.branch or "",
        pr=args.pr or "",
        tags=tuple(args.tag),
        source=args.source or "",
        event_id=args.event_id or "",
        billing=args.billing or "",
    )
    at = parse_cli_ts(args.at, "--at") if args.at else None
    line = log_entry(usage, at, meta)
    target = args.log or paths.usage_log or default_log_path()  # the Paths given, never the process env alone
    try:
        append_log(target, line)
    except OSError as exc:  # an unwritable log path is a ToolError, never a traceback
        raise ToolError(f"cannot write {target}: {exc}") from exc
    emit(json.dumps(line, ensure_ascii=False))
    if args.log and not _reports_read(Path(target), paths):
        emit_err(
            f"note: {target} is not a file reports read — add it to usage_logs in {paths.user_config_file()}"
        )
    return 0


def _reports_read(target: Path, paths: Paths) -> bool:
    """Whether a report would read ``target``: the default log, or a path the config lists under ``usage_logs``."""
    listed = [paths.usage_log or default_log_path()]
    try:
        listed += [Path(p).expanduser() for p in load_config(paths).usage_logs]
    except ToolError:  # a config that does not parse: the report itself will say so
        return True
    return any(_same_file(target, path) for path in listed)


def _same_file(one: Path, other: Path) -> bool:
    try:
        return one.resolve() == other.resolve()
    except (OSError, RuntimeError):
        return False


def cmd_selftest(args: argparse.Namespace, paths: Paths) -> int:
    """The shipped tests without pytest, then the tests of every loaded plugin that ships a tests package."""
    from .selftest import run_package, run_selftest

    code = run_selftest(emit, verbose=args.verbose)
    plugins, config_code = _selftest_plugins(paths, emit)
    return max(code, config_code, _plugin_tests(plugins, emit, args.verbose, run_package))


def _selftest_plugins(paths: Paths, emit: Emit) -> tuple[Sequence[Any], int]:
    """The plugins the user config names; a config that does not parse is a counted failure, not an early exit."""
    from .ops import load_configured_plugins

    try:
        config = load_config(paths)
    except ToolError as exc:
        emit(f"  FAIL user config: {exc} — the plugins it names were not tested")
        return (), 1
    try:
        loaded = load_configured_plugins(config)
    except ToolError as exc:  # a broken bundle marker: the cause is the marker, not the config
        emit(f"  FAIL plugins: {exc} — no plugin was tested")
        return (), 1
    for warning in loaded.warnings:  # a plugin the config names but that cannot be used: its tests never ran
        emit(f"  FAIL plugin: {warning}")
    return loaded.plugins, 1 if loaded.warnings else 0


def _plugin_tests(plugins: Sequence[Any], emit: Emit, verbose: bool, run: Any) -> int:
    """Every plugin's tests package; one that cannot be imported or collected is a counted failure, never an abort."""
    code = 0
    for plugin in plugins:
        if not plugin.tests_package:
            continue
        try:
            package = importlib.import_module(plugin.tests_package)
            code = max(code, run(package, emit, verbose, label=f"{plugin.name} selftest"))
        except Exception as exc:
            emit(
                f"  FAIL {plugin.name} selftest: tests package {plugin.tests_package!r} cannot be run: {exc}"
            )
            code = 1
    return code


COMMANDS = {
    "report": cmd_report,
    "prices": cmd_prices,
    "doctor": cmd_doctor,
    "monitor": cmd_monitor,
    "daily": cmd_daily,
    "reconcile": cmd_reconcile,
    "install": cmd_install,
    "log": cmd_log,
    "selftest": cmd_selftest,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Parse, dispatch, translate errors to exit codes."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or (raw[0].startswith("-") and raw[0] not in ("-h", "--help", "--version")):
        raw = ["report", *raw]
    args = build_parser().parse_args(raw)
    try:
        return COMMANDS[args.command](args, Paths.from_env())
    except ToolError as exc:
        emit_err(str(exc))
        return exc.code
