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
        if with_user:
            self._add_user("hermes")
            self.groups.add("docker")
            self.present.update({"docker", "git"})

    def _add_user(self, name):
        home = self.tmp / "home" / name
        home.mkdir(parents=True, exist_ok=True)
        self.users[name] = ks.UserInfo(name, 1001, 1001, str(home))

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
        return ks.Result(0, "", "")

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.present else None

    def user(self, name):
        return self.users.get(name)

    def user_groups(self, name):
        return set(self.groups) if name in self.users else set()

    def chown(self, path, user, group):
        pass

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
        return True

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


@pytest.fixture
def discord(monkeypatch):
    state, base, server = fd.start()
    state.roles[BOT_ROLE]["permissions"] = str(ks.invite_permissions(True))
    monkeypatch.setenv("TEAMCTL_DISCORD_API", base)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    yield state
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
                                          "source_dir": KIT_DIR})

    @property
    def text(self):
        return "\n".join(str(line) for line in self.lines)

    def state(self):
        return json.loads(self.state_file.read_text())

    @property
    def home(self):
        return self.tmp / "home" / "hermes"


def write_answers(tmp: Path, llm_base: str, **extra) -> Path:
    data = {"install_docker": True, "install_hermes": True, "llm_base_url": llm_base, "admins": ["adminbe"],
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
                     etc_dir=tmp / "etc-hermes", linger_dir=system.linger_dir, out=lines.append)
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
    api = kd.DiscordAPI(TOKEN)
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
                   context_overrides={"etc_dir": wizard.etc, "linger_dir": wizard.sys.linger_dir, "source_dir": KIT_DIR})
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
