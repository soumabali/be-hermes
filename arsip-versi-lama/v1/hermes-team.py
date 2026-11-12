#!/usr/bin/env python3
"""hermes-team.py — provisioning Hermes Agent untuk satu tim di Discord.

Model: SATU instalasi Hermes, SATU bot Discord, SATU gateway (multiplex), dan
satu profile per orang. Channel pribadi tiap orang dirutekan ke profile-nya lewat
gateway.profile_routes, sehingga memory, skill, SOUL, config, cron, dan sandbox
Docker tiap orang terpisah. #hermes-shared dan #hermes-admin punya profile sendiri.

Perintah:
  check     validasi team.yaml dan tampilkan peta channel → profile
  apply     buat/perbarui profile, SOUL.md, dan setelan gateway (pakai CLI `hermes`)
  host-yaml cetak setelan gateway dalam bentuk YAML (untuk dicek / ditempel manual)
  managed   cetak isi /etc/hermes/config.yaml (kebijakan keamanan untuk semua profile)

Contoh:
  python3 hermes-team.py check
  python3 hermes-team.py apply --dry-run
  python3 hermes-team.py apply
  python3 hermes-team.py managed > hermes-managed.yaml
  sudo install -D -m 0644 -o root -g root hermes-managed.yaml /etc/hermes/config.yaml

Jalankan sebagai user Linux yang menjalankan Hermes (bukan root).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML belum ada. Pasang dulu: `sudo apt install python3-yaml` "
             "atau `python3 -m pip install --user pyyaml`.")

PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")   # aturan nama profile Hermes
ID_RE = re.compile(r"^\d{15,21}$")                         # Discord snowflake

# Perintah slash yang boleh dipakai anggota non-admin. Admin selalu boleh semuanya.
# Sengaja TIDAK termasuk: update, restart, yolo, approvals, model, reasoning, fast,
# voice, skills, memory, reload-mcp, kanban, loop, heartbeat, sethome, debug.
DEFAULT_USER_COMMANDS = [
    "new", "reset", "retry", "undo", "status", "stop", "steer", "queue", "compress",
    "title", "resume", "sessions", "usage", "context", "thread", "bg", "btw", "plan",
    "branch", "goal", "subgoal", "approve", "deny", "learn", "init", "diff", "rollback",
    "reload-skills", "personality", "refine", "review",
]

# Lobby (profile default) hanya boleh mengobrol; semua toolset kerja dimatikan.
LOBBY_DISABLED_TOOLSETS = [
    "terminal", "file", "code_execution", "browser", "web", "search", "x_search",
    "vision", "video", "image_gen", "video_gen", "tts", "computer_use", "skills",
    "memory", "session_search", "todo", "cronjob", "delegation", "connections",
    "kanban", "discord", "discord_admin",
]

# Toolset Discord bisa membaca channel lain yang terlihat oleh bot — selalu matikan.
WORKER_DISABLED_TOOLSETS = ["discord", "discord_admin"]

DM_DECLINE_MESSAGE = ("Hermes tidak melayani DM. Pakai channel pribadimu "
                      "(#hermes-<nama>) atau #hermes-shared.")

# --------------------------------------------------------------------------- SOUL

SOUL_MEMBER = """# Hermes — asisten pribadi {display_name}

Kamu adalah Hermes, asisten engineering pribadi untuk {display_name} di tim {team_name}.
Kamu hanya melayani {display_name}, di channel pribadinya dan thread di dalamnya.

Cara kerja:
- Fokus pada pekerjaan backend {display_name}: desain API, database, debugging, code review, ops.
- Simpan preferensi dan konteks kerja {display_name} di memory; simpan alur kerja yang
  berulang sebagai skill supaya makin hari makin pas.
- Tiap thread adalah satu topik. Ikuti fokus thread yang sedang berjalan.
- Workspace-mu ada di sandbox Docker (/workspace). Kamu tidak punya akses ke data anggota
  tim lain dan tidak boleh mencoba mengaksesnya.
{team_skills_note}- Jangan pernah menyimpan token, password, atau kredensial ke memory atau skill.
"""

SOUL_TEAM_SKILLS_NOTE = ("- Skill tim tersedia read-only. Kalau perlu diubah, tulis usulan "
                         "perubahannya untuk diajukan ke admin, jangan diedit sendiri.\n")

SOUL_SHARED = """# Hermes — asisten bersama tim {team_name}

