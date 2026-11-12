"""Penjaga jadwal cron: menolak job yang lebih rapat dari `cron_min_interval_minutes`.

Dipanggil dari hook `pre_tool_call`. Yang terkonfirmasi dari sumber Hermes (tools/cronjob_tools.py,
cron/jobs.py::parse_schedule):
  - nama tool `cronjob_manage` (toolset `cronjob`), argumen `action` (create / update / …) dan
    `schedule` (string);
  - bentuk jadwal: "30m", "every 2h", "every hour", "in 30m" (sekali), "every monday 9am" /
    "weekdays at 9am" (harian/mingguan), ekspresi cron 5 kolom, dan timestamp ISO (sekali).
Yang TIDAK terkonfirmasi (dianggap konservatif):
  - nama lama `cronjob` untuk tool yang sama (diterima juga, tidak merugikan bila tidak ada);
  - ekspresi cron 6 kolom: diteruskan Hermes ke pustaka `croniter`, yang membaca kolom ke-6 sebagai
    detik — di sini kolom ke-6 dengan lebih dari satu nilai dianggap "lebih rapat dari semenit";
  - nama bulan/hari (JAN, MON) hanya sah di kolom bulan/hari, yang tidak memengaruhi jarak terpendek.
Jadwal yang tidak bisa diurai DILOLOSKAN dengan satu baris log (lebih baik longgar daripada memblokir
bentuk jadwal sah yang belum dikenal).
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

from .shared import log_once

CRON_TOOLS = {"cronjob_manage", "cronjob"}
_DURATION = re.compile(r"^(\d*)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$")
_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}
_CRON_FIELD = re.compile(r"^[A-Za-z\d\*\-,/]+$")
_DAY_WORDS = {
    "day", "daily", "everyday", "weekday", "weekdays", "weekend", "weekends",
    "sunday", "sun", "monday", "mon", "tuesday", "tue", "tues", "wednesday", "wed", "weds",
    "thursday", "thu", "thur", "thurs", "friday", "fri", "saturday", "sat",
}

MSG_CRON = ("Jadwal cron '{schedule}' berjalan tiap ±{every}; batas tim paling rapat tiap {limit} menit. "
            "Pakai jadwal yang lebih jarang (mis. 'every {limit}m'), atau minta admin tim bila memang perlu.")


def _duration_minutes(text: str) -> Optional[float]:
    match = _DURATION.match(text.strip().lower())
    if not match:
        return None
    return float((int(match.group(1)) if match.group(1) else 1) * _UNIT_MINUTES[match.group(2)[0]])


def _field_values(field: str, low: int, high: int) -> Optional[set[int]]:
    """Nilai yang cocok untuk satu kolom cron numerik (`*`, `*/n`, `a`, `a-b`, `a-b/n`, `a/n`, daftar)."""
    values: set[int] = set()
    for part in field.split(","):
        base, _, step_text = part.partition("/")
        try:
            step = int(step_text) if step_text else 1
            if step <= 0:
                return None
            if base == "*":
                start, end = low, high
            elif "-" in base:
                a, b = base.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = int(base)
                end = high if step_text else start
        except ValueError:
            return None
        if start < low or end > high or start > end:
            return None
        values.update(range(start, end + 1, step))
    return values or None


def _min_gap(points: list[int], cycle: int) -> int:
    """Jarak terpendek antar titik berurutan pada lingkaran sepanjang `cycle`."""
    if len(points) == 1:
        return cycle
    points = sorted(points)
    gaps = [b - a for a, b in zip(points, points[1:])] + [points[0] + cycle - points[-1]]
    return min(gaps)


def _cron_minutes(parts: list[str]) -> Optional[float]:
    if len(parts) not in (5, 6):
        return None
    minutes, hours = _field_values(parts[0], 0, 59), _field_values(parts[1], 0, 23)
    if minutes is None or hours is None:
        return None
    if len(parts) == 6:
        seconds = _field_values(parts[5], 0, 59)
        if seconds is None:
            return None
        if len(seconds) > 1:
            return _min_gap(list(seconds), 60) / 60.0
    # Kolom hari/bulan hanya bisa MENJARANGKAN, jadi jarak terpendek dalam sehari adalah batas bawah yang aman.
    return float(_min_gap([h * 60 + m for h in hours for m in minutes], 1440))


def schedule_interval_minutes(schedule: Any) -> Optional[float]:
    """Jarak terpendek antar eksekusi (menit). `math.inf` = sekali jalan; None = tidak bisa diurai."""
    if not isinstance(schedule, str) or not schedule.strip():
        return None
    text = schedule.strip()
    lower = text.lower()
    is_every = lower.startswith("every ")
    rest = lower[6:].strip() if is_every else lower
    parts = text.split()
    if not is_every and len(parts) >= 5 and all(_CRON_FIELD.match(p) for p in parts[:5]):
        return _cron_minutes(parts)
    first = rest.replace(",", " ").split()[:1]
    if first and first[0] in _DAY_WORDS and (len(rest.split()) > 1 or not is_every):
        return 1440.0                                         # "every monday 9am", "weekdays at 9am"
    if is_every:
        return _duration_minutes(rest)
    if re.match(r"^\d{4}-\d{2}-\d{2}", lower):
        return math.inf                                       # timestamp ISO: sekali jalan
    if lower.startswith("in "):
        return math.inf if _duration_minutes(lower[3:]) is not None else None
    return _duration_minutes(lower)


def _human(minutes: float) -> str:
    if minutes < 1:
        return f"{max(1, int(round(minutes * 60)))} detik"
    return f"{int(minutes)} menit" if minutes == int(minutes) else f"{minutes:.1f} menit"


def check_cron_call(tool_name: Any, args: Any, min_minutes: Any) -> Optional[str]:
    """Pesan penolakan bila panggilan tool cron membuat/mengubah jadwal yang terlalu rapat; None = boleh."""
    try:
        limit = int(min_minutes or 0)
    except (TypeError, ValueError):
        return None
    if limit <= 0 or str(tool_name or "") not in CRON_TOOLS or not isinstance(args, dict):
        return None
    schedule = args.get("schedule")
    if not isinstance(schedule, str) or not schedule.strip():
        return None                                           # list / pause / run / update tanpa jadwal baru
    interval = schedule_interval_minutes(schedule)
    if interval is None:
        log_once("cron-unparsed", "jadwal cron tidak bisa diurai penjaga interval (contoh: %r); diloloskan",
                 schedule[:80])
        return None
    if interval >= limit:
        return None
    return MSG_CRON.format(schedule=schedule.strip()[:80], every=_human(interval), limit=limit)
