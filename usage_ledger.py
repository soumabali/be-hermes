#!/usr/bin/env python3
"""Buku besar pemakaian LLM tim: token dan request per orang, per hari / minggu / bulan.

Kenapa ada, padahal Hermes sudah mencatat di state.db?
- state.db menyimpan angka KUMULATIF per sesi, bukan per hari, dan barisnya ikut hilang saat riwayat
  sesi dibersihkan (`hermes sessions prune`, auto_prune). Buku besar ini memotret selisihnya secara
  berkala dan menyimpannya per hari di berkas sendiri, jadi riwayat pemakaian tetap utuh.
- Pemakaian diatribusikan ke ORANG (user Discord dari tabel `sessions`), bukan hanya ke profile, jadi
  pemakaian di #hermes-shared terbagi benar. Sesi tanpa user (cron, subagent) ikut pemilik profile.

Keamanan:
- state.db dibuka HANYA-BACA (`mode=ro`); tidak ada tulisan ke data Hermes.
- Yang disimpan hanya angka, nama model, nama profile, dan ID user Discord. Tidak ada isi percakapan,
  judul sesi, prompt, maupun secret.
- Buku besar: <state_dir>/usage.db, mode 0600, satu penulis pada satu waktu (kunci berkas), transaksi
  atomik. Pembaca tidak pernah menulis ke state.db dan boleh jalan kapan saja.

Pemakaian:
  python3 usage_ledger.py collect                       # potret selisih (jalankan tiap 15 menit)
  python3 usage_ledger.py report --period day           # hari ini
  python3 usage_ledger.py report --period week --offset 1 --format markdown   # minggu lalu
  python3 usage_ledger.py report --period month --user budi --format json
  python3 usage_ledger.py report --period day --last 7  # 7 hari terakhir, per hari

"request" = panggilan API ke model (`api_call_count`), bukan jumlah pesan chat.
Ketelitian batas hari = selang `collect`: pemakaian dicatat pada hari saat ia dipotret.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# Folder skrip harus ada di sys.path walau Python dijalankan dengan -I / PYTHONSAFEPATH (launcher tertentu
# memakainya); modul kit lain (kit_*, kit/, teamctl/) berada di folder yang sama.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import kit_paths

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

METRICS = ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
           "reasoning_tokens", "cost_usd")
# kolom state.db → kolom buku besar
SOURCE_COLS = {"requests": "api_call_count", "input_tokens": "input_tokens", "output_tokens": "output_tokens",
               "cache_read_tokens": "cache_read_tokens", "cache_write_tokens": "cache_write_tokens",
               "reasoning_tokens": "reasoning_tokens"}
RETENTION_DAYS = 800
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    day TEXT NOT NULL, user_key TEXT NOT NULL, profile TEXT NOT NULL, model TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    requests INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_key, profile, model, source));
CREATE INDEX IF NOT EXISTS idx_daily_user ON daily(user_key, day);
CREATE TABLE IF NOT EXISTS snapshot (
    profile TEXT NOT NULL, row_key TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0, seen REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (profile, row_key));
CREATE TABLE IF NOT EXISTS session_days (
    day TEXT NOT NULL, user_key TEXT NOT NULL, profile TEXT NOT NULL, session_id TEXT NOT NULL,
    PRIMARY KEY (day, user_key, profile, session_id));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class LedgerError(Exception):
    pass


# ----------------------------------------------------------------------------- waktu
def team_timezone(team_file: Optional[Path] = None) -> str:
    """`usage.timezone` dari team.yaml tim (kosong bila tidak ada / tidak terbaca)."""
    return kit_paths.usage_timezone(team_file)


def _team_raw(team_file: Optional[Path] = None) -> dict:
    """Isi mentah team.yaml lewat SATU pembaca bersama (kit_paths.load_raw). Tidak terbaca → {}."""
    return kit_paths.load_raw(team_file)


DEFAULT_PROFILE_PREFIX = "be-"


def team_profile_prefix(team_file: Optional[Path] = None) -> str:
    """`profile_prefix` dari TEAM.yaml (bukan members.yaml). Aturannya sama dengan hermes_team.load_team:
    key tidak ada → "be-"; string kosong sah (profile = nama anggota)."""
    return kit_paths.profile_prefix(team_file, DEFAULT_PROFILE_PREFIX)


def resolve_tz(name: Optional[str] = None):
    """Urutan: argumen → HERMES_TEAM_TZ → team.yaml `usage.timezone` → zona waktu server."""
    name = name or os.environ.get("HERMES_TEAM_TZ") or team_timezone()
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception as exc:
            raise LedgerError(f"zona waktu '{name}' tidak dikenal ({exc})")
    return datetime.now().astimezone().tzinfo or timezone.utc


def day_of(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, tz).date().isoformat()


def period_range(period: str, today: date, offset: int = 0) -> tuple[date, date, str]:
    """(awal, akhir inklusif, label). offset=1 → periode sebelumnya. Minggu = Senin–Minggu (ISO)."""
    if offset < 0:
        raise LedgerError("offset tidak boleh negatif")
    if period == "day":
        d = today - timedelta(days=offset)
        return d, d, d.isoformat()
    if period == "week":
        start = today - timedelta(days=today.weekday()) - timedelta(weeks=offset)
        iso = start.isocalendar()
        return start, start + timedelta(days=6), f"{iso[0]}-W{iso[1]:02d} ({start.isoformat()} s.d. {(start + timedelta(days=6)).isoformat()})"
    if period == "month":
        y, m = today.year, today.month
        for _ in range(offset):
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        start = date(y, m, 1)
        end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
        return start, end, f"{y}-{m:02d}"
    raise LedgerError(f"periode '{period}' tidak dikenal (day, week, month)")


# ----------------------------------------------------------------------------- berkas
def default_ledger_path() -> Path:
    return kit_paths.ledger_path()


@contextlib.contextmanager
def _writer_lock(path: Path):
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(lock.parent, 0o700)
    fh = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - non-POSIX
            pass
        yield
    finally:
        os.close(fh)


def _check_schema_version(con: sqlite3.Connection, path: Path) -> None:
    """Buku besar ditulis kit versi lebih baru → pembaca lama menolak dengan pesan jelas, bukan membaca kolom yang
    mungkin sudah berubah arti. Tanpa baris versi (berkas lama) dianggap versi 1."""
    try:
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error:
        return
    try:
        found = int(row[0]) if row else 1
    except (TypeError, ValueError):
        found = 1
    if found > SCHEMA_VERSION:
        con.close()
        raise LedgerError(f"buku besar {path} berskema {found}, lebih baru dari yang dipahami kit ini "
                          f"({SCHEMA_VERSION}); perbarui kit (VERSION {kit_paths_version()}) sebelum membacanya")


def kit_paths_version() -> str:
    try:
        from kit_base import KIT_VERSION
        return KIT_VERSION
    except Exception:       # noqa: BLE001 — hanya untuk pesan galat
        return "?"


def open_ledger(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path).expanduser()
    if readonly:
        if not path.exists():
            raise LedgerError(f"buku besar belum ada di {path} — jalankan `usage_ledger.py collect` dulu")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        _check_schema_version(con, path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        if fresh:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        con = sqlite3.connect(path, timeout=30)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        con.executescript(SCHEMA)
        _check_schema_version(con, path)       # sebelum menulis apa pun ke buku besar milik kit yang lebih baru
        con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        con.commit()
    con.row_factory = sqlite3.Row
    return con


# ----------------------------------------------------------------------------- baca state.db
def profile_homes(root: Path) -> list[tuple[str, Path]]:
    root = Path(root).expanduser()
    found = [("default", root)] if (root / "state.db").exists() else []
    base = root / "profiles"
    if base.is_dir():
        found += [(p.name, p) for p in sorted(base.iterdir()) if (p / "state.db").exists()]
    return found


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def read_state(db: Path) -> list[dict]:
    """Baris kumulatif per (sesi, model, …) dari satu state.db, hanya-baca. Kolom yang tidak ada di
    versi skema lama diisi 0/kosong. Lempar sqlite3.Error bila tabel pemakaian tidak ada."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        con.row_factory = sqlite3.Row
        ucols = _columns(con, "session_model_usage")
        if not ucols:
            raise sqlite3.OperationalError("tabel session_model_usage tidak ada")
        scols = _columns(con, "sessions")
        pick = lambda c, default="0": (f"u.{c}" if c in ucols else default)  # noqa: E731
        cost = ("CASE WHEN COALESCE(u.actual_cost_usd,0) > 0 THEN u.actual_cost_usd ELSE COALESCE(u.estimated_cost_usd,0) END"
                if {"actual_cost_usd", "estimated_cost_usd"} <= ucols else pick("estimated_cost_usd"))
        join = user = source = ""
        if "id" in scols:
            join = " LEFT JOIN sessions s ON s.id = u.session_id"
            user_expr = "s.user_id" if "user_id" in scols else "NULL"
            if "parent_session_id" in scols and "user_id" in scols:
                join += " LEFT JOIN sessions p ON p.id = s.parent_session_id"
                user_expr = "COALESCE(NULLIF(s.user_id,''), p.user_id)"
            user = f", {user_expr} AS user_id"
            source = ", s.source AS source" if "source" in scols else ""
        rows = con.execute(
            "SELECT u.session_id AS session_id, u.model AS model, "
            + ", ".join(f"{pick(c, repr(''))} AS {c}" for c in ("billing_provider", "billing_base_url", "billing_mode", "task"))
            + ", " + ", ".join(f"COALESCE({pick(src)},0) AS {name}" for name, src in SOURCE_COLS.items())
            + f", {cost} AS cost_usd, COALESCE({pick('last_seen', 'NULL')}, {pick('first_seen', 'NULL')}, 0) AS seen"
            + user + source + " FROM session_model_usage u" + join).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["row_key"] = "\x1f".join(str(d.get(k) or "") for k in
                                   ("session_id", "model", "billing_provider", "billing_base_url", "billing_mode", "task"))
        d.setdefault("user_id", None)
        d.setdefault("source", "")
        out.append(d)
    return out


