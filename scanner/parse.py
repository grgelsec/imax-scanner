"""Extract showtimes from whatever the page happens to be made of.

Layers are tried in order of durability and the first one that yields a
showtime wins:

  1. JSON API payload      (if discovery found an endpoint)
  2. JSON-LD ScreeningEvent
  3. Embedded state blob   (__NEXT_DATA__ / __NUXT__ / __INITIAL_STATE__)
  4. HTML DOM
  5. regex sweep -- DETECTION ONLY, never used to build records

Layer 5 exists so that "the page has no showtimes" and "our selectors broke"
are distinguishable. That distinction is the whole reason this monitor can be
trusted when it stays quiet.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .models import Showtime, dedupe, parse_datetime

try:  # DOM layer is optional; the rest works without bs4 installed.
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

log = logging.getLogger(__name__)

# --- key vocabularies -------------------------------------------------------
DATE_KEYS = (
    "startdate", "starttime", "startsat", "start", "showtime", "showdatetime",
    "showdate", "datetime", "date_time", "performancetime", "performancedate",
    "sessiontime", "sessiondatetime", "screeningtime", "beginsat", "when", "date",
)
# Date-ish keys that are definitely NOT screenings. schema.org Movie objects
# carry several of these and would otherwise parse as phantom showtimes.
DATE_KEY_BLOCKLIST = {
    "releasedate", "enddate", "expirydate", "expiresat", "createdat", "updatedat",
    "modifiedat", "publishedat", "datepublished", "datemodified", "datecreated",
    "uploaddate", "closingdate", "validfrom", "validthrough", "availablefrom",
    "availableto", "openingdate", "birthdate", "lastmodified", "printeddate",
}
ID_KEYS = ("performanceid", "sessionid", "showtimeid", "screeningid", "eventid",
           "performancecode", "sessioncode", "id", "code", "uid")
URL_KEYS = ("ticketurl", "bookingurl", "purchaseurl", "buyurl", "url", "link", "href", "permalink")
FORMAT_KEYS = ("format", "filmformat", "presentation", "experience", "versionname",
               "version", "attributes", "printtype", "medium", "projection")
ROOM_KEYS = ("auditorium", "screen", "screenname", "theatre", "theater", "hall", "room", "venue")
STATUS_KEYS = ("status", "availability", "soldout", "issoldout", "available",
               "isavailable", "onsale", "sellingstatus", "seatsavailable")

PATH_HINTS = ("showtime", "session", "performance", "screening", "showing",
              "event", "times", "schedule", "shows")

# Minutes are optional: this venue writes "11AM" as often as "10:15PM".
TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?\b"   # 6:30PM, 11AM, 7 p.m.
    r"|\b([01]?\d|2[0-3]):([0-5]\d)\b"                     # 19:00
)
# TickMarq fronts Veezi. Ticket links carry the Veezi session id, and each
# button's title spells out the whole datetime -- far sturdier to read than
# inferring a date from whichever heading happens to sit above the link.
VEEZI_TITLE_RE = re.compile(r"(?i)\bat\s+(\d{1,2}(?::\d{2})?\s*[AP]\.?M\.?)\s+on\s+(.+?)\s*$")
VEEZI_PURCHASE_RE = re.compile(r"(?i)veezi\.com/purchase/(\d+)")
TICKET_HREF_RE = re.compile(r"(?i)/(tickets?|showtimes?|performances?|sessions?|booking|order|seats)\b")
FORMAT_HINT_RE = re.compile(
    r"(?i)\b(IMAX\s*(?:70|35)\s*mm(?:\s+film)?|IMAX\s+with\s+Laser|IMAX|70\s*mm|35\s*mm"
    r"|Dolby\s+Cinema|Dolby|Digital|3-?D|2-?D|Laser)\b"
)
# Ticket links usually carry the platform's performance id; reusing it keeps a
# showtime's identity stable even if its listed time or format is edited.
TICKET_ID_RE = re.compile(r"(?i)/(?:tickets?|performances?|sessions?|showtimes?)/([A-Za-z0-9_-]{3,})")
SOLDOUT_RE = re.compile(r"(?i)\b(sold\s*out|unavailable|no\s+seats|not\s+available)\b")
# Text that only appears once a *specific performance* is purchasable. A page
# carrying these while yielding zero parsed showtimes is broken, not empty --
# generic "buy tickets" nav wording is deliberately excluded as too weak.
PURCHASE_FLOW_RE = re.compile(
    r"(?i)(select\s+(?:your\s+)?seats?|choose\s+(?:your\s+)?seats?|pick\s+(?:your\s+)?seats?"
    r"|seat\s*map|seating\s+chart|how\s+many\s+tickets|number\s+of\s+tickets"
    r"|ticket\s+quantity|quantity\s+of\s+tickets|add\s+to\s+cart|proceed\s+to\s+checkout)"
)
FILM_ID_RE = re.compile(r"/films/(ST\d+)")
DATE_TEXT_RE = re.compile(
    r"(?i)\b(?:(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?,?\s+)?"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})"
    r"(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b"
)
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MONTHS["sept"] = 9


@dataclass
class ParseResult:
    showtimes: list[Showtime] = field(default_factory=list)
    layer: str = "none"
    saw_showtime_text: bool = False
    title: str = ""

    @property
    def empty(self) -> bool:
        return not self.showtimes


# --- helpers ----------------------------------------------------------------
def _norm(key: str) -> str:
    return re.sub(r"[^a-z]", "", str(key).lower())


def _first(mapping: dict, keys, predicate=None):
    normalized = {_norm(k): v for k, v in mapping.items()}
    for key in keys:
        if key in normalized:
            value = normalized[key]
            if value in (None, "", [], {}):
                continue
            if predicate and not predicate(value):
                continue
            return value
    return None


def _as_text(value) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, dict):
        for key in ("name", "title", "label", "value", "text"):
            if isinstance(value.get(key), str):
                return value[key].strip()
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (_as_text(v) for v in value) if part)
    return ""


def _status_from(value) -> str:
    text = _as_text(value).lower()
    if not text:
        return "unknown"
    if isinstance(value, bool):
        return "soldout" if value else "onsale"
    if any(word in text for word in ("soldout", "sold out", "outofstock", "unavailable")):
        return "soldout"
    if any(word in text for word in ("instock", "available", "onsale", "on sale", "true")):
        return "onsale"
    return "unknown"


def _plausible(dt: datetime, today: date) -> bool:
    """Reject dates far outside a cinema listing window."""
    return (today - timedelta(days=120)) <= dt.date() <= (today + timedelta(days=730))


def _absolute(url: str, base_url: str) -> str:
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/"):
        return base_url.rstrip("/") + url
    return url


# --- layer 1 & 3: generic JSON walking --------------------------------------
def _walk(node, path: str = ""):
    if isinstance(node, dict):
        yield node, path
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, path)


def _dict_to_showtime(node: dict, path: str, film_id: str, cfg, today: date) -> Showtime | None:
    normalized = {_norm(k): v for k, v in node.items()}
    matched_key, starts_at = None, None
    for key in DATE_KEYS:
        if key in DATE_KEY_BLOCKLIST or key not in normalized:
            continue
        candidate = parse_datetime(normalized[key], cfg.local_tz)
        if candidate is not None:
            matched_key, starts_at = key, candidate
            break
    if starts_at is None or not _plausible(starts_at, today):
        return None

    ticket_url = _absolute(_as_text(_first(node, URL_KEYS)), cfg.base_url)
    performance_id = _as_text(_first(node, ID_KEYS))
    has_booking = bool(ticket_url or performance_id)
    path_hint = any(hint in path.lower() for hint in PATH_HINTS)
    # A bare midnight timestamp is usually a plain date, not a screening.
    has_clock = starts_at.hour or starts_at.minute or matched_key not in ("date", "showdate")

    if not (path_hint or has_booking or has_clock):
        return None

    return Showtime(
        film_id=film_id,
        starts_at=starts_at,
        title=_as_text(_first(node, ("title", "name", "filmname", "moviename"))),
        format=_as_text(_first(node, FORMAT_KEYS)),
        auditorium=_as_text(_first(node, ROOM_KEYS)),
        ticket_url=ticket_url,
        performance_id=performance_id,
        status=_status_from(_first(node, STATUS_KEYS)),
    )


def showtimes_from_json(payload, film_id: str, cfg, today: date | None = None) -> list[Showtime]:
    today = today or datetime.now().date()
    found = []
    for node, path in _walk(payload):
        show = _dict_to_showtime(node, path, film_id, cfg, today)
        if show is not None:
            found.append(show)
    return dedupe(found)


# --- layer 2: JSON-LD -------------------------------------------------------
SCREENING_TYPES = {"screeningevent", "event", "theaterevent", "eventseries"}


def _iter_jsonld(html: str):
    for match in re.finditer(
        r'(?is)<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>', html
    ):
        raw = match.group(1).strip()
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            # Some sites emit several concatenated objects or trailing commas.
            for chunk in re.findall(r"\{.*?\}(?=\s*[,\]]|\s*$)", raw, re.DOTALL):
                try:
                    yield json.loads(chunk)
                except json.JSONDecodeError:
                    continue


def showtimes_from_jsonld(html: str, film_id: str, cfg, today: date | None = None) -> list[Showtime]:
    today = today or datetime.now().date()
    found = []
    for document in _iter_jsonld(html):
        for node, _ in _walk(document):
            types = node.get("@type") or node.get("type") or ""
            types = [types] if isinstance(types, str) else types
            if not any(_norm(t) in SCREENING_TYPES for t in types):
                continue
            starts_at = parse_datetime(node.get("startDate"), cfg.local_tz)
            if starts_at is None or not _plausible(starts_at, today):
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            work = node.get("workPresented") or node.get("about") or {}
            location = node.get("location") or {}
            found.append(
                Showtime(
                    film_id=film_id,
                    starts_at=starts_at,
                    title=_as_text(work.get("name") if isinstance(work, dict) else work)
                    or _as_text(node.get("name")),
                    format=_as_text(node.get("videoFormat") or node.get("additionalType")),
                    auditorium=_as_text(location.get("name") if isinstance(location, dict) else location),
                    ticket_url=_absolute(_as_text(offers.get("url") if isinstance(offers, dict) else ""),
                                         cfg.base_url),
                    performance_id=_as_text(node.get("identifier") or node.get("@id")),
                    status=_status_from(
                        (offers.get("availability") if isinstance(offers, dict) else None)
                        or node.get("eventStatus")
                    ),
                )
            )
    return dedupe(found)


# --- layer 3: embedded state blobs ------------------------------------------
STATE_PATTERNS = (
    r'(?is)<script[^>]+id\s*=\s*["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    r"(?is)window\.__NUXT__\s*=\s*(\{.*?\})\s*;?\s*</script>",
    r"(?is)window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>",
    r"(?is)window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>",
    r"(?is)window\.__DATA__\s*=\s*(\{.*?\})\s*;?\s*</script>",
)


def extract_state_blobs(html: str) -> list:
    blobs = []
    for pattern in STATE_PATTERNS:
        for match in re.finditer(pattern, html):
            try:
                blobs.append(json.loads(match.group(1).strip()))
            except json.JSONDecodeError:
                continue
    return blobs


def showtimes_from_embedded(html: str, film_id: str, cfg, today: date | None = None) -> list[Showtime]:
    found = []
    for blob in extract_state_blobs(html):
        found.extend(showtimes_from_json(blob, film_id, cfg, today))
    return dedupe(found)


# --- layer 4a: TickMarq/Veezi, the shape this venue actually ships ---
COMING_SOON_RE = re.compile(r"(?i)\b(coming soon|on sale soon|not yet on sale)\b")


def _tickmarq_button(element, film_id: str, cfg, fallback_date, today: date) -> "Showtime | None":
    """Read one showtime button.

    Handles both kinds the venue emits: an <a class="veezi-buy"> that is
    purchasable, and a <span ... title="Tickets Coming Soon"> for a screening
    that is scheduled but not yet on sale. The second is the earliest possible
    signal that a date has been added, so it is captured, not skipped.
    """
    classes = element.get("class") or []
    title = element.get("title", "") or ""
    href = element.get("href", "") or ""
    label = element.get_text(" ", strip=True)

    purchase = VEEZI_PURCHASE_RE.search(href)
    titled = VEEZI_TITLE_RE.search(title)
    if not (purchase or titled or ("button" in classes and _parse_time_text(label))):
        return None

    # The title attribute is authoritative when present; it carries both halves.
    day, clock = fallback_date, None
    if titled:
        clock = _parse_time_text(titled.group(1))
        day = _parse_date_text(titled.group(2), today) or fallback_date
    if clock is None:
        clock = _parse_time_text(label)
    if clock is None or day is None:
        return None

    starts_at = parse_datetime(datetime(day.year, day.month, day.day, clock[0], clock[1]), cfg.local_tz)
    if starts_at is None or not _plausible(starts_at, today):
        return None

    if "sold-out" in classes or SOLDOUT_RE.search(title):
        status = "soldout"
    elif purchase and element.name == "a":
        status = "onsale"
    elif COMING_SOON_RE.search(title) or element.name != "a":
        status = "announced"
    else:
        status = "unknown"

    # "<Film Name> at 6:30PM on Aug. 8, 2026" -> pull the format out of the name.
    format_source = title.split(" at ")[0] if titled else ""
    format_match = FORMAT_HINT_RE.search(format_source)

    return Showtime(
        film_id=film_id,
        starts_at=starts_at,
        title=format_source,
        format=format_match.group(0) if format_match else "",
        ticket_url=_absolute(href, cfg.base_url),
        performance_id=purchase.group(1) if purchase else "",
        status=status,
    )


def showtimes_from_tickmarq(html: str, film_id: str, cfg, today: date | None = None) -> list[Showtime]:
    """Film pages list <dl><dt>date</dt><dd>time buttons</dd></dl>."""
    if BeautifulSoup is None:
        return []
    today = today or datetime.now().date()
    soup = BeautifulSoup(html, "lxml")
    found: list[Showtime] = []

    for definition_list in soup.find_all("dl"):
        current_date = None
        for child in definition_list.find_all(["dt", "dd"], recursive=False) or definition_list.find_all(["dt", "dd"]):
            if child.name == "dt":
                current_date = _parse_date_text(child.get_text(" ", strip=True), today)
                continue
            for element in child.find_all(["a", "span"]):
                show = _tickmarq_button(element, film_id, cfg, current_date, today)
                if show is not None:
                    found.append(show)

    # The venue-wide calendar uses cards instead of a <dl>, but the buttons
    # still carry the full datetime in their title.
    for element in soup.find_all("a", title=True):
        if VEEZI_TITLE_RE.search(element.get("title", "")):
            show = _tickmarq_button(element, film_id, cfg, None, today)
            if show is not None:
                found.append(show)

    return dedupe(found)


# --- layer 4: DOM -----------------------------------------------------------
def _parse_date_text(text: str, today: date) -> date | None:
    iso = ISO_DATE_RE.search(text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    match = DATE_TEXT_RE.search(text)
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower()[:4].rstrip(".")) or MONTHS.get(match.group(1).lower()[:3])
    if not month:
        return None
    day = int(match.group(2))
    if match.group(3):
        year = int(match.group(3))
    else:
        # Year-less heading ("Friday, December 18"): pick the next occurrence.
        year = today.year
        try:
            if date(year, month, day) < today - timedelta(days=30):
                year += 1
        except ValueError:
            return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_time_text(text: str):
    match = TIME_RE.search(text)
    if not match:
        return None
    if match.group(1):
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        meridiem = match.group(3).lower()
        if meridiem == "p" and hour != 12:
            hour += 12
        elif meridiem == "a" and hour == 12:
            hour = 0
    else:
        hour, minute = int(match.group(4)), int(match.group(5))
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _dom_status(anchor, label: str) -> str:
    """Sold-out state for one link, without letting a sibling's badge leak in."""
    own = " ".join(
        [label, anchor.get("aria-label", "") or "", anchor.get("title", "") or ""]
        + list(anchor.get("class") or [])
    )
    if SOLDOUT_RE.search(own):
        return "soldout"
    parent = anchor.parent
    if parent is not None and len(parent.find_all("a")) == 1:
        scope = " ".join(list(parent.get("class") or []) + [parent.get_text(" ", strip=True)])
        if SOLDOUT_RE.search(scope):
            return "soldout"
    return "unknown"


