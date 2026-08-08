"""Each parser layer, plus the empty-vs-broken distinction the alerts rely on."""

import pytest

from conftest import fixture
from scanner.parse import looks_like_showtimes, parse_film_index, parse_showtimes


@pytest.mark.parametrize(
    "name,expected_layer,expected_count",
    [
        ("film_jsonld.html", "json-ld", 2),
        ("film_nextdata.html", "embedded-state", 2),
        ("film_dom.html", "dom", 4),
        ("film_time_tags.html", "dom", 2),
    ],
)
def test_each_layer_extracts_showtimes(cfg, today, name, expected_layer, expected_count):
    result = parse_showtimes(fixture(name), "ST00001410", cfg, today=today)
    assert result.layer == expected_layer
    assert len(result.showtimes) == expected_count
    assert all(show.ticket_url for show in result.showtimes)
    assert all(show.starts_at.year == 2026 for show in result.showtimes)


def test_movie_metadata_is_not_mistaken_for_a_screening(cfg, today):
    """schema.org Movie carries releaseDate/uploadDate/datePublished; a naive
    date-key walk would invent three phantom showtimes from them."""
    result = parse_showtimes(fixture("film_jsonld.html"), "ST00001410", cfg, today=today)
    assert len(result.showtimes) == 2
    assert {show.performance_id for show in result.showtimes} == {"PF10001", "PF10002"}


def test_format_prefers_the_specific_label(cfg, today):
    result = parse_showtimes(fixture("film_dom.html"), "ST00001410", cfg, today=today)
    assert {show.format for show in result.showtimes} == {"IMAX 70mm"}


def test_sold_out_does_not_leak_to_sibling_showtimes(cfg, today):
    """Only the 5:00 PM show is flagged sold out in the fixture, even though it
    shares a <li> with the 1:30 PM one."""
    shows = parse_showtimes(fixture("film_dom.html"), "ST00001410", cfg, today=today).showtimes
    by_time = {show.starts_at.strftime("%H:%M"): show.status for show in shows}
    assert by_time["17:00"] == "soldout"
    assert by_time["13:30"] != "soldout"


def test_sold_out_read_from_jsonld_offers(cfg, today):
    shows = parse_showtimes(fixture("film_jsonld.html"), "ST00001410", cfg, today=today).showtimes
    status = {show.performance_id: show.status for show in shows}
    assert status == {"PF10001": "onsale", "PF10002": "soldout"}


def test_empty_page_is_not_reported_as_broken(cfg, today):
    """A film with no showtimes yet must stay silent -- no alert, no false news."""
    result = parse_showtimes(fixture("film_no_showtimes.html"), "ST00001410", cfg, today=today)
    assert result.empty
    assert result.saw_showtime_text is False


def test_broken_page_is_distinguishable_from_an_empty_one(cfg, today):
    """Times visible but nothing parsed -- this is what triggers the alert."""
    result = parse_showtimes(fixture("film_broken.html"), "ST00001410", cfg, today=today)
    assert result.empty
    assert result.saw_showtime_text is True


def test_looks_like_showtimes_ignores_script_and_style():
    assert looks_like_showtimes("<style>a{margin:7:00}</style><p>nothing here</p>") is False
    assert looks_like_showtimes("<p>7:00 PM</p><p>10:30 PM</p>") is True


def test_film_index_maps_ids_to_titles(cfg):
    films = parse_film_index(fixture("films_index.html"), cfg)
    assert films["ST00001410"].startswith("Dune: Part Three")
    assert set(films) == {"ST00001410", "ST00001426", "ST00001268"}


def test_parser_never_raises_on_garbage(cfg, today):
    for junk in ("", "<html>", "<<<>>>", "not html at all", "<time datetime='nope'></time>"):
        result = parse_showtimes(junk, "ST00001410", cfg, today=today)
        assert result.showtimes == []


def test_purchase_widget_without_parseable_times_is_a_fault_not_an_empty_page(cfg, today):
    """A client-rendered page carries a seat/quantity picker but no clock text.
    Without this, it looks identical to 'no showtimes on sale' and the scanner
    would stay silent forever -- the exact failure this monitor exists to avoid.
    """
    result = parse_showtimes(fixture("film_js_shell.html"), "ST00001410", cfg, today=today)
    assert result.empty
    assert result.saw_showtime_text is True


def test_generic_buy_tickets_wording_alone_does_not_cry_wolf(cfg, today):
    """Site-wide nav saying 'Buy Tickets' must not flag a genuinely empty page."""
    html = "<html><body><nav>Buy Tickets</nav><p>Opening December 18.</p></body></html>"
    assert parse_showtimes(html, "ST00001410", cfg, today=today).saw_showtime_text is False
