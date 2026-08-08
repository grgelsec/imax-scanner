"""Email delivery and message rendering."""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate
from html import escape

log = logging.getLogger(__name__)


@dataclass
class Message:
    subject: str
    text: str
    html: str = ""


class Notifier:
    def __init__(self, cfg, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.sent: list[Message] = []

    def send(self, message: Message) -> bool:
        self.sent.append(message)
        if self.dry_run:
            print(f"\n--- [dry-run] would send ---\nSubject: {message.subject}\n\n{message.text}")
            return True
        if not self.cfg.email_configured:
            log.warning("email not configured; skipping: %s", message.subject)
            return False

        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = self.cfg.mail_from or self.cfg.smtp_user
        email["To"] = self.cfg.mail_to
        email["Date"] = formatdate(localtime=True)
        email.set_content(message.text)
        if message.html:
            email.add_alternative(message.html, subtype="html")

        try:
            context = ssl.create_default_context()
            if self.cfg.smtp_port == 465:
                with smtplib.SMTP_SSL(self.cfg.smtp_host, self.cfg.smtp_port,
                                      context=context, timeout=30) as server:
                    server.login(self.cfg.smtp_user, self.cfg.smtp_pass)
                    server.send_message(email)
            else:
                with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as server:
                    server.starttls(context=context)
                    server.login(self.cfg.smtp_user, self.cfg.smtp_pass)
                    server.send_message(email)
        except (smtplib.SMTPException, OSError) as exc:
            log.error("failed to send %r: %s", message.subject, exc)
            return False
        log.info("sent: %s", message.subject)
        return True


# --- rendering --------------------------------------------------------------
def film_name(title: str, fallback: str = "") -> str:
    """Just the film, for subject lines.

    'Dune: Part Three - The IMAX 70mm Experience | IMAX Theater in the Indiana
    State Museum' -> 'Dune: Part Three'. Subjects get truncated around 45
    characters on a phone lock screen, so the count and the dates have to come
    first and the venue boilerplate has to go.
    """
    name = short_title(title, fallback)
    for separator in (" - The IMAX", ": The IMAX", " - IMAX", " in IMAX"):
        name = name.split(separator)[0]
    return name.strip(" -:") or fallback


def short_title(title: str, fallback: str = "") -> str:
    """'Dune: Part Three - ... at IMAX Theater in the ...' -> 'Dune: Part Three - ...'"""
    cleaned = (title or "").strip()
    if not cleaned:
        return fallback
    return cleaned.split(" at IMAX Theater")[0].split(" | ")[0].strip() or fallback


def _date_range(showtimes) -> str:
    if not showtimes:
        return ""
    first, last = showtimes[0].starts_at, showtimes[-1].starts_at
    if first.date() == last.date():
        return first.strftime("%b %-d")
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.strftime('%b %-d')}-{last.strftime('%-d')}"
    return f"{first.strftime('%b %-d')} - {last.strftime('%b %-d')}"


def _wrap(body_html: str, footer: str = "") -> str:
    return (
        '<body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.5;color:#1a1a1a;">'
        f"{body_html}"
        f'<p style="color:#777;font-size:12px;margin-top:24px;border-top:1px solid #e5e5e5;'
        f'padding-top:8px;">{escape(footer)}</p></body>'
    )


def _showtime_list_html(showtimes) -> str:
    rows = []
    for show in showtimes:
        label = escape(show.describe())
        if show.ticket_url:
            link = escape(show.ticket_url, quote=True)
            rows.append(f'<li><a href="{link}" style="color:#0b5cff;">{label}</a></li>')
        else:
            rows.append(f"<li>{label}</li>")
    return '<ul style="padding-left:18px;">' + "".join(rows) + "</ul>"


def _showtime_list_text(showtimes) -> str:
    lines = []
    for show in showtimes:
        lines.append(f"  * {show.describe()}")
        if show.ticket_url:
            lines.append(f"      {show.ticket_url}")
    return "\n".join(lines)


