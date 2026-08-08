"""The daily 'still working' email.

Driven by local time inside the hourly run rather than by a UTC cron entry,
so these tests pin the three properties that choice buys: it lands at noon on
both sides of DST, it self-heals after a missed run, and it still goes out
when the scan itself failed.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import scanner.main as main_module
from scanner.main import ScanOutcome, SourceSummary, maybe_heartbeat
from scanner.state import default_state

TZ = ZoneInfo("America/Indiana/Indianapolis")


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True


@pytest.fixture
def freeze(monkeypatch):
    def _freeze(moment):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment
        monkeypatch.setattr(main_module, "datetime", Frozen)
    return _freeze


def _outcome(ok=True):
    summary = SourceSummary("ST00001410", "Dune: Part Three", "https://example/f", count=7,
                            first="2026-12-18", last="2026-12-20", ok=ok)
    outcome = ScanOutcome(sources=[summary])
    if not ok:
        outcome.issues.append("could not fetch the film page: HTTP 403")
    return outcome


def test_not_due_before_noon(cfg, freeze):
    freeze(datetime(2026, 8, 8, 11, 59, tzinfo=TZ))
    state, notifier = default_state(), Recorder()
    assert maybe_heartbeat(cfg, state, notifier, _outcome()) is False
    assert notifier.sent == []


def test_sends_at_noon_and_only_once_that_day(cfg, freeze):
    state, notifier = default_state(), Recorder()
    freeze(datetime(2026, 8, 8, 12, 0, tzinfo=TZ))
    assert maybe_heartbeat(cfg, state, notifier, _outcome()) is True
    assert state["last_heartbeat_date_local"] == "2026-08-08"

    freeze(datetime(2026, 8, 8, 13, 0, tzinfo=TZ))
    assert maybe_heartbeat(cfg, state, notifier, _outcome()) is False

    freeze(datetime(2026, 8, 9, 12, 0, tzinfo=TZ))
    assert maybe_heartbeat(cfg, state, notifier, _outcome()) is True
    assert len(notifier.sent) == 2


def test_a_delayed_run_still_sends_that_day(cfg, freeze):
    """GitHub's scheduler drops and delays runs; the noon email must not be
    skipped just because no run happened at exactly 12:00."""
    freeze(datetime(2026, 8, 8, 13, 40, tzinfo=TZ))
    state, notifier = default_state(), Recorder()
    assert maybe_heartbeat(cfg, state, notifier, _outcome()) is True


@pytest.mark.parametrize("month,expected_utc_hour", [(1, 17), (7, 16)])
def test_lands_at_noon_local_on_both_sides_of_dst(cfg, freeze, month, expected_utc_hour):
    """Indiana's UTC offset changes; a pinned UTC cron would drift to 11am for
    half the year. Local-time logic keeps it at noon year-round."""
    local_noon = datetime(2026, month, 15, 12, 0, tzinfo=TZ)
    assert local_noon.astimezone(ZoneInfo("UTC")).hour == expected_utc_hour
    freeze(local_noon)
    state, notifier = default_state(), Recorder()
    assert maybe_heartbeat(cfg, state, notifier, _outcome()) is True


def test_degraded_scan_still_produces_a_heartbeat(cfg, freeze):
    """A broken scanner and a silent scanner must not look the same."""
    freeze(datetime(2026, 8, 8, 12, 0, tzinfo=TZ))
    state, notifier = default_state(), Recorder()
    assert maybe_heartbeat(cfg, state, notifier, _outcome(ok=False)) is True
    message = notifier.sent[0]
    assert "DEGRADED" in message.subject
    assert "HTTP 403" in message.text


def test_force_does_not_consume_the_scheduled_send(cfg, freeze):
    freeze(datetime(2026, 8, 8, 9, 0, tzinfo=TZ))
    state, notifier = default_state(), Recorder()
    assert maybe_heartbeat(cfg, state, notifier, _outcome(), force=True) is True
    assert state["last_heartbeat_date_local"] is None
    freeze(datetime(2026, 8, 8, 12, 0, tzinfo=TZ))
    assert maybe_heartbeat(cfg, state, notifier, _outcome()) is True


def test_digest_reports_run_counts_and_window(cfg, freeze):
    freeze(datetime(2026, 8, 8, 12, 0, tzinfo=TZ))
    state, notifier = default_state(), Recorder()
    state["run_log"] = [{"ts": datetime.now(ZoneInfo("UTC")).isoformat(), "http": 200,
                         "showtimes": 7, "ok": True}]
    maybe_heartbeat(cfg, state, notifier, _outcome())
    text = notifier.sent[0].text
    assert "expect ~24" in text
    assert "7 showtime(s) on sale (2026-12-18 through 2026-12-20)" in text
    assert "If this email stops arriving" in text
