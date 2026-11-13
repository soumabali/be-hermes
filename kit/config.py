"""Bagian dari kit tim (dipecah dari hermes_team.py). Lihat docs/KONTRAK.md dan README."""
from __future__ import annotations
import contextlib
import copy
import difflib
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable, Optional
from kit_base import TeamError, _id
from kit.constants import ALIAS_RE, ALLOW_PLACEHOLDERS_ENV, DEFAULT_USER_COMMANDS, ENV_NAME_RE, GOAL_COMMANDS, MODEL_COMPRESSION_KEYS, NAME_RE, PLACEHOLDER_ID_RE, REPORT_KINDS, SANDBOX_CPU_RANGE, SANDBOX_DEFAULT_MEMORY_MB, SANDBOX_MEMORY_MB_RANGE, SECRET_LIKE_KEYS
from kit.templates import DEFAULT_WELCOME, DEFAULT_WELCOME_BACK
from kit.util import _bool, _int, _number, _profile, _read_yaml_file, atomic_write, dump_yaml, file_lock


def _secret_like_keys(block: Any, trail: str = "") -> list[str]:
    """Key di blok provider yang kelihatan berisi kredensial (rekursif). `key_env` hanya NAMA env → boleh."""
    found: list[str] = []
    if isinstance(block, dict):
        for key, value in block.items():
            label = f"{trail}{key}"
            if re.sub(r"[\s\-]+", "_", str(key).strip().lower()) in SECRET_LIKE_KEYS:
                found.append(label)
            found += _secret_like_keys(value, label + ".")
    elif isinstance(block, list):
        for item in block:
            found += _secret_like_keys(item, trail)
    return found


def _sandbox_override(raw: Any, where: str) -> dict:
    """`sandbox: {memory_mb, cpu}` milik satu anggota → dict berisi hanya key yang diisi (boleh kosong)."""
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        raise TeamError(f"{where}: harus mapping (memory_mb, cpu)")
    unknown = sorted(str(k) for k in raw if k not in ("memory_mb", "cpu"))
    if unknown:
        raise TeamError(f"{where}.{unknown[0]}: tidak dikenal (pilihan: memory_mb, cpu)")
    out: dict = {}
    if raw.get("memory_mb") is not None:
        low, high = SANDBOX_MEMORY_MB_RANGE
        memory = _int(raw["memory_mb"], f"{where}.memory_mb")
        if not low <= memory <= high:
            raise TeamError(f"{where}.memory_mb: harus {low}..{high} MB (sekarang {memory})")
        out["memory_mb"] = memory
    if raw.get("cpu") is not None:
        low_cpu, high_cpu = SANDBOX_CPU_RANGE
        cpu = _number(raw["cpu"], f"{where}.cpu")
        if not low_cpu <= cpu <= high_cpu:
            raise TeamError(f"{where}.cpu: harus {low_cpu}..{high_cpu} (sekarang {cpu})")
        out["cpu"] = int(cpu) if cpu == int(cpu) else float(cpu)
    return out


def _report_time(value: Any, where: str) -> str:
    """Jam laporan "HH:MM" (24 jam, waktu lokal usage.timezone). Kosong/false = laporan itu mati."""
    if value is None or value is False or value == "":
        return ""
    if isinstance(value, (int, float)):
        # YAML 1.1 membaca 10:30 tanpa kutip sebagai bilangan (630), jadi jamnya sudah hilang di sini.
        raise TeamError(f"{where}: tulis jam di dalam tanda kutip, mis. \"08:00\" (sekarang {value!r})")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise TeamError(f"{where}: '{value}' bukan jam \"HH:MM\" 24 jam (mis. \"08:00\"); kosongkan untuk mematikan")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _load_usage(raw: Any) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TeamError("usage: harus mapping (timezone, report_channel_id, reports)")
    channel = raw.get("report_channel_id")
    reports_raw = raw.get("reports")
    if reports_raw is None or reports_raw is False:
        reports_raw = {}
    if not isinstance(reports_raw, dict):
        raise TeamError("usage.reports: harus mapping (daily, weekly, monthly)")
    unknown = sorted(str(k) for k in reports_raw if k not in REPORT_KINDS)
    if unknown:
        raise TeamError(f"usage.reports.{unknown[0]}: tidak dikenal (pilihan: {', '.join(REPORT_KINDS)})")
    return {
        "timezone": str(raw.get("timezone") or "").strip(),
        # Channel tujuan laporan pemakaian terjadwal (tools/usage_post.py). Kosong = tidak ada.
        "report_channel_id": _id(channel, "usage.report_channel_id") if channel else "",
        "reports": {kind: _report_time(reports_raw.get(kind), f"usage.reports.{kind}") for kind in REPORT_KINDS},
    }


