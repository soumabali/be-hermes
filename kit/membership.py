"""Bagian dari kit tim (dipecah dari hermes_team.py). Lihat docs/KONTRAK.md dan README."""
from __future__ import annotations
import json
import re
import shlex
import shutil
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
import yaml
from kit_base import TeamError, _id
from kit_discord import PERM, discord_token, private_overwrites, DiscordAPI
from kit.constants import NAME_RE
from kit.profiles import MODEL_TARGETS, PROFILE_KINDS, RESERVED_NAMES, SYSTEM_KINDS
from kit.util import _profile, atomic_write, clean_display_name, now_iso
from kit.config import _sandbox_override, _validate_unique, load_team, members_lock, reload_team, save_members, team_lock
from kit.derive import _get_nested, compression_settings, model_settings, sandbox_limits, sandbox_settings, sandbox_worst_case_mb
from kit.hermes_cli import Hermes
from kit.skills import _save_registry, _unshare_skill, shared_skill_registry
from kit.provision import cmd_apply, write_roster


def _check_new_name(team: dict, name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise TeamError(f"nama '{name}' tidak valid (huruf kecil, angka, '-' atau '_', maks 41)")
    if name in RESERVED_NAMES:
        raise TeamError(f"nama '{name}' dipakai sistem; pilih nama lain")
    return name


# Nama publik untuk dua pembantu yang dipakai teamctl_mcp.py (nama bergaris bawah tetap ada demi kompatibilitas).
check_new_name = _check_new_name


def _store_member(team: dict, hermes: Hermes, member: dict, *, replace: bool = False) -> None:
    """Tulis satu anggota ke members.yaml (baru, atau mengganti entri dengan user ID yang sama)."""
    with members_lock(Path(team["paths"]["team_file"])):
        fresh = load_team(Path(team["paths"]["team_file"]), Path(team["paths"]["members_file"]))
        if replace:
            fresh["members"] = [member if m["user_id"] == member["user_id"] else m for m in fresh["members"]]
        else:
            if any(m["name"] == member["name"] or m["user_id"] == member["user_id"] for m in fresh["members"]):
                raise TeamError("anggota yang sama baru saja ditambahkan proses lain")
            fresh["members"].append(member)
        _validate_unique(fresh)
        if not hermes.dry_run:
            save_members(fresh)
    team.clear()
    team.update(fresh)


def _unstore_member(team: dict, hermes: Hermes, member: dict, previous: Optional[dict]) -> None:
    """Kebalikan `_store_member`: kembalikan entri lama (`previous`) atau buang anggota yang baru ditulis."""
    with members_lock(Path(team["paths"]["team_file"])):
        fresh = load_team(Path(team["paths"]["team_file"]), Path(team["paths"]["members_file"]))
        if previous is not None:
            fresh["members"] = [previous if m["user_id"] == member["user_id"] else m for m in fresh["members"]]
        else:
            fresh["members"] = [m for m in fresh["members"] if m["user_id"] != member["user_id"]]
        save_members(fresh)
    team.clear()
    team.update(fresh)


def _discord(hermes: Hermes, api: Optional[DiscordAPI]) -> DiscordAPI:
    return api or DiscordAPI(discord_token(hermes.root))


def _taken_names(team: dict, hermes: Hermes) -> set[str]:
    """Nama yang tidak boleh dipakai anggota BARU: anggota di members.yaml, plus setiap folder profile
    yang sudah ada (sisa anggota lama yang dihapus dari members.yaml masih menyimpan .env/memory/skill)."""
    prefix = team["prefix"]
    taken = {m["name"] for m in team["members"]}
    profiles = hermes.profile_dirs() | {team["template"], *(team[kind]["profile"] for kind in SYSTEM_KINDS)}
    taken |= {p[len(prefix):] for p in profiles if p.startswith(prefix) and len(p) > len(prefix)}
    return taken


taken_names = _taken_names


def _validate_new_member(team: dict, hermes: Hermes, *, name: str, user_id: str, model: str = "",
                         channel_id: str = "") -> str:
    """Semua pemeriksaan untuk anggota baru, SEBELUM ada yang dibuat di Discord/Hermes. Sama dengan yang
    dijalankan `_store_member` (keunikan nama/user/channel/profile) + profile sisa. Kembalikan nama profile."""
    if model and team["llm"] and model not in team["llm"]["models"]:
        raise TeamError(f"model '{model}' tidak ada di katalog: {', '.join(team['llm']['models'])}")
    for m in team["members"]:
        if m["name"] == name:
            raise TeamError(f"anggota '{name}' sudah ada" + ("" if m["active"] else " (nonaktif — aktifkan lagi)"))
        if m["user_id"] == user_id:
            raise TeamError(f"user {user_id} sudah terdaftar sebagai '{m['name']}'")
    profile = _profile(f"{team['prefix']}{name}", "name")
    if profile in (team["template"], *(team[kind]["profile"] for kind in SYSTEM_KINDS)):
        raise TeamError(f"nama '{name}' menghasilkan profile '{profile}' yang dipakai sistem; pilih nama lain")
    if hermes.dir_exists(profile):
        raise TeamError(
            f"profile '{profile}' sudah ada di {hermes.home(profile)} tetapi bukan milik anggota mana pun di "
            "members.yaml (sisa anggota lama?). Anggota baru tidak boleh mewarisi .env/memory/skill-nya: hapus "
            f"(`hermes profile delete {profile}`) atau ganti nama profile lama itu, atau pakai nama anggota lain.")
    probe = dict(team)
    probe["members"] = [*team["members"], {"name": name, "user_id": user_id, "profile": profile,
                                           "channel_id": channel_id or "0" * 18}]
    _validate_unique(probe)
    return profile


def _channel_conflict(team: dict, channel_id: str, user_id: str) -> str:
    """Kenapa channel ini tidak boleh jadi channel pribadi user tsb. ('' = boleh)."""
    for kind in SYSTEM_KINDS:
        if channel_id == team[kind]["channel_id"]:
            return f"itu {PROFILE_KINDS[kind].channel_label}"
    if channel_id == team["category_id"]:
        return "itu kategori, bukan channel"
    for m in team["members"]:
        if m["channel_id"] == channel_id and m["user_id"] != user_id:
            return f"itu channel pribadi anggota '{m['name']}'"
    return ""


def _assert_private_channel(team: dict, api: DiscordAPI, channel: dict, user_id: str, notes: list[str],
                            *, who: str) -> None:
    """Privasi channel pribadi DIPAKSAKAN, bukan diasumsikan: harus channel teks di server ini, bukan
    channel bersama/admin/milik anggota lain; set izin lengkap ditegaskan ulang (@everyone tidak melihat,
    pemilik + bot boleh) dan izin untuk user/role lain dihapus (dicatat di `notes`)."""
    cid = str(channel.get("id") or "")
    conflict = _channel_conflict(team, cid, user_id)
    if conflict:
        raise TeamError(f"channel {cid} tidak bisa dipakai sebagai channel pribadi {who}: {conflict}")
    if str(channel.get("guild_id") or "") != team["guild_id"]:
        raise TeamError(f"channel {cid} bukan milik server Discord tim (guild {team['guild_id']})")
    if channel.get("type") != 0:
        raise TeamError(f"channel {cid} bukan channel teks biasa (type {channel.get('type')}); "
                        "thread, voice, forum, dan kategori tidak bisa jadi channel pribadi")
    desired = {o["id"]: o for o in private_overwrites(team["guild_id"], user_id, api.bot_user_id())}
    current = {str(o.get("id")): o for o in (channel.get("permission_overwrites") or []) if isinstance(o, dict)}
    reason = f"Hermes: channel pribadi {who} (privat)"
    for target, want in desired.items():
        have = current.get(target)
        same = have is not None and str(have.get("allow", "0")) == want["allow"] \
            and str(have.get("deny", "0")) == want["deny"] and str(have.get("type", want["type"])) in (
                str(want["type"]), "role" if want["type"] == 0 else "member")
        if not same:
            api.set_overwrite(cid, target, want["type"], int(want["allow"]), int(want["deny"]), reason=reason)
    removed = []
    for target, have in current.items():
        if target in desired:
            continue
        api.delete_overwrite(cid, target, reason=reason)
        kind = "role" if str(have.get("type", 0)) in ("0", "role") else "user"
        removed.append(f"{kind} {target}")
    if removed:
        notes.append(f"izin lain di channel <#{cid}> dihapus supaya tetap privat: {', '.join(removed)}")


def _find_reusable_channel(team: dict, api: DiscordAPI, name: str, user_id: str) -> Optional[dict]:
    """Channel #<prefix><nama> di kategori tim yang izinnya memang untuk user ini (sisa percobaan
    sebelumnya yang gagal di tengah) → dipakai lagi, supaya percobaan ulang tidak membuat channel dobel."""
    expected = f"{team['channel_prefix']}{name}"
    allowed = {team["guild_id"], user_id, api.bot_user_id()}
    for channel in api.guild_channels(team["guild_id"]):
        if not isinstance(channel, dict) or channel.get("type") != 0 or str(channel.get("name")) != expected:
            continue
        if str(channel.get("parent_id") or "") != team["category_id"]:
            continue
        if _channel_conflict(team, str(channel.get("id")), user_id):
            continue
        overwrites = {str(o.get("id")): o for o in (channel.get("permission_overwrites") or [])}
        owner = overwrites.get(user_id)
        try:
            owner_sees = bool(owner) and bool(int(owner.get("allow", 0)) & PERM["VIEW_CHANNEL"])
        except (TypeError, ValueError):
            owner_sees = False
        if not owner_sees or set(overwrites) - allowed:
            continue
        channel.setdefault("guild_id", team["guild_id"])
        return channel
    return None


def _new_private_channel(team: dict, api: DiscordAPI, *, name: str, user_id: str, display: str,
                         notes: list[str], emit: Callable[..., None]) -> tuple[str, bool]:
    """Channel pribadi untuk `name`: pakai lagi yang cocok, kalau tidak ada buat baru. → (id, dibuat_sekarang)."""
    found = _find_reusable_channel(team, api, name, user_id)
    if found is not None:
        _assert_private_channel(team, api, found, user_id, notes, who=name)
        cid = str(found["id"])
        notes.append(f"channel <#{cid}> yang sudah ada dipakai lagi (tidak membuat yang baru)")
        emit("channel", channel_id=cid, created=False, reused=True)
        return cid, False
    cid = api.create_private_channel(
        team["guild_id"], f"{team['channel_prefix']}{name}", user_id, api.bot_user_id(),
        team["category_id"], f"Hermes pribadi {display}. Hanya {display} dan bot.")
    emit("channel", channel_id=cid, created=True)
    return cid, True


def _commit_member(team: dict, hermes: Hermes, api: Optional[DiscordAPI], member: dict, *,
                   previous: Optional[dict] = None, channel_created: bool = False,
                   sync_shared: bool = False) -> dict:
    """members.yaml → profile → gateway → roster untuk satu anggota. Kalau gagal di tengah, yang dibuat
    panggilan INI dibatalkan (entri members.yaml, folder profile baru, channel baru) lalu error diteruskan,
    supaya percobaan ulang mulai dari keadaan bersih dan tidak ada channel/profil yatim."""
    had_dir = hermes.dir_exists(member["profile"])
    stored = False
    try:
        _store_member(team, hermes, member, replace=previous is not None)
        stored = True
        return cmd_apply(team, hermes, only=[member["name"]],
                         sync_shared=[member["name"]] if sync_shared else ())
    except Exception as exc:
        if hermes.dry_run:
            raise
        undone = _rollback_member(team, hermes, api, member, previous=previous, stored=stored,
                                  remove_dir=not had_dir, channel_created=channel_created)
        if isinstance(exc, TeamError) and undone:
            raise TeamError(f"{exc} — dibatalkan: {'; '.join(undone)}") from exc
        raise


def _rollback_member(team: dict, hermes: Hermes, api: Optional[DiscordAPI], member: dict, *,
                     previous: Optional[dict], stored: bool, remove_dir: bool, channel_created: bool) -> list[str]:
    undone: list[str] = []
    if stored:
        try:
            _unstore_member(team, hermes, member, previous)
            undone.append("entri members.yaml dikembalikan")
        except (TeamError, OSError) as exc:
            undone.append(f"members.yaml TIDAK bisa dikembalikan ({exc}); periksa entri '{member['name']}'")
    home = hermes.home(member["profile"])
    if remove_dir and member["profile"] != "default" and home.parent == hermes.root / "profiles" \
            and (home.exists() or home.is_symlink()):
        try:
            if home.is_symlink():
                home.unlink()
            else:
                shutil.rmtree(home)
            undone.append(f"profile {member['profile']} yang setengah jadi dihapus")
        except OSError as exc:
            undone.append(f"profile {member['profile']} TIDAK bisa dihapus ({exc}); hapus manual")
    if channel_created:
        try:
            _discord(hermes, api).delete_channel(member["channel_id"],
                                                 reason=f"Hermes: onboarding {member['name']} gagal, dibatalkan")
            undone.append(f"channel {member['channel_id']} dihapus")
        except TeamError as exc:
            undone.append(f"channel {member['channel_id']} TIDAK bisa dihapus ({exc}); hapus manual")
    for line in undone:
        hermes.say(f"  ↩ {line}")
    return undone


def add_member(team: dict, hermes: Hermes, *, name: str, user_id: str, display_name: str = "",
               model: str = "", channel_id: str = "", create_channel: bool = True,
               api: Optional[DiscordAPI] = None) -> dict:
    """Tambah anggota: validasi SEMUA → channel (pakai lagi / buat) → members.yaml → profile → setelan
    gateway → roster → role @backend (terakhir, supaya onboarding otomatis melihat anggota yang sudah
    lengkap). Gagal setelah channel dibuat → channel & entri dibatalkan."""
    with team_lock(team):
        reload_team(team)
        name = _check_new_name(team, name)
        user_id = _id(user_id, "discord_user_id")
        explicit = bool(str(channel_id or "").strip())
        if explicit:
            channel_id = _id(channel_id, "channel_id")
            conflict = _channel_conflict(team, channel_id, user_id)
            if conflict:
                raise TeamError(f"channel {channel_id} tidak bisa dipakai: {conflict}")
        elif not create_channel:
            raise TeamError("isi channel_id atau izinkan pembuatan channel")
        display = clean_display_name(display_name, name.capitalize())
        profile = _validate_new_member(team, hermes, name=name, user_id=user_id, model=model,
                                       channel_id=channel_id if explicit else "")
        notes: list[str] = []
        channel_created = False
        if hermes.dry_run:
            if not explicit:
                channel_id = "9" * 18
                hermes.say(f"  (dry-run) buat channel #{team['channel_prefix']}{name}")
        else:
            api = _discord(hermes, api)
            try:
                api.guild_member(team["guild_id"], user_id)
            except TeamError as exc:
                raise TeamError(f"user {user_id} tidak ditemukan di server Discord ({exc})")
            if explicit:
                channel = api.get_channel(channel_id)
                if channel is None:
                    raise TeamError(f"channel {channel_id} tidak ditemukan di Discord")
                _assert_private_channel(team, api, channel, user_id, notes, who=name)
            else:
                channel_id, channel_created = _new_private_channel(
                    team, api, name=name, user_id=user_id, display=display, notes=notes,
                    emit=lambda *_a, **_k: None)
                if channel_created:
                    notes.append(f"channel <#{channel_id}> dibuat")
        member = {"name": name, "display_name": display, "user_id": user_id, "channel_id": channel_id,
                  "profile": profile, "model": model, "active": True, "forward_env": []}
        result = _commit_member(team, hermes, api, member, channel_created=channel_created)
        if not explicit and not hermes.dry_run:
            try:
                _discord(hermes, api).add_role(team["guild_id"], user_id, team["member_role"])
                notes.append("role anggota diberikan")
            except TeamError as exc:
                notes.append(f"role anggota belum diberikan ({exc}); berikan manual")
        return {"member": member, "notes": notes, "status": "created", **result}


def derive_member_name(username: str, user_id: str, taken: Iterable[str]) -> str:
    """Nama anggota dari username Discord: huruf kecil ASCII, '.'/spasi → '-', unik, bukan nama sistem.
    `taken` = nama yang sudah terpakai; untuk anggota baru pakai `_taken_names` (ikut menghitung folder
    profile sisa), bukan hanya nama di members.yaml."""
    text = unicodedata.normalize("NFKD", str(username or "")).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9_-]+", "-", text)
    base = re.sub(r"[-_]{2,}", "-", base).strip("-_")[:30]
    if not base or not base[0].isalnum():
        base = f"user{str(user_id)[-4:]}"
    taken = set(taken) | RESERVED_NAMES
    name, i = base, 2
    while name in taken:
        name = f"{base[:26]}-{i}"
        i += 1
    return name


