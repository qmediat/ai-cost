"""The distribution's identity: the PyPI name is ``ai-costs`` (``ai-cost`` belongs to another project) and the
version the code reports is the version the package metadata declares. Both live in ``pyproject.toml``, which a
checkout has and an installed package does not: outside a checkout there is nothing to compare and the test passes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

from .. import __version__

DISTRIBUTION = "ai-costs"
ROOT = Path(__file__).resolve().parents[3]


def test_the_distribution_is_ai_costs_at_the_code_version() -> None:
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.is_file():
        return
    text = pyproject.read_text(encoding="utf-8")
    assert re.search(r'^name = "ai-costs"$', text, re.M), "the PyPI distribution is ai-costs"
    assert re.search(
        rf'^version = "{re.escape(__version__)}"$', text, re.M
    ), "pyproject version == __version__"
    for doc in ("README.md", "docs/SETUP.md", "standalone/SKILL.md"):
        assert f"pipx install {DISTRIBUTION}" in (ROOT / doc).read_text(encoding="utf-8"), doc


def _npm_package() -> ModuleType | None:
    """``scripts/npm_package.py`` of a checkout (it must be there); None in an installed package, which has none."""
    if not (ROOT / "pyproject.toml").is_file():
        return None
    script = ROOT / "scripts" / "npm_package.py"
    assert script.is_file(), "a checkout carries the npm package's assembler"
    name = "ai_cost_npm_package"
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = (
        module  # a dataclass resolves its module through sys.modules while it is being defined
    )
    spec.loader.exec_module(module)
    return module


def _node() -> str | None:
    """Node for the launcher tests: required in CI, optional on a machine without it."""
    node = shutil.which("node")
    assert node or not os.environ.get("CI"), "CI runs the npm launcher tests: node must be on PATH"
    return node


def _launcher(tmp_path: Path) -> list[str] | None:
    """The assembled package's launcher, run with node; None where there is no checkout or no node."""
    npm = _npm_package()
    node = (
        _node() if npm is not None else None
    )  # an installed package has nothing to run: node is not asked for
    if npm is None or node is None:
        return None
    zipapp = tmp_path / "ai-cost"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build.py"), str(ROOT / "src"), str(zipapp)], check=True
    )
    npm.assemble(tmp_path / "npm", zipapp)
    return [node, str(tmp_path / "npm" / "bin" / "ai-cost.js")]


def _run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, capture_output=True, text=True, check=False, timeout=60)


def test_the_npm_package_is_ai_costs_at_the_code_version_and_installs_nothing(tmp_path: Path) -> None:
    npm = _npm_package()
    if npm is None:
        return
    zipapp = tmp_path / "ai-cost"
    zipapp.write_bytes(b"PK\x05\x06" + bytes(18))  # an empty zip: the assembler copies it, never reads it
    out = tmp_path / "npm"
    assert npm.main([str(out), "--zipapp", str(zipapp)]) == 0
    manifest = json.loads((out / "package.json").read_text(encoding="utf-8"))
    assert manifest["name"] == DISTRIBUTION and manifest["version"] == __version__
    assert manifest["bin"] == {"ai-cost": "bin/ai-cost.js"} and manifest["license"] == "MIT"
    assert manifest["repository"]["url"] == "git+https://github.com/qmediat/ai-cost.git"
    assert "scripts" not in manifest and "dependencies" not in manifest, "installing the package runs nothing"
    assert (out / "ai-cost.pyz").read_bytes() == zipapp.read_bytes(), "the zipapp, byte for byte"
    assert os.access(out / "bin" / "ai-cost.js", os.X_OK)
    assert npm.main([str(out), "--zipapp", str(zipapp)]) == 2, "a written package is never overwritten"
    (tmp_path / "a-link").symlink_to(tmp_path / "empty-dir", target_is_directory=True)
    (tmp_path / "empty-dir").mkdir()
    assert (
        npm.main([str(tmp_path / "a-link"), "--zipapp", str(zipapp)]) == 2
    ), "a link as the destination: exit 2"
    (tmp_path / "a-file").write_text("x")
    assert npm.main([str(tmp_path / "a-file"), "--zipapp", str(zipapp)]) == 2, "a file in the way: exit 2"
    assert npm.main([str(tmp_path / "other"), "--zipapp", str(tmp_path / "absent")]) == 2
    assert "npm install -g ai-costs" in (ROOT / "README.md").read_text(
        encoding="utf-8"
    ), "the README says how"


def test_the_launcher_asks_for_the_python_pyproject_requires() -> None:
    npm = _npm_package()
    if npm is None:
        return
    source = (ROOT / "npm" / "bin" / "ai-cost.js").read_text(encoding="utf-8")
    floor = re.search(r"const MINIMUM = \[(\d+), (\d+)\];", source)
    assert floor is not None, "the launcher names its minimum Python"
    assert (int(floor.group(1)), int(floor.group(2))) == npm.requires_python(ROOT / "pyproject.toml")


