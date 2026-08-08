"""State must survive crashes, corruption, and concurrent writers."""

import json
from pathlib import Path

from scanner.state import (
    default_state,
    events_in_last_hours,
    load_state,
    record_event,
    record_run,
    runs_in_last_hours,
    save_state,
    source_entry,
)


def test_missing_file_yields_a_usable_default(tmp_path):
    state = load_state(tmp_path / "nope.json")
    assert state == default_state()


def test_corrupt_state_does_not_wedge_the_scanner(tmp_path):
    """A truncated write must not stop the scanner forever; it restarts clean
    and re-bootstraps rather than crash-looping."""
    path = tmp_path / "state.json"
    path.write_text('{"sources": {"film:X": {"showtimes"')
    assert load_state(path) == default_state()


def test_round_trip(tmp_path):
    path = tmp_path / "nested" / "state.json"
    state = default_state()
    source_entry(state, "film:ST1")["title"] = "Dune"
    save_state(path, state)
    assert load_state(path)["sources"]["film:ST1"]["title"] == "Dune"


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "state.json"
    for index in range(3):
        state = default_state()
        state["last_run_utc"] = f"run-{index}"
        save_state(path, state)
    assert json.loads(path.read_text())["last_run_utc"] == "run-2"
    assert [p.name for p in Path(tmp_path).iterdir()] == ["state.json"]


def test_run_log_is_bounded(tmp_path):
    state = default_state()
    for _ in range(200):
        record_run(state, http=200, showtimes=3, ok=True, limit=48)
    assert len(state["run_log"]) == 48
    assert runs_in_last_hours(state, 24) == (48, 48)


def test_stale_runs_fall_out_of_the_window():
    state = default_state()
    state["run_log"] = [{"ts": "2020-01-01T00:00:00+00:00", "ok": True}]
    assert runs_in_last_hours(state, 24) == (0, 0)


def test_events_are_bounded_and_windowed():
    state = default_state()
    for index in range(100):
        record_event(state, "added", f"event {index}", limit=60)
    assert len(state["events"]) == 60
    assert len(events_in_last_hours(state, 24)) == 60
    state["events"].append({"ts": "2020-01-01T00:00:00+00:00", "kind": "x", "text": "old"})
    assert all(e["text"] != "old" for e in events_in_last_hours(state, 24))


def test_malformed_log_entries_are_skipped():
    state = default_state()
    state["run_log"] = [{"nope": 1}, {"ts": "garbage", "ok": True}]
    assert runs_in_last_hours(state, 24) == (0, 0)