def _ensure_member_channel_ex(team: dict, hermes: Hermes, member: dict, api: Optional[DiscordAPI],
                              emit: Callable[..., None], notes: list[str]) -> tuple[str, bool]:
    """Pastikan channel pribadi anggota ada, privat, dan pemiliknya (lagi) punya akses.
    → (channel ID, True bila channel BARU dibuat di panggilan ini)."""
    if hermes.dry_run:
        return member["channel_id"], False
    api = _discord(hermes, api)
    channel = api.get_channel(member["channel_id"])
    if channel is None:
        cid, created = _new_private_channel(team, api, name=member["name"], user_id=member["user_id"],
                                            display=member["display_name"], notes=notes, emit=emit)
        if created:
            notes.append(f"channel lama sudah tidak ada; channel baru <#{cid}> dibuat")
        return cid, created
    _assert_private_channel(team, api, channel, member["user_id"], notes, who=member["name"])
    emit("channel", channel_id=member["channel_id"], created=False)
    return member["channel_id"], False


def _ensure_member_channel(team: dict, hermes: Hermes, member: dict, api: Optional[DiscordAPI],
                           emit: Callable[..., None], notes: list[str]) -> str:
    """Seperti `_ensure_member_channel_ex`, hanya mengembalikan channel ID. Pemanggil WAJIB menyimpan
    hasilnya: ID bisa berbeda dari `member['channel_id']` bila channel lama sudah dihapus."""
    return _ensure_member_channel_ex(team, hermes, member, api, emit, notes)[0]


