"""Secret per profile: kebijakan nama, baca/tulis `.env` (0600, atomik, kunci berkas), indeks nama,
dan sinkronisasi `terminal.env_passthrough`.

Murni Python (PyYAML opsional, hanya untuk membaca config.yaml profile).
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from .redact import Redactor
from .state import atomic_write, file_lock, now_iso

ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|CREDENTIALS?|AUTH|PAT|PRIVATE_KEY|DSN)(?:_|$)")
MAX_VALUE = 8192
MANAGED_ENV = Path("/etc/hermes/.env")      # .env managed (milik root); hanya dibaca untuk penyamaran

# Nama yang dipakai Hermes sendiri: kredensial provider, gerbang akses platform, setelan runtime/sandbox.
HERMES_NAMES = {
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY", "XAI_API_KEY", "NOUS_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY",
    "PERPLEXITY_API_KEY", "COHERE_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY", "AZURE_OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
    "GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN", "COPILOT_API_KEY",
    "FIRECRAWL_API_KEY", "TAVILY_API_KEY", "EXA_API_KEY", "BRAVE_API_KEY", "SERPER_API_KEY",
    "ELEVENLABS_API_KEY", "FAL_KEY", "BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID", "API_SERVER_KEY",
    "SUDO_PASSWORD", "MESSAGING_CWD", "WEBHOOK_SECRET",
}
HERMES_PREFIXES = (
    "HERMES_", "_HERMES_", "GATEWAY_", "TERMINAL_", "AUXILIARY_", "DISCORD_", "TELEGRAM_", "SLACK_", "WHATSAPP_",
    "SIGNAL_", "MATRIX_", "MATTERMOST_", "EMAIL_", "TEAMS_", "API_SERVER_", "TEAMCTL_", "NOUS_", "IDP_",
)
SYSTEM_NAMES = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR",
    "TEMP", "TMP", "HOSTNAME", "IFS", "PS1", "PS2", "PS4", "ENV", "BASH_ENV", "PROMPT_COMMAND", "SHLVL", "EDITOR",
    "PAGER", "VISUAL", "MAIL", "DISPLAY", "VIRTUAL_ENV", "NODE_OPTIONS", "NODE_PATH", "PERL5LIB", "PERL5OPT",
    "RUBYOPT", "RUBYLIB", "CLASSPATH", "JAVA_TOOL_OPTIONS", "GIT_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE", "KUBECONFIG",
}
SYSTEM_PREFIXES = ("LD_", "DYLD_", "LC_", "PYTHON", "SSH_", "XDG_", "DOCKER_", "BASH_", "SUDO_", "GPG_")
SUGGEST = {
    "GITHUB_TOKEN": "GH_PAT", "GH_TOKEN": "GH_PAT", "COPILOT_GITHUB_TOKEN": "GH_PAT",
    "OPENAI_API_KEY": "MY_OPENAI_KEY", "ANTHROPIC_API_KEY": "MY_ANTHROPIC_KEY", "GEMINI_API_KEY": "MY_GEMINI_KEY",
    "GOOGLE_API_KEY": "MY_GOOGLE_KEY", "AWS_ACCESS_KEY_ID": "MY_AWS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY": "MY_AWS_SECRET", "HF_TOKEN": "MY_HF_TOKEN",
}
USAGE_HINT = {"GH_PAT": " Di sandbox: `GH_TOKEN=$GH_PAT gh pr list`."}


def _suggest(name: str) -> str:
    alt = SUGGEST.get(name) or f"MY_{name}"[:64]
    return f" Pakai nama lain, mis. `{alt}`.{USAGE_HINT.get(alt, '')}"


def check_env_name(name: Any, *, reserved: Iterable[str] = (), allow: Iterable[str] = (),
                   deny: Iterable[str] = (), upstream: Optional[Callable[[str], bool]] = None) -> tuple[bool, str]:
    """(boleh?, pesan). `reserved` (key LLM tim) dan `deny` selalu ditolak; `allow` hanya membuka
    nama yang biasanya ditolak karena dipakai Hermes/sistem."""
    if not isinstance(name, str) or not ENV_NAME_RE.match(name):
        return False, ("Nama harus HURUF BESAR, angka, dan garis bawah (2–64 karakter, diawali huruf), "
                       "mis. `GH_PAT`.")
    if name in {str(r) for r in reserved}:
        return False, f"`{name}` adalah API key LLM tim (dikelola admin) dan tidak bisa ditimpa." + _suggest(name)
    if name in {str(d) for d in deny}:
        return False, f"`{name}` dilarang oleh admin tim (secrets.deny_names)." + _suggest(name)
    if name in {str(a) for a in allow}:
        return True, ""
    if name in HERMES_NAMES or name.startswith(HERMES_PREFIXES):
        why = ("dipakai Hermes sebagai kredensial provider Copilot" if name in ("GITHUB_TOKEN", "GH_TOKEN",
                                                                                 "COPILOT_GITHUB_TOKEN")
               else "dipakai Hermes sendiri (kredensial provider / gerbang akses / setelan sandbox)")
        return False, f"`{name}` {why}, jadi tidak boleh ditimpa." + _suggest(name)
    if name in SYSTEM_NAMES or name.startswith(SYSTEM_PREFIXES):
        return False, f"`{name}` adalah variabel sistem; mengubahnya bisa merusak sandbox." + _suggest(name)
    if upstream is not None:
        try:
            if upstream(name):
                return False, f"`{name}` dikelola Hermes dan tidak diteruskan ke sandbox." + _suggest(name)
        except Exception:
            pass
    return True, ""


def check_env_value(value: Any, min_length: int = 6) -> tuple[bool, str]:
    if not isinstance(value, str):
        return False, "Nilai harus teks."
    if "\n" in value or "\r" in value or "\x00" in value:
        return False, ("Nilai multi-baris tidak didukung. Simpan sebagai base64 dengan nama berakhiran `_B64` "
                       "(mis. `base64 -w0 id_ed25519`).")
    if len(value) < int(min_length):
        return False, (f"Nilai terlalu pendek (minimal {int(min_length)} karakter) — nilai sependek itu tidak "
                       "bisa disamarkan dengan aman.")
    if len(value) > MAX_VALUE:
        return False, f"Nilai terlalu panjang (maks {MAX_VALUE} karakter)."
    return True, ""


# ----------------------------------------------------------------------------- .env
def parse_env_text(text: str) -> dict[str, str]:
    """Tokenizer .env yang sama dengan hermes_team.py / Hermes (bare, 'single', "double")."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not (sep and key.strip()):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            quoted, parts, i = value[1:-1], [], 0
            while i < len(quoted):
                escaped = quoted[i] == "\\" and quoted[i + 1:i + 2] in ('"', "\\")
                parts.append(quoted[i + 1] if escaped else quoted[i])
                i += 2 if escaped else 1
            value = "".join(parts)
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out[key.strip()] = value
    return out


