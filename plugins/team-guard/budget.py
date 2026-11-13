"""Anggaran token harian per orang dan per tim.

Sumber angka: buku besar pemakaian kit (`usage.db`, tabel `daily`, ditulis `usage_ledger.py collect`).
Plugin hanya MEMBACA (sqlite `mode=ro`) dan tidak pernah mengimpor modul kit; supaya angkanya segar,
kolektor kit dijalankan sebagai subprocess lepas paling sering tiap `COLLECT_EVERY` detik.

Gagal-terbuka: buku besar yang belum ada / tidak terbaca mematikan penegakan anggaran dengan SATU baris
log — pesan anggota tetap dilayani. Itu disengaja: gangguan pencatatan tidak boleh menghentikan tim.
Supaya tidak diam-diam: bila potret terakhir kolektor (`meta.last_collect`) lebih tua dari `STALE_AFTER`
(atau buku besar tidak terbaca selama itu), admin dikabari sekali sehari dan metrics.json memuat
`budget.stale: true`.

Jatah tambahan per orang (`budget_overrides.json`) HANYA menaikkan batas orang itu; plafon tim tetap
seperti di konfigurasi (batas keras).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .shared import log_once

CACHE_TTL = 60.0            # hasil baca buku besar dipakai ulang selama ini
COLLECT_EVERY = 120.0       # jeda minimal antar kolektor
WARN_RATIO = 0.8
STALE_AFTER = 1800.0        # potret kolektor lebih tua dari ini → anggaran dianggap tidak ditegakkan

ADMIN_BUDGET_STALE = ("⚠️ Anggaran tidak ditegakkan: buku besar basi sejak {since} (kolektor pemakaian "
                      "`usage_ledger.py collect` gagal atau tidak jalan). Pesan anggota tetap dilayani dengan "
                      "angka lama; periksa kolektor di server.")
ADMIN_BUDGET_UNREADABLE = ("⚠️ Anggaran tidak ditegakkan: buku besar pemakaian tidak terbaca / belum pernah diisi "
                           "kolektor sejak {since}. Pesan anggota tetap dilayani tanpa batas token; periksa "
                           "`usage_ledger.py collect` di server.")


def spawn_detached(argv: list[str], env: dict) -> Any:
    """Kolektor sebagai proses lepas (sesi sendiri, tanpa stdio); tidak pernah ditunggu."""
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=env, close_fds=True, start_new_session=True)


def collector_argv(roster: Any) -> Optional[list[str]]:
    """`<python kit> <folder kit>/usage_ledger.py [opsi] collect`, atau None bila roster tidak memuat kit."""
    kit = getattr(roster, "kit", None) or {}
    script = kit.get("script")
    if not script:
        return None
    tool = Path(script).parent / "usage_ledger.py"
    usage = usage_config(roster)
    argv = [kit.get("python") or "python3", str(tool)]
    if usage["ledger"]:
        argv += ["--ledger", usage["ledger"]]
    argv += ["--hermes-root", str(roster.hermes_root)]
    if kit.get("members_file"):
        argv += ["--members-file", kit["members_file"]]
    if usage["timezone"]:
        argv += ["--tz", usage["timezone"]]
    return argv + ["collect"]


def usage_config(roster: Any) -> dict:
    raw = getattr(roster, "usage", None)
    raw = raw if isinstance(raw, dict) else {}
    return {"timezone": str(raw.get("timezone") or "").strip(), "ledger": str(raw.get("ledger") or "").strip()}


def resolve_tz(name: str) -> Any:
    """Zona waktu IANA dari roster; kosong / tidak dikenal → zona waktu server (None)."""
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            log_once(f"budget-tz-{name}", "zona waktu '%s' tidak dikenal; anggaran memakai waktu server", name)
    return None


def day_of(ts: float, tz: Any) -> str:
    stamp = datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)
    return stamp.date().isoformat()


def read_ledger(ledger: str, day: str) -> tuple[dict[str, int], Optional[float]]:
    """(user_key → token (input + output) pada `day`, waktu `meta.last_collect` atau None bila tidak ada).
    Baca-saja; melempar sqlite3.Error/OSError bila gagal."""
    path = Path(ledger).expanduser()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=0.25)
    try:
        rows = con.execute("SELECT user_key, SUM(input_tokens + output_tokens) FROM daily WHERE day = ? "
                           "GROUP BY user_key", (day,)).fetchall()
        try:
            found = con.execute("SELECT value FROM meta WHERE key = 'last_collect'").fetchone()
            last = float(found[0]) if found else None
        except (sqlite3.Error, TypeError, ValueError):
            last = None                           # buku besar lama tanpa tabel meta
    finally:
        con.close()
    return {str(key): int(total or 0) for key, total in rows}, last


def read_usage(ledger: str, day: str) -> dict[str, int]:
    """user_key → token (input + output) pada `day`. Baca-saja; melempar sqlite3.Error/OSError bila gagal."""
    return read_ledger(ledger, day)[0]


class Verdict:
    """Hasil pemeriksaan satu pesan. `state`: ok | warn | over | team_over."""
    __slots__ = ("state", "used", "limit", "team_used", "team_limit", "first", "team_warn", "stale_notice")

    def __init__(self, state: str = "ok", used: int = 0, limit: int = 0, team_used: int = 0, team_limit: int = 0,
                 first: bool = False, team_warn: bool = False):
        self.state, self.used, self.limit = state, used, limit
        self.team_used, self.team_limit = team_used, team_limit
        self.first = first              # pertama kali hari ini untuk orang ini (→ kabari admin / peringatkan)
        self.team_warn = team_warn      # tim baru saja melewati 80% (→ kabari admin sekali sehari)
        self.stale_notice = ""          # buku besar basi (→ kabari admin; terisi paling banyak sekali sehari)

    @property
    def refused(self) -> bool:
        return self.state in ("over", "team_over")


class Budget:
    def __init__(self, state_dir: Path, *, wall: Callable[[], float] = time.time,
                 spawn: Callable[[list[str], dict], Any] = spawn_detached):
        self.state_dir = Path(state_dir)
        self.wall, self.spawn = wall, spawn
        self._lock = threading.Lock()
        self._cache: Optional[tuple[float, str, str, dict[str, int]]] = None    # (waktu, hari, ledger, pemakaian)
        self._overrides: tuple[Any, dict] = (None, {})
        self._collected = -1e18
        self._proc: Any = None
        self._told: set[tuple[str, str, str]] = set()                           # (hari, jenis, uid)
        self._last_collect: Optional[float] = None      # meta.last_collect dari pembacaan terakhir yang berhasil
        self._since: Optional[float] = None             # pertama kali anggaran diperiksa (acuan bila belum ada potret)

    # ------------------------------------------------------------------ sumber data
    def today(self, roster: Any) -> str:
        return day_of(self.wall(), resolve_tz(usage_config(roster)["timezone"]))

    def usage(self, roster: Any, quiet: bool = False) -> Optional[dict[str, int]]:
        """Pemakaian hari ini per user_key (cache 60 detik); None bila buku besar tidak tersedia."""
        ledger = usage_config(roster)["ledger"]
        if not ledger:
            if not quiet:
                log_once("budget-no-ledger", "anggaran token nonaktif: roster.json tidak memuat usage.ledger "
                         "(jalankan ulang `hermes_team.py apply`)")
            return None
        now, day = self.wall(), self.today(roster)
        with self._lock:
            cached = self._cache
            if cached is not None and cached[1] == day and cached[2] == ledger and 0 <= now - cached[0] < CACHE_TTL:
                return cached[3]
            try:
                used, self._last_collect = read_ledger(ledger, day)
            except (OSError, sqlite3.Error, ValueError) as exc:
                if cached is not None and cached[1] == day and cached[2] == ledger:
                    return cached[3]              # sedang ditulis kolektor: pakai angka terakhir hari ini
                if not quiet:
                    log_once("budget-ledger", "anggaran token nonaktif: buku besar %s tidak terbaca (%s: %s); "
                             "pesan tetap dilayani", ledger, type(exc).__name__, exc)
                return None
            self._cache = (now, day, ledger, used)
            return used

    def overrides(self, day: str) -> dict[str, int]:
        """uid → token tambahan dari `<state_dir>/budget_overrides.json` yang berlaku pada `day`."""
        path = self.state_dir / "budget_overrides.json"
        try:
            st = path.stat()
            stamp: Any = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        with self._lock:
            if stamp != self._overrides[0]:
                data: dict = {}
                if stamp is not None:
                    try:
                        loaded = json.loads(path.read_text(encoding="utf-8"))
                        data = loaded if isinstance(loaded, dict) else {}
                    except (OSError, ValueError) as exc:
                        log_once("budget-overrides", "budget_overrides.json tidak terbaca (%s); diabaikan", exc)
                self._overrides = (stamp, data)
            raw = self._overrides[1]
        out: dict[str, int] = {}
        for uid, entry in raw.items():
            if isinstance(entry, dict) and str(entry.get("day") or "") == day:
                try:
                    extra = int(entry.get("extra_tokens") or 0)
                except (TypeError, ValueError):
                    continue
                if extra > 0:
                    out[str(uid)] = extra
        return out

    def refresh(self, roster: Any) -> bool:
        """Jalankan kolektor kit (lepas, tidak ditunggu) paling sering tiap COLLECT_EVERY detik."""
        now = self.wall()
        with self._lock:
            if 0 <= now - self._collected < COLLECT_EVERY:
                return False
            proc = self._proc
            if proc is not None and getattr(proc, "poll", None) is not None and proc.poll() is None:
                return False                      # kolektor sebelumnya masih jalan
            self._collected = now
        argv = collector_argv(roster)
        if argv is None:
            return False
        env = dict(os.environ)
        env["HERMES_HOME"] = str(roster.hermes_root)
        try:
            self._proc = self.spawn(argv, env)
            return True
        except Exception as exc:
            log_once("budget-collect", "kolektor pemakaian tidak bisa dijalankan (%s: %s); anggaran memakai "
                     "angka terakhir di buku besar", type(exc).__name__, exc)
            return False

    def staleness(self, roster: Any) -> tuple[bool, Optional[float]]:
        """(basi?, acuan waktunya). Basi = potret kolektor terakhir lebih tua dari STALE_AFTER; bila belum
        pernah ada potret (buku besar tidak terbaca / belum diisi), acuannya saat anggaran pertama diperiksa."""
        if not usage_config(roster)["ledger"]:
            return False, None
        now = self.wall()
        with self._lock:
            if self._since is None:
                self._since = now
            reference = self._last_collect if self._last_collect is not None else self._since
        return now - reference > STALE_AFTER, reference

    def _stale_notice(self, roster: Any, day: str) -> str:
        """Teks untuk #hermes-admin (paling banyak sekali sehari) + satu baris log; "" bila tidak basi."""
        stale, reference = self.staleness(roster)
        if not stale or reference is None:
            return ""
        tz = resolve_tz(usage_config(roster)["timezone"])
        stamp = datetime.fromtimestamp(reference, tz) if tz is not None else datetime.fromtimestamp(reference)
        since = stamp.strftime("%Y-%m-%d %H:%M")
        log_once("budget-stale", "anggaran tidak ditegakkan: buku besar basi sejak %s (kolektor pemakaian gagal "
                 "atau tidak jalan)", since)
        if not self._once(day, "stale", ""):
            return ""
        template = ADMIN_BUDGET_STALE if self._last_collect is not None else ADMIN_BUDGET_UNREADABLE
        return template.format(since=since)

    # ------------------------------------------------------------------ keputusan
    def _once(self, day: str, kind: str, uid: str) -> bool:
        key = (day, kind, uid)
        with self._lock:
            if key in self._told:
                return False
            self._told = {k for k in self._told if k[0] == day}
            self._told.add(key)
            return True

    def check(self, roster: Any, user_id: str) -> Verdict:
        """Keputusan untuk satu pesan anggota non-admin. Tidak pernah melempar."""
        try:
            return self._check(roster, str(user_id))
        except Exception as exc:
            log_once("budget-check", "pemeriksaan anggaran gagal (%s: %s); pesan tetap dilayani",
                     type(exc).__name__, exc)
            return Verdict()

    def _check(self, roster: Any, uid: str) -> Verdict:
        settings = roster.settings
        per_user = _int(settings.get("daily_token_budget"))
        per_team = _int(settings.get("team_daily_token_budget"))
        if per_user <= 0 and per_team <= 0:
            return Verdict()
        self.refresh(roster)
        used_map = self.usage(roster)
        day = self.today(roster)
        stale_notice = self._stale_notice(roster, day)
        if used_map is None:
            verdict = Verdict()
            verdict.stale_notice = stale_notice
            return verdict
        extras = self.overrides(day)
        used = int(used_map.get(uid, 0))
        limit = per_user + extras.get(uid, 0) if per_user > 0 else 0
        team_used = sum(used_map.values())
        # Jatah tambahan per orang TIDAK menaikkan plafon tim: anggaran tim adalah batas keras. (Dulu ikut
        # dijumlahkan, sehingga tambahan untuk satu orang melonggarkan batas semua orang.)
        team_limit = per_team if per_team > 0 else 0
        verdict = Verdict("ok", used, limit, team_used, team_limit)
        verdict.stale_notice = stale_notice
        if team_limit > 0 and team_used >= WARN_RATIO * team_limit and team_used < team_limit:
            verdict.team_warn = self._once(day, "team-warn", "")
        if limit > 0 and used >= limit:
            verdict.state, verdict.first = "over", self._once(day, "over", uid)
        elif team_limit > 0 and team_used >= team_limit:
            verdict.state, verdict.first = "team_over", self._once(day, "team-over", "")
        elif limit > 0 and used >= WARN_RATIO * limit:
            verdict.state, verdict.first = "warn", self._once(day, "warn", uid)
            if not verdict.first:
                verdict.state = "ok"              # peringatan 80% hanya sekali per orang per hari
        return verdict

    def snapshot(self, roster: Any) -> dict:
        """Bagian `budget` di metrics.json."""
        try:
            day = self.today(roster)
            used = self.usage(roster, quiet=True) or {}
        except Exception:
            day, used = "", {}
        try:
            stale = self.staleness(roster)[0]
        except Exception:
            stale = False
        # `stale: true` = potret kolektor terlalu tua → angka di atas BUKAN keadaan sekarang dan anggaran
        # praktis tidak ditegakkan. `last_collect` = waktu potret terakhir (epoch) atau null.
        return {"day": day, "used_by_user": dict(used), "team_used": sum(used.values()), "stale": bool(stale),
                "last_collect": self._last_collect}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