def _member_state_stale(team: dict, hermes: Hermes, member: dict) -> bool:
    """Rute gateway / roster belum mencerminkan anggota aktif ini (apply sebelumnya gagal di tengah)."""
    routes = _get_nested(hermes.read_config("default"), "gateway.profile_routes")
    routed = any(isinstance(r, dict) and str(r.get("chat_id")) == member["channel_id"]
                 and r.get("profile") == member["profile"] for r in (routes if isinstance(routes, list) else []))
    if not routed:
        return True
    try:
        roster = json.loads((Path(team["paths"]["state_dir"]) / "roster.json").read_text(encoding="utf-8"))
        user = (roster.get("users") or {}).get(member["user_id"]) or {}
    except (OSError, ValueError, AttributeError):
        return True
    return user.get("channel_id") != member["channel_id"] or user.get("profile") != member["profile"]


def _onboard_existing(team: dict, hermes: Hermes, existing: dict, api: Optional[DiscordAPI],
                      emit: Callable[..., None], notes: list[str]) -> dict:
    """Anggota sudah aktif: pastikan channel-nya (ID baru DISIMPAN bila channel lama hilang) dan sembuhkan
    sendiri profile yang hilang / rute yang basi. Kegagalan dilaporkan di notes + failures, tidak ditelan."""
    member = dict(existing)
    failures: list[str] = []
    cid, created = existing["channel_id"], False

    def changed_only(event: str, **fields: Any) -> None:
        if fields.get("channel_id") != existing["channel_id"]:
            emit(event, **fields)

    try:
        cid, created = _ensure_member_channel_ex(team, hermes, member, api, changed_only, notes)
    except TeamError as exc:
        notes.append(f"channel pribadi belum bisa dipastikan: {exc}")
        failures.append(f"channel {existing['channel_id']}: {exc}")
    result: dict = {"created": [], "restart_needed": False, "live_changed": [], "failures": []}
    if cid != existing["channel_id"]:
        member["channel_id"] = cid
        result = _commit_member(team, hermes, api, member, previous=existing, channel_created=created)
    elif not hermes.exists(member["profile"]) or _member_state_stale(team, hermes, member):
        notes.append("profile/rute anggota belum lengkap → diterapkan ulang")
        result = cmd_apply(team, hermes, only=[member["name"]])
    return {"status": "exists", "member": member, "notes": notes, **result,
            "failures": failures + list(result.get("failures", []))}