# ----------------------------------------------------------------------------- potret
def user_key_for(row: dict, profile: str, owners: dict[str, str]) -> str:
    """ID user Discord bila sesi punya pengirim; kalau tidak, pemilik profile; kalau tidak ada juga,
    `profile:<nama>` (mis. cron di team-shared)."""
    uid = str(row.get("user_id") or "").strip()
    if uid.isdigit():
        return uid
    return owners.get(profile) or f"profile:{profile}"


def collect(ledger: Path, hermes_root: Path, *, owners: Optional[dict[str, str]] = None,
            now: Optional[float] = None, tz=None) -> dict:
    """Potret selisih sejak potret terakhir ke tabel `daily`. Aman dijalankan kapan saja dan
    berulang: tanpa pemakaian baru hasilnya nol baris. `owners`: profile → ID user pemiliknya."""
    now = time.time() if now is None else now
    tz = tz or resolve_tz()
    today = day_of(now, tz)
    owners = owners or {}
    summary = {"day": today, "profiles": 0, "rows": 0, "requests": 0, "tokens": 0, "skipped": []}
    with _writer_lock(Path(ledger)):
        con = open_ledger(ledger)
        try:
            for profile, home in profile_homes(hermes_root):
                try:
                    rows = read_state(home / "state.db")
                except sqlite3.Error as exc:
                    summary["skipped"].append({"profile": profile, "reason": str(exc)})
                    continue
                summary["profiles"] += 1
                seeded = con.execute("SELECT 1 FROM meta WHERE key=?", (f"seeded:{profile}",)).fetchone()
                snaps = {r["row_key"]: r for r in
                         con.execute("SELECT * FROM snapshot WHERE profile=?", (profile,))}
                with con:   # satu transaksi per profile
                    for row in rows:
                        snap = snaps.get(row["row_key"])
                        delta = {m: (row[m] or 0) - ((snap[m] if snap else 0) or 0) for m in METRICS}
                        if any(delta[m] < 0 for m in METRICS if m != "cost_usd"):
                            delta = {m: row[m] or 0 for m in METRICS}      # penghitung di-reset → hitung dari nol
                        delta["cost_usd"] = max(0.0, float(delta["cost_usd"]))
                        if not any(delta[m] > 0 for m in METRICS):
                            continue
                        # Potret PERTAMA sebuah profile: riwayat lama ditaruh di hari sesi itu terakhir aktif.
                        day = day_of(row["seen"], tz) if (not seeded and row["seen"]) else today
                        key = user_key_for(row, profile, owners)
                        con.execute(
                            "INSERT INTO daily (day, user_key, profile, model, source, " + ", ".join(METRICS) + ") "
                            "VALUES (?,?,?,?,?," + ",".join("?" * len(METRICS)) + ") "
                            "ON CONFLICT(day, user_key, profile, model, source) DO UPDATE SET "
                            + ", ".join(f"{m} = {m} + excluded.{m}" for m in METRICS),
                            (day, key, profile, row["model"], str(row.get("source") or ""), *[delta[m] for m in METRICS]))
                        con.execute("INSERT OR IGNORE INTO session_days VALUES (?,?,?,?)",
                                    (day, key, profile, row["session_id"]))
                        con.execute(
                            "INSERT INTO snapshot (profile, row_key, " + ", ".join(METRICS) + ", seen) "
                            "VALUES (?,?," + ",".join("?" * len(METRICS)) + ",?) "
                            "ON CONFLICT(profile, row_key) DO UPDATE SET "
                            + ", ".join(f"{m} = excluded.{m}" for m in METRICS) + ", seen = excluded.seen",
                            (profile, row["row_key"], *[row[m] or 0 for m in METRICS], row["seen"] or now))
                        summary["rows"] += 1
                        summary["requests"] += int(delta["requests"])
                        summary["tokens"] += int(delta["input_tokens"] + delta["output_tokens"])
                    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"seeded:{profile}", str(now)))
            with con:
                cutoff = (datetime.fromtimestamp(now, tz).date() - timedelta(days=RETENTION_DAYS)).isoformat()
                con.execute("DELETE FROM daily WHERE day < ?", (cutoff,))
                con.execute("DELETE FROM session_days WHERE day < ?", (cutoff,))
                # Potret sesi yang sudah lama tak berubah tidak perlu disimpan selamanya.
                con.execute("DELETE FROM snapshot WHERE seen < ?", (now - RETENTION_DAYS * 86400,))
                con.execute("INSERT OR REPLACE INTO meta VALUES ('last_collect', ?)", (str(now),))
        finally:
            con.close()
    return summary


