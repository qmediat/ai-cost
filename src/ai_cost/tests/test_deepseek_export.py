"""DeepSeek's usage export imported as its day report (ADR-0008, decision 8): checks, store, merge, closed days."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from ..errors import ToolError, UsageError
from ..models import Window
from ..providers.deepseek_export import import_export, read_days, store_path

FIRST, LAST = "2026-09-20", "2026-09-22"
ZIP_TIME = (2026, 9, 23, 1, 0, 0)  # DeepSeek writes its members' time in UTC+8, with no zone
CAPTURED = datetime(
    2026, 9, 22, 11, 0, tzinfo=timezone.utc
)  # that time read in UTC+14: the earliest it can be


def _fixture(kind: str) -> str:
    name = f"data/deepseek-export/{kind}-{FIRST}_{LAST}.csv"
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8-sig")


def _export(
    tmp: Path,
    edit: Callable[[str, str], str] = lambda kind, text: text,
    first: str = FIRST,
    when: tuple[int, ...] = ZIP_TIME,
    last: str = LAST,
) -> Path:
    """A ZIP like DeepSeek's, from the 3-day fixture (the real export cut to 09-20..09-22, the ids replaced)."""
    path = tmp / f"usage_data_{first}_{last}_{len(list(tmp.glob('*.zip')))}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for kind in ("cost", "amount"):
            info = zipfile.ZipInfo(f"{kind}-{first}_{last}.csv", date_time=when)  # type: ignore[arg-type]
            archive.writestr(info, "﻿" + edit(kind, _fixture(kind)))
    return path


def _refused(call: Callable[[], object]) -> str:
    """The message of the UsageError ``call`` raises; a call that does not refuse fails the test."""
    try:
        call()
    except UsageError as exc:
        return str(exc)
    raise AssertionError("expected a refusal (UsageError)")


def _store(state: Path) -> dict[str, Any]:
    stored: dict[str, Any] = json.loads(store_path(state).read_text())
    return stored


def test_an_export_is_checked_and_stored_without_the_account_or_its_keys(tmp_path: Path) -> None:
    result = import_export(_export(tmp_path), tmp_path / "state")
    stored = _store(tmp_path / "state")
    text = json.dumps(stored)
    assert (
        result.added
        == (
            "2026-09-20T00:00:00+02:00",
            "2026-09-21T00:00:00+02:00",
            "2026-09-22T00:00:00+02:00",
        )
        and result.replaced == ()
    )
    assert "00000000-0000-4000-8000" not in text and "test-key" not in text and "sk-test" not in text
    assert len(stored["account"]) == 16, "an opaque fingerprint of the account"
    lines = {(d["start"][:10], line["label"]): line for d in stored["days"] for line in d["lines"]}
    assert lines[("2026-09-21", "deepseek-flash")]["gross"] == "8.7635721780000000"
    assert lines[("2026-09-21", "deepseek-flash")]["requests"] == 142, "the export's request_count"
    assert set(lines[("2026-09-21", "deepseek-flash")]["tokens"]) == {
        "input_cache_hit_tokens",
        "input_cache_miss_tokens",
        "output_tokens",
    }


def _without(day: str) -> Callable[[str, str], str]:
    """An edit that drops every row of ``day`` from both files (the day billed nothing)."""

    def edit(kind: str, text: str) -> str:
        kept = [line for line in text.splitlines() if line.split(",")[1:2] != [f"{day}T00:00:00+02:00"]]
        return "\n".join(kept) + "\n"

    return edit


def test_the_time_between_two_days_is_a_zero_interval_and_dates_without_a_boundary_are_not_stored(
    tmp_path: Path,
) -> None:
    import_export(_export(tmp_path, _without("2026-09-21"), first="2026-09-19"), tmp_path / "state")
    days = [(d["start"], d["end"], d["lines"] == []) for d in _store(tmp_path / "state")["days"]]
    assert days == [
        ("2026-09-20T00:00:00+02:00", "2026-09-21T00:00:00+02:00", False),
        (
            "2026-09-21T00:00:00+02:00",
            "2026-09-22T00:00:00+02:00",
            True,
        ),  # between two days: covered, billed nothing
        ("2026-09-22T00:00:00+02:00", "2026-09-23T00:00:00+02:00", False),
    ], "09-19 has no row, so no boundary the export states: not stored"