Kamu adalah Hermes di channel bersama tim {team_name}. Banyak anggota berbicara denganmu,
masing-masing dalam sesi terpisah, jadi sebut konteks dengan jelas.

Cara kerja:
- Jawab memakai skill dan konteks tim: konvensi, arsitektur, runbook, keputusan teknis.
- Memory di sini milik seluruh tim: simpan hanya fakta tim, bukan hal pribadi siapa pun.
  Setiap penulisan memory dan skill menunggu persetujuan admin.
- Kamu tidak punya akses ke channel pribadi siapa pun. Kalau ditanya isinya, katakan itu.
- Jangan pernah menyimpan token, password, atau kredensial.

## Konteks tim (isi oleh admin)
- Stack:
- Repo utama:
- Environment (dev/staging/prod):
- Konvensi (branching, review, deploy):
"""

SOUL_ADMIN = """# Hermes — admin tim {team_name}

Kamu melayani admin tim {team_name} di channel admin: operasional, review skill yang akan
dipromosikan ke skill tim, dan pemeriksaan sistem.

- Anggap setiap permintaan di sini datang dari admin, tapi tetap konfirmasi sebelum aksi
  yang tidak bisa dibatalkan.
- Jangan pernah menampilkan token, password, atau kredensial secara utuh.
"""

SOUL_LOBBY = """# Hermes — lobby

