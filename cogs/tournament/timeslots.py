"""
timeslots.py — availability, timezone maths and overlap.

The rule this module exists to enforce: availability is entered in the player's
LOCAL time and stored that way, but every comparison and every stored schedule
is UTC. Converting at the boundary (rather than storing UTC slots) means a
player who fixes a wrong offset does not silently shift all their availability.

Offsets are hours as a float — 5.5 for IST, -3.5 for NST. Never a string like
"IST": abbreviations are ambiguous (IST is India, Ireland and Israel) and do not
survive DST.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_INDEX = {d.lower(): i for i, d in enumerate(DAYS)}
_DAY_INDEX.update({d.lower()[:3]: i for i, d in enumerate(DAYS)})
_DAY_INDEX.update({
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
})

WEEK_MINUTES = 7 * 24 * 60

_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:.]?\s*(\d{2})?\s*$")


class SlotError(ValueError):
    """Raised for input a player can fix — the message is shown to them."""


# ── Parsing / validation ──────────────────────────────────────────────────────

def parse_hhmm(value: str) -> int:
    """'18:00' -> 1080 minutes past midnight. Strict: bad input raises."""
    if value is None:
        raise SlotError("Missing time.")
    m = _TIME_RE.match(str(value))
    if not m:
        raise SlotError(f"`{value}` isn't a time — use HH:MM, e.g. `18:00`.")
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    if not (0 <= hh <= 24) or not (0 <= mm <= 59):
        raise SlotError(f"`{value}` isn't a real time.")
    total = hh * 60 + mm
    if total > 24 * 60:
        raise SlotError(f"`{value}` is past midnight.")
    return total


def fmt_hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_day(value: str) -> int:
    key = str(value or "").strip().lower()
    if key not in _DAY_INDEX:
        raise SlotError(f"`{value}` isn't a day — use Mon/Tue/.../Sun.")
    return _DAY_INDEX[key]


def parse_offset(value) -> float:
    """'+5:30', '5.5', '-3' -> hours as float. Rejects anything out of range."""
    if isinstance(value, (int, float)):
        off = float(value)
    else:
        raw = str(value or "").strip().upper().replace("UTC", "").replace("GMT", "")
        raw = raw.strip()
        if not raw:
            raise SlotError("Missing timezone offset.")
        sign = -1.0 if raw.startswith("-") else 1.0
        raw = raw.lstrip("+-").strip()
        if ":" in raw:
            hh, _, mm = raw.partition(":")
            try:
                off = sign * (int(hh) + int(mm) / 60.0)
            except ValueError:
                raise SlotError(f"`{value}` isn't an offset — try `+5:30`.")
        else:
            try:
                off = sign * float(raw)
            except ValueError:
                raise SlotError(f"`{value}` isn't an offset — try `+5:30`.")
    if not (-12.0 <= off <= 14.0):
        raise SlotError("Offset must be between UTC-12 and UTC+14.")
    return round(off * 4) / 4.0          # quarter-hour resolution is enough


def validate_slots(raw: Iterable[dict]) -> list[dict]:
    """Normalise and strictly validate availability. Returns local-time slots.

    Overnight ranges (22:00-02:00) are rejected rather than silently split: a
    player almost always means 02:00 the NEXT day, and guessing produces
    schedules nobody expects.
    """
    out: list[dict] = []
    for entry in raw or []:
        day = parse_day(entry.get("day"))
        start = parse_hhmm(entry.get("start"))
        end = parse_hhmm(entry.get("end"))
        if end <= start:
            raise SlotError(
                f"{DAYS[day]} {fmt_hhmm(start)}-{fmt_hhmm(end)}: end must be "
                f"after start. Split an overnight range across two days.")
        if end - start < 30:
            raise SlotError(f"{DAYS[day]} slot is under 30 minutes — too short "
                            f"to schedule a match in.")
        out.append({"day": DAYS[day], "start": fmt_hhmm(start),
                    "end": fmt_hhmm(end)})
    if not out:
        raise SlotError("Add at least one availability slot.")
    return _merge_local(out)


def _merge_local(slots: list[dict]) -> list[dict]:
    """Collapse overlapping same-day slots so duplicates can't inflate overlap."""
    by_day: dict[int, list[tuple[int, int]]] = {}
    for s in slots:
        by_day.setdefault(parse_day(s["day"]), []).append(
            (parse_hhmm(s["start"]), parse_hhmm(s["end"])))
    merged: list[dict] = []
    for day in sorted(by_day):
        spans = sorted(by_day[day])
        cur_s, cur_e = spans[0]
        for s, e in spans[1:]:
            if s <= cur_e:
                cur_e = max(cur_e, e)
            else:
                merged.append({"day": DAYS[day], "start": fmt_hhmm(cur_s),
                               "end": fmt_hhmm(cur_e)})
                cur_s, cur_e = s, e
        merged.append({"day": DAYS[day], "start": fmt_hhmm(cur_s),
                       "end": fmt_hhmm(cur_e)})
    return merged


