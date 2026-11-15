"""``ai-cost --instance DIR …``: the engine through the command, the same JSON, the commands that refuse an instance."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest import mock

from .. import api, cli
from ..timeutil import iso
from .engine_fixtures import WHOLE, doctor_on, engine, leaky_home, log_line, plugin_module
from .fixtures import BASE, run_cli

AT = iso(BASE + timedelta(minutes=10))


def _instance(tmp: Path, *argv: str) -> tuple[int, str, str]:
    return run_cli(["--instance", str(tmp / "app"), *argv])


def _users_files(tmp: Path) -> dict[str, str]:
    """The machine user's own files, under ``tmp``: the developer's config and prices never decide a test."""
    return {
        "AI_COST_USAGE_LOG": str(tmp / "user.jsonl"),
        "AI_COST_CONFIG_DIR": str(tmp / "cfg"),
        "AI_COST_CONFIG": str(tmp / "cfg" / "config.json"),
        "AI_COST_PRICES": str(tmp / "cfg" / "prices.json"),
        "AI_COST_STATE_DIR": str(tmp / "state"),
    }


def _log(tmp: Path, *extra: str) -> tuple[int, str, str]:
    return _instance(
        tmp, "log", "--provider", "openai", "--model", "gpt-6-sol", "--input", "1000", "--at", AT, *extra
    )


def test_instance_init_twice_changes_nothing(tmp_path: Path) -> None:
    code, out, _ = run_cli(["instance", "init", str(tmp_path / "app")])
    config = (tmp_path / "app" / "config.json").read_text("utf-8")
    (tmp_path / "app" / "config.json").write_text(
        config.replace('"subscriptions": []', '"subscriptions": [ ]')
    )
    again, out_again, _ = run_cli(["--instance", str(tmp_path / "app"), "instance", "init"])
    assert code == again == 0 and "(written)" in out and "(kept)" in out_again, out_again
    assert '"subscriptions": [ ]' in (tmp_path / "app" / "config.json").read_text(
        "utf-8"
    ), "an edited config stays"
    assert (tmp_path / "app" / "usage").is_dir() and (tmp_path / "app" / "state").is_dir()


def test_log_writes_the_months_segment_with_labels(tmp_path: Path) -> None:
    code, out, _ = _log(tmp_path, "--label", "tenant=acme", "--label", "feature=chat")
    segment = tmp_path / "app" / "usage" / "2026-09.jsonl"
    assert code == 0 and json.loads(out)["labels"] == {"tenant": "acme", "feature": "chat"}
    assert json.loads(segment.read_text("utf-8")) == json.loads(out)


def test_log_refuses_a_log_file_beside_an_instance(tmp_path: Path) -> None:
    code, _, err = _log(tmp_path, "--log", str(tmp_path / "other.jsonl"))
    assert code == 2 and "--log with --instance" in err, err


def test_price_prints_the_engines_price_as_json(tmp_path: Path) -> None:
    code, out, _ = _instance(
        tmp_path,
        "price",
        "--provider",
        "openai",
        "--model",
        "gpt-6-sol",
        "--input",
        "1000",
        "--at",
        AT,
        "--format",
        "json",
    )
    expected = engine(tmp_path / "app").price(api.Call("openai", "gpt-6-sol", {"input": 1000}, at=BASE))
    assert code == 0 and json.loads(out) == {"usd": expected.usd, "note": expected.note}


def test_price_of_an_unpriced_model_exits_5(tmp_path: Path) -> None:
    code, out, _ = _instance(
        tmp_path, "price", "--provider", "openai", "--model", "gpt-nowhere", "--input", "5"
    )
    assert code == 5 and out.startswith("no price"), out


def test_report_json_is_the_engines_json_byte_for_byte(tmp_path: Path) -> None:
    app = engine(tmp_path / "app")
    app.record(api.Call("openai", "gpt-6-sol", {"input": 1000}, at=BASE, labels={"tenant": "acme"}))
    app.record(
        api.Call("google", "gemini-3.8-flash", {"prompt": 500, "output": 20}, at=BASE, labels={"tenant": "b"})
    )
    with (tmp_path / "app" / "usage" / "2026-09.jsonl").open("a", encoding="utf-8") as segment:
        segment.write(log_line(BASE, model="gpt-nowhere") + "\n")  # unpriced: the engine skips it, counted
    window = ["--since", iso(WHOLE.since), "--until", iso(WHOLE.until)]
    with mock.patch("ai_cost.ops.now", return_value=BASE + timedelta(days=3)):
        library = app.report_json(WHOLE, by="tenant", detail=True)
        code, command, _ = _instance(
            tmp_path, "report", *window, "--by", "tenant", "--detail", "--format", "json", "--no-auto-check"
        )
    assert code == 0 and command == library, "the command's defaults on an instance are the engine's"
    assert len(json.loads(library)["skipped"]) == 1
    assert json.loads(library)["generated_at"] == iso(BASE + timedelta(days=3))


def test_report_where_keeps_the_exact_label(tmp_path: Path) -> None:
    _log(tmp_path, "--label", "tenant=acme")
    _log(tmp_path, "--label", "tenant=Acme")
    window = ["--since", iso(WHOLE.since), "--until", iso(WHOLE.until)]
    code, out, _ = _instance(tmp_path, "report", *window, "--where", "tenant=acme", "--format", "json")
    assert code == 0 and json.loads(out)["row_count"] == 1


def test_an_instance_report_from_the_command_reads_only_the_instance(tmp_path: Path) -> None:
    _log(tmp_path, "--ref", "app-call")
    window = ["--since", iso(WHOLE.since), "--until", iso(WHOLE.until)]
    with leaky_home(tmp_path, "leak_cli"):
        code, out, _ = _instance(
            tmp_path, "report", *window, "--detail", "--format", "json", "--no-auto-check"
        )
        clis, with_clis, _ = run_cli(
            [
                "--instance",
                str(tmp_path / "app"),
                "--read-clis",
                "report",
                *window,
                "--detail",
                "--format",
                "json",
                "--no-auto-check",
                "--unpriced",
                "skip",
            ]
        )
    assert code == 0 and [row["ref"] for row in json.loads(out)["rows"]] == ["app-call"]
    refs = [row["ref"] for row in json.loads(with_clis)["rows"]]
    assert clis == 0 and "app-call" in refs and "plugin-row" not in refs and len(refs) > 1, refs


def test_doctor_on_an_instance_names_no_machine_job(tmp_path: Path) -> None:
    _log(tmp_path)
    with doctor_on():
        code, out, _ = _instance(tmp_path, "doctor")
    assert code == 0 and "scheduled" not in out and "no-cli" not in out, out
    assert str(tmp_path / "app" / "usage" / "2026-09.jsonl") in out


def test_monitor_keeps_its_history_in_the_instance(tmp_path: Path) -> None:
    _log(tmp_path)
    code, _, _ = _instance(tmp_path, "monitor", "--hours", "1", "--append")
    assert code == 0 and (tmp_path / "app" / "state" / "history.jsonl").is_file()


def test_the_default_command_follows_the_global_options(tmp_path: Path) -> None:
    code, out, _ = _instance(tmp_path, "--hours", "1", "--format", "json", "--no-auto-check")
    assert code == 0 and json.loads(out)["row_count"] == 0


def test_commands_on_the_machines_own_files_refuse_an_instance(tmp_path: Path) -> None:
    refused = (
        ["install", "--init-config"],
        ["daily"],
        ["prices", "check"],
        ["prices", "update"],
        ["reconcile", "--provider", "xai", "--usd", "1"],
        ["import", "deepseek", str(tmp_path / "export.zip")],
        ["selftest"],
        ["collect", "box:app", "--into", str(tmp_path / "into")],
    )
    with mock.patch.dict(os.environ, {"AI_COST_SSH": str(tmp_path / "no-ssh")}):  # collect never reaches ssh
        for argv in refused:
            code, out, err = _instance(tmp_path, *argv)
            assert code == 2 and out == "" and "never an instance" in err and err.count("\n") == 1, (
                argv,
                err,
            )
    assert not (tmp_path / "app").exists()


def test_the_instance_can_come_from_the_environment(tmp_path: Path) -> None:
    with mock.patch.dict(os.environ, {cli.INSTANCE_ENV: str(tmp_path / "app")}):
        code, _, _ = run_cli(
            ["log", "--provider", "openai", "--model", "gpt-6-sol", "--input", "1", "--at", AT]
        )
        refused, _, err = run_cli(["daily"])
    assert code == 0 and (tmp_path / "app" / "usage" / "2026-09.jsonl").is_file()
    assert refused == 2 and cli.INSTANCE_ENV in err


def test_read_clis_without_an_instance_is_a_usage_error() -> None:
    code, _, err = run_cli(["--read-clis", "report"])
    assert code == 2 and "give --instance" in err, err


def test_an_instance_report_never_asks_github(tmp_path: Path) -> None:
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\ntouch {tmp_path / 'gh-ran'}\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    with mock.patch.dict(os.environ, {"PATH": f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}"}):
        code, out, err = _instance(
            tmp_path, "report", "--hours", "1", "--github", "foo/bar", "--no-auto-check"
        )
    assert code == 2 and out == "" and "--github" in err and err.count("\n") == 1, err
    assert not (tmp_path / "gh-ran").exists(), "gh was run"


def test_a_global_option_without_its_value_is_argparses_usage_error(tmp_path: Path) -> None:
    for argv in (["--instance"], ["--instance", str(tmp_path), "--plugin"]):
        try:
            run_cli(argv)
        except SystemExit as exc:
            assert exc.code == 2, argv
        else:
            raise AssertionError(f"{argv} ran")


def test_the_instance_doctor_names_only_instance_remedies(tmp_path: Path) -> None:
    code, out, _ = _instance(tmp_path, "doctor")
    root = tmp_path / "app"
    assert code == 1 and f"!!  instance {root} does not exist yet — ai-cost instance init {root}" in out, out
    run_cli(["instance", "init", str(root)])
    (root / "config.json").unlink()
    _, out, _ = _instance(tmp_path, "doctor")
    assert f"absent → defaults (ai-cost instance init {root} writes it)" in out, out
    for machine in ("install --init-config", "CLAUDE_CONFIG_DIR", "AI_COST_", "schedule"):
        assert machine not in out, (machine, out)


def test_the_command_names_outside_logs_and_plugins_like_the_library(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text(log_line(BASE + timedelta(minutes=10), ref="outside") + "\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.json").write_text(json.dumps({"usage_logs": [str(outside)]}), "utf-8")
    window = ["--since", iso(WHOLE.since), "--until", iso(WHOLE.until)]
    code, _, err = _instance(tmp_path, "report", *window, "--format", "json")
    assert code == 2 and "--outside-log PATH" in err and "Instance.outside_logs" in err, err
    argv = ["--instance", str(tmp_path / "app"), "--outside-log", str(outside), "--plugin", "leak_named_cli"]
    with plugin_module(tmp_path, "leak_named_cli"):
        code, out, _ = run_cli([*argv, "report", *window, "--detail", "--format", "json", "--no-auto-check"])
    assert str(tmp_path) not in sys.path, "the plugin's folder leaves the path with the fixture"
    assert code == 0 and sorted(row["ref"] for row in json.loads(out)["rows"]) == ["outside", "plugin-row"]
    refused, _, err = run_cli(["--plugin", "leak_named_cli", "report"])
    assert refused == 2 and "give --instance" in err


def test_a_time_without_a_zone_is_refused_on_an_instance_only(tmp_path: Path) -> None:
    code, _, err = _log(tmp_path, "--at", "2026-11-16T02:30:00")
    assert code == 2 and "give the zone" in err, err
    _log(tmp_path)  # the fixture's --at carries its Z
    with mock.patch.dict(os.environ, _users_files(tmp_path)):
        plain, out, _ = run_cli(
            [
                "log",
                "--provider",
                "openai",
                "--model",
                "gpt-6-sol",
                "--input",
                "1",
                "--at",
                "2026-11-16T02:30:00",
            ]
        )
    assert (
        plain == 0 and json.loads(out)["at"] == "2026-11-16T02:30:00Z"
    ), "the user's log keeps reading it as UTC"


def test_a_label_or_filter_named_twice_is_refused(tmp_path: Path) -> None:
    code, _, err = _log(tmp_path, "--label", "tenant=a", "--label", "tenant=b")
    assert code == 2 and "tenant given twice" in err, err
    code, _, err = _instance(tmp_path, "report", "--where", "t=a", "--where", "t=b", "--no-auto-check")
    assert code == 2 and "t given twice" in err, err


def test_the_users_log_takes_a_long_line_an_instance_refuses(tmp_path: Path) -> None:
    long_ref = ["--ref", "x" * 5000]
    with mock.patch.dict(os.environ, _users_files(tmp_path)):
        plain, _, _ = run_cli(
            ["log", "--provider", "openai", "--model", "gpt-6-sol", "--input", "1", *long_ref]
        )
    assert plain == 0 and len((tmp_path / "user.jsonl").read_bytes()) > 5000
    code, _, err = _log(tmp_path, *long_ref)
    assert code == 2 and "more than 4096" in err, err


def _log_argv() -> list[str]:
    return ["log", "--provider", "openai", "--model", "gpt-6-sol", "--input", "1000", "--at", AT]


def test_an_empty_instance_option_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    for variable in (str(tmp_path / "other"), None):
        with mock.patch.dict(os.environ, _users_files(tmp_path)):
            os.environ.pop(cli.INSTANCE_ENV, None)
            if variable is not None:
                os.environ[cli.INSTANCE_ENV] = variable
            code, _, err = run_cli(["--instance", "", *_log_argv()])
        assert code == 2 and "--instance: name a directory" in err, err
        assert not (tmp_path / "user.jsonl").exists() and not (tmp_path / "other").exists()


def test_an_empty_instance_variable_is_an_unset_one(tmp_path: Path) -> None:
    with mock.patch.dict(os.environ, {**_users_files(tmp_path), cli.INSTANCE_ENV: ""}):
        code, _, err = run_cli(_log_argv())
    assert code == 0 and (tmp_path / "user.jsonl").is_file(), err


def test_log_refuses_a_segment_that_links_out_of_the_instance(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text("kept\n", "utf-8")
    (tmp_path / "app" / "usage").mkdir(parents=True)
    (tmp_path / "app" / "usage" / "2026-09.jsonl").symlink_to(outside)
    code, _, err = _log(tmp_path)
    assert code != 0 and "log: cannot write" in err and "leads outside the instance" in err, err
    assert outside.read_text("utf-8") == "kept\n"


def test_monitor_on_an_instance_skips_an_unpriced_model_as_the_report_does(tmp_path: Path) -> None:
    _instance(tmp_path, "instance", "init")
    segment = tmp_path / "app" / "usage" / "2026-09.jsonl"
    segment.write_text(log_line(BASE + timedelta(minutes=10), model="no-such-model") + "\n", "utf-8")
    window = ["--since", iso(BASE), "--until", iso(BASE + timedelta(hours=1))]
    code, out, err = _instance(tmp_path, "monitor", *window)
    assert code == 0 and json.loads(out)["api_usd"] == 0, err


@contextmanager
def _working_in(folder: Path) -> Iterator[None]:
    """The command run with ``folder`` as its working directory."""
    here = os.getcwd()
    os.chdir(folder)
    try:
        yield
    finally:
        os.chdir(here)


def test_instance_init_with_a_relative_directory_beside_the_option(tmp_path: Path) -> None:
    with _working_in(tmp_path):
        code, out, err = run_cli(["--instance", "./app", "instance", "init", "app"])
    assert code == 0 and (tmp_path / "app" / "config.json").is_file(), err + out


def test_dot_names_the_working_directory_in_every_spelling_of_the_command(tmp_path: Path) -> None:
    for folder in ("by-option", "by-variable", "by-init"):
        (tmp_path / folder).mkdir()
    with mock.patch.dict(os.environ, _users_files(tmp_path)):
        os.environ.pop(cli.INSTANCE_ENV, None)
        with _working_in(tmp_path / "by-option"):
            by_option, _, err = run_cli(["--instance", ".", *_log_argv()])
        with _working_in(tmp_path / "by-init"):
            by_init, _, _ = run_cli(["instance", "init", "."])
        os.environ[cli.INSTANCE_ENV] = "."
        with _working_in(tmp_path / "by-variable"):
            by_variable, out, _ = run_cli(
                ["report", "--since", iso(BASE), "--until", iso(BASE + timedelta(hours=1))]
            )
    assert by_option == by_init == by_variable == 0, err + out
    assert (tmp_path / "by-option" / "usage" / "2026-09.jsonl").is_file()
    assert (tmp_path / "by-init" / "config.json").is_file()
    assert not (tmp_path / "user.jsonl").exists(), "the machine's log is never the instance"


def test_a_blank_name_is_refused_and_a_home_name_is_expanded(tmp_path: Path) -> None:
    for argv in (
        ["--instance", " ", *_log_argv()],
        ["instance", "init", ""],
        ["--outside-log", "", "--instance", str(tmp_path / "app"), "doctor"],
    ):
        code, _, err = run_cli(argv)
        assert code == 2 and "name " in err, (argv, err)
    with mock.patch.dict(os.environ, {cli.INSTANCE_ENV: " "}):
        code, _, err = run_cli(_log_argv())
    assert code == 2 and cli.INSTANCE_ENV in err, err
    with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
        code, _, err = run_cli(["--instance", "~/home-app", *_log_argv()])
    assert code == 0 and (tmp_path / "home-app" / "usage" / "2026-09.jsonl").is_file(), err


def test_an_instance_takes_no_window_time_without_its_zone(tmp_path: Path) -> None:
    for command in ("report", "monitor"):
        code, _, err = _instance(tmp_path, command, "--since", "2026-01-01T00:00:00")
        assert code == 2 and "--since '2026-01-01T00:00:00': give the zone" in err, (command, err)
    code, _, err = _instance(tmp_path, "report", "--since", iso(BASE), "--until", "2026-09-21")
    assert code == 2 and "--until" in err, err


def test_the_instance_doctor_names_an_outside_segment_as_a_problem_and_never_reads_its_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text(log_line(BASE) + "\n", "utf-8")
    run_cli(["instance", "init", str(tmp_path / "app")])
    (tmp_path / "app" / "usage" / "2026-09.jsonl").symlink_to(outside)
    with doctor_on():
        code, out, _ = _instance(tmp_path, "doctor")
    segment = tmp_path / "app" / "usage" / "2026-09.jsonl"
    assert code == 1 and f"!!  {segment}: a link that leads outside the instance, not read" in out, out
    assert f"usage log {segment}" not in out and str(outside) not in out, out


def test_a_blank_window_time_is_refused_and_every_zoned_spelling_taken(tmp_path: Path) -> None:
    for blank in ("", "  "):
        code, _, err = _instance(tmp_path, "report", "--since", blank, "--until", iso(BASE))
        assert code == 2 and "--since: give a time" in err, (blank, err)
    for since in ("1789000000", iso(BASE)):
        code, _, err = _instance(tmp_path, "report", "--since", since, "--until", "now", "--no-auto-check")
        assert code == 0, (since, err)


def test_a_global_option_never_takes_a_subcommand_or_an_option_as_its_value(tmp_path: Path) -> None:
    for argv in (["--instance", "log"], ["--instance", "--read-clis"], ["--outside-log", "report"]):
        with _working_in(tmp_path):
            try:
                run_cli(argv)
            except SystemExit as exc:
                assert exc.code == 2, argv
            else:
                raise AssertionError(f"{argv} ran")
    assert list(tmp_path.iterdir()) == [], "no directory named like a subcommand was made"
    with _working_in(tmp_path):
        code, _, err = run_cli(["--instance", "./log", "instance", "init"])
    assert code == 0 and (tmp_path / "log" / "config.json").is_file(), err


def test_an_empty_group_or_github_is_refused_never_the_default(tmp_path: Path) -> None:
    for flag, value in (("--group", ""), ("--group", ","), ("--github", " ")):
        code, _, err = _instance(tmp_path, "report", flag, value)
        assert code == 2 and f"{flag}: name at least one" in err, (flag, value, err)


def test_a_session_transcript_is_read_on_an_instance_only_with_read_clis(tmp_path: Path) -> None:
    from .fixtures import paths_in, write_claude_session

    transcript = write_claude_session(paths_in(tmp_path / "outside"), tmp_path / "proj")
    run_cli(["instance", "init", str(tmp_path / "app")])
    asked = ["report", "--session", str(transcript), "--format", "json", "--no-auto-check"]
    code, out, err = _instance(tmp_path, *asked)
    assert (
        code == 2 and out == "" and "--session reads a coding-CLI transcript" in err and "--read-clis" in err
    ), err
    code, out, err = run_cli(["--instance", str(tmp_path / "app"), "--read-clis", *asked])
    assert code == 0 and json.loads(out)["row_count"] > 0, err


def test_instance_init_keeps_a_config_that_appears_after_the_check(tmp_path: Path) -> None:
    from .. import ops

    config = tmp_path / "app" / "config.json"
    config.parent.mkdir()
    config.write_text('{"mine": true}\n', "utf-8")
    lines: list[str] = []
    with mock.patch.object(Path, "exists", return_value=False):  # a check that would say "absent"
        code = ops.init_instance(api.Instance(tmp_path / "app"), lines.append)
    assert code == 0 and config.read_text("utf-8") == '{"mine": true}\n' and "(kept)" in lines[0], lines
