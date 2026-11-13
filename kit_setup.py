#!/usr/bin/env python3
"""Wizard pemasangan kit tim Hermes: dari server kosong sampai gateway jalan.

    sudo bash setup.sh                      # interaktif; bisa dihentikan dan dilanjutkan kapan saja
    sudo bash setup.sh --list               # daftar langkah dan statusnya
    sudo bash setup.sh --dry-run            # hanya menampilkan rencana, tidak mengubah apa pun
    sudo bash setup.sh --from llm           # ulangi mulai satu langkah
    sudo bash setup.sh --only verify        # satu langkah saja
    sudo HERMES_SETUP_BOT_TOKEN=... HERMES_SETUP_LLM_KEY=... \\
        bash setup.sh --non-interactive --answers jawaban.yaml

Wizard ini mengotomatiskan "Instalasi manual" (docs/INSTALL.md). Yang tetap manual: membuat
aplikasi bot di Discord Developer Portal dan mengklik tautan undangan (Discord tidak menyediakan
API untuk keduanya); wizard memberi instruksi lalu memeriksa hasilnya lewat API.

Rahasia (token bot, API key LLM) dibaca dengan getpass atau dari env HERMES_SETUP_BOT_TOKEN /
HERMES_SETUP_LLM_KEY. Keduanya langsung ditulis ke berkas tujuan akhirnya (~hermes/.hermes/.env dan
/etc/hermes/.env) dan TIDAK PERNAH masuk berkas state, argv/env proses anak, atau layar.

STATUS VERIFIKASI — baca sebelum mempercayai wizard ini di server sungguhan
----------------------------------------------------------------------------
Wizard ini BELUM PERNAH dijalankan utuh di server sungguhan. tests/test_setup.py mengujinya dengan
runner perintah tiruan, prompter tiruan, direktori sementara, dan server Discord/LLM tiruan lokal.
  Diuji terhadap Discord TIRUAN (bentuk request benar menurut dokumentasi, perilaku Discord asli
  belum dibuktikan):
    4 discord_bot     /users/@me, /oauth2/applications/@me (bit flags intent), /users/@me/guilds
    5 discord_server  cari-atau-buat role/kategori/channel, overwrite, hierarki role, pencarian admin
    7 options         pembuatan channel laporan
  Diuji terhadap endpoint LLM TIRUAN:
    6 llm             GET /models dan POST /chat/completions
  Hanya diuji terhadap RUNNER TIRUAN (perintahnya dirangkai dan dicatat, tidak pernah dieksekusi):
    1 preflight       timedatectl
    2 packages        apt-get, skrip get.docker.com
    3 linux_user      adduser, usermod, loginctl enable-linger, docker run hello-world
    8 hermes_install  installer Hermes (--non-interactive), hermes --version, hermes doctor
    9 kit_install     chown/chmod (penyalinan berkasnya sendiri diuji sungguhan di tmp)
   10 write_config    hermes_team.py check (team.yaml yang dihasilkan diuji dengan load_team asli)
   11 provision       hermes_team.py apply / managed
   12 extras          docker build, systemctl --user
   13 gateway         hermes gateway install/start/status, hermes logs
   14 verify          kit_doctor.py --discord, tests/selftest.sh
  Tidak diuji sama sekali: runuser + `env -i` di sistem sungguhan, perilaku systemd/linger, apt.

Hanya stdlib + PyYAML; mengimpor kit_base/kit_discord, tidak mengimpor hermes_team.
"""
from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from kit_base import ID_RE, KIT_VERSION, TeamError, parse_env_text, rewrite_env_text
from kit_discord import BOT_ALLOW, MEMBER_ALLOW, PERM, DiscordAPI, DiscordHTTPError, permission_bits, permission_names

STATE_FILE = Path("/var/lib/hermes-team-setup/state.json")
ENV_BOT_TOKEN = "HERMES_SETUP_BOT_TOKEN"
ENV_LLM_KEY = "HERMES_SETUP_LLM_KEY"
DOCKER_SCRIPT_URL = "https://get.docker.com"
HERMES_INSTALL_URL = "https://hermes-agent.nousresearch.com/install.sh"
DISCORD_PROBE_URL = "https://discord.com/api/v10/gateway"
PORTAL_URL = "https://discord.com/developers/applications"
RESUME_HINT = "sudo bash setup.sh"

# --------------------------------------------------------------------------- Discord: izin & intent
# Izin bot yang diminta lewat tautan undangan (docs/INSTALL.md Langkah 3). Nama = kunci kit_discord.PERMISSIONS.
INVITE_PERMISSIONS = (
    "view_channel", "send_messages", "send_messages_in_threads", "read_message_history", "embed_links",
    "attach_files", "add_reactions", "create_public_threads", "use_application_commands",
    "manage_roles", "manage_channels", "view_audit_log", "manage_messages", "manage_threads",
)
MODERATION_PERMISSIONS = ("moderate_members", "kick_members")
# Bit `flags` aplikasi (Discord: Application Flags).
FLAG_MEMBERS = (1 << 14) | (1 << 15)      # GATEWAY_GUILD_MEMBERS | GATEWAY_GUILD_MEMBERS_LIMITED
FLAG_CONTENT = (1 << 18) | (1 << 19)      # GATEWAY_MESSAGE_CONTENT | GATEWAY_MESSAGE_CONTENT_LIMITED
USAGE_ADMIN_ALLOW = PERM["VIEW_CHANNEL"] | PERM["READ_MESSAGE_HISTORY"]


def invite_permissions(moderation: bool = False) -> int:
    return permission_bits(INVITE_PERMISSIONS + (MODERATION_PERMISSIONS if moderation else ()))


def invite_url(app_id: str, moderation: bool = False) -> str:
    return ("https://discord.com/oauth2/authorize?client_id=%s&scope=bot+applications.commands&permissions=%d"
            % (urllib.parse.quote(str(app_id)), invite_permissions(moderation)))


def intent_status(flags: Any) -> Dict[str, bool]:
    try:
        value = int(flags or 0)
    except (TypeError, ValueError):
        value = 0
    return {"members": bool(value & FLAG_MEMBERS), "content": bool(value & FLAG_CONTENT)}


def private_channel_overwrites(guild_id: str, role_id: str, bot_id: str, role_allow: int = MEMBER_ALLOW) -> List[dict]:
    """@everyone tidak melihat; satu role boleh; bot boleh. Urutan = urutan aman saat menyetel channel
    yang sudah ada (bot dulu, @everyone terakhir, supaya bot tidak mengunci dirinya sendiri)."""
    return [
        {"id": str(bot_id), "type": 1, "allow": str(BOT_ALLOW), "deny": "0"},
        {"id": str(role_id), "type": 0, "allow": str(role_allow), "deny": "0"},
        {"id": str(guild_id), "type": 0, "allow": "0", "deny": str(PERM["VIEW_CHANNEL"])},
    ]


# --------------------------------------------------------------------------- galat & hasil perintah
class StepError(Exception):
    """Kegagalan yang sudah punya penjelasan untuk operator."""

    def __init__(self, message: str, cause: str = "", fix: str = ""):
        super().__init__(message)
        self.message, self.cause, self.fix = message, cause, fix


class Result:
    def __init__(self, rc: int, out: str = "", err: str = ""):
        self.rc, self.out, self.err = rc, out or "", err or ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


class UserInfo:
    def __init__(self, name: str, uid: int, gid: int, home: str):
        self.name, self.uid, self.gid, self.home = name, uid, gid, str(home)


def user_command(info: UserInfo, argv: List[str], extra_env: Optional[Dict[str, str]] = None) -> List[str]:
    """SATU-SATUNYA cara wizard menjalankan perintah sebagai user Hermes: runuser + lingkungan bersih
    (tanpa env root, jadi HERMES_SETUP_* tidak bocor), dengan XDG_RUNTIME_DIR untuk `systemctl --user`.
    Nilai `extra_env` ikut di argv (terlihat di `ps`) — jangan pernah berisi rahasia."""
    env = {
        "HOME": info.home, "USER": info.name, "LOGNAME": info.name, "SHELL": "/bin/bash", "LANG": "C.UTF-8",
        "PATH": f"{info.home}/.local/bin:{info.home}/.hermes/hermes-agent/.hermes/bin:"
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "XDG_RUNTIME_DIR": f"/run/user/{info.uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{info.uid}/bus",
    }
    env.update(extra_env or {})
    return ["runuser", "-u", info.name, "--", "env", "-i"] + [f"{k}={v}" for k, v in env.items()] + list(argv)


