"""Whole-run behaviour: what actually lands in the inbox, and when.

The contract being pinned here is that silence means "nothing new" -- never
"the scraper broke". Every path that could quietly stop reporting has a test.
"""

import pytest

from conftest import fixture
from scanner.fetch import FetchResult
from scanner.main import scan_film, scan_films_index
from scanner.state import default_state

NOW = "2026-08-08T17:00:00+00:00"


class StubFetcher:
    """Serves canned pages; `page` is swapped between runs to simulate edits."""

    def __init__(self, page="film_jsonld.html", index="films_index.html"):
        self.page, self.index = page, index
        self.result = None
        self.requests = []

    def get(self, url, etag="", last_modified=""):
        self.requests.append(url)
        if self.result is not None:
            return self.result
        body = fixture(self.index if url.rstrip("/").endswith("/films") else self.page)
        return FetchResult(url=url, status=200, text=body, headers={})


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True


@pytest.fixture
def run(cfg):
    state = default_state()

    def _run(fetcher, notifier=None):
        notifier = notifier or Recorder()
        summary = scan_film(cfg, state, notifier, fetcher, "ST00001410", NOW)
        return summary, notifier, state

    return _run


def test_first_run_sends_one_baseline_email_not_one_per_showtime(run):
    summary, notifier, state = run(StubFetcher())
    assert len(notifier.sent) == 1
    assert "Now watching" in notifier.sent[0].subject
    assert summary.count == 2
    assert state["sources"]["film:ST00001410"]["bootstrapped"] is True


def test_second_identical_run_is_silent(run):
    fetcher = StubFetcher()
    run(fetcher)
    _, notifier, _ = run(fetcher)
    assert notifier.sent == []


def test_added_showtime_produces_exactly_one_alert(run):
    fetcher = StubFetcher()
    run(fetcher)
    fetcher.page = "film_added.html"
    summary, notifier, _ = run(fetcher)
    assert len(notifier.sent) == 1
    assert "1 new showtime" in notifier.sent[0].subject
    assert "PF10003" in notifier.sent[0].text
    assert summary.count == 3
    # and it must not alert a second time for the same showtime
    _, quiet, _ = run(fetcher)
    assert quiet.sent == []


def test_broken_parser_alerts_once_on_the_third_strike(run, cfg):
    fetcher = StubFetcher()
    run(fetcher)
    fetcher.page = "film_broken.html"

    for attempt in range(1, cfg.failure_alert_threshold):
        summary, notifier, state = run(fetcher)
        assert notifier.sent == [], f"alerted too early on attempt {attempt}"
        assert summary.ok is False

    summary, notifier, state = run(fetcher)
    assert len(notifier.sent) == 1
    assert "needs attention" in notifier.sent[0].subject

    _, quiet, _ = run(fetcher)
    assert quiet.sent == [], "must not re-alert every hour while broken"


def test_recovery_is_announced(run, cfg):
    fetcher = StubFetcher()
    run(fetcher)
    fetcher.page = "film_broken.html"
    for _ in range(cfg.failure_alert_threshold):
        run(fetcher)
    fetcher.page = "film_jsonld.html"
    _, notifier, state = run(fetcher)
    assert any("recovered" in m.subject for m in notifier.sent)
    assert state["sources"]["film:ST00001410"]["consecutive_failures"] == 0


def test_genuinely_empty_page_never_alerts(run):
    """No showtimes on sale yet is normal, not a fault."""
    fetcher = StubFetcher(page="film_no_showtimes.html")
    summary, notifier, _ = run(fetcher)
    assert summary.ok is True
    assert not any("needs attention" in m.subject for m in notifier.sent)


def test_showtimes_vanishing_after_a_page_change_is_treated_as_a_fault(run, cfg):
    """Seven tracked showtimes becoming zero is far more likely a redesign
    than the venue silently cancelling the run."""
    fetcher = StubFetcher()
    run(fetcher)
    fetcher.page = "film_no_showtimes.html"
    for _ in range(cfg.failure_alert_threshold):
        summary, notifier, _ = run(fetcher)
    assert summary.ok is False
    assert any("needs attention" in m.subject for m in notifier.sent)