def _plan_onboard(team: dict, hermes: Hermes, *, user_id: str, username: str = "", display_name: str = "",
                  name: str = "", model: str = "") -> dict:
    """Bagian onboarding TANPA efek samping: tentukan jenisnya dan jalankan semua validasi.
    → {"kind": "exists" | "reactivated" | "created", "member": {...}, "previous": entri lama | None}.
    `member["channel_id"]` anggota baru masih kosong sampai `_onboard_channel` dijalankan."""
    if model and team["llm"] and model not in team["llm"]["models"]:
        raise TeamError(f"model '{model}' tidak ada di katalog: {', '.join(team['llm']['models'])}")
    existing = next((m for m in team["members"] if m["user_id"] == user_id), None)
    if existing and existing.get("active", True):
        return {"kind": "exists", "member": dict(existing), "previous": existing}
    if existing:
        member = dict(existing)
        if model:
            member["model"] = model
        return {"kind": "reactivated", "member": member, "previous": existing}
    if name:
        name = _check_new_name(team, name)
    else:
        # Folder profile yang sudah ada dihitung terpakai: anggota baru tidak pernah menumpang profile lama.
        name = derive_member_name(username or display_name, user_id, _taken_names(team, hermes))
    display = clean_display_name(display_name or username or name, name)
    profile = _validate_new_member(team, hermes, name=name, user_id=user_id, model=model)
    member = {"name": name, "display_name": display, "user_id": user_id, "channel_id": "",
              "profile": profile, "model": model, "active": True, "forward_env": []}
    return {"kind": "created", "member": member, "previous": None}


def _onboard_channel(team: dict, hermes: Hermes, plan: dict, api: Optional[DiscordAPI],
                     emit: Callable[..., None], notes: list[str]) -> bool:
    """Bagian Discord dari onboarding: pastikan/buat channel pribadi dan isi `plan["member"]["channel_id"]`.
    → True bila channel BARU dibuat di panggilan ini (dipakai untuk pembatalan)."""
    member = plan["member"]
    if plan["kind"] == "reactivated":
        cid, created = _ensure_member_channel_ex(team, hermes, member, api, emit, notes)
        member["channel_id"] = cid
        member["active"] = True
        return created
    name, user_id = member["name"], member["user_id"]
    if hermes.dry_run:
        member["channel_id"] = "9" * 18
        hermes.say(f"  (dry-run) buat channel #{team['channel_prefix']}{name}")
        emit("channel", channel_id=member["channel_id"], created=True)
        return False
    api = _discord(hermes, api)
    try:
        api.guild_member(team["guild_id"], user_id)
    except TeamError as exc:
        raise TeamError(f"user {user_id} tidak ditemukan di server Discord ({exc})")
    channel_id, channel_created = _new_private_channel(
        team, api, name=name, user_id=user_id, display=member["display_name"], notes=notes, emit=emit)
    if channel_created:
        notes.append(f"channel <#{channel_id}> dibuat")
    member["channel_id"] = channel_id
    return channel_created


def onboard_member(team: dict, hermes: Hermes, *, user_id: str, username: str = "", display_name: str = "",
                   name: str = "", model: str = "", api: Optional[DiscordAPI] = None,
                   emit: Optional[Callable[..., None]] = None) -> dict:
    """Jadikan user Discord anggota aktif. Idempoten:
    - sudah aktif      → status "exists" (channel dipastikan; channel pengganti disimpan; profile/rute yang
                         belum lengkap diterapkan ulang)
    - pernah nonaktif  → status "reactivated" (channel lama dipakai lagi / dibuat ulang bila sudah dihapus)
    - baru             → status "created" (channel privat + profile + rute)
    Semua validasi dijalankan sebelum channel dibuat; gagal di tengah → yang dibuat panggilan ini dibatalkan.
    Profile, memory, skill, .env, dan sandbox Docker tiap anggota selalu milik profile be-<nama> sendiri."""
    emit = emit or (lambda *_a, **_k: None)
    user_id = _id(user_id, "discord_user_id")
    with team_lock(team):
        reload_team(team)
        notes: list[str] = []
        plan = _plan_onboard(team, hermes, user_id=user_id, username=username, display_name=display_name,
                             name=name, model=model)
        if plan["kind"] == "exists":
            return _onboard_existing(team, hermes, plan["previous"], api, emit, notes)
        if not hermes.dry_run:
            api = _discord(hermes, api)
        channel_created = _onboard_channel(team, hermes, plan, api, emit, notes)
        result = _commit_member(team, hermes, api, plan["member"], previous=plan["previous"],
                                channel_created=channel_created, sync_shared=plan["kind"] == "reactivated")
        return {"status": plan["kind"], "member": plan["member"], "notes": notes, **result}


BATCH_ENTRY_KEYS = ("user_id", "discord_user_id", "username", "display_name", "name", "model")