def new_showtimes_message(film_title: str, film_url: str, diff) -> Message:
    count = len(diff.added)
    plural = "" if count == 1 else "s"
    title = short_title(film_title, "Dune: Part Three")
    # Count and dates first: this is what survives truncation, and the dates
    # are the whole point -- they say whether anything beyond what you already
    # know about has appeared.
    subject = (f"\U0001f39f\ufe0f {count} new showtime{plural}: {_date_range(diff.added)}"
               f" \u2014 {film_name(film_title, 'Dune: Part Three')}")

    text = [f"{count} new showtime{plural} listed for {title}:", "",
            _showtime_list_text(diff.added), ""]
    body = [f"<p><strong>{count} new showtime{plural}</strong> listed for {escape(title)}:</p>",
            _showtime_list_html(diff.added)]

    text.append(film_url)
    body.append(f'<p><a href="{escape(film_url, quote=True)}">View the film page</a></p>')
    return Message(subject, "\n".join(text), _wrap("".join(body), "imax-scanner"))


def new_films_message(films, site_url: str, film_url_fn) -> Message:
    plural = "" if len(films) == 1 else "s"
    subject = f"\U0001f3ac New film page{plural} matching your watch list: " + ", ".join(
        title or film_id for film_id, title in films
    )[:90]
    text = ["A new film page appeared on the venue's film list:", ""]
    body = ["<p>A new film page appeared on the venue's film list:</p><ul>"]
    for film_id, title in films:
        url = film_url_fn(film_id)
        text += [f"  * {title or film_id} ({film_id})", f"      {url}"]
        body.append(f'<li><a href="{escape(url, quote=True)}">{escape(title or film_id)}</a> '
                    f"({escape(film_id)})</li>")
    body.append("</ul><p>Add its ID to <code>FILM_IDS</code> to track its showtimes too.</p>")
    text += ["", "Add its ID to FILM_IDS to track its showtimes too.", site_url]
    return Message(subject, "\n".join(text), _wrap("".join(body), "imax-scanner"))


def bootstrap_message(film_title: str, film_url: str, showtimes) -> Message:
    title = short_title(film_title, "Dune: Part Three")
    subject = f"\U0001f440 Now watching {title} - {len(showtimes)} showtime(s) on sale"
    text = [
        f"imax-scanner is now monitoring {title}.",
        "",
        f"Baseline: {len(showtimes)} showtime(s) currently listed. You will only be emailed",
        "when something is ADDED after this point.",
        "",
        _showtime_list_text(showtimes) if showtimes else "  (none listed yet)",
        "",
        film_url,
    ]
    body = [
        f"<p><strong>imax-scanner is now monitoring {escape(title)}.</strong></p>",
        f"<p>Baseline: {len(showtimes)} showtime(s) currently listed. You will only be emailed "
        "when something is <em>added</em> after this point.</p>",
        _showtime_list_html(showtimes) if showtimes else "<p><em>None listed yet.</em></p>",
        f'<p><a href="{escape(film_url, quote=True)}">View the film page</a></p>',
    ]
    return Message(subject, "\n".join(text), _wrap("".join(body), "imax-scanner"))


def parser_alert_message(film_url: str, reason: str, detail: str) -> Message:
    subject = f"⚠️ imax-scanner needs attention: {reason}"
    text = [
        "The scanner ran but could not read showtimes it expected to find.",
        "",
        f"Reason : {reason}",
        f"Detail : {detail}",
        "",
        "Silence from this scanner is supposed to mean 'no new showtimes'. This email",
        "means it could not tell -- the page layout or a network path likely changed,",
        "so check the page manually until this clears.",
        "",
        film_url,
    ]
    body = [
        "<p>The scanner ran but <strong>could not read showtimes</strong> it expected to find.</p>",
        f"<p><strong>Reason:</strong> {escape(reason)}<br><strong>Detail:</strong> {escape(detail)}</p>",
        "<p>Silence from this scanner is supposed to mean &ldquo;no new showtimes&rdquo;. "
        "This email means it could not tell &mdash; check the page manually until this clears.</p>",
        f'<p><a href="{escape(film_url, quote=True)}">Open the film page</a></p>',
    ]
    return Message(subject, "\n".join(text), _wrap("".join(body), "imax-scanner"))


