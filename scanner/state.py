"""Atomic load/save of the scanner's memory between runs."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def default_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_run_utc": None,
        "known_film_ids": [],
        "consecutive_parse_failures": 0,
        "last_heartbeat_date_local": None,
        "run_log": [],
        "sources": {},
    }


def load_state(path: str | os.PathLike) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        return default_state()
    try:
        loaded = json.loads(file_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # A corrupt state file must not wedge the scanner forever. Start clean;
        # the run is treated as a bootstrap, so no false flood of alerts.
        log.error("state file %s unreadable (%s); starting fresh", file_path, exc)
        return default_state()
    state = default_state()
    state.update(loaded if isinstance(loaded, dict) else {})
    state["schema_version"] = SCHEMA_VERSION
    return state


def save_state(path: str | os.PathLike, state: dict) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, file_path)  # atomic: never leaves a half-written state
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def source_entry(state: dict, key: str) -> dict:
    return state.setdefault("sources", {}).setdefault(key, {"showtimes": {}})


def record_run(state: dict, *, http: int | None, showtimes: int, ok: bool, limit: int = 48) -> None:
    log_entries = state.setdefault("run_log", [])
    log_entries.append(
        {
            "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "http": http,
            "showtimes": showtimes,
            "ok": ok,
        }
    )
    del log_entries[:-limit]


def runs_in_last_hours(state: dict, hours: int = 24) -> tuple[int, int]:
    """(successful runs, total runs) inside the window -- powers the heartbeat."""
    now = datetime.now(timezone.utc)
    total = ok = 0
    for entry in state.get("run_log", []):
        try:
            stamp = datetime.fromisoformat(entry["ts"])
        except (KeyError, ValueError):
            continue
        if (now - stamp).total_seconds() <= hours * 3600:
            total += 1
            ok += bool(entry.get("ok"))
    return ok, total


def record_event(state: dict, kind: str, text: str, limit: int = 60) -> None:
    """Append a human-readable event so the daily heartbeat can recap 24h."""
    events = state.setdefault("events", [])
    events.append(
        {
            "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "kind": kind,
            "text": text,
        }
    )
    del events[:-limit]


def events_in_last_hours(state: dict, hours: int = 24) -> list[dict]:
    now = datetime.now(timezone.utc)
    recent = []
    for event in state.get("events", []):
        try:
            stamp = datetime.fromisoformat(event["ts"])
        except (KeyError, ValueError):
            continue
        if (now - stamp).total_seconds() <= hours * 3600:
            recent.append(event)
    return recent
