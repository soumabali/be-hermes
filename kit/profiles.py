"""Jenis profile (PROFILE_KINDS): satu tempat yang mendefinisikan member / template / shared / admin / lobby.

Sebelumnya tiap jenis tersebar di ~20 percabangan (`if role in ("member","template") / elif "shared" / ...`,
`for key in ("shared","admin")`, RESERVED_NAMES, target set-model, ...). Kini setiap percabangan bertanya ke
registri ini: setelan dasar per jenis, teks SOUL, apakah butuh channel, toolset tambahan yang dimatikan,
dan apakah namanya dicadangkan (tidak boleh jadi nama anggota).

Tambah jenis baru = satu `ProfileKind` di `PROFILE_KINDS` (+ teks SOUL di kit/templates/).
Hanya bergantung pada kit.constants dan kit.templates (tanpa siklus ke config/derive).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from kit.constants import (CRON_TOOLSET, HOST_SIDE_TOOLSETS, KIT_SKILLS, LOBBY_DISABLED_TOOLSETS, TEAMCTL,
                           WORKER_DISABLED_TOOLSETS)
from kit.templates import SOUL_ADMIN, SOUL_LOBBY, SOUL_MEMBER, SOUL_SHARED

Setting = tuple[str, object]
# fungsi(team, member, ctx) → setelan DASAR jenis ini; ctx = {"disabled", "hermes_bin", "hermes_root"}.
SettingsFn = Callable[[dict, Optional[dict], dict], list[Setting]]


def teamctl_server(team: dict, hermes_bin: str, hermes_root: Path) -> dict:
    return {
        "command": sys.executable,
        "args": [str(TEAMCTL), "--team", team["paths"]["team_file"],
                 "--members", team["paths"]["members_file"]],
        "env": {"HERMES_BIN": hermes_bin, "HERMES_HOME": str(hermes_root),
                "HERMES_TEAM_DIR": team["paths"]["team_dir"]},
        "timeout": 900,
        "connect_timeout": 60,
        "elicitation": {"enabled": True, "timeout": 300},
    }


def _skills_dir(team: dict) -> list[str]:
    return [team["team_skills_dir"]] if team["team_skills_dir"] else []


def _member_settings(team: dict, member: Optional[dict], ctx: dict) -> list[Setting]:
    return [
        ("agent.disabled_toolsets", ctx["disabled"]),
        ("memory.memory_enabled", True),
        ("memory.user_profile_enabled", True),
        ("memory.write_approval", False),
        ("skills.write_approval", False),
        ("skills.external_dirs", _skills_dir(team) if team["team_skills_in_personal"] else []),
        ("terminal.docker_forward_env", (member or {}).get("forward_env") or []),
    ]


def _shared_settings(team: dict, member: Optional[dict], ctx: dict) -> list[Setting]:
    return [
        ("agent.disabled_toolsets", ctx["disabled"]),
        ("memory.memory_enabled", True),
        ("memory.user_profile_enabled", False),   # tidak ada "profil user" di otak bersama
        ("memory.write_approval", True),           # admin menyetujui via /memory pending
        ("skills.write_approval", True),           # admin menyetujui via /skills pending
        ("skills.external_dirs", _skills_dir(team)),
    ]


def _admin_settings(team: dict, member: Optional[dict], ctx: dict) -> list[Setting]:
    return [
        ("agent.disabled_toolsets", ctx["disabled"]),
        ("memory.memory_enabled", True),
        ("memory.user_profile_enabled", True),
        ("memory.write_approval", False),
        ("skills.write_approval", False),
        # Skill bawaan kit (mis. team-usage-report) dibaca langsung dari folder kit yang dimiliki root.
        ("skills.external_dirs", _skills_dir(team) + ([str(KIT_SKILLS)] if KIT_SKILLS.is_dir() else [])),
        ("mcp_servers.teamctl", teamctl_server(team, ctx["hermes_bin"], ctx["hermes_root"])),
    ]


@dataclass(frozen=True)
class ProfileKind:
    name: str
    soul: str                                  # teks SOUL.md (format: team_name / display_name / team_skills_note)
    settings: Optional[SettingsFn]             # None = bukan profile yang diprovisi lewat profile_settings (lobby)
    needs_channel: bool = False                # punya channel_id sendiri di team.yaml (shared, admin)
    per_member: bool = False                   # satu profile per anggota (member)
    reserved: bool = False                     # nama tidak boleh dipakai anggota baru
    model_as: str = ""                         # jenis yang menentukan alias model profile ini ("" = dirinya sendiri)
    model_target: bool = False                 # boleh jadi target `set-model` (selain nama anggota / all)
    cron_exempt: bool = False                  # toolset cronjob tetap menyala meski allow_member_cron mati
    extra_disabled_toolsets: tuple = ()        # toolset tambahan yang dimatikan (lobby: semua)
    note: str = ""                             # keterangan di tampilan `status`
    channel_label: str = ""                    # "channel bersama" — dipakai pesan bila channel dipakai jenis ini

    @property
    def alias_kind(self) -> str:
        return self.model_as or self.name


MEMBER = ProfileKind("member", SOUL_MEMBER, _member_settings, per_member=True, model_as="member")
TEMPLATE = ProfileKind("template", SOUL_MEMBER, _member_settings, reserved=True, model_as="member")
SHARED = ProfileKind("shared", SOUL_SHARED, _shared_settings, needs_channel=True, reserved=True, model_target=True,
                     note="bersama, @mention → thread", channel_label="channel bersama")
ADMIN = ProfileKind("admin", SOUL_ADMIN, _admin_settings, needs_channel=True, reserved=True, model_target=True,
                    cron_exempt=True, note="admin, tanpa @mention", channel_label="channel admin")
LOBBY = ProfileKind("lobby", SOUL_LOBBY, None, reserved=True, model_target=True,
                    extra_disabled_toolsets=tuple(LOBBY_DISABLED_TOOLSETS))

PROFILE_KINDS: dict[str, ProfileKind] = {k.name: k for k in (MEMBER, TEMPLATE, SHARED, ADMIN, LOBBY)}

# Kind yang punya blok sendiri di team.yaml (profile + channel_id + auto_skills): shared, admin.
SYSTEM_KINDS: tuple[str, ...] = tuple(k.name for k in PROFILE_KINDS.values() if k.needs_channel and not k.per_member)
# Target set-model / llm-key selain nama anggota: shared, admin, lobby.
MODEL_TARGETS: tuple[str, ...] = tuple(k.name for k in PROFILE_KINDS.values() if k.model_target)
# Nama di luar jenis profile yang bentrok dengan target set-model / profile bawaan Hermes.
GENERIC_RESERVED = frozenset({"all", "semua", "all-members", "default", "main"})
RESERVED_NAMES = set(GENERIC_RESERVED | {k.name for k in PROFILE_KINDS.values() if k.reserved})


def kind_of(role: str) -> ProfileKind:
    try:
        return PROFILE_KINDS[role]
    except KeyError:
        raise ValueError(role) from None


def worker_disabled_toolsets(team: dict, role: str = "admin") -> list[str]:
    """Toolset yang dimatikan untuk profile pekerja. Cron job berjalan tanpa pesan (di luar rate limit dan
    antrean), jadi toolset `cronjob` mati untuk anggota dan profile bersama kecuali
    `scale.allow_member_cron: true`; profile admin selalu memilikinya (laporan terjadwal)."""
    kind = PROFILE_KINDS.get(role)
    disabled = WORKER_DISABLED_TOOLSETS + ([] if team.get("allow_browser") else HOST_SIDE_TOOLSETS)
    if not (kind and kind.cron_exempt) and not team["scale"].get("allow_member_cron"):
        disabled = disabled + [CRON_TOOLSET]
    return disabled


def base_settings(team: dict, role: str, member: Optional[dict], hermes_bin: str, hermes_root: Path) -> list[Setting]:
    """Setelan dasar jenis `role` (tanpa model/scoped/plugin, yang sama untuk semua jenis)."""
    kind = kind_of(role)
    if kind.settings is None:
        raise ValueError(role)
    ctx: dict[str, Any] = {"disabled": worker_disabled_toolsets(team, role), "hermes_bin": hermes_bin,
                           "hermes_root": hermes_root}
    return kind.settings(team, member, ctx)