def load_batch_file(path: Path) -> list:
    """Berkas onboarding massal: daftar YAML/JSON berisi {user_id, username?, display_name?, name?, model?}
    (boleh juga mapping dengan key `members`). Isinya divalidasi di `onboard_batch`."""
    try:
        raw = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TeamError(f"{path} tidak ditemukan")
    except (OSError, UnicodeDecodeError) as exc:
        raise TeamError(f"{path} tidak bisa dibaca: {exc}")
    except yaml.YAMLError as exc:
        raise TeamError(f"{path}: YAML/JSON tidak valid: {exc}")
    if isinstance(raw, dict) and isinstance(raw.get("members"), list):
        raw = raw["members"]
    if not isinstance(raw, list) or not raw:
        raise TeamError(f"{path}: isinya harus daftar anggota yang tidak kosong")
    return raw


def _batch_validate(team: dict, hermes: Hermes, entries: list) -> list[dict]:
    """Langkah 1 onboard_batch: validasi SEMUA entri tanpa efek samping. `probe` ikut memuat anggota baru yang
    sudah direncanakan supaya nama turunan dan keunikan dicek terhadap sesama isi berkas."""
    probe = dict(team)
    probe["members"] = list(team["members"])
    items: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        where = f"entri[{i}]"
        try:
            if not isinstance(entry, dict):
                raise TeamError("harus mapping {user_id, username?, display_name?, name?, model?}")
            unknown = sorted(str(k) for k in entry if k not in BATCH_ENTRY_KEYS)
            if unknown:
                raise TeamError(f"key tidak dikenal: {', '.join(unknown)}")
            user_id = _id(entry.get("user_id") or entry.get("discord_user_id"), "user_id")
            if user_id in seen:
                raise TeamError(f"user {user_id} muncul dua kali di berkas")
            plan = _plan_onboard(probe, hermes, user_id=user_id,
                                 username=str(entry.get("username") or ""),
                                 display_name=str(entry.get("display_name") or ""),
                                 name=str(entry.get("name") or ""), model=str(entry.get("model") or ""))
        except TeamError as exc:
            errors.append(f"{where}: {exc}")
            continue
        seen.add(user_id)
        if plan["kind"] == "created":
            probe["members"].append(dict(plan["member"], channel_id=f"batch-{i}"))
        items.append({"index": i, "user_id": user_id, "plan": plan, "notes": [], "status": plan["kind"],
                      "channel_created": False, "error": "", "result": {}})
    if errors:
        raise TeamError("berkas onboarding massal tidak valid, tidak ada yang diubah:\n  - "
                        + "\n  - ".join(errors))
    return items


def _batch_fail(item: dict, exc: BaseException, undone: Iterable[str] = ()) -> None:
    item["status"] = "failed"
    item["error"] = str(exc) + (f" — dibatalkan: {'; '.join(undone)}" if undone else "")


def _batch_emitter(emit: Callable[..., None], item: dict) -> Callable[..., None]:
    return lambda event, **fields: emit(event, user_id=item["user_id"], **fields)


def _batch_channels(team: dict, hermes: Hermes, items: list[dict], api: Optional[DiscordAPI],
                    emit: Callable[..., None]) -> None:
    """Langkah 2: channel untuk semua (anggota yang sudah aktif hanya dipastikan/disembuhkan seperti biasa)."""
    for item in items:
        plan = item["plan"]
        try:
            if plan["kind"] == "exists":
                item["result"] = _onboard_existing(team, hermes, plan["previous"], api, _batch_emitter(emit, item),
                                                   item["notes"])
                plan["member"] = item["result"]["member"]
                continue
            item["channel_created"] = _onboard_channel(team, hermes, plan, api, _batch_emitter(emit, item),
                                                       item["notes"])
            if hermes.dry_run and plan["kind"] == "created":
                plan["member"]["channel_id"] = str(10 ** 18 - 1 - item["index"])  # unik per entri
        except TeamError as exc:
            _batch_fail(item, exc)


def _batch_store(team: dict, hermes: Hermes, items: list[dict], api: Optional[DiscordAPI]) -> dict[str, dict]:
    """Langkah 3: members.yaml untuk semua yang channel-nya siap → {nama anggota: item} yang menunggu apply."""
    pending: dict[str, dict] = {}
    for item in items:
        plan = item["plan"]
        if item["status"] == "failed" or plan["kind"] == "exists":
            continue
        member = plan["member"]
        item["had_dir"] = hermes.dir_exists(member["profile"])
        try:
            if hermes.dry_run:   # `_store_member` tidak menulis saat dry-run → kumpulkan di memori saja
                team["members"] = [m for m in team["members"] if m["user_id"] != member["user_id"]] + [member]
                _validate_unique(team)
            else:
                _store_member(team, hermes, member, replace=plan["previous"] is not None)
        except TeamError as exc:
            _batch_fail(item, exc, _rollback_member(team, hermes, api, member, previous=plan["previous"],
                                                    stored=False, remove_dir=False,
                                                    channel_created=item["channel_created"]))
            continue
        pending[member["name"]] = item
    return pending


def _batch_apply(team: dict, hermes: Hermes, items: list[dict], pending: dict[str, dict],
                 api: Optional[DiscordAPI]) -> dict:
    """Langkah 4: SATU apply untuk semuanya. Profile yang gagal dibuat dibatalkan saat itu juga (pembatal yang
    sama dengan onboarding tunggal: entri members.yaml, folder setengah jadi, channel baru), sehingga rute dan
    roster yang ditulis di akhir apply hanya memuat anggota yang berhasil."""
    summary: dict = {"created": [], "restart_needed": False, "live_changed": []}

    def merge(result: dict) -> None:
        summary["created"] = list(dict.fromkeys(summary["created"] + list(result.get("created", []))))
        summary["restart_needed"] = summary["restart_needed"] or bool(result.get("restart_needed"))
        summary["live_changed"] = sorted(set(summary["live_changed"]) | set(result.get("live_changed", [])))

    def undo(item: dict, exc: BaseException) -> None:
        plan = item["plan"]
        _batch_fail(item, exc, _rollback_member(
            team, hermes, api, plan["member"], previous=plan["previous"], stored=True,
            remove_dir=not item["had_dir"], channel_created=item["channel_created"]))

    for item in items:
        if item["plan"]["kind"] == "exists" and item["status"] != "failed":
            merge(item["result"])
    if pending:
        try:
            merge(cmd_apply(team, hermes, only=list(pending),
                            sync_shared=[n for n, it in pending.items() if it["plan"]["kind"] == "reactivated"],
                            on_member_error=lambda member, exc: undo(pending[member["name"]], exc)))
        except Exception as exc:  # noqa: BLE001 — gagal di luar profile anggota (template, host gateway)
            if hermes.dry_run:
                raise
            for item in pending.values():
                if item["status"] != "failed":
                    undo(item, exc)
            # Samakan lagi host gateway + roster dengan members.yaml yang sekarang (tanpa anggota batal).
            try:
                merge(cmd_apply(team, hermes, only=[], host_only=True))
            except Exception as sync_exc:  # noqa: BLE001
                hermes.failures.append(f"sinkronisasi host setelah pembatalan gagal: {sync_exc}")
    return summary


