"""Command-line surface: parse, dispatch, return an exit code. ``__main__.run()`` is the only ``sys.exit``."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import math
import os
import shutil
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .api import ApiError, Call, Engine, UsageImport, price_call
from .collect import Ssh, collect, parse_target
from .collectors.usage_log import LOG_COUNTERS, writable_segment
from .config import Instance, Paths, PriceBook, builtin_prices, load_config, load_pricebook
from .daily import Output, daily_reports, yesterday
from .doctor import doctor
from .errors import ConfigError, ToolError, UsageError
from .log import (
    MAX_LINE_BYTES,
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
from .monitor import history, monitor
from .onboarding import SETUP_GUIDE, START_HERE
from .ops import (
    INSTANCE_GROUPS,
    ReportRequest,
    build_report,
    init_config,
    init_instance,
    run_auto_check,
)
from .output import Emit
from .prices_check import (
    CheckResult,
    ModelCheck,
    apply_check,
    check_prices,
    exit_code,
    merged_registry_json,
    save_result,
)
from .process import self_command, transient_warning
from .providers.deepseek_export import import_export, store_path
from .reconcile import Reported, run_reconcile
from .render import plain, render_json, render_markdown, render_text
from .schedules import (
    DAILY_CADENCE,
    DAILY_JOB,
    Cadence,
    install_job,
    install_schedule,
    remove_job,
    remove_schedule,
)
from .timeutil import now, parse_cli_ts, zoned
from .values import checked_label


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
        "--session",
        help="one Claude Code session (an id, a unique prefix or a transcript path): only the rows stamped with it — "
        "its transcript and the work it launched, from every project — no subscription share; "
        "'latest' or 'all' name a period instead",
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
    report.add_argument("--group", help="real,api,vendor (default; real,api with --instance)")
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
        help="a model without a price and without a reported cost: fail (exit 5, the default) or skip it, counted "
        "(the default with --instance)",
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
    report.add_argument(
        "--where",
        action="append",
        default=[],
        metavar="LABEL=VALUE",
        help="keep only the rows whose label LABEL is exactly VALUE (repeatable: every pair must hold); rows "
        "without labels — every source's but an app's usage log — are left out, and so is the plan share",
    )
    report.add_argument(
        "--by", metavar="LABEL", help="the attribution group lists one line per value of the label LABEL"
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
    _add_counters(log)
    log.add_argument("--billing", choices=["api", "subscription"])
    for key in ("ref", "session", "branch", "pr", "source", "event-id"):
        log.add_argument(f"--{key}")
    log.add_argument("--origin-session", help="the session that launched this work")
    log.add_argument(
        "--origin-repo",
        metavar="OWNER/NAME",
        help="the repository the work belongs to (needed by --origin-pr)",
    )
    log.add_argument("--origin-pr", type=int, metavar="N", help="the pull request this work belongs to")
    log.add_argument("--run-id", help="this invocation's own id (one run can log many lines)")
    log.add_argument("--tag", action="append", default=[], metavar="TAG")
    log.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="an app's own label (tenant, feature, …), exact; repeatable — report --where / --by read it",
    )
    log.add_argument("--at", help="ISO-8601 timestamp of the request (default: now)")
    log.add_argument("--log", type=Path, help="the log file (default: AI_COST_USAGE_LOG or the XDG data dir)")


def _add_counters(parser: argparse.ArgumentParser) -> None:
    """The provider-native counters of one request, and what it was charged (``log`` and ``price``)."""
    for counter in _LOG_COUNTERS:
        parser.add_argument(f"--{counter.replace('_', '-')}", type=int, metavar="N")
    parser.add_argument("--cost", type=float, help="what the request was charged, USD")


def _price_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    price = sub.add_parser("price", help="the list price of one request (exit 5 when nothing prices it)")
    price.add_argument("--provider", required=True, help="pricebook provider id")
    price.add_argument("--model", required=True, help="model id as the API reports it")
    _add_counters(price)
    price.add_argument(
        "--at", help="ISO-8601 time of the request (default: now): the rates of that day and hour"
    )
    price.add_argument("--format", choices=["text", "json"], default="text")


def _instance_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    instance = sub.add_parser("instance", help="init: create an app installation's directory (docs/EMBED.md)")
    instance.add_argument("action", choices=["init"])
    instance.add_argument("dir", nargs="?", help="the directory (default: --instance)")


def _import_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    imp = sub.add_parser(
        "import", help="import a provider's usage export as its day report, or usage lines into an instance"
    )
    imp.add_argument(
        "kind",
        choices=sorted([*IMPORTERS, USAGE]),
        help="the provider whose export this is, or usage: a log stream's usage lines into --instance",
    )
    imp.add_argument(
        "path",
        type=Path,
        help="the export: its ZIP, or the directory / one of the two CSV files; for usage a file, or - for stdin",
    )
    imp.add_argument(
        "--captured",
        metavar="WHEN",
        help="when the export was taken (ISO-8601); required for CSV files, which carry no time of their own",
    )


def _collect_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    pull = sub.add_parser("collect", help="pull app instances' usage logs from other machines over ssh")
    pull.add_argument(
        "targets", nargs="+", metavar="HOST:PATH", help="a remote instance directory (repeatable)"
    )
    pull.add_argument("--into", required=True, metavar="DIR", help="one file per HOST:PATH is written here")
    pull.add_argument(
        "--timeout", type=int, default=15, metavar="SECONDS", help="ssh's connect timeout (default 15)"
    )


def _selftest_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    selftest = sub.add_parser("selftest", help="run the shipped tests without pytest")
    selftest.add_argument("-v", "--verbose", action="store_true", help="print every passing test too")


IMPORTERS = {"deepseek": import_export}  # the providers whose usage export is imported (no usage API)
USAGE = "usage"  # import's kind for usage lines into an instance (Engine.import_usage)
_LOG_COUNTERS = LOG_COUNTERS  # one list: the usage log's provider-native counters (+ the thoughts alias)
INSTANCE_ENV = "AI_COST_INSTANCE"
SSH_ENV = "AI_COST_SSH"  # another ssh executable for collect
# the commands that work on an instance; the rest read the machine's own files
ON_AN_INSTANCE = ("report", "log", "price", "doctor", "monitor", "instance", "prices show", "import usage")
_COMMANDS = (
    _report_command,
    _prices_command,
    _doctor_command,
    _monitor_command,
    _daily_command,
    _reconcile_command,
    _install_command,
    _log_command,
    _price_command,
    _instance_command,
    _import_command,
    _collect_command,
    _selftest_command,
)


def _instance_options(parser: argparse.ArgumentParser) -> None:
    """The global options of an instance: the same fields ``ai_cost.api.Instance`` takes."""
    parser.add_argument(
        "--instance",
        metavar="DIR",
        help=f"an app installation's directory: its usage, config, prices and state only (or {INSTANCE_ENV}); "
        "before the subcommand",
    )
    parser.add_argument(
        "--read-clis", action="store_true", help="with --instance: also read this machine's coding-CLI files"
    )
    parser.add_argument(
        "--outside-log",
        action="append",
        default=[],
        metavar="PATH",
        help="with --instance: a usage log outside it that its config may list (repeatable)",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        metavar="MODULE",
        help="with --instance: a plugin module the instance runs (repeatable; none by default)",
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
    _instance_options(parser)
    sub = parser.add_subparsers(dest="command", required=True)  # report is put in when none is named
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


def parse_pairs(specs: Sequence[str], flag: str) -> dict[str, str]:
    """``NAME=VALUE`` specs as a dict; a malformed spec or label name is a ``UsageError`` naming the flag."""
    pairs: dict[str, str] = {}
    for spec in specs:
        name, eq, value = spec.partition("=")
        if not eq:
            raise UsageError(f"{flag}: expected NAME=VALUE, got {spec!r}")
        checked_label(flag, name, value)
        if name in pairs:
            raise UsageError(f"{flag}: {name} given twice — a label has one value")
        pairs[name] = value
    return pairs


def _request(args: argparse.Namespace, paths: Paths) -> ReportRequest:
    """The report of the flags; on an instance the engine's defaults: every project, real and api, unpriced skipped."""
    engine = paths.instance is not None
    for flag, value in (("--group", args.group), ("--github", args.github)):
        if value is not None and not value.strip(", "):
            raise UsageError(f"{flag}: name at least one (an empty value is never the default)")
    groups = args.group or (",".join(INSTANCE_GROUPS) if engine else "real,api,vendor")
    return ReportRequest(
        session=args.session,
        project=args.project,
        all_projects=args.all_projects or (engine and not args.project and not args.session),
        since=args.since,
        until=args.until,
        hours=args.hours,
        settings=parse_settings(args.setting),
        github=tuple(g.strip() for g in (args.github or "").split(",") if g.strip()),
        groups=tuple(g.strip() for g in groups.split(",") if g.strip()),
        items=args.items,
        vendor_profile=args.vendor_profile,
        detail=args.detail,
        attribute=tuple(args.attribute),
        unpriced=args.unpriced or "",  # the run's default: ops.unpriced_rule
        where=parse_pairs(args.where, "--where"),
        by=_by(args.by),
    )


