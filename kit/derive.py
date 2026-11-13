"""Bagian dari kit tim (dipecah dari hermes_team.py). Lihat docs/KONTRAK.md dan README."""
from __future__ import annotations
import copy
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Optional
import yaml

import kit_config
from kit_contracts import ROSTER_SCHEMA
from kit_base import KIT_VERSION, TeamError
from kit.constants import AUX_TASKS, CRON_TOOLSET, DM_DECLINE_MESSAGE, HOST_SIDE_TOOLSETS, KIT_DIR, KIT_SKILLS, LOBBY_DISABLED_TOOLSETS, PLUGIN_NAME, PROFILE_SCOPED_KEYS, TEAMCTL, WORKER_DISABLED_TOOLSETS
from kit.util import atomic_write, dump_yaml, managed_dir, now_iso
from kit.profiles import (MODEL_TARGETS, PROFILE_KINDS, SYSTEM_KINDS, base_settings, kind_of,  # noqa: F401
                          teamctl_server, worker_disabled_toolsets)
from kit.config import compression_limits


MEMINFO_PATH = Path("/proc/meminfo")


# --------------------------------------------------------------------------- setelan yang dihasilkan
def active_members(team: dict) -> list[dict]:
    return [m for m in team["members"] if m.get("active", True)]


def model_alias(team: dict, role: str, member: Optional[dict] = None) -> str:
    llm = team["llm"]
    if kind_of(role).alias_kind == "member":
        return (member or {}).get("model") or llm["default_model"]
    return team["model_overrides"].get(role) or llm[f"{role}_model"]


def build_routes(team: dict) -> list[dict]:
    g = team["guild_id"]
    routes = []
    for m in active_members(team):
        route = {"name": f"personal-{m['name']}", "platform": "discord",
                 "guild_id": g, "chat_id": m["channel_id"], "profile": m["profile"]}
        if team["strict"]:
            route["user_id"] = m["user_id"]
        routes.append(route)
    routes.append({"name": team["shared"]["profile"], "platform": "discord", "guild_id": g,
                   "chat_id": team["shared"]["channel_id"], "profile": team["shared"]["profile"]})
    if team["admin_strict"]:
        for i, admin in enumerate(team["admins"]):
            routes.append({"name": f"{team['admin']['profile']}-{i + 1}", "platform": "discord",
                           "guild_id": g, "chat_id": team["admin"]["channel_id"], "user_id": admin,
                           "profile": team["admin"]["profile"]})
    else:
        routes.append({"name": team["admin"]["profile"], "platform": "discord", "guild_id": g,
                       "chat_id": team["admin"]["channel_id"], "profile": team["admin"]["profile"]})
    routes.extend(team["extra_routes"])
    return routes


def model_settings(team: dict, alias: str) -> list[tuple[str, object]]:
    llm = team["llm"]
    if not llm:
        return []
    model = llm["models"][alias]
    settings: list[tuple[str, object]] = [(f"providers.{n}", b) for n, b in llm["providers"].items()]
    settings += [("model.provider", model["provider"]), ("model.default", model["model"])]
    chain = [llm["models"][a] for a in llm["fallback"] if a != alias]
    settings.append(("fallback_providers", [{"provider": c["provider"], "model": c["model"]} for c in chain]))
    return settings


def compression_settings(team: dict, alias: Optional[str] = None) -> list[tuple[str, object]]:
    """Ambang kompresi profile mengikuti MODEL utamanya (ditulis per profile, bukan di managed config)."""
    threshold, prune = compression_limits(team, alias)
    return [("compression.threshold_tokens", threshold), ("compression.proactive_prune_tokens", prune)]


def sandbox_limits(team: dict, member: Optional[dict] = None) -> tuple[Any, int]:
    """(cpu, memory_mb) sandbox satu profile: pengecualian anggota bila ada, selain itu bawaan tim."""
    override = (member or {}).get("sandbox") or {}
    return (override.get("cpu", team["sandbox"]["cpu"]),
            int(override.get("memory_mb", team["sandbox"]["memory_mb"])))


def sandbox_settings(team: dict, member: Optional[dict] = None) -> list[tuple[str, object]]:
    cpu, memory = sandbox_limits(team, member)
    return [("terminal.container_cpu", cpu), ("terminal.container_memory", memory)]