def test_a_daylight_saving_change_keeps_each_days_own_interval(tmp_path: Path) -> None:
    def autumn(kind: str, text: str) -> str:
        text = text.replace("2026-09-20T00:00:00+02:00", "2026-10-24T00:00:00+02:00")
        text = text.replace("2026-09-21T00:00:00+02:00", "2026-10-25T00:00:00+02:00")  # starts in summer time
        text = text.replace(
            "2026-09-22T00:00:00+02:00", "2026-10-26T00:00:00+01:00"
        )  # the 25 h day ends in winter
        return text.replace("2026-09-23T00:00:00+02:00", "2026-10-27T00:00:00+01:00")

    import_export(
        _export(tmp_path, autumn, first="2026-10-24", last="2026-10-26", when=(2026, 10, 28, 1, 0, 0)),
        tmp_path / "state",
    )
    spans = [(d["start"], d["end"]) for d in _store(tmp_path / "state")["days"]]
    assert spans == [
        ("2026-10-24T00:00:00+02:00", "2026-10-25T00:00:00+02:00"),
        ("2026-10-25T00:00:00+02:00", "2026-10-26T00:00:00+01:00"),
        ("2026-10-26T00:00:00+01:00", "2026-10-27T00:00:00+01:00"),
    ]


def test_a_row_of_the_wrong_length_is_refused_naming_its_line(tmp_path: Path) -> None:
    def truncated(kind: str, text: str) -> str:
        lines = text.splitlines()
        return (
            "\n".join([lines[0], lines[1].rsplit(",", 2)[0], *lines[2:]]) + "\n" if kind == "cost" else text
        )

    def longer(kind: str, text: str) -> str:
        lines = text.splitlines()
        return "\n".join([lines[0], lines[1] + ",surprise", *lines[2:]]) + "\n" if kind == "cost" else text

    assert "line 2 is not 7 fields" in _refused(
        lambda: import_export(_export(tmp_path, truncated), tmp_path / "s1")
    )
    assert "line 2 is not 7 fields" in _refused(
        lambda: import_export(_export(tmp_path, longer), tmp_path / "s2")
    )


def test_a_missing_file_or_an_impossible_time_is_said_as_such(tmp_path: Path) -> None:
    assert "no such file or directory" in _refused(
        lambda: import_export(tmp_path / "nope.zip", tmp_path / "state")
    )
    early = _export(tmp_path, when=(1980, 1, 1, 0, 0, 0))
    assert "before its own range" in _refused(lambda: import_export(early, tmp_path / "state"))


def test_a_day_is_closed_only_when_it_ended_before_the_capture(tmp_path: Path) -> None:
    import_export(_export(tmp_path), tmp_path / "state")
    window = Window(
        datetime(2026, 9, 19, 22, tzinfo=timezone.utc), datetime(2026, 9, 22, 22, tzinfo=timezone.utc)
    )
    report = read_days(tmp_path / "state", window)
    assert [(d.start.date().isoformat(), d.closed) for d in report.days] == [
        ("2026-09-20", True),
        ("2026-09-21", True),
        ("2026-09-22", False),  # ends 09-22T22:00Z, after the capture's earliest instant 09-22T11:00Z
    ]
    assert report.captured == CAPTURED and report.missing == ()


def test_a_duplicated_cost_row_is_refused(tmp_path: Path) -> None:
    def twice(kind: str, text: str) -> str:
        return text + text.splitlines()[1] + "\n" if kind == "cost" else text

    assert "duplicated row" in _refused(lambda: import_export(_export(tmp_path, twice), tmp_path / "state"))
    assert not store_path(tmp_path / "state").exists()


def test_a_cost_that_its_priced_amounts_do_not_make_is_refused(tmp_path: Path) -> None:
    def off(kind: str, text: str) -> str:
        return text.replace("8.7635721780000000", "8.7635721790000000") if kind == "cost" else text

    assert "≠ Σ price × amount" in _refused(lambda: import_export(_export(tmp_path, off), tmp_path / "state"))


