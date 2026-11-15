"""Every email address in the tree is in ``qmediat.io`` or on the allowed list; no shape is exempt.

The check is ``scripts/check_addresses.py``. A checkout carries it; an installed package, the zipapp and an unpacked
sdist do not: there every test here returns after one line on stderr says the check was not run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_addresses.py"
# Every address is joined at run time, so this module's own text holds none.
AT = "@"
FOREIGN = f"someone{AT}foreign.invalid"
SKIPPED = f"skipped{AT}foreign.invalid"
ACCENTED = f"José.Łukasz{AT}foreign.invalid"
_SAID: list[bool] = []  # the "not run" line is said once per run, not once per test


def _is_checkout() -> bool:
    return (
        (ROOT / "pyproject.toml").is_file()
        and (ROOT / "src" / "ai_cost").is_dir()
        and (ROOT / "scripts").is_dir()
    )


def _script() -> Path | None:
    """The check, or None — said on stderr — where this tree carries no ``scripts/check_addresses.py``."""
    if _is_checkout():
        assert SCRIPT.is_file(), "a checkout's scripts/ carries the address check"
    if SCRIPT.is_file():
        return SCRIPT
    if not _SAID:
        _SAID.append(True)
        print(
            "test_addresses: no scripts/check_addresses.py in this tree: the address check is not run",
            file=sys.stderr,
        )
    return None


def _run(*args: str) -> subprocess.CompletedProcess[str] | None:
    """Run the check with ARGS; None where there is no check to run."""
    script = _script()
    if script is None:
        return None
    return subprocess.run(
        [sys.executable, str(script), *args], capture_output=True, text=True, check=False, timeout=60
    )


def _check(root: Path, *flags: str) -> subprocess.CompletedProcess[str] | None:
    """Run the check on ROOT with FLAGS; None where there is no check to run."""
    return _run(*flags, str(root))


@contextmanager
def _loaded() -> Iterator[Any]:  # the check's module, loaded from its file: its attributes are not typed here
    """The check imported from its file, unregistered afterwards; None where there is no check."""
    script = _script()
    if script is None:
        yield None
        return
    spec = importlib.util.spec_from_file_location("check_addresses_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module while the class is created
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _not_as_root(what: str) -> bool:
    """False — said on stderr — when running as root, who reads what has no permissions: nothing to prove."""
    if os.geteuid() != 0:
        return True
    print(f"test_addresses: running as root: {what} is not run", file=sys.stderr)
    return False


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """A directory holding FILES (relative path → text)."""
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_an_address_in_another_domain_fails_naming_path_and_line(tmp_path: Path) -> None:
    run = _check(_tree(tmp_path, {"docs/a.md": f"# a\n\nWrite to {FOREIGN} today.\n"}))
    if run is None:
        return
    assert run.returncode == 1, run.stderr
    assert f"docs/a.md:3: {FOREIGN}" in run.stderr.splitlines()


def test_qmediat_and_every_allowed_kind_pass(tmp_path: Path) -> None:
    text = (
        f"dev{AT}qmediat.io, security{AT}qmediat.io, ops{AT}mail.qmediat.io\n"
        f"12345+someone{AT}users.noreply.github.com\n"
        f"a{AT}example.com b{AT}example.org c{AT}example.net\n"
        f"Co-Authored-By: Claude <noreply{AT}anthropic.com>\n"
    )
    run = _check(_tree(tmp_path, {"README.md": text}))
    if run is None:
        return
    assert run.returncode == 0, run.stderr
    assert "1 files read" in run.stdout


def test_every_address_shaped_text_is_a_finding(tmp_path: Path) -> None:
    shaped = [
        (f"git clone git{AT}github.com:o/r.git", f"git{AT}github.com"),
        (f"git clone ssh://git{AT}github.com/o/r.git", f"git{AT}github.com"),
        (f"![logo](logo{AT}2x.png)", f"logo{AT}2x.png"),
        (f"npx name{AT}1.0.0-alpha.beta", f"name{AT}1.0.0-alpha.beta"),
        (f"Write to contact{AT}2x.com", f"contact{AT}2x.com"),
        (f"https://alice{AT}gmail.com/profile", f"alice{AT}gmail.com"),
        (f"Contact {FOREIGN}: details", FOREIGN),
    ]
    run = _check(_tree(tmp_path, {"a.md": "\n".join(line for line, _ in shaped) + "\n"}))
    if run is None:
        return
    assert run.returncode == 1, run.stderr
    expected = [f"a.md:{number}: {address}" for number, (_, address) in enumerate(shaped, start=1)]
    assert run.stderr.splitlines()[: len(shaped)] == expected, run.stderr


@dataclass(frozen=True)
class Corpus:
    """The shared corpus: lines with the addresses each reports in order, lines that report nothing, allowed lines."""

    findings: tuple[tuple[str, tuple[str, ...]], ...]
    clean: tuple[str, ...]
    allowed: tuple[str, ...]


def _corpus() -> Corpus | None:
    """The shared corpus with ``(at)`` replaced by the at-sign; None — said on stderr — where it is absent."""
    corpus = resources.files(__package__).joinpath("data/address-corpus.json")
    if not corpus.is_file():
        print("test_addresses: no data/address-corpus.json here: the corpus is not run", file=sys.stderr)
        return None
    raw = json.loads(corpus.read_text(encoding="utf-8").replace("(at)", AT))
    findings = tuple(
        (
            str(item["line"]),
            tuple(map(str, item["addresses"])) if "addresses" in item else (str(item["address"]),),
        )
        for item in raw["findings"]
    )
    return Corpus(findings, tuple(map(str, raw["clean"])), tuple(map(str, raw["allowed"])))


def test_every_corpus_finding_is_reported_exactly_and_nothing_else(tmp_path: Path) -> None:
    corpus = _corpus()
    if corpus is None:
        return
    files = {
        "findings.md": "\n".join(line for line, _ in corpus.findings) + "\n",
        "clean.md": "\n".join(corpus.clean) + "\n",
        "allowed.md": "\n".join(corpus.allowed) + "\n",
    }
    run = _check(_tree(tmp_path, files))
    if run is None:
        return
    assert run.returncode == 1, run.stderr
    expected = [
        f"findings.md:{number}: {address}"
        for number, (_, addresses) in enumerate(corpus.findings, start=1)
        for address in addresses
    ]
    assert run.stderr.splitlines()[:-1] == expected, run.stderr


def test_every_corpus_clean_and_allowed_line_passes(tmp_path: Path) -> None:
    corpus = _corpus()
    if corpus is None:
        return
    run = _check(_tree(tmp_path, {"lines.md": "\n".join(corpus.clean + corpus.allowed) + "\n"}))
    if run is None:
        return
    assert run.returncode == 0, run.stderr


def test_a_last_label_that_is_not_two_letters_is_not_matched(tmp_path: Path) -> None:
    text = (
        f"npx sdk{AT}1.29.x and name{AT}1.2.3\n"
        f"npm i {AT}scope/name\n"
        f"pkg {AT} https://files.example.invalid/pkg.whl\n"
        f"user{AT}host/path\n"
    )
    run = _check(_tree(tmp_path, {"SETUP.md": text}))
    if run is None:
        return
    assert run.returncode == 0, run.stderr


def test_an_accented_local_part_in_another_domain_fails(tmp_path: Path) -> None:
    run = _check(_tree(tmp_path, {"a.txt": f"{ACCENTED}\n"}))
    if run is None:
        return
    assert run.returncode == 1, run.stderr
    assert f"a.txt:1: {ACCENTED}" in run.stderr.splitlines()


def test_a_repository_entry_is_allowed_in_its_file_only() -> None:
    with _loaded() as module:
        if module is None:
            return
        module.REPOSITORY_ADDRESSES = (module.RepositoryAddress("docs/a.md", FOREIGN.upper()),)
        assert module.findings_in("docs/a.md", f"{FOREIGN}\n") == []
        assert module.findings_in("docs/b.md", f"{FOREIGN}\n") == [module.Finding("docs/b.md", 1, FOREIGN)]
    assert "check_addresses_under_test" not in sys.modules


def _run_with_entry(
    tmp_path: Path, entry_file: str, tree: Path, *flags: str
) -> subprocess.CompletedProcess[str] | None:
    """Run a copy of the check whose allowlist holds one entry (ENTRY_FILE, the foreign address in capitals)."""
    script = _script()
    if script is None:
        return None
    opening = "REPOSITORY_ADDRESSES: tuple[RepositoryAddress, ...] = ("
    source = script.read_text(encoding="utf-8")
    assert source.count(opening) == 1, "the check declares its allowlist in one place"
    entry = f'RepositoryAddress("{entry_file}", "{FOREIGN.upper()}"),'
    copy = tmp_path / "check_with_entry.py"
    copy.write_text(source.replace(opening, opening + entry), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(copy), *flags, str(tree)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_a_repository_entry_allows_its_address_in_its_file_only_through_the_command(tmp_path: Path) -> None:
    tree = _tree(tmp_path / "tree", {"docs/a.md": f"{FOREIGN}\n", "docs/b.md": f"{FOREIGN}\n"})
    run = _run_with_entry(tmp_path, "docs/a.md", tree)
    if run is None:
        return
    assert run.returncode == 1, run.stderr
    assert f"docs/b.md:1: {FOREIGN}" in run.stderr.splitlines()
    assert "docs/a.md" not in run.stderr


def test_a_repository_entry_covers_its_file_below_a_prefix(tmp_path: Path) -> None:
    files = {"ai-cost/src/ai_cost/x.md": f"{FOREIGN}\n", "ai-cost/src/ai_cost/other.md": f"{FOREIGN}\n"}
    run = _run_with_entry(tmp_path, "src/ai_cost/x.md", _tree(tmp_path / "tree", files), "--walk")
    if run is None:
        return
    assert run.returncode == 1, run.stderr
    assert f"ai-cost/src/ai_cost/other.md:1: {FOREIGN}" in run.stderr.splitlines()
    assert "x.md" not in run.stderr


def test_an_unreadable_file_cannot_be_told_in_one_line(tmp_path: Path) -> None:
    if not _not_as_root("the unreadable-file test"):
        return
    root = _tree(tmp_path, {"locked.txt": "x\n"})
    (root / "locked.txt").chmod(0)
    run = _check(root)
    (root / "locked.txt").chmod(0o600)
    if run is None:
        return
    assert run.returncode == 2, run.stderr
    lines = run.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("check_addresses: cannot read locked.txt: "), run.stderr


def test_a_symlink_out_of_the_tree_cannot_be_told(tmp_path: Path) -> None:
    root = _tree(tmp_path / "tree", {"a.txt": "x\n"})
    outside = _tree(tmp_path / "outside", {"b.txt": f"{FOREIGN}\n"})
    (root / "link.txt").symlink_to(outside / "b.txt")
    run = _check(root)
    if run is None:
        return
    assert run.returncode == 2, run.stderr
    assert "check_addresses: cannot read link.txt:" in run.stderr


def test_a_symlink_loop_cannot_be_told_in_one_line(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"a.txt": "x\n"})
    (root / "loop").symlink_to("loop")
    run = _check(root)
    if run is None:
        return
    assert run.returncode == 2, run.stderr
    lines = run.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("check_addresses: cannot read loop: "), run.stderr


def test_the_fallback_walk_skips_a_symlinked_git_or_cache_directory(tmp_path: Path) -> None:
    root = _tree(tmp_path / "tree", {"a.txt": "x\n"})
    outside = _tree(tmp_path / "outside", {"config": f"{FOREIGN}\n"})
    (root / ".git").symlink_to(outside, target_is_directory=True)
    (root / "__pycache__").symlink_to(outside, target_is_directory=True)
    run = _check(root)
    if run is None:
        return
    assert run.returncode == 0, run.stderr


def test_a_binary_file_is_skipped(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"a.txt": "x\n"})
    (root / "blob.bin").write_bytes(b"\x00\x01" + FOREIGN.encode() + b"\n")
    run = _check(root)
    if run is None:
        return
    assert run.returncode == 0, run.stderr
    assert "1 files read" in run.stdout


def test_a_walk_skips_only_caches(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "deep/er/a.md": f"{FOREIGN}\n",
            "dist/x.txt": f"{FOREIGN}\n",
            "build/y.txt": f"{FOREIGN}\n",
            "pkg.egg-info/PKG-INFO": f"{FOREIGN}\n",
            ".git/config": f"{FOREIGN}\n",
            "src/__pycache__/m.txt": f"{SKIPPED}\n",
        },
    )
    run = _check(root, "--walk")
    if run is None:
        return
    assert run.returncode == 1, run.stderr
    found = run.stderr.splitlines()
    for name in ("deep/er/a.md", "dist/x.txt", "build/y.txt", "pkg.egg-info/PKG-INFO", ".git/config"):
        assert f"{name}:1: {FOREIGN}" in found, found
    assert SKIPPED not in run.stderr


def test_a_walk_reads_a_nested_git_directory(tmp_path: Path) -> None:
    run = _check(_tree(tmp_path, {"src/ai_cost/.git/config": f"{FOREIGN}\n"}), "--walk")
    if run is None:
        return
    assert run.returncode == 1, run.stderr
    assert f"src/ai_cost/.git/config:1: {FOREIGN}" in run.stderr.splitlines()


def test_the_fallback_walk_skips_git_directories(tmp_path: Path) -> None:
    run = _check(_tree(tmp_path, {"a.md": "x\n", "src/.git/config": f"{FOREIGN}\n"}))
    if run is None:
        return
    assert run.returncode == 0, run.stderr


def _git(cwd: Path, *args: str) -> None:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=True, timeout=60)


def _have_git() -> bool:
    if shutil.which("git") is None:
        print("test_addresses: no git on this machine: the git-listing tests are not run", file=sys.stderr)
        return False
    return True


def _repo(tmp_path: Path, tracked: dict[str, str], other: dict[str, str]) -> Path:
    """A git work tree where the TRACKED files are added and the OTHER files are only written."""
    root = _tree(tmp_path, tracked)
    _git(root, "init", "-q")
    _git(root, "add", "--", *tracked)
    return _tree(root, other)


def test_a_walk_reads_what_gitignore_ignores_and_the_tracked_mode_reads_only_tracked_files(
    tmp_path: Path,
) -> None:
    if not _have_git():
        return
    root = _repo(
        tmp_path,
        {"a.md": "x\n", ".gitignore": "secret.md\n"},
        {"secret.md": f"{FOREIGN}\n", "new.md": f"{SKIPPED}\n"},
    )
    walked = _check(root, "--walk")
    tracked = _check(root)
    if walked is None or tracked is None:
        return
    assert walked.returncode == 1, walked.stderr
    assert f"secret.md:1: {FOREIGN}" in walked.stderr.splitlines()
    assert f"new.md:1: {SKIPPED}" in walked.stderr.splitlines()
    assert tracked.returncode == 0, tracked.stderr
    assert "2 files read" in tracked.stdout


def test_a_tree_inside_a_work_tree_that_tracks_nothing_there_is_walked(tmp_path: Path) -> None:
    if not _have_git():
        return
    repo = _repo(tmp_path, {".gitignore": "export/\n"}, {})
    run = _check(_tree(repo / "export", {"a.md": f"{FOREIGN}\n"}))
    if run is None:
        return
    assert run.returncode == 1, run.stderr
    assert f"a.md:1: {FOREIGN}" in run.stderr.splitlines()


def test_a_tracked_file_under_a_directory_that_cannot_be_searched_cannot_be_told(tmp_path: Path) -> None:
    if not _have_git() or not _not_as_root("the unsearchable-directory test"):
        return
    root = _repo(tmp_path, {"a.md": "x\n", "locked/b.md": "x\n"}, {})
    (root / "locked").chmod(0)
    run = _check(root)
    (root / "locked").chmod(0o700)
    if run is None:
        return
    assert run.returncode == 2, run.stderr
    assert "check_addresses: cannot read locked/b.md: " in run.stderr, run.stderr


def test_a_work_tree_whose_tracked_files_are_all_gone_is_walked(tmp_path: Path) -> None:
    if not _have_git():
        return
    repo = _repo(tmp_path, {"sub/a.md": "x\n", ".gitignore": "ignored.txt\n"}, {})
    (repo / "sub" / "a.md").unlink()
    run = _check(_tree(repo / "sub", {"ignored.txt": f"{FOREIGN}\n"}))
    if run is None:
        return
    assert run.returncode == 1, run.stderr
    assert f"ignored.txt:1: {FOREIGN}" in run.stderr.splitlines()


def test_a_second_argument_or_an_option_is_a_usage_error(tmp_path: Path) -> None:
    for args in ([str(tmp_path), str(tmp_path)], ["--help"], ["--walk", "-x", str(tmp_path)]):
        run = _run(*args)
        if run is None:
            return
        assert run.returncode == 2, run.stderr
        assert run.stderr.startswith("usage: "), run.stderr


def test_the_package_tree_holds_only_allowed_addresses() -> None:
    run = _check(ROOT)
    if run is None:
        return
    assert run.returncode == 0, run.stderr
