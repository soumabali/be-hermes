"""Fixture bersama uji kit. Tidak butuh binary `hermes` asli maupun jaringan:

- `hermes` tiruan (skrip Python di tmp_path) mencatat argv dan meniru `config set`, `profile create
  --clone-from` (menyalin config.yaml/.env/SOUL.md/memories/skills seperti upstream), `plugins enable`.
  config.yaml ditulis sebagai JSON (subset YAML) supaya skrip tiruan tidak perlu PyYAML dan cepat.
- HERMES_HOME dan HERMES_TEAM_DIR menunjuk ke tmp_path.
- Discord: tests/fake_discord.py lewat env TEAMCTL_DISCORD_API.
"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
KIT_DIR = TESTS_DIR.parent
for _path in (str(KIT_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import fake_discord  # noqa: E402
import hermes_team as ht  # noqa: E402

BOT_TOKEN = "tiruan-token-bot-JANGAN-BOCOR-0123456789"
HOST_ONLY_SECRET = "rahasia-host-ZX81-jangan-diwarisi"

FAKE_HERMES = r'''#!__PYTHON__ -S
"""hermes tiruan untuk uji kit: mencatat argv, meniru subset CLI yang dipakai hermes_team.py."""
import json
import os
import re
import shutil
import sys

root = os.environ["HERMES_HOME"]
argv = sys.argv[1:]
with open(os.environ.get("FAKE_HERMES_LOG") or os.path.join(root, "fake-hermes.log"), "a") as fh:
    fh.write(json.dumps(argv) + "\n")

profile = "default"
if argv[:1] == ["-p"]:
    profile, argv = argv[1], argv[2:]


def home_of(name):
    return root if name == "default" else os.path.join(root, "profiles", name)


def load(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=1)


def fail(message):
    sys.stderr.write("Traceback (tiruan)\nError: " + message + "\n")
    sys.exit(1)


def listed(name):
    return [x for x in os.environ.get(name, "").split(",") if x]


home = home_of(profile)
if argv[:2] == ["config", "set"] and len(argv) == 4:
    key, raw = argv[2], argv[3]
    if key in listed("FAKE_HERMES_FAIL_KEYS"):
        fail("tidak bisa menyetel " + key)
    if profile != "default" and not os.path.isdir(home):
        fail("profile '%s' tidak ada" % profile)
    if key.isupper():
        path = os.path.join(home, ".env")
        try:
            with open(path) as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        lines = [line for line in lines if not line.startswith(key + "=")] + [key + "=" + raw]
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        sys.exit(0)
    if raw in ("true", "false"):
        value = raw == "true"
    elif raw[:1] in ("[", "{"):
        value = json.loads(raw)
    elif re.fullmatch(r"-?[0-9]+", raw):
        value = int(raw)            # seperti YAML: deretan digit jadi int
    else:
        value = raw
    path = os.path.join(home, "config.yaml")
    cfg = load(path)
    node = cfg
    parts = key.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    save(path, cfg)
elif argv[:2] == ["plugins", "enable"] and len(argv) == 3:
    if "plugins.enabled" in listed("FAKE_HERMES_FAIL_KEYS"):
        fail("tidak bisa mengaktifkan plugin")
    path = os.path.join(home, "config.yaml")
    cfg = load(path)
    enabled = cfg.setdefault("plugins", {}).setdefault("enabled", [])
    if argv[2] not in enabled:
        enabled.append(argv[2])
    save(path, cfg)
elif argv[:2] == ["profile", "create"] and "--clone-from" in argv:
    name = argv[2]
    source = home_of(argv[argv.index("--clone-from") + 1])
    dest = home_of(name)
    if os.path.exists(dest):
        fail("profile '%s' already exists" % name)
    if name in listed("FAKE_HERMES_FAIL_CREATE"):
        os.makedirs(dest)                      # gagal di tengah: folder setengah jadi tertinggal
        with open(os.path.join(dest, "config.yaml"), "w") as fh:
            fh.write("{}")
        fail("disk penuh saat membuat " + name)
    os.makedirs(os.path.join(dest, "memories"))
    os.makedirs(os.path.join(dest, "skills"))
    # upstream _CLONE_CONFIG_FILES + _CLONE_SUBDIR_FILES: .env IKUT disalin (mode 0600)
    for rel in ("config.yaml", ".env", "SOUL.md", "memories/MEMORY.md", "memories/USER.md"):
        if os.path.exists(os.path.join(source, rel)):
            shutil.copy2(os.path.join(source, rel), os.path.join(dest, rel))
    if os.path.exists(os.path.join(dest, ".env")):
        os.chmod(os.path.join(dest, ".env"), 0o600)
    cfg = load(os.path.join(dest, "config.yaml"))
    if isinstance(cfg.get("gateway"), dict):
        cfg["gateway"].pop("multiplex_profiles", None)   # upstream _strip_multiplex_flag
    save(os.path.join(dest, "config.yaml"), cfg)
    if os.path.isdir(os.path.join(source, "skills")):
        shutil.copytree(os.path.join(source, "skills"), os.path.join(dest, "skills"), dirs_exist_ok=True)
else:
    fail("perintah tidak ditiru: " + " ".join(argv))
'''

LLM = {
    "providers": {"td": {"api": "https://llm.example.internal/v1", "key_env": "TD_API_KEY"}},
    "models": {"td-normal": {"provider": "custom:td", "model": "td-normal"},
               "td-simple": {"provider": "custom:td", "model": "td-simple"}},
    "default_model": "td-simple", "admin_model": "td-normal", "shared_model": "td-simple",
    "lobby_model": "td-simple", "aux_model": "td-simple", "fallback": ["td-simple"],
}

BE1 = {"name": "be1", "display_name": "BE1", "discord_user_id": "500000000000000001",
       "channel_id": "600000000000000001", "model": "td-normal"}
BUDI = {"name": "budi", "display_name": "Budi", "discord_user_id": "500000000000000002",
        "channel_id": "600000000000000002"}
CITRA, DODI, EKA = "500000000000000003", "500000000000000004", "500000000000000005"


def base_team() -> dict:
    return {
        "team_name": "Backend", "guild_id": fake_discord.GUILD,
        "roles": {"member": fake_discord.ROLE_MEMBER, "admin": fake_discord.ROLE_ADMIN,
                  "entry": fake_discord.ROLE_ENTRY},
        "admins": [fake_discord.ADMIN], "category_id": fake_discord.CAT_HERMES,
        "channel_prefix": "hermes-", "profile_prefix": "be-", "template_profile": "be-template",
        "shared": {"profile": "team-shared", "channel_id": fake_discord.CH_SHARED},
        "admin": {"profile": "team-admin", "channel_id": fake_discord.CH_ADMIN},
        "llm": json.loads(json.dumps(LLM)),
    }


class Kit:
    """Satu instalasi tiruan: folder tim, HERMES_HOME, dan `hermes` tiruan."""

    def __init__(self, tmp_path: Path):
        self.team_dir = tmp_path / "team"
        self.root = tmp_path / "hermes-home"
        self.bin = tmp_path / "bin" / "hermes"
        self.log = tmp_path / "fake-hermes.log"
        self.team_file = self.team_dir / "team.yaml"
        self.members_file = self.team_dir / "members.yaml"
        self.state = self.team_dir / "state"
        for folder in (self.team_dir, self.root, self.bin.parent):
            folder.mkdir(parents=True)
        self.bin.write_text(FAKE_HERMES.replace("__PYTHON__", sys.executable), encoding="utf-8")
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        # Profile host: punya token bot dan satu secret yang TIDAK boleh diwarisi profile hasil clone.
        (self.root / "config.yaml").write_text(json.dumps({"model": {"default": "bawaan"}}), encoding="utf-8")
        (self.root / ".env").write_text(
            f"DISCORD_BOT_TOKEN={BOT_TOKEN}\nHOST_ONLY_SECRET={HOST_ONLY_SECRET}\n", encoding="utf-8")
        (self.root / "memories").mkdir()
        (self.root / "memories" / "MEMORY.md").write_text("catatan host\n", encoding="utf-8")
        self.write_team()
        self.write_members([BE1, BUDI])

    # ---- file tim
    def write_team(self, **overrides) -> dict:
        data = base_team()
        for key, value in overrides.items():
            if value is None:
                data.pop(key, None)
            elif isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key] = {**data[key], **value}
            else:
                data[key] = value
        self.team_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return data

    def write_members(self, members, models=None) -> None:
        body = {"members": [dict(m) for m in members]}
        if models:
            body["models"] = models
        self.members_file.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")

    def members_yaml(self) -> list:
        return (yaml.safe_load(self.members_file.read_text(encoding="utf-8")) or {}).get("members") or []

    def team(self) -> dict:
        return ht.load_team(self.team_file)

    # ---- hermes tiruan
    def hermes(self, dry_run: bool = False) -> "ht.Hermes":
        return ht.Hermes(str(self.bin), self.root, dry_run=dry_run, quiet=True)

    def calls(self) -> list:
        try:
            return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        except OSError:
            return []

    def config_sets(self) -> list:
        """[(profile, key, value)] dari semua `config set` yang tercatat."""
        out = []
        for argv in self.calls():
            profile = argv[1] if argv[:1] == ["-p"] else "default"
            rest = argv[2:] if argv[:1] == ["-p"] else argv
            if rest[:2] == ["config", "set"]:
                out.append((profile, rest[2], rest[3]))
        return out

    def creates(self) -> list:
        return [argv[2] for argv in self.calls() if argv[:2] == ["profile", "create"]]

    def clear_log(self) -> None:
        self.log.write_text("", encoding="utf-8")

    def home(self, profile: str) -> Path:
        return self.root if profile == "default" else self.root / "profiles" / profile

    def config(self, profile: str) -> dict:
        return json.loads((self.home(profile) / "config.yaml").read_text(encoding="utf-8"))

    def roster(self) -> dict:
        return json.loads((self.state / "roster.json").read_text(encoding="utf-8"))

    def make_profile(self, profile: str, env: str = "", memory: str = "") -> Path:
        """Folder profile yang sudah ada sebelum kit menyentuhnya (mis. sisa anggota lama)."""
        home = self.home(profile)
        (home / "memories").mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}", encoding="utf-8")
        (home / ".env").write_text(env, encoding="utf-8")
        if memory:
            (home / "memories" / "MEMORY.md").write_text(memory, encoding="utf-8")
        return home

    def make_skill(self, profile: str, name: str, body: str = "Langkah deploy.\n", category: str = "") -> Path:
        folder = self.home(profile) / "skills" / category / name if category else self.home(profile) / "skills" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: uji {name}\n---\n{body}", encoding="utf-8")
        return folder

    def fake_scanner(self, monkeypatch, tmp_path: Path, verdict: str = "safe", body: str = "") -> Path:
        """Pasang `tools.skills_guard` tiruan (lewat PYTHONPATH) yang mengembalikan verdict tertentu."""
        pkg = tmp_path / f"scanner-{verdict}" / "tools"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "skills_guard.py").write_text(body or (
            "class F:\n    severity='high'; file='SKILL.md'; line=3; description='temuan tiruan'\n"
            "class R:\n"
            f"    verdict={verdict!r}\n"
            f"    findings=[] if {verdict!r}=='safe' else [F()]\n"
            "def scan_skill(path, source='community'):\n    return R()\n"), encoding="utf-8")
        monkeypatch.setenv("PYTHONPATH", str(pkg.parent))
        return pkg.parent


@pytest.fixture
def kit(tmp_path, monkeypatch):
    k = Kit(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(k.root))
    monkeypatch.setenv("HERMES_TEAM_DIR", str(k.team_dir))
    monkeypatch.setenv("FAKE_HERMES_LOG", str(k.log))
    monkeypatch.setenv("HERMES_BIN", str(k.bin))
    for name in ("DISCORD_BOT_TOKEN", "PYTHONPATH", "HERMES_TEAM_DEBUG", "FAKE_HERMES_FAIL_KEYS",
                 "FAKE_HERMES_FAIL_CREATE"):
        monkeypatch.delenv(name, raising=False)
    return k


@pytest.fixture(autouse=True)
def _no_real_discord(monkeypatch):
    """Tanpa server tiruan, panggilan Discord apa pun harus gagal cepat, bukan keluar ke internet."""
    if "TEAMCTL_DISCORD_API" not in os.environ:
        monkeypatch.setenv("TEAMCTL_DISCORD_API", "http://127.0.0.1:9/api/v10")


@pytest.fixture
def discord(monkeypatch):
    """Server Discord tiruan; REST kit diarahkan ke sana lewat TEAMCTL_DISCORD_API."""
    state, base, server = fake_discord.start()
    monkeypatch.setenv("TEAMCTL_DISCORD_API", base)
    state.base = base
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def api(discord):
    """Klien DiscordAPI ke server tiruan; `sleep` dicatat, tidak benar-benar menunggu."""
    naps = []
    client = ht.DiscordAPI(BOT_TOKEN, sleep=naps.append)
    client.naps = naps
    return client