# ----------------------------------------------------------------------------- laporan
def member_profiles(members_file: Optional[Path], prefix: Optional[str] = None,
                    team_file: Optional[Path] = None) -> list[tuple[str, dict]]:
    """[(profile, entri members.yaml)] untuk tiap anggota bernama. Satu-satunya tempat pemetaan
    anggota → profile di luar hermes_team.py (dipakai juga usage_report.py).

    Nama profile: field `profile` eksplisit bila ada, kalau tidak `<profile_prefix><name>`. Prefix dibaca
    dari TEAM.yaml — `team_file`, kalau tidak team.yaml di samping members.yaml, kalau tidak yang
    ditentukan kit_paths — bukan dari members.yaml (dulu salah: prefix selain "be-" membuat pemakaian
    jatuh ke `profile:<nama>` alih-alih ke pemiliknya). Berkas hilang / rusak → daftar kosong."""
    if not members_file:
        return []
    path = Path(members_file).expanduser()
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    if prefix is None:
        beside = path.parent / "team.yaml"
        prefix = team_profile_prefix(team_file or (beside if beside.is_file() else None))
    out = []
    for m in data.get("members") or []:
        if isinstance(m, dict) and m.get("name"):
            out.append((str(m.get("profile") or f"{prefix}{m['name']}"), m))
    return out


