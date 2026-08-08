"""Compare what is on the page now against what we saw last run."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Showtime


@dataclass
class Change:
    before: Showtime
    after: Showtime
    kinds: list[str] = field(default_factory=list)

    def describe(self) -> str:
        detail = []
        if "time" in self.kinds:
            detail.append(
                f"moved {self.before.starts_at.strftime('%a %b %-d %-I:%M %p')} "
                f"-> {self.after.starts_at.strftime('%a %b %-d %-I:%M %p')}"
            )
        if "on_sale" in self.kinds:
            detail.append(f"tickets now on sale (was {self.before.status})")
        elif "status" in self.kinds:
            detail.append(f"{self.before.status} -> {self.after.status}")
        return f"{self.after.describe()} ({'; '.join(detail)})"


@dataclass
class ShowtimeDiff:
    added: list[Showtime] = field(default_factory=list)
    removed: list[Showtime] = field(default_factory=list)
    changed: list[Change] = field(default_factory=list)
    went_on_sale: list[Change] = field(default_factory=list)
    unchanged: int = 0

    @property
    def has_news(self) -> bool:
        """Only a screening that was not there last run is worth an email.

        Tickets opening on a screening you already know about is tracked (it
        appears in the daily digest) but does not interrupt: a screening first
        listed as "Tickets Coming Soon" already emailed when the date appeared,
        which is the earliest signal there is.
        """
        return bool(self.added)

    @property
    def any_movement(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def diff_showtimes(previous: dict, current: list[Showtime], tz) -> ShowtimeDiff:
    """previous: {key: serialized showtime} from state; current: freshly parsed."""
    result = ShowtimeDiff()
    previous = previous or {}
    remaining = dict(previous)
    pending: list[Showtime] = []

    # Pass 1 -- exact identity.
    for show in current:
        old_raw = remaining.pop(show.key, None)
        if old_raw is None:
            pending.append(show)
            continue
        _compare(result, Showtime.from_dict(old_raw, tz), show)

    # Pass 2 -- the same screening whose identity migrated. This venue lists a
    # screening as "Tickets Coming Soon" with no Veezi id, then gives it one
    # once it is purchasable. Matching the leftovers by exact start time keeps
    # that as a single showtime going on sale, instead of announcing three new
    # screenings and three vanished ones for what is really one event.
    by_instant: dict[tuple, list[str]] = {}
    for key, raw in remaining.items():
        old = Showtime.from_dict(raw, tz)
        if old is not None:
            by_instant.setdefault((old.film_id, old.starts_at), []).append(key)

    for show in pending:
        candidates = by_instant.get((show.film_id, show.starts_at), [])
        if len(candidates) == 1:  # unambiguous -- one screening at that instant
            key = candidates.pop()
            _compare(result, Showtime.from_dict(remaining.pop(key), tz), show)
        else:
            result.added.append(show)

    for raw in remaining.values():
        old = Showtime.from_dict(raw, tz)
        if old is not None:
            result.removed.append(old)

    result.added.sort(key=lambda s: s.starts_at)
    result.removed.sort(key=lambda s: s.starts_at)
    return result


def _compare(result: ShowtimeDiff, old: Showtime | None, new: Showtime) -> None:
    if old is None:
        result.unchanged += 1
        return
    kinds = []
    if old.starts_at != new.starts_at:
        kinds.append("time")
    # "unknown" means a layer could not read status; do not report that as a flip.
    if old.status != new.status and "unknown" not in (old.status, new.status):
        kinds.append("status")

    if new.status == "onsale" and old.status in ("announced", "soldout"):
        result.went_on_sale.append(Change(before=old, after=new, kinds=kinds + ["on_sale"]))
    elif kinds:
        result.changed.append(Change(before=old, after=new, kinds=kinds))
    else:
        result.unchanged += 1


def diff_films(known_ids, current: dict, watch_pattern: str):
    """Return (all new film ids, [(id, title)] matching the watch pattern).

    Dune: Part Two shipped as two film IDs at this venue, so a premiere or a
    second format can appear as a page the tracked film never links to.
    """
    known = set(known_ids or ())
    new_ids = [film_id for film_id in current if film_id not in known]
    try:
        pattern = re.compile(watch_pattern)
    except re.error:
        pattern = re.compile(r"(?i)dune")
    interesting = [(fid, current.get(fid, "")) for fid in new_ids if pattern.search(current.get(fid, "") or "")]
    return sorted(new_ids), sorted(interesting)