def _by(name: str | None) -> str:
    """``--by``: absent is no grouping; given, it names a label (an empty name is a ``UsageError``, never ignored)."""
    if name is None:
        return ""
    checked_label("--by", name)
    return name


def cmd_report(args: argparse.Namespace, paths: Paths) -> int:
    """Collect, group, render."""
    config, book = load_config(paths), load_pricebook(paths)
    if not args.no_auto_check:
        run_auto_check(paths, book, args.quiet, emit_err)
    report = build_report(_request(args, paths), paths, config, book)
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


_CHECK_FLAGS = {
    CheckStatus.CONFIRMED: "ok ",
    CheckStatus.CHANGED: "!! ",
    CheckStatus.NOT_FOUND: "?? ",
    CheckStatus.APPLIED: "++ ",
}


def _emit_check(model: str, check: ModelCheck, quiet: bool) -> None:
    """One model's line of `prices check`, and one for the rates its vendor lists apart that its row does not show.

    A changed line names what the row lacks; ``quiet`` prints only what changed or vanished.
    """
    if quiet and check.status is CheckStatus.CONFIRMED:
        return
    flag = _CHECK_FLAGS.get(check.status, "   ")
    lacks = f" — not in its row: {', '.join(check.missing)}" if check.missing else ""
    emit(f"   {flag}{model:<26} expected {check.expected} seen {check.seen[:12]}{lacks}")
    if check.unchecked:
        emit(f"   ~  {model:<26} listed apart from its row: {', '.join(check.unchecked)} — confirm by hand")