def load_members(members_file: Optional[Path], prefix: Optional[str] = None,
                 team_file: Optional[Path] = None) -> tuple[dict[str, str], dict[str, dict]]:
    """(profile → ID user pemilik, ID user → {name, display_name}). Berkas hilang → kosong."""
    owners: dict[str, str] = {}
    people: dict[str, dict] = {}
    for profile, m in member_profiles(members_file, prefix, team_file):
        if not m.get("discord_user_id"):
            continue
        uid, name = str(m["discord_user_id"]), str(m["name"])
        owners[profile] = uid
        people[uid] = {"name": name, "display_name": str(m.get("display_name") or name)}
    return owners, people


def _label(user_key: str, people: dict[str, dict]) -> tuple[str, str]:
    if user_key in people:
        return people[user_key]["name"], people[user_key]["display_name"]
    if user_key.startswith("profile:"):
        return user_key, f"({user_key[8:]}, tanpa pengirim)"
    return user_key, f"user {user_key} (bukan anggota aktif)"


def query(ledger: Path, start: date, end: date, *, people: Optional[dict[str, dict]] = None,
          user: Optional[str] = None, by_day: bool = False) -> dict:
    people = people or {}
    con = open_ledger(ledger, readonly=True)
    try:
        group = "user_key" + (", day" if by_day else "")
        rows = con.execute(
            f"SELECT {group}, " + ", ".join(f"SUM({m}) AS {m}" for m in METRICS)
            + f" FROM daily WHERE day BETWEEN ? AND ? GROUP BY {group} ORDER BY {group}",
            (start.isoformat(), end.isoformat())).fetchall()
        models = con.execute(
            "SELECT user_key, model, SUM(requests) AS requests, SUM(input_tokens + output_tokens) AS tokens "
            "FROM daily WHERE day BETWEEN ? AND ? GROUP BY user_key, model", (start.isoformat(), end.isoformat())).fetchall()
        sessions = dict(con.execute(
            "SELECT user_key, COUNT(DISTINCT profile || ':' || session_id) FROM session_days "
            "WHERE day BETWEEN ? AND ? GROUP BY user_key", (start.isoformat(), end.isoformat())).fetchall())
        last = con.execute("SELECT value FROM meta WHERE key='last_collect'").fetchone()
    finally:
        con.close()
    wanted = (user or "").strip().lower().lstrip("@")
    users: dict[str, dict] = {}
    for r in rows:
        name, display = _label(r["user_key"], people)
        if wanted and wanted not in (r["user_key"].lower(), name.lower(), display.lower()):
            continue
        u = users.setdefault(r["user_key"], {
            "user_key": r["user_key"], "name": name, "display_name": display,
            "total": {m: 0 for m in METRICS}, "sessions": int(sessions.get(r["user_key"], 0)),
            "models": {}, **({"days": {}} if by_day else {})})
        vals = {m: (r[m] or 0) for m in METRICS}
        for m in METRICS:
            u["total"][m] += vals[m]
        if by_day:
            u["days"][r["day"]] = vals
    for r in models:
        if r["user_key"] in users:
            users[r["user_key"]]["models"][r["model"]] = {"requests": int(r["requests"] or 0), "tokens": int(r["tokens"] or 0)}
    ordered = sorted(users.values(), key=lambda u: u["total"]["input_tokens"] + u["total"]["output_tokens"], reverse=True)
    for u in ordered:
        u["total"]["cost_usd"] = round(u["total"]["cost_usd"], 6)
        u["total"]["tokens"] = int(u["total"]["input_tokens"] + u["total"]["output_tokens"])
    totals = {m: sum(u["total"][m] for u in ordered) for m in (*METRICS, "tokens")}
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    return {"start": start.isoformat(), "end": end.isoformat(), "users": ordered, "totals": totals,
            "last_collect": float(last[0]) if last else None}