# --------------------------------------------------------------------------- akses ke mesin (bisa diganti di uji)
class HostSystem:
    """Semua sentuhan ke mesin di luar berkas milik wizard. Uji menggantinya dengan tiruan."""

    def euid(self) -> int:
        return os.geteuid()

    def run(self, argv: List[str], env: Optional[Dict[str, str]] = None, input: Optional[str] = None,
            cwd: Optional[str] = None, stream: bool = False, timeout: Optional[float] = None) -> Result:
        pipe = None if stream else subprocess.PIPE
        try:
            proc = subprocess.run(argv, env=env, input=input, cwd=cwd, stdout=pipe, stderr=pipe, text=True,
                                  timeout=timeout, stdin=None if input is not None else subprocess.DEVNULL)
        except FileNotFoundError:
            return Result(127, "", f"perintah tidak ditemukan: {argv[0]}")
        except subprocess.TimeoutExpired:
            return Result(124, "", f"melewati batas waktu {timeout} detik")
        return Result(proc.returncode, proc.stdout or "", proc.stderr or "")

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def user(self, name: str) -> Optional[UserInfo]:
        import pwd
        try:
            row = pwd.getpwnam(name)
        except KeyError:
            return None
        return UserInfo(row.pw_name, row.pw_uid, row.pw_gid, row.pw_dir)

    def user_groups(self, name: str) -> set:
        import grp
        return {g.gr_name for g in grp.getgrall() if name in g.gr_mem}

    def chown(self, path: Path, user: str, group: str) -> None:
        shutil.chown(str(path), user=user, group=group)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def timestamp(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S")

    def os_release(self) -> Dict[str, str]:
        try:
            return parse_env_text(Path("/etc/os-release").read_text(encoding="utf-8"))
        except OSError:
            return {}

    def machine(self) -> str:
        return platform.machine()

    def mem_total_gb(self) -> Optional[float]:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 * 1024)
        except (OSError, ValueError, IndexError):
            pass
        return None

    def disk_free_gb(self, path: str = "/") -> Optional[float]:
        try:
            return shutil.disk_usage(path).free / (1024 ** 3)
        except OSError:
            return None

    def has_systemd(self) -> bool:
        return Path("/run/systemd/system").is_dir()

    def system_timezone(self) -> str:
        try:
            name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
            if name:
                return name
        except OSError:
            pass
        real = os.path.realpath("/etc/localtime")
        return real.split("zoneinfo/", 1)[1] if "zoneinfo/" in real else ""

    def net_check(self, url: str) -> Tuple[bool, str]:
        """Terjangkau = server menjawab HTTP apa pun (termasuk 4xx)."""
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10) as resp:
                return True, f"HTTP {getattr(resp, 'status', 200)}"
        except urllib.error.HTTPError as exc:
            return True, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 — apa pun di sini berarti "tidak terjangkau"
            return False, f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"

    def http_json(self, method: str, url: str, headers: Dict[str, str], body: Any = None,
                  timeout: float = 30.0) -> Tuple[int, Any]:
        """(status, isi). Status 0 = tidak terjangkau. Isi = JSON bila bisa, kalau tidak potongan teks."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in headers.items():
            req.add_header(key, value)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, raw = getattr(resp, "status", 200), resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except Exception as exc:  # noqa: BLE001
            return 0, f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw[:300].decode("utf-8", "replace")


# --------------------------------------------------------------------------- state
class State:
    """Langkah selesai + jawaban non-rahasia + ID yang dikumpulkan. Berkas 0600 milik root."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.completed: List[str] = []
        self.answers: Dict[str, Any] = {}
        self.data: Dict[str, Any] = {}
        self.readable = True

    def load(self) -> "State":
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self
        except PermissionError:
            self.readable = False
            return self
        except (OSError, ValueError) as exc:
            raise StepError(f"berkas state {self.path} rusak: {exc}",
                            fix="perbaiki berkasnya, atau mulai ulang dengan --reset")
        if isinstance(raw, dict):
            self.completed = [str(s) for s in raw.get("completed") or []]
            self.answers = dict(raw.get("answers") or {})
            self.data = dict(raw.get("data") or {})
        return self

    def save(self, secrets: Tuple[str, ...] = ()) -> None:
        text = json.dumps({"version": 1, "kit_version": KIT_VERSION, "completed": self.completed,
                           "answers": self.answers, "data": self.data}, indent=2, ensure_ascii=False) + "\n"
        if any(s and s in text for s in secrets):
            raise RuntimeError("bug wizard: nilai rahasia hampir tertulis ke berkas state; penulisan dibatalkan")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        _atomic_write(self.path, text, 0o600)

    def done(self, name: str) -> bool:
        return name in self.completed

    def mark(self, name: str) -> None:
        if name not in self.completed:
            self.completed.append(name)

    def unmark(self, name: str) -> None:
        if name in self.completed:
            self.completed.remove(name)

    def reset(self) -> None:
        self.completed, self.answers, self.data = [], {}, {}
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write(path: Path, text: str, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- jawaban & prompt
SECRET_KEY_RE = re.compile(r"(token|secret|password|passwd|passphrase|credential|api[_\-]?key|private[_\-]?key|"
                           r"llm[_\-]?key|bearer|authorization)", re.I)
SECRET_KEY_ALLOWED = {"llm_key_env"}       # NAMA variabel env, bukan nilainya
ANSWER_KEYS = {
    "install_docker", "moderation", "guild_id", "invite_wait_seconds", "on_missing_intents",
    "on_missing_permissions", "on_role_hierarchy", "role_entry", "role_member", "role_admin", "category_name",
    "shared_channel", "admin_channel", "admins", "llm_base_url", "llm_key_env", "llm_model_normal",
    "llm_model_simple", "llm_on_failure", "team_name", "timezone", "threads_personal", "usage_reports",
    "usage_channel", "report_daily", "report_weekly", "report_monthly", "install_hermes",
    "overwrite_team_yaml", "build_sandbox", "sandbox_tag", "usage_timer", "report_timer", "run_selftest",
}
TRUE_WORDS = {"y", "ya", "yes", "true", "1", "iya", "on"}
FALSE_WORDS = {"t", "tidak", "n", "no", "false", "0", "off"}


def find_secret_keys(data: Any, trail: str = "") -> List[str]:
    found: List[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            label = f"{trail}{key}"
            if str(key) not in SECRET_KEY_ALLOWED and SECRET_KEY_RE.search(str(key)):
                found.append(label)
            found += find_secret_keys(value, label + ".")
    elif isinstance(data, list):
        for item in data:
            found += find_secret_keys(item, trail)
    return found


def load_answers(path: Path) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise StepError(f"berkas jawaban {path} tidak bisa dibaca: {exc}")
    if not isinstance(data, dict):
        raise StepError(f"berkas jawaban {path}: isinya harus mapping kunci → nilai")
    leaked = find_secret_keys(data)
    if leaked:
        raise StepError(
            f"berkas jawaban {path} memuat kunci yang terlihat seperti rahasia: {', '.join(leaked)}",
            cause="rahasia tidak boleh ditaruh di berkas jawaban",
            fix=f"hapus kunci itu; berikan token bot lewat env {ENV_BOT_TOKEN} dan API key LLM lewat {ENV_LLM_KEY}")
    unknown = sorted(str(k) for k in data if str(k) not in ANSWER_KEYS)
    if unknown:
        raise StepError(f"berkas jawaban {path}: kunci tidak dikenal: {', '.join(unknown)}",
                        fix="kunci yang dikenal: " + ", ".join(sorted(ANSWER_KEYS)))
    return data


class Prompter:
    """Satu-satunya pintu pertanyaan ke operator. Urutan sumber jawaban: berkas --answers → (bila
    --non-interactive / --yes) jawaban tersimpan atau bawaan → tanya di terminal."""

    def __init__(self, answers: Optional[Dict[str, Any]] = None, saved: Optional[Dict[str, Any]] = None,
                 non_interactive: bool = False, assume_yes: bool = False,
                 out: Callable[[str], None] = print, input_fn: Callable[[str], str] = input,
                 getpass_fn: Callable[[str], str] = getpass.getpass, environ: Optional[Dict[str, str]] = None):
        self.answers = dict(answers or {})
        self.saved = saved if saved is not None else {}
        self.non_interactive, self.assume_yes = non_interactive, assume_yes
        self.out, self.input_fn, self.getpass_fn = out, input_fn, getpass_fn
        self.environ = os.environ if environ is None else environ

    def _read(self, prompt: str) -> str:
        try:
            return self.input_fn(prompt)
        except EOFError:
            raise StepError("masukan terminal habis (EOF) saat wizard menunggu jawaban",
                            fix="jalankan di terminal interaktif, atau pakai --non-interactive --answers <berkas>")

    def ask(self, key: str, question: str, default: Optional[str] = None,
            validate: Optional[Callable[[str], Optional[str]]] = None, reask: bool = False) -> str:
        """Teks. default None = wajib diisi; default "" = boleh kosong (ketik '-' untuk mengosongkan)."""
        if key in self.saved and self.saved[key] is not None:
            default = str(self.saved[key])
        if key in self.answers and not reask:
            raw = self.answers[key]
            value = ",".join(str(v) for v in raw) if isinstance(raw, (list, tuple)) else str(raw if raw is not None else "")
            value = value.strip()
            problem = validate(value) if validate else None
            if problem:
                raise StepError(f"jawaban '{key}' di berkas --answers tidak valid: {problem}")
            self.saved[key] = value
            return value
        if self.non_interactive or (self.assume_yes and default is not None and not reask):
            if default is None:
                raise StepError(f"jawaban '{key}' wajib ada ({question})",
                                fix=f"tambahkan `{key}: ...` ke berkas --answers")
            problem = validate(default) if validate else None
            if problem:
                raise StepError(f"nilai bawaan '{key}' ({default}) tidak valid: {problem}",
                                fix=f"tambahkan `{key}: ...` ke berkas --answers")
            self.saved[key] = default
            return default
        while True:
            hint = f" [{default}]" if default else (" [kosong]" if default == "" else "")
            raw = self._read(f"  {question}{hint}: ").strip()
            if raw == "-" and default is not None:
                raw = ""
            elif not raw and default is not None:
                raw = default
            if not raw and default is None:
                self.out("    Wajib diisi.")
                continue
            problem = validate(raw) if validate else None
            if problem:
                self.out(f"    {problem}")
                continue
            self.saved[key] = raw
            return raw

    @staticmethod
    def _to_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        return True if text in TRUE_WORDS else False if text in FALSE_WORDS else None

    def confirm(self, key: str, question: str, default: Optional[bool] = None) -> bool:
        """Ya/tidak. default None = persetujuan eksplisit: --yes TIDAK menjawabnya, dan mode
        --non-interactive menuntut kuncinya ada di berkas --answers."""
        explicit = default is None
        if not explicit and isinstance(self.saved.get(key), bool):
            default = self.saved[key]
        if key in self.answers:
            value = self._to_bool(self.answers[key])
            if value is None:
                raise StepError(f"jawaban '{key}' di berkas --answers harus true/false")
            self.saved[key] = value
            return value
        if self.non_interactive:
            if explicit:
                raise StepError(f"langkah ini butuh persetujuan eksplisit '{key}' ({question})",
                                fix=f"tambahkan `{key}: true` (atau false) ke berkas --answers")
            self.saved[key] = bool(default)
            return bool(default)
        if self.assume_yes and not explicit:
            self.saved[key] = bool(default)
            return bool(default)
        hint = "[y/t]" if default is None else "[Y/t]" if default else "[y/T]"
        while True:
            raw = self._read(f"  {question} {hint}: ").strip()
            value = default if (not raw and default is not None) else self._to_bool(raw) if raw else None
            if value is None:
                self.out("    Jawab y (ya) atau t (tidak).")
                continue
            self.saved[key] = value
            return value

    def choose(self, key: str, question: str, options: List[Tuple[str, str]], default: Optional[str] = None,
               record: bool = True) -> str:
        values = [v for v, _ in options]
        if key in self.answers:
            value = str(self.answers[key])
            if value not in values:
                raise StepError(f"jawaban '{key}' di berkas --answers bukan salah satu pilihan: {', '.join(values)}")
        elif self.non_interactive or (self.assume_yes and default is not None):
            if default is None:
                raise StepError(f"jawaban '{key}' wajib ada ({question})",
                                fix=f"tambahkan `{key}: ...` ke berkas --answers; pilihan: {', '.join(values)}")
            value = default
        else:
            self.out(f"  {question}")
            for i, (_v, label) in enumerate(options, 1):
                self.out(f"    {i}. {label}")
            while True:
                raw = self._read("  Nomor pilihan" + (f" [{values.index(default) + 1}]" if default in values else "") + ": ").strip()
                if not raw and default in values:
                    value = default
                    break
                if raw.isdigit() and 1 <= int(raw) <= len(options):
                    value = values[int(raw) - 1]
                    break
                self.out(f"    Ketik angka 1–{len(options)}.")
        if record:
            self.saved[key] = value
        return value

    def secret(self, key: str, question: str, env_var: str) -> str:
        """Rahasia: dari env (untuk mode otomatis) atau getpass. Tidak pernah disimpan atau dicetak."""
        value = str(self.environ.get(env_var) or "").strip()
        if value:
            return value
        if self.non_interactive:
            raise StepError(f"{key}: env {env_var} belum diisi",
                            fix=f"jalankan dengan env {env_var}=... (jangan taruh di berkas --answers)")
        while True:
            try:
                value = (self.getpass_fn(f"  {question} (tidak tampil saat diketik): ") or "").strip()
            except EOFError:
                raise StepError("masukan terminal habis (EOF) saat wizard menunggu rahasia")
            if value:
                return value
            self.out("    Wajib diisi.")

    def pause(self, message: str) -> None:
        if not (self.non_interactive or self.assume_yes):
            self._read(f"  {message} — tekan Enter untuk lanjut ")


# --------------------------------------------------------------------------- konteks
class Context:
    def __init__(self, system: HostSystem, state: State, prompter: Prompter, *, user: str = "hermes",
                 kit_dir: Path = Path("/opt/hermes-team"), source_dir: Optional[Path] = None,
                 etc_dir: Path = Path("/etc/hermes"), linger_dir: Path = Path("/var/lib/systemd/linger"),
                 dry_run: bool = False, out: Callable[[str], None] = print):
        self.sys, self.state, self.prompt = system, state, prompter
        self.user_name = user
        self.kit_dir = Path(kit_dir)
        self.source_dir = Path(source_dir) if source_dir else Path(__file__).resolve().parent
        self.etc_dir, self.linger_dir = Path(etc_dir), Path(linger_dir)
        self.dry_run = dry_run
        self._out = out
        self.secrets: List[str] = []
        self.warnings: List[str] = []
        self._token: Optional[str] = None
        self._llm_key: Optional[str] = None
        self._api: Optional[DiscordAPI] = None
        self.poll_interval = 5.0

    # ---- keluaran
    def redact(self, text: Any) -> str:
        text = str(text)
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "«rahasia»")
        return text

    def say(self, text: str = "") -> None:
        self._out(self.redact(text))

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        self.say(f"  ! {text}")

    def add_secret(self, value: str) -> None:
        if value and value not in self.secrets:
            self.secrets.append(value)

    def save(self) -> None:
        if not self.dry_run:
            self.state.save(tuple(self.secrets))

    @property
    def data(self) -> Dict[str, Any]:
        return self.state.data

    # ---- user & path
    def user(self) -> UserInfo:
        info = self.sys.user(self.user_name)
        if info is None:
            raise StepError(f"user Linux '{self.user_name}' belum ada",
                            fix=f"jalankan langkah linux_user dulu: {RESUME_HINT} --from linux_user")
        return info

    def home(self) -> Optional[Path]:
        info = self.sys.user(self.user_name)
        return Path(info.home) if info else None

    def _home(self) -> Path:
        return Path(self.user().home)

    @property
    def hermes_env(self) -> Path:
        return self._home() / ".hermes" / ".env"

    @property
    def team_dir(self) -> Path:
        return self._home() / ".hermes-team"

    @property
    def team_file(self) -> Path:
        return self.team_dir / "team.yaml"

    @property
    def llm_env(self) -> Path:
        return self.etc_dir / ".env"

    @property
    def managed_file(self) -> Path:
        return self.etc_dir / "config.yaml"

    def hermes_bin(self) -> Path:
        """Launcher Hermes: ~/.local/bin/hermes, atau yang dibuat installer di dalam checkout-nya."""
        home = self._home()
        candidates = [home / ".local" / "bin" / "hermes", home / ".hermes" / "hermes-agent" / ".hermes" / "bin" / "hermes"]
        return next((p for p in candidates if p.exists()), candidates[0])

    # ---- berkas
    def write_file(self, path: Path, text: str, mode: int, owner: str = "root", group: str = "root") -> None:
        path = Path(path)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            self.sys.chown(tmp, owner, group)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def make_dir(self, path: Path, mode: int, owner: str = "root", group: str = "root") -> None:
        path = Path(path)
        if not path.is_dir():
            path.mkdir(parents=True)
            self.sys.chown(path, owner, group)
            os.chmod(path, mode)

    def set_env_value(self, path: Path, key: str, value: str, mode: int, owner: str, group: str) -> None:
        """Tulis satu baris KEY=value ke berkas .env (baris lain dipertahankan), langsung dari memori."""
        try:
            old = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            old = ""
        self.write_file(path, rewrite_env_text(old, key, value), mode, owner, group)

    @staticmethod
    def read_env_value(path: Path, key: str) -> str:
        try:
            return parse_env_text(Path(path).read_text(encoding="utf-8")).get(key, "")
        except OSError:
            return ""

    # ---- perintah
    def run(self, argv: List[str], *, as_user: bool = False, check: bool = True, stream: bool = False,
            input: Optional[str] = None, extra_env: Optional[Dict[str, str]] = None,
            timeout: Optional[float] = None, what: str = "", fix: str = "") -> Result:
        """Satu-satunya pintu shell-out. Menolak rahasia di argv/env, tidak pernah jalan saat --dry-run."""
        if self.dry_run:
            raise RuntimeError("bug wizard: perintah dijalankan saat --dry-run")
        argv = [str(a) for a in argv]
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_SETUP_")}
        cwd = None
        shown = " ".join(argv)
        if as_user:
            info = self.user()
            argv, cwd = user_command(info, argv, extra_env), info.home
            shown = f"(sebagai {info.name}) {shown}"
        elif extra_env:
            env.update(extra_env)
        blob = "\0".join(argv) + "\0" + "\0".join(f"{k}={v}" for k, v in env.items())
        if any(s and s in blob for s in self.secrets):
            raise RuntimeError("bug wizard: nilai rahasia hampir diteruskan ke argv/env proses anak; dibatalkan")
        self.say(f"  $ {shown}")
        result = self.sys.run(argv, env=env, input=input, cwd=cwd, stream=stream, timeout=timeout)
        if check and not result.ok:
            tail = "\n".join((result.err.strip() or result.out.strip()).splitlines()[-15:])
            raise StepError(f"{what or 'perintah'} gagal (kode keluar {result.rc}): {shown}", cause=tail, fix=fix)
        return result

    # ---- rahasia (malas: memori → berkas tujuan akhir → env/getpass)
    def bot_token(self, ask: bool = True) -> str:
        if not self._token:
            token = self.read_env_value(self.hermes_env, "DISCORD_BOT_TOKEN") if self.home() else ""
            if not token and ask:
                token = self.prompt.secret("token bot", "Token bot Discord", ENV_BOT_TOKEN)
            self.set_token(token)
        return self._token or ""

    def set_token(self, token: str) -> None:
        self._token, self._api = token or None, None
        self.add_secret(token)

    def discord(self) -> DiscordAPI:
        if self._api is None:
            self._api = DiscordAPI(self.bot_token(), sleep=self.sys.sleep)
        return self._api

    def llm_key(self, key_env: str, ask: bool = True, fresh: bool = False) -> str:
        if fresh:
            self._llm_key = None
        if not self._llm_key:
            key = "" if fresh else self.read_env_value(self.llm_env, key_env)
            if not key and ask:
                key = self.prompt.secret("API key LLM", f"API key LLM ({key_env})", ENV_LLM_KEY)
            self._llm_key = key or None
            self.add_secret(key)
        return self._llm_key or ""

    # ---- keputusan saat ada masalah yang bisa diperbaiki operator
    def resolve(self, key: str, problem: str, fix: str, allow_skip: bool = True) -> str:
        """'retry' atau 'skip'. Non-interaktif: hanya `<key>: skip` di --answers yang melewati;
        selain itu gagal dengan penjelasan (tidak pernah berputar tanpa henti)."""
        self.say(f"  ✗ {problem}")
        for line in fix.splitlines():
            self.say(f"    → {line}")
        if self.prompt.non_interactive or key in self.prompt.answers:
            if allow_skip and str(self.prompt.answers.get(key, "")) == "skip":
                self.warn(f"dilewati atas permintaan ({key}: skip): {problem}")
                return "skip"
            raise StepError(problem, fix=fix)
        options = [("retry", "Sudah saya perbaiki — periksa lagi")]
        if allow_skip:
            options.append(("skip", "Lewati pemeriksaan ini (risiko sendiri)"))
        options.append(("abort", "Berhenti dulu (bisa dilanjutkan nanti)"))
        choice = self.prompt.choose(key, "Apa selanjutnya?", options, record=False)
        if choice == "abort":
            raise StepError(problem, fix=fix)
        if choice == "skip":
            self.warn(f"dilewati: {problem}")
        return choice


# --------------------------------------------------------------------------- langkah
class Step:
    name = ""
    title = ""
    explain = ""

    def check(self, ctx: Context) -> bool:
        """Sudah selesai? Tidak boleh menjalankan perintah atau menyentuh jaringan (dipakai --list/--dry-run)."""
        return ctx.state.done(self.name)

    def plan(self, ctx: Context) -> List[str]:
        return []

    def run(self, ctx: Context) -> None:
        raise NotImplementedError


class Preflight(Step):
    name, title = "preflight", "Pemeriksaan awal server"
    explain = "Memeriksa OS, arsitektur, RAM, disk, jaringan, dan jam. Tidak mengubah apa pun."

    def plan(self, ctx):
        return ["baca /etc/os-release, RAM, dan ruang disk",
                f"coba hubungi {DISCORD_PROBE_URL} dan {HERMES_INSTALL_URL}",
                "jalankan: timedatectl show -p NTPSynchronized --value"]

    def run(self, ctx):
        rel = ctx.sys.os_release()
        family = f"{rel.get('ID', '')} {rel.get('ID_LIKE', '')}".lower()
        label = rel.get("PRETTY_NAME") or rel.get("ID") or "tidak dikenal"
        if "debian" in family or "ubuntu" in family:
            ctx.say(f"  ✓ OS: {label}")
        else:
            ctx.warn(f"OS '{label}' bukan Debian/Ubuntu; wizard memakai apt-get dan adduser, mungkin gagal")
        machine = ctx.sys.machine()
        if machine in ("x86_64", "amd64", "aarch64", "arm64"):
            ctx.say(f"  ✓ Arsitektur: {machine}")
        else:
            ctx.warn(f"arsitektur {machine} belum pernah dicoba untuk kit ini")
        ram, free = ctx.sys.mem_total_gb(), ctx.sys.disk_free_gb("/")
        if ram is None:
            ctx.warn("RAM tidak bisa dibaca")
        elif ram < 7.5:      # mesin "8 GB" melapor sedikit di bawah 8
            ctx.warn(f"RAM {ram:.1f} GB (di bawah 8 GB): cukup untuk mencoba, kurang untuk tim (docs/SKALA.md: ±32 GB untuk 20 orang)")
        else:
            ctx.say(f"  ✓ RAM: {ram:.1f} GB")
        if free is None:
            ctx.warn("ruang disk tidak bisa dibaca")
        elif free < 20:
            ctx.warn(f"ruang disk kosong {free:.1f} GB (di bawah 20 GB): image sandbox dan Docker butuh ruang")
        else:
            ctx.say(f"  ✓ Disk kosong: {free:.1f} GB")
        if not ctx.sys.has_systemd():
            ctx.warn("systemd tidak terdeteksi: layanan gateway dan timer (langkah 12–13) tidak akan bisa dipasang")
        ok, detail = ctx.sys.net_check(DISCORD_PROBE_URL)
        if not ok:
            raise StepError("discord.com tidak terjangkau dari server ini", cause=detail,
                            fix="periksa DNS/firewall/proxy keluar; bot butuh HTTPS ke discord.com dan gateway.discord.gg")
        ctx.say(f"  ✓ discord.com terjangkau ({detail})")
        ok, detail = ctx.sys.net_check(HERMES_INSTALL_URL)
        if ok:
            ctx.say(f"  ✓ host installer Hermes terjangkau ({detail})")
        else:
            ctx.warn(f"host installer Hermes tidak terjangkau ({detail}); langkah hermes_install akan gagal bila Hermes belum terpasang")
        if ctx.sys.which("timedatectl"):
            synced = ctx.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], check=False).out.strip()
            if synced == "yes":
                ctx.say("  ✓ Jam tersinkron (NTP)")
            else:
                ctx.warn("jam server belum tersinkron NTP; jam yang melenceng membuat TLS dan laporan pemakaian kacau "
                         "(perbaiki: timedatectl set-ntp true)")
        else:
            ctx.say("  · timedatectl tidak ada; pastikan jam server benar (NTP)")


