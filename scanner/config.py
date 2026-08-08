"""Runtime configuration, sourced from environment variables.

Every value has a working default so `python -m scanner.main --dry-run` runs
with no setup at all; only the SMTP settings must be supplied to send mail.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_USER_AGENT = (
    "imax-scanner/1.0 (personal showtime monitor; "
    "+https://github.com/grgelsec/imax-scanner)"
)


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # --- what to watch -------------------------------------------------
    base_url: str = "https://www.tickmarq.com"
    site: str = "indyimax"
    film_ids: tuple[str, ...] = ("ST00001410",)
    # Any *new* film page whose title matches this is worth an alert. Dune:
    # Part Two occupied two film IDs at this venue (ST00000974 for 70mm and
    # ST00000992 for the "Fan First Premieres"), so a Part Three premiere may
    # well appear under an ID the main film page never links to.
    watch_pattern: str = r"(?i)dune"

    # --- timing --------------------------------------------------------
    local_tz: str = "America/Indiana/Indianapolis"
    heartbeat_hour: int = 12
    jitter_seconds: int = 420

    # --- http ----------------------------------------------------------
    user_agent: str = DEFAULT_USER_AGENT
    timeout: int = 20
    max_retries: int = 3
    respect_robots: bool = True

    # --- email ---------------------------------------------------------
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_pass: str = field(default="", repr=False)
    mail_to: str = ""
    mail_from: str = ""

    # --- misc ----------------------------------------------------------
    state_path: str = "state/indyimax.json"
    run_log_limit: int = 48
    failure_alert_threshold: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        film_ids = tuple(
            part.strip()
            for part in _env_str("FILM_IDS", "ST00001410").split(",")
            if part.strip()
        )
        smtp_user = _env_str("SMTP_USER", "")
        return cls(
            base_url=_env_str("BASE_URL", cls.base_url).rstrip("/"),
            site=_env_str("SITE", cls.site),
            film_ids=film_ids or cls.film_ids,
            watch_pattern=_env_str("WATCH_PATTERN", cls.watch_pattern),
            local_tz=_env_str("LOCAL_TZ", cls.local_tz),
            heartbeat_hour=_env_int("HEARTBEAT_HOUR", cls.heartbeat_hour),
            jitter_seconds=_env_int("JITTER_SECONDS", cls.jitter_seconds),
            user_agent=_env_str("USER_AGENT", cls.user_agent),
            timeout=_env_int("HTTP_TIMEOUT", cls.timeout),
            max_retries=_env_int("HTTP_MAX_RETRIES", cls.max_retries),
            respect_robots=_env_bool("RESPECT_ROBOTS", cls.respect_robots),
            smtp_host=_env_str("SMTP_HOST", cls.smtp_host),
            smtp_port=_env_int("SMTP_PORT", cls.smtp_port),
            smtp_user=smtp_user,
            smtp_pass=_env_str("SMTP_PASS", ""),
            mail_to=_env_str("MAIL_TO", ""),
            mail_from=_env_str("MAIL_FROM", smtp_user),
            state_path=_env_str("STATE_PATH", cls.state_path),
        )

    # --- derived URLs ---------------------------------------------------
    @property
    def site_url(self) -> str:
        return f"{self.base_url}/sites/{self.site}"

    def film_url(self, film_id: str) -> str:
        return f"{self.site_url}/films/{film_id}"

    @property
    def films_index_url(self) -> str:
        return f"{self.site_url}/films/"

    def showtimes_url(self, date_iso: str | None = None) -> str:
        base = f"{self.site_url}/showtimes"
        return f"{base}?date={date_iso}" if date_iso else base

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass and self.mail_to)
