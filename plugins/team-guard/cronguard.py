"""Penjaga jadwal cron: menolak job yang lebih rapat dari `cron_min_interval_minutes`.

Dipanggil dari hook `pre_tool_call`. Yang terkonfirmasi dari sumber Hermes (tools/cronjob_tools.py,
cron/jobs.py::parse_schedule):
  - nama tool `cronjob_manage` (toolset `cronjob`), argumen `action` (create / update / …) dan
    `schedule` (string);
  - bentuk jadwal: "30m", "every 2h", "every hour", "in 30m" (sekali), "every monday 9am" /
    "weekdays at 9am" (harian/mingguan), ekspresi cron 5 kolom, dan timestamp ISO (sekali).
Yang TIDAK terkonfirmasi (dianggap konservatif):
  - nama lama `cronjob` untuk tool yang sama (diterima juga, tidak merugikan bila tidak ada);
  - ekspresi cron 6/7 kolom: Hermes meneruskannya apa adanya ke pustaka `croniter`, yang membaca kolom
    ke-6 sebagai DETIK dan kolom ke-7 sebagai tahun — di sini kolom detik dengan lebih dari satu nilai
    berarti "lebih rapat dari semenit"; kolom tahun hanya menjarangkan;
  - rentang melingkar (`59-58`, `50-10`, detik `59-0`) dihitung melingkar seperti croniter versi baru;
  - nama bulan/hari (JAN, MON) hanya sah di kolom bulan/hari, yang tidak memengaruhi jarak terpendek.

Kebijakan untuk jadwal yang tidak bisa dihitung:
  - string yang BERBENTUK ekspresi cron (≥5 kolom karakter cron — syarat yang sama dengan
    parse_schedule Hermes sebelum menyerahkannya ke croniter) tetapi jarak terpendeknya tidak bisa
    dihitung → DITOLAK dengan pesan jelas (gagal-tertutup: bentuk cron eksotis tidak boleh jadi celah);
  - teks bebas yang memang bukan cron → diloloskan dengan satu baris log (Hermes sendiri yang menolaknya
    bila tidak sah).
"""
from __future__ import annotations

import math
import re
import threading
from collections import deque
from typing import Any, Callable, Optional

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

MSG_CRON_UNKNOWN = ("Jadwal cron '{schedule}' tidak bisa diperiksa penjaga interval tim (bentuk ekspresi cron "
                    "ini tidak dikenali), jadi ditolak. Pakai bentuk 5 kolom biasa (mis. '*/{limit} * * * *' atau "
                    "'0 9 * * *') atau interval (mis. 'every {limit}m'); batas tim paling rapat tiap {limit} menit.")
MSG_CRON = ("Jadwal cron '{schedule}' berjalan tiap ±{every}; batas tim paling rapat tiap {limit} menit. "
            "Pakai jadwal yang lebih jarang (mis. 'every {limit}m'), atau minta admin tim bila memang perlu.")


def _duration_minutes(text: str) -> Optional[float]:
    match = _DURATION.match(text.strip().lower())
    if not match:
        return None
    return float((int(match.group(1)) if match.group(1) else 1) * _UNIT_MINUTES[match.group(2)[0]])


def _field_values(field: str, low: int, high: int) -> Optional[set[int]]:
    """Nilai yang cocok untuk satu kolom cron numerik (`*`, `*/n`, `a`, `a-b`, `a-b/n`, `a/n`, daftar).
    Rentang melingkar (`a-b` dengan a > b) berjalan dari a sampai batas atas lalu dari batas bawah sampai b."""
    values: set[int] = set()
    for part in field.split(","):
        base, _, step_text = part.partition("/")
        try:
            step = int(step_text) if step_text else 1
            if step <= 0 or (step_text == "" and "/" in part):
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
        if not (low <= start <= high and low <= end <= high):
            return None
        if start <= end:
            values.update(range(start, end + 1, step))
        elif step != 1:
            return None                                        # melingkar + langkah: tafsirnya tidak pasti
        else:                                                  # melingkar: 59-58, 50-10, …
            values.update(range(start, high + 1))
            values.update(range(low, end + 1))
    return values or None


def _min_gap(points: list[int], cycle: int) -> int:
    """Jarak terpendek antar titik berurutan pada lingkaran sepanjang `cycle`."""
    points = sorted(set(points))
    if len(points) == 1:
        return cycle
    gaps = [b - a for a, b in zip(points, points[1:])] + [points[0] + cycle - points[-1]]
    return min(gaps)


def looks_like_cron(schedule: Any) -> bool:
    """Bentuk yang oleh Hermes (cron/jobs.py::parse_schedule) diserahkan ke croniter: bukan "every …",
    bukan frasa hari, ≥5 kolom dan lima kolom pertama hanya berisi karakter cron."""
    if not isinstance(schedule, str):
        return False
    text = schedule.strip()
    if text.lower().startswith("every ") or _is_day_phrase(text.lower(), False):
        return False
    parts = text.split()
    return len(parts) >= 5 and all(_CRON_FIELD.match(p) for p in parts[:5])


def _is_day_phrase(rest: str, is_every: bool) -> bool:
    first = rest.replace(",", " ").split()[:1]
    return bool(first) and first[0] in _DAY_WORDS and (len(rest.split()) > 1 or not is_every)