def _load_llm(raw: dict) -> dict:
    llm = raw.get("llm")
    if not llm:
        return {}
    if not isinstance(llm, dict):
        raise TeamError("llm: harus mapping")
    providers = {}
    for name, block in (llm.get("providers") or {}).items():
        if not re.match(r"^[a-z0-9][a-z0-9_-]*$", str(name)) or not isinstance(block, dict):
            raise TeamError(f"llm.providers.{name}: nama huruf kecil dan isinya mapping")
        block = dict(block)
        leaked = _secret_like_keys(block)
        if leaked:
            raise TeamError(f"llm.providers.{name}: key '{leaked[0]}' terlihat seperti secret. Jangan tulis API key "
                            "di team.yaml (blok provider diteruskan ke `hermes config set` lewat argv dan tercatat "
                            "di log). Taruh di /etc/hermes/.env dan rujuk namanya lewat key_env.")
        if "api" not in block:
            raise TeamError(f"llm.providers.{name}.api wajib diisi (URL OpenAI-compatible)")
        key_env = block.get("key_env")
        if key_env and not ENV_NAME_RE.match(str(key_env)):
            raise TeamError(f"llm.providers.{name}.key_env '{key_env}' bukan nama env yang valid")
        providers[str(name)] = block
    models = {}
    for alias, block in (llm.get("models") or {}).items():
        alias = str(alias)
        if not ALIAS_RE.match(alias) or not isinstance(block, dict):
            raise TeamError(f"llm.models.{alias}: alias tidak valid")
        provider = str(block.get("provider") or "").strip()
        model = str(block.get("model") or "").strip()
        if not provider or not model:
            raise TeamError(f"llm.models.{alias}: provider dan model wajib diisi")
        if provider.startswith("custom:") and provider[7:] not in providers:
            raise TeamError(f"llm.models.{alias}: provider {provider} tidak ada di llm.providers")
        models[alias] = {"provider": provider, "model": model}
        # Ambang kompresi khusus model ini (opsional; tanpa ini → scale.*). Model berjendela konteks besar
        # boleh dipangkas lebih jarang: tiap pemangkasan menulis ulang riwayat dan memutus prompt cache.
        for key in MODEL_COMPRESSION_KEYS:
            if block.get(key) is not None:
                models[alias][key] = _int(block[key], f"llm.models.{alias}.{key}", 1)
        threshold, prune = (models[alias].get(k) for k in MODEL_COMPRESSION_KEYS)
        if threshold and prune and prune >= threshold:
            raise TeamError(f"llm.models.{alias}.proactive_prune_tokens ({prune}) harus lebih kecil dari "
                            f"compression_threshold_tokens ({threshold})")
    if not models:
        raise TeamError("llm.models: minimal satu model")
    out = {"providers": providers, "models": models}
    for key in ("default_model", "admin_model", "shared_model", "lobby_model", "aux_model"):
        value = str(llm.get(key) or llm.get("default_model") or "")
        if value not in models:
            raise TeamError(f"llm.{key}: '{value}' tidak ada di llm.models")
        out[key] = value
    fallback = [str(a) for a in (llm.get("fallback") or [])]
    for alias in fallback:
        if alias not in models:
            raise TeamError(f"llm.fallback: '{alias}' tidak ada di llm.models")
    out["fallback"] = fallback
    out["per_user_keys"] = _bool(llm.get("per_user_keys"), "llm.per_user_keys", False)
    return out