class Packages(Step):
    name, title = "packages", "Paket sistem: Docker, git, PyYAML"
    explain = "Memasang git, curl, dan python3-yaml lewat apt bila belum ada; Docker lewat skrip resmi get.docker.com (dengan persetujuanmu)."
    APT = (("git", "git"), ("curl", "curl"))

    def _missing(self, ctx):
        return [pkg for binary, pkg in self.APT if not ctx.sys.which(binary)]

    def check(self, ctx):
        return not self._missing(ctx) and bool(ctx.sys.which("docker"))

    def plan(self, ctx):
        lines = []
        if self._missing(ctx):
            lines.append("apt-get update && apt-get install -y " + " ".join(self._missing(ctx) + ["ca-certificates"]))
        if not ctx.sys.which("docker"):
            lines.append(f"(setelah persetujuan) unduh {DOCKER_SCRIPT_URL} lalu jalankan dengan sh")
        return lines or ["tidak ada yang perlu dipasang"]

    def run(self, ctx):
        missing = self._missing(ctx)
        if missing:
            apt_env = {"DEBIAN_FRONTEND": "noninteractive"}
            fix = "periksa koneksi dan `apt-get update`; di OS non-Debian pasang git/curl secara manual lalu ulangi"
            ctx.run(["apt-get", "update"], extra_env=apt_env, stream=True, what="apt-get update", fix=fix)
            ctx.run(["apt-get", "install", "-y"] + missing + ["ca-certificates"], extra_env=apt_env, stream=True,
                    what="pemasangan paket", fix=fix)
        if not ctx.sys.which("docker"):
            ctx.say("  Docker belum ada. Cara pasang resmi tercepat adalah skrip get.docker.com, yang berjalan sebagai root:")
            ctx.say(f"    curl -fsSL {DOCKER_SCRIPT_URL} | sh")
            manual = "pasang Docker sendiri (https://docs.docker.com/engine/install/), lalu ulangi: " + RESUME_HINT
            if not ctx.prompt.confirm("install_docker", "Unduh dan jalankan skrip itu sekarang?", None):
                raise StepError("Docker wajib ada (sandbox tiap anggota berjalan di container)", fix=manual)
            script = ctx.state.path.parent / "get-docker.sh"
            ctx.run(["curl", "-fsSL", DOCKER_SCRIPT_URL, "-o", str(script)], what="unduh skrip Docker", fix=manual)
            ctx.run(["sh", str(script)], stream=True, what="pemasangan Docker", fix=manual)
            if not ctx.sys.which("docker"):
                raise StepError("skrip Docker selesai tetapi perintah `docker` tetap tidak ditemukan", fix=manual)
        ctx.run(["docker", "--version"], what="docker --version")


class LinuxUser(Step):
    name, title = "linux_user", "User Linux khusus untuk Hermes"
    explain = ("Membuat user tanpa kata sandi, memasukkannya ke grup docker, dan mengaktifkan linger supaya layanannya "
               "hidup tanpa login. Catatan: anggota grup docker setara root; user ini hanya dipakai untuk Hermes.")

    def _linger(self, ctx):
        return (ctx.linger_dir / ctx.user_name).exists()

    def check(self, ctx):
        return (ctx.sys.user(ctx.user_name) is not None and "docker" in ctx.sys.user_groups(ctx.user_name)
                and self._linger(ctx))

    def plan(self, ctx):
        u, lines = ctx.user_name, []
        if ctx.sys.user(u) is None:
            lines.append(f'adduser --disabled-password --gecos "" {u}')
        if "docker" not in ctx.sys.user_groups(u):
            lines.append(f"usermod -aG docker {u}")
        if not self._linger(ctx):
            lines.append(f"loginctl enable-linger {u}")
        return lines + [f"(uji) sebagai {u}: docker run --rm hello-world"]

    def run(self, ctx):
        u = ctx.user_name
        if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", u):
            raise StepError(f"nama user '{u}' tidak valid", fix="pakai huruf kecil/angka/-/_, mis. --user hermes")
        if u == "root":
            raise StepError("Hermes tidak boleh dipasang sebagai root", fix="pakai --user hermes")
        if ctx.sys.user(u) is None:
            if ctx.sys.which("adduser"):
                ctx.run(["adduser", "--disabled-password", "--gecos", "", u], what="pembuatan user")
            else:
                ctx.run(["useradd", "--create-home", "--shell", "/bin/bash", u], what="pembuatan user")
        if "docker" not in ctx.sys.user_groups(u):
            ctx.run(["usermod", "-aG", "docker", u], what="menambah user ke grup docker",
                    fix="grup `docker` dibuat saat Docker dipasang; ulangi langkah packages bila grupnya belum ada")
        if not self._linger(ctx):
            ctx.run(["loginctl", "enable-linger", u], what="loginctl enable-linger",
                    fix="butuh systemd-logind; tanpa linger layanan gateway mati saat tidak ada yang login")
        test = ctx.run(["docker", "run", "--rm", "hello-world"], as_user=True, check=False, timeout=180)
        if test.ok:
            ctx.say(f"  ✓ user {u} bisa menjalankan container")
        else:
            ctx.warn(f"`docker run hello-world` sebagai {u} gagal (kode {test.rc}); periksa `id {u}` dan `systemctl status docker`. "
                     "Tanpa ini perintah terminal di sandbox akan gagal.")


def describe_intents(status: Dict[str, bool]) -> List[str]:
    missing = []
    if not status["members"]:
        missing.append("Server Members Intent")
    if not status["content"]:
        missing.append("Message Content Intent")
    return missing