def _cron_minutes(parts: list[str]) -> Optional[float]:
    """Jarak terpendek (menit) untuk ekspresi 5 kolom, 6 kolom (+detik), atau 7 kolom (+detik, tahun).
    None = tidak bisa dihitung (jumlah kolom lain, nama di kolom menit/jam, sintaks croniter eksotis)."""
    if len(parts) not in (5, 6, 7):
        return None
    minutes, hours = _field_values(parts[0], 0, 59), _field_values(parts[1], 0, 23)
    if minutes is None or hours is None:
        return None
    if len(parts) == 7 and _field_values(parts[6], 1970, 2199) is None:
        return None                                           # kolom tahun tidak dikenali
    # Kolom hari/bulan/tahun hanya bisa MENJARANGKAN, jadi jarak terpendek dalam sehari adalah batas bawah
    # yang aman.
    gap = float(_min_gap([h * 60 + m for h in hours for m in minutes], 1440))
    if len(parts) >= 6:
        seconds = _field_values(parts[5], 0, 59)
        if seconds is None:
            return None
        if len(seconds) > 1:                                  # beberapa kali dalam menit yang sama
            gap = min(gap, _min_gap(list(seconds), 60) / 60.0)
    return gap


def schedule_interval_minutes(schedule: Any) -> Optional[float]:
    """Jarak terpendek antar eksekusi (menit). `math.inf` = sekali jalan; None = tidak bisa diurai
    (pemanggil membedakan "berbentuk cron" dari teks bebas lewat `looks_like_cron`)."""
    if not isinstance(schedule, str) or not schedule.strip():
        return None
    text = schedule.strip()
    lower = text.lower()
    is_every = lower.startswith("every ")
    rest = lower[6:].strip() if is_every else lower
    # Urutan sama dengan Hermes: frasa hari dulu ("every monday 9am", "weekdays at 9am"), lalu "every X",
    # baru ekspresi cron.
    if _is_day_phrase(rest, is_every):
        return 1440.0
    if is_every:
        return _duration_minutes(rest)
    parts = text.split()
    if len(parts) >= 5 and all(_CRON_FIELD.match(p) for p in parts[:5]):
        return _cron_minutes(parts)
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
    if interval is None and looks_like_cron(schedule):
        return MSG_CRON_UNKNOWN.format(schedule=schedule.strip()[:80], limit=limit)
    if interval is None:
        log_once("cron-unparsed", "jadwal cron tidak bisa diurai penjaga interval (contoh: %r); diloloskan",
                 schedule[:80])
        return None
    if interval >= limit:
        return None
    return MSG_CRON.format(schedule=schedule.strip()[:80], every=_human(interval), limit=limit)


# ----------------------------------------------------------------------------- laju pembuatan / perubahan
# Bentuk sekali-jalan ("in 1m", timestamp ISO) lolos pemeriksaan interval, padahal job sekali-jalan yang
# membuat job berikutnya (rantai) = satu giliran LLM per menit tanpa batas. Selain itu `action=run`
# memicu job seketika. Karena itu SETIAP pembuatan / perubahan / pemicuan dihitung dalam jendela bergulir
# satu jam per profile.
RATE_WINDOW = 3600.0
RATED_ACTIONS = frozenset({"create", "update", "run"})
MSG_CRON_RATE = ("Terlalu banyak membuat/mengubah/menjalankan jadwal cron: batas tim {limit} kali per jam per "
                 "profile (rantai job sekali-jalan tetap memakai satu giliran LLM tiap kali). Coba lagi dalam "
                 "±{wait}, atau pakai satu jadwal berulang (mis. 'every {every}m'). Minta admin tim bila memang perlu.")


def rate_limit_per_hour(min_minutes: Any) -> int:
    """Jatah pembuatan/perubahan/pemicuan per jam per profile: `max(3, 60 // max(1, cron_min_interval_minutes))`."""
    try:
        minutes = int(min_minutes or 0)
    except (TypeError, ValueError):
        minutes = 0
    return max(3, 60 // max(1, minutes))


class CronRate:
    """Jendela bergulir per profile, di memori, aman antar-thread. Satu objek per proses (slot bersama), jam
    disuntikkan lewat argumen `now` (uji memakai jam tiruan)."""

    def __init__(self, window: float = RATE_WINDOW):
        self.window = float(window)
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}

    def acquire(self, profile: str, limit: int, now: float) -> float:
        """0.0 = diizinkan (dan dicatat); selain itu detik sampai satu slot bebas (tidak dicatat)."""
        with self._lock:
            hits = self._hits.setdefault(str(profile), deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) < limit:
                hits.append(now)
                return 0.0
            return max(1.0, hits[0] + self.window - now)


def is_rated_call(tool_name: Any, args: Any) -> bool:
    """Panggilan tool cron yang membuat / mengubah / memicu job (bukan list/pause/resume/remove)."""
    if str(tool_name or "") not in CRON_TOOLS or not isinstance(args, dict):
        return False
    action = args.get("action")
    action = action.strip().lower() if isinstance(action, str) else ""
    if action:
        return action in RATED_ACTIONS
    return isinstance(args.get("schedule"), str) and bool(args["schedule"].strip())     # bentuk lama tanpa `action`


def check_cron_rate(tool_name: Any, args: Any, min_minutes: Any, rate: CronRate, profile: str,
                    now: Callable[[], float]) -> Optional[str]:
    """Pesan penolakan bila profile melewati jatah per jam; None = boleh (dan dicatat). Nonaktif bila
    `cron_min_interval_minutes` kosong/0, sama dengan penjaga interval."""
    try:
        limit_minutes = int(min_minutes or 0)
    except (TypeError, ValueError):
        return None
    if limit_minutes <= 0 or not is_rated_call(tool_name, args):
        return None
    limit = rate_limit_per_hour(limit_minutes)
    wait = rate.acquire(profile, limit, now())
    if wait <= 0:
        return None
    return MSG_CRON_RATE.format(limit=limit, wait=_human(wait) if wait < 60 else f"{int(math.ceil(wait / 60))} menit",
                                every=limit_minutes)