SCALE_DEFAULTS = {
    "max_concurrent_sessions": 8,     # slot sesi aktif bersamaan untuk anggota (seluruh gateway)
    "admin_reserved_slots": 2,        # slot tambahan yang hanya bisa dipakai admin (tidak ikut antre)
    "user_rate_per_minute": 6,        # pesan per menit per orang (0 = tanpa batas)
    "user_burst": 4,
    "queue_max_wait_seconds": 180,
    "agent_max_turns": 60,            # langkah tool per giliran
    "goals_max_turns": 4,             # lanjutan otomatis /goal (4 × agent_max_turns langkah dari satu pesan)
    "delegation_max_children": 1,     # subagent paralel per giliran (tiap subagent = satu percakapan LLM lagi)
    "compression_threshold_tokens": 64000,
    "proactive_prune_tokens": 48000,
    "idle_compact_after_seconds": 3600,
    "busy_input_mode": "queue",
    "title_llm": False,               # judul sesi pakai LLM? false = judul instan, tanpa request
    # --- docs/RENCANA-SKALA.md. Yang ditegakkan plugin team-guard dikirim lewat roster.json → settings.
    "user_max_concurrent": 2,         # sesi aktif per orang (minimal 1); pesan berikutnya antre
    "max_llm_parallel": 0,            # panggilan LLM paralel seluruh tim (0 = tidak dibatasi); isi dari tools/probe_llm.py
    "daily_token_budget": 0,          # token per orang per hari (0 = tanpa batas)
    "team_daily_token_budget": 0,     # token seluruh tim per hari (0 = tanpa batas)
    "adaptive_throttle": True,        # turunkan batas sesi efektif saat 429 beruntun, pulih perlahan
    "allow_member_cron": False,       # false = toolset cronjob mati untuk anggota & profile bersama (admin tetap)
    "cron_min_interval_minutes": 15,  # jadwal cron lebih rapat dari ini ditolak plugin (0 = tanpa batas)
    "allow_goals": False,             # false = /goal dan /subgoal keluar dari daftar perintah bawaan anggota
    "session_retention_days": 90,     # sesi berakhir yang lebih tua dipangkas Hermes (0 = kit tidak mengatur)
}


# Setelan `scale` yang nilainya minimal 1 (0 akan berarti "semua ditolak", bukan "tanpa batas").
SCALE_MIN_ONE = ("user_max_concurrent",)


SECRETS_DEFAULTS = {
    "max_keys": 40, "min_length": 6, "scrub_chat": True, "delete_secret_messages": True,
    "block_env_dump": True, "hint": True, "allow_names": [], "deny_names": [],
}


ONBOARDING_DEFAULTS = {
    "enabled": True,                    # role @backend diberikan → channel + profile + sambutan otomatis
    "give_entry_role": True,            # user baru di server mendapat roles.entry
    "remove_entry_role": True,          # role entry dicabut setelah onboarding selesai
    "verify_actor": True,               # hanya kalau pemberi @backend adalah admin tim (audit log Discord)
    "trusted_actor_ids": [],            # bot lain yang boleh memberi @backend (mis. bot verifikasi)
    "offboard_on_role_remove": True,    # @backend dicabut / user keluar → anggota dinonaktifkan
    "hide_channel_on_offboard": True,   # dan izin melihat channel pribadinya dicabut (data tetap)
    "entry_role_on_offboard": True,     # user yang masih di server kembali mendapat roles.entry
    "reconcile_minutes": 10,            # cek berkala @backend yang terlewat saat bot offline (0 = mati)
    "admin_notices": True,              # kabar onboarding ke channel admin
    "welcome_message": DEFAULT_WELCOME,
    "welcome_back_message": DEFAULT_WELCOME_BACK,
}


SERVER_ADMIN_DEFAULTS = {
    "enabled": True,                    # tool discord_* di #hermes-admin
    "allow_moderation": True,           # timeout & kick (ban selalu manual)
    "allow_elevated_permissions": False,  # izin tinggi (Administrator, Manage Roles, ...) lewat Hermes
    "read_messages": True,              # discord_read_messages (channel pribadi selalu ditolak)
}


def default_user_commands(scale: dict) -> list[str]:
    """Perintah slash bawaan anggota. `/goal` dan `/subgoal` hanya ikut bila `scale.allow_goals: true`."""
    if scale.get("allow_goals"):
        return list(DEFAULT_USER_COMMANDS)
    return [c for c in DEFAULT_USER_COMMANDS if c not in GOAL_COMMANDS]