def scoped_settings(team: dict, role: str, member: Optional[dict] = None) -> list[tuple[str, object]]:
    """Setelan PROFILE_SCOPED_KEYS untuk satu profile (role: member/template/shared/admin/lobby)."""
    kind = kind_of(role)
    alias = model_alias(team, kind.alias_kind, member) if team["llm"] else None
    return sandbox_settings(team, member if kind.per_member else None) + compression_settings(team, alias)


def host_memory_mb(path: Optional[Path] = None) -> Optional[int]:
    """RAM host dalam MB dari /proc/meminfo; None bila tidak bisa dibaca (macOS, container tanpa /proc)."""
    try:
        text = Path(path or MEMINFO_PATH).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^MemTotal:\s+(\d+)\s*kB", text, re.MULTILINE)
    return int(match.group(1)) // 1024 if match else None


def sandbox_worst_case_mb(team: dict) -> int:
    """Plafon RAM bila SEMUA sandbox penuh sekaligus: anggota aktif + shared + admin."""
    return (sum(sandbox_limits(team, m)[1] for m in active_members(team))
            + 2 * sandbox_limits(team)[1])


def sandbox_warnings(team: dict) -> list[str]:
    total, host = sandbox_worst_case_mb(team), host_memory_mb()
    if host is None or total <= host:
        return []
    return [f"Plafon RAM sandbox terburuk {total} MB melebihi RAM server {host} MB. Batas itu plafon, bukan "
            "cadangan, tetapi bila banyak sandbox penuh bersamaan server bisa kehabisan memori. Turunkan "
            "sandbox.memory_mb / pengecualian per anggota (set-sandbox), atau tambah RAM."]


def used_model_aliases(team: dict) -> list[str]:
    """Alias yang benar-benar menjadi model UTAMA suatu profile (anggota aktif, bawaan, shared, admin, lobby)."""
    if not team["llm"]:
        return []
    aliases = [model_alias(team, "member", m) for m in active_members(team)]
    aliases += [team["llm"]["default_model"]] + [model_alias(team, role) for role in MODEL_TARGETS]
    return list(dict.fromkeys(aliases))


def fallback_warnings(team: dict) -> list[str]:
    """Peringatan (bukan error) soal rantai fallback. Fallback di provider yang sama tidak menolong saat
    endpoint itu membalas 429 atau mati; model yang juga satu-satunya isi `llm.fallback` tidak punya
    cadangan sama sekali (model yang sama dilewati, lihat `model_settings`)."""
    llm = team["llm"]
    if not llm:
        return []
    none: list[str] = []
    same: list[str] = []
    for alias in used_model_aliases(team):
        provider = llm["models"][alias]["provider"]
        chain = [llm["models"][a]["provider"] for a in llm["fallback"] if a != alias]
        if not chain:
            none.append(alias)
        elif all(p == provider for p in chain):
            same.append(f"{alias} ({provider})")
    warnings = []
    if none:
        warnings.append("model tanpa fallback sama sekali: " + ", ".join(none)
                        + " — bila endpoint-nya error/429, giliran langsung gagal")
    if same:
        warnings.append("semua fallback berada di provider yang sama dengan model utamanya: " + ", ".join(same)
                        + " — 429 atau gangguan di endpoint itu ikut menjatuhkan fallback")
    if warnings:
        warnings.append("saran: tambah provider kedua (endpoint cadangan atau key lain) di llm.providers dan "
                        "masukkan modelnya ke llm.fallback; lihat contoh di team.example.yaml")
    return warnings


def plugin_settings(team: dict) -> list[tuple[str, object]]:
    return [(f"plugins.entries.{PLUGIN_NAME}.settings.state_dir", team["paths"]["state_dir"])]


def gateway_channels(team: dict) -> tuple[list[str], list[str]]:
    """(allowed_channels, free_response_channels) untuk adapter Discord host."""
    personal = [m["channel_id"] for m in active_members(team)]
    shared, admin = team["shared"]["channel_id"], team["admin"]["channel_id"]
    # Bot hanya hidup di channel terdaftar; thread ikut lewat parent channel-nya.
    # Channel pribadi & admin: tanpa @mention. #hermes-shared: wajib @mention → thread.
    return personal + [shared, admin], personal + [admin]


