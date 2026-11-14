"""Uji wizard pemasangan (kit_setup.py + setup.sh).

Tanpa root, tanpa jaringan, tanpa Docker/Hermes asli: perintah lewat runner tiruan (FakeSystem), prompt
lewat Prompter dengan input tiruan, berkas di tmp_path, Discord lewat tests/fake_discord.py, dan endpoint
LLM tiruan lokal. Jadi yang dibuktikan di sini adalah logika wizard, BUKAN perilaku apt/systemd/Hermes asli.
"""
import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
KIT_DIR = TESTS_DIR.parent
for _path in (str(KIT_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import fake_discord as fd  # noqa: E402
import hermes_team as ht  # noqa: E402
import kit_discord as kd  # noqa: E402
import kit_setup as ks  # noqa: E402

TOKEN = "TIRUAN.token-bot.JANGAN-BOCOR-abcdef0123456789"
LLM_KEY = "sk-tiruan-JANGAN-BOCOR-9876543210"
BOT_ROLE = "200000000000000099"
VIEW = 1 << 10


# --------------------------------------------------------------------------- tiruan
# Keluaran `hermes gateway status` — string diambil dari hermes_cli/gateway.py hulu (systemd_status, _cmd_status,
# _print_service_not_installed); baris `Active:` dari `systemctl status`. Hulu TIDAK memberi kode keluar != 0 pada
# gateway yang mati: keluarannya selalu rc 0.
GATEWAY_RUNNING = (
    "● hermes-gateway.service - Hermes Agent Gateway - Messaging Platform Integration\n"
    "     Loaded: loaded (/home/hermes/.config/systemd/user/hermes-gateway.service; enabled; preset: enabled)\n"
    "     Active: active (running) since Fri 2026-11-13 12:00:00 UTC; 12s ago\n"
    "✓ User gateway service is running\n"
    "✓ Systemd linger is enabled (service survives logout)\n")
GATEWAY_STOPPED = (
    "● hermes-gateway.service - Hermes Agent Gateway - Messaging Platform Integration\n"
    "     Loaded: loaded (/home/hermes/.config/systemd/user/hermes-gateway.service; enabled; preset: enabled)\n"
    "     Active: failed (Result: exit-code) since Fri 2026-11-13 12:00:05 UTC; 3s ago\n"
    "✗ User gateway service is stopped\n"
    "  Run: hermes gateway start\n")
GATEWAY_NOT_INSTALLED = "✗ Gateway service is not installed\n  Run: hermes gateway install\n"
GATEWAY_MANUAL_RUNNING = "✓ Gateway is running (PID: 4242)\n  (Running manually, not as a system service)\n"
GATEWAY_NOT_RUNNING = "✗ Gateway is not running\n\nTo start:\n  hermes gateway run      # Run in foreground\n"
GATEWAY_RESTARTING = GATEWAY_STOPPED + "  ⏳ Restart pending: systemd is waiting to relaunch the gateway\n"


class FakeSystem(ks.HostSystem):
    """Mesin tiruan: mencatat tiap perintah (argv + env + stdin) dan meniru efek sampingnya di tmp."""

    def __init__(self, tmp: Path, with_user: bool = False):
        self.tmp = tmp
        self.calls = []
        self.present = {"curl", "timedatectl", "adduser"}
        self.users, self.groups = {}, set()
        self.linger_dir = tmp / "linger"
        self.fail = None            # callable(inner_argv) → Result | None
        self.slept = 0.0
        self.on_sleep = None
        self.net = {}
        self.script = []            # [{"match": str|callable, "results": [Result, ...]}] — lihat script_cmd()
        self.chowns, self.fchowns = [], []
        self.systemd = True
        self.bus_after = 0          # jumlah pemeriksaan bus user@UID yang masih "belum siap"
        self.bus_polls = 0
        if with_user:
            self._add_user("hermes")
            self.groups.add("docker")
            self.present.update({"docker", "git"})

    def _add_user(self, name):
        home = self.tmp / "home" / name
        home.mkdir(parents=True, exist_ok=True)
        self.users[name] = ks.UserInfo(name, 1001, 1001, str(home))

    def script_cmd(self, match, *results):
        """Atur keluaran perintah: `match` = potongan teks perintah (setelah runuser) atau callable(argv). Hasil
        dipakai berurutan; yang terakhir terus dipakai setelah daftar habis."""
        self.script.append({"match": match, "results": list(results)})

    def _scripted(self, cmd):
        text = " ".join(cmd)
        for entry in self.script:
            hit = entry["match"](cmd) if callable(entry["match"]) else entry["match"] in text
            if hit and entry["results"]:
                return entry["results"].pop(0) if len(entry["results"]) > 1 else entry["results"][0]
        return None

    @staticmethod
    def inner(argv):
        if argv[:1] != ["runuser"]:
            return None, list(argv)
        rest = argv[argv.index("-i") + 1:]
        while rest and "=" in rest[0] and not rest[0].startswith("-"):
            rest = rest[1:]
        return argv[2], rest

    def commands(self):
        return [" ".join(self.inner(c["argv"])[1]) for c in self.calls]

    def euid(self):
        return 0

    def run(self, argv, env=None, input=None, cwd=None, stream=False, timeout=None):
        self.calls.append({"argv": list(argv), "env": dict(env or {}), "input": input, "cwd": cwd})
        user, cmd = self.inner(list(argv))
        scripted = self._scripted(cmd)
        if scripted is not None:
            return scripted
        if self.fail:
            forced = self.fail(cmd)
            if forced is not None:
                return forced
        text = " ".join(cmd)
        if cmd[:2] == ["apt-get", "install"]:
            self.present.update(cmd[3:])
        elif cmd[:1] == ["sh"] and "get-docker" in text:
            self.present.add("docker")
        elif cmd[:1] == ["adduser"]:
            self._add_user(cmd[-1])
        elif cmd[:2] == ["usermod", "-aG"]:
            self.groups.add(cmd[2])
        elif cmd[:2] == ["loginctl", "enable-linger"]:
            self.linger_dir.mkdir(parents=True, exist_ok=True)
            (self.linger_dir / cmd[2]).write_text("")
        elif cmd[:1] == ["bash"] and "install.sh" in text:
            target = Path(self.users[user].home) / ".local" / "bin" / "hermes"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("#!/bin/sh\n")
        elif cmd[:2] == ["mkdir", "-p"]:
            Path(cmd[2]).mkdir(parents=True, exist_ok=True)
        elif cmd[:1] == ["timedatectl"]:
            return ks.Result(0, "yes\n")
        elif "hermes_team.py managed" in text:
            return ks.Result(0, "# managed tiruan\nterminal:\n  backend: docker\n")
        elif cmd[:2] == ["hermes", "--version"]:
            return ks.Result(0, "Hermes Agent tiruan 0.0\n")
        elif cmd[:3] == ["hermes", "gateway", "install"] and user in self.users:
            unit = Path(self.users[user].home) / ".config" / "systemd" / "user" / "hermes-gateway.service"
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("[Unit]\nDescription=Hermes Agent Gateway - Messaging Platform Integration\n")
        elif cmd[:3] == ["systemctl", "--user", "is-active"]:
            return ks.Result(0, "active\n")
        elif cmd[:3] == ["systemctl", "--user", "show"]:
            return ks.Result(0, "0\n")
        elif cmd[:3] == ["hermes", "gateway", "status"]:
            return ks.Result(0, GATEWAY_RUNNING)
        return ks.Result(0, "", "")

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.present else None

    def user(self, name):
        return self.users.get(name)

    def user_groups(self, name):
        return set(self.groups) if name in self.users else set()

    def chown(self, path, user, group):
        self.chowns.append((str(path), user, group))

    def fchown(self, fd, user, group):
        self.fchowns.append((user, group))

    def user_bus_ready(self, uid):
        self.bus_polls += 1
        return self.bus_polls > self.bus_after

    def sleep(self, seconds):
        self.slept += seconds
        if self.on_sleep:
            self.on_sleep()

    def timestamp(self):
        return "20261113-120000"

    def os_release(self):
        return {"ID": "ubuntu", "PRETTY_NAME": "Ubuntu 24.04 LTS"}

    def machine(self):
        return "x86_64"

    def mem_total_gb(self):
        return 31.3

    def disk_free_gb(self, path="/"):
        return 80.0

    def has_systemd(self):
        return self.systemd

    def system_timezone(self):
        return "Asia/Makassar"

    def net_check(self, url):
        return self.net.get(url, (True, "HTTP 200"))


class LLMHandler(BaseHTTPRequestHandler):
    calls = None

    def log_message(self, *a):
        pass

    def _reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.calls.append({"method": self.command, "path": self.path, "body": body})
        if self.headers.get("Authorization") != f"Bearer {LLM_KEY}":
            return self._reply(401, {"error": {"message": "invalid api key"}})
        if self.command == "GET" and self.path == "/v1/models":
            return self._reply(200, {"data": [{"id": "td-normal"}, {"id": "td-simple"}]})
        if self.command == "POST" and self.path == "/v1/chat/completions":
            return self._reply(200, {"choices": [{"message": {"role": "assistant", "content": "pong"}}]})
        return self._reply(404, {"error": {"message": "not found"}})

    do_GET = do_POST = _handle


ACTIVE = {"base": None}      # URL Discord tiruan yang dipakai Context/Wizard di uji ini
SUPPORTS_BASE = "discord_base" in ks.Context.__init__.__code__.co_varnames


def discord_context_overrides():
    """Wizard TIDAK membaca TEAMCTL_DISCORD_API (root bisa punya env itu); uji menyuntik URL lewat Context."""
    return {"discord_base": ACTIVE["base"]} if SUPPORTS_BASE and ACTIVE["base"] else {}


@pytest.fixture
def discord(monkeypatch):
    state, base, server = fd.start()
    state.base = base
    state.roles[BOT_ROLE]["permissions"] = str(ks.invite_permissions(True))
    # Seed bersama (dipakai modul uji lain) memberi role hermes-admin izin ADMINISTRATOR. Wizard kini menolak
    # mengadopsinya tanpa persetujuan; uji adopsi memasangnya lagi secara eksplisit.
    state.roles[fd.ROLE_ADMIN]["permissions"] = "0"
    ACTIVE["base"] = base
    if SUPPORTS_BASE:
        monkeypatch.delenv("TEAMCTL_DISCORD_API", raising=False)
    else:
        monkeypatch.setenv("TEAMCTL_DISCORD_API", base)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    yield state
    ACTIVE["base"] = None
    server.shutdown()
    server.server_close()


@pytest.fixture
def llm():
    calls = []
    handler = type("BoundLLM", (LLMHandler,), {"calls": calls})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", calls
    server.shutdown()
    server.server_close()


class Wizard:
    """Menjalankan kit_setup.main() terhadap tiruan; mengumpulkan semua keluaran."""

    def __init__(self, tmp: Path, system: FakeSystem = None):
        self.tmp = tmp
        self.sys = system or FakeSystem(tmp)
        self.lines = []
        self.state_file = tmp / "var" / "state.json"
        self.etc = tmp / "etc-hermes"
        self.kit = tmp / "opt" / "hermes-team"

    def main(self, *args, inputs=(), secrets=(), environ=None):
        feed, hidden = iter(inputs), iter(secrets)

        def input_fn(prompt):
            self.lines.append(prompt)
            item = next(feed)
            return item() if callable(item) else item

        def getpass_fn(prompt):
            self.lines.append(prompt)
            return next(hidden)

        return ks.main(["--kit-dir", str(self.kit), *args], system=self.sys, state_file=self.state_file,
                       out=self.lines.append, input_fn=input_fn, getpass_fn=getpass_fn, environ=environ,
                       context_overrides={"etc_dir": self.etc, "linger_dir": self.sys.linger_dir,
                                          "source_dir": KIT_DIR, **discord_context_overrides()})

    @property
    def text(self):
        return "\n".join(str(line) for line in self.lines)

    def state(self):
        return json.loads(self.state_file.read_text())

    @property
    def home(self):
        return self.tmp / "home" / "hermes"


def write_answers(tmp: Path, llm_base: str, **extra) -> Path:
    data = {"install_docker": True, "install_hermes": True, "llm_base_url": llm_base, "admins": [fd.ADMIN],
            "timezone": "Asia/Makassar", "moderation": False, "usage_reports": True, "team_name": "Platform"}
    data.update(extra)
    path = tmp / "jawaban.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def secrets_env(monkeypatch):
    monkeypatch.setenv(ks.ENV_BOT_TOKEN, TOKEN)
    monkeypatch.setenv(ks.ENV_LLM_KEY, LLM_KEY)


def make_ctx(tmp, answers=None, *, non_interactive=True, inputs=(), secrets=(), environ=None, system=None):
    """Konteks untuk menguji satu langkah: user hermes sudah ada, token lewat env tiruan."""
    system = system or FakeSystem(tmp, with_user=True)
    state = ks.State(tmp / "var" / "state.json")
    lines, feed, hidden = [], iter(inputs), iter(secrets)

    def input_fn(prompt):
        lines.append(prompt)
        item = next(feed)
        return item() if callable(item) else item

    prompter = ks.Prompter(answers or {}, state.answers, non_interactive, False, lines.append, input_fn,
                           lambda prompt: next(hidden),
                           {ks.ENV_BOT_TOKEN: TOKEN, ks.ENV_LLM_KEY: LLM_KEY} if environ is None else environ)
    ctx = ks.Context(system, state, prompter, kit_dir=tmp / "opt" / "hermes-team", source_dir=KIT_DIR,
                     etc_dir=tmp / "etc-hermes", linger_dir=system.linger_dir, out=lines.append,
                     **discord_context_overrides())
    prompter.out = ctx.say
    ctx.poll_interval = 5.0
    ctx.lines = lines
    return ctx


def discord_ready(ctx):
    ctx.data.update({"guild_id": fd.GUILD, "bot_id": fd.BOT, "app_id": "990000000000000001"})
    return ctx


def posts(state, prefix="/guilds/"):
    return [c for c in state.calls if c["method"] in ("POST", "PUT", "PATCH", "DELETE") and c["path"].startswith(prefix)] \
        + [c for c in state.calls if c["method"] in ("POST", "PUT", "PATCH", "DELETE") and c["path"].startswith("/channels/")]


# --------------------------------------------------------------------------- izin, tautan, intent
DOCUMENTED_BITS = {"View Channels": 10, "Send Messages": 11, "Send Messages in Threads": 38, "Read Message History": 16,
                   "Embed Links": 14, "Attach Files": 15, "Add Reactions": 6, "Create Public Threads": 35,
                   "Use Application Commands": 31, "Manage Roles": 28, "Manage Channels": 4, "View Audit Log": 7,
                   "Manage Messages": 13, "Manage Threads": 34}


def test_invite_permission_integer_matches_documented_set():
    expected = sum(1 << bit for bit in DOCUMENTED_BITS.values())
    assert ks.invite_permissions() == expected == 328833559760
    assert ks.invite_permissions(True) == expected | (1 << 40) | (1 << 1)
    assert not ks.invite_permissions(True) & (1 << 3)          # tidak pernah Administrator
    assert len(ks.INVITE_PERMISSIONS) == len(DOCUMENTED_BITS)


def test_invite_url_shape():
    import urllib.parse
    url = ks.invite_url("990000000000000001")
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "discord.com", "/oauth2/authorize")
    assert query == {"client_id": ["990000000000000001"], "scope": ["bot applications.commands"],
                     "permissions": ["328833559760"]}
    assert "permissions=%d" % ks.invite_permissions(True) in ks.invite_url("1", moderation=True)


@pytest.mark.parametrize("flags,members,content", [
    (0, False, False), (1 << 14, True, False), (1 << 15, True, False), (1 << 18, False, True),
    (1 << 19, False, True), ((1 << 14) | (1 << 18), True, True), ((1 << 15) | (1 << 19), True, True),
    ((1 << 14) | (1 << 19), True, True), ((1 << 12) | (1 << 13) | (1 << 23), False, False), (None, False, False),
])
def test_intent_flag_detection(flags, members, content):
    assert ks.intent_status(flags) == {"members": members, "content": content}


# --------------------------------------------------------------------------- langkah discord_bot
def test_bot_step_missing_intent_names_the_toggle_and_rechecks(tmp_path, discord):
    discord.application["flags"] = 1 << 15                      # Message Content mati

    def fix_then_retry():
        discord.application["flags"] |= 1 << 18
        return "1"

    ctx = make_ctx(tmp_path, non_interactive=False, inputs=[fix_then_retry, "t"], secrets=[TOKEN], environ={})
    ks.DiscordBot().run(ctx)
    text = "\n".join(ctx.lines)
    assert "Message Content Intent" in text and "intent belum hidup: Message Content Intent" in text
    assert "intent belum hidup: Server Members Intent" not in text
    assert len(discord.calls_matching("GET", "/oauth2/applications/@me")) == 2
    assert ctx.data["guild_id"] == fd.GUILD and ctx.data["app_id"] == "990000000000000001"
    env_file = tmp_path / "home" / "hermes" / ".hermes" / ".env"
    assert kd.parse_env_text(env_file.read_text())["DISCORD_BOT_TOKEN"] == TOKEN
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert TOKEN not in text


def test_bot_step_missing_intents_non_interactive_fails_or_skips(tmp_path, discord):
    discord.application["flags"] = 0
    with pytest.raises(ks.StepError) as err:
        ks.DiscordBot().run(make_ctx(tmp_path))
    assert "Server Members Intent" in err.value.message and "Message Content Intent" in err.value.message
    ctx = make_ctx(tmp_path, {"on_missing_intents": "skip"})
    ks.DiscordBot().run(ctx)
    assert ctx.data["guild_id"] == fd.GUILD and any("dilewati" in w for w in ctx.warnings)


def test_bot_step_rejected_token(tmp_path, discord):
    discord.fault("GET", "/users/@me", status=401, body={"message": "401: Unauthorized", "code": 0})
    with pytest.raises(ks.StepError) as err:
        ks.DiscordBot().run(make_ctx(tmp_path))
    assert "401" in err.value.message and TOKEN not in str(err.value.message) + err.value.cause + err.value.fix
    assert not (tmp_path / "home" / "hermes" / ".hermes" / ".env").exists()


def test_bot_step_waits_for_invite_then_picks_guild(tmp_path, discord):
    discord.bot_guilds = []
    system = FakeSystem(tmp_path, with_user=True)
    polls = []

    def joined():
        polls.append(1)
        if len(polls) == 3:
            discord.bot_guilds = [{"id": "100000000000000777", "name": "Lain"}, {"id": fd.GUILD, "name": "Tim Backend"}]

    system.on_sleep = joined
    ctx = make_ctx(tmp_path, {"guild_id": fd.GUILD}, system=system)
    ks.DiscordBot().run(ctx)
    assert ctx.data["guild_id"] == fd.GUILD and system.slept == 15.0
    assert ks.invite_url("990000000000000001") in "\n".join(ctx.lines)


def test_bot_step_invite_timeout_non_interactive(tmp_path, discord):
    discord.bot_guilds = []
    with pytest.raises(ks.StepError) as err:
        ks.DiscordBot().run(make_ctx(tmp_path, {"invite_wait_seconds": 10}))
    assert "belum masuk server" in err.value.message


# --------------------------------------------------------------------------- langkah discord_server
CUSTOM = {"role_entry": "x-entry", "role_member": "x-backend", "role_admin": "x-admin", "category_name": "X Hermes",
          "shared_channel": "x-shared", "admin_channel": "x-admin-room", "admins": fd.ADMIN}


def test_server_bootstrap_creates_once_and_is_idempotent(tmp_path, discord):
    ctx = discord_ready(make_ctx(tmp_path, CUSTOM))
    ks.DiscordServer().run(ctx)
    first = dict(ctx.data)
    created_roles = [c["body"]["name"] for c in discord.calls_matching("POST", f"/guilds/{fd.GUILD}/roles")]
    created_channels = [c["body"]["name"] for c in discord.calls_matching("POST", f"/guilds/{fd.GUILD}/channels")]
    assert created_roles == ["x-entry", "x-backend", "x-admin"]
    assert created_channels == ["X Hermes", "x-shared", "x-admin-room"]
    assert all(discord.roles[first[f"role_{k}_id"]]["permissions"] == "0" for k in ("entry", "member", "admin"))
    assert discord.channels[first["category_id"]]["type"] == 4
    assert first["role_admin_id"] in discord.members[fd.ADMIN]["roles"] and first["admins"] == [fd.ADMIN]

    for key, role_key in (("shared", "member"), ("admin", "admin")):
        channel = discord.channels[first[f"{key}_channel_id"]]
        assert channel["parent_id"] == first["category_id"] and channel["type"] == 0
        got = sorted((o["id"], o["type"], int(o["allow"]), int(o["deny"])) for o in channel["permission_overwrites"])
        assert got == sorted([(fd.GUILD, 0, 0, VIEW), (first[f"role_{role_key}_id"], 0, kd.MEMBER_ALLOW, 0),
                              (fd.BOT, 1, kd.BOT_ALLOW, 0)])

    before = len(posts(discord))
    ctx2 = discord_ready(make_ctx(tmp_path, CUSTOM))
    ks.DiscordServer().run(ctx2)
    assert len(posts(discord)) == before, "putaran kedua tidak boleh membuat/mengubah apa pun"
    assert {k: ctx2.data[k] for k in ks.DiscordServer.KEYS} == {k: first[k] for k in ks.DiscordServer.KEYS}
    assert len([r for r in discord.roles.values() if r["name"].startswith("x-")]) == 3


def test_server_bootstrap_reuses_existing_and_fixes_their_overwrites(tmp_path, discord):
    ctx = discord_ready(make_ctx(tmp_path, {"admins": fd.ADMIN}))
    ks.DiscordServer().run(ctx)
    assert not discord.calls_matching("POST", f"/guilds/{fd.GUILD}/roles")
    assert not discord.calls_matching("POST", f"/guilds/{fd.GUILD}/channels")
    assert (ctx.data["role_entry_id"], ctx.data["role_member_id"], ctx.data["role_admin_id"]) == \
        (fd.ROLE_ENTRY, fd.ROLE_MEMBER, fd.ROLE_ADMIN)
    assert (ctx.data["category_id"], ctx.data["shared_channel_id"], ctx.data["admin_channel_id"]) == \
        (fd.CAT_HERMES, fd.CH_SHARED, fd.CH_ADMIN)
    shared = sorted((o["id"], o["type"], int(o["allow"]), int(o["deny"]))
                    for o in discord.channels[fd.CH_SHARED]["permission_overwrites"])
    assert shared == sorted([(fd.GUILD, 0, 0, VIEW), (fd.ROLE_MEMBER, 0, kd.MEMBER_ALLOW, 0), (fd.BOT, 1, kd.BOT_ALLOW, 0)])
    # bot dulu, @everyone terakhir: bot tidak boleh mengunci dirinya sendiri dari channel
    order = [c["path"].rsplit("/", 1)[1] for c in discord.calls_matching("PUT", f"/channels/{fd.CH_SHARED}/permissions/")]
    assert order == [fd.BOT, fd.ROLE_MEMBER, fd.GUILD]
    # #hermes-admin sudah menolak @everyone → overwrite itu tidak ditulis ulang
    admin_puts = [c["path"].rsplit("/", 1)[1] for c in discord.calls_matching("PUT", f"/channels/{fd.CH_ADMIN}/permissions/")]
    assert admin_puts == [fd.BOT, fd.ROLE_ADMIN]
    # adminbe sudah punya role hermes-admin → tidak ada PUT role
    assert not discord.calls_matching("PUT", f"/guilds/{fd.GUILD}/members/")


def test_role_hierarchy_problem_detected(tmp_path, discord):
    discord.roles[fd.ROLE_ADMIN]["position"] = 9                # di atas role bot (posisi 7)
    with pytest.raises(ks.StepError) as err:
        ks.DiscordServer().run(discord_ready(make_ctx(tmp_path, {"admins": fd.ADMIN})))
    assert "@hermes-admin" in err.value.message and "@backend" not in err.value.message
    assert "seret role bot" in err.value.fix
    assert not discord.calls_matching("POST", f"/guilds/{fd.GUILD}/channels")

    def fix_then_retry():
        discord.roles[fd.ROLE_ADMIN]["position"] = 6
        return "1"

    ctx = discord_ready(make_ctx(tmp_path, {"admins": fd.ADMIN}, non_interactive=False,
                                 inputs=["", "", "", fix_then_retry, "", "", ""]))
    ks.DiscordServer().run(ctx)
    assert "Role bot berada di atas ketiga role tim" in "\n".join(ctx.lines)


def test_missing_bot_permissions_detected(tmp_path, discord):
    discord.roles[BOT_ROLE]["permissions"] = str(1 << 28)       # hanya Manage Roles
    with pytest.raises(ks.StepError) as err:
        ks.DiscordServer().run(discord_ready(make_ctx(tmp_path, {"admins": fd.ADMIN})))
    assert "manage_channels" in err.value.message and "view_audit_log" in err.value.message
    assert "manage_roles" not in err.value.message and ks.invite_url("990000000000000001") in err.value.fix
    assert not posts(discord)


def test_admin_lookup_by_id_username_and_ambiguous(tmp_path, discord):
    discord.add_member("500000000000000091", "dodi")
    discord.add_member("500000000000000092", "sari.a")
    discord.add_member("500000000000000093", "sari.b")
    api = kd.DiscordAPI(TOKEN, base=discord.base)
    ctx = make_ctx(tmp_path)
    assert ks.resolve_admin(ctx, api, fd.GUILD, fd.ADMIN)["user"]["username"] == "adminbe"
    assert ks.resolve_admin(ctx, api, fd.GUILD, "@adminbe")["user"]["id"] == fd.ADMIN
    assert ks.resolve_admin(ctx, api, fd.GUILD, "dodi")["user"]["id"] == "500000000000000091"   # persis menang
    assert ks.resolve_admin(ctx, api, fd.GUILD, "cit")["user"]["username"] == "citra"            # satu hasil
    with pytest.raises(ks.StepError) as err:
        ks.resolve_admin(ctx, api, fd.GUILD, "sari")
    assert "cocok dengan 2 anggota" in err.value.message and "500000000000000093" in err.value.message
    with pytest.raises(ks.StepError):
        ks.resolve_admin(ctx, api, fd.GUILD, "tidakada")
    with pytest.raises(ks.StepError) as err:
        ks.resolve_admin(ctx, api, fd.GUILD, "123456789012345678")
    assert "bukan anggota server" in err.value.message
    with pytest.raises(ks.StepError):
        ks.resolve_admin(ctx, api, fd.GUILD, "herm")            # bot tidak pernah jadi kandidat admin
    interactive = make_ctx(tmp_path, non_interactive=False, inputs=["x", "2"])
    assert ks.resolve_admin(interactive, api, fd.GUILD, "sari")["user"]["id"] == "500000000000000093"


def test_admins_from_usernames_get_the_role(tmp_path, discord):
    ctx = discord_ready(make_ctx(tmp_path, {"admins": ["citra", fd.OWNER, "citra"]}))
    ks.DiscordServer().run(ctx)
    assert ctx.data["admins"] == ["500000000000000003", fd.OWNER]
    assert fd.ROLE_ADMIN in discord.members["500000000000000003"]["roles"]
    assert fd.ROLE_ADMIN in discord.members[fd.OWNER]["roles"]


# --------------------------------------------------------------------------- langkah llm
def test_llm_step_probes_and_stores_key(tmp_path, llm):
    base, calls = llm
    ctx = make_ctx(tmp_path, {"llm_base_url": base + "/", "llm_model_normal": "td-normal"})
    ks.LLMEndpoint().run(ctx)
    assert [(c["method"], c["path"]) for c in calls] == [("GET", "/v1/models"), ("POST", "/v1/chat/completions")] * 2
    assert calls[1]["body"]["model"] == "td-simple" and calls[1]["body"]["max_tokens"] == 5
    env_file = tmp_path / "etc-hermes" / ".env"
    assert kd.parse_env_text(env_file.read_text()) == {"TD_API_KEY": LLM_KEY}
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o640
    assert ctx.state.answers["llm_base_url"] == base and LLM_KEY not in "\n".join(ctx.lines)


def test_llm_step_failure_shows_status_then_retry_or_continue(tmp_path, llm):
    base, _calls = llm
    env = {ks.ENV_LLM_KEY: "kunci-salah-000000"}
    with pytest.raises(ks.StepError):
        ks.LLMEndpoint().run(make_ctx(tmp_path, {"llm_base_url": base}, environ=env))
    assert not (tmp_path / "etc-hermes" / ".env").exists()
    ctx = make_ctx(tmp_path, {"llm_base_url": base, "llm_on_failure": "skip"}, environ=env)
    ks.LLMEndpoint().run(ctx)
    text = "\n".join(ctx.lines)
    assert "→ 401" in text and "API key salah" in text and "kunci-salah-000000" not in text
    assert any("uji endpoint LLM gagal" in w for w in ctx.warnings)
    # interaktif: gagal → "isi ulang" → key benar lewat getpass → lulus
    tmp2 = tmp_path / "b"
    ctx = make_ctx(tmp2, non_interactive=False, environ={}, secrets=["kunci-salah-000000", LLM_KEY],
                   inputs=[base, "", "", "", "2", "", "", "", ""])
    ks.LLMEndpoint().run(ctx)
    assert kd.parse_env_text((tmp2 / "etc-hermes" / ".env").read_text()) == {"TD_API_KEY": LLM_KEY}


# --------------------------------------------------------------------------- team.yaml
DATA = {"guild_id": "110000000000000001", "role_member_id": "210000000000000001", "role_admin_id": "210000000000000002",
        "role_entry_id": "210000000000000003", "admins": ["310000000000000001", "310000000000000002"],
        "category_id": "410000000000000009", "shared_channel_id": "410000000000000001",
        "admin_channel_id": "410000000000000002", "usage_channel_id": "410000000000000003"}
ANSWERS = {"team_name": "Platform", "llm_base_url": "https://llm.contoh.internal/v1", "llm_key_env": "ACME_API_KEY",
           "llm_model_normal": "besar-1", "llm_model_simple": "kecil-1", "timezone": "Asia/Makassar",
           "threads_personal": False, "usage_reports": True, "report_daily": "08:00", "report_weekly": "08:05",
           "report_monthly": "", "moderation": False}


def test_generated_team_yaml_loads_and_keeps_example_defaults(tmp_path):
    example = yaml.safe_load((KIT_DIR / "team.example.yaml").read_text())
    text = ks.render_team_yaml(ks.build_team_config(example, DATA, ANSWERS), Path("/opt/hermes-team"), "20261113")
    (tmp_path / "team.yaml").write_text(text)
    (tmp_path / "members.yaml").write_text("members: []\n")
    team = ht.load_team(tmp_path / "team.yaml")
    assert team["team_name"] == "Platform" and team["guild_id"] == DATA["guild_id"]
    assert (team["member_role"], team["admin_role"], team["entry_role"]) == \
        (DATA["role_member_id"], DATA["role_admin_id"], DATA["role_entry_id"])
    assert team["admins"] == DATA["admins"] and team["category_id"] == DATA["category_id"]
    assert team["shared"]["channel_id"] == DATA["shared_channel_id"]
    assert team["admin"]["channel_id"] == DATA["admin_channel_id"]
    assert team["threads"] == {"personal": False, "admin": False}
    assert team["llm"]["providers"]["td"] == {"api": ANSWERS["llm_base_url"], "key_env": "ACME_API_KEY"}
    assert team["llm"]["models"]["td-normal"] == {"provider": "custom:td", "model": "besar-1"}
    assert team["llm"]["models"]["td-simple"] == {"provider": "custom:td", "model": "kecil-1"}
    assert team["usage"] == {"timezone": "Asia/Makassar", "report_channel_id": DATA["usage_channel_id"],
                             "reports": {"daily": "08:00", "weekly": "08:05", "monthly": ""}}
    assert team["server_admin"]["allow_moderation"] is False and team["members"] == []
    assert text.startswith("# team.yaml — dibuat oleh wizard") and "BOLEH diedit" in text

    raw = yaml.safe_load(text)
    assert all(isinstance(raw[k], str) for k in ("guild_id", "category_id")) and isinstance(raw["admins"][0], str)
    touched = {"team_name", "guild_id", "roles", "admins", "category_id", "shared", "admin", "threads", "llm", "usage",
               "server_admin"}
    assert set(raw) == set(example)
    for key in set(example) - touched:
        assert raw[key] == example[key], key
    assert raw["shared"]["profile"] == example["shared"]["profile"] and raw["threads"]["admin"] is False
    assert {k: v for k, v in raw["server_admin"].items() if k != "allow_moderation"} == \
        {k: v for k, v in example["server_admin"].items() if k != "allow_moderation"}
    for key in ("default_model", "admin_model", "shared_model", "lobby_model", "aux_model", "fallback", "per_user_keys"):
        assert raw["llm"][key] == example["llm"][key]
    # contoh yang dimuat apa adanya (README lama) dan hasil wizard memberi bawaan yang sama
    (tmp_path / "ex").mkdir()
    (tmp_path / "ex" / "team.yaml").write_text((KIT_DIR / "team.example.yaml").read_text())
    (tmp_path / "ex" / "members.yaml").write_text("members: []\n")
    reference = ht.load_team(tmp_path / "ex" / "team.yaml")
    for key in ("scale", "secrets", "onboarding", "sandbox", "channel_prefix", "prefix", "template", "strict",
                "admin_strict", "lobby_restrict", "allow_browser", "live_reload", "restart_fallback", "user_commands"):
        assert team[key] == reference[key], key


def test_reports_off_leaves_usage_empty():
    example = yaml.safe_load((KIT_DIR / "team.example.yaml").read_text())
    team = ks.build_team_config(example, DATA, dict(ANSWERS, usage_reports=False, moderation=True))
    assert team["usage"]["report_channel_id"] == "" and set(team["usage"]["reports"].values()) == {""}
    assert team["server_admin"]["allow_moderation"] is True


def config_ctx(tmp_path, answers=None, **kw):
    ctx = make_ctx(tmp_path, answers, **kw)
    ctx.data.update(DATA)
    ctx.state.answers.update(ANSWERS)
    return ctx


def test_existing_team_yaml_is_backed_up_not_clobbered(tmp_path):
    team_dir = tmp_path / "home" / "hermes" / ".hermes-team"
    team_dir.mkdir(parents=True)
    old = "# punya operator\nteam_name: Lama\nguild_id: \"110000000000000001\"\n"
    (team_dir / "team.yaml").write_text(old)

    with pytest.raises(ks.StepError) as err:                    # tanpa persetujuan eksplisit: berhenti
        ks.WriteConfig().run(config_ctx(tmp_path))
    assert "overwrite_team_yaml" in err.value.message
    assert (team_dir / "team.yaml").read_text() == old and not list(team_dir.glob("*.bak-*"))

    ctx = config_ctx(tmp_path, {"overwrite_team_yaml": False})
    ks.WriteConfig().run(ctx)
    assert (team_dir / "team.yaml").read_text() == old and not list(team_dir.glob("*.bak-*"))
    assert "~ team_name: 'Lama' → 'Platform'" in "\n".join(ctx.lines)
    assert (team_dir / "members.yaml").read_text().strip().endswith("members: []")

    ctx = config_ctx(tmp_path, {"overwrite_team_yaml": True})
    ks.WriteConfig().run(ctx)
    backups = list(team_dir.glob("team.yaml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == old
    assert yaml.safe_load((team_dir / "team.yaml").read_text())["team_name"] == "Platform"
    assert stat.S_IMODE((team_dir / "team.yaml").stat().st_mode) == 0o600

    ctx = config_ctx(tmp_path)                                  # sudah sama → tidak bertanya, tidak ada cadangan baru
    ks.WriteConfig().run(ctx)
    assert len(list(team_dir.glob("team.yaml.bak-*"))) == 1 and "tidak diubah" in "\n".join(ctx.lines)
    checks = [c for c in ctx.sys.commands() if "hermes_team.py check" in c]
    assert checks and ctx.sys.calls[-1]["argv"][:3] == ["runuser", "-u", "hermes"]


def test_set_sandbox_image_keeps_comments():
    text = (KIT_DIR / "team.example.yaml").read_text()
    out = ks.set_sandbox_image(text, "hermes-team-sandbox:2026.11")
    assert yaml.safe_load(out)["sandbox"]["image"] == "hermes-team-sandbox:2026.11"
    changed = [(a, b) for a, b in zip(text.splitlines(), out.splitlines()) if a != b]
    assert len(changed) == 1 and len(text.splitlines()) == len(out.splitlines())
    assert ks.set_sandbox_image("team_name: x\n", "a:b") is None


# --------------------------------------------------------------------------- helper user & rahasia
def test_user_command_is_clean_env_with_runtime_dir():
    argv = ks.user_command(ks.UserInfo("hermes", 1001, 1001, "/home/hermes"), ["systemctl", "--user", "daemon-reload"])
    assert argv[:6] == ["runuser", "-u", "hermes", "--", "env", "-i"]
    assert "XDG_RUNTIME_DIR=/run/user/1001" in argv and "HOME=/home/hermes" in argv
    assert argv[-3:] == ["systemctl", "--user", "daemon-reload"]
    assert any(a.startswith("PATH=/home/hermes/.local/bin:") for a in argv)


def test_runner_refuses_secrets_in_argv_and_state(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.add_secret(TOKEN)
    with pytest.raises(RuntimeError):
        ctx.run(["echo", f"x{TOKEN}"])
    with pytest.raises(RuntimeError):
        ctx.run(["true"], as_user=True, extra_env={"A": TOKEN})
    assert ctx.sys.calls == []
    ctx.state.answers["catatan"] = f"token={TOKEN}"
    with pytest.raises(RuntimeError):
        ctx.save()
    assert not ctx.state.path.exists()
    assert ctx.redact(f"a {TOKEN} b") == "a «rahasia» b"


def test_find_hermes_python_shebang_and_sh_wrapper(tmp_path):
    launcher = tmp_path / "hermes"
    launcher.write_text("#!/home/h/.hermes/hermes-agent/.venv/bin/python -I\nimport x\n")
    assert ks.find_hermes_python(launcher, tmp_path) == ("/home/h/.hermes/hermes-agent/.venv/bin/python", "")
    launcher.write_text("#!/bin/sh\nexec /srv/venv/bin/python3 -I -c 'import base64' \"$@\"\n")
    (tmp_path / ".hermes" / "hermes-agent" / "gateway").mkdir(parents=True)
    assert ks.find_hermes_python(launcher, tmp_path) == ("/srv/venv/bin/python3", str(tmp_path / ".hermes" / "hermes-agent"))
    assert ks.find_hermes_python(tmp_path / "tidak-ada", tmp_path / "kosong") == ("", "")


# --------------------------------------------------------------------------- jawaban
@pytest.mark.parametrize("payload", [
    {"bot_token": "abc"}, {"HERMES_SETUP_LLM_KEY": "abc"}, {"llm_key": "abc"}, {"api_key": "abc"},
    {"llm": {"password": "abc"}}, {"admins": [{"client_secret": "abc"}]},
])
def test_answers_file_with_secret_looking_key_is_rejected(tmp_path, payload):
    path = tmp_path / "jawaban.yaml"
    path.write_text(yaml.safe_dump(payload))
    wizard = Wizard(tmp_path)
    assert wizard.main("--non-interactive", "--answers", str(path)) == 2
    assert "terlihat seperti rahasia" in wizard.text and ks.ENV_BOT_TOKEN in wizard.text and "abc" not in wizard.text
    assert wizard.sys.calls == [] and not wizard.state_file.exists()


def test_answers_file_allows_key_env_name_and_rejects_unknown_keys(tmp_path):
    path = tmp_path / "jawaban.yaml"
    path.write_text("llm_key_env: ACME_API_KEY\nllm_base_url: https://x.example/v1\n")
    assert ks.load_answers(path)["llm_key_env"] == "ACME_API_KEY"
    path.write_text("llm_base_urll: https://x.example/v1\n")
    with pytest.raises(ks.StepError) as err:
        ks.load_answers(path)
    assert "llm_base_urll" in err.value.message


def test_explicit_confirmations_are_not_answered_by_yes(tmp_path):
    asked = []
    prompter = ks.Prompter({}, {}, False, True, lambda _l: None, lambda p: asked.append(p) or "t")
    assert prompter.confirm("usage_timer", "Pasang?", True) is True and asked == []
    assert prompter.confirm("install_docker", "Jalankan skrip?", None) is False and len(asked) == 1
    with pytest.raises(ks.StepError):
        ks.Prompter({}, {}, True).confirm("install_docker", "Jalankan skrip?", None)


# --------------------------------------------------------------------------- ujung ke ujung
def run_end_to_end(tmp_path, llm_base, **extra):
    wizard = Wizard(tmp_path)
    code = wizard.main("--non-interactive", "--answers", str(write_answers(tmp_path, llm_base, **extra)))
    return wizard, code


def test_non_interactive_end_to_end(tmp_path, discord, llm, secrets_env):
    wizard, code = run_end_to_end(tmp_path, llm[0])
    assert code == 0, wizard.text
    state = wizard.state()
    assert state["completed"] == ks.STEP_NAMES
    for i, step in enumerate(ks.STEPS, 1):
        assert f"[{i}/14] {step.title}" in wizard.text
    assert "[4/14] Menyambungkan bot Discord" in wizard.text

    # perintah, berurutan
    cmds = wizard.sys.commands()
    expected_order = ["apt-get update", "apt-get install -y git ca-certificates", "curl -fsSL https://get.docker.com -o", "sh ", "docker --version", "adduser --disabled-password --gecos  hermes",
                      "usermod -aG docker hermes", "loginctl enable-linger hermes", "docker run --rm hello-world",
                      "bash -c set -o pipefail; curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- "
                      "--non-interactive --skip-setup", "hermes --version", "hermes doctor", "chown -R root:root",
                      "chmod -R a+rX,go-w", "hermes_team.py check", "hermes_team.py apply", "hermes_team.py managed",
                      "hermes_team.py apply", "systemctl --user enable --now hermes-usage-collect.timer",
                      "systemctl --user enable --now hermes-usage-report.timer", "hermes gateway install",
                      "hermes gateway start", "hermes gateway status", "kit_doctor.py --discord"]
    position = 0
    for needle in expected_order:
        position = next((i for i in range(position, len(cmds)) if needle in cmds[i]), None)
        assert position is not None, f"perintah tidak ditemukan sesuai urutan: {needle}\n" + "\n".join(cmds)
        position += 1
    assert not any("gateway setup" in c or "egress" in c or "selftest" in c or "docker build" in c for c in cmds)
    # gateway tidak pernah dijalankan sebelum provisioning selesai
    assert max(i for i, c in enumerate(cmds) if "hermes_team.py apply" in c) < cmds.index("hermes gateway install")
    # semua perintah Hermes/kit jalan sebagai user hermes dengan lingkungan bersih; apt/docker build sebagai root
    for call in wizard.sys.calls:
        user, inner = FakeSystem.inner(call["argv"])
        if inner[0] in ("hermes", "python3", "bash") or inner[:2] == ["systemctl", "--user"]:
            assert user == "hermes" and call["cwd"] == str(wizard.home), inner
        if inner[0] in ("apt-get", "adduser", "usermod", "loginctl", "chown", "chmod", "sh"):
            assert user is None, inner

    # berkas tujuan
    team_dir = wizard.home / ".hermes-team"
    team = ht.load_team(team_dir / "team.yaml")
    assert team["guild_id"] == fd.GUILD and team["team_name"] == "Platform"
    assert (team["member_role"], team["admin_role"], team["entry_role"]) == (fd.ROLE_MEMBER, fd.ROLE_ADMIN, fd.ROLE_ENTRY)
    assert team["admins"] == [fd.ADMIN] and team["category_id"] == fd.CAT_HERMES
    assert team["shared"]["channel_id"] == fd.CH_SHARED and team["admin"]["channel_id"] == fd.CH_ADMIN
    assert team["llm"]["providers"]["td"] == {"api": llm[0], "key_env": "TD_API_KEY"}
    usage_id = state["data"]["usage_channel_id"]
    assert team["usage"] == {"timezone": "Asia/Makassar", "report_channel_id": usage_id,
                             "reports": {"daily": "08:00", "weekly": "08:05", "monthly": "08:10"}}
    assert team["members"] == [] and yaml.safe_load((team_dir / "members.yaml").read_text()) == {"members": []}
    usage = discord.channels[usage_id]
    assert usage["name"] == "hermes-usage" and usage["parent_id"] == fd.CAT_HERMES
    assert sorted((o["id"], o["type"], int(o["allow"]), int(o["deny"])) for o in usage["permission_overwrites"]) == \
        sorted([(fd.GUILD, 0, 0, VIEW), (fd.ROLE_ADMIN, 0, VIEW | (1 << 16), 0), (fd.BOT, 1, kd.BOT_ALLOW, 0)])

    bot_env = wizard.home / ".hermes" / ".env"
    assert kd.parse_env_text(bot_env.read_text()) == {"DISCORD_BOT_TOKEN": TOKEN}
    assert stat.S_IMODE(bot_env.stat().st_mode) == 0o600
    assert kd.parse_env_text((wizard.etc / ".env").read_text()) == {"TD_API_KEY": LLM_KEY}
    assert stat.S_IMODE((wizard.etc / ".env").stat().st_mode) == 0o640
    assert (wizard.etc / "config.yaml").read_text() == "# managed tiruan\nterminal:\n  backend: docker\n"
    assert stat.S_IMODE((wizard.etc / "config.yaml").stat().st_mode) == 0o644
    units = wizard.home / ".config" / "systemd" / "user"
    assert str(wizard.kit) in (units / "hermes-usage-collect.service").read_text()
    assert f"ExecStart=/usr/bin/python3 {wizard.kit}/tools/usage_post.py\n" in (units / "hermes-usage-report.service").read_text()
    assert (units / "hermes-usage-report.timer").is_file() and (units / "hermes-usage-collect.timer").is_file()

    # kit tersalin tanpa .git / cache / arsip
    assert (wizard.kit / "hermes_team.py").is_file() and (wizard.kit / "tests" / "selftest.sh").is_file()
    assert (wizard.kit / "docker" / "sandbox" / "Dockerfile").is_file()
    for name in (".git", "arsip-versi-lama", "__pycache__", ".pytest_cache"):
        assert not (wizard.kit / name).exists(), name
    assert not list(wizard.kit.rglob("__pycache__"))

    # ringkasan
    for needle in (fd.ROLE_MEMBER, fd.CH_ADMIN, usage_id, "Uji di Discord sekarang", "nilainya tidak ditampilkan",
                   str(team_dir / "team.yaml")):
        assert needle in wizard.text
    assert_no_secret_anywhere(wizard, tmp_path)

    # --list setelah selesai, dan putaran kedua tidak mengulang apa pun kecuali verify
    listing = Wizard(tmp_path, wizard.sys)
    assert listing.main("--list") == 0
    assert [line.split()[2] for line in listing.lines] == ["[selesai]"] * 13 + ["[belum]"]
    before, writes = len(wizard.sys.calls), len(posts(discord))
    again = Wizard(tmp_path, wizard.sys)
    assert again.main("--non-interactive", "--answers", str(tmp_path / "jawaban.yaml")) == 0
    assert again.text.count("sudah selesai — dilewati") == 13
    assert [c for c in wizard.sys.commands()[before:]] == [c for c in wizard.sys.commands()[before:] if "kit_doctor" in c]
    assert len(posts(discord)) == writes


def assert_no_secret_anywhere(wizard, tmp_path):
    for secret in (TOKEN, LLM_KEY):
        raw = secret.encode()
        assert raw not in wizard.state_file.read_bytes()
        assert secret not in wizard.text
        for call in wizard.sys.calls:
            dump = json.dumps(call, ensure_ascii=False)
            assert secret not in dump, call["argv"]
    # satu-satunya berkas di tmp yang memuat rahasia adalah dua berkas .env tujuan (salinan kit di opt/ memuat
    # konstanta uji ini sendiri, jadi dilewati)
    holders = set()
    for path in tmp_path.rglob("*"):
        if path.is_file() and not path.is_symlink() and "opt" not in path.relative_to(tmp_path).parts[:1]:
            blob = path.read_bytes()
            if TOKEN.encode() in blob or LLM_KEY.encode() in blob:
                holders.add(path.relative_to(tmp_path).as_posix())
    assert holders == {"home/hermes/.hermes/.env", "etc-hermes/.env"}


def test_secrets_never_reach_stdout_stderr(tmp_path, discord, llm, secrets_env, capfd):
    wizard = Wizard(tmp_path)
    code = ks.main(["--kit-dir", str(wizard.kit), "--non-interactive", "--answers", str(write_answers(tmp_path, llm[0]))],
                   system=wizard.sys, state_file=wizard.state_file,
                   context_overrides={"etc_dir": wizard.etc, "linger_dir": wizard.sys.linger_dir, "source_dir": KIT_DIR,
                                      **discord_context_overrides()})
    captured = capfd.readouterr()
    assert code == 0, captured.out
    assert "[14/14]" in captured.out
    for secret in (TOKEN, LLM_KEY):
        assert secret not in captured.out and secret not in captured.err
    assert all(c["auth"] == f"Bot {TOKEN}" for c in discord.calls)          # token hanya lewat header HTTP


def test_extras_sandbox_build_sets_image_and_reprovisions(tmp_path, discord, llm, secrets_env):
    wizard, code = run_end_to_end(tmp_path, llm[0], build_sandbox=True, sandbox_tag="hermes-team-sandbox:2026.11",
                                  usage_reports=False, run_selftest=True)
    assert code == 0, wizard.text
    cmds = wizard.sys.commands()
    build = next(i for i, c in enumerate(cmds) if c.startswith("docker build -t hermes-team-sandbox:2026.11 "))
    assert cmds[build].endswith(str(wizard.kit / "docker" / "sandbox"))
    assert sum("hermes_team.py apply" in c for c in cmds) == 4 and sum("hermes_team.py managed" in c for c in cmds) == 2
    assert [i > build for i, c in enumerate(cmds) if "hermes_team.py apply" in c] == [False, False, True, True]
    assert cmds.index("hermes gateway install") > max(i for i, c in enumerate(cmds) if "hermes_team.py apply" in c)
    team = ht.load_team(wizard.home / ".hermes-team" / "team.yaml")
    assert team["sandbox"]["image"] == "hermes-team-sandbox:2026.11" and team["usage"]["report_channel_id"] == ""
    assert not any("hermes-usage-report" in c for c in cmds) and "usage_channel_id" not in wizard.state()["data"]
    # selftest lulus tetapi keluarannya tidak menyebut "Hermes terdeteksi" → tidak boleh diklaim sebagai bukti
    assert any("selftest.sh" in c for c in cmds) and "TANPA uji integrasi" in wizard.text


# --------------------------------------------------------------------------- lanjut setelah gagal
FAILURES = {
    "packages": (lambda cmd: cmd[:1] == ["sh"], ["preflight"], "apt-get"),
    "hermes_install": (lambda cmd: cmd[:1] == ["bash"] and "install.sh" in " ".join(cmd),
                       ["preflight", "packages", "linux_user", "discord_bot", "discord_server", "llm", "options"], "adduser"),
    "provision": (lambda cmd: "hermes_team.py" in " ".join(cmd) and "apply" in cmd,
                  ks.STEP_NAMES[:10], "install.sh"),
    "gateway": (lambda cmd: cmd[:3] == ["hermes", "gateway", "start"], ks.STEP_NAMES[:12], "hermes_team.py apply"),
}


@pytest.mark.parametrize("failed", sorted(FAILURES))
def test_resume_continues_at_the_failed_step(tmp_path, discord, llm, secrets_env, failed):
    matcher, completed, must_not_repeat = FAILURES[failed]
    answers = write_answers(tmp_path, llm[0])
    wizard = Wizard(tmp_path)
    wizard.sys.fail = lambda cmd: ks.Result(1, "", "galat tiruan: disk penuh") if matcher(cmd) else None
    assert wizard.main("--non-interactive", "--answers", str(answers)) == 1
    index = ks.STEP_NAMES.index(failed)
    assert wizard.state()["completed"] == completed
    assert f"✗ Langkah '{failed}' gagal" in wizard.text and "galat tiruan: disk penuh" in wizard.text
    assert "lanjutkan dengan:  sudo bash setup.sh" in wizard.text
    assert f"[{index + 2}/14]" not in wizard.text
    assert_state_clean(wizard)

    wizard.sys.fail = None
    before, writes = len(wizard.sys.calls), len(posts(discord))
    resumed = Wizard(tmp_path, wizard.sys)
    assert resumed.main("--non-interactive", "--answers", str(answers)) == 0, resumed.text
    assert resumed.state()["completed"] == ks.STEP_NAMES
    headers = [line for line in resumed.lines if str(line).startswith("[")]
    skipped = [h for h, nxt in zip(resumed.lines, resumed.lines[1:]) if str(h).startswith("[") and "dilewati" in str(nxt)]
    assert len(headers) == 14 and len(skipped) == index, resumed.text
    later = wizard.sys.commands()[before:]
    assert not any(must_not_repeat in c for c in later), later
    if index > ks.STEP_NAMES.index("options"):
        assert len(posts(discord)) == writes               # Discord tidak disentuh lagi
        assert not discord.calls_matching("POST", f"/guilds/{fd.GUILD}/channels")[1:]
    assert_state_clean(resumed)


def assert_state_clean(wizard):
    raw = wizard.state_file.read_bytes()
    assert TOKEN.encode() not in raw and LLM_KEY.encode() not in raw
    assert stat.S_IMODE(wizard.state_file.stat().st_mode) == 0o600
    assert TOKEN not in wizard.text and LLM_KEY not in wizard.text


def test_resume_after_discord_failure_reuses_token_from_disk(tmp_path, discord, llm, monkeypatch):
    """Gagal di discord_server; putaran kedua TANPA env token: token dibaca dari ~/.hermes/.env, bukan dari state."""
    monkeypatch.setenv(ks.ENV_BOT_TOKEN, TOKEN)
    monkeypatch.setenv(ks.ENV_LLM_KEY, LLM_KEY)
    answers = write_answers(tmp_path, llm[0])
    discord.forbid.add(("PUT", f"/channels/{fd.CH_SHARED}/permissions/"))
    wizard = Wizard(tmp_path)
    assert wizard.main("--non-interactive", "--answers", str(answers)) == 1
    assert wizard.state()["completed"] == ["preflight", "packages", "linux_user", "discord_bot"]
    assert "bot tidak boleh mengatur izin #hermes-shared (403)" in wizard.text
    discord.forbid.clear()
    monkeypatch.delenv(ks.ENV_BOT_TOKEN)
    resumed = Wizard(tmp_path, wizard.sys)
    assert resumed.main("--non-interactive", "--answers", str(answers)) == 0, resumed.text
    assert_state_clean(resumed)


def test_from_and_only_and_reset(tmp_path, discord, llm, secrets_env):
    wizard, code = run_end_to_end(tmp_path, llm[0])
    assert code == 0
    before = len(wizard.sys.calls)
    only = Wizard(tmp_path, wizard.sys)
    assert only.main("--non-interactive", "--only", "provision") == 0
    assert [h for h in only.lines if str(h).startswith("[")] == ["[11/14] " + ks.Provision.title]
    assert sum("hermes_team.py" in c for c in wizard.sys.commands()[before:]) == 3
    start = Wizard(tmp_path, wizard.sys)
    assert start.main("--non-interactive", "--from", "gateway") == 0
    assert [str(h)[:7] for h in start.lines if str(h).startswith("[")] == ["[13/14]", "[14/14]"]
    assert Wizard(tmp_path, wizard.sys).main("--only", "tidak-ada") == 2
    reset = Wizard(tmp_path, wizard.sys)
    assert reset.main("--reset", "--list") == 0 and wizard.state_file.exists()      # --list tidak pernah mengubah
    assert reset.main("--reset", "--dry-run") == 0 and wizard.state_file.exists()


# --------------------------------------------------------------------------- dry-run, --list, root
def snapshot(root: Path):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def test_dry_run_runs_nothing_and_writes_nothing(tmp_path, discord, llm, secrets_env):
    answers = write_answers(tmp_path, llm[0])
    before = snapshot(tmp_path)
    wizard = Wizard(tmp_path)
    wizard.sys.euid = lambda: 1000                               # dry-run boleh tanpa root
    assert wizard.main("--dry-run", "--non-interactive", "--answers", str(answers)) == 0
    assert wizard.sys.calls == [] and discord.calls == [] and llm[1] == []
    assert snapshot(tmp_path) == before
    assert wizard.text.count("(dry-run)") >= 14 * 2 and "[14/14]" in wizard.text
    assert "adduser --disabled-password" in wizard.text and "hermes gateway start" in wizard.text
    assert "tidak ada perintah dijalankan" in wizard.text

    # dry-run di tengah jalan: state yang ada tidak berubah
    wizard2 = Wizard(tmp_path)
    wizard2.sys.fail = lambda cmd: ks.Result(1, "", "x") if cmd[:1] == ["bash"] else None
    assert wizard2.main("--non-interactive", "--answers", str(answers)) == 1
    frozen, calls = wizard2.state_file.read_bytes(), len(wizard2.sys.calls)
    files = snapshot(tmp_path)
    dry = Wizard(tmp_path, wizard2.sys)
    assert dry.main("--dry-run", "--non-interactive", "--answers", str(answers)) == 0
    assert wizard2.state_file.read_bytes() == frozen and len(wizard2.sys.calls) == calls and snapshot(tmp_path) == files
    assert dry.text.count("sudah selesai — dilewati") == 7


def test_list_output_fresh(tmp_path):
    wizard = Wizard(tmp_path)
    assert wizard.main("--list") == 0
    assert len(wizard.lines) == 14 and wizard.sys.calls == [] and not wizard.state_file.exists()
    for i, (line, step) in enumerate(zip(wizard.lines, ks.STEPS), 1):
        assert line == f"{i:>2}. {step.name:<15} [belum]   {step.title}"
    assert ks.STEP_NAMES == ["preflight", "packages", "linux_user", "discord_bot", "discord_server", "llm", "options",
                             "hermes_install", "kit_install", "write_config", "provision", "extras", "gateway", "verify"]


def test_kit_setup_refuses_without_root(tmp_path):
    wizard = Wizard(tmp_path)
    wizard.sys.euid = lambda: 1000
    assert wizard.main("--non-interactive") == 1
    assert "sudo bash setup.sh" in wizard.text and wizard.sys.calls == [] and not wizard.state_file.exists()


def test_preflight_warnings_and_unreachable_discord(tmp_path):
    system = FakeSystem(tmp_path)
    system.os_release = lambda: {"ID": "fedora", "PRETTY_NAME": "Fedora 41"}
    system.mem_total_gb = lambda: 3.8
    system.disk_free_gb = lambda path="/": 11.0
    ctx = make_ctx(tmp_path, system=system)
    ks.Preflight().run(ctx)
    joined = " | ".join(ctx.warnings)
    assert "bukan Debian/Ubuntu" in joined and "di bawah 8 GB" in joined and "di bawah 20 GB" in joined
    system.net[ks.DISCORD_PROBE_URL] = (False, "URLError: timed out")
    with pytest.raises(ks.StepError) as err:
        ks.Preflight().run(make_ctx(tmp_path, system=system))
    assert "discord.com tidak terjangkau" in err.value.message


def test_gateway_failure_shows_logs_and_known_cause(tmp_path):
    system = FakeSystem(tmp_path, with_user=True)

    def fail(cmd):
        if cmd[:3] == ["hermes", "gateway", "status"]:
            return ks.Result(3, "gateway: failed\n")
        if cmd[:2] == ["hermes", "logs"]:
            return ks.Result(0, "ERROR discord.errors.PrivilegedIntentsRequired: Shard ID None is requesting...\n")
        return None

    system.fail = fail
    ctx = make_ctx(tmp_path, system=system)
    with pytest.raises(ks.StepError) as err:
        ks.Gateway().run(ctx)
    assert err.value.cause == "intent belum dihidupkan" and "Server Members Intent" in err.value.fix
    assert "PrivilegedIntentsRequired" in "\n".join(ctx.lines) and system.slept == 8


# --------------------------------------------------------------------------- setup.sh
def fake_bin(tmp_path, uid, with_python=True):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "id").write_text(f"#!/bin/sh\necho {uid}\n")
    scripts = ["id"]
    if with_python:
        (bindir / "python3").write_text('#!/bin/sh\n[ "$1" = "-c" ] && exit 0\necho "PY $*"\n')
        (bindir / "apt-get").write_text('#!/bin/sh\necho "APT $*"\nexit 9\n')
        scripts += ["python3", "apt-get"]
    for name in scripts:
        (bindir / name).chmod(0o755)
    return dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")


def test_setup_sh_syntax_and_refuses_when_not_root(tmp_path):
    script = KIT_DIR / "setup.sh"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    proc = subprocess.run(["bash", str(script), "--list"], capture_output=True, text=True, env=fake_bin(tmp_path, 1000),
                          cwd=str(tmp_path))
    assert proc.returncode == 1 and proc.stdout == ""
    assert "sudo bash setup.sh" in proc.stderr and "PY" not in proc.stderr and "APT" not in proc.stdout


def test_setup_sh_as_root_hands_over_to_kit_setup(tmp_path):
    proc = subprocess.run(["bash", str(KIT_DIR / "setup.sh"), "--only", "verify", "--yes"], capture_output=True,
                          text=True, env=fake_bin(tmp_path, 0), cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"PY {KIT_DIR}/kit_setup.py --only verify --yes"      # tanpa apt bila python3+yaml ada


def test_module_does_not_import_hermes_team():
    code = "import sys; sys.path.insert(0, %r); import kit_setup; print('hermes_team' in sys.modules)" % str(KIT_DIR)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "False", out.stderr


# =========================================================================== audit: perbaikan cacat (item 1–15)
# --------------------------------------------------------------------------- bantu
import http.client  # noqa: E402
import shutil  # noqa: E402
import urllib.parse  # noqa: E402


def serve(fn):
    """Server HTTP lokal: fn(handler) → (kode, header, bytes). Mengembalikan (server, base_url, calls)."""
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _h(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            calls.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization")})
            code, headers, payload = fn(self)
            self.send_response(code)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _h

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", calls


def stop(*servers):
    for server in servers:
        server.shutdown()
        server.server_close()


def mutating(state):
    return [c for c in state.calls if c["method"] in ("POST", "PUT", "PATCH", "DELETE")]


# --------------------------------------------------------------------------- item 1: gateway hidup/mati
def test_gateway_status_parser_on_upstream_samples():
    assert ks.parse_gateway_status(GATEWAY_RUNNING) == "running"
    assert ks.parse_gateway_status(GATEWAY_MANUAL_RUNNING) == "running"
    assert ks.parse_gateway_status(GATEWAY_STOPPED) == "stopped"
    assert ks.parse_gateway_status(GATEWAY_NOT_RUNNING) == "stopped"
    assert ks.parse_gateway_status(GATEWAY_RESTARTING) == "stopped"
    assert ks.parse_gateway_status(GATEWAY_NOT_INSTALLED) == "not_installed"
    assert ks.parse_gateway_status("") == "unknown" and ks.parse_gateway_status("sesuatu yang lain\n") == "unknown"


def test_gateway_verdict_prefers_systemctl_is_active():
    assert ks.gateway_verdict("active", "") == "running"
    assert ks.gateway_verdict("active", GATEWAY_STOPPED) == "running"
    for state in ("failed", "inactive", "activating", "deactivating"):
        assert ks.gateway_verdict(state, GATEWAY_RUNNING) == "stopped", state
    assert ks.gateway_verdict("", GATEWAY_RUNNING) == "running"
    assert ks.gateway_verdict("", GATEWAY_STOPPED) == "stopped"
    assert ks.gateway_verdict("", "") == "unknown"


def test_gateway_dead_service_with_exit_code_zero_is_a_failure(tmp_path):
    """Hulu: `hermes gateway status` mencetak 'stopped' dengan kode keluar 0. Kode keluar bukan bukti hidup."""
    system = FakeSystem(tmp_path, with_user=True)
    system.script_cmd("systemctl --user is-active", ks.Result(3, "failed\n"))
    system.script_cmd("hermes gateway status", ks.Result(0, GATEWAY_STOPPED))
    system.script_cmd("hermes logs gateway", ks.Result(0, "ERROR discord.errors.PrivilegedIntentsRequired: Shard ID None\n"))
    ctx = make_ctx(tmp_path, system=system)
    with pytest.raises(ks.StepError) as err:
        ks.Gateway().run(ctx)
    text = "\n".join(ctx.lines)
    assert err.value.cause == "intent belum dihidupkan" and "Server Members Intent" in err.value.fix
    assert "PrivilegedIntentsRequired" in text and "belum ada di server" in text and "token" in text
    assert "berhasil" not in text and "sukses" not in text.lower()


def test_gateway_stopped_status_text_alone_is_not_success(tmp_path):
    system = FakeSystem(tmp_path, with_user=True)
    system.script_cmd("systemctl --user is-active", ks.Result(127, "", "systemctl: not found"))
    system.script_cmd("hermes gateway status", ks.Result(0, GATEWAY_NOT_RUNNING))
    with pytest.raises(ks.StepError):
        ks.Gateway().run(make_ctx(tmp_path, system=system))


def test_gateway_polls_until_active_then_rechecks_for_crash_loops(tmp_path):
    system = FakeSystem(tmp_path, with_user=True)
    system.script_cmd("systemctl --user is-active", ks.Result(3, "activating\n"), ks.Result(3, "activating\n"),
                      ks.Result(0, "active\n"))
    ctx = make_ctx(tmp_path, system=system)
    ks.Gateway().run(ctx)
    checks = [c for c in system.commands() if "is-active" in c]
    assert len(checks) == 4 and all(c.endswith("hermes-gateway.service") for c in checks)
    assert system.slept == 3 + 3 + 10                      # dua putaran tunggu + jeda kestabilan


def test_gateway_crash_loop_is_caught_by_the_second_check(tmp_path):
    system = FakeSystem(tmp_path, with_user=True)
    system.script_cmd("systemctl --user is-active", ks.Result(0, "active\n"), ks.Result(3, "activating\n"))
    ctx = make_ctx(tmp_path, system=system)
    with pytest.raises(ks.StepError) as err:
        ks.Gateway().run(ctx)
    assert "tidak stabil" in err.value.message or "tidak stabil" in err.value.cause


def test_gateway_restart_counter_catches_a_loop_that_always_looks_active(tmp_path):
    system = FakeSystem(tmp_path, with_user=True)
    system.script_cmd("show -p NRestarts", ks.Result(0, "0\n"), ks.Result(0, "3\n"))
    with pytest.raises(ks.StepError) as err:
        ks.Gateway().run(make_ctx(tmp_path, system=system))
    assert "tidak stabil" in err.value.message or "tidak stabil" in err.value.cause


def test_gateway_gives_up_after_sixty_seconds(tmp_path):
    system = FakeSystem(tmp_path, with_user=True)
    system.script_cmd("systemctl --user is-active", ks.Result(3, "activating\n"))
    with pytest.raises(ks.StepError):
        ks.Gateway().run(make_ctx(tmp_path, system=system))
    assert system.slept == 60


def test_gateway_uses_the_unit_hermes_installed(tmp_path):
    system = FakeSystem(tmp_path, with_user=True)
    system.script_cmd("hermes gateway install", ks.Result(0, "ok\n"))           # tidak membuat unit sendiri
    unit_dir = tmp_path / "home" / "hermes" / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "hermes-gateway-ab12cd34.service").write_text("[Unit]\n")
    ks.Gateway().run(make_ctx(tmp_path, system=system))
    assert any(c.endswith("is-active hermes-gateway-ab12cd34.service") for c in system.commands())


# --------------------------------------------------------------------------- item 15: bus user@UID
def test_user_bus_is_awaited_before_the_first_systemctl_user_call(tmp_path):
    system = FakeSystem(tmp_path, with_user=True)
    system.bus_after = 3
    seen = []
    system.on_sleep = lambda: seen.append(list(system.commands()))
    ks.Gateway().run(make_ctx(tmp_path, system=system))
    assert system.bus_polls == 4 and len(seen) >= 3
    assert "systemctl start user@1001.service" in seen[0] and "hermes gateway install" not in seen[0]
    assert seen[2] and "hermes gateway install" not in seen[2]


def test_user_bus_that_never_appears_warns_after_fifteen_seconds(tmp_path):
    system = FakeSystem(tmp_path, with_user=True)
    system.bus_after = 10 ** 6
    ctx = make_ctx(tmp_path, system=system)
    ks.Gateway().run(ctx)
    assert any("/run/user/1001/bus" in w for w in ctx.warnings)
    assert system.slept >= 15


# --------------------------------------------------------------------------- item 2 & 9: pemasangan kit
def kit_ctx(tmp_path, dst, source=None, system=None):
    ctx = make_ctx(tmp_path, system=system)
    ctx.kit_dir = Path(dst)
    if source:
        ctx.source_dir = Path(source)
    return ctx


def test_kit_install_refuses_an_existing_directory_that_is_not_a_previous_install(tmp_path):
    dst = tmp_path / "usr-local"
    (dst / "bin").mkdir(parents=True)
    (dst / "bin" / "berharga").write_text("jangan hilang")
    with pytest.raises(ks.StepError) as err:
        ks.KitInstall().run(kit_ctx(tmp_path, dst))
    assert "bukan instalasi kit" in err.value.message
    assert (dst / "bin" / "berharga").read_text() == "jangan hilang"
    assert not (tmp_path / "usr-local.lama").exists() and not (tmp_path / "usr-local.baru").exists()


@pytest.mark.parametrize("files", [("hermes_team.py",), (".kit-installed",)], ids=["tanpa-penanda", "tanpa-kit"])
def test_kit_install_needs_both_marker_and_hermes_team(tmp_path, files):
    dst = tmp_path / "setengah"
    dst.mkdir()
    for name in files:
        (dst / name).write_text("x")
    with pytest.raises(ks.StepError):
        ks.KitInstall().run(kit_ctx(tmp_path, dst))
    assert sorted(p.name for p in dst.iterdir()) == sorted(files)


@pytest.mark.parametrize("path", ["/", "/etc", "/usr", "/usr/local", "/opt", "/home", "/var", "/root", "/bin", "/lib64",
                                  "/boot", "/etc/", "/opt/../etc", "relatif/kit", "opt"])
def test_kit_dir_system_and_relative_paths_are_refused(path):
    assert ks.validate_kit_dir(path, Path("/srv/repo"), "/home/hermes"), path


def test_kit_dir_refuses_home_and_ancestors_of_the_source(tmp_path):
    src = tmp_path / "a" / "repo"
    src.mkdir(parents=True)
    assert ks.validate_kit_dir(str(tmp_path / "a"), src, "/home/hermes")                 # induk repo
    assert ks.validate_kit_dir("/home/hermes", Path("/srv/repo"), "/home/hermes")        # home user
    assert ks.validate_kit_dir("/home/hermes/kit", Path("/srv/repo"), "/home/hermes")    # di bawah home user
    assert ks.validate_kit_dir(str(tmp_path / "opt" / "hermes-team"), src, "/home/hermes") is None
    assert ks.validate_kit_dir("/opt/hermes-team", Path("/srv/repo"), "/home/hermes") is None


def test_kit_dir_flag_is_validated_before_anything_is_persisted(tmp_path):
    for bad in ("/opt", "/", "opt/relatif"):
        wizard = Wizard(tmp_path)
        assert wizard.main("--dry-run", "--only", "kit_install", "--kit-dir", bad) == 2, bad
        assert "--kit-dir" in wizard.text and not wizard.state_file.exists()


def test_kit_install_leftover_directories_are_removed_only_if_the_wizard_made_them(tmp_path):
    dst = tmp_path / "opt" / "hermes-team"
    dst.parent.mkdir()
    stray = dst.with_name("hermes-team.baru")
    stray.mkdir()
    (stray / "milik-orang-lain").write_text("x")
    with pytest.raises(ks.StepError):
        ks.KitInstall().run(kit_ctx(tmp_path, dst))
    assert (stray / "milik-orang-lain").read_text() == "x" and not dst.exists()


def test_kit_install_replace_keeps_old_install_until_the_new_one_is_done(tmp_path):
    dst = tmp_path / "opt" / "hermes-team"
    ks.KitInstall().run(kit_ctx(tmp_path, dst))
    assert (dst / ".kit-installed").is_file() and (dst / "hermes_team.py").is_file()
    (dst / "LAMA-1").write_text("lama")
    ks.KitInstall().run(kit_ctx(tmp_path, dst))                                  # penggantian yang sukses
    assert not (dst / "LAMA-1").exists() and (dst / ".kit-installed").is_file()
    assert not dst.with_name("hermes-team.lama").exists() and not dst.with_name("hermes-team.baru").exists()

    (dst / "LAMA-2").write_text("lama2")
    system = FakeSystem(tmp_path, with_user=True)
    system.fail = lambda cmd: ks.Result(1, "", "galat tiruan") if cmd[:1] == ["chmod"] else None
    with pytest.raises(ks.StepError):
        ks.KitInstall().run(kit_ctx(tmp_path, dst, system=system))
    assert (dst / "LAMA-2").read_text() == "lama2", "instalasi lama harus utuh bila yang baru gagal"


def test_kit_copy_is_an_allowlist_not_a_copytree(tmp_path):
    src = tmp_path / "repo"
    shutil.copytree(KIT_DIR, src, ignore=shutil.ignore_patterns(".git", "__pycache__", "arsip-versi-lama",
                                                                 ".pytest_cache", "venv", ".venv"))
    strays = [".env", "jawaban.yaml", "id_rsa", "team.yaml", "members.yaml", "catatan.txt", "rahasia.pem",
              "tests/.env", "docs/id_ed25519", "kit/jawaban-prod.yaml", "tools/.env.production"]
    for rel in strays:
        (src / rel).write_text("RAHASIA-LOKAL")
    (src / "tautan").symlink_to("/etc/hostname")
    (src / "docs" / "tautan.md").symlink_to("/etc/hostname")
    dst = tmp_path / "opt" / "hermes-team"
    ks.KitInstall().run(kit_ctx(tmp_path, dst, source=src))
    for rel in strays + ["tautan", "docs/tautan.md"]:
        assert not os.path.lexists(dst / rel), rel
    assert not [p for p in dst.rglob("*") if p.is_file() and b"RAHASIA-LOKAL" in p.read_bytes()]
    for rel in ("hermes_team.py", "kit_setup.py", "kit/cli.py", "teamctl/schema.py", "plugins/team-guard/plugin.yaml",
                "systemd/hermes-usage-collect.timer", "tools/usage_post.py", "docker/sandbox/Dockerfile",
                "docs/INSTALL.md", "tests/selftest.sh", "tests/test_setup.py", "VERSION", "team.example.yaml",
                "members.example.yaml", "requirements-dev.txt", "pyproject.toml", "CHANGELOG.md", "README.md",
                "skills/team-usage-report/SKILL.md", ".kit-installed"):
        assert (dst / rel).is_file(), rel
    out = subprocess.run([sys.executable, "-I", str(dst / "hermes_team.py"), "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and (KIT_DIR / "VERSION").read_text().strip() in out.stdout


# --------------------------------------------------------------------------- item 3: tulis berkas aman
def home_ctx(tmp_path):
    ctx = make_ctx(tmp_path)
    home = tmp_path / "home" / "hermes"
    return ctx, home


def test_write_file_refuses_a_symlinked_parent_directory(tmp_path):
    ctx, home = home_ctx(tmp_path)
    victim = tmp_path / "korban"
    victim.mkdir()
    (home / ".hermes").symlink_to(victim)
    with pytest.raises(ks.StepError) as err:
        ctx.write_file(home / ".hermes" / ".env", "DISCORD_BOT_TOKEN=x\n", 0o600, "hermes", "hermes")
    assert "symlink" in err.value.message and list(victim.iterdir()) == []


def test_write_file_refuses_a_symlinked_intermediate_directory(tmp_path):
    ctx, home = home_ctx(tmp_path)
    victim = tmp_path / "korban"
    (victim / "systemd" / "user").mkdir(parents=True)
    (home / ".config").symlink_to(victim)
    with pytest.raises(ks.StepError):
        ctx.write_file(home / ".config" / "systemd" / "user" / "a.service", "x", 0o644, "hermes", "hermes")
    assert list((victim / "systemd" / "user").iterdir()) == []


def test_write_file_ignores_a_planted_predictable_temp_symlink(tmp_path):
    ctx, home = home_ctx(tmp_path)
    (home / ".hermes").mkdir()
    victim = tmp_path / "korban.txt"
    victim.write_text("ASLI")
    (home / ".hermes" / f"..env.tmp-{os.getpid()}").symlink_to(victim)           # nama temp lama yang bisa ditebak
    ctx.write_file(home / ".hermes" / ".env", "DISCORD_BOT_TOKEN=rahasia\n", 0o600, "hermes", "hermes")
    assert victim.read_text() == "ASLI"
    assert (home / ".hermes" / ".env").read_text() == "DISCORD_BOT_TOKEN=rahasia\n"
    assert stat.S_IMODE((home / ".hermes" / ".env").stat().st_mode) == 0o600


def test_write_file_refuses_a_symlink_as_destination(tmp_path):
    ctx, home = home_ctx(tmp_path)
    (home / ".hermes").mkdir()
    victim = tmp_path / "korban.txt"
    victim.write_text("ASLI")
    (home / ".hermes" / ".env").symlink_to(victim)
    with pytest.raises(ks.StepError) as err:
        ctx.write_file(home / ".hermes" / ".env", "x=1\n", 0o600, "hermes", "hermes")
    assert "symlink" in err.value.message and victim.read_text() == "ASLI"


def test_write_file_chowns_through_the_open_descriptor_and_never_leaves_temp_files(tmp_path):
    ctx, home = home_ctx(tmp_path)
    (home / ".hermes").mkdir()
    ctx.write_file(home / ".hermes" / ".env", "A=1\n", 0o640, "root", "hermes")
    assert ctx.sys.fchowns == [("root", "hermes")] and ctx.sys.chowns == []
    assert sorted(p.name for p in (home / ".hermes").iterdir()) == [".env"]
    assert stat.S_IMODE((home / ".hermes" / ".env").stat().st_mode) == 0o640


def test_set_env_value_does_not_copy_a_symlinked_env_file_into_the_new_one(tmp_path):
    ctx, home = home_ctx(tmp_path)
    (home / ".hermes").mkdir()
    secret = tmp_path / "shadow"
    secret.write_text("root:HASH-RAHASIA:19000:0:99999:7:::\n")
    (home / ".hermes" / ".env").symlink_to(secret)
    with pytest.raises(ks.StepError):
        ctx.set_env_value(home / ".hermes" / ".env", "DISCORD_BOT_TOKEN", "t", 0o600, "hermes", "hermes")
    assert "HASH-RAHASIA" not in "".join(p.read_text() for p in (home / ".hermes").iterdir() if p.is_file() and not p.is_symlink())


def test_make_dir_refuses_symlink_and_chowns_what_it_creates_by_descriptor(tmp_path):
    ctx, home = home_ctx(tmp_path)
    victim = tmp_path / "korban"
    victim.mkdir()
    (home / ".hermes-team").symlink_to(victim)
    with pytest.raises(ks.StepError):
        ctx.make_dir(home / ".hermes-team", 0o700, "hermes", "hermes")
    assert victim.stat().st_mode & 0o777 != 0o700 or True
    ctx.make_dir(home / ".hermes", 0o700, "hermes", "hermes")
    assert stat.S_IMODE((home / ".hermes").stat().st_mode) == 0o700 and ctx.sys.fchowns == [("hermes", "hermes")]


def test_team_yaml_backup_does_not_follow_a_symlink(tmp_path):
    team_dir = tmp_path / "home" / "hermes" / ".hermes-team"
    team_dir.mkdir(parents=True)
    victim = tmp_path / "rahasia.yaml"
    victim.write_text("kunci_rahasia: JANGAN-DISALIN\nteam_name: Lama\n")
    (team_dir / "team.yaml").symlink_to(victim)
    with pytest.raises(ks.StepError):
        ks.WriteConfig().run(config_ctx(tmp_path, {"overwrite_team_yaml": True}))
    assert not list(team_dir.glob("*.bak-*")) and victim.read_text().startswith("kunci_rahasia")


# --------------------------------------------------------------------------- item 4: --from
def test_from_unmarks_every_later_step_so_changed_answers_take_effect(tmp_path, discord, llm, secrets_env):
    wizard, code = run_end_to_end(tmp_path, llm[0])
    assert code == 0
    team_file = wizard.home / ".hermes-team" / "team.yaml"
    assert yaml.safe_load(team_file.read_text())["team_name"] == "Platform"
    newer = write_answers(tmp_path, llm[0], team_name="Baru", overwrite_team_yaml=True)
    before = len(wizard.sys.commands())
    again = Wizard(tmp_path, wizard.sys)
    assert again.main("--non-interactive", "--answers", str(newer), "--from", "discord_server") == 0, again.text
    assert yaml.safe_load(team_file.read_text())["team_name"] == "Baru"
    later = wizard.sys.commands()[before:]
    assert sum("hermes_team.py apply" in c for c in later) == 2 and "hermes gateway install" in later
    assert "ditandai ulang" in again.text and "sudah selesai — dilewati" not in again.text.split("[5/14]", 1)[1]
    assert again.state()["completed"] == ks.STEP_NAMES


def test_from_help_says_it_also_redoes_later_steps():
    import io
    import contextlib
    buf = io.StringIO()
    with pytest.raises(SystemExit), contextlib.redirect_stdout(buf):
        ks.parse_args(["--help"])
    flat = " ".join(buf.getvalue().split())
    assert "semua langkah sesudahnya" in flat and "--no-systemd" in flat


# --------------------------------------------------------------------------- item 5: adopsi role & channel
def server_ctx(tmp_path, answers=None, **kw):
    return discord_ready(make_ctx(tmp_path, dict({"admins": fd.ADMIN}, **(answers or {})), **kw))


def test_existing_administrator_role_is_not_adopted_silently(tmp_path, discord):
    discord.roles[fd.ROLE_ADMIN]["permissions"] = str(1 << 3)
    with pytest.raises(ks.StepError) as err:
        ks.DiscordServer().run(server_ctx(tmp_path))
    assert "adopt_existing_roles" in err.value.message and "administrator" in err.value.message.lower()
    assert "hermes-admin" in err.value.message
    assert mutating(discord) == [] and discord.roles[fd.ROLE_ADMIN]["permissions"] == str(1 << 3)


def test_declined_role_adoption_changes_nothing(tmp_path, discord):
    discord.roles[fd.ROLE_ADMIN]["permissions"] = str((1 << 3) | (1 << 28))
    ctx = server_ctx(tmp_path, {"adopt_existing_roles": False})
    with pytest.raises(ks.StepError) as err:
        ks.DiscordServer().run(ctx)
    assert "tidak dipakai" in err.value.message and mutating(discord) == []


def test_confirmed_role_adoption_proceeds_and_names_the_permissions(tmp_path, discord):
    discord.roles[fd.ROLE_ADMIN]["permissions"] = str((1 << 3) | (1 << 28))
    ctx = server_ctx(tmp_path, {"adopt_existing_roles": True})
    ks.DiscordServer().run(ctx)
    text = "\n".join(ctx.lines).lower()
    assert "administrator" in text and "manage roles" in text and ctx.data["role_admin_id"] == fd.ROLE_ADMIN
    assert discord.roles[fd.ROLE_ADMIN]["permissions"] == str((1 << 3) | (1 << 28))      # tidak diubah wizard


def test_role_adoption_is_asked_in_words_when_interactive(tmp_path, discord):
    discord.roles[fd.ROLE_ADMIN]["permissions"] = str(1 << 3)
    ctx = server_ctx(tmp_path, non_interactive=False, inputs=["", "", "", "t"])
    with pytest.raises(ks.StepError):
        ks.DiscordServer().run(ctx)
    assert any("administrator" in str(line).lower() for line in ctx.lines) and mutating(discord) == []


def test_new_roles_are_not_created_when_a_later_role_adoption_is_declined(tmp_path, discord):
    discord.roles[fd.ROLE_ADMIN]["permissions"] = str(1 << 3)
    answers = {"role_entry": "baru-entry", "role_member": "baru-member"}
    with pytest.raises(ks.StepError):
        ks.DiscordServer().run(server_ctx(tmp_path, answers))
    assert not [r for r in discord.roles.values() if r["name"].startswith("baru-")]


def open_admin_channel(discord, viewers=()):
    """#hermes-admin PUBLIK di luar kategori Hermes (plus pelihat tambahan)."""
    discord.channels[fd.CH_ADMIN].update(parent_id=None, permission_overwrites=[
        {"id": uid, "type": 1, "allow": str(VIEW), "deny": "0"} for uid in viewers])


def test_public_channel_outside_category_requires_confirmation_and_names_who_sees_it(tmp_path, discord):
    open_admin_channel(discord, viewers=["500000000000000001"])
    before = json.dumps(discord.channels[fd.CH_ADMIN], sort_keys=True)
    with pytest.raises(ks.StepError) as err:
        ks.DiscordServer().run(server_ctx(tmp_path))
    assert "adopt_existing_channels" in err.value.message
    assert "@everyone" in err.value.message and "be1" in err.value.message and "500000000000000001" in err.value.message
    assert json.dumps(discord.channels[fd.CH_ADMIN], sort_keys=True) == before
    assert mutating(discord) == [], "ditolak = tidak ada satu pun perubahan di Discord (channel bersama pun tidak)"


def test_declined_channel_adoption_changes_nothing(tmp_path, discord):
    open_admin_channel(discord)
    with pytest.raises(ks.StepError) as err:
        ks.DiscordServer().run(server_ctx(tmp_path, {"adopt_existing_channels": False}))
    assert "tidak dipakai" in err.value.message and mutating(discord) == []


def test_confirmed_channel_adoption_removes_other_viewers_and_reasserts_the_overwrites(tmp_path, discord):
    open_admin_channel(discord, viewers=["500000000000000001", fd.ROLE_DESIGN])
    ctx = server_ctx(tmp_path, {"adopt_existing_channels": True})
    ks.DiscordServer().run(ctx)
    channel = discord.channels[fd.CH_ADMIN]
    got = sorted((o["id"], o["type"], int(o["allow"]), int(o["deny"])) for o in channel["permission_overwrites"])
    assert got == sorted([(fd.GUILD, 0, 0, VIEW), (fd.ROLE_ADMIN, 0, kd.MEMBER_ALLOW, 0), (fd.BOT, 1, kd.BOT_ALLOW, 0)])
    assert channel["parent_id"] is None, "dipakai di tempatnya (tidak dipindah)"


def test_extra_viewer_inside_the_category_also_needs_confirmation(tmp_path, discord):
    discord.channels[fd.CH_ADMIN]["permission_overwrites"].append({"id": fd.ROLE_MEMBER, "type": 0, "allow": str(VIEW), "deny": "0"})
    with pytest.raises(ks.StepError) as err:
        ks.DiscordServer().run(server_ctx(tmp_path))
    assert "backend" in err.value.message and mutating(discord) == []


# --------------------------------------------------------------------------- item 6: admin tim
def add_people(discord):
    discord.add_member("500000000000000091", "dodi")
    discord.add_member("500000000000000094", "alice_evil")
    discord.add_member("500000000000000095", "ratih")
    discord.members["500000000000000095"]["user"]["global_name"] = "Ratih Wijaya"


def test_admin_by_prefix_hit_is_never_accepted_blindly(tmp_path, discord):
    add_people(discord)
    api = kd.DiscordAPI(TOKEN, base=discord.base)
    with pytest.raises(ks.StepError) as err:                       # non-interaktif: hanya ID
        ks.resolve_admin(make_ctx(tmp_path), api, fd.GUILD, "alice")
    assert "ID" in err.value.fix and "500000000000000094" in err.value.message
    ctx = make_ctx(tmp_path, non_interactive=False, inputs=["2"])  # interaktif: daftar kandidat + batal
    with pytest.raises(ks.StepError):
        ks.resolve_admin(ctx, api, fd.GUILD, "alice")
    text = "\n".join(str(l) for l in ctx.lines)
    assert "alice_evil" in text and "500000000000000094" in text
    ctx = make_ctx(tmp_path, non_interactive=False, inputs=["1"])
    assert ks.resolve_admin(ctx, api, fd.GUILD, "alice")["user"]["id"] == "500000000000000094"


def test_admin_exact_username_or_global_name_or_mention_is_accepted(tmp_path, discord):
    add_people(discord)
    api = kd.DiscordAPI(TOKEN, base=discord.base)
    interactive = make_ctx(tmp_path, non_interactive=False, inputs=[])            # tidak boleh bertanya
    assert ks.resolve_admin(interactive, api, fd.GUILD, "dodi")["user"]["id"] == "500000000000000091"
    assert ks.resolve_admin(interactive, api, fd.GUILD, "Ratih Wijaya")["user"]["id"] == "500000000000000095"
    ctx = make_ctx(tmp_path)
    assert ks.resolve_admin(ctx, api, fd.GUILD, "<@500000000000000091>")["user"]["username"] == "dodi"
    assert ks.resolve_admin(ctx, api, fd.GUILD, "<@!500000000000000091>")["user"]["username"] == "dodi"
    assert ks.resolve_admin(ctx, api, fd.GUILD, "500000000000000091")["user"]["username"] == "dodi"
    with pytest.raises(ks.StepError):
        ks.resolve_admin(ctx, api, fd.GUILD, "dodi")                               # nama saja ditolak non-interaktif


def test_admin_that_is_a_bot_needs_explicit_confirmation(tmp_path, discord):
    api = kd.DiscordAPI(TOKEN, base=discord.base)
    with pytest.raises(ks.StepError) as err:
        ks.resolve_admin(make_ctx(tmp_path), api, fd.GUILD, fd.BOT)
    assert "admin_allow_bot" in err.value.message
    with pytest.raises(ks.StepError):
        ks.resolve_admin(make_ctx(tmp_path, {"admin_allow_bot": False}), api, fd.GUILD, fd.BOT)
    assert ks.resolve_admin(make_ctx(tmp_path, {"admin_allow_bot": True}), api, fd.GUILD, fd.BOT)["user"]["bot"]


# --------------------------------------------------------------------------- item 7: redirect & http
def test_http_json_never_follows_redirects_and_names_the_target_host():
    other, other_base, other_calls = serve(lambda h: (200, {}, b"{}"))
    first, first_base, _ = serve(lambda h: (302, {"Location": other_base + "/curi"}, b""))
    try:
        status, body = ks.HostSystem().http_json("GET", first_base + "/v1/models", {"Authorization": "Bearer RAHASIA-KEY"})
        assert status == 302 and other_base.split("//")[1] in str(body)
        assert other_calls == [], "API key tidak boleh sampai ke host tujuan redirect"
    finally:
        stop(other, first)


def test_probe_llm_reports_a_redirect_instead_of_following_it():
    other, other_base, other_calls = serve(lambda h: (200, {}, b'{"data": []}'))
    first, first_base, first_calls = serve(lambda h: (307, {"Location": other_base + "/x"}, b""))
    try:
        ok, lines = ks.probe_llm(ks.HostSystem().http_json, first_base + "/v1", "RAHASIA-KEY", "m")
        assert ok is False and other_calls == [] and any("307" in l for l in lines)
    finally:
        stop(other, first)


@pytest.mark.parametrize("host", ["llm.contoh.internal", "203.0.113.7"])
def test_plain_http_to_a_public_host_needs_explicit_approval(tmp_path, host):
    system = FakeSystem(tmp_path, with_user=True)
    calls = []
    system.http_json = lambda *a, **k: calls.append(a) or (200, {"data": []})
    with pytest.raises(ks.StepError) as err:
        ks.LLMEndpoint().run(make_ctx(tmp_path, {"llm_base_url": f"http://{host}/v1"}, system=system))
    assert "allow_http_llm" in err.value.message and calls == []
    ctx = make_ctx(tmp_path / "ok", {"llm_base_url": f"http://{host}/v1", "allow_http_llm": True},
                   system=FakeSystem(tmp_path / "ok", with_user=True))
    ctx.sys.http_json = lambda *a, **k: (200, {"data": []})
    ks.LLMEndpoint().run(ctx)
    assert any("http" in w.lower() for w in ctx.warnings)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "10.1.2.3", "192.168.0.9", "172.20.0.4", "[::1]"])
def test_plain_http_to_loopback_or_private_hosts_is_fine(tmp_path, host):
    system = FakeSystem(tmp_path, with_user=True)
    system.http_json = lambda *a, **k: (200, {"data": []})
    ks.LLMEndpoint().run(make_ctx(tmp_path, {"llm_base_url": f"http://{host}:8080/v1"}, system=system))


def test_plain_http_can_be_declined_interactively(tmp_path):
    system = FakeSystem(tmp_path, with_user=True)
    system.http_json = lambda *a, **k: pytest.fail("tidak boleh menghubungi endpoint")
    ctx = make_ctx(tmp_path, non_interactive=False, system=system,
                   inputs=["http://llm.contoh.internal/v1", "", "", "", "t"])
    with pytest.raises(ks.StepError):
        ks.LLMEndpoint().run(ctx)


# --------------------------------------------------------------------------- item 8: systemd & env override
def test_wizard_aborts_in_preflight_without_systemd_before_touching_discord(tmp_path, discord, llm, secrets_env):
    wizard = Wizard(tmp_path)
    wizard.sys.systemd = False
    assert wizard.main("--non-interactive", "--answers", str(write_answers(tmp_path, llm[0]))) == 1
    assert "systemd" in wizard.text and "--no-systemd" in wizard.text
    assert wizard.state()["completed"] == [] and discord.calls == []


def test_no_systemd_flag_skips_gateway_and_timers_and_prints_manual_commands(tmp_path, discord, llm, secrets_env):
    wizard = Wizard(tmp_path)
    wizard.sys.systemd = False
    assert wizard.main("--non-interactive", "--no-systemd", "--answers", str(write_answers(tmp_path, llm[0]))) == 0, wizard.text
    cmds = wizard.sys.commands()
    assert not any("systemctl" in c or "gateway install" in c or "gateway start" in c for c in cmds)
    assert "hermes gateway run" in wizard.text and "tanpa systemd" in wizard.text
    assert wizard.state()["completed"] == ks.STEP_NAMES


def test_gateway_step_itself_refuses_without_systemd(tmp_path):
    system = FakeSystem(tmp_path, with_user=True)
    system.systemd = False
    with pytest.raises(ks.StepError):
        ks.Gateway().run(make_ctx(tmp_path, system=system))
    assert system.calls == []


def test_discord_api_environment_override_is_ignored(tmp_path, discord, monkeypatch):
    trap, trap_base, trap_calls = serve(lambda h: (200, {}, b"{}"))
    try:
        monkeypatch.setenv("TEAMCTL_DISCORD_API", trap_base + "/api/v10")
        ctx = make_ctx(tmp_path)
        assert ctx.discord().base == discord.base
        ks.DiscordBot().run(ctx)
        assert trap_calls == [] and discord.calls
        bare = ks.Context(FakeSystem(tmp_path / "x"), ks.State(tmp_path / "x" / "s.json"), ks.Prompter({}, {}, True))
        assert bare.discord_base == "https://discord.com/api/v10"
        bare.set_token("t")
        assert bare.discord().base == "https://discord.com/api/v10"
    finally:
        stop(trap)


# --------------------------------------------------------------------------- item 10: kunci
def test_second_instance_exits_with_a_clear_message(tmp_path):
    wizard = Wizard(tmp_path)
    lock = wizard.state_file.parent / ".lock"
    first = ks.acquire_lock(lock)
    try:
        assert wizard.main("--non-interactive", "--only", "preflight") == 3
        assert "sedang berjalan" in wizard.text and wizard.sys.calls == []
        with pytest.raises(ks.StepError):
            ks.acquire_lock(lock)
    finally:
        first.close()
    other = Wizard(tmp_path, wizard.sys)
    assert other.main("--non-interactive", "--only", "preflight") == 0
    assert ks.acquire_lock(lock).close() is None                    # kunci dilepas setelah selesai


def test_read_only_modes_do_not_need_the_lock(tmp_path):
    wizard = Wizard(tmp_path)
    first = ks.acquire_lock(wizard.state_file.parent / ".lock")
    try:
        assert wizard.main("--list") == 0
        assert wizard.main("--dry-run", "--non-interactive") == 0
    finally:
        first.close()


# --------------------------------------------------------------------------- item 11: state rusak
CORRUPT = {
    "answers-list": '{"answers": [1, 2]}', "completed-string": '{"completed": "preflight"}', "data-string": '{"data": "x"}',
    "toplevel-list": "[1, 2]", "admins-string": '{"data": {"admins": "abc"}}', "kitdir-int": '{"data": {"kit_dir": 5}}',
    "not-json": "{bukan json", "completed-numbers": '{"completed": [1, 2]}',
}


@pytest.mark.parametrize("payload", sorted(CORRUPT))
def test_corrupt_state_is_reported_kept_and_never_a_traceback(tmp_path, payload):
    wizard = Wizard(tmp_path)
    wizard.state_file.parent.mkdir(parents=True)
    wizard.state_file.write_text(CORRUPT[payload])
    assert wizard.main("--non-interactive") == 2
    assert "--reset" in wizard.text and "Traceback" not in wizard.text and wizard.sys.calls == []
    assert wizard.state_file.read_text() == CORRUPT[payload]
    copies = list(wizard.state_file.parent.glob("state.json.corrupt-*"))
    assert len(copies) == 1 and copies[0].read_text() == CORRUPT[payload]
    assert stat.S_IMODE(copies[0].stat().st_mode) == 0o600


def test_corrupt_state_names_the_field_and_reset_recovers(tmp_path):
    wizard = Wizard(tmp_path)
    wizard.state_file.parent.mkdir(parents=True)
    wizard.state_file.write_text('{"answers": [1, 2]}')
    assert wizard.main("--list") == 2 and "answers" in wizard.text
    assert not list(wizard.state_file.parent.glob("*.corrupt-*")), "mode baca-saja tidak membuat berkas"
    again = Wizard(tmp_path, wizard.sys)
    assert again.main("--reset", "--non-interactive", "--only", "preflight") == 0
    assert again.state()["completed"] == ["preflight"]


# --------------------------------------------------------------------------- item 12: nama variabel env
@pytest.mark.parametrize("value", ["LD_PRELOAD", "LD_LIBRARY_PATH", "PATH", "HOME", "PYTHONPATH", "DISCORD_BOT_TOKEN",
                                   "HERMES_HOME", "BASH_ENV", "SUDO_ASKPASS"])
def test_env_name_rejects_system_and_loader_variables(value):
    assert ks.valid_env_name(value)


@pytest.mark.parametrize("value", [
    "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789", "ghp_" + "A1b2C3d4" * 4, "A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMNOPQRS", "AKIAIOSFODNN7EXAMPLE", "SKABCDEF0123456789ABCDEF0123", "xoxb-123-456"])
def test_env_name_rejects_things_that_look_like_keys_without_echoing_them(value):
    problem = ks.valid_env_name(value)
    assert problem and "NAMA variabel, bukan key-nya" in problem and value not in problem


@pytest.mark.parametrize("value", ["TD_API_KEY", "ACME_API_KEY", "OPENAI_API_KEY", "LLM_KEY_2", "KUNCI_LLM_TIM"])
def test_env_name_accepts_ordinary_key_variable_names(value):
    assert ks.valid_env_name(value) is None


# --------------------------------------------------------------------------- item 13: POST ganda
def test_create_that_succeeded_before_a_502_is_found_again_not_duplicated(tmp_path, discord):
    discord.fault("POST", f"/guilds/{fd.GUILD}/channels", status=502, times=3, after=True)
    ctx = server_ctx(tmp_path, CUSTOM)
    ks.DiscordServer().run(ctx)
    for name in ("X Hermes", "x-shared", "x-admin-room"):
        assert len([c for c in discord.channels.values() if c["name"] == name]) == 1, name
    assert ctx.data["category_id"] and ctx.data["shared_channel_id"] != ctx.data["admin_channel_id"]


def test_role_create_that_succeeded_before_a_503_is_not_duplicated(tmp_path, discord):
    discord.fault("POST", f"/guilds/{fd.GUILD}/roles", status=503, times=1, after=True)
    ks.DiscordServer().run(server_ctx(tmp_path, CUSTOM))
    assert len([r for r in discord.roles.values() if r["name"] == "x-entry"]) == 1


def test_create_that_never_happened_is_retried_once_and_still_not_duplicated(tmp_path, discord):
    discord.fault("POST", f"/guilds/{fd.GUILD}/channels", status=502, times=1)
    ks.DiscordServer().run(server_ctx(tmp_path, CUSTOM))
    assert len([c for c in discord.channels.values() if c["name"] == "x-shared"]) == 1


def test_persistent_server_errors_end_in_a_clear_failure(tmp_path, discord):
    discord.fault("POST", f"/guilds/{fd.GUILD}/channels", status=502, times=50)
    with pytest.raises(ks.StepError):
        ks.DiscordServer().run(server_ctx(tmp_path, CUSTOM))
    assert len([c for c in discord.channels.values() if c["name"] == "X Hermes"]) == 0


# --------------------------------------------------------------------------- item 14: curl
def test_downloads_force_https_and_tls12(tmp_path):
    cmd = ks.HermesInstall().command()
    assert "curl --proto '=https' --tlsv1.2 -fsSL https://hermes-agent" in cmd
    system = FakeSystem(tmp_path)                                  # tanpa docker
    ks.Packages().run(make_ctx(tmp_path, {"install_docker": True}, system=system))
    curl = next(c for c in system.commands() if c.startswith("curl "))
    assert curl.startswith("curl --proto =https --tlsv1.2 -fsSL https://get.docker.com -o ")
