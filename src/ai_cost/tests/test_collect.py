"""``ai-cost collect HOST:PATH … --into DIR``: remote instances' segments pulled over a fake ssh into one file each.

The fake ssh records its argv as one JSON line and runs the remote command with the local ``sh`` in a directory that
stands for the remote home; the host ``down`` fails as an unreachable one does.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from ..collect import Outcome, Ssh, Target, pull
from ..collectors.usage_log import SEGMENT_NAME
from .fixtures import run_cli

FAKE_SSH = """#!{python}
import json, os, subprocess, sys
with open(os.environ["FAKE_SSH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv) + "\\n")
if sys.argv[-2] == "down":
    sys.stderr.write("ssh: connect to host down port 22: Connection refused\\nsecond line\\n")
    sys.exit(255)
env = dict(os.environ, PATH=os.environ.get("FAKE_SSH_REMOTE_PATH", os.environ["PATH"]))
sys.exit(subprocess.run(sys.argv[-1], shell=True, cwd=os.environ["FAKE_SSH_HOME"], env=env).returncode)
"""


@contextmanager
def fake_ssh(tmp: Path, name: str = "ssh") -> Iterator[Path]:
    """A fake ``name`` first on PATH, the remote home ``tmp/remote``; yields the argv log."""
    bin_dir, home, log = tmp / "bin", tmp / "remote", tmp / "ssh-argv.jsonl"
    bin_dir.mkdir()
    home.mkdir(exist_ok=True)
    program = bin_dir / name
    program.write_text(FAKE_SSH.format(python=sys.executable))
    program.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "FAKE_SSH_LOG": str(log)}
    with mock.patch.dict(os.environ, {**env, "FAKE_SSH_HOME": str(home)}):
        os.environ.pop("AI_COST_SSH", None)
        yield log


def _segments(tmp: Path, root: str, files: dict[str, str]) -> None:
    usage = tmp / "remote" / root / "usage"
    usage.mkdir(parents=True)
    for name, text in files.items():
        (usage / name).write_text(text)


def _argvs(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _pulled(tmp: Path, host: str, path: str) -> str:
    return (tmp / "into" / Target(host, path).file_name).read_text()


def _collect(tmp: Path, *argv: str) -> tuple[int, str, str]:
    return run_cli(["collect", "--into", str(tmp / "into"), *argv])


def test_a_pull_writes_the_segments_concatenated_in_name_order(tmp_path: Path) -> None:
    month_named = {
        "2026-10.jsonl": "b\n",
        "2025-12.jsonl": "a\n",
        "2026-13.jsonl": "x\n",
        "2026-00.jsonl": "x\n",
    }
    _segments(tmp_path, "app", {**month_named, "notes.jsonl": "x\n", "2026-11.jsonl.tmp": "x\n"})
    with fake_ssh(tmp_path):
        code, out, _ = _collect(tmp_path, "box:app")
    assert code == 0 and _pulled(tmp_path, "box", "app") == "a\nb\n"
    assert out.count("\n") == 1 and ": 2 lines (4 bytes)" in out


def test_the_ssh_call_is_batch_mode_with_the_timeout_and_a_quoted_path(tmp_path: Path) -> None:
    path = "my app; touch pwned"
    _segments(tmp_path, path, {"2026-11.jsonl": "a\n"})
    with fake_ssh(tmp_path) as log:
        code, _, _ = _collect(tmp_path, f"user@box:{path}", "--timeout", "7")
    (argv,) = _argvs(log)
    options = ["BatchMode=yes", "ConnectTimeout=7", "ServerAliveInterval=15", "ServerAliveCountMax=3"]
    assert argv[1:-2] == [word for option in options for word in ("-o", option)] and argv[-2] == "user@box"
    assert shlex.split(argv[-1])[-1] == path
    assert code == 0 and _pulled(tmp_path, "user@box", path) == "a\n"
    assert not (tmp_path / "remote" / "pwned").exists()


def test_a_leading_tilde_is_the_remote_home(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-11.jsonl": "a\n"})
    with fake_ssh(tmp_path) as log:
        code, _, _ = _collect(tmp_path, "box:~/app")
    assert code == 0 and shlex.split(_argvs(log)[0][-1])[-1] == "app"
    assert _pulled(tmp_path, "box", "~/app") == "a\n"


def test_two_instances_on_one_host_give_two_files(tmp_path: Path) -> None:
    _segments(tmp_path, "one", {"2026-11.jsonl": "1\n"})
    _segments(tmp_path, "two", {"2026-11.jsonl": "2\n"})
    with fake_ssh(tmp_path):
        code, _, _ = _collect(tmp_path, "box:one", "box:two")
    assert code == 0 and len(list((tmp_path / "into").iterdir())) == 2
    assert (_pulled(tmp_path, "box", "one"), _pulled(tmp_path, "box", "two")) == ("1\n", "2\n")


def test_a_failed_host_keeps_its_file_and_the_others_are_pulled(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-11.jsonl": "new\n"})
    (tmp_path / "into").mkdir()
    (tmp_path / "into" / Target("down", "app").file_name).write_text("old\n")
    with fake_ssh(tmp_path):
        code, out, _ = _collect(tmp_path, "down:app", "box:app")
    assert (
        code == 1
        and _pulled(tmp_path, "down", "app") == "old\n"
        and _pulled(tmp_path, "box", "app") == "new\n"
    )
    assert "ssh exit 255): ssh: connect to host down port 22: Connection refused —" in out
    assert "second line" not in out and out.splitlines()[-1] == "1 of 2 pulls failed"


def test_a_malformed_argument_exits_2_and_runs_no_ssh(tmp_path: Path) -> None:
    with fake_ssh(tmp_path) as log:
        for spec in (
            "nocolon",
            ":app",
            "box:",
            "-oProxyCommand=x:app",
        ):  # after --: argparse leaves it a target
            code, out, err = _collect(tmp_path, "--", "box:app", spec)
            assert code == 2 and out == "" and "collect:" in err, spec
        refused, _, _ = _collect(tmp_path, "--timeout", "0", "box:app")
    assert refused == 2 and _argvs(log) == [] and not (tmp_path / "into").exists()


def test_an_empty_remote_instance_writes_an_empty_file(tmp_path: Path) -> None:
    (tmp_path / "remote" / "app").mkdir(parents=True)
    with fake_ssh(tmp_path):
        code, out, _ = _collect(tmp_path, "box:app")
    assert code == 0 and _pulled(tmp_path, "box", "app") == "" and ": 0 lines (0 bytes)" in out


def test_a_missing_remote_directory_is_a_failed_pull(tmp_path: Path) -> None:
    with fake_ssh(tmp_path):
        code, out, _ = _collect(tmp_path, "box:nowhere")
    assert code == 1 and "ssh exit 3): no such directory or cannot enter: nowhere" in out
    assert not (tmp_path / "into" / Target("box", "nowhere").file_name).exists()


def test_a_pull_replaces_the_file_whole_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-11.jsonl": "new\n"})
    (tmp_path / "into").mkdir()
    final = tmp_path / "into" / Target("box", "app").file_name
    final.write_text("o\n")  # smaller than the pull: a pull never shrinks the file
    with fake_ssh(tmp_path):
        code, _, _ = _collect(tmp_path, "box:app")
    assert code == 0 and final.read_text() == "new\n" and os.listdir(tmp_path / "into") == [final.name]


def test_the_file_name_is_safe_and_distinct_per_host_and_path() -> None:
    name = Target("user@box", "/var/lib/my app").file_name
    assert re.fullmatch(r"user-box-var-lib-my-app-[0-9a-f]{16}\.jsonl", name), name
    assert Target("box", "/a/b").file_name != Target("box", "/a-b").file_name
    assert Target("..", "..").file_name.startswith("_-_-")


def test_ai_cost_ssh_names_the_executable(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-11.jsonl": "a\n"})
    with fake_ssh(tmp_path, name="my-ssh") as log:
        with mock.patch.dict(os.environ, {"AI_COST_SSH": str(tmp_path / "bin" / "no-such-ssh")}):
            missing, _, err = _collect(tmp_path, "box:app")
        with mock.patch.dict(os.environ, {"AI_COST_SSH": str(tmp_path / "bin" / "my-ssh")}):
            code, _, _ = _collect(tmp_path, "box:app")
    assert missing == 1 and "AI_COST_SSH" in err
    assert code == 0 and _argvs(log)[0][0].endswith("my-ssh")


def _pull_then_break(tmp: Path, break_usage: str) -> tuple[int, str, str]:
    """A good pull of ``box:app``, then its ``usage/`` made unreadable (``mode``) or a plain file (``file``)."""
    _segments(tmp, "app", {"2026-11.jsonl": "good\n"})
    usage = tmp / "remote" / "app" / "usage"
    with fake_ssh(tmp):
        assert _collect(tmp, "box:app")[0] == 0
        if break_usage == "mode":
            usage.chmod(0o000)
        else:
            (usage / "2026-11.jsonl").unlink()
            usage.rmdir()
            usage.write_text("")
        try:
            code, out, _ = _collect(tmp, "box:app")
        finally:
            usage.chmod(0o755)
    return code, out, _pulled(tmp, "box", "app")


def test_a_usage_directory_that_cannot_be_read_fails_and_keeps_the_file(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        return  # root enters and reads every directory: the refusal this test needs cannot happen
    code, out, kept = _pull_then_break(tmp_path, "mode")
    assert code == 1 and kept == "good\n" and "ssh exit 4): cannot read app/usage" in out and "kept" in out


def test_a_usage_that_is_no_directory_fails_and_keeps_the_file(tmp_path: Path) -> None:
    code, out, kept = _pull_then_break(tmp_path, "file")
    assert code == 1 and kept == "good\n" and "cannot read app/usage" in out


def test_a_segment_without_its_last_newline_is_not_glued_to_the_next(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-10.jsonl": '{"a":1}', "2026-11.jsonl": '{"b":2}\n'})
    with fake_ssh(tmp_path):
        code, out, _ = _collect(tmp_path, "box:app")
    assert code == 0 and _pulled(tmp_path, "box", "app") == '{"a":1}\n{"b":2}\n' and ": 2 lines (" in out


def _month_turns_into(tmp: Path, make: str) -> tuple[int, str, str]:
    """A good pull of two months, then 2026-10 made a link (``link``), a directory (``dir``) or a fifo (``fifo``)."""
    _segments(tmp, "app", {"2026-10.jsonl": "old\n", "2026-11.jsonl": "own\n"})
    month = tmp / "remote" / "app" / "usage" / "2026-10.jsonl"
    with fake_ssh(tmp):
        assert _collect(tmp, "box:app")[0] == 0
        month.unlink()
        (tmp / "remote" / "app" / "usage" / "2026-11.jsonl").write_text(
            "own, and grown past the lost month\n"
        )
        if make == "link":
            (tmp / "outside.jsonl").write_text("outside\n")
            month.symlink_to(tmp / "outside.jsonl")
        elif make == "dir":
            month.mkdir()
        else:
            os.mkfifo(month)
        code, out, _ = _collect(tmp, "box:app")
    return code, out, _pulled(tmp, "box", "app")


def test_a_linked_month_fails_the_pull_and_keeps_the_file(tmp_path: Path) -> None:
    code, out, kept = _month_turns_into(tmp_path, "link")
    assert (
        code == 1
        and kept == "old\nown\n"
        and "ssh exit 6): app/usage/2026-10.jsonl is a link: not pulled" in out
    )


def test_a_month_that_is_a_directory_fails_the_pull(tmp_path: Path) -> None:
    code, out, kept = _month_turns_into(tmp_path, "dir")
    assert code == 1 and kept == "old\nown\n" and "2026-10.jsonl is not a regular file: not pulled" in out


def test_a_month_that_is_a_fifo_fails_the_pull_without_reading_it(tmp_path: Path) -> None:
    code, out, kept = _month_turns_into(tmp_path, "fifo")
    assert code == 1 and kept == "old\nown\n" and "ssh exit 7)" in out


def test_the_pulled_names_are_exactly_those_the_reader_accepts(tmp_path: Path) -> None:
    names = [
        "2026-01.jsonl",
        "2026-09.jsonl",
        "2026-10.jsonl",
        "2026-12.jsonl",
        "2026-00.jsonl",
        "2026-13.jsonl",
    ]
    names += [
        "2026-19.jsonl",
        "2026-1.jsonl",
        "26-01.jsonl",
        "2026-01.json",
        "2026-01.jsonl.tmp",
        "x2026-01.jsonl",
    ]
    _segments(tmp_path, "app", {name: name + "\n" for name in names})
    with fake_ssh(tmp_path):
        assert _collect(tmp_path, "box:app")[0] == 0
    accepted = sorted(name for name in names if SEGMENT_NAME.fullmatch(name))
    assert _pulled(tmp_path, "box", "app").splitlines() == accepted and len(accepted) == 4


def test_offline_refuses_collect_before_any_ssh(tmp_path: Path) -> None:
    with fake_ssh(tmp_path) as log, mock.patch.dict(os.environ, {"AI_COST_OFFLINE": "1"}):
        code, out, err = _collect(tmp_path, "box:app")
    assert code == 2 and out == "" and "collect disabled" in err and _argvs(log) == []


def test_an_into_that_cannot_be_written_is_refused_before_any_ssh(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        return  # root enters and reads every directory: the refusal this test needs cannot happen
    (tmp_path / "into").mkdir(mode=0o555)
    with fake_ssh(tmp_path) as log:
        code, _, err = _collect(tmp_path, "box:app")
    assert code == 2 and "--into: cannot write" in err and _argvs(log) == []


def test_a_long_path_gives_a_bounded_file_name(tmp_path: Path) -> None:
    path = "a" * 250
    _segments(tmp_path, path, {"2026-11.jsonl": "a\n"})
    with fake_ssh(tmp_path):
        code, _, _ = _collect(tmp_path, f"box:{path}")
    name = Target("box", path).file_name
    assert code == 0 and len(name) <= 140 and _pulled(tmp_path, "box", path) == "a\n"


def test_a_local_error_is_said_as_one_and_no_file_is_claimed_kept(tmp_path: Path) -> None:
    result = pull(
        Ssh("ssh", 15), Target("box", "app"), tmp_path / "missing"
    )  # fails before ssh: no directory
    assert (
        result.outcome is Outcome.LOCAL_ERROR and "FAILED (local error): FileNotFoundError" in result.line()
    )
    assert result.line().endswith("no file written")


def test_one_remote_directory_has_one_file_name() -> None:
    spellings = ("app", "~/app", "app/", "./app//", "~/./app", "app/.", "./app/./")
    assert len({Target("box", path).file_name for path in spellings}) == 1
    assert Target("box", "/srv/./app/.").remote == "/srv/app" and Target("box", "~").remote == "."
    assert Target("box", "~/~/app").remote == "~/app" and Target("box", "/").remote == "/"


def test_a_path_with_a_parent_component_is_refused(tmp_path: Path) -> None:
    with fake_ssh(tmp_path) as log:
        codes = [_collect(tmp_path, f"box:{path}")[0] for path in ("../app", "/srv/../app", "app/..")]
    assert codes == [2, 2, 2] and _argvs(log) == []


def test_an_instance_root_without_search_permission_fails_and_keeps_the_file(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        return  # root enters and reads every directory: the refusal this test needs cannot happen
    _segments(tmp_path, "app", {"2026-11.jsonl": "good\n"})
    root = tmp_path / "remote" / "app"
    with fake_ssh(tmp_path):
        assert _collect(tmp_path, "box:app")[0] == 0
        root.chmod(0o600)
        try:
            code, out, _ = _collect(tmp_path, "box:app")
        finally:
            root.chmod(0o755)
    assert code == 1 and _pulled(tmp_path, "box", "app") == "good\n" and "kept" in out


def test_a_smaller_pull_is_refused_and_the_file_kept(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-10.jsonl": "a\n", "2026-11.jsonl": "bb\n"})
    with fake_ssh(tmp_path):
        assert _collect(tmp_path, "box:app")[0] == 0
        (tmp_path / "remote" / "app" / "usage" / "2026-10.jsonl").unlink()
        code, out, _ = _collect(tmp_path, "box:app")
    file = tmp_path / "into" / Target("box", "app").file_name
    assert code == 1 and file.read_text() == "a\nbb\n" and os.listdir(tmp_path / "into") == [file.name]
    assert (
        f"FAILED (smaller pull): pulled 1 lines (3 bytes) where {file} holds 2 lines (5 bytes) — kept" in out
    )
    assert f"delete {file} to accept a smaller pull" in out and out.splitlines()[-1] == "1 of 1 pulls failed"


def test_an_equal_or_larger_pull_replaces_the_file(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-11.jsonl": "a\n"})
    with fake_ssh(tmp_path):
        assert _collect(tmp_path, "box:app")[0] == 0
        equal, _, _ = _collect(tmp_path, "box:app")
        (tmp_path / "remote" / "app" / "usage" / "2026-11.jsonl").write_text("a\nb\n")
        larger, _, _ = _collect(tmp_path, "box:app")
    assert (equal, larger) == (0, 0) and _pulled(tmp_path, "box", "app") == "a\nb\n"


def test_an_empty_pull_over_an_empty_file_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "remote" / "app").mkdir(parents=True)
    with fake_ssh(tmp_path):
        first, _, _ = _collect(tmp_path, "box:app")
        again, out, _ = _collect(tmp_path, "box:app")
    assert (first, again) == (0, 0) and _pulled(tmp_path, "box", "app") == "" and ": 0 lines (0 bytes)" in out


def test_an_exported_cdpath_never_moves_the_pull(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-11.jsonl": "real\n"})
    decoy = tmp_path / "decoy" / "app" / "usage"
    decoy.mkdir(parents=True)
    (decoy / "2026-11.jsonl").write_text("decoy\n")
    with fake_ssh(tmp_path), mock.patch.dict(os.environ, {"CDPATH": str(tmp_path / "decoy")}):
        code, _, _ = _collect(tmp_path, "box:app")
    assert code == 0 and _pulled(tmp_path, "box", "app") == "real\n"


def test_a_root_named_dash_is_never_oldpwd(tmp_path: Path) -> None:
    _segments(tmp_path, "-", {"2026-11.jsonl": "dash\n"})
    _segments(tmp_path, "decoy", {"2026-11.jsonl": "decoy\n"})
    with fake_ssh(tmp_path):
        os.environ.pop("OLDPWD", None)
        unset, _, _ = _collect(tmp_path, "box:./-")
        with mock.patch.dict(os.environ, {"OLDPWD": str(tmp_path / "remote" / "decoy")}):
            decoy, _, _ = _collect(tmp_path, "box:-")
    assert (unset, decoy) == (0, 0) and _pulled(tmp_path, "box", "-") == "dash\n"


def _mode(file: Path) -> int:
    return file.stat().st_mode & 0o777


def test_a_new_file_has_the_tools_mode_under_the_umask(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-11.jsonl": "a\n"})
    mask = os.umask(0o022)
    os.umask(mask)
    with fake_ssh(tmp_path):
        assert _collect(tmp_path, "box:app")[0] == 0
    assert _mode(tmp_path / "into" / Target("box", "app").file_name) == 0o644 & ~mask


def test_a_re_pull_keeps_the_files_mode(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-11.jsonl": "a\n"})
    with fake_ssh(tmp_path):
        assert _collect(tmp_path, "box:app")[0] == 0
        final = tmp_path / "into" / Target("box", "app").file_name
        final.chmod(0o640)
        assert _collect(tmp_path, "box:app")[0] == 0
    assert _mode(final) == 0o640


def test_an_into_that_cannot_be_created_is_a_usage_error(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("")
    with fake_ssh(tmp_path) as log:
        code, _, err = run_cli(["collect", "--into", str(tmp_path / "file" / "into"), "box:app"])
    assert code == 2 and "--into: cannot create" in err and _argvs(log) == []


def test_a_linked_usage_directory_fails_the_pull_and_keeps_the_file(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-11.jsonl": "own\n"})
    _segments(tmp_path, "other", {"2026-11.jsonl": "other instance's records, more of them\n"})
    usage = tmp_path / "remote" / "app" / "usage"
    with fake_ssh(tmp_path):
        assert _collect(tmp_path, "box:app")[0] == 0
        (usage / "2026-11.jsonl").unlink()
        usage.rmdir()
        usage.symlink_to(tmp_path / "remote" / "other" / "usage")
        code, out, _ = _collect(tmp_path, "box:app")
    assert code == 1 and _pulled(tmp_path, "box", "app") == "own\n"
    assert "ssh exit 5): app/usage is a link: not pulled — " in out and "kept" in out


def test_a_re_pull_over_a_read_only_file_succeeds_and_keeps_its_mode(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-11.jsonl": "a\n"})
    with fake_ssh(tmp_path):
        assert _collect(tmp_path, "box:app")[0] == 0
        final = tmp_path / "into" / Target("box", "app").file_name
        final.chmod(0o444)
        (tmp_path / "remote" / "app" / "usage" / "2026-11.jsonl").write_text("a\nb\n")
        code, _, _ = _collect(tmp_path, "box:app")
    assert code == 0 and final.read_text() == "a\nb\n" and _mode(final) == 0o444


def test_a_remote_without_awk_is_a_failed_pull_that_names_it(tmp_path: Path) -> None:
    _segments(tmp_path, "app", {"2026-11.jsonl": "a\n"})
    only_sh = tmp_path / "only-sh"  # the remote's PATH: sh and nothing else
    only_sh.mkdir()
    (only_sh / "sh").symlink_to("/bin/sh")
    remote_path = {"FAKE_SSH_REMOTE_PATH": str(only_sh)}
    with fake_ssh(tmp_path), mock.patch.dict(os.environ, remote_path):
        code, out, _ = _collect(tmp_path, "box:app")
    assert code == 1 and "awk" in out.splitlines()[0] and "no file written" in out