def test_not_modified_keeps_the_tracked_showtimes(run):
    fetcher = StubFetcher()
    run(fetcher)
    fetcher.result = FetchResult(url="x", status=304, not_modified=True)
    summary, notifier, _ = run(fetcher)
    assert summary.count == 2
    assert summary.ok is True
    assert notifier.sent == []


def test_fetch_failure_counts_toward_the_alert(run, cfg):
    fetcher = StubFetcher()
    run(fetcher)
    fetcher.result = FetchResult(url="x", status=403, error="HTTP 403")
    for _ in range(cfg.failure_alert_threshold):
        summary, notifier, state = run(fetcher)
    assert summary.ok is False
    assert "HTTP 403" in notifier.sent[0].text


def test_removed_showtime_is_logged_but_not_emailed(run):
    fetcher = StubFetcher(page="film_added.html")
    run(fetcher)
    fetcher.page = "film_jsonld.html"
    _, notifier, state = run(fetcher)
    assert notifier.sent == []
    assert any(event["kind"] == "removed" for event in state["events"])


def test_film_sweep_is_silent_on_its_first_pass(cfg):
    """Bootstrapping the index must not email about every film already listed."""
    state, notifier = default_state(), Recorder()
    scan_films_index(cfg, state, notifier, StubFetcher())
    assert notifier.sent == []
    assert "ST00001410" in state["known_film_ids"]


def test_film_sweep_alerts_on_a_new_dune_page(cfg, tmp_path):
    state, notifier = default_state(), Recorder()
    fetcher = StubFetcher()
    scan_films_index(cfg, state, notifier, fetcher)

    extra = fixture("films_index.html").replace(
        "</ul>",
        '<li><a href="/sites/indyimax/films/ST00001433">'
        "Dune: Part Three Fan First Premieres in IMAX</a></li></ul>",
    )
    (tmp_path / "index2.html").write_text(extra)

    class Swapped(StubFetcher):
        def get(self, url, etag="", last_modified=""):
            return FetchResult(url=url, status=200, text=extra, headers={})

    scan_films_index(cfg, state, notifier, Swapped())
    assert len(notifier.sent) == 1
    assert "ST00001433" in notifier.sent[0].text
    assert "ST00001433" in state["known_film_ids"]


def test_a_finished_run_going_empty_is_not_a_fault(run, cfg):
    """The benign mirror of the test above: when a film's run ends, every
    tracked showtime is already in the past, so an empty page is expected
    and must not raise an alert."""
    fetcher = StubFetcher()
    _, _, state = run(fetcher)
    stored = state["sources"]["film:ST00001410"]["showtimes"]
    for payload in stored.values():
        payload["datetime_local"] = payload["datetime_local"].replace("2026-12", "2020-12")

    fetcher.page = "film_no_showtimes.html"
    for _ in range(cfg.failure_alert_threshold + 1):
        summary, notifier, _ = run(fetcher)
    assert summary.ok is True
    assert notifier.sent == []


def test_only_additions_send_mail(run, cfg):
    """The whole alerting contract in one test: a screening appearing emails
    once; that same screening becoming purchasable does not."""
    fetcher = StubFetcher(page="live_film_odyssey.html")
    run(fetcher)                                    # baseline

    announced_gone = fixture("live_film_odyssey.html").replace(
        '<span class="button hollow disabled has-tip top" data-tooltip aria-haspopup="true" '
        'data-click-open="false" data-disable-hover="false" title="Tickets Coming Soon">2:45PM</span>',
        '<a href="https://ticketing.uswest.veezi.com/purchase/22459?siteToken=h1x" '
        'class="button  veezi-buy" title="The Odyssey: The IMAX 70mm Experience at 2:45PM on '
        'Aug. 8, 2026">2:45PM</a>')

    class Swapped(StubFetcher):
        def get(self, url, etag="", last_modified=""):
            return FetchResult(url=url, status=200, text=announced_gone, headers={})

    _, notifier, state = run(Swapped())
    assert notifier.sent == [], "tickets opening must not interrupt"
    assert any(event["kind"] == "on-sale" for event in state["events"]), "but it is still recorded"


