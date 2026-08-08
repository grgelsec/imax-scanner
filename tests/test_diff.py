"""Golden diff behaviour: alert exactly once, for exactly what is new."""

from dataclasses import replace

from conftest import fixture
from scanner.diff import diff_films, diff_showtimes
from scanner.parse import parse_showtimes

TZ = "America/Indiana/Indianapolis"


def _parse(name, cfg, today):
    return parse_showtimes(fixture(name), "ST00001410", cfg, today=today).showtimes


def test_unchanged_page_reports_nothing(cfg, today):
    shows = _parse("film_jsonld.html", cfg, today)
    stored = {s.key: s.to_dict() for s in shows}
    result = diff_showtimes(stored, shows, TZ)
    assert (result.added, result.removed, result.changed) == ([], [], [])
    assert result.unchanged == len(shows)
    assert result.has_news is False


def test_one_added_showtime_is_reported_once(cfg, today):
    before = _parse("film_jsonld.html", cfg, today)
    after = _parse("film_added.html", cfg, today)
    stored = {s.key: s.to_dict() for s in before}

    first = diff_showtimes(stored, after, TZ)
    assert len(first.added) == 1
    assert first.added[0].performance_id == "PF10003"
    assert first.has_news

    # Once stored, the same page must never alert again.
    stored.update({s.key: s.to_dict() for s in after})
    assert diff_showtimes(stored, after, TZ).has_news is False


def test_sellout_is_a_change_not_an_add_and_remove(cfg, today):
    shows = _parse("film_jsonld.html", cfg, today)
    stored = {s.key: s.to_dict() for s in shows}
    flipped = [replace(shows[0], status="soldout")] + shows[1:]
    result = diff_showtimes(stored, flipped, TZ)
    assert (len(result.added), len(result.removed)) == (0, 0)
    assert [c.kinds for c in result.changed] == [["status"]]
    assert result.has_news is False  # not worth an email


def test_unknown_status_does_not_fake_a_change(cfg, today):
    """A layer that cannot read availability reports 'unknown'; that must not
    read as a sellout when the previous layer could read it."""
    shows = _parse("film_jsonld.html", cfg, today)
    stored = {s.key: s.to_dict() for s in shows}
    vague = [replace(s, status="unknown") for s in shows]
    assert diff_showtimes(stored, vague, TZ).changed == []


def test_rescheduled_showtime_is_a_change_when_ids_are_stable(cfg, today):
    shows = _parse("film_jsonld.html", cfg, today)
    stored = {s.key: s.to_dict() for s in shows}
    moved = [replace(shows[0], starts_at=shows[0].starts_at.replace(hour=20))] + shows[1:]
    result = diff_showtimes(stored, moved, TZ)
    assert len(result.added) == 0
    assert "time" in result.changed[0].kinds


def test_pulled_showtime_is_reported_as_removed(cfg, today):
    shows = _parse("film_jsonld.html", cfg, today)
    stored = {s.key: s.to_dict() for s in shows}
    result = diff_showtimes(stored, shows[:1], TZ)
    assert len(result.removed) == 1
    assert result.has_news is False


def test_empty_memory_treats_everything_as_new(cfg, today):
    shows = _parse("film_jsonld.html", cfg, today)
    assert len(diff_showtimes({}, shows, TZ).added) == len(shows)


def test_film_sweep_flags_only_matching_new_pages():
    current = {
        "ST00001410": "Dune: Part Three - The IMAX 70mm Experience",
        "ST00001433": "Dune: Part Three Fan First Premieres in IMAX",
        "ST00001440": "Oppenheimer in IMAX 70mm",
    }
    new_ids, interesting = diff_films(["ST00001410"], current, r"(?i)dune")
    assert new_ids == ["ST00001433", "ST00001440"]
    assert interesting == [("ST00001433", "Dune: Part Three Fan First Premieres in IMAX")]


def test_film_sweep_survives_a_bad_pattern():
    new_ids, interesting = diff_films([], {"ST1": "Dune"}, "(unclosed[")
    assert interesting == [("ST1", "Dune")]