def _emit_unlisted(result: CheckResult) -> None:
    """The closing line of `prices check` when a vendor's page names models the price book has no row for."""
    unlisted = sum(len(prov.unlisted) for prov in result.providers.values())
    if unlisted:
        emit(
            f"\n{unlisted} model(s) on vendor pages without a price row: add the row to prices.json, or name the id under the provider's `no_row`."
        )


def cmd_prices(args: argparse.Namespace, paths: Paths) -> int:
    """Show / check / update."""
    if args.action == "show":
        return _show_prices(args, paths)
    _refuse_offline(paths, "price check")
    book = load_pricebook(paths)
    providers = [p.strip() for p in args.providers.split(",")] if args.providers else None
    try:
        result = check_prices(book, providers)
        applied = apply_check(result, book, paths.user_prices_file()) if args.action == "update" else []
        save_result(paths, result)
    finally:  # a check that raised (a price-book key that is no regex) must not hold the next one off
        with contextlib.suppress(OSError):  # never the error that hides the check's own
            (paths.state_dir / "prices-check.lock").unlink(missing_ok=True)
    unresolved = len(result.drift())
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
            _emit_check(model, check, args.quiet)
        for model in prov.unlisted:
            emit(f"   new {model:<26} on the vendor's page, not in the price book")
    for provider, model, new_in, new_out in applied:
        emit(f"applied {provider}/{model} → input {new_in} output {new_out} ({paths.user_prices_file()})")
    code = exit_code(unresolved)
    _emit_unlisted(result)
    if code:
        emit(
            f"\n{unresolved} suspected change(s) not applied. Verify on the vendor page, then `ai-cost prices update` or edit {paths.user_prices_file()}."
        )
    return code


def _refuse_offline(paths: Paths, what: str) -> None:
    """A command that uses the network is refused (exit 2) under ``AI_COST_OFFLINE``."""
    if paths.offline:
        raise ToolError(f"AI_COST_OFFLINE=1 — {what} disabled", code=2)


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


def _checked_usage(usage: Usage, cost: float | None, book: PriceBook, at: datetime) -> Usage:
    """The line as the reader will accept it on its day (``validated``), with ``--cost`` filled in; a UsageError."""
    if cost is not None and usage.cost is None:
        usage = Usage(usage.provider, usage.model, usage.tokens, cost)
    try:
        return validated(usage, book, at)
    except ValueError as exc:
        raise UsageError(f"log: {exc}") from exc


