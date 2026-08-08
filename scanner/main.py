"""Entry point: jitter -> fetch -> parse -> diff -> notify -> heartbeat -> persist."""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .diff import diff_films, diff_showtimes
from .fetch import Fetcher
from .notify import (
    Notifier,
    bootstrap_message,
    heartbeat_message,
    new_films_message,
    new_showtimes_message,
    parser_alert_message,
    recovery_message,
    short_title,
    test_message,
)
from .models import parse_datetime
from .parse import parse_film_index, parse_showtimes
from .state import (
    events_in_last_hours,
    load_state,
    record_event,
    record_run,
    runs_in_last_hours,
    save_state,
    source_entry,
)

log = logging.getLogger("imax-scanner")
EXPECTED_RUNS_PER_DAY = 24


@dataclass
class SourceSummary:
    film_id: str
    title: str
    url: str
    count: int = 0
    first: str = ""
    last: str = ""
    ok: bool = True
    http: int | None = None
    issue: str = ""


@dataclass
class ScanOutcome:
    sources: list[SourceSummary] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.issues and all(source.ok for source in self.sources)

    @property
    def total_showtimes(self) -> int:
        return sum(source.count for source in self.sources)


# --- persistence helpers ----------------------------------------------------
def _store_showtimes(entry: dict, showtimes, now_iso: str) -> None:
    previous = entry.get("showtimes", {})
    current = {}
    for show in showtimes:
        payload = show.to_dict()
        payload["first_seen"] = (previous.get(show.key) or {}).get("first_seen") or now_iso
        payload["last_seen"] = now_iso
        current[show.key] = payload
    entry["showtimes"] = current


def _future_count(stored: dict, tz: str) -> int:
    """How many tracked showtimes are still ahead of us?"""
    now = datetime.now(ZoneInfo(tz))
    pending = 0
    for payload in (stored or {}).values():
        moment = parse_datetime(payload.get("datetime_local"), tz)
        if moment is not None and moment > now:
            pending += 1
    return pending


def _note_failure(cfg, state, entry, summary, notifier, reason: str, detail: str) -> None:
    """Count a failure and alert exactly once, on crossing the threshold."""
    entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
    summary.ok = False
    summary.issue = f"{reason}: {detail}" if detail else reason
    count = entry["consecutive_failures"]
    log.warning("%s (%s) - consecutive failures: %s", reason, detail, count)
    if count == cfg.failure_alert_threshold and not entry.get("alerted"):
        entry["alerted"] = True
        notifier.send(parser_alert_message(summary.url, reason, f"{detail} (x{count} in a row)"))
        record_event(state, "failure",
                     f"{short_title(summary.title, summary.film_id)}: {reason}")


def _note_success(entry, state, summary, notifier, count: int) -> None:
    if entry.get("alerted"):
        entry["alerted"] = False
        notifier.send(recovery_message(summary.url, count))
        record_event(state, "recovery",
                     f"{short_title(summary.title, summary.film_id)}: parsing recovered")
    entry["consecutive_failures"] = 0


