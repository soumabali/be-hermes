"""Bagian dari kit tim (dipecah dari hermes_team.py). Lihat docs/KONTRAK.md dan README."""
from __future__ import annotations
import contextlib
import json
import os
from pathlib import Path
from typing import Callable, Iterable, Optional
from kit_base import TeamError
from kit.profiles import PROFILE_KINDS, SYSTEM_KINDS
from kit.templates import SOUL_LOBBY, SOUL_MEMBER, SOUL_TEAM_SKILLS_NOTE
from kit.util import atomic_write
from kit.config import _lock_depth, reload_team, team_lock
from kit.derive import active_members, host_settings, profile_settings, roster_data
from kit.hermes_cli import Hermes
from kit.skills import SHARED_CATEGORY, _copy_shared_skill, shared_skill_registry


# --------------------------------------------------------------------------- provisioning
def write_roster(team: dict, hermes: Hermes) -> Path:
    state = Path(team["paths"]["state_dir"])
    path = state / "roster.json"
    data = roster_data(team, hermes.root, hermes.binary)
    old = None
    with contextlib.suppress(OSError, ValueError):
        old = json.loads(path.read_text(encoding="utf-8"))
    if old and {k: v for k, v in old.items() if k != "generated"} == {k: v for k, v in data.items() if k != "generated"}:
        return path
    hermes.say(f"  + tulis {path}")
    if not hermes.dry_run:
        state.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(state, 0o700)
        atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=0o600)
    return path


def soul_member(team: dict, display_name: str) -> str:
    note = SOUL_TEAM_SKILLS_NOTE if (team["team_skills_dir"] and team["team_skills_in_personal"]) else \
        "- Skill di kategori `shared/` adalah kiriman rekan lewat admin; boleh dipakai dan disesuaikan.\n"
    return SOUL_MEMBER.format(display_name=display_name, team_name=team["team_name"], team_skills_note=note)


def ensure_template(team: dict, hermes: Hermes, *, force_soul: bool = False) -> bool:
    template = team["template"]
    created = hermes.create(template, "default")
    hermes.write_file(hermes.home(template) / "SOUL.md", soul_member(team, "{nama}"), force=force_soul or created,
                      backup=not created)
    hermes.config_set(template, profile_settings(team, "template", hermes_bin=hermes.binary, hermes_root=hermes.root))
    hermes.park(template)
    return created


def provision_member(team: dict, hermes: Hermes, member: dict, *, force_soul: bool = False,
                     sync_shared: bool = False) -> bool:
    """`sync_shared`: pasang juga skill ber-scope "all" yang BELUM dimiliki profile ini (anggota yang
    diaktifkan lagi melewatkan skill yang dibagikan selama ia nonaktif). Profile baru selalu menerimanya."""
    hermes.say(f" {member['profile']}  ({member['display_name']})")
    created = hermes.create(member["profile"], team["template"])
    hermes.write_file(hermes.home(member["profile"]) / "SOUL.md", soul_member(team, member["display_name"]),
                      force=force_soul or created, backup=not created)
    hermes.config_set(member["profile"], profile_settings(team, "member", member,
                                                          hermes_bin=hermes.binary, hermes_root=hermes.root))
    hermes.install_plugin(member["profile"])
    if created or sync_shared:  # anggota baru / aktif lagi menerima skill yang dibagikan ke "semua"
        for skill, entry in shared_skill_registry(team).get("skills", {}).items():
            if entry.get("scope") != "all" or entry.get("origin_profile") == member["profile"]:
                continue
            if (hermes.home(member["profile"]) / "skills" / SHARED_CATEGORY / skill).exists():
                continue   # sudah punya (mungkin sudah disesuaikan sendiri) → jangan ditimpa
            try:
                status = _copy_shared_skill(team, hermes, skill, entry, member["profile"])
            except (TeamError, OSError) as exc:
                status = f"gagal: {exc}"
            hermes.say(f"  + skill bersama {skill}: {status}")
    return created