def recovery_message(film_url: str, count: int) -> Message:
    subject = f"✅ imax-scanner recovered - reading {count} showtime(s) again"
    text = ["The scanner is parsing showtimes normally again.",
            f"Currently reading {count} showtime(s).", "", film_url]
    return Message(subject, "\n".join(text),
                   _wrap(f"<p>The scanner is parsing showtimes normally again "
                         f"({count} showtime(s) read).</p>"
                         f'<p><a href="{escape(film_url, quote=True)}">Film page</a></p>',
                         "imax-scanner"))


def heartbeat_message(*, healthy: bool, local_now, sources, runs_ok: int, runs_total: int,
                      expected_runs: int, last_success: str, failures: int,
                      events, film_url: str, issues) -> Message:
    total_showtimes = sum(source["count"] for source in sources)
    flag = "✅" if healthy else "⚠️"
    status = "OK" if healthy else "DEGRADED"
    subject = (f"{flag} imax-scanner {status} - {total_showtimes} showtime(s) tracked, "
               f"{len(events)} change(s) in 24h")

    lines = [f"Daily check-in - {local_now.strftime('%A %B %-d, %Y at %-I:%M %p %Z')}", ""]
    lines.append(f"Status          : {status}")
    lines.append(f"Runs (24h)      : {runs_ok} ok / {runs_total} total (expect ~{expected_runs})")
    lines.append(f"Last good fetch : {last_success or 'never'}")
    lines.append(f"Consecutive fail: {failures}")
    lines.append("")
    for source in sources:
        lines.append(f"{source['title']}")
        window = f" ({source['first']} through {source['last']})" if source["count"] else ""
        lines.append(f"  {source['count']} showtime(s) on sale{window}")
        lines.append(f"  {source['url']}")
    lines.append("")
    if events:
        lines.append("Last 24 hours:")
        lines += [f"  * {event['text']}" for event in events]
    else:
        lines.append("Last 24 hours: no changes.")
    if issues:
        lines += ["", "Issues:"] + [f"  ! {issue}" for issue in issues]
    lines += ["", "If this email stops arriving, the scanner has stopped running.", film_url]

    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;'>{escape(label)}</td>"
        f"<td style='padding:4px 0;'><strong>{escape(value)}</strong></td></tr>"
        for label, value in (
            ("Status", status),
            ("Runs (24h)", f"{runs_ok} ok / {runs_total} total (expect ~{expected_runs})"),
            ("Last good fetch", last_success or "never"),
            ("Consecutive failures", str(failures)),
        )
    )
    source_html = "".join(
        f"<p><strong>{escape(source['title'])}</strong><br>{source['count']} showtime(s) on sale"
        + (f" ({escape(source['first'])} through {escape(source['last'])})" if source["count"] else "")
        + f'<br><a href="{escape(source["url"], quote=True)}">{escape(source["url"])}</a></p>'
        for source in sources
    )
    events_html = (
        "<p><strong>Last 24 hours</strong></p><ul>"
        + "".join(f"<li>{escape(event['text'])}</li>" for event in events) + "</ul>"
        if events else "<p><strong>Last 24 hours:</strong> no changes.</p>"
    )
    issues_html = (
        "<p><strong>Issues</strong></p><ul>"
        + "".join(f"<li>{escape(issue)}</li>" for issue in issues) + "</ul>" if issues else ""
    )
    body = (
        f"<p style='font-size:17px;'>{flag} <strong>imax-scanner {status}</strong></p>"
        f"<table style='border-collapse:collapse;'>{rows}</table>"
        f"{source_html}{events_html}{issues_html}"
        "<p style='color:#777;font-size:12px;'>If this email stops arriving, the scanner has "
        "stopped running.</p>"
    )
    return Message(subject, "\n".join(lines), _wrap(body, "imax-scanner daily heartbeat"))


def test_message() -> Message:
    return Message(
        "✉️ imax-scanner test email",
        "If you are reading this, SMTP credentials work and alerts will reach you.",
        _wrap("<p>If you are reading this, SMTP credentials work and alerts will reach you.</p>",
              "imax-scanner"),
    )