# Key yang dikenal di team.yaml (teratas dan per bagian) dan members.yaml. Key lain TIDAK menggagalkan
# pemuatan (kecuali yang sejak dulu ditolak: onboarding.*, usage.reports.*, sandbox per anggota) tetapi
# dikumpulkan ke team["warnings"]: salah ketik seperti scale.max_concurent_sessions dulu hilang diam-diam.
TOP_LEVEL_KEYS = (
    "team_name", "guild_id", "roles", "admins", "category_id", "channel_prefix", "profile_prefix",
    "template_profile", "shared", "admin", "team_skills_dir", "team_skills_in_personal",
    "strict_personal_routes", "strict_admin_routes", "lobby_restrict", "threads", "allow_browser", "llm",
    "sandbox", "scale", "usage", "secrets", "onboarding", "live_reload", "restart_fallback", "server_admin",
    "user_allowed_commands", "extra_routes", "members")


ROLE_KEYS = ("member", "admin", "entry")


SYSTEM_PROFILE_KEYS = ("profile", "channel_id", "auto_skills")


THREAD_KEYS = ("personal", "admin")


SANDBOX_KEYS = ("image", "cpu", "memory_mb", "network", "http_proxy", "no_proxy")


USAGE_KEYS = ("timezone", "report_channel_id", "reports")


LLM_KEYS = ("providers", "models", "default_model", "admin_model", "shared_model", "lobby_model", "aux_model",
            "fallback", "per_user_keys")


LLM_MODEL_KEYS = ("provider", "model", "compression_threshold_tokens", "proactive_prune_tokens")


MEMBERS_FILE_KEYS = ("members", "models")


MEMBER_KEYS = ("name", "display_name", "discord_user_id", "channel_id", "model", "active", "forward_env",
               "sandbox")


def _unknown_keys(block: Any, known: Iterable[str], where: str, source: str, warnings: list[str]) -> None:
    """Catat key di `block` yang tidak ada di `known`, dengan saran "maksudnya …?" (difflib)."""
    if not isinstance(block, dict):
        return
    known = [str(k) for k in known]
    for key in block:
        if str(key) in known:
            continue
        label = f"{where}.{key}" if where else str(key)
        near = difflib.get_close_matches(str(key), known, n=1, cutoff=0.6)
        hint = f"maksudnya '{near[0]}'?" if near else "pilihan: " + ", ".join(sorted(known))
        warnings.append(f"{source}: key '{label}' tidak dikenal dan DIABAIKAN ({hint})")


def _team_unknown_keys(raw: dict) -> list[str]:
    warnings: list[str] = []
    src = "team.yaml"
    _unknown_keys(raw, TOP_LEVEL_KEYS, "", src, warnings)
    for section, known in (("roles", ROLE_KEYS), ("shared", SYSTEM_PROFILE_KEYS), ("admin", SYSTEM_PROFILE_KEYS),
                           ("threads", THREAD_KEYS), ("sandbox", SANDBOX_KEYS), ("scale", SCALE_DEFAULTS),
                           ("usage", USAGE_KEYS), ("secrets", SECRETS_DEFAULTS),
                           ("server_admin", SERVER_ADMIN_DEFAULTS), ("llm", LLM_KEYS)):
        _unknown_keys(raw.get(section), known, section, src, warnings)
    llm = raw.get("llm")
    if isinstance(llm, dict) and isinstance(llm.get("models"), dict):
        for alias, block in llm["models"].items():
            _unknown_keys(block, LLM_MODEL_KEYS, f"llm.models.{alias}", src, warnings)
    return warnings


def _members_unknown_keys(mraw: dict) -> list[str]:
    warnings: list[str] = []
    src = "members.yaml"
    _unknown_keys(mraw, MEMBERS_FILE_KEYS, "", src, warnings)
    for i, entry in enumerate(mraw.get("members") or []):
        if isinstance(entry, dict):
            _unknown_keys(entry, MEMBER_KEYS, f"members[{i}]" + (f" ({entry['name']})" if entry.get("name") else ""),
                          src, warnings)
    return warnings