# ── Local <-> UTC on the weekly circle ────────────────────────────────────────

def to_utc_intervals(slots: list[dict], utc_offset: float) -> list[tuple[int, int]]:
    """Local weekly slots -> minute intervals on a UTC week [0, 10080).

    A slot can wrap past Sunday midnight once shifted, so it is emitted as two
    intervals rather than one that runs backwards. Everything downstream can
    then treat intervals as plain ranges.
    """
    shift = int(round(utc_offset * 60))
    out: list[tuple[int, int]] = []
    for s in slots:
        day = parse_day(s["day"])
        start = day * 1440 + parse_hhmm(s["start"]) - shift
        end = day * 1440 + parse_hhmm(s["end"]) - shift
        start %= WEEK_MINUTES
        end %= WEEK_MINUTES
        if end == start:
            continue
        if end < start:                       # wrapped across the week boundary
            out.append((start, WEEK_MINUTES))
            out.append((0, end))
        else:
            out.append((start, end))
    return _merge_intervals(out)


def _merge_intervals(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    out = [spans[0]]
    for s, e in spans[1:]:
        ls, le = out[-1]
        if s <= le:
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


def overlap(a: list[tuple[int, int]], b: list[tuple[int, int]]
            ) -> list[tuple[int, int]]:
    """Intersection of two interval sets on the UTC week."""
    out: list[tuple[int, int]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def overlap_minutes(spans: list[tuple[int, int]]) -> int:
    return sum(e - s for s, e in spans)


# ── Choosing a concrete kickoff ───────────────────────────────────────────────

def week_minute(ts: float) -> int:
    """Minute-of-week for a UTC timestamp. Monday 00:00 UTC is 0."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.weekday() * 1440 + dt.hour * 60 + dt.minute


def next_occurrence(after: float, target_week_minute: int) -> float:
    """First UTC timestamp at or after `after` landing on that minute-of-week."""
    delta = (target_week_minute - week_minute(after)) % WEEK_MINUTES
    base = datetime.fromtimestamp(after, tz=timezone.utc).replace(
        second=0, microsecond=0)
    return (base + timedelta(minutes=delta)).timestamp()


def best_match_time(a_slots: list[dict], a_off: float,
                    b_slots: list[dict], b_off: float,
                    after: float, duration_min: int = 30,
                    busy: Optional[list[tuple[float, float]]] = None
                    ) -> Optional[float]:
    """Earliest UTC kickoff both players can make, or None if they never overlap.

    `busy` is a list of (start, end) UTC ranges the pair already has committed,
    which is how a player is kept from being booked into two matches at once.
    """
    spans = overlap(to_utc_intervals(a_slots, a_off),
                    to_utc_intervals(b_slots, b_off))
    spans = [(s, e) for s, e in spans if e - s >= duration_min]
    if not spans:
        return None

    candidates = sorted(next_occurrence(after, s) for s, _ in spans)
    for ts in candidates:
        if not _conflicts(ts, duration_min, busy):
            return ts
    # Every first occurrence is blocked — try the following week rather than
    # giving up, since a weekly slot repeats.
    for ts in candidates:
        nxt = ts + WEEK_MINUTES * 60
        if not _conflicts(nxt, duration_min, busy):
            return nxt
    return None


def _conflicts(start: float, duration_min: int,
               busy: Optional[list[tuple[float, float]]]) -> bool:
    end = start + duration_min * 60
    for bs, be in (busy or []):
        if start < be and bs < end:
            return True
    return False


def local_str(ts: float, utc_offset: float) -> str:
    """Render a UTC timestamp in one player's local time."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=utc_offset)
    sign = "+" if utc_offset >= 0 else "-"
    mag = abs(utc_offset)
    off = f"UTC{sign}{int(mag)}" + (f":{int(round((mag % 1) * 60)):02d}"
                                    if mag % 1 else "")
    return dt.strftime("%a %d %b, %H:%M ") + off


def discord_ts(ts: float, style: str = "F") -> str:
    """Discord renders this in each viewer's own timezone automatically, which
    beats any conversion we could do for channel-wide messages."""
    return f"<t:{int(ts)}:{style}>"