def _batch_report(hermes: Hermes, items: list[dict]) -> list[dict]:
    members = []
    for item in items:
        member = item["plan"]["member"]
        marker = f" {shlex.join(Hermes.profile_args(member['profile']))} "
        own = [f for f in hermes.failures if marker in f]
        if item["status"] == "failed":
            own = [item["error"]] + own
        elif item["plan"]["kind"] == "exists":
            own = list(dict.fromkeys(list(item["result"].get("failures", [])) + own))
        members.append({"status": item["status"], "user_id": item["user_id"], "member": member,
                        "notes": item["notes"], "failures": own})
    return members


def onboard_batch(team: dict, hermes: Hermes, entries: Iterable[Any], *, api: Optional[DiscordAPI] = None,
                  emit: Optional[Callable[..., None]] = None) -> dict:
    """Onboarding banyak orang sekaligus (hari pertama): validasi SEMUA entri → semua channel → semua
    entri members.yaml → SATU `apply` (satu kali tulis roster = satu live reload).

    Validasi gagal di entri mana pun → TeamError berisi semua kesalahan, tidak ada yang diubah. Setelah
    itu kegagalan per orang (Discord, profile) tidak menghentikan yang lain: yang dibuat untuk orang itu
    dibatalkan dengan pembatal yang sama dengan onboarding tunggal, lalu dilaporkan di `failed`.
    → {"members": [hasil per orang], "failed": [...], "restart_needed", "live_changed", "failures"}."""
    emit = emit or (lambda *_a, **_k: None)
    entries = list(entries)
    with team_lock(team):
        reload_team(team)
        items = _batch_validate(team, hermes, entries)
        if not hermes.dry_run and any(it["plan"]["kind"] != "exists" for it in items):
            api = _discord(hermes, api)
        _batch_channels(team, hermes, items, api, emit)
        pending = _batch_store(team, hermes, items, api)
        summary = _batch_apply(team, hermes, items, pending, api)
        members = _batch_report(hermes, items)
        failed = [m for m in members if m["status"] == "failed"]
        return {"members": members, "failed": failed, **summary,
                "failures": [f"{m['member']['name']} ({m['user_id']}): {m['failures'][0]}" for m in failed]
                + list(hermes.failures)}


def offboard_member(team: dict, hermes: Hermes, *, user_id: str, hide_channel: bool = True,
                    api: Optional[DiscordAPI] = None) -> dict:
    """Nonaktifkan anggota berdasarkan user Discord. Profile, memory, .env, dan channel TIDAK dihapus;
    rute dicabut dan (opsional) izin melihat channel pribadinya dilepas."""
    user_id = _id(user_id, "discord_user_id")
    with team_lock(team):
        reload_team(team)
        member = next((m for m in team["members"] if m["user_id"] == user_id), None)
        if member is None:
            return {"status": "not-member", "member": None, "notes": [], "restart_needed": False,
                    "live_changed": [], "failures": []}
        notes: list[str] = []
        if member.get("active", True):
            member = dict(member, active=False)
            _store_member(team, hermes, member, replace=True)
            result = cmd_apply(team, hermes, only=[], host_only=True)
            status = "deactivated"
        else:
            result = {"restart_needed": False, "live_changed": [], "failures": list(hermes.failures)}
            status = "already-inactive"
        if hide_channel and not hermes.dry_run:
            try:
                _discord(hermes, api).delete_overwrite(member["channel_id"], user_id,
                                                       reason=f"Hermes: {member['name']} dinonaktifkan")
                notes.append("akses ke channel pribadinya dicabut (channel & isinya disimpan)")
            except TeamError as exc:
                notes.append(f"izin channel belum dicabut ({exc})")
        return {"status": status, "member": member, "notes": notes, **result}


def set_model(team: dict, hermes: Hermes, target: str, alias: str) -> dict:
    with team_lock(team):
        return _set_model(team, hermes, target, alias)


def _set_model(team: dict, hermes: Hermes, target: str, alias: str) -> dict:
    if not team["llm"]:
        raise TeamError("team.yaml belum punya bagian llm (katalog model)")
    if alias not in team["llm"]["models"]:
        raise TeamError(f"model '{alias}' tidak ada di katalog: {', '.join(team['llm']['models'])}")
    target = (target or "").strip().lower()
    with members_lock(Path(team["paths"]["team_file"])):
        fresh = load_team(Path(team["paths"]["team_file"]), Path(team["paths"]["members_file"]))
        touched: list[tuple[str, str, Optional[dict]]] = []
        if target in MODEL_TARGETS:
            fresh["model_overrides"][target] = alias
            profile = fresh[target]["profile"] if PROFILE_KINDS[target].needs_channel else "default"   # lobby = host
            touched.append((target, profile, None))
        elif target in ("all", "semua", "all-members"):
            for m in fresh["members"]:
                m["model"] = alias
                touched.append(("member", m["profile"], m))
        else:
            member = next((m for m in fresh["members"] if m["name"] == target), None)
            if member is None:
                raise TeamError(f"anggota '{target}' tidak ada")
            member["model"] = alias
            touched.append(("member", member["profile"], member))
        if not hermes.dry_run:
            save_members(fresh)
    team.clear()
    team.update(fresh)
    for role, profile, member in touched:
        if member is not None and not member.get("active", True):
            continue
        # Ambang kompresi mengikuti model (llm.models.<alias>), jadi ikut ditulis ulang saat model berganti.
        settings = model_settings(team, alias) + compression_settings(team, alias)
        hermes.config_set(profile, settings)
    write_roster(team, hermes)
    return {"target": target, "model": alias, "profiles": [p for _, p, _ in touched], "failures": list(hermes.failures)}