def test_priced_amounts_without_a_cost_row_are_refused(tmp_path: Path) -> None:
    def dropped(kind: str, text: str) -> str:
        return (
            "\n".join(line for line in text.splitlines() if "2026-09-21" not in line) + "\n"
            if kind == "cost"
            else text
        )

    assert "cost 0 ≠" in _refused(lambda: import_export(_export(tmp_path, dropped), tmp_path / "state"))


def test_an_unknown_counter_or_column_is_refused(tmp_path: Path) -> None:
    def counter(kind: str, text: str) -> str:
        return text.replace("output_tokens", "image_tokens", 1) if kind == "amount" else text

    def column(kind: str, text: str) -> str:
        return text.replace("wallet_type", "wallet", 1) if kind == "cost" else text

    assert "unknown counter 'image_tokens'" in _refused(
        lambda: import_export(_export(tmp_path, counter), tmp_path / "state")
    )
    assert "columns" in _refused(lambda: import_export(_export(tmp_path, column), tmp_path / "state"))


def test_a_grant_wallet_is_a_discount_on_the_usage(tmp_path: Path) -> None:
    def granted(kind: str, text: str) -> str:
        return (
            text.replace("deepseek-flash,Paid,8.76", "deepseek-flash,Granted,8.76")
            if kind == "cost"
            else text
        )

    import_export(_export(tmp_path, granted), tmp_path / "state")
    window = Window(
        datetime(2026, 9, 20, 22, tzinfo=timezone.utc), datetime(2026, 9, 21, 22, tzinfo=timezone.utc)
    )
    (day,) = read_days(tmp_path / "state", window).days
    assert (day.gross, day.net) == (Decimal("8.7635721780000000"), Decimal(0))


def test_another_account_is_refused(tmp_path: Path) -> None:
    import_export(_export(tmp_path), tmp_path / "state")

    def other(kind: str, text: str) -> str:
        return text.replace("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002")

    assert "another DeepSeek account" in _refused(
        lambda: import_export(_export(tmp_path, other), tmp_path / "state")
    )


def test_an_older_capture_never_replaces_a_newer_one(tmp_path: Path) -> None:
    import_export(_export(tmp_path), tmp_path / "state")
    assert "this one is older" in _refused(
        lambda: import_export(_export(tmp_path, when=(2026, 9, 22, 23, 0, 0)), tmp_path / "state")
    )


def test_a_later_import_replaces_the_days_it_covers_and_keeps_the_others(tmp_path: Path) -> None:
    import_export(_export(tmp_path), tmp_path / "state")

    def last_day_only(kind: str, text: str) -> str:
        return _without("2026-09-21")(kind, _without("2026-09-20")(kind, text))

    later = _export(tmp_path, last_day_only, first="2026-09-22", when=(2026, 9, 24, 1, 0, 0))
    result = import_export(later, tmp_path / "state")
    assert (result.added, result.replaced, result.kept) == ((), ("2026-09-22T00:00:00+02:00",), 2)
    captured = {d["start"][:10]: d["captured"] for d in _store(tmp_path / "state")["days"]}
    assert captured["2026-09-20"] < captured["2026-09-22"], "the kept days keep their own capture"


def test_an_import_with_other_day_boundaries_leaves_no_overlap(tmp_path: Path) -> None:
    import_export(_export(tmp_path), tmp_path / "state")

    def utc(kind: str, text: str) -> str:
        return text.replace("+02:00", "+00:00")

    import_export(_export(tmp_path, utc, when=(2026, 9, 24, 1, 0, 0)), tmp_path / "state")
    days = sorted(_store(tmp_path / "state")["days"], key=lambda d: datetime.fromisoformat(d["start"]))
    ends = [datetime.fromisoformat(d["end"]) for d in days]
    starts = [datetime.fromisoformat(d["start"]) for d in days]
    assert all(end <= start for end, start in zip(ends, starts[1:])), "no two stored days overlap"
    assert [d["start"] for d in days] == [
        "2026-09-20T00:00:00+00:00",
        "2026-09-21T00:00:00+00:00",
        "2026-09-22T00:00:00+00:00",
    ], "every +02:00 day the UTC range touched is gone, the new days replace them"