class DiscordBot(Step):
    name, title = "discord_bot", "Menyambungkan bot Discord"
    explain = ("Bagian manual: buat aplikasi bot di Developer Portal dan undang ke servermu. Wizard memeriksa token, "
               "intent, dan menunggu sampai bot benar-benar masuk server.")

    def check(self, ctx):
        return (ctx.state.done(self.name) and bool(ctx.data.get("guild_id")) and bool(ctx.data.get("app_id"))
                and ctx.home() is not None and bool(ctx.read_env_value(ctx.hermes_env, "DISCORD_BOT_TOKEN")))

    def plan(self, ctx):
        return ["tampilkan instruksi Developer Portal; baca token dengan getpass",
                "GET /users/@me dan /oauth2/applications/@me (periksa dua privileged intent)",
                f"tulis DISCORD_BOT_TOKEN ke ~{ctx.user_name}/.hermes/.env (0600)",
                "cetak tautan undangan; tunggu GET /users/@me/guilds berisi server"]

    def instructions(self, ctx):
        ctx.say(f"""  Kerjakan di browser — {PORTAL_URL}
    1. New Application → beri nama (mis. Hermes Backend).
    2. Tab Installation: Install Link → None, lalu Save.
       (Tanpa ini "Public Bot" tidak bisa dimatikan.)
    3. Tab Bot:
         Public Bot              : MATI
         Presence Intent         : MATI
         Server Members Intent   : HIDUP
         Message Content Intent  : HIDUP
       lalu Save.
    4. Tab Bot → Reset Token → salin tokennya (hanya tampil sekali).
       Jangan tempel token di chat/tiket/repo; tempel hanya di prompt di bawah.""")

    def run(self, ctx):
        info = ctx.user()
        api, me = None, None
        existing = ctx.bot_token(ask=False)
        if not existing:
            self.instructions(ctx)
        while True:
            token = existing or ctx.prompt.secret("token bot", "Tempel token bot", ENV_BOT_TOKEN)
            ctx.set_token(token)
            api = ctx.discord()
            try:
                me = api.request("GET", "/users/@me")
                break
            except DiscordHTTPError as exc:
                if exc.status != 401:
                    raise StepError("Discord menolak pemeriksaan token", cause=str(exc))
                problem = ("token yang tersimpan di ~/.hermes/.env ditolak Discord (401)" if existing
                           else "token ditolak Discord (401)")
                if ctx.prompt.non_interactive or ENV_BOT_TOKEN in ctx.prompt.environ and not existing:
                    raise StepError(problem, cause="token salah ketik, terpotong, atau sudah di-reset",
                                    fix=f"Developer Portal → Bot → Reset Token, lalu ulangi dengan env {ENV_BOT_TOKEN} yang baru")
                ctx.say(f"  ✗ {problem}. Reset Token di tab Bot, lalu tempel yang baru.")
                existing = ""
        ctx.say(f"  ✓ Token sah: bot «{me.get('username')}» (ID {me['id']})")
        ctx.data["bot_id"] = str(me["id"])
        ctx.make_dir(Path(info.home) / ".hermes", 0o700, info.name, info.name)
        ctx.set_env_value(ctx.hermes_env, "DISCORD_BOT_TOKEN", token, 0o600, info.name, info.name)
        ctx.say(f"  ✓ Token disimpan di {ctx.hermes_env} (0600, milik {info.name}); tidak disimpan di tempat lain")

        while True:
            app = api.request("GET", "/oauth2/applications/@me")
            missing = describe_intents(intent_status(app.get("flags")))
            if not missing:
                ctx.say("  ✓ Server Members Intent dan Message Content Intent hidup")
                break
            fix = (f"{PORTAL_URL} → aplikasi «{app.get('name')}» → tab Bot → Privileged Gateway Intents → "
                   f"hidupkan: {', '.join(missing)} → Save Changes")
            if ctx.resolve("on_missing_intents", f"intent belum hidup: {', '.join(missing)} "
                           "(gateway akan gagal start dengan PrivilegedIntentsRequired)", fix) == "skip":
                break
        ctx.data["app_id"], ctx.data["app_name"] = str(app["id"]), str(app.get("name") or "")
        if app.get("bot_public"):
            ctx.warn("Public Bot masih hidup: siapa pun yang tahu ID aplikasi bisa mengundang bot ini. "
                     "Matikan di tab Bot (set Installation → Install Link ke None dulu).")
        ctx.save()

        moderation = ctx.prompt.confirm(
            "moderation", "Izinkan tool moderasi (timeout & kick) di #hermes-admin? Menambah izin Moderate Members + Kick Members",
            False)
        url = invite_url(ctx.data["app_id"], moderation)
        ctx.data["invite_url"] = url
        guilds = api.request("GET", "/users/@me/guilds") or []
        if not guilds:
            ctx.say("  Undang bot ke servermu: buka tautan ini di browser (login sebagai pemilik/admin server),")
            ctx.say("  pilih server, lalu Authorize. Jangan tambah izin Administrator.")
            ctx.say(f"\n    {url}\n")
            guilds = self.wait_for_guild(ctx, api)
        guild = self.pick_guild(ctx, guilds)
        ctx.data["guild_id"], ctx.data["guild_name"] = str(guild["id"]), str(guild.get("name") or "")
        ctx.say(f"  ✓ Bot ada di server «{ctx.data['guild_name']}» (ID {ctx.data['guild_id']})")

    def wait_for_guild(self, ctx, api):
        try:
            limit = float(ctx.prompt.answers.get("invite_wait_seconds", 600))
        except (TypeError, ValueError):
            raise StepError("jawaban 'invite_wait_seconds' harus angka")
        waited = 0.0
        ctx.say("  Menunggu bot masuk server ...")
        while True:
            guilds = api.request("GET", "/users/@me/guilds") or []
            if guilds:
                return guilds
            if waited >= limit:
                ctx.resolve("invite_wait", f"bot belum masuk server mana pun setelah {int(waited)} detik",
                            "buka tautan undangan di atas, pilih server, Authorize (butuh izin Manage Server)",
                            allow_skip=False)
                waited = 0.0
                continue
            ctx.sys.sleep(ctx.poll_interval)
            waited += ctx.poll_interval

    def pick_guild(self, ctx, guilds):
        by_id = {str(g["id"]): g for g in guilds}
        wanted = str(ctx.prompt.answers.get("guild_id") or "")
        if len(by_id) == 1 and (not wanted or wanted in by_id):
            return guilds[0]
        if wanted and wanted not in by_id:
            raise StepError(f"bot tidak berada di server guild_id {wanted}",
                            fix="undang bot ke server itu, atau perbaiki guild_id di berkas --answers")
        choice = ctx.prompt.choose("guild_id", "Bot ada di beberapa server. Pakai yang mana?",
                                   [(gid, f"{g.get('name')} ({gid})") for gid, g in by_id.items()])
        return by_id[choice]


def _name_key(name: Any) -> str:
    return str(name or "").strip().casefold()


def find_role(roles: List[dict], name: str) -> Optional[dict]:
    hits = [r for r in roles if _name_key(r.get("name")) == _name_key(name) and not r.get("managed")]
    return hits[0] if hits else None


def ensure_role(ctx: Context, api: DiscordAPI, guild_id: str, roles: List[dict], name: str) -> Tuple[dict, bool]:
    role = find_role(roles, name)
    if role is not None:
        return role, False
    role = api.create_role(guild_id, {"name": name, "permissions": "0", "mentionable": False},
                           reason="Hermes setup: role tim")
    roles.append(role)
    return role, True


def ensure_category(api: DiscordAPI, guild_id: str, channels: List[dict], name: str) -> Tuple[dict, bool]:
    for ch in channels:
        if ch.get("type") == 4 and _name_key(ch.get("name")) == _name_key(name):
            return ch, False
    cat = api.create_channel(guild_id, {"name": name, "type": 4}, reason="Hermes setup: kategori")
    channels.append(cat)
    return cat, True


def ensure_text_channel(ctx: Context, api: DiscordAPI, guild_id: str, channels: List[dict], name: str,
                        parent_id: str, overwrites: List[dict], topic: str = "") -> Tuple[dict, bool]:
    """Cari channel teks bernama `name` (utamakan yang di kategori Hermes) atau buat. Channel yang sudah
    ada hanya disetel overwrite-nya yang berbeda; overwrite lain milik operator tidak disentuh."""
    hits = [c for c in channels if c.get("type") == 0 and _name_key(c.get("name")) == _name_key(name)]
    hits.sort(key=lambda c: str(c.get("parent_id") or "") != str(parent_id))
    if not hits:
        body = {"name": name, "type": 0, "parent_id": parent_id, "topic": topic[:1024],
                "permission_overwrites": overwrites}
        ch = api.create_channel(guild_id, body, reason=f"Hermes setup: #{name}")
        channels.append(ch)
        return ch, True
    ch = hits[0]
    if str(ch.get("parent_id") or "") != str(parent_id):
        ctx.say(f"  · #{name} sudah ada di luar kategori Hermes; dipakai apa adanya (tidak dipindah)")
    current = {str(o.get("id")): o for o in ch.get("permission_overwrites") or []}
    wanted_ids = set()
    for want in overwrites:
        wanted_ids.add(want["id"])
        have = current.get(want["id"])
        if have and int(have.get("allow") or 0) == int(want["allow"]) and int(have.get("deny") or 0) == int(want["deny"]):
            continue
        try:
            api.set_overwrite(ch["id"], want["id"], want["type"], int(want["allow"]), int(want["deny"]),
                              reason="Hermes setup: channel privat")
        except DiscordHTTPError as exc:
            if exc.status != 403:
                raise
            raise StepError(f"bot tidak boleh mengatur izin #{name} (403)",
                            cause="channel itu sudah privat dan bot tidak bisa melihat/mengelolanya",
                            fix=f"di Discord: Edit Channel #{name} → Permissions → tambahkan bot dengan View Channel + "
                                "Manage Permissions, lalu ulangi")
    for other_id, other in current.items():
        if other_id not in wanted_ids and int(other.get("allow") or 0) & PERM["VIEW_CHANNEL"]:
            ctx.warn(f"#{name} punya izin lihat tambahan untuk ID {other_id} (tidak diubah wizard); pastikan memang disengaja")
    return ch, False


def resolve_admin(ctx: Context, api: DiscordAPI, guild_id: str, text: str) -> dict:
    """ID user atau username → member. Hasil pencarian ganda: operator memilih (non-interaktif: gagal)."""
    text = text.strip().lstrip("@")
    if ID_RE.match(text):
        try:
            return api.guild_member(guild_id, text)
        except DiscordHTTPError as exc:
            if exc.status == 404:
                raise StepError(f"user ID {text} bukan anggota server ini", fix="minta orangnya masuk server dulu, atau periksa ID-nya")
            raise
    hits = [m for m in api.search_members(guild_id, text, limit=10) if not (m.get("user") or {}).get("bot")]
    if not hits:
        raise StepError(f"tidak ada anggota server yang namanya diawali '{text}'",
                        fix="pakai username Discord persis, atau user ID (Developer Mode → klik kanan → Copy User ID)")
    exact = [m for m in hits if _name_key(m["user"].get("username")) == _name_key(text)]
    if len(exact) == 1:
        return exact[0]
    if len(hits) == 1:
        return hits[0]
    labels = [(str(m["user"]["id"]), f"{m['user'].get('username')} — {m['user'].get('global_name') or m.get('nick') or ''} "
               f"({m['user']['id']})") for m in hits]
    if ctx.prompt.non_interactive:
        raise StepError(f"'{text}' cocok dengan {len(hits)} anggota: " + "; ".join(label for _v, label in labels),
                        fix="tulis user ID-nya di `admins`")
    choice = ctx.prompt.choose("admin_pick", f"'{text}' cocok dengan beberapa orang:", labels, record=False)
    return next(m for m in hits if str(m["user"]["id"]) == choice)