def host_settings(team: dict) -> list[tuple[str, object]]:
    """Setelan profile default: pemilik bot, host multiplexer, sekaligus lobby."""
    allowed, free = gateway_channels(team)
    admin = team["admin"]["channel_id"]
    bindings = [{"id": team[k]["channel_id"], "skills": team[k]["auto_skills"]}
                for k in SYSTEM_KINDS if team[k]["auto_skills"]]
    scale = team["scale"]
    settings: list[tuple[str, object]] = [
        ("gateway.multiplex_profiles", True),
        ("gateway.profile_routes", build_routes(team)),
        ("group_sessions_per_user", True),
        ("discord.allowed_channels", allowed),
        ("discord.free_response_channels", free),
        # Channel tanpa-@mention (pribadi, admin): tiap pesan baru di channel membuka THREAD baru = sesi baru
        # dengan konteks sendiri; balasan di dalam thread tetap satu sesi. Memory/skill/.env/sandbox milik
        # profile tetap sama di semua thread. #hermes-admin dikecualikan lewat no_thread_channels.
        ("discord.free_response_auto_thread", bool(team["threads"]["personal"])),
        ("discord.no_thread_channels", [] if team["threads"]["admin"] else [admin]),
        ("discord.require_mention", True),
        ("discord.auto_thread", True),
        # Hanya pemegang role tim yang dilayani. Role tidak berlaku di DM → DM ditolak.
        ("DISCORD_ALLOWED_ROLES", [team["member_role"], team["admin_role"]]),
        ("DISCORD_HOME_CHANNEL", admin),
        ("unauthorized_dm_behavior", "decline"),
        ("gateway.unauthorized_dm_decline_message", DM_DECLINE_MESSAGE),
        ("platforms.discord.extra.allow_admin_from", team["admins"]),
        ("platforms.discord.extra.group_allow_admin_from", team["admins"]),
        ("platforms.discord.extra.user_allowed_commands", team["user_commands"]),
        ("platforms.discord.extra.group_user_allowed_commands", team["user_commands"]),
        ("platforms.discord.extra.channel_skill_bindings", bindings),
        # Skala: batas keras Hermes = slot anggota + cadangan admin. Antrean team-guard menahan anggota
        # di `max_concurrent_sessions`, jadi admin tetap bisa masuk saat anggota penuh.
        ("max_concurrent_sessions", int(scale["max_concurrent_sessions"]) + int(scale["admin_reserved_slots"])),
    ]
    if team["lobby_restrict"]:
        settings += [
            ("agent.disabled_toolsets", list(PROFILE_KINDS["lobby"].extra_disabled_toolsets)),
            ("memory.memory_enabled", False),
            ("memory.user_profile_enabled", False),
        ]
    if team["llm"]:
        settings += model_settings(team, model_alias(team, "lobby"))
    settings += scoped_settings(team, "lobby")
    settings += plugin_settings(team)
    return settings


def profile_settings(team: dict, role: str, member: Optional[dict] = None, *,
                     hermes_bin: str = "hermes", hermes_root: Optional[Path] = None) -> list[tuple[str, object]]:
    """Setelan per profile. Selalu eksplisit supaya tidak mewarisi setelan lobby dari clone.
    Bagian yang khas per jenis ada di kit.profiles.PROFILE_KINDS; model, batas sandbox/kompresi, dan plugin
    sama untuk semua jenis."""
    kind = kind_of(role)
    settings = base_settings(team, role, member, hermes_bin, hermes_root or Path("~/.hermes").expanduser())
    if team["llm"]:
        settings += model_settings(team, model_alias(team, kind.alias_kind, member))
    settings += scoped_settings(team, role, member)
    settings += plugin_settings(team)
    return settings


