"""Muat team.yaml + members.yaml menjadi dict `team`.

Validasi tiap setelan ada di `kit_config` (tabel FIELDS = skema). Di sini hanya yang saling merujuk antar
bagian: katalog llm, role yang harus berbeda, anggota, keunikan channel/profile, kunci tim, dan nama-nama
lama (SCALE_DEFAULTS dst.) yang diturunkan dari skema supaya kode lain tetap bisa memakainya.
"""
from __future__ import annotations

import contextlib
import copy
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

import kit_config
from kit.constants import (ALIAS_RE, ALLOW_PLACEHOLDERS_ENV, DEFAULT_USER_COMMANDS, ENV_NAME_RE, GOAL_COMMANDS,
                           MODEL_COMPRESSION_KEYS, NAME_RE, PLACEHOLDER_ID_RE, SANDBOX_CPU_RANGE,
                           SANDBOX_MEMORY_MB_RANGE, SECRET_LIKE_KEYS)
from kit.profiles import MODEL_TARGETS, SYSTEM_KINDS  # noqa: F401
from kit.util import _read_yaml_file, atomic_write, dump_yaml, file_lock
from kit_base import TeamError, _id
from kit_config import _bool, _int, _number, _profile, _report_time  # noqa: F401  (nama lama tetap tersedia)


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
    for key in kit_config.llm_model_keys():
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


# --- Nama lama, diturunkan dari skema (kit_config.FIELDS) — jangan diisi tangan.
SCALE_DEFAULTS = kit_config.defaults("scale")
SCALE_MIN_ONE = tuple(f.key for f in kit_config.fields_in("scale") if f.min == 1)
SECRETS_DEFAULTS = kit_config.defaults("secrets")
ONBOARDING_DEFAULTS = kit_config.defaults("onboarding")
SERVER_ADMIN_DEFAULTS = kit_config.defaults("server_admin")
TOP_LEVEL_KEYS = kit_config.top_level_keys()
ROLE_KEYS = kit_config.section_keys("roles")
SYSTEM_PROFILE_KEYS = kit_config.section_keys("shared")
THREAD_KEYS = kit_config.section_keys("threads")
SANDBOX_KEYS = kit_config.section_keys("sandbox")
USAGE_KEYS = kit_config.section_keys("usage")
LLM_KEYS = kit_config.section_keys("llm")
LLM_MODEL_KEYS = kit_config.LLM_MODEL_KEYS
MEMBERS_FILE_KEYS = kit_config.MEMBERS_FILE_KEYS
MEMBER_KEYS = kit_config.MEMBER_KEYS
_unknown_keys = kit_config._unknown_keys
_team_unknown_keys = kit_config.unknown_key_warnings
_members_unknown_keys = kit_config.members_unknown_key_warnings


def _load_usage(raw: Any) -> dict:
    return kit_config.usage_view({"usage": raw})


def _load_onboarding(raw: Any) -> dict:
    return {path.split(".", 1)[1]: value for path, value in kit_config.parse_section("onboarding", raw).items()}


def default_user_commands(scale: dict) -> list[str]:
    """Perintah slash bawaan anggota. `/goal` dan `/subgoal` hanya ikut bila `scale.allow_goals: true`."""
    if scale.get("allow_goals"):
        return list(DEFAULT_USER_COMMANDS)
    return [c for c in DEFAULT_USER_COMMANDS if c not in GOAL_COMMANDS]


def placeholder_ids(team: dict) -> list[str]:
    """Key yang masih berisi ID contoh, sebagai "key (nilai)". `load_team` tetap memuatnya (contoh & uji
    memakainya); `check` gagal dan `apply` menolak bila daftar ini tidak kosong."""
    pairs: list[tuple[str, str]] = [
        ("guild_id", team["guild_id"]), ("roles.member", team["member_role"]), ("roles.admin", team["admin_role"]),
        ("roles.entry", team["entry_role"]), ("category_id", team["category_id"]),
        *((f"{kind}.channel_id", team[kind]["channel_id"]) for kind in SYSTEM_KINDS),
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
    warnings = kit_config.unknown_key_warnings(raw)
    team_dir = team_path.parent.resolve()
    team = kit_config.parse_top_level(raw)          # semua setelan bertipe + default dari tabel FIELDS
    if len({team["member_role"], team["admin_role"], team["entry_role"] or "-"}) < 3:
        raise TeamError("roles.member, roles.admin, dan roles.entry harus role yang berbeda")
    if not team["admins"]:
        raise TeamError("admins: minimal satu user ID admin (tanpa ini semua orang bisa /update dan /restart)")
    team["llm"] = _load_llm(raw)
    _validate_model_compression(team)
    cmds = raw.get("user_allowed_commands")
    # Daftar eksplisit di team.yaml selalu menang (admin sengaja memilihnya), termasuk bila berisi goal/subgoal.
    team["user_commands"] = ([str(c).lstrip("/").lower() for c in cmds] if cmds
                             else default_user_commands(team["scale"]))
    team["extra_routes"] = list(raw.get("extra_routes") or [])
    members_file = Path(members_path).expanduser() if members_path else team_dir / "members.yaml"
    if members_file.exists():
        mraw = _read_yaml_file(members_file, "members")
    elif raw.get("members"):  # kompatibel dengan team.yaml v1 (anggota di file yang sama)
        mraw = {"members": raw.get("members"), "models": {}}
    else:
        mraw = {"members": [], "models": {}}
    if members_file.exists():
        warnings += kit_config.members_unknown_key_warnings(mraw)
    team["warnings"] = warnings
    team["members"] = [_load_member(m, i, team) for i, m in enumerate(mraw.get("members") or [])]
    team["model_overrides"] = _model_overrides(team, mraw)
    team["paths"] = {
        "team_file": str(team_path.resolve()), "members_file": str(members_file.resolve()),
        "team_dir": str(team_dir), "state_dir": str(team_dir / "state"),
    }
    _validate_unique(team)
    _validate_report_channel(team)
    return team


def _model_overrides(team: dict, mraw: dict) -> dict:
    """`models:` di members.yaml → {shared|admin|lobby: alias} (kind yang boleh jadi target set-model)."""
    overrides = {}
    for key, alias in (mraw.get("models") or {}).items():
        if key not in MODEL_TARGETS:
            raise TeamError(f"members.yaml models.{key}: hanya {', '.join(MODEL_TARGETS)}")
        if team["llm"] and alias not in team["llm"]["models"]:
            raise TeamError(f"members.yaml models.{key}: '{alias}' tidak ada di llm.models")
        overrides[key] = str(alias)
    return overrides


def _validate_report_channel(team: dict) -> None:
    report_channel = team["usage"]["report_channel_id"]
    owner = next((m for m in team["members"] if m["channel_id"] == report_channel), None) if report_channel else None
    if owner is not None:
        raise TeamError(f"usage.report_channel_id: {report_channel} adalah channel pribadi anggota '{owner['name']}'. "
                        "Laporan pemakaian memuat angka semua orang; pakai channel admin atau channel khusus.")


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
    pairs += [(kind, team[kind]["channel_id"]) for kind in SYSTEM_KINDS]
    for label, ch in pairs:
        if ch in seen_channels:
            raise TeamError(f"channel {ch} dipakai dua kali ({seen_channels[ch]} dan {label})")
        seen_channels[ch] = label
    profiles = [m["profile"] for m in team["members"]]
    profiles += [team[kind]["profile"] for kind in SYSTEM_KINDS] + [team["template"]]
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