class DiscordServer(Step):
    name, title = "discord_server", "Menyiapkan server Discord (role, kategori, channel)"
    explain = ("Dengan token bot: mencari atau membuat tiga role, kategori, dan dua channel privat, lalu mencatat semua "
               "ID-nya. Yang sudah ada (nama sama) dipakai ulang, tidak digandakan.")

    KEYS = ("role_entry_id", "role_member_id", "role_admin_id", "category_id", "shared_channel_id",
            "admin_channel_id", "admins")

    def check(self, ctx):
        return ctx.state.done(self.name) and all(ctx.data.get(k) for k in self.KEYS)

    def plan(self, ctx):
        return ["periksa izin bot di server (dari role-nya)",
                "cari/buat role entry, backend, hermes-admin (tanpa izin)",
                "periksa role bot berada di atas ketiganya",
                "cari/buat kategori Hermes, #hermes-shared (backend + bot), #hermes-admin (hermes-admin + bot)",
                "tanya admin tim (ID atau username) dan beri mereka role hermes-admin"]

    def bot_view(self, api, guild_id, bot_id):
        roles = api.guild_roles(guild_id)
        member = api.guild_member(guild_id, bot_id)
        mine = [r for r in roles if str(r["id"]) in {str(x) for x in member.get("roles") or []}]
        perms = 0
        for role in mine + [r for r in roles if str(r["id"]) == str(guild_id)]:
            perms |= int(role.get("permissions") or 0)
        top = max([int(r.get("position") or 0) for r in mine] or [0])
        return roles, perms, top

    def run(self, ctx):
        api, guild_id = ctx.discord(), str(ctx.data.get("guild_id") or "")
        if not guild_id:
            raise StepError("server Discord belum dipilih", fix=f"{RESUME_HINT} --from discord_bot")
        bot_id = ctx.data.get("bot_id") or api.bot_user_id()
        wanted = invite_permissions(bool(ctx.state.answers.get("moderation")))
        while True:
            roles, perms, _top = self.bot_view(api, guild_id, bot_id)
            lacking = 0 if perms & PERM["ADMINISTRATOR"] else wanted & ~perms
            if not lacking:
                ctx.say("  ✓ Izin bot di server lengkap")
                break
            url = invite_url(ctx.data.get("app_id", ""), bool(ctx.state.answers.get("moderation")))
            if ctx.resolve("on_missing_permissions", "izin bot kurang: " + ", ".join(permission_names(lacking)),
                           "undang ulang bot dengan tautan ini (izin diperbarui, bot tidak perlu dikeluarkan):\n" + url
                           + "\natau centang izin itu di Server Settings → Roles → role bot") == "skip":
                break

        names = {
            "entry": ctx.prompt.ask("role_entry", "Nama role untuk user baru", "entry"),
            "member": ctx.prompt.ask("role_member", "Nama role anggota tim (memberi role ini memicu onboarding)", "backend"),
            "admin": ctx.prompt.ask("role_admin", "Nama role admin tim", "hermes-admin"),
        }
        if len({_name_key(n) for n in names.values()}) < 3:
            raise StepError("ketiga nama role harus berbeda")
        found: Dict[str, dict] = {}
        try:
            for key, role_name in names.items():
                found[key], created = ensure_role(ctx, api, guild_id, roles, role_name)
                ctx.data[f"role_{key}_id"], ctx.data[f"role_{key}_name"] = str(found[key]["id"]), role_name
                ctx.say(f"  ✓ role @{role_name}: {found[key]['id']}" + (" (baru dibuat)" if created else " (sudah ada)"))
        except DiscordHTTPError as exc:
            raise self.forbidden(exc, "membuat role", "Manage Roles")
        ctx.save()

        while True:
            roles, _perms, top = self.bot_view(api, guild_id, bot_id)
            position = {str(r["id"]): int(r.get("position") or 0) for r in roles}
            above = [names[k] for k in names if position.get(ctx.data[f"role_{k}_id"], 0) >= top]
            if not above:
                ctx.say("  ✓ Role bot berada di atas ketiga role tim")
                break
            if ctx.resolve("on_role_hierarchy",
                           "role bot berada di bawah (atau sejajar) role: " + ", ".join("@" + n for n in above),
                           "Discord → Server Settings → Roles: seret role bot (namanya sama dengan nama aplikasi) ke ATAS "
                           "role tersebut.\nBot hanya bisa memberi/mencabut role yang posisinya di bawah role bot.") == "skip":
                break

        category_name = ctx.prompt.ask("category_name", "Nama kategori channel", "Hermes")
        shared_name = ctx.prompt.ask("shared_channel", "Nama channel bersama", "hermes-shared", validate=valid_channel_name)
        admin_name = ctx.prompt.ask("admin_channel", "Nama channel admin", "hermes-admin", validate=valid_channel_name)
        if _name_key(shared_name) == _name_key(admin_name):
            raise StepError("channel bersama dan channel admin harus berbeda")
        try:
            channels = api.guild_channels(guild_id)
            category, created = ensure_category(api, guild_id, channels, category_name)
            ctx.data["category_id"], ctx.data["category_name"] = str(category["id"]), category_name
            ctx.say(f"  ✓ kategori {category_name}: {category['id']}" + (" (baru dibuat)" if created else " (sudah ada)"))
            for key, ch_name, role_key, topic in (
                    ("shared", shared_name, "member", "Hermes bersama tim: mention bot untuk memulai thread."),
                    ("admin", admin_name, "admin", "Hermes admin tim: kelola anggota, model, dan server.")):
                ch, created = ensure_text_channel(
                    ctx, api, guild_id, channels, ch_name, ctx.data["category_id"],
                    private_channel_overwrites(guild_id, ctx.data[f"role_{role_key}_id"], bot_id), topic)
                ctx.data[f"{key}_channel_id"], ctx.data[f"{key}_channel_name"] = str(ch["id"]), ch_name
                ctx.say(f"  ✓ #{ch_name}: {ch['id']}" + (" (baru dibuat)" if created else " (sudah ada, izin diselaraskan)"))
        except DiscordHTTPError as exc:
            raise self.forbidden(exc, "membuat kategori/channel", "Manage Channels")
        ctx.save()

        owner_id = str((api.guild(guild_id) or {}).get("owner_id") or "")
        raw = ctx.prompt.ask(
            "admins", "Admin tim: user ID atau username Discord, pisahkan koma (bawaan: pemilik server)",
            owner_id or None)
        admins: List[str] = []
        for item in [p for p in re.split(r"[,\s]+", raw) if p]:
            member = resolve_admin(ctx, api, guild_id, item)
            uid = str(member["user"]["id"])
            if uid in admins:
                continue
            admins.append(uid)
            if ctx.data["role_admin_id"] not in [str(r) for r in member.get("roles") or []]:
                try:
                    api.add_role(guild_id, uid, ctx.data["role_admin_id"], reason="Hermes setup: admin tim")
                except DiscordHTTPError as exc:
                    raise self.forbidden(exc, f"memberi role @{names['admin']}", "Manage Roles")
            ctx.say(f"  ✓ admin: {member['user'].get('username')} ({uid}) punya role @{names['admin']}")
        if not admins:
            raise StepError("minimal satu admin tim", fix="isi `admins` dengan user ID atau username")
        ctx.data["admins"] = admins

    @staticmethod
    def forbidden(exc: DiscordHTTPError, doing: str, permission: str) -> StepError:
        if exc.status == 403:
            return StepError(f"Discord menolak {doing} (403)",
                             cause=f"bot tidak punya izin {permission}, atau role bot berada di bawah role yang diatur",
                             fix="undang ulang bot dengan tautan dari langkah discord_bot (izin lengkap), dan seret role bot "
                                 "ke atas role entry/backend/hermes-admin di Server Settings → Roles")
        return StepError(f"Discord menolak {doing}", cause=str(exc))


def valid_channel_name(value: str) -> Optional[str]:
    if not re.match(r"^[a-z0-9_-]{1,90}$", value.lstrip("#")) or value.startswith("#"):
        return "nama channel: huruf kecil, angka, '-' atau '_', tanpa '#'"
    return None


def valid_url(value: str) -> Optional[str]:
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return "tulis URL lengkap, mis. https://llm.example.internal/v1"
    if parts.username or parts.password:
        return "jangan taruh kredensial di URL"
    return None


def valid_env_name(value: str) -> Optional[str]:
    if not re.match(r"^[A-Z][A-Z0-9_]{1,63}$", value):
        return "nama variabel env: HURUF_BESAR, angka, garis bawah (ini NAMA variabel, bukan key-nya)"
    if value == "DISCORD_BOT_TOKEN":
        return "DISCORD_BOT_TOKEN tidak boleh dipakai di sini"
    return None


def valid_model(value: str) -> Optional[str]:
    return None if re.match(r"^[^\s]{1,200}$", value) else "nama model tanpa spasi"


def probe_llm(http: Callable[..., Tuple[int, Any]], base: str, key: str, model: str) -> Tuple[bool, List[str]]:
    """GET /models (informasi) lalu satu POST /chat/completions kecil (penentu). → (ok, baris laporan)."""
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    lines: List[str] = []
    status, body = http("GET", f"{base}/models", headers)
    if status == 200 and isinstance(body, dict) and isinstance(body.get("data"), list):
        ids = [str(m.get("id")) for m in body["data"] if isinstance(m, dict)]
        lines.append(f"GET /models → 200 ({len(ids)} model)")
        if ids and model not in ids:
            lines.append(f"model '{model}' tidak ada di daftar /models (contoh: {', '.join(ids[:5])})")
    else:
        lines.append(f"GET /models → {status or 'tidak terjangkau'}" + (f" ({body})" if status == 0 else " (diabaikan)"))
    status, body = http("POST", f"{base}/chat/completions", headers,
                        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5})
    if status == 200:
        lines.append(f"POST /chat/completions ({model}) → 200")
        return True, lines
    detail = body.get("error") if isinstance(body, dict) else body
    if isinstance(detail, dict):
        detail = detail.get("message")
    lines.append(f"POST /chat/completions ({model}) → {status or 'tidak terjangkau'}: {str(detail)[:200]}")
    return False, lines


LLM_STATUS_HINTS = {
    0: "URL salah, DNS/firewall, atau endpoint hanya bisa dijangkau dari jaringan lain",
    401: "API key salah atau kedaluwarsa", 403: "API key tidak berhak atas model/endpoint ini",
    404: "URL dasar salah (biasanya harus berakhir /v1) atau nama model tidak dikenal",
    429: "kuota/rate limit endpoint habis",
}


class LLMEndpoint(Step):
    name, title = "llm", "Endpoint LLM dan API key"
    explain = ("Mencatat URL endpoint OpenAI-compatible dan nama model, menyimpan API key di /etc/hermes/.env "
               "(root, tidak bisa ditimpa anggota), lalu menguji satu permintaan kecil.")

    def check(self, ctx):
        ans = ctx.state.answers
        return (ctx.state.done(self.name) and bool(ans.get("llm_base_url")) and bool(ans.get("llm_key_env"))
                and bool(ctx.read_env_value(ctx.llm_env, str(ans.get("llm_key_env")))))

    def plan(self, ctx):
        return ["tanya URL dasar, nama variabel env key, nama model normal/simple; baca key dengan getpass",
                "uji: GET {base}/models lalu POST {base}/chat/completions (max_tokens 5)",
                f"tulis baris key ke {ctx.llm_env} (0640 root:{ctx.user_name})"]

    def run(self, ctx):
        info = ctx.user()
        reask = False
        while True:
            base = ctx.prompt.ask("llm_base_url", "URL dasar endpoint LLM (OpenAI-compatible)", None, valid_url, reask).rstrip("/")
            ctx.state.answers["llm_base_url"] = base
            key_env = ctx.prompt.ask("llm_key_env", "NAMA variabel env untuk API key", "TD_API_KEY", valid_env_name, reask)
            normal = ctx.prompt.ask("llm_model_normal", "Nama model di endpoint untuk alias td-normal (model utama)",
                                    "td-normal", valid_model, reask)
            simple = ctx.prompt.ask("llm_model_simple", "Nama model di endpoint untuk alias td-simple (model ringan)",
                                    "td-simple", valid_model, reask)
            key = ctx.llm_key(key_env, fresh=reask)
            ok = True
            for model in dict.fromkeys((simple, normal)):
                good, lines = probe_llm(ctx.sys.http_json, base, key, model)
                for line in lines:
                    ctx.say(f"  {'✓' if good else '·'} {line}")
                ok = ok and good
                status = 200 if good else _last_status(lines)
                if not good:
                    ctx.say(f"    kemungkinan: {LLM_STATUS_HINTS.get(status, 'lihat pesan endpoint di atas')}")
            if ok:
                break
            choice = self.on_failure(ctx)
            if choice == "skip":
                ctx.warn("uji endpoint LLM gagal dan dilanjutkan; AI tidak akan menjawab sampai URL/key/model benar")
                break
            reask = choice == "edit"
            if choice == "retry":
                continue
        ctx.make_dir(ctx.etc_dir, 0o755)
        ctx.set_env_value(ctx.llm_env, key_env, key, 0o640, "root", info.name)
        if ctx.read_env_value(ctx.llm_env, "DISCORD_BOT_TOKEN"):
            ctx.warn(f"{ctx.llm_env} memuat DISCORD_BOT_TOKEN; berkas itu terbaca semua profile — hapus baris itu")
        ctx.say(f"  ✓ {key_env} disimpan di {ctx.llm_env} (0640 root:{info.name})")

    def on_failure(self, ctx):
        if ctx.prompt.non_interactive or "llm_on_failure" in ctx.prompt.answers:
            if str(ctx.prompt.answers.get("llm_on_failure", "")) == "skip":
                return "skip"
            raise StepError("uji endpoint LLM gagal (status HTTP di atas)",
                            fix="perbaiki URL/key/model, atau set `llm_on_failure: skip` untuk melanjutkan tanpa uji")
        return ctx.prompt.choose("llm_on_failure", "Uji endpoint gagal. Apa selanjutnya?", [
            ("retry", "Coba lagi dengan nilai yang sama"),
            ("edit", "Isi ulang URL, model, dan key"),
            ("skip", "Lanjut tanpa uji (perbaiki nanti)")], record=False)


def _last_status(lines: List[str]) -> int:
    match = re.search(r"→ (\d+)", lines[-1]) if lines else None
    return int(match.group(1)) if match else 0


def valid_report_time(value: str) -> Optional[str]:
    if value == "" or re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
        return None
    return "tulis jam HH:MM (24 jam), mis. 08:00; '-' untuk mematikan"


def valid_timezone(value: str) -> Optional[str]:
    if value == "":
        return None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("UTC")
    except Exception:  # noqa: BLE001 — tanpa tzdata tidak bisa memvalidasi; terima bentuknya saja
        return None if re.match(r"^[A-Za-z_]+(/[A-Za-z0-9_+\-]+)*$", value) else "zona waktu tidak valid"
    try:
        ZoneInfo(value)
    except Exception:  # noqa: BLE001
        return "zona waktu tidak dikenal (contoh: Asia/Makassar, Asia/Jakarta)"
    return None


class Options(Step):
    name, title = "options", "Zona waktu dan pilihan tim"
    explain = "Beberapa pilihan untuk team.yaml. Semuanya bisa diubah nanti dengan mengedit team.yaml lalu `apply`."

    def check(self, ctx):
        ans = ctx.state.answers
        return ctx.state.done(self.name) and (not ans.get("usage_reports") or bool(ctx.data.get("usage_channel_id")))

    def plan(self, ctx):
        return ["tanya nama tim, zona waktu, thread per pesan, laporan pemakaian terjadwal",
                "bila laporan dipilih: cari/buat #hermes-usage privat (hermes-admin hanya-baca + bot)"]

    def run(self, ctx):
        ctx.prompt.ask("team_name", "Nama tim", "Backend")
        ctx.prompt.ask("timezone", "Zona waktu batas hari laporan pemakaian", ctx.sys.system_timezone() or "", valid_timezone)
        ctx.prompt.confirm("threads_personal", "Tiap pesan baru di channel pribadi membuka thread/sesi baru?", True)
        if not ctx.prompt.confirm("usage_reports", "Kirim laporan pemakaian token terjadwal ke channel khusus (tanpa LLM)?", True):
            ctx.data.pop("usage_channel_id", None)
            return
        name = ctx.prompt.ask("usage_channel", "Nama channel laporan", "hermes-usage", validate=valid_channel_name)
        ctx.prompt.ask("report_daily", "Jam laporan harian (kemarin)", "08:00", valid_report_time)
        ctx.prompt.ask("report_weekly", "Jam laporan mingguan (tiap Senin)", "08:05", valid_report_time)
        ctx.prompt.ask("report_monthly", "Jam laporan bulanan (tiap tanggal 1)", "08:10", valid_report_time)
        need = ("guild_id", "category_id", "role_admin_id")
        if not all(ctx.data.get(k) for k in need):
            raise StepError("server Discord belum disiapkan", fix=f"{RESUME_HINT} --from discord_server")
        api, guild_id = ctx.discord(), ctx.data["guild_id"]
        bot_id = ctx.data.get("bot_id") or api.bot_user_id()
        try:
            ch, created = ensure_text_channel(
                ctx, api, guild_id, api.guild_channels(guild_id), name, ctx.data["category_id"],
                private_channel_overwrites(guild_id, ctx.data["role_admin_id"], bot_id, USAGE_ADMIN_ALLOW),
                "Laporan pemakaian LLM terjadwal (angka dari buku besar, tanpa AI).")
        except DiscordHTTPError as exc:
            raise DiscordServer.forbidden(exc, f"membuat #{name}", "Manage Channels")
        ctx.data["usage_channel_id"], ctx.data["usage_channel_name"] = str(ch["id"]), name
        ctx.say(f"  ✓ #{name}: {ch['id']}" + (" (baru dibuat)" if created else " (sudah ada, izin diselaraskan)"))


