#!/usr/bin/env python3
"""Probe BODHI's Toast Tables booking page and report the API behind it.

Toast Tables is a client-rendered app, so the page HTML says nothing useful:
availability and booking go through XHR calls. This loads the page in a real
browser, records every XHR/fetch it makes, and pulls the GraphQL operations out
of the JS bundles, so the reservation client can be written against the calls
the site actually makes rather than guessed ones. Read-only: it never books.

    python scripts/toast_discover.py [--date 2026-10-29] [--party 2]
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from playwright.sync_api import sync_playwright

RESTAURANT_GUID = "8d2f934e-edc4-458e-94bc-477a69f0cc01"
FIND_TIME_URL = f"https://tables.toasttab.com/restaurants/{RESTAURANT_GUID}/findTime"

OPERATION_RE = re.compile(r"\b(query|mutation)\s+([A-Za-z0-9_]+)\s*[({]")
ENDPOINT_RE = re.compile(r"""["'`](https?://[A-Za-z0-9.\-]*toasttab\.com[^"'`\s]*)["'`]""")
INTERESTING_RE = re.compile(r"(?i)book|reserv|avail|slot|guest|deposit|card|hold|lock|waitlist")
INTERESTING_HEADERS = {"content-type", "toast-restaurant-external-id", "x-toast-restaurant-guid",
                       "apollographql-client-name", "apollographql-client-version", "origin",
                       "referer", "x-requested-with"}


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}", flush=True)


def clip(text: str | None, limit: int = 3000) -> str:
    text = text or ""
    return text if len(text) <= limit else f"{text[:limit]}... [{len(text):,} chars]"


def graphql_documents(js: str) -> dict[str, str]:
    """Pull `query Foo(...) { ... }` documents out of a bundle by brace matching."""
    documents = {}
    for match in OPERATION_RE.finditer(js):
        start = js.find("{", match.end() - 1)
        depth, end = 0, start
        for end in range(start, min(len(js), start + 20000)):
            if js[end] == "{":
                depth += 1
            elif js[end] == "}":
                depth -= 1
                if depth == 0:
                    break
        documents.setdefault(match.group(2), f"{match.group(1)} {js[match.start(2):end + 1]}")
    return documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", default="", help="date to try searching, YYYY-MM-DD")
    parser.add_argument("--party", default="2", help="party size to try searching")
    args = parser.parse_args()

    calls = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(locale="en-US", timezone_id="America/Indiana/Indianapolis")

        def on_response(response):
            request = response.request
            if request.resource_type not in ("xhr", "fetch"):
                return
            try:
                body = response.text()
            except Exception as exc:  # body unavailable for redirects, aborted calls
                body = f"<unreadable: {exc}>"
            calls.append({
                "method": request.method,
                "url": request.url,
                "status": response.status,
                "headers": {k: v for k, v in request.headers.items()
                            if k.lower() in INTERESTING_HEADERS},
                "post": request.post_data,
                "body": body,
            })

        page.on("response", on_response)

        rule(f"PAGE LOAD\n{FIND_TIME_URL}")
        response = page.goto(FIND_TIME_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        print(f"HTTP     : {response.status if response else 'none'}")
        print(f"final URL: {page.url}")
        print(f"title    : {page.title()}")

        rule("VISIBLE TEXT (policies, hours, booking window)")
        print(clip(page.inner_text("body"), 5000))

        rule("FORM CONTROLS")
        for element in page.query_selector_all("button, select, input, [role=button], [role=option]"):
            descriptor = {
                "tag": element.evaluate("e => e.tagName.toLowerCase()"),
                "text": (element.inner_text() or "").strip()[:60],
                "aria": element.get_attribute("aria-label"),
                "name": element.get_attribute("name"),
                "testid": element.get_attribute("data-testid"),
                "type": element.get_attribute("type"),
            }
            print(json.dumps({k: v for k, v in descriptor.items() if v}))

        # Best effort: try to make the page run an availability search, so the
        # request shape for "what times are open" shows up in the capture.
        if args.date:
            for candidate in (f"{FIND_TIME_URL}?date={args.date}&partySize={args.party}",
                              f"{FIND_TIME_URL}?date={args.date}&covers={args.party}"):
                page.goto(candidate, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(3000)
        for label in ("Find a time", "Find a table", "Search", "Find"):
            button = page.get_by_role("button", name=re.compile(label, re.I))
            if button.count():
                try:
                    button.first.click(timeout=5000)
                    page.wait_for_timeout(4000)
                    print(f"\nclicked button matching {label!r}")
                    rule("VISIBLE TEXT AFTER SEARCH")
                    print(clip(page.inner_text("body"), 4000))
                except Exception as exc:
                    print(f"\ncould not click {label!r}: {exc}")
                break

        rule(f"XHR / FETCH CALLS ({len(calls)})")
        for index, call in enumerate(calls, 1):
            print(f"\n--- [{index}] {call['method']} {call['status']} {call['url']}")
            if call["headers"]:
                print(f"headers : {json.dumps(call['headers'])}")
            if call["post"]:
                print(f"request : {clip(call['post'], 4000)}")
            print(f"response: {clip(call['body'], 4000)}")

        rule("JS BUNDLES: endpoints and GraphQL operations")
        scripts = [src for src in page.eval_on_selector_all(
            "script[src]", "els => els.map(e => e.src)") if src]
        documents: dict[str, str] = {}
        endpoints: set[str] = set()
        for src in scripts:
            try:
                js = page.request.get(src, timeout=30000).text()
            except Exception as exc:
                print(f"could not fetch {src}: {exc}")
                continue
            print(f"{len(js):>10,} bytes  {src}")
            endpoints.update(ENDPOINT_RE.findall(js))
            for name, document in graphql_documents(js).items():
                documents.setdefault(name, document)
        print("\nendpoints:")
        for endpoint in sorted(endpoints):
            print(f"  {endpoint}")
        print(f"\noperations ({len(documents)}): {', '.join(sorted(documents))}")
        for name in sorted(documents):
            if INTERESTING_RE.search(name):
                rule(f"OPERATION {name}")
                print(clip(documents[name], 6000))

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
