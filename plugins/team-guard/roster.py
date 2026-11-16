"""Pembaca roster.json — satu-satunya masukan konfigurasi plugin (ditulis hermes_team.py).

Murni Python: tidak mengimpor Hermes maupun discord.py.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from .shared import log_once

# Versi kontrak roster.json yang dipahami plugin ini (docs/KONTRAK.md). Roster tanpa field `schema` = 0
# (kit lama). Roster dengan skema LEBIH BESAR tetap dibaca sebisanya (kunci tak dikenal diabaikan), tetapi
# admin diberi tahu sekali sehari supaya plugin diperbarui bersama kit.
MAX_SUPPORTED_ROSTER_SCHEMA = 1

SETTINGS_DEFAULTS: dict[str, Any] = {
    "rate_per_minute": 6, "burst": 4, "max_active": 8, "queue_max_wait": 180,
    "max_keys": 40, "min_length": 6, "scrub_chat": True, "delete_secret_messages": True,
    "block_env_dump": True, "hint": True, "allow_names": [], "deny_names": [],
    # Skala lanjutan (docs/RENCANA-SKALA.md). 0 / False = mati, supaya roster lama berperilaku seperti dulu.
    "user_max_concurrent": 0, "max_llm_parallel": 0, "daily_token_budget": 0, "team_daily_token_budget": 0,
    "adaptive_throttle": False, "cron_min_interval_minutes": 0,
}
ONBOARDING_DEFAULTS: dict[str, Any] = {
    "enabled": True, "give_entry_role": True, "remove_entry_role": True, "verify_actor": True,
    "trusted_actor_ids": [], "offboard_on_role_remove": True, "hide_channel_on_offboard": True,
    "entry_role_on_offboard": True, "reconcile_minutes": 10, "admin_notices": True,
    "welcome_message": "", "welcome_back_message": "",
}


def roster_schema(data: Any) -> int:
    """`schema` roster sebagai bilangan bulat; tidak ada / bukan bilangan bulat tak-negatif → 0."""
    value = data.get("schema") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def default_state_dir() -> Path:
    base = os.environ.get("HERMES_TEAM_DIR") or "~/.hermes-team"
    return Path(base).expanduser() / "state"


# Field yang, bila WADAHNYA bertipe salah (mis. `channels` berupa daftar), membuat gerbang tidak bisa ditegakkan dengan benar (channel tim tidak dikenali,
# admin/anggota tidak dikenali, setelan pembatas hilang). Roster baru yang rusak di salah satu field ini TIDAK
# menggantikan roster sah yang sedang dipakai (lihat RosterCache.get).
CRITICAL_FIELDS = frozenset({"channels", "users", "inactive_users", "admins", "settings"})


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _coerce_setting(value: Any, default: Any) -> tuple[Any, bool]:
    """(nilai, sah?) untuk satu setelan, dengan tipe nilai bawaannya sebagai acuan. Angka menerima int/float
    (apa adanya) dan teks bilangan bulat; bool hanya bool; daftar hanya list berisi skalar (jadi list[str])."""
    if value is None:                                   # null = "tidak diisi" → bawaan, bukan kesalahan
        return (list(default) if isinstance(default, list) else default), True
    if isinstance(default, bool):
        return (value, True) if isinstance(value, bool) else (default, False)
    if isinstance(default, int):
        if isinstance(value, int) and not isinstance(value, bool):
            return value, True
        if isinstance(value, float) and value == value and abs(value) != float("inf"):
            return value, True                          # pecahan sah (mis. queue_max_wait 0.5)
        if isinstance(value, str) and value.strip():
            try:
                return int(value.strip()), True
            except ValueError:
                pass
        return default, False
    if isinstance(default, list):
        if isinstance(value, list) and all(_is_scalar(v) for v in value):
            return [str(v) for v in value], True
        return list(default), False
    if isinstance(default, str):
        return (value, True) if isinstance(value, str) else (default, False)
    return value, True


class Roster:
    """Tampilan baca-saja atas isi roster.json, dengan nilai bawaan untuk field yang hilang.

    Pembuatan objek ini TOTAL: masukan apa pun (termasuk field bertipe salah) menghasilkan roster yang bisa
    dipakai — field yang salah bentuk diganti nilai bawaan yang aman dan dicatat di `problems` (satu baris log
    per field). Gerbang akses, penyamaran token, guard dump environment, dan penjaga cron bergantung pada
    objek ini; kalau pembuatannya melempar, semuanya ikut terlewat."""

    def __init__(self, data: Optional[dict] = None):
        self.data: dict = data if isinstance(data, dict) else {}
        self.problems: list[str] = []          # "field: apa yang salah" — untuk log & RosterCache.error
        self.critical: list[str] = []          # field di CRITICAL_FIELDS yang bentuknya salah
        d = self.data
        self.schema: int = roster_schema(d)
        self.team_name: str = self._text(d, "team_name") or "tim"
        self.guild_id: str = self._text(d, "guild_id")
        self.admins: set[str] = set(self._str_list(d, "admins"))
        roles = self._mapping(d, "roles")
        self.member_role: str = self._text(roles, "member", "roles.member")
        self.admin_role: str = self._text(roles, "admin", "roles.admin")
        self.entry_role: str = self._text(roles, "entry", "roles.entry")
        self.shared_channel: str = self._text(d, "shared_channel")
        self.admin_channel: str = self._text(d, "admin_channel")
        self.channels: dict[str, dict] = self._entries(d, "channels")
        self.users: dict[str, dict] = self._entries(d, "users")
        self.inactive_users: dict[str, dict] = self._entries(d, "inactive_users")
        self.reserved_env: list[str] = self._str_list(d, "reserved_env")
        self.settings: dict[str, Any] = self._typed(d, "settings", SETTINGS_DEFAULTS)
        self.onboarding: dict[str, Any] = self._typed(d, "onboarding", ONBOARDING_DEFAULTS)
        gw = self._mapping(d, "gateway")
        routes = gw.get("profile_routes")
        if routes is not None and not isinstance(routes, list):
            self._bad("gateway.profile_routes", "bukan daftar")
            routes = []
        self.profile_routes: list[dict] = [dict(r) for r in routes or [] if isinstance(r, dict)]
        self.allowed_channels: list[str] = self._str_list(gw, "allowed_channels", "gateway.allowed_channels")
        self.free_response_channels: list[str] = self._str_list(gw, "free_response_channels",
                                                                "gateway.free_response_channels")
        live = d.get("live_reload", True)
        if not isinstance(live, bool):
            self._bad("live_reload", "bukan true/false")
            live = True
        self.live_reload: bool = live
        self.restart_fallback: str = self._text(d, "restart_fallback") or "idle"
        usage = self._mapping(d, "usage")
        self.usage: dict[str, str] = {"timezone": self._text(usage, "timezone", "usage.timezone").strip(),
                                      "ledger": self._text(usage, "ledger", "usage.ledger").strip()}
        self.kit: dict[str, str] = {str(k): str(v) for k, v in self._mapping(d, "kit").items() if _is_scalar(v)}
        root = self._text(d, "hermes_root") or "~/.hermes"
        try:
            self.hermes_root: Path = Path(root).expanduser()
        except (RuntimeError, ValueError, OSError):
            self._bad("hermes_root", "path tidak bisa dipakai")
            self.hermes_root = Path(root)
        self.hermes_bin: str = self._text(d, "hermes_bin") or "hermes"

    # ------------------------------------------------------------------ pembaca field (tidak pernah melempar)
    def _bad(self, field: str, what: str, structural: bool = False) -> None:
        """`structural` = wadahnya sendiri bertipe salah (bukan satu isi di dalamnya)."""
        self.problems.append(f"{field}: {what}")
        if structural and field in CRITICAL_FIELDS and field not in self.critical:
            self.critical.append(field)

    def _null(self, src: dict, key: str) -> None:
        """Field penentu gerbang yang ADA tetapi `null` = rusak (kit selalu menulis isinya); tidak ada sama sekali
        = roster kit lama, sah."""
        if src is self.data and key in src and key in CRITICAL_FIELDS:
            self._bad(key, "null", structural=True)

    def _text(self, src: dict, key: str, label: str = "") -> str:
        """Teks / angka → teks; kosong/None → ""; bentuk lain (list, dict, bool) → "" + catatan."""
        value = src.get(key)
        if value is None or value == "":
            return ""
        if _is_scalar(value):
            return str(value)
        self._bad(label or key, "bukan teks")
        return ""

    def _mapping(self, src: dict, key: str, label: str = "") -> dict:
        value = src.get(key)
        if value is None:
            self._null(src, key)
            return {}
        if isinstance(value, dict):
            return value
        self._bad(label or key, "bukan objek", structural=True)
        return {}

    def _str_list(self, src: dict, key: str, label: str = "") -> list[str]:
        value = src.get(key)
        if value is None:
            self._null(src, key)
            return []
        if not isinstance(value, list):
            self._bad(label or key, "bukan daftar", structural=True)
            return []
        out = [str(v) for v in value if _is_scalar(v) and str(v)]
        if len(out) != len(value):
            self._bad(label or key, "memuat isi yang bukan teks (diabaikan)")
        return out

    def _entries(self, src: dict, key: str) -> dict[str, dict]:
        """Pemetaan ID → objek. Entri yang isinya bukan objek TETAP dipertahankan sebagai {} (ID-nya masih
        dikenali: channel tim tetap channel tim, anggota nonaktif tetap nonaktif) dan dicatat."""
        out: dict[str, dict] = {}
        broken = 0
        for k, v in self._mapping(src, key).items():
            if isinstance(v, dict):
                out[str(k)] = dict(v)
            else:
                out[str(k)] = {}
                broken += 1
        if broken:
            self._bad(key, f"{broken} entri bukan objek (dianggap kosong)")
        return out

    def _typed(self, src: dict, key: str, defaults: dict) -> dict[str, Any]:
        """Setelan = bawaan + isi roster; nilai yang tipenya tidak cocok dengan bawaannya diganti bawaan itu.
        Kunci yang tidak dikenal (kit lebih baru) dibiarkan apa adanya."""
        out: dict[str, Any] = {k: (list(v) if isinstance(v, list) else v) for k, v in defaults.items()}
        for k, v in self._mapping(src, key).items():
            name = str(k)
            if name in defaults:
                value, ok = _coerce_setting(v, defaults[name])
                if not ok:
                    self._bad(f"{key}.{name}", "tipe salah (dipakai nilai bawaan)")
                out[name] = value
            else:
                out[name] = v
        return out

    def __bool__(self) -> bool:
        return bool(self.data)

    @property
    def newer_schema(self) -> bool:
        """Roster ditulis kit yang lebih baru daripada yang dipahami plugin ini."""
        return self.schema > MAX_SUPPORTED_ROSTER_SCHEMA

    # ------------------------------------------------------------------ orang & channel
    def is_admin(self, user_id: Any) -> bool:
        return str(user_id) in self.admins

    def channel_entry(self, *channel_ids: Any) -> Optional[tuple[str, dict]]:
        """Entri channel terdaftar untuk salah satu ID (channel, parent thread, thread)."""
        for cid in channel_ids:
            key = str(cid) if cid else ""
            if key and key in self.channels:
                return key, self.channels[key]
        return None

    def user_status(self, user_id: Any) -> str:
        uid = str(user_id)
        if uid in self.admins:
            return "admin"
        if uid in self.users:
            return "active"
        if uid in self.inactive_users:
            return "inactive"
        return "unknown"

    # ------------------------------------------------------------------ profile
    def profile_home(self, profile: str) -> Path:
        return self.hermes_root if profile in ("", "default") else self.hermes_root / "profiles" / profile

    def profile_of_home(self, home: Any) -> str:
        return profile_name_of_home(home, self.hermes_root)

    def profile_kind(self, profile: str) -> str:
        if profile == "default":
            return "host"
        for entry in self.channels.values():
            if entry.get("profile") == profile:
                return {"personal": "member"}.get(str(entry.get("kind")), str(entry.get("kind") or "other"))
        return "other"

    def _profile_entry(self, profile: str) -> dict:
        for entry in self.channels.values():
            if entry.get("profile") == profile:
                return entry
        return {}

    def profile_cron_exempt(self, profile: str) -> bool:
        """Profile ini boleh membuat cron job rapat? Kit menulis `cron_exempt: true` di entri channel jenisnya
        (PROFILE_KINDS); roster lama tanpa kunci itu: hanya jenis `admin`."""
        entry = self._profile_entry(profile)
        if entry.get("kind") == "personal":            # profile anggota tidak pernah dikecualikan, apa pun isi roster
            return False
        if "cron_exempt" in entry:
            return entry["cron_exempt"] is True          # hanya boolean true ("false", 1, dst. = tidak)
        return entry.get("kind") == "admin"

    def channel_is_admin(self, entry: dict) -> bool:
        """Channel milik role admin? Kit menulis `role: "admin"` untuk jenis ber-role admin; roster lama: kind admin."""
        if "role" in entry:
            return entry["role"] == "admin"
        return entry.get("kind") == "admin"

    def kind_channel(self, kind: str) -> str:
        """ID channel pertama yang berjenis `kind` ("" bila tidak ada) — untuk placeholder `{<kind>_channel}`."""
        for cid, entry in sorted(self.channels.items()):
            if entry.get("kind") == kind:
                return str(cid)
        return ""

    def kinds(self) -> set[str]:
        return {str(e.get("kind")) for e in self.channels.values() if e.get("kind") and e.get("kind") != "personal"}

    def profiles(self) -> set[str]:
        return {str(e.get("profile")) for e in self.channels.values() if e.get("profile")}


def profile_name_of_home(home: Any, hermes_root: Any = None) -> str:
    """Nama profile dari HERMES_HOME: akar → "default", <akar>/profiles/<nama> → nama."""
    path = Path(str(home)).expanduser()
    if hermes_root is not None:
        try:
            if path.resolve() == Path(str(hermes_root)).expanduser().resolve():
                return "default"
        except OSError:
            pass
    if path.parent.name == "profiles":
        return path.name
    return "default"


class RosterCache:
    """Memuat ulang roster.json saat mtime/ukuran berubah; mempertahankan versi terakhir yang sah."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: Optional[tuple[int, int]] = None
        self._roster = Roster()
        self.error: str = ""

    def stamp(self) -> Optional[tuple[int, int]]:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def changed(self) -> bool:
        return self.stamp() != self._stamp

    def get(self, force: bool = False) -> Roster:
        stamp = self.stamp()
        with self._lock:
            if not force and stamp == self._stamp:
                return self._roster
            if stamp is None:
                self._stamp, self._roster, self.error = None, Roster(), "roster.json belum ada"
                return self._roster
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("isi roster bukan objek JSON")
            except (OSError, ValueError, RecursionError) as exc:
                # File sedang ditulis ulang / rusak: pakai versi terakhir, coba lagi di pemanggilan berikut.
                self.error = f"roster.json tidak terbaca: {exc}"
                return self._roster
            try:
                fresh = Roster(data)
            except Exception as exc:  # Roster() dirancang total; ini jaring terakhir, bukan jalur normal
                self._stamp, self.error = stamp, f"roster.json tidak bisa dipakai: {type(exc).__name__}: {exc}"
                log_once("roster-unusable", "%s; roster terakhir yang sah tetap dipakai", self.error)
                return self._roster
            for problem in fresh.problems:
                log_once(f"roster-field-{problem}", "roster.json: field %s — dipakai nilai aman; perbaiki dengan "
                         "`hermes_team.py apply`", problem)
            if fresh.critical and self._roster:
                # Field penentu gerbang rusak dan ada roster sah sebelumnya: yang lama TETAP dipakai (roster
                # baru akan mengosongkan daftar channel/anggota/setelan → gerbang mati diam-diam). Cap berkas
                # dicatat supaya tidak diurai ulang di tiap pesan; berkas yang diperbaiki punya cap baru.
                self._stamp = stamp
                self.error = ("roster.json baru tidak dipakai (field rusak: " + ", ".join(fresh.critical)
                              + "); roster terakhir yang sah tetap dipakai")
                log_once("roster-kept-" + ",".join(fresh.critical), "%s", self.error)
                return self._roster
            self._stamp, self._roster = stamp, fresh
            self.error = ("roster.json memuat field salah bentuk: " + "; ".join(fresh.problems[:8])
                          if fresh.problems else "")
            if self._roster.newer_schema:
                log_once(f"roster-schema-{self._roster.schema}",
                         "roster.json memakai skema %d, plugin ini memahami sampai %d — dibaca sebisanya; perbarui "
                         "plugin team-guard bersama kit (`hermes_team.py apply`)", self._roster.schema,
                         MAX_SUPPORTED_ROSTER_SCHEMA)
            return self._roster