def _log_meta(args: argparse.Namespace) -> Attribution:
    """The optional keys of the line, one flag per ``Attribution`` field; a malformed value is a UsageError."""
    given = {f.name: getattr(args, f.name) for f in fields(Attribution) if f.name not in ("tags", "labels")}
    labels = parse_pairs(args.label, "--label")
    try:
        return Attribution(
            tags=tuple(args.tag),
            labels=labels,
            **{key: value for key, value in given.items() if value is not None},
        )
    except ValueError as exc:
        raise UsageError(f"log: {exc}") from exc


def cmd_log(args: argparse.Namespace, paths: Paths) -> int:
    """Append one usage line and echo it."""
    given = _at(args.at, paths)
    at = given or now()  # the line's day: what the reader prices it by
    usage = _checked_usage(_log_usage(args), args.cost, load_pricebook(paths), at)
    meta = _log_meta(args)
    line = log_entry(usage, at, meta)
    target = _log_target(args, paths, at)
    try:
        append_log(target, line, MAX_LINE_BYTES if paths.instance is not None else None)
    except OSError as exc:  # an unwritable log path is a ToolError, never a traceback
        raise ToolError(f"cannot write {target}: {exc}") from exc
    except ValueError as exc:  # a line too long for one atomic write
        raise UsageError(f"log: {exc}") from exc
    emit(json.dumps(line, ensure_ascii=False))
    if args.log and not _reports_read(Path(target), paths):
        emit_err(
            f"note: {target} is not a file reports read — add it to usage_logs in {paths.user_config_file()}"
        )
    return 0


def _log_target(args: argparse.Namespace, paths: Paths, at: datetime) -> Path:
    """``--log``, else the instance's segment of the line's month, else the default log of the ``Paths`` given."""
    if paths.instance is not None:
        if args.log:
            raise UsageError("log: --log with --instance — the instance's segment of the month is the file")
        try:
            return writable_segment(paths.instance, at)
        except ValueError as exc:
            raise ToolError(f"log: cannot write: {exc}") from exc
    # the Paths given, never the process env alone
    return Path(args.log or paths.usage_log or default_log_path())


def _at(text: str | None, paths: Paths) -> datetime | None:
    """``--at``: on an instance the time must say its zone, as the library's must; elsewhere as before (UTC)."""
    if paths.instance is not None:
        zoned(text, "--at")
    return parse_cli_ts(text, "--at")


def cmd_price(args: argparse.Namespace, paths: Paths) -> int:
    """The list price of one request; exit 5 when nothing prices it."""
    at = _at(args.at, paths)
    tokens = {name: getattr(args, name) for name in _LOG_COUNTERS if getattr(args, name) is not None}
    try:
        quote = price_call(
            Call(args.provider, args.model, tokens, args.cost, at), load_pricebook(paths), load_config(paths)
        )
    except ApiError as exc:
        raise UsageError(f"price: {exc}") from exc
    if args.format == "json":
        emit(json.dumps(plain(quote)))
    elif quote.usd is None:
        emit(f"no price — {quote.note}")
    else:
        emit(f"{quote.usd:.6f} USD — {quote.note}")
    return 0 if quote.usd is not None else 5


def cmd_instance(args: argparse.Namespace, paths: Paths) -> int:
    """``instance init``: the directory, its usage segments directory and a starting config; twice changes nothing."""
    given = paths.instance.root if paths.instance is not None else None
    named = (
        _instance(_user_path(args.dir, "instance init", "a directory")).root if args.dir is not None else None
    )
    if given is not None and named is not None and named != given:  # both absolute: ./app and app are one
        raise UsageError(f"instance init: {args.dir} and --instance {given} name two directories")
    root = named or given
    if root is None:
        raise UsageError("instance init: name the directory (ai-cost instance init DIR)")
    return init_instance(Instance(root), emit)


def _user_path(text: str, flag: str, kind: str) -> str:
    """A path the user typed, as the library takes it: absolute, so "." is the directory the command runs in.

    A blank name is refused (an unset variable never names this machine's files); a leading ``~`` is expanded as
    ``AI_COST_USAGE_LOG``'s is.
    """
    if not text.strip():
        raise UsageError(
            f"{flag}: name {kind} (an empty value is an unset variable, never this machine's files)"
        )
    return os.path.abspath(os.path.expanduser(text))