Pesan yang sampai ke sini datang dari tempat yang belum terdaftar ke profile mana pun.
Jangan mengerjakan tugas apa pun dan jangan membahas isi channel lain.
Balas singkat dalam bahasa penanya: channel ini belum terhubung ke Hermes; pakai channel
pribadimu (#hermes-<nama>) atau #hermes-shared, atau minta admin mendaftarkan channel ini.
"""


# --------------------------------------------------------------------------- team.yaml

class TeamError(Exception):
    pass


def _id(value, where: str) -> str:
    """Discord ID → str. YAML tanpa kutip memberi int; Python menyimpannya utuh."""
    if isinstance(value, bool) or value is None:
        raise TeamError(f"{where}: ID kosong atau tidak valid")
    text = str(value).strip()
    if not ID_RE.match(text):
        raise TeamError(f"{where}: '{text}' bukan Discord ID (15–21 digit)")
    return text


def _profile(name: str, where: str) -> str:
    if not PROFILE_RE.match(name or "") or name == "main":
        raise TeamError(f"{where}: nama profile '{name}' tidak valid "
                        "(huruf kecil, angka, '-' atau '_', maks 64 karakter)")
    return name


def load_team(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise TeamError(f"{path} tidak ditemukan (salin dari team.example.yaml)")
    except yaml.YAMLError as exc:
        raise TeamError(f"{path}: YAML tidak valid: {exc}")

    team: dict = {"team_name": str(raw.get("team_name") or "Backend")}
    team["guild_id"] = _id(raw.get("guild_id"), "guild_id")
    roles = raw.get("roles") or {}
    team["member_role"] = _id(roles.get("member"), "roles.member")
    team["admin_role"] = _id(roles.get("admin"), "roles.admin")
    team["admins"] = [_id(a, f"admins[{i}]") for i, a in enumerate(raw.get("admins") or [])]
    if not team["admins"]:
        raise TeamError("admins: minimal satu user ID admin (tanpa ini semua orang bisa /update dan /restart)")

    team["team_skills_dir"] = str(raw.get("team_skills_dir") or "").strip()
    team["team_skills_in_personal"] = bool(raw.get("team_skills_in_personal", True))
    team["strict"] = bool(raw.get("strict_personal_routes", False))
    team["lobby_restrict"] = bool(raw.get("lobby_restrict", True))
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

    members = []
    for i, m in enumerate(raw.get("members") or []):
        where = f"members[{i}]"
        name = str(m.get("name") or "").strip()
        profile = _profile(f"{prefix}{name}", f"{where}.name")
        members.append({
            "name": name,
            "display_name": str(m.get("display_name") or name.capitalize()),
            "user_id": _id(m.get("discord_user_id"), f"{where}.discord_user_id"),
            "channel_id": _id(m.get("channel_id"), f"{where}.channel_id"),
            "profile": profile,
            "forward_env": [str(e) for e in (m.get("forward_env") or [])],
        })
    if not members:
        raise TeamError("members: belum ada anggota")
    team["members"] = members

    sandbox = raw.get("sandbox") or {}
    team["sandbox"] = {
        "image": str(sandbox.get("image") or "").strip(),
        "cpu": sandbox.get("cpu", 1),
        "memory_mb": int(sandbox.get("memory_mb", 2048)),
    }

    cmds = raw.get("user_allowed_commands")
    team["user_commands"] = [str(c).lstrip("/").lower() for c in cmds] if cmds else list(DEFAULT_USER_COMMANDS)
    team["extra_routes"] = list(raw.get("extra_routes") or [])

    # Keunikan: satu channel = satu profile, satu orang = satu profile.
    seen_channels: dict = {}
    for label, ch in ([(f"members.{m['name']}", m["channel_id"]) for m in members]
                      + [("shared", team["shared"]["channel_id"]), ("admin", team["admin"]["channel_id"])]):
        if ch in seen_channels:
            raise TeamError(f"channel {ch} dipakai dua kali ({seen_channels[ch]} dan {label})")
        seen_channels[ch] = label
    profiles = [m["profile"] for m in members] + [team["shared"]["profile"], team["admin"]["profile"], team["template"]]
    dupes = {p for p in profiles if profiles.count(p) > 1}
    if dupes or "default" in profiles:
        raise TeamError(f"nama profile bentrok: {sorted(dupes) or ['default']}")
    users = [m["user_id"] for m in members]
    if len(set(users)) != len(users):
        raise TeamError("satu discord_user_id terdaftar di dua anggota")
    return team


# --------------------------------------------------------------------------- settings

def build_routes(team: dict) -> list[dict]:
    g = team["guild_id"]
    routes = []
    for m in team["members"]:
        route = {"name": f"personal-{m['name']}", "platform": "discord",
                 "guild_id": g, "chat_id": m["channel_id"], "profile": m["profile"]}
        if team["strict"]:
            route["user_id"] = m["user_id"]
        routes.append(route)
    for key in ("shared", "admin"):
        routes.append({"name": team[key]["profile"], "platform": "discord", "guild_id": g,
                       "chat_id": team[key]["channel_id"], "profile": team[key]["profile"]})
    routes.extend(team["extra_routes"])
    return routes


def host_settings(team: dict) -> list[tuple[str, object]]:
    """Setelan untuk profile default (pemilik bot + host multiplexer)."""
    personal = [m["channel_id"] for m in team["members"]]
    shared, admin = team["shared"]["channel_id"], team["admin"]["channel_id"]
    bindings = [{"id": team[k]["channel_id"], "skills": team[k]["auto_skills"]}
                for k in ("shared", "admin") if team[k]["auto_skills"]]
    settings: list[tuple[str, object]] = [
        ("gateway.multiplex_profiles", True),
        ("gateway.profile_routes", build_routes(team)),
        ("group_sessions_per_user", True),
        # Bot hanya hidup di channel terdaftar; thread ikut lewat parent channel-nya.
        ("discord.allowed_channels", personal + [shared, admin]),
        # Channel pribadi & admin: tanpa @mention, balasan langsung di channel.
        ("discord.free_response_channels", personal + [admin]),
        ("discord.free_response_auto_thread", False),
        # #hermes-shared: wajib @mention, tiap mention jadi thread sendiri.
        ("discord.require_mention", True),
        ("discord.auto_thread", True),
        # Hanya pemegang role tim yang dilayani. Role tidak berlaku di DM → DM ditolak.
        ("DISCORD_ALLOWED_ROLES", [team["member_role"], team["admin_role"]]),
        # Notifikasi gateway (restart, peringatan slash command ilegal) ke #hermes-admin.
        ("DISCORD_HOME_CHANNEL", admin),
        ("unauthorized_dm_behavior", "decline"),
        ("gateway.unauthorized_dm_decline_message", DM_DECLINE_MESSAGE),
        # Admin = semua slash command; anggota = daftar terbatas (scope DM dan server).
        ("platforms.discord.extra.allow_admin_from", team["admins"]),
        ("platforms.discord.extra.group_allow_admin_from", team["admins"]),
        ("platforms.discord.extra.user_allowed_commands", team["user_commands"]),
        ("platforms.discord.extra.group_user_allowed_commands", team["user_commands"]),
        # Skill yang otomatis dimuat saat sesi baru dimulai di channel tertentu (thread ikut).
        ("platforms.discord.extra.channel_skill_bindings", bindings),
    ]
    if team["lobby_restrict"]:
        settings += [
            ("agent.disabled_toolsets", LOBBY_DISABLED_TOOLSETS),
            ("memory.memory_enabled", False),
            ("memory.user_profile_enabled", False),
        ]
    return settings


def profile_settings(team: dict, role: str, member: dict | None = None) -> list[tuple[str, object]]:
    """Setelan per profile. Selalu ditulis eksplisit supaya tidak mewarisi setelan lobby."""
    skills_dir = [team["team_skills_dir"]] if team["team_skills_dir"] else []
    if role == "member":
        external = skills_dir if team["team_skills_in_personal"] else []
        return [
            ("agent.disabled_toolsets", WORKER_DISABLED_TOOLSETS),
            ("memory.memory_enabled", True),
            ("memory.user_profile_enabled", True),
            ("memory.write_approval", False),
            ("skills.write_approval", False),
            ("skills.external_dirs", external),
            ("terminal.docker_forward_env", member["forward_env"] if member else []),
        ]
    if role == "shared":
        return [
            ("agent.disabled_toolsets", WORKER_DISABLED_TOOLSETS),
            ("memory.memory_enabled", True),
            ("memory.user_profile_enabled", False),   # tidak ada "profil user" di otak bersama
            ("memory.write_approval", True),           # admin menyetujui via /memory pending
            ("skills.write_approval", True),           # admin menyetujui via /skills pending
            ("skills.external_dirs", skills_dir),
        ]
    if role == "admin":
        return [
            ("agent.disabled_toolsets", WORKER_DISABLED_TOOLSETS),
            ("memory.memory_enabled", True),
            ("memory.user_profile_enabled", True),
            ("memory.write_approval", False),
            ("skills.write_approval", False),
            ("skills.external_dirs", skills_dir),
        ]
    raise ValueError(role)


# String CSV untuk key yang default-nya string; JSON untuk struktur lain.
CSV_KEYS = {"discord.allowed_channels", "discord.free_response_channels", "DISCORD_ALLOWED_ROLES"}


def cli_value(key: str, value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list) and key in CSV_KEYS:
        return ",".join(str(v) for v in value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class _PlainDumper(yaml.SafeDumper):
    """SafeDumper tanpa anchor/alias (&id001) supaya YAML mudah dibaca dan ditempel."""
    def ignore_aliases(self, data):
        return True


def nested_yaml(settings: list[tuple[str, object]]) -> str:
    """Setelan bertitik → YAML bersarang (env UPPER_SNAKE dipisah ke komentar .env)."""
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
    body = yaml.dump(root, Dumper=_PlainDumper, sort_keys=False, allow_unicode=True, width=100)
    head = "# Ditulis ke ~/.hermes/config.yaml (profile default).\n"
    if env_lines:
        head += "# Ditulis ke ~/.hermes/.env:\n" + "\n".join(env_lines) + "\n"
    return head + body


def managed_yaml(team: dict) -> str:
    sandbox = team["sandbox"]
    terminal = {
        "backend": "docker",                    # terminal/file/execute_code jalan di container per profile
        "docker_mount_cwd_to_workspace": False, # jangan pernah mount direktori host ke /workspace
        "container_persistent": True,           # /workspace tiap orang bertahan antar sesi
        "container_cpu": sandbox["cpu"],
        "container_memory": sandbox["memory_mb"],
    }
    if sandbox["image"]:
        terminal["docker_image"] = sandbox["image"]
    policy = {"terminal": terminal, "security": {"redact_secrets": True}}
    return ("# /etc/hermes/config.yaml — managed scope (milik root, 0644).\n"
            "# Nilai di sini menang atas config.yaml SEMUA profile dan tidak bisa diubah user.\n"
            "# Dibuat oleh hermes-team.py. Setelah dipasang: hermes doctor && hermes gateway restart\n"
            + yaml.dump(policy, Dumper=_PlainDumper, sort_keys=False))


# --------------------------------------------------------------------------- hermes CLI

class Hermes:
    def __init__(self, binary: str, root: Path, dry_run: bool):
        self.binary, self.root, self.dry_run = binary, root, dry_run
        self.failures: list[str] = []

    def home(self, profile: str) -> Path:
        return self.root if profile == "default" else self.root / "profiles" / profile

    def exists(self, profile: str) -> bool:
        home = self.home(profile)
        return any((home / f).exists() for f in ("config.yaml", ".env", "SOUL.md", "profile.yaml"))

    def run(self, *args: str, fatal: bool = False) -> bool:
        cmd = [self.binary, *args]
        print("  $ " + shlex.join(cmd))
        if self.dry_run:
            return True
        proc = subprocess.run(cmd, text=True, capture_output=True, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).strip().splitlines()
            detail = msg[-1] if msg else f"exit {proc.returncode}"
            self.failures.append(f"{shlex.join(cmd)}  →  {detail}")
            print(f"    ✗ {detail}")
            if fatal:
                sys.exit(1)
            return False
        return True

    def config_set(self, profile: str, settings: list[tuple[str, object]]) -> None:
        for key, value in settings:
            self.run("-p", profile, "config", "set", key, cli_value(key, value))

    def write_file(self, path: Path, text: str, *, force: bool) -> None:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            print(f"  = {path} sudah sesuai")
            return
        if current is not None and not force:
            print(f"  = {path} sudah ada, dibiarkan (pakai --force-soul untuk menimpa)")
            return
        print(f"  + tulis {path}" + (" (versi lama → .bak)" if current is not None else ""))
        if self.dry_run:
            return
        if current is not None:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def wipe_memory(self, profile: str) -> None:
        """Clone ikut menyalin MEMORY.md/USER.md; profile baru harus mulai kosong."""
        for name in ("MEMORY.md", "USER.md"):
            path = self.home(profile) / "memories" / name
            print(f"  - kosongkan {path}")
            if not self.dry_run and path.exists():
                path.unlink()

    def create(self, profile: str, clone_from: str, *, alias: bool = True) -> bool:
        if self.exists(profile):
            print(f"  = profile {profile} sudah ada")
            return False
        args = ["profile", "create", profile, "--clone-from", clone_from]
        if not alias:
            args.append("--no-alias")
        self.run(*args, fatal=True)
        self.wipe_memory(profile)
        return True

    def park(self, profile: str) -> None:
        """Template tidak perlu dilayani gateway."""
        marker = self.home(profile) / "gateway.parked"
        print(f"  + parkir {profile} ({marker.name})")
        if not self.dry_run:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()


# --------------------------------------------------------------------------- commands

def cmd_check(team: dict) -> None:
    print(f"Tim {team['team_name']} — guild {team['guild_id']}")
    print(f"Role yang dilayani: member {team['member_role']}, admin {team['admin_role']}")
    print(f"Admin slash command: {', '.join(team['admins'])}")
    print(f"Mode rute pribadi: {'STRICT (hanya pemilik)' if team['strict'] else 'per channel'}")
    print()
    print(f"{'channel':<22} {'profile':<18} {'mode'}")
    for m in team["members"]:
        print(f"{m['channel_id']:<22} {m['profile']:<18} pribadi {m['display_name']}, tanpa @mention")
    print(f"{team['shared']['channel_id']:<22} {team['shared']['profile']:<18} bersama, @mention → thread")
    print(f"{team['admin']['channel_id']:<22} {team['admin']['profile']:<18} admin, tanpa @mention")
    print(f"{'(lainnya / DM)':<22} {'default':<18} lobby{' (dibatasi)' if team['lobby_restrict'] else ''}; "
          "di luar allowed_channels diabaikan, DM ditolak")
    print()
    skills = team["team_skills_dir"] or "(tidak ada)"
    print(f"Skill tim: {skills}; di channel pribadi: {'ya' if team['team_skills_in_personal'] else 'tidak'}")
    print("team.yaml valid.")


def cmd_apply(team: dict, hermes: Hermes, *, force_soul: bool) -> None:
    template = team["template"]
    note = SOUL_TEAM_SKILLS_NOTE if (team["team_skills_dir"] and team["team_skills_in_personal"]) else ""
    # Profile yang baru dibuat SELALU mendapat SOUL baru (clone ikut menyalin SOUL sumbernya);
    # profile yang sudah ada hanya ditimpa dengan --force-soul.
    print(f"\n[1/4] Template {template}")
    created = hermes.create(template, "default", alias=False)
    hermes.write_file(hermes.home(template) / "SOUL.md",
                      SOUL_MEMBER.format(display_name="{nama}", team_name=team["team_name"],
                                         team_skills_note=note), force=force_soul or created)
    hermes.config_set(template, profile_settings(team, "member"))
    hermes.park(template)

    print("\n[2/4] Profile anggota")
    for m in team["members"]:
        print(f" {m['profile']}")
        created = hermes.create(m["profile"], template)
        hermes.write_file(hermes.home(m["profile"]) / "SOUL.md",
                          SOUL_MEMBER.format(display_name=m["display_name"], team_name=team["team_name"],
                                             team_skills_note=note), force=force_soul or created)
        hermes.config_set(m["profile"], profile_settings(team, "member", m))

    print("\n[3/4] Profile bersama & admin")
    for key, soul in (("shared", SOUL_SHARED), ("admin", SOUL_ADMIN)):
        profile = team[key]["profile"]
        print(f" {profile}")
        created = hermes.create(profile, template)
        hermes.write_file(hermes.home(profile) / "SOUL.md", soul.format(team_name=team["team_name"]),
                          force=force_soul or created)
        hermes.config_set(profile, profile_settings(team, key))

    print("\n[4/4] Host gateway (profile default)")
    if team["lobby_restrict"]:
        hermes.write_file(hermes.home("default") / "SOUL.md", SOUL_LOBBY, force=True)
    hermes.config_set("default", host_settings(team))

    known = {m["profile"] for m in team["members"]} | {template}
    profiles_dir = hermes.root / "profiles"
    stale = sorted(d.name for d in profiles_dir.iterdir()
                   if d.is_dir() and team["prefix"] and d.name.startswith(team["prefix"])
                   and d.name not in known) if profiles_dir.is_dir() else []

    print("\nSelesai." if not hermes.failures else f"\nSelesai dengan {len(hermes.failures)} kegagalan:")
    for failure in hermes.failures:
        print(f"  ✗ {failure}")
    if stale:
        print(f"Profile berawalan sama yang tidak ada di team.yaml: {', '.join(stale)} "
              "(hapus dengan `hermes profile delete <nama>` kalau memang sudah keluar)")
    print("\nLangkah berikutnya:")
    print("  python3 hermes-team.py managed > hermes-managed.yaml")
    print("  sudo install -D -m 0644 -o root -g root hermes-managed.yaml /etc/hermes/config.yaml")
    print("  hermes doctor && hermes gateway restart && hermes gateway status")
    if hermes.failures:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check", "apply", "host-yaml", "managed"])
    parser.add_argument("-c", "--config", default="team.yaml", help="file roster (default: team.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="tampilkan perintah tanpa menjalankannya")
    parser.add_argument("--force-soul", action="store_true", help="timpa SOUL.md yang sudah ada (backup .bak)")
    parser.add_argument("--hermes-bin", default=shutil.which("hermes") or "hermes")
    parser.add_argument("--hermes-root", default=os.path.expanduser("~/.hermes"),
                        help="home profile default (default: ~/.hermes)")
    parser.add_argument("--allow-root", action="store_true",
                        help="izinkan jalan sebagai root (hanya kalau Hermes memang dipasang di user root)")
    args = parser.parse_args()

    try:
        team = load_team(Path(args.config))
    except TeamError as exc:
        sys.exit(f"team.yaml: {exc}")

    if args.command == "check":
        cmd_check(team)
    elif args.command == "host-yaml":
        print(nested_yaml(host_settings(team)), end="")
    elif args.command == "managed":
        print(managed_yaml(team), end="")
    else:
        if os.geteuid() == 0 and not args.dry_run and not args.allow_root:
            sys.exit("Jangan jalankan sebagai root; pakai user Linux yang menjalankan Hermes.")
        if not args.dry_run and not shutil.which(args.hermes_bin) and not Path(args.hermes_bin).exists():
            sys.exit(f"CLI hermes tidak ditemukan ({args.hermes_bin}). Pasang Hermes dulu atau pakai --hermes-bin.")
        cmd_apply(team, Hermes(args.hermes_bin, Path(args.hermes_root), args.dry_run), force_soul=args.force_soul)


if __name__ == "__main__":
    main()
