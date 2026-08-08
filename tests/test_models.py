"""Fingerprint stability -- the property that stops duplicate alerts."""

from dataclasses import replace

from scanner.models import Showtime, dedupe, parse_datetime

TZ = "America/Indiana/Indianapolis"


def test_same_instant_written_two_ways_has_one_identity():
    naive = Showtime("F", parse_datetime("2026-12-18T19:00", TZ), format="IMAX 70mm")
    offset = Showtime("F", parse_datetime("2026-12-18T19:00:00-05:00", TZ), format="IMAX 70mm")
    utc = Showtime("F", parse_datetime("2026-12-19T00:00:00Z", TZ), format="IMAX 70mm")
    assert naive.key == offset.key == utc.key


def test_naive_times_are_venue_local_not_utc():
    """A page printing '7:00 PM' means 7pm in Indianapolis. Reading it as UTC
    would shift every showtime by five hours and re-alert on every DST change."""
    parsed = parse_datetime("2026-12-18 19:00", TZ)
    assert (parsed.hour, parsed.utcoffset().total_seconds() / 3600) == (19, -5)
    summer = parse_datetime("2026-07-04 19:00", TZ)
    assert summer.utcoffset().total_seconds() / 3600 == -4


def test_status_is_excluded_from_identity():
    show = Showtime("F", parse_datetime("2026-12-18T19:00", TZ), status="onsale")
    assert show.key == replace(show, status="soldout").key


def test_performance_id_wins_over_the_composite_hash():
    show = Showtime("F", parse_datetime("2026-12-18T19:00", TZ), performance_id="PF1")
    assert show.key == "pid:PF1"
    assert replace(show, format="anything").key == show.key


def test_different_times_are_different_showtimes():
    early = Showtime("F", parse_datetime("2026-12-18T19:00", TZ))
    late = Showtime("F", parse_datetime("2026-12-18T22:30", TZ))
    assert early.key != late.key


def test_round_trip_through_state_preserves_identity():
    show = Showtime("F", parse_datetime("2026-12-18T19:00", TZ), format="IMAX 70mm",
                    ticket_url="/t/1", status="onsale")
    assert Showtime.from_dict(show.to_dict(), TZ).key == show.key


def test_dedupe_keeps_the_richer_record():
    bare = Showtime("F", parse_datetime("2026-12-18T19:00", TZ))
    rich = Showtime("F", parse_datetime("2026-12-18T19:00", TZ), ticket_url="/t/1", format="IMAX")
    merged = dedupe([bare, rich])
    assert len(merged) == 1 and merged[0].ticket_url == "/t/1"


def test_unparseable_input_returns_none():
    for junk in ("", "soon", None, "not a date", True):
        assert parse_datetime(junk, TZ) is None