def _n(value: float) -> str:
    value = int(value)
    return f"{value / 1_000_000:.2f}M" if value >= 1_000_000 else f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def render(report: dict, label: str, fmt: str = "text", *, now: Optional[float] = None) -> str:
    if fmt == "json":
        return json.dumps({"period": label, **report}, indent=2, ensure_ascii=False)
    head = ("Pengguna", "Request", "Token masuk", "Token keluar", "Total token", "Sesi", "Biaya USD")
    rows = [(u["display_name"], str(int(u["total"]["requests"])), _n(u["total"]["input_tokens"]),
             _n(u["total"]["output_tokens"]), _n(u["total"]["tokens"]), str(u["sessions"]),
             f"{u['total']['cost_usd']:.4f}") for u in report["users"]]
    t = report["totals"]
    rows.append(("TOTAL", str(int(t["requests"])), _n(t["input_tokens"]), _n(t["output_tokens"]), _n(t["tokens"]),
                 "", f"{t['cost_usd']:.4f}"))
    title = f"Pemakaian LLM — {label}"
    if fmt == "markdown":
        lines = [f"**{title}**", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        lines += ["| " + " | ".join(r) + " |" for r in rows]
    else:
        table = [head] + rows
        widths = [max(len(r[i]) for r in table) for i in range(len(head))]
        lines = [title, ""] + ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in table]
    if not report["users"]:
        lines.append("" if fmt == "markdown" else "")
        lines.append("Tidak ada pemakaian tercatat pada periode ini.")
    for u in report["users"]:
        if "days" in u and u["days"]:
            lines.append("")
            lines.append(f"{u['display_name']} per hari: " + "; ".join(
                f"{d}: {int(v['requests'])} req, {_n(v['input_tokens'] + v['output_tokens'])} token"
                for d, v in sorted(u["days"].items())))
    if report.get("last_collect"):
        age = int(((time.time() if now is None else now) - report["last_collect"]) / 60)
        lines += ["", f"Data dipotret {age} menit lalu."]
    return "\n".join(lines)