def managed_config(team: dict) -> dict:
    sandbox, scale, llm = team["sandbox"], team["scale"], team["llm"]
    terminal: dict = {
        "backend": "docker",                     # terminal/file/execute_code di container per profile
        "docker_mount_cwd_to_workspace": False,  # jangan pernah mount direktori host ke /workspace
        "container_persistent": True,            # /workspace tiap orang bertahan antar sesi
        "docker_volumes": [],                    # tidak ada mount host tambahan
        "docker_extra_args": [],                 # tidak ada --privileged dan sejenisnya
    }
    # container_cpu / container_memory dan dua ambang kompresi (PROFILE_SCOPED_KEYS) SENGAJA tidak dipin di
    # sini. Key yang ada di managed config menang atas semua profile dan tidak bisa dibedakan per profile,
    # padahal batas RAM perlu pengecualian per orang dan ambang kompresi mengikuti model tiap profile.
    # Keduanya ditulis `apply` ke config.yaml tiap profile (profile_settings / host_settings).
    # Tetap aman terhadap ANGGOTA: agent bekerja di sandbox Docker tanpa akses ke ~/.hermes (tidak ada
    # mount host: docker_volumes, docker_extra_args, dan mount cwd tetap dipin di bawah), dan perintah slash
    # yang mengubah config hanya untuk admin (user_allowed_commands). Yang hilang: jaminan dari root.
    # Operator dengan shell sebagai user Hermes kini bisa mengubah empat nilai ini langsung di config.yaml
    # profile (dulu hanya root lewat /etc/hermes); `apply` berikutnya mengembalikannya ke nilai tim.
    if sandbox["image"]:
        terminal["docker_image"] = sandbox["image"]
    if sandbox["network"]:
        terminal["docker_network"] = sandbox["network"]
    if sandbox["http_proxy"]:
        proxy = sandbox["http_proxy"]
        terminal["docker_env"] = {
            "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
            "NO_PROXY": sandbox["no_proxy"], "no_proxy": sandbox["no_proxy"]}
    policy: dict = {
        "terminal": terminal,
        "security": {"redact_secrets": True},
        "display": {"busy_input_mode": scale["busy_input_mode"]},
        "agent": {"max_turns": int(scale["agent_max_turns"]), "budget_warning_ratio": 0.8},
        "goals": {"max_turns": int(scale["goals_max_turns"])},
        "delegation": {"max_concurrent_children": int(scale["delegation_max_children"]), "max_spawn_depth": 1},
        # Ambang threshold_tokens / proactive_prune_tokens: per profile, mengikuti modelnya (lihat di atas).
        "compression": {"idle_compact_after_seconds": int(scale["idle_compact_after_seconds"])},
    }
    # Cron: satu job pada satu waktu per profile (tiap job = percakapan LLM di luar antrean team-guard).
    policy["cron"] = {"max_parallel_jobs": 1}
    if int(scale["session_retention_days"]) > 0:
        # Thread per percakapan membuat jumlah sesi tumbuh cepat; Hermes hanya memangkas sesi yang SUDAH
        # berakhir dan tidak aktif selama retention_days. Buku besar pemakaian (usage.db) tidak terpengaruh.
        policy["sessions"] = {"auto_prune": True, "retention_days": int(scale["session_retention_days"])}
    if llm:
        policy["providers"] = copy.deepcopy(llm["providers"])
        aux = llm["models"][llm["aux_model"]]
        # Cron job tanpa model sendiri memakai model murah (urutan Hermes: pin per job > cron.model > model.default).
        policy["cron"].update({"model": aux["model"], "model_provider": aux["provider"]})
        auxiliary = {task: {"provider": aux["provider"], "model": aux["model"]} for task in AUX_TASKS}
        if not scale["title_llm"]:
            auxiliary["title_generation"]["model_upgrade_enabled"] = False
        policy["auxiliary"] = auxiliary
    return policy


def managed_yaml(team: dict) -> str:
    return ("# /etc/hermes/config.yaml — managed scope (milik root, 0644).\n"
            "# Nilai di sini menang atas config.yaml SEMUA profile dan tidak bisa diubah user.\n"
            "# Dibuat oleh hermes_team.py. Setelah dipasang: hermes doctor && hermes gateway restart\n"
            + dump_yaml(managed_config(team)))


def _flatten(data: Any, trail: str = "") -> dict:
    """Mapping bersarang → {"a.b.c": nilai}. Daftar dan mapping kosong dihitung sebagai nilai."""
    if isinstance(data, dict) and data:
        out: dict = {}
        for key, value in data.items():
            out.update(_flatten(value, f"{trail}.{key}" if trail else str(key)))
        return out
    return {trail: data} if trail else {}