def rewrite_env_text(text: str, key: str, value: Optional[str]) -> str:
    """Ganti/hapus satu key; baris lain (komentar, urutan) dipertahankan."""
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    line = None
    if value is not None:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        line = f'{key}="{escaped}"'
    out, written = [], False
    for existing in text.splitlines():
        if pattern.match(existing):
            if line is not None and not written:
                out.append(line)
                written = True
            continue
        out.append(existing)
    if line is not None and not written:
        out.append(line)
    return ("\n".join(out) + "\n") if out else ""


class SecretStore:
    """Secret satu profile: `<home>/.env` + indeks nama `<home>/team-guard/secrets.json`."""

    def __init__(self, home: Any, *, extra_env: Iterable[Path] = (MANAGED_ENV,)):
        self.home = Path(str(home))
        self.env_path = self.home / ".env"
        self.index_path = self.home / "team-guard" / "secrets.json"
        self.lock_path = self.home / "team-guard" / ".env.lock"
        self.extra_env = [Path(p) for p in extra_env]
        self._lock = threading.RLock()
        self._cache: Optional[tuple[Any, Redactor, dict[str, str]]] = None
        self.on_change: Optional[Callable[[dict[str, str]], None]] = None

    # ------------------------------------------------------------------ baca
    def read_env(self) -> dict[str, str]:
        try:
            return parse_env_text(self.env_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return {}

    def index(self) -> dict[str, dict]:
        try:
            keys = json.loads(self.index_path.read_text(encoding="utf-8")).get("keys", {})
        except (OSError, ValueError, AttributeError):
            return {}
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in keys.items()} if isinstance(keys, dict) else {}

    def names(self) -> list[str]:
        """Nama secret milik user (disimpan lewat /env-set) yang masih ada di .env."""
        env = self.read_env()
        return sorted(n for n in self.index() if n in env)

    def _stamp(self) -> tuple:
        stamps = []
        for path in (self.env_path, self.index_path, *self.extra_env):
            try:
                st = path.stat()
                stamps.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stamps.append(None)
        return tuple(stamps)

    def values(self, reserved: Iterable[str] = (), min_length: int = 6) -> dict[str, str]:
        """Nilai yang harus disamarkan: secret user, nama berbentuk secret di .env, dan key LLM tim."""
        env, index = self.read_env(), self.index()
        out = {k: v for k, v in env.items()
               if v and len(v) >= min_length and (k in index or SECRET_NAME_RE.search(k))}
        wanted = {str(r) for r in reserved}
        for path in self.extra_env:
            try:
                extra = parse_env_text(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            out.update({k: v for k, v in extra.items()
                        if v and len(v) >= min_length and (k in wanted or SECRET_NAME_RE.search(k))})
        for name in wanted:
            value = os.environ.get(name)
            if value and len(value) >= min_length:
                out.setdefault(name, value)
        return out

    def redactor(self, reserved: Iterable[str] = (), min_length: int = 6) -> Redactor:
        key = (self._stamp(), tuple(sorted(str(r) for r in reserved)), int(min_length))
        with self._lock:
            if self._cache is not None and self._cache[0] == key:
                return self._cache[1]
            values = self.values(reserved, min_length)
            redactor = Redactor(values, min_length)
            changed = self._cache is None or self._cache[2] != values
            self._cache = (key, redactor, values)
        if changed and self.on_change is not None:
            try:
                self.on_change(values)
            except Exception:
                pass
        return redactor

    # ------------------------------------------------------------------ tulis
    def _write_index(self, keys: dict[str, dict]) -> None:
        atomic_write(self.index_path, json.dumps({"version": 1, "keys": keys}, indent=2, sort_keys=True) + "\n",
                     0o600)

    def set(self, name: str, value: str, by: str = "") -> bool:
        """Simpan secret. True bila nama baru (bukan penggantian)."""
        with self._lock, file_lock(self.lock_path):
            try:
                text = self.env_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            fresh = name not in parse_env_text(text)
            atomic_write(self.env_path, rewrite_env_text(text, name, value), 0o600)
            keys = self.index()
            keys[name] = {"set_at": now_iso(), "by": str(by)}
            self._write_index(keys)
            self._cache = None
            return fresh

    def unset(self, name: str) -> bool:
        with self._lock, file_lock(self.lock_path):
            try:
                text = self.env_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            keys = self.index()
            existed = name in parse_env_text(text) and name in keys
            if not existed:
                return False
            atomic_write(self.env_path, rewrite_env_text(text, name, None), 0o600)
            keys.pop(name, None)
            self._write_index(keys)
            self._cache = None
            return True


# ----------------------------------------------------------------------------- env_passthrough
def read_passthrough(home: Path) -> Optional[list[str]]:
    """`terminal.env_passthrough` dari config.yaml profile; None bila tidak bisa dibaca."""
    try:
        import yaml  # PyYAML adalah dependensi Hermes; tanpa itu kita hanya memakai indeks sendiri
        data = yaml.safe_load((Path(home) / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    terminal = data.get("terminal") if isinstance(data, dict) else None
    current = terminal.get("env_passthrough") if isinstance(terminal, dict) else None
    return [str(x) for x in current] if isinstance(current, list) else []


def passthrough_plan(current: Optional[list[str]], wanted: Iterable[str],
                     removed: Iterable[str] = ()) -> Optional[list[str]]:
    """Daftar baru (urut, tanpa duplikat) atau None bila tidak ada yang perlu diubah."""
    gone = set(removed)
    base = [n for n in (current or []) if n not in gone]
    new = base + sorted(n for n in set(wanted) - set(base) - gone)
    return None if current is not None and new == list(current) else new


def passthrough_argv(hermes_bin: str, profile: str, names: list[str]) -> list[str]:
    return [hermes_bin, "-p", profile or "default", "config", "set", "terminal.env_passthrough",
            json.dumps(names, ensure_ascii=False)]


Runner = Callable[[list[str], dict], Awaitable[tuple[int, str]]]


async def run_argv(argv: list[str], env: dict, timeout: float = 120.0) -> tuple[int, str]:
    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env)
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    lines = (out or b"").decode("utf-8", "replace").strip().splitlines()
    return int(proc.returncode or 0), (lines[-1] if lines else "")


async def sync_passthrough(roster: Any, profile: str, store: SecretStore, *, removed: Iterable[str] = (),
                           runner: Optional[Runner] = None) -> tuple[bool, str]:
    """Samakan `terminal.env_passthrough` profile dengan nama secret tersimpan (lewat `hermes config set`,
    cara yang sama dengan hermes_team.py — tidak mengedit config.yaml langsung)."""
    plan = passthrough_plan(read_passthrough(store.home), store.names(), removed)
    if plan is None:
        return True, "sudah sesuai"
    env = dict(os.environ)
    env["HERMES_HOME"] = str(roster.hermes_root)
    code, detail = await (runner or run_argv)(passthrough_argv(roster.hermes_bin, profile, plan), env)
    return code == 0, detail if code else "diperbarui"