def report(ledger: Path, period: str = "day", *, offset: int = 0, last: int = 0, user: Optional[str] = None,
           fmt: str = "text", people: Optional[dict[str, dict]] = None, now: Optional[float] = None, tz=None) -> str:
    """Laporan siap kirim. `last` > 0 (hanya period=day): N hari terakhir dengan rincian per hari."""
    now = time.time() if now is None else now
    tz = tz or resolve_tz()
    today = datetime.fromtimestamp(now, tz).date()
    if last:
        if period != "day" or not 1 <= last <= 92:
            raise LedgerError("--last hanya untuk period=day, 1–92 hari")
        start, end, label = today - timedelta(days=last - 1), today, f"{last} hari terakhir (s.d. {today.isoformat()})"
        return render(query(ledger, start, end, people=people, user=user, by_day=True), label, fmt, now=now)
    start, end, label = period_range(period, today, offset)
    names = {"day": "harian", "week": "mingguan", "month": "bulanan"}
    return render(query(ledger, start, end, people=people, user=user), f"{names[period]} {label}", fmt, now=now)


# ----------------------------------------------------------------------------- CLI
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Buku besar pemakaian LLM tim (token & request per orang).")
    ap.add_argument("--ledger", help="berkas buku besar (default: $HERMES_TEAM_DIR/state/usage.db, "
                                     "kalau tidak ~/.hermes-team/state/usage.db)")
    ap.add_argument("--hermes-root", default=os.environ.get("HERMES_HOME") or "~/.hermes")
    ap.add_argument("--members-file", default=None,
                    help="members.yaml (default: di folder tim; profile_prefix dibaca dari team.yaml di sampingnya)")
    ap.add_argument("--tz", default=None, help="zona waktu IANA untuk batas hari, mis. Asia/Makassar")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect", help="potret selisih pemakaian sejak potret terakhir")
    rp = sub.add_parser("report", help="laporan per orang")
    rp.add_argument("--period", choices=("day", "week", "month"), default="day")
    rp.add_argument("--offset", type=int, default=0, help="0 = periode berjalan, 1 = periode sebelumnya, …")
    rp.add_argument("--last", type=int, default=0, help="N hari terakhir dengan rincian per hari (period=day)")
    rp.add_argument("--user", help="saring satu orang (nama anggota, nama tampilan, atau ID user)")
    rp.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    rp.add_argument("--no-collect", action="store_true", help="jangan memotret dulu (pakai data terakhir)")
    args = ap.parse_args(argv)
    ledger = Path(args.ledger).expanduser() if args.ledger else default_ledger_path()
    members = Path(args.members_file).expanduser() if args.members_file else kit_paths.members_file()
    try:
        tz = resolve_tz(args.tz)
        owners, people = load_members(members)
        if args.cmd == "collect":
            s = collect(ledger, Path(args.hermes_root), owners=owners, tz=tz)
            print(f"{s['day']}: {s['profiles']} profile, {s['rows']} baris baru, {s['requests']} request, "
                  f"{s['tokens']} token" + (f"; dilewati: {', '.join(x['profile'] for x in s['skipped'])}" if s["skipped"] else ""))
            return 0
        if not args.no_collect:
            with contextlib.suppress(OSError, sqlite3.Error):
                collect(ledger, Path(args.hermes_root), owners=owners, tz=tz)
        print(report(ledger, args.period, offset=args.offset, last=args.last, user=args.user, fmt=args.format,
                     people=people, tz=tz))
        return 0
    except LedgerError as exc:
        print(f"usage_ledger: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