def test_a_csv_export_needs_its_capture_time(tmp_path: Path) -> None:
    folder = tmp_path / "export"
    folder.mkdir()
    for kind in ("cost", "amount"):
        (folder / f"{kind}-{FIRST}_{LAST}.csv").write_text(_fixture(kind), encoding="utf-8")
    assert "--captured" in _refused(lambda: import_export(folder, tmp_path / "state"))
    result = import_export(folder / f"cost-{FIRST}_{LAST}.csv", tmp_path / "state", CAPTURED)
    assert len(result.added) == 3, "one file of the pair names the other"


def test_a_window_no_import_covers_is_a_missing_span_never_a_zero(tmp_path: Path) -> None:
    import_export(_export(tmp_path), tmp_path / "state")
    start = datetime(2026, 9, 21, 22, tzinfo=timezone.utc)
    report = read_days(tmp_path / "state", Window(start, start + timedelta(days=3)))
    assert [(s.start, s.end, s.why) for s in report.missing] == [
        (
            datetime(2026, 9, 22, 22, tzinfo=timezone.utc),
            start + timedelta(days=3),
            "not in any imported export",
        )
    ]


def test_the_whole_store_states_whether_its_days_are_utc_days(tmp_path: Path) -> None:
    after = datetime(2026, 9, 24, tzinfo=timezone.utc)  # a day no import has reached yet
    window = Window(after, after + timedelta(days=1))
    assert not read_days(tmp_path / "absent", window).utc_days, "no store, no zone to state"
    import_export(_export(tmp_path), tmp_path / "local")
    assert not read_days(tmp_path / "local", window).utc_days, "an export taken in +02:00"
    utc = _export(tmp_path, lambda kind, text: text.replace("+02:00", "+00:00"))
    import_export(utc, tmp_path / "utc")
    assert read_days(tmp_path / "utc", window).utc_days, "an export taken in UTC, the day not reached yet"


def test_an_unreadable_store_is_an_unreadable_span_with_its_reason(tmp_path: Path) -> None:
    path = store_path(tmp_path / "state")
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    window = Window(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 21, tzinfo=timezone.utc))
    report = read_days(tmp_path / "state", window)
    assert report.days == () and report.missing == () and "cannot be read" in report.unreadable[0].why


def test_a_broken_stored_day_is_said_and_its_neighbours_still_count(tmp_path: Path) -> None:
    import_export(_export(tmp_path), tmp_path / "state")
    stored = _store(tmp_path / "state")
    stored["days"][1]["lines"][0]["gross"] = "NaN"
    store_path(tmp_path / "state").write_text(json.dumps(stored))
    window = Window(
        datetime(2026, 9, 19, 22, tzinfo=timezone.utc), datetime(2026, 9, 22, 22, tzinfo=timezone.utc)
    )
    report = read_days(tmp_path / "state", window)
    assert len(report.days) == 2 and "stored day #2 cannot be read" in report.unreadable[0].why
    assert (report.unreadable[0].start, report.unreadable[0].end) == (
        datetime(2026, 9, 20, 22, tzinfo=timezone.utc),
        datetime(2026, 9, 21, 22, tzinfo=timezone.utc),
    ), "a broken day whose interval can be read costs that interval only"
    stored["days"][1]["start"] = "not a time"
    store_path(tmp_path / "state").write_text(json.dumps(stored))
    try:
        import_export(_export(tmp_path, when=(2026, 9, 24, 1, 0, 0)), tmp_path / "state")
    except ToolError as exc:
        assert "a stored day cannot be read" in str(exc) and "not imported" in str(exc)
    else:
        raise AssertionError("an import over a broken store must write nothing")