def set_sandbox(team: dict, hermes: Hermes, name: str, *, memory_mb: Any = None, cpu: Any = None,
                reset: bool = False) -> dict:
    """Batas sandbox khusus satu anggota: tulis members.yaml (di bawah kunci tim) lalu terapkan ke profile-nya.
    `reset` menghapus pengecualian (kembali ke team.yaml → sandbox); bisa digabung dengan nilai baru."""
    with team_lock(team):
        return _set_sandbox(team, hermes, name, memory_mb=memory_mb, cpu=cpu, reset=reset)


def _set_sandbox(team: dict, hermes: Hermes, name: str, *, memory_mb: Any, cpu: Any, reset: bool) -> dict:
    if memory_mb is None and cpu is None and not reset:
        raise TeamError("set-sandbox: isi --memory-mb dan/atau --cpu, atau --reset untuk kembali ke bawaan tim")
    name = (name or "").strip().lower()
    change = _sandbox_override({k: v for k, v in (("memory_mb", memory_mb), ("cpu", cpu)) if v is not None},
                               "set-sandbox")
    with members_lock(Path(team["paths"]["team_file"])):
        fresh = load_team(Path(team["paths"]["team_file"]), Path(team["paths"]["members_file"]))
        member = next((m for m in fresh["members"] if m["name"] == name), None)
        if member is None:
            raise TeamError(f"anggota '{name}' tidak ada (yang ada: "
                            f"{', '.join(sorted(m['name'] for m in fresh['members'])) or '-'})")
        override = {} if reset else dict(member.get("sandbox") or {})
        override.update(change)
        # Nilai yang sama dengan bawaan tim tidak disimpan sebagai pengecualian.
        default_cpu, default_memory = sandbox_limits(fresh)
        if override.get("memory_mb") == default_memory:
            override.pop("memory_mb")
        if "cpu" in override and float(override["cpu"]) == float(default_cpu):
            override.pop("cpu")
        member["sandbox"] = override
        if not hermes.dry_run:
            save_members(fresh)
    team.clear()
    team.update(fresh)
    active = member.get("active", True)
    if active:   # anggota nonaktif: profile-nya diparkir; nilainya diterapkan `apply` saat ia aktif lagi
        hermes.config_set(member["profile"], sandbox_settings(team, member))
    cpu_now, memory_now = sandbox_limits(team, member)
    return {"name": name, "profile": member["profile"], "memory_mb": memory_now, "cpu": cpu_now,
            "override": dict(override), "applied": bool(active), "worst_case_mb": sandbox_worst_case_mb(team),
            "managed_blocked": sorted(hermes.managed_blocked), "failures": list(hermes.failures)}


def set_member_active(team: dict, hermes: Hermes, name: str, active: bool) -> dict:
    with team_lock(team):
        with members_lock(Path(team["paths"]["team_file"])):
            fresh = load_team(Path(team["paths"]["team_file"]), Path(team["paths"]["members_file"]))
            member = next((m for m in fresh["members"] if m["name"] == name), None)
            if member is None:
                raise TeamError(f"anggota '{name}' tidak ada")
            member["active"] = bool(active)
            if not hermes.dry_run:
                save_members(fresh)
        team.clear()
        team.update(fresh)
        result = cmd_apply(team, hermes, only=[name] if active else [], host_only=not active,
                           sync_shared=[name] if active else ())
        notes: list[str] = []
        if not hermes.dry_run and (active or team["onboarding"]["hide_channel_on_offboard"]):
            # Samakan izin channel pribadi dengan status: aktif → pemilik melihat lagi; nonaktif → dicabut.
            try:
                api = _discord(hermes, None)
                if active:
                    channel = api.get_channel(member["channel_id"])
                    if channel is None:
                        notes.append(f"channel pribadinya ({member['channel_id']}) sudah tidak ada — jalankan "
                                     f"`onboard --user-id {member['user_id']}` untuk membuat yang baru")
                    else:
                        _assert_private_channel(team, api, channel, member["user_id"], notes, who=name)
                        notes.append("akses ke channel pribadinya dipulihkan")
                else:
                    api.delete_overwrite(member["channel_id"], member["user_id"], reason=f"Hermes: {name} dinonaktifkan")
                    notes.append("akses ke channel pribadinya dicabut (channel & isinya disimpan)")
            except TeamError as exc:
                notes.append(f"izin channel tidak diubah ({exc})")
        return {"member": name, "active": active, "notes": notes, **result}


def _sandbox_cleanup_commands(profile: str) -> list[str]:
    """Perintah docker untuk sandbox satu profile. Kit TIDAK menjalankannya (volume = isi /workspace)."""
    return [f'docker ps -a --filter "label=hermes-profile={profile}" --format "{{{{.ID}}}} {{{{.Names}}}} {{{{.Mounts}}}}"'
            "    # lihat container & volume-nya",
            f'docker rm -f $(docker ps -aq --filter "label=hermes-profile={profile}")    # hapus container',
            "docker volume rm <nama-volume-dari-kolom-Mounts>    # hapus /workspace (PERMANEN)"]


def remove_member_plan(team: dict, hermes: Hermes, name: str, *, delete_profile: bool = False,
                       delete_channel: bool = False) -> dict:
    """Apa saja yang akan dihapus / dibiarkan oleh `remove-member` (tidak mengubah apa pun)."""
    member = next((m for m in team["members"] if m["name"] == name), None)
    if member is None:
        raise TeamError(f"anggota '{name}' tidak ada di members.yaml")
    if member.get("active", True):
        raise TeamError(f"anggota '{name}' masih AKTIF. Offboard dulu: `hermes_team.py set-active {name} off` "
                        f"(atau `offboard --user-id {member['user_id']}`), lalu ulangi remove-member.")
    profile = member["profile"]
    registry = shared_skill_registry(team)["skills"]
    origin = sorted(s for s, e in registry.items()
                    if e.get("origin_member") == name or e.get("origin_profile") == profile)
    targeted = sorted(s for s, e in registry.items() if name in (e.get("targets") or []) and s not in origin)
    will = [f"entri '{name}' di {team['paths']['members_file']}"]
    if targeted:
        will.append(f"nama '{name}' dari daftar penerima skill bersama: {', '.join(targeted)}")
    if origin:
        will.append(f"skill bersama kiriman '{name}' ditarik dari semua penerima berikut arsipnya: {', '.join(origin)}")
    keep: list[str] = []
    home = hermes.home(profile)
    if delete_profile:
        will.append(f"profile {profile} ({home}: memory, .env/secret, skill, riwayat sesi) lewat `hermes profile delete`"
                    if hermes.dir_exists(profile) else f"profile {profile}: sudah tidak ada di disk (dilewati)")
    elif hermes.dir_exists(profile):
        keep.append(f"profile {profile} di {home} (hapus: tambah --delete-profile, atau `hermes profile delete {profile}`)")
    if delete_channel:
        will.append(f"channel Discord {member['channel_id']} berikut seluruh riwayat pesannya")
    else:
        keep.append(f"channel Discord {member['channel_id']} (hapus: tambah --delete-channel, atau hapus di Discord)")
    keep.append(f"container & volume sandbox profile {profile} (kit tidak menghapusnya):")
    keep += ["    " + line for line in _sandbox_cleanup_commands(profile)]
    keep.append("angka pemakaian lama di buku besar (state/usage.db) — tetap tersimpan sebagai riwayat")
    return {"member": member, "will_delete": will, "kept": keep, "origin_skills": origin, "target_skills": targeted}


