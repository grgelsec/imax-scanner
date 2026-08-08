#!/usr/bin/env python3
"""Probe the live site once and report how showtimes are actually delivered.

Run this from a machine that can reach tickmarq.com BEFORE trusting the
parser. It answers the one question that changes the design -- is the data in
the server HTML, in JSON-LD, in an embedded state blob, behind a JSON API, or
only rendered by JavaScript -- and freezes the responses into tests/fixtures/
so the parser can be tested against the real markup forever after.

    python scripts/discover.py [--save]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.config import Config          # noqa: E402
from scanner.fetch import Fetcher          # noqa: E402
from scanner.parse import (                # noqa: E402
    extract_state_blobs,
    looks_like_showtimes,
    page_title,
    parse_film_index,
    parse_showtimes,
    showtimes_from_dom,
    showtimes_from_embedded,
    showtimes_from_jsonld,
    showtimes_from_tickmarq,
)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
API_HINT_RE = re.compile(r"""["'](/[A-Za-z0-9/_\-.]*(?:api|graphql|\.json)[A-Za-z0-9/_\-.?=]*)["']""")


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def report_page(name: str, url: str, fetcher: Fetcher, cfg, film_id: str, save: bool) -> None:
    rule(f"{name}\n{url}")
    response = fetcher.get(url)
    print(f"HTTP           : {response.status}  ({response.error or 'ok'})")
    for header in ("Content-Type", "ETag", "Last-Modified", "Cache-Control", "Server",
                   "CF-Ray", "X-Powered-By", "Set-Cookie"):
        if header in response.headers:
            print(f"{header:<15}: {response.headers[header][:110]}")
    if not response.text:
        print("no body returned - nothing more to inspect")
        return

    html = response.text
    print(f"body bytes     : {len(html):,}")
    print(f"<title>        : {page_title(html)}")
    print(f"showtime-ish text present : {looks_like_showtimes(html)}")

    ld_blocks = len(re.findall(r'(?is)type\s*=\s*["\']application/ld\+json', html))
    blobs = extract_state_blobs(html)
    print(f"JSON-LD blocks : {ld_blocks}")
    print(f"state blobs    : {len(blobs)}"
          + (f"  (top-level keys: {sorted(blobs[0])[:8]})" if blobs else ""))

    endpoints = sorted({m.group(1) for m in API_HINT_RE.finditer(html)})
    print(f"api-ish URLs   : {len(endpoints)}")
    for endpoint in endpoints[:15]:
        print(f"   {endpoint}")
    if len(endpoints) > 15:
        print(f"   ... and {len(endpoints) - 15} more")

    print("\nper-layer yield:")
    for layer, extractor in (("json-ld", showtimes_from_jsonld),
                             ("embedded-state", showtimes_from_embedded),
                             ("tickmarq", showtimes_from_tickmarq),
                             ("dom", showtimes_from_dom)):
        try:
            shows = extractor(html, film_id, cfg)
            print(f"   {layer:<15}: {len(shows)} showtime(s)")
            for show in shows[:4]:
                print(f"        {show.describe()}  [{show.status}]  {show.ticket_url}")
        except Exception as exc:  # noqa: BLE001
            print(f"   {layer:<15}: RAISED {type(exc).__name__}: {exc}")

    chosen = parse_showtimes(html, film_id, cfg)
    print(f"\n=> parser would use layer '{chosen.layer}' -> {len(chosen.showtimes)} showtime(s)")
    for show in chosen.showtimes:
        print(f"     {show.describe():<52} [{show.status}]  {show.ticket_url}")
    if chosen.empty and looks_like_showtimes(html):
        dump_markup(html)
    if chosen.empty and chosen.saw_showtime_text:
        print("   !! times are visible on the page but no layer parsed them.")
        print("   !! showtimes are probably rendered client-side: look for an API endpoint")
        print("      above, or switch the fetch layer to Playwright.")

    if save:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        target = FIXTURES / f"live_{name.lower().replace(' ', '_')}.html"
        target.write_text(html, encoding="utf-8")
        print(f"   saved -> {target.relative_to(Path.cwd()) if target.is_relative_to(Path.cwd()) else target}")


def dump_markup(html: str, window: int = 2600) -> None:
    """Print the markup around the first visible time, plus link and JSON-LD shapes.

    Runs only when a page shows times that no layer could parse -- exactly the
    case where the selectors need rewriting and the real markup is the only
    thing that helps. Prints into the job log because artifact downloads are
    not reachable from the dev environment.
    """
    from scanner.parse import TIME_RE  # local import keeps the module's API clean

    stripped = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    match = TIME_RE.search(re.sub(r"<[^>]+>", " ", stripped))
    print("\n   --- markup around the first visible showtime " + "-" * 26)
    if match:
        # Map back into the tagged HTML by searching for the matched text.
        needle = match.group(0)
        pos = stripped.find(needle)
        chunk = stripped[max(0, pos - window // 3): pos + window]
        chunk = re.sub(r"\n\s*\n+", "\n", chunk)
        for line in chunk.splitlines():
            line = line.rstrip()
            if line.strip():
                print("   | " + line[:220])
    else:
        print("   (no time text found to anchor on)")

    hrefs = sorted({h for h in re.findall(r'href="([^"]{4,90})"', stripped)
                    if re.search(r"(?i)ticket|showtime|perform|session|seat|order|book|film", h)})
    print(f"\n   --- {len(hrefs)} candidate link shape(s) " + "-" * 34)
    for href in hrefs[:25]:
        print(f"   | {href}")

    for i, block in enumerate(re.findall(
            r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html)):
        print(f"\n   --- JSON-LD block {i} (first 700 chars) " + "-" * 24)
        print("   | " + re.sub(r"\s+", " ", block.strip())[:700])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--save", action="store_true", help="write responses to tests/fixtures/")
    args = parser.parse_args()

    cfg = Config.from_env()
    fetcher = Fetcher(cfg)
    film_id = cfg.film_ids[0]

    rule("robots.txt")
    robots = fetcher.get(f"{cfg.base_url}/robots.txt")
    print(robots.text[:1200] if robots.text else f"(no robots.txt: {robots.status} {robots.error})")
    for url in (cfg.film_url(film_id), cfg.films_index_url, cfg.showtimes_url()):
        print(f"allowed to fetch {url}: {fetcher.allowed(url)}")

    # Probe every configured film. Pointing this at a film with tickets on sale
    # today acts as a control: if that one also yields zero showtimes, the
    # parser is broken rather than the schedule being empty.
    for fid in cfg.film_ids:
        report_page(f"Film page {fid}", cfg.film_url(fid), fetcher, cfg, fid, args.save)

    rule(f"Films index\n{cfg.films_index_url}")
    index = fetcher.get(cfg.films_index_url)
    print(f"HTTP: {index.status} ({index.error or 'ok'})")
    if index.text:
        films = parse_film_index(index.text, cfg)
        print(f"{len(films)} film(s) found:")
        for fid, title in sorted(films.items()):
            print(f"   {fid}  {title[:70]}")
        if args.save:
            FIXTURES.mkdir(parents=True, exist_ok=True)
            (FIXTURES / "live_films_index.html").write_text(index.text, encoding="utf-8")
            print(f"   saved -> {FIXTURES / 'live_films_index.html'}")

    report_page("Showtimes calendar", cfg.showtimes_url(), fetcher, cfg, film_id, args.save)

    rule("What to do next")
    print("""
 * A JSON endpoint in the 'api-ish URLs' list is the best source: fetch it
   directly and set is_json=True in parse_showtimes.
 * If a layer already yields the right showtimes, you are done -- commit the
   saved fixtures and add an assertion in tests/test_parse.py pinning the
   expected count so a future site redesign fails the test loudly.
 * If nothing parsed but times are visible, the page is client-rendered.
   Open devtools -> Network -> XHR on the live page, find the request that
   returns the showtimes, and point the scanner at that URL.
""".rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