def managed_diff(team: dict, path: Optional[Path] = None) -> dict:
    """Bandingkan `managed_config(team)` dengan berkas managed yang TERPASANG (hanya membaca).

    Kembalikan {path, missing_file, unreadable, missing, changed, extra, moved}:
      missing  key yang diinginkan kit tetapi belum ada di berkas terpasang
      changed  key yang nilainya berbeda
      extra    key di berkas terpasang yang tidak (lagi) dipin kit — nilainya tetap menang atas semua profile
      moved    bagian dari `extra`: PROFILE_SCOPED_KEYS, yang sekarang ditulis `apply` per profile
    """
    path = Path(path) if path else managed_dir() / "config.yaml"
    result: dict = {"path": str(path), "missing_file": False, "unreadable": "", "missing": [], "changed": [],
                    "extra": [], "moved": []}
    try:
        installed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        result["missing_file"] = True
        return result
    except (OSError, yaml.YAMLError) as exc:
        result["unreadable"] = f"{type(exc).__name__}: {exc}"
        return result
    want = _flatten(yaml.safe_load(dump_yaml(managed_config(team))))
    have = _flatten(installed if isinstance(installed, dict) else {})
    result["missing"] = sorted(k for k in want if k not in have)
    result["changed"] = sorted(k for k in want if k in have and have[k] != want[k])
    result["moved"] = sorted(k for k in PROFILE_SCOPED_KEYS if _get_nested(installed, k) is not None)
    result["extra"] = sorted(k for k in have if k not in want)
    return result


def managed_diff_clean(diff: dict) -> bool:
    return not (diff["missing_file"] or diff["unreadable"] or diff["missing"] or diff["changed"] or diff["extra"])


def managed_install_command(team: dict) -> str:
    """Perintah sudo persis untuk memasang managed config (team.yaml disebut eksplisit: HOME root berbeda)."""
    cmd = ["sudo", sys.executable or "python3", str(KIT_DIR / "hermes_team.py"), "managed", "--install",
           "-c", team["paths"]["team_file"]]
    if os.environ.get("HERMES_MANAGED_DIR", "").strip():
        cmd[1:1] = ["env", f"HERMES_MANAGED_DIR={managed_dir()}"]
    return shlex.join(cmd)


def managed_diff_lines(diff: dict) -> list[str]:
    path = diff["path"]
    if diff["missing_file"]:
        return [f"{path} belum ada"]
    if diff["unreadable"]:
        return [f"{path} tidak terbaca ({diff['unreadable']})"]
    lines = [f"kurang (belum dipin): {key}" for key in diff["missing"]]
    lines += [f"berbeda: {key}" for key in diff["changed"]]
    for key in diff["extra"]:
        note = (" — sudah dipindah kit ke config per profile; selama masih dipin di sini nilai per profile "
                "TIDAK berlaku") if key in diff["moved"] else " — tidak dipin kit (bila sengaja ditambah manual, abaikan)"
        lines.append(f"berlebih: {key}{note}")
    return lines


def install_managed(team: dict, *, target_dir: Optional[Path] = None, euid: Optional[int] = None,
                    dry_run: bool = False) -> Path:
    """Tulis managed config secara atomik (0644). Harus root; kalau bukan → TeamError berisi perintah sudo."""
    euid = os.geteuid() if euid is None else euid
    target = Path(target_dir) if target_dir else managed_dir()
    path = target / "config.yaml"
    if euid != 0:
        raise TeamError(f"memasang {path} harus sebagai root. Jalankan persis:\n  {managed_install_command(team)}")
    if dry_run:
        return path
    target.mkdir(parents=True, exist_ok=True)
    atomic_write(path, managed_yaml(team), mode=0o644)
    return path