# --- the scan ---------------------------------------------------------------
def scan_film(cfg, state, notifier, fetcher, film_id: str, now_iso: str) -> SourceSummary:
    entry = source_entry(state, f"film:{film_id}")
    url = cfg.film_url(film_id)
    summary = SourceSummary(film_id=film_id, title=entry.get("title", ""), url=url)

    response = fetcher.get(url, entry.get("etag", ""), entry.get("last_modified", ""))
    summary.http = response.status

    if response.not_modified:
        stored = entry.get("showtimes", {})
        summary.count = len(stored)
        _summarize_window(summary, stored)
        _note_success(entry, state, summary, notifier, summary.count)
        log.info("%s unchanged (304), %s showtime(s) tracked", film_id, summary.count)
        return summary

    if not response.ok:
        _note_failure(cfg, state, entry, summary, notifier, "could not fetch the film page",
                      response.error or "unknown error")
        return summary

    if response.etag:
        entry["etag"] = response.etag
    if response.last_modified:
        entry["last_modified"] = response.last_modified

    parsed = parse_showtimes(response.text, film_id, cfg)
    if parsed.title:
        entry["title"] = parsed.title
        summary.title = parsed.title
    stored = entry.get("showtimes", {})
    entry["content_hash"] = response.hash

    if parsed.empty:
        # The page loaded. Is it genuinely empty, or did our parsers break?
        if parsed.saw_showtime_text:
            _note_failure(cfg, state, entry, summary, notifier,
                          "page shows times but none could be parsed",
                          "the listing markup likely changed")
            return summary
        # Losing showtimes that have not happened yet is far more likely a
        # redesign than a cancellation. A run simply ending is the benign
        # case, and there every tracked showtime is already in the past.
        pending = _future_count(stored, cfg.local_tz)
        if pending:
            _note_failure(cfg, state, entry, summary, notifier,
                          "upcoming showtimes disappeared from the page",
                          f"{pending} future showtime(s) were tracked before this run")
            return summary
        log.info("%s: no showtimes listed yet (page looks genuinely empty)", film_id)
        _note_success(entry, state, summary, notifier, 0)
        _store_showtimes(entry, [], now_iso)
        entry["bootstrapped"] = True
        return summary

    _note_success(entry, state, summary, notifier, len(parsed.showtimes))
    summary.count = len(parsed.showtimes)
    log.info("%s: parsed %s showtime(s) via %s", film_id, summary.count, parsed.layer)

    if not entry.get("bootstrapped"):
        # First sight of this film: record a baseline instead of alerting on
        # every showtime that was already on sale before we started watching.
        notifier.send(bootstrap_message(summary.title, url, parsed.showtimes))
        record_event(state, "bootstrap",
                     f"started watching {short_title(summary.title, film_id)} "
                     f"({summary.count} showtimes)")
        entry["bootstrapped"] = True
    else:
        changes = diff_showtimes(stored, parsed.showtimes, cfg.local_tz)
        if changes.has_news:
            notifier.send(new_showtimes_message(summary.title, url, changes))
            record_event(state, "added",
                         f"{len(changes.added)} new showtime(s): "
                         + ", ".join(s.describe() for s in changes.added[:5]))
        elif changes.any_movement:
            # Removals and sold-outs are logged for the daily digest, not emailed.
            if changes.removed:
                record_event(state, "removed", f"{len(changes.removed)} showtime(s) no longer listed")
            for change in changes.changed:
                record_event(state, "changed", change.describe())

    _store_showtimes(entry, parsed.showtimes, now_iso)
    _summarize_window(summary, entry["showtimes"])
    return summary


def _summarize_window(summary: SourceSummary, stored: dict) -> None:
    dates = sorted(
        value.get("datetime_local", "") for value in stored.values() if value.get("datetime_local")
    )
    if dates:
        summary.first = dates[0][:10]
        summary.last = dates[-1][:10]


def scan_films_index(cfg, state, notifier, fetcher) -> list[str]:
    """Watch for brand-new film pages matching the watch pattern."""
    entry = source_entry(state, "films-index")
    url = cfg.films_index_url
    response = fetcher.get(url, entry.get("etag", ""), entry.get("last_modified", ""))
    if response.not_modified:
        return []
    if not response.ok:
        log.warning("films index fetch failed: %s", response.error)
        return [f"films index unreachable ({response.error})"]

    if response.etag:
        entry["etag"] = response.etag
    if response.last_modified:
        entry["last_modified"] = response.last_modified

    films = parse_film_index(response.text, cfg)
    if not films:
        log.warning("films index parsed to zero films")
        return ["films index returned no film links (layout may have changed)"]

    new_ids, interesting = diff_films(state.get("known_film_ids", []), films, cfg.watch_pattern)
    if entry.get("bootstrapped") and interesting:
        notifier.send(new_films_message(interesting, cfg.site_url, cfg.film_url))
        record_event(state, "new-film",
                     "new film page(s): " + ", ".join(f"{t or i} ({i})" for i, t in interesting))
    entry["bootstrapped"] = True
    state["known_film_ids"] = sorted(set(state.get("known_film_ids", [])) | set(films))
    log.info("films index: %s film(s) known, %s new, %s matching watch pattern",
             len(state["known_film_ids"]), len(new_ids), len(interesting))
    return []


def scan_all(cfg, state, notifier, fetcher) -> ScanOutcome:
    outcome = ScanOutcome()
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for film_id in cfg.film_ids:
        outcome.sources.append(scan_film(cfg, state, notifier, fetcher, film_id, now_iso))
    outcome.issues.extend(scan_films_index(cfg, state, notifier, fetcher))
    outcome.issues.extend(s.issue for s in outcome.sources if s.issue)
    return outcome


