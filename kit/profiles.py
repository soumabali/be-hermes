"""Jenis profile (PROFILE_KINDS): satu tempat yang mendefinisikan member / template / shared / admin / lobby.

Sebelumnya tiap jenis tersebar di ~20 percabangan (`if role in ("member","template") / elif "shared" / ...`,
`for key in ("shared","admin")`, RESERVED_NAMES, target set-model, ...). Kini setiap percabangan bertanya ke
registri ini: setelan dasar per jenis, teks SOUL, apakah butuh channel, toolset tambahan yang dimatikan,
dan apakah namanya dicadangkan (tidak boleh jadi nama anggota).

Tambah jenis baru yang punya channel sendiri (`needs_channel`) / jadi target set-model (`model_target`):
daftarkan satu `ProfileKind` di `PROFILE_KINDS` — argumen `soul` (teks SOUL.md) WAJIB karena tidak bisa diturunkan.
Selebihnya DITURUNKAN dari registri ini: blok `<kind>.{profile,channel_id,auto_skills}` dan baris
`llm.<kind>_model` di skema (kit_config), rute gateway, alias model + `models:` di members.yaml, channel/kind di
roster, check/describe/status, cek channel doctor, urutan provisioning, dan nama yang dicadangkan.
`teamctl` (target set-model, channel tim, penjaga admin), berbagi skill, dan plugin team-guard (lewat kunci roster
`role` / `cron_exempt`) juga mengikutinya. Yang TIDAK generik (lihat docs/PENGEMBANGAN.md): channel rumah
(`DISCORD_HOME_CHANNEL` / `no_thread_channels`, memakai channel admin).
`SYSTEM_KINDS`, `MODEL_TARGETS`, `RESERVED_NAMES` adalah TAMPILAN malas atas `PROFILE_KINDS` (selalu mutakhir).

Jenis ber-channel BARU sebaiknya `optional=True`: blok `<kind>` di team.yaml boleh tidak ada, dan tim yang belum
mengisinya tetap valid — jenis itu tidak diprovisi untuk tim tersebut (tanpa rute, tanpa entri roster, bukan
target set-model). Tanpa `optional`, `<kind>.channel_id` WAJIB dan setiap team.yaml yang sudah ada gagal dimuat
sampai dimigrasi. Karena itu kode yang berjalan untuk SATU tim memakai `active_system_kinds(team)` /
`active_model_targets(team)`, bukan `SYSTEM_KINDS` / `MODEL_TARGETS` (yang berisi semua jenis TERDAFTAR).
Hanya bergantung pada kit.constants dan kit.templates (tanpa siklus ke config/derive).
"""
from __future__ import annotations

import collections.abc
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from kit.constants import (CRON_TOOLSET, HOST_SIDE_TOOLSETS, KIT_SKILLS, KNOWN_TOOLSETS, LOBBY_DISABLED_TOOLSETS,
                           TEAMCTL, WORKER_ALLOWED_TOOLSETS, WORKER_DISABLED_TOOLSETS)
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
    role: str = "member"                       # role Discord yang boleh melihat channel-nya: "member" | "admin"
    admin_routes: bool = False                 # rute dikunci per user_id admin bila `strict_admin_routes` (admin)
    free_response: bool = False                # channel tanpa @mention (admin); False = wajib @mention (shared)
    model_label: str = ""                      # kata untuk deskripsi llm.<kind>_model ("" = nama jenis)
    # True = blok `<kind>` di team.yaml boleh tidak ada/kosong; jenis itu lalu TIDAK diprovisi untuk tim tsb.
    # Bawaan (shared, admin) tetap wajib. Jenis ber-channel baru: pakai True, kalau tidak semua team.yaml lama gagal.
    optional: bool = False
    # Toolset yang DIIZINKAN untuk jenis ini (daftar eksplisit; lihat kit.constants.WORKER_ALLOWED_TOOLSETS).
    # Toolset yang dikenal kit tetapi tidak di sini dimatikan; toolset bersyarat diatur worker_allowed_toolsets.
    allowed_toolsets: tuple = tuple(WORKER_ALLOWED_TOOLSETS)

    @property
    def alias_kind(self) -> str:
        return self.model_as or self.name


MEMBER = ProfileKind("member", SOUL_MEMBER, _member_settings, per_member=True, model_as="member")
TEMPLATE = ProfileKind("template", SOUL_MEMBER, _member_settings, reserved=True, model_as="member")
SHARED = ProfileKind("shared", SOUL_SHARED, _shared_settings, needs_channel=True, reserved=True, model_target=True,
                     note="bersama, @mention → thread", channel_label="channel bersama",
                     model_label="bersama")
ADMIN = ProfileKind("admin", SOUL_ADMIN, _admin_settings, needs_channel=True, reserved=True, model_target=True,
                    cron_exempt=True, note="admin, tanpa @mention", channel_label="channel admin",
                    role="admin", admin_routes=True, free_response=True, model_label="admin")
LOBBY = ProfileKind("lobby", SOUL_LOBBY, None, reserved=True, model_target=True,
                    extra_disabled_toolsets=tuple(LOBBY_DISABLED_TOOLSETS), model_label="default (lobby)",
                    allowed_toolsets=())

PROFILE_KINDS: dict[str, ProfileKind] = {k.name: k for k in (MEMBER, TEMPLATE, SHARED, ADMIN, LOBBY)}