class FailingRecorder(Recorder):
    """A notifier whose mail never leaves -- e.g. a mistyped app password."""

    def send(self, message):
        self.sent.append(message)
        return False


def test_undelivered_alert_is_not_recorded_as_seen(run, cfg):
    """The failure that would have cost the first real email: if SMTP rejects
    the login, the showtimes must NOT be written to state. Otherwise they
    become the baseline, the alert is lost forever, and the run still claims
    to be healthy."""
    fetcher = StubFetcher()
    run(fetcher)                                     # baseline, delivered fine
    fetcher.page = "film_added.html"

    summary, notifier, state = run(fetcher, FailingRecorder())
    assert len(notifier.sent) == 1                   # it tried
    assert summary.ok is False                       # and reported the failure
    stored = state["sources"]["film:ST00001410"]["showtimes"]
    assert len(stored) == 2, "the new showtime must not be recorded as seen"


def test_the_retry_sends_exactly_once_when_mail_recovers(run, cfg):
    fetcher = StubFetcher()
    run(fetcher)
    fetcher.page = "film_added.html"

    run(fetcher, FailingRecorder())                  # mail broken
    run(fetcher, FailingRecorder())                  # still broken, still pending

    _, notifier, state = run(fetcher)                # mail works again
    assert len(notifier.sent) == 1
    assert "1 new showtime" in notifier.sent[0].subject
    assert len(state["sources"]["film:ST00001410"]["showtimes"]) == 3

    _, quiet, _ = run(fetcher)                       # and never again
    assert quiet.sent == []


def test_undelivered_baseline_retries(run, cfg):
    """A bootstrap that could not be sent must not mark the film bootstrapped,
    or the baseline is silently adopted and never announced."""
    fetcher = StubFetcher()
    _, _, state = run(fetcher, FailingRecorder())
    assert state["sources"]["film:ST00001410"].get("bootstrapped") is not True

    _, notifier, state = run(fetcher)
    assert "Now watching" in notifier.sent[0].subject
    assert state["sources"]["film:ST00001410"]["bootstrapped"] is True


def test_undelivered_new_film_alert_is_not_marked_known(cfg):
    """Same rule for the films sweep: don't claim to know a film until its
    alert has actually gone out."""
    state, notifier = default_state(), Recorder()
    scan_films_index(cfg, state, notifier, StubFetcher())     # bootstrap, silent

    extra = fixture("films_index.html").replace(
        "</ul>", '<li><a href="/sites/indyimax/films/ST00001433">Dune: Part Three '
                 'Fan First Premieres in IMAX</a></li></ul>')

    class Swapped(StubFetcher):
        def get(self, url, etag="", last_modified=""):
            return FetchResult(url=url, status=200, text=extra, headers={})

    issues = scan_films_index(cfg, state, notifier, Swapped(), )
    assert "ST00001433" in state["known_film_ids"]

    state2, failing = default_state(), FailingRecorder()
    scan_films_index(cfg, state2, failing, StubFetcher())
    issues = scan_films_index(cfg, state2, failing, Swapped())
    assert "ST00001433" not in state2["known_film_ids"], "must retry next run"
    assert issues


def test_subject_puts_the_new_dates_before_the_film_name(run, cfg):
    """The dates are the payload -- they are what tells you whether anything
    beyond what you already know about has appeared. Phone lock screens cut
    the subject around 45 characters, so the count and dates must come first
    and the venue boilerplate must not push them off the end."""
    fetcher = StubFetcher()
    run(fetcher)
    fetcher.page = "film_added.html"
    _, notifier, _ = run(fetcher)

    subject = notifier.sent[0].subject
    assert "Dec 20" in subject[:45], f"date lost to truncation: {subject!r}"
    assert subject.index("Dec 20") < subject.index("Dune"), "dates must precede the film name"
    assert "IMAX 70mm Experience" not in subject, "venue boilerplate must be stripped"