def test_the_npm_launcher_runs_the_zipapp_and_hands_back_its_exit_code(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    if launcher is None:
        return
    env = {**os.environ, "AI_COST_PYTHON": sys.executable, "AI_COST_OFFLINE": "1", "HOME": str(tmp_path)}
    version = _run([*launcher, "--version"], env)
    assert version.returncode == 0 and version.stdout.strip() == f"ai-cost {__version__}", version
    usage = _run([*launcher, "install"], env)
    assert (
        usage.returncode == 2 and "nothing to do" in usage.stderr
    ), "the program's exit code, not the launcher's"
    env["AI_COST_PYTHON"] = str(tmp_path / "no-python")
    missing = _run([*launcher, "--version"], env)
    assert (
        missing.returncode == 1 and f"AI_COST_PYTHON={tmp_path / 'no-python'} did not run" in missing.stderr
    )


def test_the_npm_launcher_finds_python3_on_the_path(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    if launcher is None or sys.platform == "win32":
        return
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    env = {"PATH": str(fake_bin), "HOME": str(tmp_path), "AI_COST_OFFLINE": "1"}
    found = _run([*launcher, "--version"], env)
    assert found.returncode == 0 and found.stdout.strip() == f"ai-cost {__version__}", found


_FAKE_PYTHON = """#!/bin/sh
[ "$1" = "-c" ] && exit 0
case "$2" in
  usr1) kill -USR1 $$ ;;
esac
trap 'echo term > "$MARK"; trap - TERM; kill -TERM $$' TERM
trap 'echo int > "$MARK"; trap - INT; kill -INT $$' INT
: > "$MARK.started"
while :; do sleep 0.1; done
"""


def _fake_python(tmp_path: Path) -> dict[str, str]:
    """A stand-in interpreter: answers the version probe, then waits for SIGTERM (or dies of SIGUSR1 on ``usr1``)."""
    fake = tmp_path / "fake-python"
    fake.write_text(_FAKE_PYTHON)
    fake.chmod(0o755)
    return {**os.environ, "AI_COST_PYTHON": str(fake), "MARK": str(tmp_path / "mark")}


def test_a_sigterm_reaches_the_program_and_the_launcher_ends_by_it(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    if launcher is None or sys.platform == "win32":
        return
    env = _fake_python(tmp_path)
    child = subprocess.Popen([*launcher, "wait"], env=env)
    deadline = time.monotonic() + 30
    while not (tmp_path / "mark.started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=30) == -signal.SIGTERM, "ended by the same signal, as the program was"
    assert (
        tmp_path / "mark"
    ).read_text().strip() == "term", "the program heard the signal sent to the launcher"
    (tmp_path / "mark.started").unlink()
    child = subprocess.Popen([*launcher, "wait"], env=env)
    deadline = time.monotonic() + 30
    while not (tmp_path / "mark.started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child.send_signal(
        signal.SIGINT
    )  # to the launcher alone, as a supervisor sends it — not the terminal's group
    assert child.wait(timeout=30) == -signal.SIGINT and (tmp_path / "mark").read_text().strip() == "int"
    usr1 = _run([*launcher, "usr1"], env)
    assert (
        usr1.returncode == 128 + signal.SIGUSR1
    ), "a signal Node would treat itself is an exit code, 128 + n"


def test_scheduling_from_npx_cache_is_refused_because_the_file_may_vanish() -> None:
    from ..process import transient_warning

    cached = transient_warning(["python3", "/home/u/.npm/_npx/0a1b/node_modules/ai-costs/ai-cost.pyz"])
    assert "npx's cache" in cached and "npm install -g ai-costs" in cached
    assert transient_warning(["python3", "/usr/local/lib/node_modules/ai-costs/ai-cost.pyz"]) == ""
    assert transient_warning(["python3", "-m", "ai_cost"]) == ""


def test_install_refuses_to_schedule_a_copy_from_npx_cache(tmp_path: Path) -> None:
    from unittest import mock

    from .. import cli

    cached = ["python3", str(tmp_path / ".npm" / "_npx" / "0a1b" / "ai-cost.pyz")]
    env = {"AI_COST_CONFIG_DIR": str(tmp_path / "cfg"), "AI_COST_STATE_DIR": str(tmp_path / "state")}
    with (
        mock.patch.dict(os.environ, env),
        mock.patch.object(cli, "self_command", return_value=cached),
        mock.patch.object(cli, "install_job") as job,
        mock.patch.object(cli, "install_schedule") as schedule,
    ):
        assert cli.main(["install", "--schedule-reports"]) == 2
        assert cli.main(["install", "--schedule", "3"]) == 2
        assert cli.main(["install", "--init-config", "--schedule-reports"]) == 2
    assert not job.called and not schedule.called, "nothing is registered from a file npm may delete"
    assert (
        tmp_path / "cfg" / "config.json"
    ).is_file(), "only the jobs are refused: the config is still written"
