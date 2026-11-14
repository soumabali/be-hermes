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


class Roster:
    """Tampilan baca-saja atas isi roster.json, dengan nilai bawaan untuk field yang hilang."""

    def __init__(self, data: Optional[dict] = None):
        self.data: dict = data if isinstance(data, dict) else {}
        d = self.data
        self.schema: int = roster_schema(d)
        self.team_name: str = str(d.get("team_name") or "tim")
        self.guild_id: str = str(d.get("guild_id") or "")
        self.admins: set[str] = {str(a) for a in d.get("admins") or []}
        roles = d.get("roles") or {}
        self.member_role: str = str(roles.get("member") or "")
        self.admin_role: str = str(roles.get("admin") or "")
        self.entry_role: str = str(roles.get("entry") or "")
        self.shared_channel: str = str(d.get("shared_channel") or "")
        self.admin_channel: str = str(d.get("admin_channel") or "")
        self.channels: dict[str, dict] = {str(k): dict(v) for k, v in (d.get("channels") or {}).items()}
        self.users: dict[str, dict] = {str(k): dict(v) for k, v in (d.get("users") or {}).items()}
        self.inactive_users: dict[str, dict] = {str(k): dict(v)
                                                for k, v in (d.get("inactive_users") or {}).items()}
        self.reserved_env: list[str] = [str(n) for n in d.get("reserved_env") or []]
        self.settings: dict[str, Any] = {**SETTINGS_DEFAULTS, **(d.get("settings") or {})}
        self.onboarding: dict[str, Any] = {**ONBOARDING_DEFAULTS, **(d.get("onboarding") or {})}
        gw = d.get("gateway") or {}
        self.profile_routes: list[dict] = [dict(r) for r in gw.get("profile_routes") or [] if isinstance(r, dict)]
        self.allowed_channels: list[str] = [str(c) for c in gw.get("allowed_channels") or []]
        self.free_response_channels: list[str] = [str(c) for c in gw.get("free_response_channels") or []]
        self.live_reload: bool = bool(d.get("live_reload", True))
        self.restart_fallback: str = str(d.get("restart_fallback") or "idle")
        usage = d.get("usage") if isinstance(d.get("usage"), dict) else {}
        self.usage: dict[str, str] = {"timezone": str(usage.get("timezone") or "").strip(),
                                      "ledger": str(usage.get("ledger") or "").strip()}
        self.kit: dict[str, str] = {str(k): str(v) for k, v in (d.get("kit") or {}).items()}
        self.hermes_root: Path = Path(str(d.get("hermes_root") or "~/.hermes")).expanduser()
        self.hermes_bin: str = str(d.get("hermes_bin") or "hermes")

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
        if "cron_exempt" in entry:
            return bool(entry["cron_exempt"])
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
            except (OSError, ValueError) as exc:
                # File sedang ditulis ulang / rusak: pakai versi terakhir, coba lagi di pemanggilan berikut.
                self.error = f"roster.json tidak terbaca: {exc}"
                return self._roster
            self._stamp, self._roster, self.error = stamp, Roster(data), ""
            if self._roster.newer_schema:
                log_once(f"roster-schema-{self._roster.schema}",
                         "roster.json memakai skema %d, plugin ini memahami sampai %d — dibaca sebisanya; perbarui "
                         "plugin team-guard bersama kit (`hermes_team.py apply`)", self._roster.schema,
                         MAX_SUPPORTED_ROSTER_SCHEMA)
            return self._roster