class _KindView(collections.abc.Sequence):
    """Urutan jenis yang dihitung ULANG dari PROFILE_KINDS setiap diakses (bukan salinan saat impor)."""

    def __init__(self, pick: Callable[[ProfileKind], bool]):
        self._pick = pick

    def _names(self) -> tuple[str, ...]:
        return tuple(k.name for k in PROFILE_KINDS.values() if self._pick(k))

    def __getitem__(self, index):
        return self._names()[index]

    def __len__(self) -> int:
        return len(self._names())

    def __eq__(self, other) -> bool:
        return isinstance(other, (tuple, list, _KindView)) and self._names() == tuple(other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return repr(self._names())


class _ReservedView(collections.abc.Set):
    @staticmethod
    def _names() -> frozenset:
        return frozenset(GENERIC_RESERVED | {k.name for k in PROFILE_KINDS.values() if k.reserved or k.needs_channel})

    @classmethod
    def _from_iterable(cls, it):
        return frozenset(it)

    def __contains__(self, name) -> bool:
        return name in self._names()

    def __iter__(self):
        return iter(self._names())

    def __len__(self) -> int:
        return len(self._names())

    def __repr__(self) -> str:
        return repr(set(self._names()))


# Kind yang punya blok sendiri di team.yaml (profile + channel_id + auto_skills): shared, admin.
SYSTEM_KINDS: Sequence = _KindView(lambda k: k.needs_channel and not k.per_member)
# Target set-model / llm-key selain nama anggota: shared, admin, lobby.
MODEL_TARGETS: Sequence = _KindView(lambda k: k.model_target)
# Nama di luar jenis profile yang bentrok dengan target set-model / profile bawaan Hermes.
GENERIC_RESERVED = frozenset({"all", "semua", "all-members", "default", "main"})
RESERVED_NAMES = _ReservedView()


def kind_of(role: str) -> ProfileKind:
    try:
        return PROFILE_KINDS[role]
    except KeyError:
        raise ValueError(role) from None


def kind_configured(team: dict, kind: str) -> bool:
    """Jenis ber-channel `kind` dipakai tim ini? Jenis wajib selalu; jenis `optional` hanya bila bloknya diisi
    (ada `channel_id`)."""
    spec = PROFILE_KINDS.get(kind)
    if spec is None or not spec.needs_channel or spec.per_member:
        return False
    block = team.get(kind)
    return bool(isinstance(block, dict) and block.get("channel_id") and block.get("profile"))


def active_system_kinds(team: dict) -> tuple[str, ...]:
    """Jenis ber-channel yang DIPROVISI untuk tim ini: SYSTEM_KINDS tanpa jenis `optional` yang bloknya kosong.
    Pakai ini (bukan SYSTEM_KINDS) setiap kali membaca `team[kind]`."""
    return tuple(k for k in SYSTEM_KINDS if kind_configured(team, k))


def active_model_targets(team: dict) -> tuple[str, ...]:
    """Target set-model / `models:` di members.yaml yang berlaku untuk tim ini (lobby selalu; jenis ber-channel
    hanya bila dipakai tim)."""
    return tuple(k for k in MODEL_TARGETS if not PROFILE_KINDS[k].needs_channel or kind_configured(team, k))


def worker_allowed_toolsets(team: dict, role: str = "admin") -> list[str]:
    """Toolset yang BOLEH dipakai profile pekerja jenis `role` di tim ini: daftar eksplisit jenisnya, ditambah
    toolset bersyarat — browser/computer-use (jalan di HOST) hanya dengan `allow_browser: true`; `cronjob`
    untuk jenis `cron_exempt` (admin: laporan terjadwal) atau dengan `scale.allow_member_cron: true` (cron job
    berjalan tanpa pesan, di luar rate limit dan antrean)."""
    kind = PROFILE_KINDS.get(role)
    allowed = list(kind.allowed_toolsets if kind else WORKER_ALLOWED_TOOLSETS)
    if team.get("allow_browser"):
        allowed += HOST_SIDE_TOOLSETS
    if (kind and kind.cron_exempt) or team["scale"].get("allow_member_cron"):
        allowed.append(CRON_TOOLSET)
    return list(dict.fromkeys(t for t in allowed if t not in WORKER_DISABLED_TOOLSETS))


def worker_disabled_toolsets(team: dict, role: str = "admin") -> list[str]:
    """Toolset yang dimatikan untuk profile pekerja = semua toolset yang dikenal kit (KNOWN_TOOLSETS) yang TIDAK
    ada di `worker_allowed_toolsets`. Urutan: discord*, sisi-host, cronjob (urutan lama, supaya `apply` tidak
    menulis ulang config yang nilainya sama), lalu sisanya menurut KNOWN_TOOLSETS."""
    allowed = set(worker_allowed_toolsets(team, role))
    ordered = dict.fromkeys([*WORKER_DISABLED_TOOLSETS, *HOST_SIDE_TOOLSETS, CRON_TOOLSET, *KNOWN_TOOLSETS])
    return [t for t in ordered if t not in allowed]


def base_settings(team: dict, role: str, member: Optional[dict], hermes_bin: str, hermes_root: Path) -> list[Setting]:
    """Setelan dasar jenis `role` (tanpa model/scoped/plugin, yang sama untuk semua jenis)."""
    kind = kind_of(role)
    if kind.settings is None:
        raise ValueError(role)
    ctx: dict[str, Any] = {"disabled": worker_disabled_toolsets(team, role), "hermes_bin": hermes_bin,
                           "hermes_root": hermes_root}
    return kind.settings(team, member, ctx)