def roster_data(team: dict, hermes_root: Path, hermes_bin: str) -> dict:
    channels: dict = {}
    users: dict = {}
    llm = team["llm"]
    for m in active_members(team):
        channels[m["channel_id"]] = {"profile": m["profile"], "kind": "personal", "owner": m["user_id"], "name": m["name"]}
        users[m["user_id"]] = {"name": m["name"], "profile": m["profile"], "channel_id": m["channel_id"],
                               "display_name": m["display_name"],
                               "model": model_alias(team, "member", m) if llm else ""}
    # Anggota nonaktif: hanya untuk mengenali channel lamanya (tidak dirutekan, tidak dilayani).
    inactive = {m["user_id"]: {"name": m["name"], "channel_id": m["channel_id"]}
                for m in team["members"] if not m.get("active", True)}
    for kind in SYSTEM_KINDS:
        channels[team[kind]["channel_id"]] = {"profile": team[kind]["profile"], "kind": kind}
    reserved = sorted({str(p["key_env"]) for p in (llm.get("providers") or {}).values()
                       if p.get("key_env")}) if llm else []
    allowed, free = gateway_channels(team)
    return {
        "version": 3,                 # lama, dipertahankan demi kompatibilitas; pembeda sebenarnya: "schema"
        "schema": ROSTER_SCHEMA,
        "kit_version": KIT_VERSION,
        "generated": now_iso(),
        "team_name": team["team_name"],
        "guild_id": team["guild_id"],
        "hermes_root": str(hermes_root),
        "hermes_bin": hermes_bin,
        "admins": list(team["admins"]),
        "roles": {"member": team["member_role"], "admin": team["admin_role"], "entry": team["entry_role"]},
        "shared_channel": team["shared"]["channel_id"],
        "admin_channel": team["admin"]["channel_id"],
        "category_id": team["category_id"],
        "channels": channels,
        "users": users,
        "inactive_users": inactive,
        "reserved_env": reserved,
        # Keadaan gateway yang diinginkan; plugin menerapkannya ke proses yang sedang jalan (live reload).
        "gateway": {"profile_routes": build_routes(team), "allowed_channels": allowed,
                    "free_response_channels": free},
        # Setelan yang ditegakkan plugin DITURUNKAN dari tabel kit_config.FIELDS (baris ber-`plugin_key`):
        # menambah satu setelan di sana otomatis muncul di sini; tidak ada salinan manual lagi.
        **kit_config.plugin_view(team, "root"),                 # live_reload, restart_fallback
        "onboarding": kit_config.plugin_view(team, "onboarding"),
        "kit": {"python": sys.executable, "script": str(KIT_DIR / "hermes_team.py"),
                "team_file": team["paths"]["team_file"], "members_file": team["paths"]["members_file"]},
        "settings": kit_config.plugin_view(team, "settings"),
        # Anggaran token harian dihitung dari buku besar ini, dengan batas hari di zona waktu yang sama.
        "usage": {**kit_config.plugin_view(team, "usage"),
                  "ledger": str(Path(team["paths"]["state_dir"]) / "usage.db")},
    }


# --------------------------------------------------------------------------- nilai untuk `hermes config set`
# String CSV untuk key yang default-nya string; JSON untuk struktur lain.
CSV_KEYS = {"discord.allowed_channels", "discord.free_response_channels", "discord.no_thread_channels",
            "DISCORD_ALLOWED_ROLES"}


def cli_value(key: str, value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list) and key in CSV_KEYS:
        return ",".join(str(v) for v in value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _get_nested(cfg: Any, key: str) -> Any:
    node = cfg
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _same(key: str, current: Any, desired: Any) -> bool:
    if key in CSV_KEYS:
        def norm(v: Any) -> list[str]:
            if v is None:
                return []
            if isinstance(v, str):
                return [s.strip() for s in v.split(",") if s.strip()]
            if isinstance(v, (list, tuple, set)):
                return [str(s) for s in v]
            return [str(v)]   # satu ID tanpa kutip dibaca YAML sebagai int
        return norm(current) == norm(desired)
    if current is None and desired in ([], {}, ""):
        return True
    if isinstance(desired, bool) or isinstance(current, bool):
        return current is desired or current == desired
    if isinstance(desired, (int, float)) and isinstance(current, str):
        try:                      # "0.5" yang tersimpan sebagai teks tetap sama dengan 0.5
            return float(current) == float(desired)
        except ValueError:
            return False
    return current == desired


def nested_yaml(settings: list[tuple[str, object]], profile: str = "default") -> str:
    root: dict = {}
    env_lines = []
    for key, value in settings:
        if key.isupper():
            env_lines.append(f"#   {key}={cli_value(key, value)}")
            continue
        node = root
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    where = "~/.hermes" if profile == "default" else f"~/.hermes/profiles/{profile}"
    head = f"# Ditulis ke {where}/config.yaml.\n"
    if env_lines:
        head += f"# Ditulis ke {where}/.env:\n" + "\n".join(env_lines) + "\n"
    return head + dump_yaml(root)