def provision_role(team: dict, hermes: Hermes, key: str, *, force_soul: bool = False) -> None:
    profile = team[key]["profile"]
    hermes.say(f" {profile}")
    created = hermes.create(profile, team["template"])
    soul = PROFILE_KINDS[key].soul.format(team_name=team["team_name"])
    hermes.write_file(hermes.home(profile) / "SOUL.md", soul, force=force_soul or created, backup=not created)
    hermes.config_set(profile, profile_settings(team, key, hermes_bin=hermes.binary, hermes_root=hermes.root))
    hermes.install_plugin(profile)


def sync_parking(team: dict, hermes: Hermes) -> None:
    """Parkir resmi Hermes (`gateway.parked`) mengikuti members.yaml: anggota nonaktif diparkir — adapter dan
    cron job profile-nya berhenti, tetapi profile, memory, dan sesinya utuh — dan anggota aktif dilepas.
    Tanpa ini cron job milik anggota yang sudah keluar tetap berjalan memakai kuota LLM tim."""
    for member in team["members"]:
        if not hermes.home(member["profile"]).is_dir():
            continue
        if member.get("active", True):
            hermes.unpark(member["profile"])
        else:
            hermes.park(member["profile"])


def provision_host(team: dict, hermes: Hermes) -> int:
    if team["lobby_restrict"]:
        hermes.write_file(hermes.home("default") / "SOUL.md", SOUL_LOBBY, force=True)
    changed = hermes.config_set("default", host_settings(team))
    hermes.install_plugin("default")
    return changed


# Setelan yang hanya dibaca saat gateway start → perubahan butuh restart.
RESTART_KEYS = {
    "gateway.profile_routes", "gateway.multiplex_profiles", "group_sessions_per_user",
    "discord.allowed_channels", "discord.free_response_channels", "discord.free_response_auto_thread",
    "discord.no_thread_channels", "discord.require_mention", "discord.auto_thread", "DISCORD_ALLOWED_ROLES", "DISCORD_HOME_CHANNEL",
    "unauthorized_dm_behavior", "gateway.unauthorized_dm_decline_message", "max_concurrent_sessions",
    "plugins.enabled", "mcp_servers.teamctl",
}


# PROFILE_SCOPED_KEYS (batas sandbox, ambang kompresi) sengaja TIDAK di sini: ambang kompresi dibaca saat
# sesi dibuat (berlaku di sesi baru), batas sandbox saat container profile itu dibuat.
# Bagian yang diterapkan plugin team-guard ke gateway yang sedang jalan (dari roster.json → gateway).
LIVE_KEYS = {"gateway.profile_routes", "discord.allowed_channels", "discord.free_response_channels"}


def _changed_host_keys(hermes: Hermes) -> set:
    return {k for keys in hermes.changed_keys.values() for k in keys}


def restart_needed(hermes: Hermes, created: list[str], *, live_reload: bool = False) -> bool:
    """Profile baru dilayani multiplexer tanpa restart (rescan profiles/); rute & daftar channel
    diterapkan plugin bila live_reload aktif. Sisanya (role, plugin, MCP, gating) tetap butuh restart."""
    keys = _changed_host_keys(hermes)
    live = LIVE_KEYS if live_reload else set()
    if created and not live_reload:
        return True
    # Profile baru selalu memasang plugin (plugins.enabled di profile itu) — itu dimuat saat dipakai,
    # bukan saat start; yang butuh restart hanya plugins.enabled di profile default (host).
    host_keys = hermes.changed_keys.get("default", set())
    member_only = keys - host_keys
    hard = {k for k in host_keys if (k in RESTART_KEYS or k.startswith("platforms.discord.extra.")) and k not in live}
    hard |= {k for k in member_only if k == "mcp_servers.teamctl"}
    return bool(hard)


