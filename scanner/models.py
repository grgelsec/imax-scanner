"""The Showtime record and the fingerprint that gives it a stable identity."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Formats seen in the wild on ticketing pages, tried after ISO-8601 fails.
_HUMAN_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M",
    "%B %d, %Y %I:%M %p",
    "%B %d %Y %I:%M %p",
    "%b %d, %Y %I:%M %p",
    "%b. %d, %Y %I:%M %p",
    "%A, %B %d, %Y %I:%M %p",
    "%Y-%m-%dT%H:%M:%S.%f",
)

_WHITESPACE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WHITESPACE.sub(" ", (text or "").strip())


def parse_datetime(value, tz: str | ZoneInfo) -> datetime | None:
    """Best-effort parse into a timezone-aware datetime.

    Naive values are localized to the venue timezone -- a showtime printed as
    "7:00 PM" is 7pm in Indianapolis, not 7pm UTC. Getting this wrong would
    make every showtime look new twice a year when the offset shifts.
    """
    tzinfo = ZoneInfo(tz) if isinstance(tz, str) else tz
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        # Heuristic: epoch milliseconds once the value passes ~Sep 2001.
        seconds = value / 1000.0 if value > 1_000_000_000_000 else float(value)
        try:
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = _clean(str(value))
        if not text:
            return None
        candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            dt = None
            for fmt in _HUMAN_FORMATS:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tzinfo)
    return dt.astimezone(tzinfo)


@dataclass(frozen=True)
class Showtime:
    """One screening. Compared across runs by `key`."""

    film_id: str
    starts_at: datetime
    title: str = ""
    format: str = ""
    auditorium: str = ""
    ticket_url: str = ""
    performance_id: str = ""
    status: str = "unknown"

    @property
    def key(self) -> str:
        """Stable identity across runs.

        A platform-issued performance ID is preferred -- it survives cosmetic
        re-renders and even a rescheduled time. Otherwise fall back to a hash
        of the fields that actually define the screening. `status` is
        deliberately excluded so a show going sold-out reads as a *change*,
        not as one showtime vanishing and another appearing.
        """
        if self.performance_id:
            return f"pid:{self.performance_id}"
        raw = "|".join(
            (
                self.film_id,
                self.starts_at.isoformat(),
                self.format.lower(),
                self.auditorium.lower(),
            )
        )
        return "fp:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def local_date(self) -> str:
        return self.starts_at.strftime("%Y-%m-%d")

    def describe(self) -> str:
        parts = [self.starts_at.strftime("%a %b %-d, %Y at %-I:%M %p %Z")]
        if self.format:
            parts.append(self.format)
        if self.auditorium:
            parts.append(self.auditorium)
        if self.status == "soldout":
            parts.append("SOLD OUT")
        return " - ".join(parts)

    def to_dict(self) -> dict:
        return {
            "film_id": self.film_id,
            "datetime_local": self.starts_at.isoformat(),
            "datetime_utc": self.starts_at.astimezone(timezone.utc).isoformat(),
            "title": self.title,
            "format": self.format,
            "auditorium": self.auditorium,
            "ticket_url": self.ticket_url,
            "performance_id": self.performance_id,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict, tz: str | ZoneInfo) -> "Showtime | None":
        starts_at = parse_datetime(data.get("datetime_local") or data.get("datetime_utc"), tz)
        if starts_at is None:
            return None
        return cls(
            film_id=data.get("film_id", ""),
            starts_at=starts_at,
            title=data.get("title", ""),
            format=data.get("format", ""),
            auditorium=data.get("auditorium", ""),
            ticket_url=data.get("ticket_url", ""),
            performance_id=data.get("performance_id", ""),
            status=data.get("status", "unknown"),
        )

    def merged_with(self, previous: dict) -> dict:
        """Serialize, preserving the original first_seen timestamp."""
        payload = self.to_dict()
        payload["first_seen"] = previous.get("first_seen") if previous else None
        return payload


def _richness(show: "Showtime") -> int:
    return sum(bool(x) for x in (show.ticket_url, show.format, show.title,
                                 show.auditorium, show.performance_id))


def _compatible(a: "Showtime", b: "Showtime") -> bool:
    """True when two records describe the same screening, one just vaguer.

    Both DOM branches (the <time> scan and the ticket-link scan) can emit the
    same screening, and one may fail to spot the format label. Left unmerged
    they would occupy two fingerprints and fake a brand-new showtime.
    Genuinely different formats at the same instant stay separate.
    """
    for left, right in ((a.format.lower(), b.format.lower()),
                        (a.auditorium.lower(), b.auditorium.lower()),
                        (a.performance_id, b.performance_id)):
        if left and right and left != right:
            return False
    return True


def dedupe(showtimes: list["Showtime"]) -> list["Showtime"]:
    """Collapse duplicates, preferring whichever record carries more detail."""
    best: dict[str, Showtime] = {}
    for show in showtimes:
        existing = best.get(show.key)
        if existing is None or _richness(show) > _richness(existing):
            if existing is not None and show.status == "unknown" != existing.status:
                show = replace(show, status=existing.status)
            best[show.key] = show
        elif show.status != "unknown" and existing.status == "unknown":
            best[show.key] = replace(existing, status=show.status)

    # Second pass: merge vaguer duplicates of the same instant.
    merged: dict[tuple, Showtime] = {}
    for show in sorted(best.values(), key=lambda s: (-_richness(s), s.key)):
        slot = (show.film_id, show.starts_at)
        current = merged.get(slot)
        if current is None:
            merged[slot] = show
        elif not _compatible(current, show):
            merged[(show.film_id, show.starts_at, show.key)] = show
        elif show.status != "unknown" and current.status == "unknown":
            merged[slot] = replace(current, status=show.status)

    return sorted(merged.values(), key=lambda s: (s.starts_at, s.format, s.key))