def _instance(root: str, **options: Any) -> Instance:
    """The instance ``root`` names, with the command's options; the library's refusal is the command's usage error."""
    try:
        return Instance(Path(root), **options)
    except ApiError as exc:
        raise UsageError(str(exc)) from exc


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


def cmd_collect(args: argparse.Namespace, paths: Paths) -> int:
    """Pull every remote instance's segments into one file each; exit 1 when any pull failed."""
    targets = [parse_target(spec) for spec in args.targets]  # every argument checked before the first ssh
    if args.timeout < 1:
        raise UsageError(f"--timeout: whole seconds, at least 1 (got {args.timeout})")
    into = Path(_user_path(args.into, "--into", "a directory"))
    _refuse_offline(paths, "collect")
    program = os.environ.get(SSH_ENV, "") or "ssh"
    if shutil.which(program) is None:
        raise ToolError(f"collect: {program} is not an executable on PATH (set {SSH_ENV} to another)")
    return collect(Ssh(program, args.timeout), targets, into, emit)


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


def cmd_import(args: argparse.Namespace, paths: Paths) -> int:
    """Check an export, merge its days into the provider's store, say what changed and which days to rebuild."""
    if args.kind == USAGE:
        return _import_usage(args, paths)
    captured = parse_cli_ts(args.captured, "--captured") if args.captured else None
    result = IMPORTERS[args.kind](args.path, paths.state_dir, captured)
    emit(
        f"imported {args.kind}: {len(result.added)} day(s) added, {len(result.replaced)} replaced, "
        f"{result.kept} kept ({store_path(paths.state_dir)})"
    )
    if result.dates:
        dates = ", ".join(d.isoformat() for d in result.dates)
        emit(f"daily files that can use these days: ai-cost daily --date <date> for {dates}")
    elif result.added or result.replaced:
        emit(
            "these days are not UTC days: no daily file holds one whole — compare them with report --since/--until"
        )
    return 0


def _import_usage(args: argparse.Namespace, paths: Paths) -> int:
    """Usage lines into the instance; exit 1 when a usage line was skipped (the good ones are still imported)."""
    if paths.instance is None:
        raise UsageError(f"import usage needs an instance: give --instance DIR (or {INSTANCE_ENV})")
    if args.captured:
        raise UsageError(
            "import usage: --captured is for a provider's export; a usage line carries its own at"
        )
    try:
        engine = Engine(paths.instance)
    except ApiError as exc:  # its config or prices do not parse: exit 2, as every other command says it
        raise ConfigError(f"import usage: {exc}") from exc
    try:
        with _stream(args.path) as lines:
            result = engine.import_usage(lines)
    except ApiError as exc:
        raise ToolError(f"import usage: {exc}") from exc
    for said in _import_said(result):
        emit(said)
    return 1 if result.failed else 0


_STREAM_ENCODING = "utf-8-sig"  # a log stream's text, a byte-order mark at its start dropped, either door


@contextlib.contextmanager
def _stream(path: Path) -> Iterator[Iterable[str]]:
    """The lines of ``path``, or of stdin for ``-``, one at a time as UTF-8 (a bad byte replaced, never fatal).

    A file that cannot be opened is exit 2.
    """
    if str(path) == "-":
        wrapper = io.TextIOWrapper(sys.stdin.buffer, encoding=_STREAM_ENCODING, errors="replace")
        try:
            yield _read(wrapper, "stdin")
        finally:
            wrapper.detach()  # stdin stays open for the process
        return
    try:
        handle = path.open(encoding=_STREAM_ENCODING, errors="replace")
    except OSError as exc:
        raise UsageError(_cannot_read(str(path), exc)) from exc
    with handle:
        yield _read(handle, str(path))


def _read(lines: Iterable[str], name: str) -> Iterator[str]:
    """The lines one at a time; a read that fails midway is exit 2 — what was written stays, a replay says so."""
    try:
        yield from lines
    except OSError as exc:
        raise UsageError(_cannot_read(name, exc)) from exc


def _cannot_read(name: str, exc: OSError) -> str:
    return f"import usage: cannot read {name}: {exc.strerror or exc}"


def _import_said(result: UsageImport) -> list[str]:
    """``imported N`` always; then each count that is not zero, a skipped class with its first line."""
    said = [_imported(result)]
    if result.already_there:
        said.append(f"already there {result.already_there}")
    for reason, lines in result.skipped.items():
        example = result.examples.get(reason)
        words = f"skipped {lines} — {reason.value}"
        said.append(f"{words}: {example}" if example else words)
    return said