def _format_near(container) -> str:
    match = FORMAT_HINT_RE.search(container.get_text(" ", strip=True)) if container else None
    return match.group(0) if match else ""


def showtimes_from_dom(html: str, film_id: str, cfg, today: date | None = None) -> list[Showtime]:
    if BeautifulSoup is None:
        return []
    today = today or datetime.now().date()
    soup = BeautifulSoup(html, "lxml")
    found: list[Showtime] = []

    # 4a. <time datetime="..."> is the most reliable thing in any HTML.
    for tag in soup.find_all("time"):
        starts_at = parse_datetime(tag.get("datetime") or tag.get_text(" ", strip=True), cfg.local_tz)
        if starts_at is None or not _plausible(starts_at, today):
            continue
        anchor = tag.find_parent("a") or tag.find_next("a")
        container = tag.find_parent(["li", "div", "td", "article", "section"]) or tag
        href = (anchor.get("href") or "") if anchor else ""
        ticket_id = TICKET_ID_RE.search(href)
        found.append(
            Showtime(
                film_id=film_id,
                starts_at=starts_at,
                format=_format_near(container),
                ticket_url=_absolute(href, cfg.base_url),
                performance_id=ticket_id.group(1) if ticket_id else "",
                status=_dom_status(anchor, tag.get_text(" ", strip=True)) if anchor else "unknown",
            )
        )

    # 4b. Walk the document in order, tracking the most recent date heading, and
    #     pair it with time-shaped ticket links underneath it.
    current_date = None
    for element in soup.find_all(True):
        own_text = " ".join(element.find_all(string=True, recursive=False)).strip()
        if own_text and len(own_text) < 120:
            parsed = _parse_date_text(own_text, today)
            if parsed is not None and _parse_time_text(own_text) is None:
                current_date = parsed
        if element.name != "a":
            continue
        href = element.get("href") or ""
        label = element.get_text(" ", strip=True) or element.get("aria-label", "")
        clock = _parse_time_text(label)
        if clock is None or not (TICKET_HREF_RE.search(href) or element.get("data-showtime-id")):
            continue
        day = _parse_date_text(href, today) or current_date
        if day is None:
            continue
        starts_at = parse_datetime(
            datetime(day.year, day.month, day.day, clock[0], clock[1]), cfg.local_tz
        )
        if starts_at is None or not _plausible(starts_at, today):
            continue
        container = element.find_parent(["li", "div", "td", "article", "section"]) or element
        ticket_id = TICKET_ID_RE.search(href)
        found.append(
            Showtime(
                film_id=film_id,
                starts_at=starts_at,
                format=_format_near(container),
                ticket_url=_absolute(href, cfg.base_url),
                performance_id=_as_text(element.get("data-showtime-id") or "")
                or (ticket_id.group(1) if ticket_id else ""),
                status=_dom_status(element, label),
            )
        )
    return dedupe(found)


