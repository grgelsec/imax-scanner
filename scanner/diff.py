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
        if "status" in self.kinds:
            detail.append(f"{self.before.status} -> {self.after.status}")
        return f"{self.after.describe()} ({'; '.join(detail)})"


@dataclass
class ShowtimeDiff:
    added: list[Showtime] = field(default_factory=list)
    removed: list[Showtime] = field(default_factory=list)
    changed: list[Change] = field(default_factory=list)
    unchanged: int = 0

    @property
    def has_news(self) -> bool:
        """Only additions are worth waking someone up for."""
        return bool(self.added)

    @property
    def any_movement(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def diff_showtimes(previous: dict, current: list[Showtime], tz) -> ShowtimeDiff:
    """previous: {key: serialized showtime} from state; current: freshly parsed."""
    result = ShowtimeDiff()
    current_by_key = {show.key: show for show in current}
    previous = previous or {}

    for key, show in current_by_key.items():
        old_raw = previous.get(key)
        if old_raw is None:
            result.added.append(show)
            continue
        old = Showtime.from_dict(old_raw, tz)
        if old is None:
            result.unchanged += 1
            continue
        kinds = []
        if old.starts_at != show.starts_at:
            kinds.append("time")
        # "unknown" means a layer could not read status; do not report that as a flip.
        if old.status != show.status and "unknown" not in (old.status, show.status):
            kinds.append("status")
        if kinds:
            result.changed.append(Change(before=old, after=show, kinds=kinds))
        else:
            result.unchanged += 1

    for key, old_raw in previous.items():
        if key in current_by_key:
            continue
        old = Showtime.from_dict(old_raw, tz)
        if old is not None:
            result.removed.append(old)

    result.added.sort(key=lambda s: s.starts_at)
    result.removed.sort(key=lambda s: s.starts_at)
    return result


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