class HermesInstall(Step):
    name, title = "hermes_install", "Memasang Hermes"
    explain = ("Memasang Hermes untuk user Hermes dengan installer resminya dalam mode non-interaktif (tanpa `hermes setup` "
               "dan tanpa memasang gateway), lalu memastikan token bot ada di ~/.hermes/.env. Gateway sengaja BELUM "
               "dijalankan: konfigurasi tim harus diterapkan dulu. Jangan jalankan `hermes gateway setup` sendiri.")

    def command(self):
        return f"set -o pipefail; curl -fsSL {HERMES_INSTALL_URL} | bash -s -- --non-interactive --skip-setup"

    def check(self, ctx):
        return (ctx.home() is not None and ctx.hermes_bin().exists()
                and bool(ctx.read_env_value(ctx.hermes_env, "DISCORD_BOT_TOKEN")))

    def plan(self, ctx):
        lines = []
        if ctx.home() is None or not ctx.hermes_bin().exists():
            lines.append(f"(setelah persetujuan) sebagai {ctx.user_name}: bash -c '{self.command()}'")
        return lines + [f"pastikan DISCORD_BOT_TOKEN di ~{ctx.user_name}/.hermes/.env (0600), ditulis langsung ke berkas",
                        f"sebagai {ctx.user_name}: hermes --version; hermes doctor"]

    def run(self, ctx):
        info = ctx.user()
        if not ctx.hermes_bin().exists():
            ctx.say(f"  Hermes belum terpasang untuk user {info.name}. Perintah installer resmi (dijalankan sebagai {info.name}):")
            ctx.say(f"    {self.command()}")
            fix = (f"pasang manual: sudo -iu {info.name}, lalu `curl -fsSL {HERMES_INSTALL_URL} | bash`; "
                   f"setelah `hermes --version` jalan, ulangi: {RESUME_HINT}")
            if not ctx.prompt.confirm("install_hermes", "Unduh dan jalankan installer itu sekarang? (beberapa menit)", None):
                raise StepError("Hermes belum terpasang", fix=fix)
            ctx.run(["bash", "-c", self.command()], as_user=True, stream=True, what="installer Hermes",
                    fix=fix + f". Log installer: {info.home}/.hermes/logs/install.log")
            if not ctx.hermes_bin().exists():
                raise StepError(f"installer selesai tetapi {ctx.hermes_bin()} tidak ada",
                                cause="installer menaruh launcher di tempat lain, atau gagal diam-diam", fix=fix)
        version = ctx.run(["hermes", "--version"], as_user=True, what="hermes --version")
        ctx.say("  ✓ " + (version.out.strip().splitlines() or ["hermes terpasang"])[0])
        token = ctx.bot_token()
        ctx.make_dir(Path(info.home) / ".hermes", 0o700, info.name, info.name)
        ctx.set_env_value(ctx.hermes_env, "DISCORD_BOT_TOKEN", token, 0o600, info.name, info.name)
        ctx.say(f"  ✓ DISCORD_BOT_TOKEN ada di {ctx.hermes_env} (0600)")
        doctor = ctx.run(["hermes", "doctor"], as_user=True, check=False, timeout=300)
        for line in (doctor.out + doctor.err).strip().splitlines()[-25:]:
            ctx.say(f"    {line}")
        if not doctor.ok:
            ctx.warn(f"`hermes doctor` keluar dengan kode {doctor.rc}; baca keluarannya di atas (belum tentu fatal sebelum provisioning)")


KIT_EXCLUDE = (".git", ".github", "__pycache__", ".pytest_cache", ".venv", "arsip-versi-lama", "*.pyc", "*.bak", "*.bak-*")


class KitInstall(Step):
    name, title = "kit_install", "Memasang kit"
    explain = "Menyalin kit ini ke direktori milik root, supaya tidak bisa diubah user Hermes maupun agent."

    def check(self, ctx):
        return ctx.state.done(self.name) and (ctx.kit_dir / "hermes_team.py").is_file()

    def plan(self, ctx):
        same = ctx.kit_dir.resolve() == ctx.source_dir.resolve()
        lines = [] if same else [f"salin {ctx.source_dir} → {ctx.kit_dir} (tanpa .git, cache uji, arsip-versi-lama)"]
        return lines + [f"chown -R root:root {ctx.kit_dir}", f"chmod -R a+rX,go-w {ctx.kit_dir}"]

    def run(self, ctx):
        src, dst = ctx.source_dir.resolve(), ctx.kit_dir
        if not (src / "hermes_team.py").is_file():
            raise StepError(f"{src} bukan direktori kit (hermes_team.py tidak ada)")
        if dst.resolve() == src:
            ctx.say(f"  · wizard dijalankan dari {dst}; tidak perlu menyalin, hanya merapikan kepemilikan")
        elif (dst / ".git").exists():
            ctx.say(f"  · {dst} adalah clone git; isinya tidak ditimpa (perbarui dengan `git -C {dst} pull`)")
        else:
            stage = dst.with_name(dst.name + ".baru")
            old = dst.with_name(dst.name + ".lama")
            for leftover in (stage, old):
                if leftover.exists():
                    shutil.rmtree(leftover)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, stage, ignore=shutil.ignore_patterns(*KIT_EXCLUDE), symlinks=True)
            if dst.exists():
                os.replace(dst, old)
            os.replace(stage, dst)
            if old.exists():
                shutil.rmtree(old)
            ctx.say(f"  ✓ kit v{KIT_VERSION} disalin ke {dst}")
        ctx.run(["chown", "-R", "root:root", str(dst)], what="chown kit")
        ctx.run(["chmod", "-R", "a+rX,go-w", str(dst)], what="chmod kit")


def build_team_config(example: dict, data: Dict[str, Any], answers: Dict[str, Any]) -> dict:
    """team.example.yaml sebagai dasar; hanya nilai yang dikumpulkan wizard yang diganti."""
    team = copy.deepcopy(example)
    team["team_name"] = str(answers.get("team_name") or team.get("team_name") or "Backend")
    team["guild_id"] = str(data["guild_id"])
    team["roles"] = dict(team.get("roles") or {}, member=str(data["role_member_id"]), admin=str(data["role_admin_id"]),
                         entry=str(data["role_entry_id"]))
    team["admins"] = [str(a) for a in data["admins"]]
    team["category_id"] = str(data["category_id"])
    team["shared"] = dict(team.get("shared") or {}, channel_id=str(data["shared_channel_id"]))
    team["admin"] = dict(team.get("admin") or {}, channel_id=str(data["admin_channel_id"]))
    team["threads"] = dict(team.get("threads") or {}, personal=bool(answers.get("threads_personal", True)))
    llm = team.setdefault("llm", {})
    provider = llm.setdefault("providers", {}).setdefault("td", {})
    provider["api"], provider["key_env"] = str(answers["llm_base_url"]), str(answers.get("llm_key_env") or "TD_API_KEY")
    models = llm.setdefault("models", {})
    for alias, key in (("td-normal", "llm_model_normal"), ("td-simple", "llm_model_simple")):
        models[alias] = dict(models.get(alias) or {"provider": "custom:td"}, model=str(answers.get(key) or alias))
    usage = dict(team.get("usage") or {})
    usage["timezone"] = str(answers.get("timezone") or "")
    reports_on = bool(answers.get("usage_reports")) and bool(data.get("usage_channel_id"))
    usage["report_channel_id"] = str(data["usage_channel_id"]) if reports_on else ""
    usage["reports"] = {kind: (str(answers.get(f"report_{kind}") or "") if reports_on else "")
                        for kind in ("daily", "weekly", "monthly")}
    team["usage"] = usage
    team["server_admin"] = dict(team.get("server_admin") or {}, allow_moderation=bool(answers.get("moderation")))
    if data.get("sandbox_image"):
        team["sandbox"] = dict(team.get("sandbox") or {}, image=str(data["sandbox_image"]))
    return team


def render_team_yaml(team: dict, kit_dir: Path, stamp: str) -> str:
    header = (f"# team.yaml — dibuat oleh wizard pemasangan (kit_setup.py, kit v{KIT_VERSION}) pada {stamp}\n"
              f"# dari team.example.yaml. BOLEH diedit: penjelasan tiap setelan ada di {kit_dir}/team.example.yaml.\n"
              f"# Setelah mengubah:  python3 {kit_dir}/hermes_team.py check  &&  python3 {kit_dir}/hermes_team.py apply\n"
              "# Tulis semua ID Discord dan jam laporan di dalam tanda kutip.\n\n")
    return header + yaml.safe_dump(team, sort_keys=False, allow_unicode=True, width=100, default_flow_style=False)


def flatten(data: Any, trail: str = "") -> Dict[str, Any]:
    if isinstance(data, dict) and data:
        out: Dict[str, Any] = {}
        for key, value in data.items():
            out.update(flatten(value, f"{trail}.{key}" if trail else str(key)))
        return out
    return {trail: data}


def diff_summary(old: Any, new: Any) -> List[str]:
    a, b = flatten(old or {}), flatten(new or {})
    lines = []
    for key in sorted(set(a) | set(b)):
        if key not in b:
            lines.append(f"- {key}: {a[key]!r} (ada di berkasmu, tidak ada di hasil wizard → akan hilang)")
        elif key not in a:
            lines.append(f"+ {key}: {b[key]!r}")
        elif a[key] != b[key]:
            lines.append(f"~ {key}: {a[key]!r} → {b[key]!r}")
    return sorted(lines, key=lambda line: "~-+".index(line[0]))      # yang berubah/hilang dulu, tambahan terakhir


class WriteConfig(Step):
    name, title = "write_config", "Menulis team.yaml, members.yaml, dan /etc/hermes/.env"
    explain = ("Menyusun team.yaml dari team.example.yaml dengan ID dan pilihan yang sudah dikumpulkan (tidak ada lagi "
               "salin-tempel ID), lalu memvalidasinya dengan `hermes_team.py check`.")
    REQUIRED = ("guild_id", "role_member_id", "role_admin_id", "role_entry_id", "admins", "category_id",
                "shared_channel_id", "admin_channel_id")

    def check(self, ctx):
        return (ctx.state.done(self.name) and ctx.home() is not None and ctx.team_file.is_file()
                and (ctx.team_dir / "members.yaml").is_file())

    def plan(self, ctx):
        home = f"~{ctx.user_name}"
        return [f"buat {home}/.hermes-team (0700) dan team.yaml dari team.example.yaml + nilai yang dikumpulkan",
                "team.yaml yang sudah ada: tampilkan ringkasan beda, tanya dulu, cadangkan ke .bak-<waktu>",
                f"buat {home}/.hermes-team/members.yaml (members: []) bila belum ada",
                f"pastikan {ctx.llm_env} (0640 root:{ctx.user_name}) berisi baris API key",
                f"sebagai {ctx.user_name}: python3 {ctx.kit_dir}/hermes_team.py check"]

    def run(self, ctx):
        info = ctx.user()
        lacking = [k for k in self.REQUIRED if not ctx.data.get(k)]
        if lacking or not ctx.state.answers.get("llm_base_url"):
            raise StepError("data untuk team.yaml belum lengkap: " + ", ".join(lacking or ["llm_base_url"]),
                            fix=f"{RESUME_HINT} --from discord_server  (atau --from llm)")
        example_path = next((p for p in (ctx.kit_dir / "team.example.yaml", ctx.source_dir / "team.example.yaml")
                             if p.is_file()), None)
        if example_path is None:
            raise StepError("team.example.yaml tidak ditemukan", fix=f"{RESUME_HINT} --from kit_install")
        example = yaml.safe_load(example_path.read_text(encoding="utf-8")) or {}
        team = build_team_config(example, ctx.data, ctx.state.answers)
        ctx.make_dir(ctx.team_dir, 0o700, info.name, info.name)
        target = ctx.team_file
        write = True
        if target.exists():
            try:
                current = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                current = {"(berkas lama)": "bukan YAML yang valid"}
            changes = diff_summary(current, team)
            if not changes:
                ctx.say(f"  ✓ {target} sudah sama dengan hasil wizard; tidak diubah")
                write = False
            else:
                ctx.say(f"  {target} sudah ada dan berbeda dari hasil wizard ({len(changes)} nilai):")
                for line in changes[:30]:
                    ctx.say(f"    {line}")
                if len(changes) > 30:
                    ctx.say(f"    … dan {len(changes) - 30} lainnya")
                ctx.say("  (komentar di berkas lama juga tidak ikut ke berkas baru)")
                if ctx.prompt.confirm("overwrite_team_yaml", "Timpa dengan hasil wizard? Berkas lama dicadangkan", None):
                    backup = target.with_name(f"{target.name}.bak-{ctx.sys.timestamp()}")
                    shutil.copy2(target, backup)
                    ctx.sys.chown(backup, info.name, info.name)
                    ctx.say(f"  ✓ cadangan: {backup}")
                else:
                    ctx.say("  · team.yaml lama dipertahankan; langkah berikutnya memakai berkas itu")
                    write = False
        if write:
            ctx.write_file(target, render_team_yaml(team, ctx.kit_dir, ctx.sys.timestamp()), 0o600, info.name, info.name)
            ctx.say(f"  ✓ {target} ditulis")
        members = ctx.team_dir / "members.yaml"
        if not members.exists():
            ctx.write_file(members, "# members.yaml — anggota tim. Mulai kosong: anggota masuk lewat onboarding otomatis\n"
                           "# (beri role backend di Discord). Contoh isi manual: lihat members.example.yaml di kit.\n"
                           "members: []\n", 0o600, info.name, info.name)
            ctx.say(f"  ✓ {members} ditulis (kosong)")
        key_env = str(ctx.state.answers.get("llm_key_env") or "TD_API_KEY")
        ctx.make_dir(ctx.etc_dir, 0o755)
        ctx.set_env_value(ctx.llm_env, key_env, ctx.llm_key(key_env), 0o640, "root", info.name)
        ctx.say(f"  ✓ {ctx.llm_env} berisi {key_env} (0640 root:{info.name})")
        ctx.run(["python3", str(ctx.kit_dir / "hermes_team.py"), "check", "-c", str(target)], as_user=True, stream=True,
                what="validasi team.yaml", fix=f"perbaiki {target} sesuai pesan di atas, lalu: {RESUME_HINT}")