def _imported(result: UsageImport) -> str:
    """``imported N``; a stream with lines but not one usage line says so, since its exit stays 0."""
    if result.imported or result.already_there or result.failed or not result.skipped:
        return f"imported {result.imported}"
    return f"imported 0 — no usage line found in {sum(result.skipped.values())} lines"


COMMANDS = {
    "report": cmd_report,
    "prices": cmd_prices,
    "doctor": cmd_doctor,
    "monitor": cmd_monitor,
    "daily": cmd_daily,
    "reconcile": cmd_reconcile,
    "install": cmd_install,
    "log": cmd_log,
    "price": cmd_price,
    "instance": cmd_instance,
    "import": cmd_import,
    "collect": cmd_collect,
    "selftest": cmd_selftest,
}


_WITH_VALUE = ("--instance", "--outside-log", "--plugin")
_FLAGS = ("--read-clis",)


def _global_head(raw: Sequence[str]) -> tuple[int, bool]:
    """How many leading tokens are global options, and whether the last of them has its value."""
    index = 0
    while index < len(raw):
        token = raw[index]
        if token in _WITH_VALUE:
            if index + 1 >= len(raw) or _not_a_value(raw[index + 1]):
                return index, False
            index += 2
        elif token in _FLAGS or token.startswith(tuple(f"{name}=" for name in _WITH_VALUE)):
            index += 1
        else:
            break
    return index, True


def _not_a_value(token: str) -> bool:
    """A token a global option never takes as its value: a subcommand's name or another option (``./log`` is one)."""
    return token in COMMANDS or token.startswith("-")


def _with_command(raw: list[str]) -> list[str]:
    """``report`` is the default subcommand: put after the global options when no subcommand is named.

    A global option without its value is left for argparse to refuse, never given ``report`` as its value.
    """
    head, complete = _global_head(raw)
    rest = raw[head:]
    if not complete:
        return raw
    if not rest or (rest[0].startswith("-") and rest[0] not in ("-h", "--help", "--version")):
        return [*raw[:head], "report", *rest]
    return raw


def _on_an_instance(args: argparse.Namespace) -> bool:
    """Whether the command, by its name as typed (``prices show``, ``import usage``), works on an instance."""
    return _command_name(args) in ON_AN_INSTANCE


def _paths(args: argparse.Namespace) -> Paths:
    """The user's paths from the environment, or every path of the instance ``--instance`` / the variable names."""
    root = _instance_text(args)
    if root is None:
        options = (
            ("--read-clis", args.read_clis),
            ("--outside-log", args.outside_log),
            ("--plugin", args.plugin),
        )
        given = [flag for flag, value in options if value]
        if given:
            raise UsageError(f"{given[0]} belongs to an instance: give --instance DIR")
        return Paths.from_env()
    if not _on_an_instance(args):
        name = _command_name(args)
        raise UsageError(
            f"{name} works on this machine's own files, never an instance "
            f"(--instance / {INSTANCE_ENV} is for {', '.join(ON_AN_INSTANCE)})"
        )
    outside = tuple(Path(_user_path(path, "--outside-log", "a usage log file")) for path in args.outside_log)
    instance = _instance(root, read_clis=args.read_clis, plugins=tuple(args.plugin), outside_logs=outside)
    return Paths.for_instance(instance)


_SECOND_WORD = {"prices": "action", "import": "kind"}  # the commands named by two words


def _command_name(args: argparse.Namespace) -> str:
    """The command as typed: ``prices check``, ``import deepseek``, ``daily``."""
    second = _SECOND_WORD.get(args.command)
    return f"{args.command} {getattr(args, second)}" if second else str(args.command)


def _instance_text(args: argparse.Namespace) -> str | None:
    """The instance directory ``--instance`` or the variable names, absolute; an empty variable is an unset one."""
    if args.instance is not None:
        return _user_path(args.instance, "--instance", "a directory")
    named = os.environ.get(INSTANCE_ENV, "")
    return _user_path(named, INSTANCE_ENV, "a directory") if named else None


def main(argv: Sequence[str] | None = None) -> int:
    """Parse, dispatch, translate errors to exit codes."""
    raw = _with_command(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(raw)  # _with_command always names one: report after the global options
    try:
        return COMMANDS[args.command](args, _paths(args))
    except ToolError as exc:
        emit_err(str(exc))
        return exc.code