def placeholder_ids(team: dict) -> list[str]:
    """Key yang masih berisi ID contoh, sebagai "key (nilai)". `load_team` tetap memuatnya (contoh & uji
    memakainya); `check` gagal dan `apply` menolak bila daftar ini tidak kosong."""
    pairs: list[tuple[str, str]] = [
        ("guild_id", team["guild_id"]), ("roles.member", team["member_role"]), ("roles.admin", team["admin_role"]),
        ("roles.entry", team["entry_role"]), ("category_id", team["category_id"]),
        ("shared.channel_id", team["shared"]["channel_id"]), ("admin.channel_id", team["admin"]["channel_id"]),
        ("usage.report_channel_id", team["usage"]["report_channel_id"])]
    pairs += [(f"admins[{i}]", a) for i, a in enumerate(team["admins"])]
    pairs += [(f"onboarding.trusted_actor_ids[{i}]", a)
              for i, a in enumerate(team["onboarding"].get("trusted_actor_ids") or [])]
    for m in team["members"]:
        pairs += [(f"members.yaml {m['name']}.discord_user_id", m["user_id"]),
                  (f"members.yaml {m['name']}.channel_id", m["channel_id"])]
    return [f"{key} ({value})" for key, value in pairs if value and PLACEHOLDER_ID_RE.match(str(value))]