def run_provision(ctx: Context) -> None:
    """apply → pasang managed → apply lagi (empat setelan pindah dari managed ke tiap profile; docs/SKALA.md)."""
    kit, team = str(ctx.kit_dir / "hermes_team.py"), str(ctx.team_file)
    fix = ("baca pesan di atas. `apply` menolak key di llm.providers → pindahkan key ke /etc/hermes/.env. "
           f"Setelah diperbaiki: {RESUME_HINT}")
    ctx.run(["python3", kit, "apply", "-c", team], as_user=True, stream=True, what="hermes_team.py apply", fix=fix)
    managed = ctx.run(["python3", kit, "managed", "-c", team], as_user=True, what="hermes_team.py managed", fix=fix)
    try:
        parsed = yaml.safe_load(managed.out)
    except yaml.YAMLError as exc:
        raise StepError("keluaran `hermes_team.py managed` bukan YAML yang valid", cause=str(exc))
    if not isinstance(parsed, dict) or not parsed:
        raise StepError("keluaran `hermes_team.py managed` kosong; /etc/hermes/config.yaml tidak ditulis")
    ctx.make_dir(ctx.etc_dir, 0o755)
    ctx.write_file(ctx.managed_file, managed.out, 0o644)
    ctx.say(f"  ✓ kebijakan managed dipasang di {ctx.managed_file} (0644 root)")
    ctx.run(["python3", kit, "apply", "-c", team], as_user=True, stream=True, what="hermes_team.py apply (kedua)", fix=fix)


class Provision(Step):
    name, title = "provision", "Provisioning profile dan kebijakan managed"
    explain = ("Menjalankan `apply` (profile template/shared/admin, rute gateway, plugin team-guard), memasang kebijakan "
               "managed di /etc/hermes/config.yaml, lalu `apply` sekali lagi. Pertama kali bisa beberapa menit.")

    def check(self, ctx):
        return ctx.state.done(self.name) and ctx.managed_file.is_file()

    def plan(self, ctx):
        kit = ctx.kit_dir / "hermes_team.py"
        return [f"sebagai {ctx.user_name}: python3 {kit} apply",
                f"sebagai {ctx.user_name}: python3 {kit} managed → root menulis {ctx.managed_file} (0644 root)",
                f"sebagai {ctx.user_name}: python3 {kit} apply (kedua)"]

    def run(self, ctx):
        run_provision(ctx)


def set_sandbox_image(text: str, image: str) -> Optional[str]:
    """Ganti nilai `image:` di blok `sandbox:` tanpa menyentuh baris/komentar lain. None = tidak ketemu."""
    lines = text.splitlines(keepends=True)
    inside = False
    for i, line in enumerate(lines):
        if re.match(r"^sandbox:\s*(#.*)?$", line):
            inside = True
            continue
        if inside and re.match(r"^\S", line) and not line.startswith("#"):
            break
        match = re.match(r"^(\s+image:\s*)([^#\n]*?)(\s*(#.*)?)(\r?\n?)$", line) if inside else None
        if match:
            lines[i] = f"{match.group(1)}{json.dumps(image)}{match.group(3)}{match.group(5)}"
            return "".join(lines)
    return None