def cmd_apply(team: dict, hermes: Hermes, *, only: Optional[list[str]] = None, host_only: bool = False,
              force_soul: bool = False, sync_shared: Iterable[str] = (),
              on_member_error: Optional[Callable[[dict, Exception], None]] = None) -> dict:
    """Idempoten. Kembalikan ringkasan {created, restart_needed, live_changed, failures}.

    `on_member_error(member, exc)`: bila diisi, profile anggota yang gagal dibuat tidak menghentikan apply;
    pemanggil (onboarding massal) WAJIB membatalkan anggota itu di dalam callback (mengeluarkannya dari
    members.yaml — `team` ikut diperbarui), supaya rute dan roster yang ditulis sesudahnya tidak memuatnya.

    Dipanggil paling luar (CLI `apply`, teamctl): team.yaml + members.yaml dibaca ULANG setelah kunci
    tim dipegang, supaya tidak bekerja dari salinan sebelum proses lain selesai menulis. Dipanggil dari
    dalam operasi lain (add_member, onboard, set-active) `team` sudah segar dan dipakai apa adanya."""
    with team_lock(team):
        if _lock_depth() == 1:
            reload_team(team)
        return _cmd_apply(team, hermes, only=only, host_only=host_only, force_soul=force_soul,
                          sync_shared=set(sync_shared or ()), on_member_error=on_member_error)


def _check_only(team: dict, only: Optional[list[str]]) -> None:
    """Nama di --only harus anggota aktif; salah ketik tidak boleh jadi apply kosong tanpa pesan."""
    known = {m["name"]: m for m in team["members"]}
    unknown = [n for n in (only or []) if n not in known]
    if unknown:
        raise TeamError(f"--only: anggota tidak dikenal: {', '.join(unknown)} "
                        f"(yang ada: {', '.join(sorted(known)) or '-'})")
    inactive = [n for n in (only or []) if not known[n].get("active", True)]
    if inactive:
        raise TeamError(f"--only: anggota nonaktif: {', '.join(inactive)} (aktifkan dulu: set-active <nama> on)")


def _cmd_apply(team: dict, hermes: Hermes, *, only: Optional[list[str]], host_only: bool,
               force_soul: bool, sync_shared: Iterable[str] = (),
               on_member_error: Optional[Callable[[dict, Exception], None]] = None) -> dict:
    created: list[str] = []
    if not host_only:
        _check_only(team, only)
        hermes.say(f"\n[1/4] Template {team['template']}")
        if ensure_template(team, hermes, force_soul=force_soul):
            created.append(team["template"])
        hermes.say("\n[2/4] Profile anggota")
        names = set(only or [])
        for member in active_members(team):
            if only and member["name"] not in names:
                continue
            try:
                if provision_member(team, hermes, member, force_soul=force_soul,
                                    sync_shared=member["name"] in sync_shared):
                    created.append(member["profile"])
            except Exception as exc:  # noqa: BLE001 — tanpa callback perilakunya tetap: error diteruskan
                if on_member_error is None or hermes.dry_run:
                    raise
                on_member_error(member, exc)
        if not only:
            hermes.say("\n[3/4] Profile bersama & admin")
            for key in SYSTEM_KINDS:
                provision_role(team, hermes, key, force_soul=force_soul)
    hermes.say("\n[4/4] Host gateway (profile default)")
    provision_host(team, hermes)
    sync_parking(team, hermes)
    # Roster ditulis TERAKHIR: plugin team-guard menerapkan rute dari roster.json ke gateway yang sedang
    # jalan, jadi rute baru tidak boleh terbit sebelum profile tujuannya benar-benar ada.
    write_roster(team, hermes)
    created_real = [p for p in created if p != team["template"]]
    live_changed = sorted(_changed_host_keys(hermes) & LIVE_KEYS)
    return {"created": created,
            "restart_needed": restart_needed(hermes, created_real, live_reload=team.get("live_reload", False)),
            "live_changed": live_changed, "failures": list(hermes.failures)}