# --- heartbeat --------------------------------------------------------------
def maybe_heartbeat(cfg, state, notifier, outcome: ScanOutcome, force: bool = False) -> bool:
    """Send the daily 'still working' digest at the first run at/after noon local.

    Deliberately driven by local time rather than a cron entry: GitHub's
    scheduler is UTC-only, so a pinned UTC hour would drift by one when Indiana
    changes offset. Comparing dates also means a delayed or dropped noon run
    still sends at 1pm instead of skipping the day.
    """
    local_now = datetime.now(ZoneInfo(cfg.local_tz))
    today = local_now.date().isoformat()
    last_sent = state.get("last_heartbeat_date_local")
    due = force or (local_now.hour >= cfg.heartbeat_hour and (last_sent is None or last_sent < today))
    if not due:
        return False

    runs_ok, runs_total = runs_in_last_hours(state, 24)
    last_success = ""
    for run in reversed(state.get("run_log", [])):
        if run.get("ok"):
            last_success = f"{run['ts']} (HTTP {run.get('http')})"
            break

    failures = max(
        [source.get("consecutive_failures", 0) for source in state.get("sources", {}).values()] or [0]
    )
    state["consecutive_parse_failures"] = failures

    sources = [
        {"title": short_title(s.title, s.film_id), "url": s.url,
         "count": s.count, "first": s.first, "last": s.last}
        for s in outcome.sources
    ] or [{"title": film_id, "url": cfg.film_url(film_id), "count": 0, "first": "", "last": ""}
          for film_id in cfg.film_ids]

    notifier.send(
        heartbeat_message(
            healthy=outcome.healthy and failures == 0,
            local_now=local_now,
            sources=sources,
            runs_ok=runs_ok,
            runs_total=runs_total,
            expected_runs=EXPECTED_RUNS_PER_DAY,
            last_success=last_success,
            failures=failures,
            events=events_in_last_hours(state, 24),
            film_url=cfg.film_url(cfg.film_ids[0]) if cfg.film_ids else cfg.site_url,
            issues=outcome.issues,
        )
    )
    if not force:
        state["last_heartbeat_date_local"] = today
    return True


# --- cli --------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scanner", description=__doc__)
    parser.add_argument("--state", help="path to the state file (default from STATE_PATH)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent; write no state, send no email")
    parser.add_argument("--no-jitter", action="store_true", help="skip the startup sleep")
    parser.add_argument("--test-email", action="store_true", help="send one test email and exit")
    parser.add_argument("--force-heartbeat", action="store_true",
                        help="send the daily digest now, regardless of the time")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config.from_env()
    notifier = Notifier(cfg, dry_run=args.dry_run)

    if args.test_email:
        return 0 if notifier.send(test_message()) else 1

    state_path = args.state or cfg.state_path
    state = load_state(state_path)

    if not args.no_jitter and not args.dry_run and cfg.jitter_seconds > 0:
        delay = random.uniform(0, cfg.jitter_seconds)
        log.info("sleeping %.0fs before fetching (spreads load off the top of the hour)", delay)
        time.sleep(delay)

    outcome, scan_error = ScanOutcome(), None
    try:
        outcome = scan_all(cfg, state, notifier, Fetcher(cfg))
    except Exception as exc:  # noqa: BLE001 - the heartbeat must still go out
        scan_error = exc
        log.exception("scan failed")
        outcome.issues.append(f"scan raised {type(exc).__name__}: {exc}")

    record_run(
        state,
        http=outcome.sources[0].http if outcome.sources else None,
        showtimes=outcome.total_showtimes,
        ok=scan_error is None and outcome.healthy,
        limit=cfg.run_log_limit,
    )
    # Always attempt the heartbeat, even after a failed scan: a broken scanner
    # and a silent scanner must not look the same from the inbox.
    try:
        maybe_heartbeat(cfg, state, notifier, outcome, force=args.force_heartbeat)
    except Exception:  # noqa: BLE001
        log.exception("heartbeat failed")

    state["last_run_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if not args.dry_run:
        save_state(state_path, state)
        log.info("state written to %s", state_path)

    log.info("done: %s showtime(s) tracked, %s email(s) queued, healthy=%s",
             outcome.total_showtimes, len(notifier.sent), outcome.healthy)
    return 1 if scan_error is not None or not outcome.healthy else 0


if __name__ == "__main__":
    sys.exit(main())