class Extras(Step):
    name, title = "extras", "Tambahan opsional: image sandbox dan timer pemakaian"
    explain = "Tiga hal opsional; masing-masing ditanyakan. Semua bisa dipasang nanti (docs/INSTALL.md Langkah 8 dan docs/PEMAKAIAN.md)."
    UNITS = {"collect": ("hermes-usage-collect.service", "hermes-usage-collect.timer"),
             "report": ("hermes-usage-report.service", "hermes-usage-report.timer")}

    def plan(self, ctx):
        return [f"(opsional) docker build -t hermes-team-sandbox:<tag> {ctx.kit_dir}/docker/sandbox, set sandbox.image, provisioning ulang",
                f"(opsional) pasang timer hermes-usage-collect sebagai unit user {ctx.user_name}",
                "(opsional, bila laporan terjadwal dipilih) pasang timer hermes-usage-report",
                "hanya disebutkan, tidak dijalankan: hermes egress install && hermes egress setup && hermes egress start"]

    def run(self, ctx):
        info = ctx.user()
        if ctx.prompt.confirm("build_sandbox", "Bangun image sandbox tim (PHP, Go, Java, Node, Python, klien DB, gh)? "
                              "Lama dan besar; boleh dilewati untuk percobaan pertama", False):
            tag = ctx.prompt.ask("sandbox_tag", "Tag image", "hermes-team-sandbox:" + time.strftime("%Y.%m"),
                                 lambda v: None if re.match(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+$", v)
                                 else "bentuk nama:tag, pakai tag bertanggal (bukan :latest)")
            ctx.run(["docker", "build", "-t", tag, str(ctx.kit_dir / "docker" / "sandbox")], stream=True,
                    what="docker build image sandbox",
                    fix="periksa koneksi dan ruang disk (`docker system df`); langkah ini boleh dilewati (build_sandbox: false)")
            ctx.data["sandbox_image"] = tag
            ctx.save()
            current = ctx.team_file.read_text(encoding="utf-8")
            updated = set_sandbox_image(current, tag)
            if updated is None or ((yaml.safe_load(updated) or {}).get("sandbox") or {}).get("image") != tag:
                ctx.warn(f"baris sandbox.image tidak ditemukan di {ctx.team_file}; isi manual `sandbox: {{image: \"{tag}\"}}` "
                         "lalu jalankan apply, managed, apply")
            elif updated != current:
                ctx.write_file(ctx.team_file, updated, 0o600, info.name, info.name)
                ctx.say(f"  ✓ sandbox.image = {tag}; provisioning ulang supaya berlaku")
                run_provision(ctx)
        if ctx.prompt.confirm("usage_timer", "Pasang pemotret pemakaian token (timer tiap 15 menit)?", True):
            self.install_timer(ctx, info, "collect")
        if ctx.data.get("usage_channel_id") and ctx.state.answers.get("usage_reports"):
            if ctx.prompt.confirm("report_timer", "Pasang timer laporan pemakaian terjadwal?", True):
                self.install_timer(ctx, info, "report")
        ctx.say("  · Pembatasan internet sandbox (proxy egress Hermes) TIDAK dipasang wizard. Bila perlu, sebagai "
                f"{info.name}:\n      hermes egress install && hermes egress setup && hermes egress start")

    def install_timer(self, ctx, info, kind):
        service, timer = self.UNITS[kind]
        unit_dir = Path(info.home) / ".config" / "systemd" / "user"
        ctx.run(["mkdir", "-p", str(unit_dir)], as_user=True, what="membuat direktori unit user")
        for unit in (service, timer):
            text = (ctx.kit_dir / "systemd" / unit).read_text(encoding="utf-8")
            ctx.write_file(unit_dir / unit, text.replace("/opt/hermes-team", str(ctx.kit_dir)), 0o644, info.name, info.name)
        fix = (f"`systemctl --user` butuh linger dan manajer user yang hidup: loginctl enable-linger {info.name}; "
               f"systemctl start user@{info.uid}.service; lalu {RESUME_HINT} --only extras")
        ctx.run(["systemctl", "start", f"user@{info.uid}.service"], check=False)
        ctx.run(["systemctl", "--user", "daemon-reload"], as_user=True, what="systemctl --user daemon-reload", fix=fix)
        ctx.run(["systemctl", "--user", "enable", "--now", timer], as_user=True, what=f"mengaktifkan {timer}", fix=fix)
        ctx.say(f"  ✓ {timer} aktif")


GATEWAY_LOG_HINTS = (
    ("PrivilegedIntentsRequired", "intent belum dihidupkan",
     f"{PORTAL_URL} → tab Bot → hidupkan Server Members Intent dan Message Content Intent, lalu ulangi"),
    ("LoginFailure", "token bot ditolak Discord", f"reset token di Developer Portal, lalu: {RESUME_HINT} --from discord_bot"),
    ("Improper token", "token bot ditolak Discord", f"reset token di Developer Portal, lalu: {RESUME_HINT} --from discord_bot"),
)


class Gateway(Step):
    name, title = "gateway", "Menjalankan gateway sebagai layanan"
    explain = "Memasang unit systemd user untuk gateway Hermes, menyalakannya, dan memeriksa statusnya."

    def plan(self, ctx):
        u = ctx.user_name
        return [f"systemctl start user@<uid {u}>.service",
                f"sebagai {u}: hermes gateway install",
                f"sebagai {u}: hermes gateway start",
                f"tunggu, lalu sebagai {u}: hermes gateway status (gagal → hermes logs gateway -n 40)"]

    def run(self, ctx):
        info = ctx.user()
        ctx.run(["systemctl", "start", f"user@{info.uid}.service"], check=False)
        fix = (f"periksa: sudo -iu {info.name} lalu `hermes doctor` dan `hermes logs gateway -n 80`. "
               f"Setelah diperbaiki: {RESUME_HINT} --from gateway")
        ctx.run(["hermes", "gateway", "install"], as_user=True, what="hermes gateway install", fix=fix)
        ctx.run(["hermes", "gateway", "start"], as_user=True, what="hermes gateway start", fix=fix)
        ctx.sys.sleep(8)
        status = ctx.run(["hermes", "gateway", "status"], as_user=True, check=False)
        for line in (status.out + status.err).strip().splitlines()[-20:]:
            ctx.say(f"    {line}")
        if not status.ok:
            logs = ctx.run(["hermes", "logs", "gateway", "-n", "40"], as_user=True, check=False)
            text = (logs.out + logs.err).strip()
            ctx.say("  --- baris terakhir log gateway ---")
            for line in text.splitlines()[-40:]:
                ctx.say(f"    {line}")
            cause, hint = f"`hermes gateway status` keluar dengan kode {status.rc}", fix
            for needle, why, how in GATEWAY_LOG_HINTS:
                if needle in text:
                    cause, hint = why, how
                    break
            raise StepError("gateway tidak berjalan", cause=cause, fix=hint)
        ctx.say("  ✓ `hermes gateway status` melaporkan sukses. Pastikan sendiri: bot tampil ONLINE di Discord, dan")
        ctx.say(f"    `sudo -iu {info.name} hermes logs -f` memuat baris plugin team-guard tanpa galat.")


def find_hermes_python(launcher: Path, home: Path) -> Tuple[str, str]:
    """(python milik Hermes, direktori sumber Hermes) untuk tests/selftest.sh; "" bila tidak ketemu.
    Launcher bisa berupa skrip ber-shebang `#!<python> -I` atau pembungkus `/bin/sh` berisi `exec <python> ...`
    (selftest.sh sendiri hanya membaca kata pertama shebang, jadi pembungkus sh membuatnya melewati uji integrasi)."""
    import shlex
    python = ""
    try:
        with open(os.path.realpath(str(launcher)), "rb") as fh:
            head = fh.read(4096).decode("utf-8", "replace").splitlines()
    except OSError:
        head = []
    if head and head[0].startswith("#!"):
        try:
            first = shlex.split(head[0][2:])
        except ValueError:
            first = []
        if first and "python" in os.path.basename(first[0]):
            python = first[0]
        else:
            for line in head[1:]:
                if line.startswith("exec "):
                    try:
                        python = next((t for t in shlex.split(line)[1:] if "python" in os.path.basename(t)), "")
                    except ValueError:
                        python = ""
                    break
    source = home / ".hermes" / "hermes-agent"
    source_dir = str(source) if (source / "gateway").is_dir() else ""
    if not python and source_dir:
        python = next((str(p) for p in (source / ".venv" / "bin" / "python", source / "venv" / "bin" / "python")
                       if p.exists()), "")
    return (python if python and os.path.isabs(python) else ""), source_dir


CHECKLIST = """  Uji di Discord sekarang (pakai akun uji, bukan admin):
    1. Akun uji masuk server            → dapat role @{entry}; bot tidak melayaninya.
    2. Admin memberi role @{member}     → ±1 menit: channel #hermes-<nama> muncul dengan sambutan,
                                          @{entry} hilang, #{admin_channel} menampilkan ✅.
    3. Akun uji menulis di channelnya   → bot membuka thread baru dan menjawab.
    4. /env-set di channelnya           → form pribadi; minta "jalankan printenv" → diblokir.
    5. Admin mencabut @{member}         → akses channel hilang, @{entry} kembali.
  Daftar lengkap 11 butir: docs/INSTALL.md, "Langkah 11". Bila ada yang tidak jalan: docs/INSTALL.md, "Bila ada yang
  tidak jalan", lalu  python3 {kit}/kit_doctor.py --discord"""


class Verify(Step):
    name, title = "verify", "Pemeriksaan akhir dan ringkasan"
    explain = "Menjalankan kit_doctor terhadap Discord, menawarkan uji kit (selftest), lalu merangkum semua yang dibuat."

    def check(self, ctx):
        return False       # selalu boleh diulang: hanya membaca

    def plan(self, ctx):
        return [f"sebagai {ctx.user_name}: python3 {ctx.kit_dir}/kit_doctor.py --discord",
                f"(opsional) sebagai {ctx.user_name}: HERMES_PYTHON=<python Hermes> PYTHONPATH=<sumber Hermes> "
                f"bash {ctx.kit_dir}/tests/selftest.sh",
                "cetak daftar uji di Discord dan ringkasan (tanpa rahasia)"]

    def run(self, ctx):
        info = ctx.user()
        doctor = ctx.run(["python3", str(ctx.kit_dir / "kit_doctor.py"), "--discord", "--config", str(ctx.team_file)],
                         as_user=True, check=False, timeout=300)
        for line in (doctor.out + doctor.err).strip().splitlines():
            ctx.say(f"    {line}")
        if doctor.ok:
            ctx.say("  ✓ kit_doctor: tidak ada masalah ber-level error")
        else:
            ctx.warn(f"kit_doctor menemukan masalah (kode {doctor.rc}); ikuti saran '→' di atas, lalu: {RESUME_HINT} --only verify")
        if ctx.prompt.confirm("run_selftest", "Jalankan uji kit terhadap Hermes di server ini (HOME sementara + Discord tiruan, "
                              "beberapa menit; butuh pytest di Python milik Hermes)?", False):
            python, source = find_hermes_python(ctx.hermes_bin(), Path(info.home))
            extra = {}
            if python:
                extra["HERMES_PYTHON"] = python
            if source:
                extra["PYTHONPATH"] = source
                extra["HERMES_SRC"] = source
            ctx.say("  (keluaran muncul setelah uji selesai)")
            test = ctx.run(["bash", str(ctx.kit_dir / "tests" / "selftest.sh")], as_user=True, check=False,
                           extra_env=extra, timeout=3600)
            text = (test.out + "\n" + test.err).strip()
            for line in text.splitlines()[:3] + text.splitlines()[3:][-25:]:
                ctx.say(f"    {line}")
            # selftest.sh: 0 = lulus dengan integrasi, 3 = unit lulus tetapi integrasi DILEWATI, 2 = prasyarat kurang.
            integrated = test.rc == 0 and "Uji integrasi: DIJALANKAN" in text
            if test.rc == 3:
                ctx.warn("selftest: uji unit lulus, tetapi uji integrasi DILEWATI (alasannya tercetak di atas). Kecocokan kit "
                         "dengan versi Hermes ini BELUM terbukti. Ulangi dengan HERMES_PYTHON=<python venv Hermes> dan "
                         "HERMES_SRC=<direktori sumber Hermes>.")
            elif test.rc == 2:
                ctx.warn("selftest belum bisa jalan: pytest/PyYAML belum ada di Python yang dipakai (perintah pasangnya tercetak di atas)")
            elif not test.ok:
                ctx.warn(f"selftest gagal (kode {test.rc}); kecocokan kit dengan versi Hermes ini belum terbukti")
            elif integrated:
                ctx.say("  ✓ selftest lulus, termasuk uji integrasi terhadap Hermes di server ini")
            else:
                ctx.warn("selftest lulus TANPA uji integrasi (Hermes tidak bisa di-import dari Python yang dipakai): kecocokan kit "
                         "dengan versi Hermes ini BELUM terbukti. Ulangi dengan HERMES_PYTHON=<python venv Hermes> dan "
                         "PYTHONPATH=<direktori sumber Hermes>.")
        self.summary(ctx, info)

    def summary(self, ctx, info):
        d, a = ctx.data, ctx.state.answers
        ctx.say("")
        ctx.say("  Ringkasan")
        ctx.say(f"    Server Discord : {d.get('guild_name', '?')} ({d.get('guild_id', '?')})")
        ctx.say(f"    Bot            : {d.get('app_name', '?')} (aplikasi {d.get('app_id', '?')}, user {d.get('bot_id', '?')})")
        for key, label in (("entry", "Role entry"), ("member", "Role anggota"), ("admin", "Role admin")):
            ctx.say(f"    {label:<15}: @{d.get(f'role_{key}_name', '?')} ({d.get(f'role_{key}_id', '?')})")
        ctx.say(f"    Kategori       : {d.get('category_name', '?')} ({d.get('category_id', '?')})")
        for key, label in (("shared", "Channel bersama"), ("admin", "Channel admin"), ("usage", "Channel laporan")):
            if d.get(f"{key}_channel_id"):
                ctx.say(f"    {label:<15}: #{d.get(f'{key}_channel_name', '?')} ({d[f'{key}_channel_id']})")
        ctx.say(f"    Admin tim      : {', '.join(d.get('admins') or []) or '?'}")
        ctx.say(f"    Endpoint LLM   : {a.get('llm_base_url', '?')} (key: variabel {a.get('llm_key_env', '?')}, nilainya tidak ditampilkan)")
        if d.get("sandbox_image"):
            ctx.say(f"    Image sandbox  : {d['sandbox_image']}")
        ctx.say("    Berkas")
        for path, note in ((ctx.kit_dir, "kode kit (root)"), (ctx.team_file, "setelan tim"),
                           (ctx.team_dir / "members.yaml", "anggota"), (ctx.hermes_env, "token bot, 0600"),
                           (ctx.llm_env, f"API key LLM, 0640 root:{info.name}"), (ctx.managed_file, "kebijakan managed"),
                           (ctx.state.path, "state wizard, tanpa rahasia")):
            ctx.say(f"      {path}  — {note}")
        if ctx.warnings:
            ctx.say("    Peringatan selama pemasangan")
            for warning in ctx.warnings:
                ctx.say(f"      ! {warning}")
        ctx.say("")
        ctx.say(CHECKLIST.format(entry=d.get("role_entry_name", "entry"), member=d.get("role_member_name", "backend"),
                                 admin_channel=d.get("admin_channel_name", "hermes-admin"), kit=ctx.kit_dir))


STEPS: List[Step] = [Preflight(), Packages(), LinuxUser(), DiscordBot(), DiscordServer(), LLMEndpoint(), Options(),
                     HermesInstall(), KitInstall(), WriteConfig(), Provision(), Extras(), Gateway(), Verify()]
STEP_NAMES = [s.name for s in STEPS]


# --------------------------------------------------------------------------- mesin langkah
def list_steps(ctx: Context, steps: List[Step]) -> None:
    if not ctx.state.readable:
        ctx.say(f"(berkas state {ctx.state.path} tidak terbaca tanpa root; status di bawah bisa kurang tepat)")
    for i, step in enumerate(steps, 1):
        try:
            done = step.check(ctx)
        except StepError:
            done = False
        ctx.say(f"{i:>2}. {step.name:<15} {'[selesai]' if done else '[belum]  '} {step.title}")


def report_failure(ctx: Context, step: Step, exc: BaseException) -> None:
    ctx.say("")
    if isinstance(exc, StepError):
        ctx.say(f"✗ Langkah '{step.name}' gagal: {exc.message}")
        if exc.cause:
            ctx.say("  Kemungkinan penyebab / detail:")
            for line in str(exc.cause).splitlines():
                ctx.say(f"    {line}")
        if exc.fix:
            ctx.say("  Perbaikan:")
            for line in str(exc.fix).splitlines():
                ctx.say(f"    {line}")
    else:
        ctx.say(f"✗ Langkah '{step.name}' gagal: {type(exc).__name__}: {exc}")
        if isinstance(exc, (PermissionError, OSError)):
            ctx.say("  Kemungkinan penyebab: izin berkas atau disk penuh.")
    ctx.say(f"  Langkah yang sudah selesai tersimpan. Setelah diperbaiki, lanjutkan dengan:  {RESUME_HINT}")
    ctx.say(f"  (ulangi hanya langkah ini: {RESUME_HINT} --only {step.name};  daftar langkah: {RESUME_HINT} --list)")


def run_wizard(ctx: Context, steps: Optional[List[Step]] = None, start: Optional[str] = None,
               only: Optional[str] = None) -> int:
    steps = STEPS if steps is None else steps
    total = len(steps)
    names = [s.name for s in steps]
    for wanted in (start, only):
        if wanted and wanted not in names:
            ctx.say(f"Langkah '{wanted}' tidak dikenal. Pilihan: {', '.join(names)}")
            return 2
    first = names.index(start) if start else 0
    for i, step in enumerate(steps):
        if (only and step.name != only) or i < first:
            continue
        ctx.say("")
        ctx.say(f"[{i + 1}/{total}] {step.title}")
        forced = step.name in (start, only)
        try:
            if not forced and step.check(ctx):
                ctx.say("  ✓ sudah selesai — dilewati")
                if not ctx.dry_run:
                    ctx.state.mark(step.name)
                    ctx.save()
                continue
            ctx.say(f"  {step.explain}")
            if ctx.dry_run:
                for line in step.plan(ctx):
                    ctx.say(f"  (dry-run) {line}")
                continue
            ctx.state.unmark(step.name)
            step.run(ctx)
            ctx.state.mark(step.name)
            ctx.save()
        except KeyboardInterrupt:
            _save_quietly(ctx)
            ctx.say(f"\nDihentikan. Lanjutkan kapan saja dengan:  {RESUME_HINT}")
            return 130
        except (StepError, TeamError, OSError, RuntimeError, ValueError, KeyError) as exc:
            if isinstance(exc, TeamError) and not isinstance(exc, StepError):
                exc = StepError(str(exc), cause="permintaan ke Discord gagal",
                                fix="periksa koneksi dan izin bot; lihat docs/INSTALL.md, bagian \"Bila ada yang tidak jalan\"")
            _save_quietly(ctx)
            report_failure(ctx, step, exc)
            return 1
    ctx.say("")
    ctx.say("(dry-run selesai: tidak ada perintah dijalankan dan tidak ada berkas diubah)" if ctx.dry_run
            else "Selesai." if not only else f"Langkah '{only}' selesai.")
    return 0


def _save_quietly(ctx: Context) -> None:
    try:
        ctx.save()
    except Exception:  # noqa: BLE001 — jangan menutupi galat aslinya
        pass


# --------------------------------------------------------------------------- CLI
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="setup.sh", description="Wizard pemasangan kit tim Hermes (bisa dilanjutkan).",
                                 epilog="Langkah: " + ", ".join(STEP_NAMES))
    ap.add_argument("--list", action="store_true", help="tampilkan langkah dan statusnya, lalu keluar")
    ap.add_argument("--dry-run", action="store_true", help="tampilkan rencana tiap langkah tanpa mengubah apa pun")
    ap.add_argument("--from", dest="start", metavar="LANGKAH", help="mulai (ulang) dari langkah ini")
    ap.add_argument("--only", metavar="LANGKAH", help="jalankan satu langkah saja")
    ap.add_argument("--answers", metavar="YAML", help="berkas jawaban (tanpa rahasia)")
    ap.add_argument("--non-interactive", action="store_true",
                    help=f"tanpa pertanyaan; rahasia dari env {ENV_BOT_TOKEN} dan {ENV_LLM_KEY}")
    ap.add_argument("--yes", action="store_true", help="terima semua nilai bawaan (persetujuan eksplisit tetap ditanyakan)")
    ap.add_argument("--user", default=None, help="user Linux untuk Hermes (bawaan: hermes)")
    ap.add_argument("--kit-dir", default=None, help="tujuan pemasangan kit (bawaan: /opt/hermes-team)")
    ap.add_argument("--reset", action="store_true", help="hapus state wizard dan mulai dari awal (tidak menghapus yang sudah terpasang)")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None, system: Optional[HostSystem] = None, *, state_file: Optional[Path] = None,
         out: Callable[[str], None] = print, input_fn: Callable[[str], str] = input,
         getpass_fn: Callable[[str], str] = getpass.getpass, environ: Optional[Dict[str, str]] = None,
         context_overrides: Optional[Dict[str, Any]] = None) -> int:
    args = parse_args(argv)
    system = system or HostSystem()
    read_only = args.list or args.dry_run
    if system.euid() != 0 and not read_only:
        out("Wizard ini harus dijalankan sebagai root. Jalankan:\n  sudo bash setup.sh")
        return 1
    if args.start and args.only:
        out("Pakai salah satu: --from atau --only.")
        return 2
    state = State(state_file or STATE_FILE)
    try:
        if args.reset and not read_only:
            state.reset()
            out(f"State wizard dihapus ({state.path}).")
        state.load()
        answers = load_answers(Path(args.answers)) if args.answers else {}
    except StepError as exc:
        out(f"✗ {exc.message}")
        for extra in (exc.cause, exc.fix):
            if extra:
                out(f"  {extra}")
        return 2
    user = args.user or state.data.get("linux_user") or "hermes"
    kit_dir = args.kit_dir or state.data.get("kit_dir") or "/opt/hermes-team"
    state.data["linux_user"], state.data["kit_dir"] = user, str(kit_dir)
    prompter = Prompter(answers, state.answers, args.non_interactive, args.yes, out, input_fn, getpass_fn, environ)
    ctx = Context(system, state, prompter, user=user, kit_dir=Path(kit_dir), dry_run=args.dry_run, out=out,
                  **(context_overrides or {}))
    prompter.out = ctx.say
    for name in (ENV_BOT_TOKEN, ENV_LLM_KEY):
        ctx.add_secret(str(prompter.environ.get(name) or "").strip())
    if args.list:
        list_steps(ctx, STEPS)
        return 0
    ctx.say(f"Wizard pemasangan kit tim Hermes v{KIT_VERSION}" + (" — DRY-RUN" if args.dry_run else ""))
    ctx.say(f"User Linux: {user} · direktori kit: {kit_dir} · state: {state.path}")
    return run_wizard(ctx, STEPS, start=args.start, only=args.only)


if __name__ == "__main__":
    sys.exit(main())