REMOVED_CHANNELS_FILE = "removed_channels.json"
REMOVED_CHANNELS_SCHEMA = 1


def removed_channels_path(team: dict) -> Path:
    return Path(team["paths"]["state_dir"]) / REMOVED_CHANNELS_FILE


def _load_removed_channels(path: Path) -> dict:
    """Isi `removed_channels.json` yang sah; berkas hilang / rusak / bentuk salah → daftar kosong baru."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    channels = data.get("channels") if isinstance(data, dict) else None
    if not isinstance(channels, dict):
        return {}
    return {str(cid): entry for cid, entry in channels.items() if isinstance(entry, dict)}


def _write_removed_channels(path: Path, channels: dict) -> None:
    atomic_write(path, json.dumps({"schema": REMOVED_CHANNELS_SCHEMA, "channels": channels},
                                  ensure_ascii=False, indent=1) + "\n", mode=0o600)


def record_removed_channel(team: dict, member: dict, *, channel_name: str = "") -> None:
    """Catat channel anggota yang dihapus dari members.yaml di `<state_dir>/removed_channels.json` (nisan):
    teamctl menolak membaca / menulis / menghapus channel itu walau tak lagi tercatat sebagai channel pribadi.
    Dipanggil di bawah kunci tim SEBELUM entri members.yaml dibuang. Tidak pernah dihapus otomatis kecuali
    oleh `remove_member` sendiri bila channelnya benar-benar berhasil dihapus (`forget_removed_channel`)."""
    path = removed_channels_path(team)
    channels = _load_removed_channels(path)
    channels[member["channel_id"]] = {"name": channel_name, "member": member["name"], "removed_at": now_iso()}
    _write_removed_channels(path, channels)


def forget_removed_channel(team: dict, channel_id: str) -> None:
    path = removed_channels_path(team)
    channels = _load_removed_channels(path)
    if channels.pop(channel_id, None) is not None:
        _write_removed_channels(path, channels)


def remove_member(team: dict, hermes: Hermes, name: str, *, delete_profile: bool = False,
                  delete_channel: bool = False, api: Optional[DiscordAPI] = None) -> dict:
    """Hapus PERMANEN anggota yang sudah nonaktif: entri members.yaml + rujukan di registry skill bersama;
    opsional profile (`hermes profile delete`) dan channel Discord. Sandbox Docker tidak disentuh.
    Di bawah kunci tim. Dengan dry-run hanya mengembalikan rencana."""
    with team_lock(team):
        reload_team(team)
        plan = remove_member_plan(team, hermes, name, delete_profile=delete_profile, delete_channel=delete_channel)
        member, done, notes = plan["member"], [], []
        if hermes.dry_run:
            return {**plan, "done": done, "notes": ["dry-run: tidak ada yang dihapus"], "dry_run": True}
        # 1) skill bersama: tarik kiriman orang ini selagi ia masih dikenal, lalu lepas namanya dari daftar penerima.
        for skill in plan["origin_skills"]:
            try:
                _unshare_skill(team, hermes, skill, ("all",))
                done.append(f"skill bersama '{skill}' ditarik dari semua penerima")
            except (TeamError, OSError) as exc:
                notes.append(f"skill bersama '{skill}' tidak bisa ditarik ({exc})")
        registry = shared_skill_registry(team)
        touched = False
        for entry in registry["skills"].values():
            if name in (entry.get("targets") or []):
                entry["targets"] = [t for t in entry["targets"] if t != name]
                touched = True
        if touched:
            _save_registry(team, registry)
            done.append(f"'{name}' dilepas dari daftar penerima skill bersama")
        # 2) nisan channel DULU (crash setelah ini tidak meninggalkan channel tanpa perlindungan), baru members.yaml
        record_removed_channel(team, member)
        with members_lock(Path(team["paths"]["team_file"])):
            fresh = load_team(Path(team["paths"]["team_file"]), Path(team["paths"]["members_file"]))
            fresh["members"] = [m for m in fresh["members"] if m["name"] != name]
            save_members(fresh)
        team.clear()
        team.update(fresh)
        done.append(f"entri '{name}' dihapus dari members.yaml")
        # 3) roster & rute diselaraskan (anggota nonaktif memang sudah tanpa rute; roster memuat daftar nonaktif).
        result = cmd_apply(team, hermes, only=[], host_only=True)
        # 4) profile
        if delete_profile and hermes.dir_exists(member["profile"]):
            if hermes.run("profile", "delete", member["profile"], "--yes"):
                done.append(f"profile {member['profile']} dihapus (`hermes profile delete`)")
            else:
                notes.append(f"profile {member['profile']} GAGAL dihapus — jalankan sendiri: "
                             f"hermes profile delete {member['profile']}")
        # 5) channel
        if delete_channel:
            try:
                client = _discord(hermes, api)
                if client.get_channel(member["channel_id"]) is None:
                    notes.append(f"channel {member['channel_id']} sudah tidak ada")
                else:
                    client.delete_channel(member["channel_id"], reason=f"Hermes: anggota {name} dihapus permanen")
                    done.append(f"channel Discord {member['channel_id']} dihapus")
                    forget_removed_channel(team, member["channel_id"])      # channelnya sudah tidak ada
            except TeamError as exc:
                notes.append(f"channel {member['channel_id']} TIDAK terhapus ({exc}); hapus manual di Discord")
        return {**plan, "done": done, "notes": notes, "dry_run": False, **result}