def test_one_import_at_a_time_and_no_temporary_file_left(tmp_path: Path) -> None:
    lock = store_path(tmp_path / "state").with_name("deepseek.json.lock")
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getpid()))  # a live process holds it
    assert "another import is writing" in _refused(
        lambda: import_export(_export(tmp_path), tmp_path / "state")
    )
    lock.unlink()
    import_export(_export(tmp_path), tmp_path / "state")
    left = sorted(p.name for p in lock.parent.iterdir())
    assert left == ["deepseek.json"], "the lock is released and the temporary file renamed into place"


def test_a_zip_whose_members_cannot_be_read_is_refused_not_raised(tmp_path: Path) -> None:
    from unittest import mock

    with mock.patch.object(zipfile.ZipFile, "read", side_effect=RuntimeError("File is encrypted")):
        said = _refused(lambda: import_export(_export(tmp_path), tmp_path / "state"))
    assert "not a readable export ZIP" in said and "encrypted" in said


def test_a_window_beside_a_broken_day_says_so_not_that_nothing_was_imported(tmp_path: Path) -> None:
    import_export(_export(tmp_path), tmp_path / "state")
    stored = _store(tmp_path / "state")
    stored["days"][1]["start"] = "not a time"
    store_path(tmp_path / "state").write_text(json.dumps(stored))
    window = Window(
        datetime(2026, 9, 19, 22, tzinfo=timezone.utc), datetime(2026, 9, 22, 22, tzinfo=timezone.utc)
    )
    report = read_days(tmp_path / "state", window)
    assert [s.why for s in report.missing] == ["not in any readable stored day"]


def test_a_zip_refuses_a_capture_time_given_by_hand(tmp_path: Path) -> None:
    said = _refused(lambda: import_export(_export(tmp_path), tmp_path / "state", CAPTURED))
    assert "a ZIP carries its own time" in said


def test_a_refusal_never_names_the_account_or_a_key(tmp_path: Path) -> None:
    def twice(kind: str, text: str) -> str:
        return text + text.splitlines()[1] + "\n" if kind == "amount" else text

    said = _refused(lambda: import_export(_export(tmp_path, twice), tmp_path / "state"))
    assert (
        "duplicated row" in said and "sk-" not in said and "test-key" not in said and "00000000-" not in said
    )


def test_two_files_of_one_kind_in_an_export_are_refused(tmp_path: Path) -> None:
    path = _export(tmp_path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(
            zipfile.ZipInfo(f"extra/cost-{FIRST}_{LAST}.csv", date_time=ZIP_TIME), _fixture("cost")
        )
    assert "two cost files" in _refused(lambda: import_export(path, tmp_path / "state"))


def test_a_zero_interval_keeps_the_accounts_currency(tmp_path: Path) -> None:
    def cny(kind: str, text: str) -> str:
        return _without("2026-09-21")(kind, text.replace(",USD", ",CNY"))

    import_export(_export(tmp_path, cny), tmp_path / "state")
    assert [d["currency"] for d in _store(tmp_path / "state")["days"]] == ["CNY", "CNY", "CNY"]


def test_a_lock_left_by_a_dead_process_is_taken_over(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True
    )
    lock = store_path(tmp_path / "state").with_name("deepseek.json.lock")
    lock.parent.mkdir(parents=True)
    lock.write_text(done.stdout.strip())  # that process has ended
    if os.name == "posix":
        assert len(import_export(_export(tmp_path), tmp_path / "state").added) == 3 and not lock.exists()


def test_a_member_whose_range_is_not_two_dates_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("cost-2026-13-45_2026-09-22.csv", date_time=ZIP_TIME), _fixture("cost")
        )
    assert "its range is not two dates" in _refused(lambda: import_export(path, tmp_path / "state"))


def test_a_failed_write_leaves_no_copy_behind(tmp_path: Path) -> None:
    from unittest import mock

    from ..providers.deepseek_export import write_store

    target = tmp_path / "state" / "providers" / "deepseek.json"
    with mock.patch.object(Path, "replace", side_effect=OSError("disk full")):
        try:
            write_store(target, {"schema": 1, "account": "", "days": []})
        except ToolError as exc:
            assert "disk full" in str(exc)
        else:
            raise AssertionError("a failed write must be said")
    assert list(target.parent.iterdir()) == []