# --- layer 5: detection only ------------------------------------------------
def looks_like_showtimes(html: str) -> bool:
    """Does this page contain showtime-shaped text, regardless of parseability?

    Used only to tell "no showtimes listed" apart from "our parsers broke".
    """
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = re.sub(r"<[^>]+>", " ", text)
    times = len(TIME_RE.findall(text))
    keyword = re.search(r"(?i)\b(showtime|show time|buy tickets|get tickets|select a time)\b", text)
    if PURCHASE_FLOW_RE.search(text):
        # A live seat/quantity picker means a performance is on sale right now,
        # so failing to parse one is our bug, not an empty schedule.
        return True
    return times >= 2 or (times >= 1 and keyword is not None)


# --- orchestration ----------------------------------------------------------
def page_title(html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def parse_showtimes(text: str, film_id: str, cfg, is_json: bool = False,
                    today: date | None = None) -> ParseResult:
    today = today or datetime.now().date()
    title = "" if is_json else page_title(text)

    if is_json:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return ParseResult(layer="json-invalid", title=title)
        shows = showtimes_from_json(payload, film_id, cfg, today)
        return ParseResult(shows, "json-api" if shows else "none", bool(shows), title)

    layers = (
        ("json-ld", showtimes_from_jsonld),
        ("embedded-state", showtimes_from_embedded),
        ("tickmarq", showtimes_from_tickmarq),
        ("dom", showtimes_from_dom),
    )
    for name, extractor in layers:
        try:
            shows = extractor(text, film_id, cfg, today)
        except Exception:  # a broken layer must not sink the whole run
            log.exception("parser layer %s raised", name)
            continue
        if shows:
            return ParseResult(shows, name, True, title)

    return ParseResult([], "none", looks_like_showtimes(text), title)


def parse_film_index(html: str, cfg) -> dict[str, str]:
    """Map every film ID on the index page to its title."""
    films: dict[str, str] = {}
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "lxml")
        for anchor in soup.find_all("a", href=True):
            match = FILM_ID_RE.search(anchor["href"])
            if not match:
                continue
            label = anchor.get_text(" ", strip=True) or anchor.get("title", "") or anchor.get("aria-label", "")
            if not label:
                container = anchor.find_parent(["li", "div", "article"])
                label = container.get_text(" ", strip=True)[:120] if container else ""
            films.setdefault(match.group(1), re.sub(r"\s+", " ", label).strip())
    for match in FILM_ID_RE.finditer(html or ""):  # regex backstop
        films.setdefault(match.group(1), "")
    return films