def placeholders_allowed(flag: bool = False) -> bool:
    return bool(flag) or os.environ.get(ALLOW_PLACEHOLDERS_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def placeholder_message(found: list[str]) -> str:
    return ("konfigurasi masih memakai ID contoh dari team.example.yaml / members.example.yaml:\n"
            + "\n".join(f"  • {item}" for item in found)
            + "\nGanti dengan ID asli (Discord → Developer Mode → klik kanan → Copy ID), lalu jalankan `check` lagi.")


def load_team(team_path: Path, members_path: Optional[Path] = None) -> dict:
    team_path = Path(team_path).expanduser()
    raw = _read_yaml_file(team_path, "team")
    warnings = _team_unknown_keys(raw)
    team_dir = team_path.parent.resolve()
    team: dict = {"team_name": str(raw.get("team_name") or "Backend")}
    team["guild_id"] = _id(raw.get("guild_id"), "guild_id")
    roles = raw.get("roles") or {}
    team["member_role"] = _id(roles.get("member"), "roles.member")
    team["admin_role"] = _id(roles.get("admin"), "roles.admin")
    team["entry_role"] = _id(roles.get("entry"), "roles.entry") if roles.get("entry") else ""
    if len({team["member_role"], team["admin_role"], team["entry_role"] or "-"}) < 3:
        raise TeamError("roles.member, roles.admin, dan roles.entry harus role yang berbeda")
    team["admins"] = [_id(a, f"admins[{i}]") for i, a in enumerate(raw.get("admins") or [])]
    if not team["admins"]:
        raise TeamError("admins: minimal satu user ID admin (tanpa ini semua orang bisa /update dan /restart)")
    team["category_id"] = _id(raw["category_id"], "category_id") if raw.get("category_id") else ""
    team["channel_prefix"] = str(raw.get("channel_prefix") or "hermes-")
    team["team_skills_dir"] = str(raw.get("team_skills_dir") or "").strip()
    team["team_skills_in_personal"] = _bool(raw.get("team_skills_in_personal"), "team_skills_in_personal", True)
    team["strict"] = _bool(raw.get("strict_personal_routes"), "strict_personal_routes", False)
    team["admin_strict"] = _bool(raw.get("strict_admin_routes"), "strict_admin_routes", True)
    team["lobby_restrict"] = _bool(raw.get("lobby_restrict"), "lobby_restrict", True)
    team["allow_browser"] = _bool(raw.get("allow_browser"), "allow_browser", False)
    threads = raw.get("threads") or {}
    if not isinstance(threads, dict):
        raise TeamError("threads: harus mapping (personal, admin)")
    team["threads"] = {"personal": _bool(threads.get("personal"), "threads.personal", True),
                       "admin": _bool(threads.get("admin"), "threads.admin", False)}
    team["usage"] = _load_usage(raw.get("usage"))
    prefix = str(raw.get("profile_prefix", "be-"))
    team["prefix"] = prefix
    team["template"] = _profile(str(raw.get("template_profile") or f"{prefix}template"), "template_profile")
    for key in ("shared", "admin"):
        block = raw.get(key) or {}
        team[key] = {
            "profile": _profile(str(block.get("profile") or f"team-{key}"), f"{key}.profile"),
            "channel_id": _id(block.get("channel_id"), f"{key}.channel_id"),
            "auto_skills": [str(s) for s in (block.get("auto_skills") or [])],
        }
    sandbox = raw.get("sandbox") or {}
    if not isinstance(sandbox, dict):
        raise TeamError("sandbox: harus mapping")
    team["sandbox"] = {
        "image": str(sandbox.get("image") or "").strip(),
        "cpu": _number(sandbox.get("cpu", 1), "sandbox.cpu"),
        "memory_mb": _int(sandbox.get("memory_mb", SANDBOX_DEFAULT_MEMORY_MB), "sandbox.memory_mb", 64),
        "network": str(sandbox.get("network") or "").strip(),
        "http_proxy": str(sandbox.get("http_proxy") or "").strip(),
        "no_proxy": str(sandbox.get("no_proxy") or "localhost,127.0.0.1,::1"),
    }
    team["llm"] = _load_llm(raw)
    scale = dict(SCALE_DEFAULTS)
    if raw.get("scale") is not None and not isinstance(raw.get("scale"), dict):
        raise TeamError("scale: harus mapping")
    scale.update({k: v for k, v in (raw.get("scale") or {}).items() if k in SCALE_DEFAULTS})
    for key, default in SCALE_DEFAULTS.items():
        if isinstance(default, bool):
            scale[key] = _bool(scale[key], f"scale.{key}", default)
        elif isinstance(default, int):
            scale[key] = _int(scale[key], f"scale.{key}", 1 if key in SCALE_MIN_ONE else 0)
    if scale["busy_input_mode"] not in ("queue", "interrupt", "steer"):
        raise TeamError("scale.busy_input_mode harus queue, interrupt, atau steer")
    team["scale"] = scale
    _validate_model_compression(team)
    secrets = copy.deepcopy(SECRETS_DEFAULTS)
    secrets.update({k: v for k, v in (raw.get("secrets") or {}).items() if k in SECRETS_DEFAULTS})
    for key, default in SECRETS_DEFAULTS.items():
        if isinstance(default, bool):
            secrets[key] = _bool(secrets[key], f"secrets.{key}", default)
        elif isinstance(default, int):
            secrets[key] = _int(secrets[key], f"secrets.{key}", 1)
        elif isinstance(default, list):
            if not isinstance(secrets[key], (list, tuple)):
                raise TeamError(f"secrets.{key}: harus daftar")
            secrets[key] = [str(v) for v in secrets[key]]
    team["secrets"] = secrets
    cmds = raw.get("user_allowed_commands")
    # Daftar eksplisit di team.yaml selalu menang (admin sengaja memilihnya), termasuk bila berisi goal/subgoal.
    team["user_commands"] = [str(c).lstrip("/").lower() for c in cmds] if cmds else default_user_commands(scale)
    team["extra_routes"] = list(raw.get("extra_routes") or [])
    team["onboarding"] = _load_onboarding(raw.get("onboarding"))
    server_admin = dict(SERVER_ADMIN_DEFAULTS)
    server_admin.update({k: _bool(v, f"server_admin.{k}", SERVER_ADMIN_DEFAULTS[k])
                         for k, v in (raw.get("server_admin") or {}).items() if k in SERVER_ADMIN_DEFAULTS})
    team["server_admin"] = server_admin
    # Rute & channel baru diterapkan plugin ke gateway yang sedang jalan (tanpa restart). Kalau itu
    # gagal: idle = restart halus otomatis saat tidak ada sesi aktif; manual = admin mengetik /restart.
    team["live_reload"] = _bool(raw.get("live_reload"), "live_reload", True)
    team["restart_fallback"] = str(raw.get("restart_fallback") or "idle").strip().lower()
    if team["restart_fallback"] not in ("idle", "manual"):
        raise TeamError("restart_fallback harus idle atau manual")

    members_file = Path(members_path).expanduser() if members_path else team_dir / "members.yaml"
    if members_file.exists():
        mraw = _read_yaml_file(members_file, "members")
    elif raw.get("members"):  # kompatibel dengan team.yaml v1 (anggota di file yang sama)
        mraw = {"members": raw.get("members"), "models": {}}
    else:
        mraw = {"members": [], "models": {}}
    if members_file.exists():
        warnings += _members_unknown_keys(mraw)
    team["warnings"] = warnings
    team["members"] = [_load_member(m, i, team) for i, m in enumerate(mraw.get("members") or [])]
    overrides = {}
    for key, alias in (mraw.get("models") or {}).items():
        if key not in ("shared", "admin", "lobby"):
            raise TeamError(f"members.yaml models.{key}: hanya shared, admin, lobby")
        if team["llm"] and alias not in team["llm"]["models"]:
            raise TeamError(f"members.yaml models.{key}: '{alias}' tidak ada di llm.models")
        overrides[key] = str(alias)
    team["model_overrides"] = overrides
    team["paths"] = {
        "team_file": str(team_path.resolve()), "members_file": str(members_file.resolve()),
        "team_dir": str(team_dir), "state_dir": str(team_dir / "state"),
    }
    _validate_unique(team)
    report_channel = team["usage"]["report_channel_id"]
    owner = next((m for m in team["members"] if m["channel_id"] == report_channel), None) if report_channel else None
    if owner is not None:
        raise TeamError(f"usage.report_channel_id: {report_channel} adalah channel pribadi anggota '{owner['name']}'. "
                        "Laporan pemakaian memuat angka semua orang; pakai channel admin atau channel khusus.")
    return team


def _validate_model_compression(team: dict) -> None:
    """Model yang hanya mengisi SATU dari dua ambang memakai `scale` untuk pasangannya; hasil akhirnya
    tetap harus pangkas < kompresi (0 di `scale` = fitur itu mati, tidak diperiksa)."""
    for alias, block in ((team["llm"] or {}).get("models") or {}).items():
        if not any(key in block for key in MODEL_COMPRESSION_KEYS):
            continue
        threshold, prune = compression_limits(team, alias)
        if threshold and prune and prune >= threshold:
            mine = next(key for key in MODEL_COMPRESSION_KEYS if key in block)
            raise TeamError(f"llm.models.{alias}.{mine}: hasil akhirnya pangkas {prune} ≥ kompresi {threshold} "
                            "(pasangannya diambil dari scale). Isi kedua ambang untuk model ini; "
                            "proactive_prune_tokens harus lebih kecil dari compression_threshold_tokens.")


def _load_onboarding(raw: Any) -> dict:
    if raw is not None and not isinstance(raw, dict):
        raise TeamError("onboarding: harus mapping")
    conf = copy.deepcopy(ONBOARDING_DEFAULTS)
    for key, value in (raw or {}).items():
        if key not in conf:
            raise TeamError(f"onboarding.{key}: tidak dikenal (pilihan: {', '.join(conf)})")
        if key in ("welcome_message", "welcome_back_message"):
            text = str(value or "").strip()
            if not text or len(text) > 1900:
                raise TeamError(f"onboarding.{key}: wajib diisi, maks 1900 karakter (batas Discord 2000)")
            conf[key] = text
        elif key == "trusted_actor_ids":
            conf[key] = [_id(v, f"onboarding.trusted_actor_ids[{i}]") for i, v in enumerate(value or [])]
        elif key == "reconcile_minutes":
            conf[key] = max(0, _int(value or 0, "onboarding.reconcile_minutes"))
        else:
            conf[key] = _bool(value, f"onboarding.{key}", bool(ONBOARDING_DEFAULTS[key]))
    return conf


def _load_member(m: Any, i: int, team: dict) -> dict:
    where = f"members[{i}]"
    if not isinstance(m, dict):
        raise TeamError(f"{where}: harus mapping")
    name = str(m.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise TeamError(f"{where}.name '{name}' tidak valid (huruf kecil, angka, '-' atau '_')")
    model = str(m.get("model") or "").strip()
    if model and team["llm"] and model not in team["llm"]["models"]:
        raise TeamError(f"{where}.model '{model}' tidak ada di llm.models")
    return {
        "name": name,
        "display_name": str(m.get("display_name") or name.capitalize()),
        "user_id": _id(m.get("discord_user_id"), f"{where}.discord_user_id"),
        "channel_id": _id(m.get("channel_id"), f"{where}.channel_id"),
        "profile": _profile(f"{team['prefix']}{name}", f"{where}.name"),
        "model": model,
        "active": _bool(m.get("active"), f"{where}.active", True),
        "forward_env": [str(e) for e in (m.get("forward_env") or [])],
        # Pengecualian batas sandbox untuk orang ini; key yang tidak diisi mengikuti team.yaml → sandbox.
        "sandbox": _sandbox_override(m.get("sandbox"), f"{where}.sandbox"),
    }


def _validate_unique(team: dict) -> None:
    seen_channels: dict = {}
    pairs = [(f"members.{m['name']}", m["channel_id"]) for m in team["members"]]
    pairs += [("shared", team["shared"]["channel_id"]), ("admin", team["admin"]["channel_id"])]
    for label, ch in pairs:
        if ch in seen_channels:
            raise TeamError(f"channel {ch} dipakai dua kali ({seen_channels[ch]} dan {label})")
        seen_channels[ch] = label
    profiles = [m["profile"] for m in team["members"]]
    profiles += [team["shared"]["profile"], team["admin"]["profile"], team["template"]]
    dupes = sorted({p for p in profiles if profiles.count(p) > 1})
    if dupes:
        raise TeamError(f"nama profile bentrok: {dupes}")
    names = [m["name"] for m in team["members"]]
    if len(set(names)) != len(names):
        raise TeamError("nama anggota dobel di members.yaml")
    users = [m["user_id"] for m in team["members"]]
    if len(set(users)) != len(users):
        raise TeamError("satu discord_user_id terdaftar di dua anggota")


def save_members(team: dict) -> None:
    """Tulis ulang members.yaml dari team['members'] + team['model_overrides'] (atomik)."""
    path = Path(team["paths"]["members_file"])
    rows = []
    for m in team["members"]:
        row = {"name": m["name"], "display_name": m["display_name"],
               "discord_user_id": m["user_id"], "channel_id": m["channel_id"]}
        if m.get("model"):
            row["model"] = m["model"]
        if not m.get("active", True):
            row["active"] = False
        if m.get("forward_env"):
            row["forward_env"] = list(m["forward_env"])
        if m.get("sandbox"):
            row["sandbox"] = dict(m["sandbox"])
        rows.append(row)
    body = {"members": rows}
    if team.get("model_overrides"):
        body["models"] = dict(team["model_overrides"])
    header = ("# members.yaml — anggota tim Hermes dan model AI per orang.\n"
              "# Boleh diedit manual, lalu jalankan: python3 hermes_team.py apply\n"
              "# Juga ditulis oleh tool admin di #hermes-admin dan `hermes_team.py add-member/set-model/set-sandbox`.\n")
    atomic_write(path, header + dump_yaml(body), mode=0o600)


@contextlib.contextmanager
def members_lock(team_file: Path):
    with file_lock(Path(team_file).resolve().parent / ".members.lock"):
        yield


_TEAM_LOCK = threading.local()


@contextlib.contextmanager
def team_lock(team: dict):
    """Kunci seluruh operasi yang mengubah tim (lintas proses: CLI, teamctl, onboarding dari plugin).

    Re-entrant di thread yang sama, jadi fungsi publik boleh saling memanggil. Tanpa kunci ini,
    dua `apply` yang berjalan bersamaan bisa saling menimpa `gateway.profile_routes`.
    """
    depth = getattr(_TEAM_LOCK, "depth", 0)
    if depth:
        _TEAM_LOCK.depth = depth + 1
        try:
            yield
        finally:
            _TEAM_LOCK.depth = depth
        return
    with file_lock(Path(team["paths"]["team_dir"]) / ".team.lock"):
        _TEAM_LOCK.depth = 1
        try:
            yield
        finally:
            _TEAM_LOCK.depth = 0


def _lock_depth() -> int:
    return getattr(_TEAM_LOCK, "depth", 0)


def reload_team(team: dict) -> dict:
    """Baca ulang team.yaml + members.yaml dari disk (di dalam team_lock) dan perbarui `team` di tempat."""
    fresh = load_team(Path(team["paths"]["team_file"]), Path(team["paths"]["members_file"]))
    team.clear()
    team.update(fresh)
    return team


def compression_limits(team: dict, alias: Optional[str] = None) -> tuple[int, int]:
    """(ambang kompresi, ambang pangkas) untuk model `alias`: nilai di llm.models.<alias> bila ada,
    selain itu `scale.*`."""
    scale = team["scale"]
    block = ((team.get("llm") or {}).get("models") or {}).get(alias or "", {})
    return (int(block.get("compression_threshold_tokens") or scale["compression_threshold_tokens"]),
            int(block.get("proactive_prune_tokens") or scale["proactive_prune_tokens"]))

