# imax-scanner

Watches the [Indiana State Museum IMAX](https://www.tickmarq.com/sites/indyimax/films/ST00001410)
listing for **Dune: Part Three – The IMAX 70mm Experience** and emails you when new
showtimes go on sale. Runs hourly on GitHub Actions; sends a daily digest at noon so you
can tell "nothing new" apart from "it died".

## What it actually watches

| Source | Why |
| --- | --- |
| `/films/ST00001410` | The Part Three 70mm page — new showtimes appear here |
| `/films/` | New film *pages*. Dune: Part Two used two IDs at this venue (`ST00000974` for 70mm, `ST00000992` for the Fan First Premieres), so a premiere can land on an ID the main page never links to |

## Setup

### 1. Run discovery first

The parser tries several strategies and picks whichever one the page actually supports.
Confirm which one that is, and freeze the real markup as test fixtures:

```bash
pip install -r requirements.txt
python scripts/discover.py --save
```

It reports `robots.txt`, whether the page ships JSON-LD / an embedded state blob / a JSON
API, and how many showtimes each parser layer finds. Two outcomes need action:

- **An API endpoint is listed** — that is the most stable source. Point the scanner at it.
- **"times are visible but no layer parsed them"** — the page is rendered client-side.
  Find the XHR that returns showtimes (devtools → Network → XHR) and use that URL, or
  switch the fetch layer to Playwright.

Then confirm against the live page without sending anything or writing state:

```bash
python -m scanner.main --dry-run --no-jitter
```

### 2. Add repository secrets

`Settings → Secrets and variables → Actions → Secrets`:

| Secret | Value |
| --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | a Gmail **app password**, not your account password ([create one](https://myaccount.google.com/apppasswords); requires 2-Step Verification) |
| `MAIL_TO` | where alerts should go |

Verify them: `python -m scanner.main --test-email`, or run the workflow manually.

### 3. Merge to the default branch

**Scheduled workflows only fire from the repository's default branch.** While this lives on
a feature branch the hourly trigger does nothing — use *Actions → Scan for new showtimes →
Run workflow* to test, then merge to `main` to start the schedule.

## What arrives in your inbox

| Email | When |
| --- | --- |
| 👀 Now watching … | Once, on the first run — a baseline, so you don't get one alert per already-listed showtime |
| 🎟️ N new showtimes | Showtimes were **added**. The one you actually care about |
| 🎫 Tickets now on sale | Screenings you already knew about became purchasable |
| 🎬 New film page … | A new film page matching `WATCH_PATTERN` appeared |
| ⚠️ needs attention | The page loaded but showtimes could not be read — three runs in a row |
| ✅ recovered | Parsing works again |
| ✅/⚠️ daily digest | Once a day at noon local |

Sold-outs and removed showtimes are recorded in the daily digest but never emailed on their
own — only additions and tickets opening are worth an interruption.

**Why "on sale" is its own alert:** this venue lists a screening as *Tickets Coming Soon*
before you can buy it. That gives two separate moments worth knowing about — the date
appearing, and the tickets opening — and only the second one you can act on. Both are
captured; a screening going *sold out* is not, since there is nothing to do about it.

## The daily heartbeat

Sent by the first run at or after **12:00 local** (`LOCAL_TZ`, default
`America/Indiana/Indianapolis`), not by a second cron entry. That matters:

- It rides the **real** pipeline, so it proves fetching and parsing work — a standalone
  "email at noon" job would only prove that Actions and SMTP work.
- GitHub cron is **UTC-only**. A pinned `0 16 * * *` would quietly become 11am when Indiana
  leaves EDT; deciding in local time keeps it at noon year-round.
- If the noon run is delayed or dropped, the next run that day still sends it.
- It goes out **even when the scan fails**, marked `DEGRADED`.

It is a dead man's switch: a *missing* noon email is the alarm. If you would rather have an
alert actively arrive when the scanner dies, add a free [healthchecks.io](https://healthchecks.io)
check and ping it at the end of a successful run.

## Configuration

All optional; defaults target this film and venue.

| Variable | Default | Purpose |
| --- | --- | --- |
| `FILM_IDS` | `ST00001410` | Comma-separated film pages to track |
| `WATCH_PATTERN` | `(?i)dune` | Regex for new film pages worth alerting on |
| `LOCAL_TZ` | `America/Indiana/Indianapolis` | Showtime and heartbeat timezone |
| `HEARTBEAT_HOUR` | `12` | Local hour the digest is sent at or after |
| `JITTER_SECONDS` | `420` | Random pre-fetch sleep |
| `STATE_PATH` | `state/indyimax.json` | Where memory lives |
| `RESPECT_ROBOTS` | `true` | Honour `robots.txt` |

CLI: `--dry-run`, `--no-jitter`, `--test-email`, `--force-heartbeat`, `-v`.

## How "new" is decided

`state/indyimax.json` is committed after every run — that file is the scanner's memory, and
its diffs are a readable history of when each showtime appeared.

Each showtime gets a stable identity: the Veezi session id from its ticket link when the page
exposes one, otherwise a hash of film + exact local start time + format + auditorium. When a
screening gains an id — which happens the moment *Tickets Coming Soon* becomes purchasable —
the diff matches it back to the old record by start time, so one screening going on sale is not
reported as a different screening appearing and the original vanishing. Availability is
deliberately **not** part of that identity, so a show selling out reads as a change rather
than as one showtime vanishing and a different one appearing.

Silence is meaningful. The scanner separates "no showtimes listed" from "we could not read
the showtimes" and emails you about the second: if the page shows times none of the parsers
can read, or if showtimes that had not happened yet disappear, that is reported as a fault
rather than quietly treated as "nothing new".

## Running it somewhere else

Nothing is Actions-specific. If GitHub's IP range gets blocked by the site, a systemd timer
on a residential connection is a drop-in replacement:

```ini
# ~/.config/systemd/user/imax-scanner.service
[Service]
Type=oneshot
WorkingDirectory=%h/imax-scanner
EnvironmentFile=%h/.config/imax-scanner.env
ExecStart=%h/imax-scanner/.venv/bin/python -m scanner.main

# ~/.config/systemd/user/imax-scanner.timer
[Timer]
OnCalendar=hourly
RandomizedDelaySec=420
Persistent=true
[Install]
WantedBy=timers.target
```

`systemctl --user enable --now imax-scanner.timer`

## Tests

```bash
python -m pytest tests/ -q
```

Covers each parser layer, fingerprint stability across timezones and DST, the golden
"exactly one new showtime" diff, the empty-versus-broken distinction, heartbeat timing on
both sides of a DST boundary, and state surviving corruption.

## Being a good citizen

Identifies itself honestly in its User-Agent, honours `robots.txt`, uses conditional
requests so an unchanged page costs a `304`, retries only on timeouts and 5xx, and makes
about 24 requests a day.
