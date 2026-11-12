"""Alat bantu uji teamctl_mcp.py: lingkungan sementara, Discord tiruan, dan klien MCP stdio mini.

Tidak pernah menghubungi Discord sungguhan dan tidak butuh binary `hermes` asli.
"""
import json
import os
import queue
import stat
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
KIT = HERE.parent
for _p in (str(KIT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fake_discord as fd  # noqa: E402
import hermes_team as ht  # noqa: E402
import teamctl_mcp as tm  # noqa: E402

TOKEN = "FAKE-bot-token-JANGAN-BOCOR-0123456789"
BE1, BUDI, CITRA, DODI, EKA, FAJAR = (f"50000000000000000{i}" for i in range(1, 7))
CH_BE1, CH_BUDI = "600000000000000001", "600000000000000002"
CH_RANDOM, CH_LOUNGE = "400000000000000006", "400000000000000007"
ROLE_BOT = "200000000000000099"
THREAD_BE1, THREAD_GENERAL, CH_FOREIGN = "610000000000000001", "610000000000000002", "620000000000000001"
OTHER_GUILD = "100000000000000777"
VIEW, SEND = 1 << 10, 1 << 11
MANAGE_MESSAGES, KICK, ADMINISTRATOR = 1 << 13, 1 << 1, 1 << 3


class Handler(fd.Handler):
    """Seperti Discord asli: thread dan channel guild lain tidak muncul di daftar channel guild."""

    def _route(self, m, p, body, q):
        st = self.state
        if m == "GET" and p == ["guilds", fd.GUILD, "channels"]:
            return self._send(200, [c for c in st.channels.values()
                                    if c.get("type") not in (10, 11, 12) and c.get("guild_id") == fd.GUILD])
        return super()._route(m, p, body, q)


def start_fake():
    state = fd.State()
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return state, f"http://127.0.0.1:{server.server_address[1]}/api/v10", server


def add_channel(state, cid, name, ctype=0, parent=None, overwrites=(), guild_id=fd.GUILD):
    with state.lock:
        state.channels[cid] = {"id": cid, "name": name, "type": ctype, "parent_id": parent, "position": 9,
                               "guild_id": guild_id, "topic": None,
                               "permission_overwrites": [dict(o) for o in overwrites]}
    return state.channels[cid]


def add_role(state, rid, name, perms=0, pos=2, managed=False):
    with state.lock:
        state.roles[rid] = {"id": rid, "name": name, "permissions": str(perms), "position": pos,
                            "managed": managed, "color": 0, "hoist": False, "mentionable": False}
    return state.roles[rid]


def add_user(state, uid, username, roles=(), bot=False, nick=None, global_name=None):
    with state.lock:
        state.members[uid] = {"user": {"id": uid, "username": username, "global_name": global_name or username,
                                       "bot": bot},
                              "roles": list(roles), "nick": nick, "joined_at": "2026-01-01T00:00:00+00:00",
                              "communication_disabled_until": None}
    return state.members[uid]


def overwrite(target, allow=0, deny=0, kind=0):
    return {"id": target, "type": kind, "allow": str(allow), "deny": str(deny)}


def set_overwrites(state, cid, overwrites):
    with state.lock:
        state.channels[cid]["permission_overwrites"] = [dict(o) for o in overwrites]


def mutating(state):
    """Request ke Discord tiruan yang mengubah sesuatu."""
    return [c for c in state.calls if c["method"] != "GET"]


class Env:
    """Direktori tim + HERMES_HOME sementara + Discord tiruan."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.team_dir = tmp_path / "team"
        self.team_dir.mkdir()
        self.root = tmp_path / "hermes-home"
        self.root.mkdir()
        (self.root / ".env").write_text(f"DISCORD_BOT_TOKEN={TOKEN}\n", encoding="utf-8")
        raw = yaml.safe_load((KIT / "team.example.yaml").read_text(encoding="utf-8"))
        raw["team_skills_dir"] = ""
        self.team_file = self.team_dir / "team.yaml"
        self.team_file.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        self.members_file = self.team_dir / "members.yaml"
        self.members_file.write_text(yaml.safe_dump({"members": [
            {"name": "be1", "display_name": "BE1", "discord_user_id": BE1, "channel_id": CH_BE1, "model": "td-normal"},
            {"name": "budi", "display_name": "Budi", "discord_user_id": BUDI, "channel_id": CH_BUDI},
        ]}), encoding="utf-8")
        self.bin = tmp_path / "hermes"   # `hermes` tiruan: selalu sukses, tidak melakukan apa-apa
        self.bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.state, self.base, self.http = start_fake()
        monkeypatch.setenv("TEAMCTL_DISCORD_API", self.base)
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TEAMCTL_REQUIRE_CONSENT", raising=False)
        monkeypatch.delenv("TEAMCTL_MAX_MESSAGE_CHARS", raising=False)
        monkeypatch.setenv("HERMES_TEAM_DIR", str(self.team_dir))

    def close(self):
        self.http.shutdown()
        self.http.server_close()

    def patch_team(self, **sections):
        raw = yaml.safe_load(self.team_file.read_text(encoding="utf-8"))
        for key, value in sections.items():
            if isinstance(value, dict) and isinstance(raw.get(key), dict):
                raw[key].update(value)
            else:
                raw[key] = value
        self.team_file.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    def set_members(self, members):
        self.members_file.write_text(yaml.safe_dump({"members": members}), encoding="utf-8")

    def members(self):
        return yaml.safe_load(self.members_file.read_text(encoding="utf-8")).get("members") or []

    def audit(self):
        path = self.team_dir / "state" / "audit.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def ctl(self, answer=True, **kw):
        return Ctl(self, answer=answer, **kw)

    def wire(self, **kw):
        return Wire(self, **kw)

    def argv(self):
        return [sys.executable, str(KIT / "teamctl_mcp.py"), "--team", str(self.team_file), "--members",
                str(self.members_file), "--hermes-bin", str(self.bin), "--hermes-root", str(self.root)]

    def environ(self, **extra):
        env = dict(os.environ)
        env.update({"TEAMCTL_DISCORD_API": self.base, "HERMES_TEAM_DIR": str(self.team_dir),
                    "HERMES_HOME": str(self.root)})
        env.pop("DISCORD_BOT_TOKEN", None)
        env.update(extra)
        return env


class Ctl(tm.TeamCtl):
    """TeamCtl dalam proses dengan konfirmasi tiruan (pengganti tombol Approve/Deny).

    `answer`: True/False, atau callable(preview) → bool. `before_answer(preview)` dipanggil sebelum
    menjawab — dipakai untuk mengubah keadaan server di antara konfirmasi dan eksekusi (uji TOCTOU).
    """

    def __init__(self, env: Env, answer=True, before_answer=None):
        super().__init__(env.team_file, env.members_file, str(env.bin), env.root)
        self.env, self.answer, self.before_answer = env, answer, before_answer
        self.previews: list[str] = []

    def confirm(self, message):
        self.previews.append(message)
        if self.before_answer:
            self.before_answer(message)
        ok = self.answer(message) if callable(self.answer) else self.answer
        return (True, "disetujui") if ok else (False, "ditolak admin")

    def call(self, tool, /, **args):
        return self.call_tool(tool, args)


class _Reader:
    """Sisi baca stdin server: iterator baris yang diisi lewat antrean (None = EOF)."""

    def __init__(self):
        self.q = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self):
        item = self.q.get()
        if item is None:
            raise StopIteration
        return item


class _Writer:
    def __init__(self):
        self.q = queue.Queue()

    def write(self, text):
        for line in text.splitlines():
            if line.strip():
                self.q.put(json.loads(line))

    def flush(self):
        pass


class Wire:
    """TeamCtl sungguhan (confirm asli) yang dilayani lewat JSON-RPC per baris, di dalam proses."""

    def __init__(self, env: Env, consent_timeout=20.0, elicitation=True, protocol="2025-06-18"):
        self.inp, self.out = _Reader(), _Writer()
        self.ctl = tm.TeamCtl(env.team_file, env.members_file, str(env.bin), env.root,
                              consent_timeout=consent_timeout, out=self.out, inp=self.inp)
        self.thread = threading.Thread(target=self.ctl.serve, daemon=True)
        self.thread.start()
        self.backlog: list[dict] = []
        self.next_id = 0
        if protocol:
            caps = {"elicitation": {}} if elicitation else {}
            self.request("initialize", {"protocolVersion": protocol, "capabilities": caps,
                                        "clientInfo": {"name": "uji", "version": "0"}})
            self.notify("notifications/initialized")

    def raw(self, line: str):
        self.inp.q.put(line + "\n")

    def send(self, message):
        self.raw(json.dumps(message))

    def notify(self, method, params=None):
        self.send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def post(self, method, params=None):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                   **({"params": params} if params is not None else {})})
        return self.next_id

    def wait(self, pred, timeout=15.0):
        for i, msg in enumerate(self.backlog):
            if pred(msg):
                return self.backlog.pop(i)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"tidak ada pesan yang cocok; antrean: {self.backlog}")
            try:
                msg = self.out.q.get(timeout=left)
            except queue.Empty:
                continue
            if pred(msg):
                return msg
            self.backlog.append(msg)

    def response(self, mid, timeout=15.0):
        return self.wait(lambda m: m.get("id") == mid and "method" not in m, timeout)

    def request(self, method, params=None, timeout=15.0):
        return self.response(self.post(method, params), timeout)

    def call(self, tool, /, **args):
        return self.post("tools/call", {"name": tool, "arguments": args})

    def elicitation(self, timeout=15.0):
        return self.wait(lambda m: m.get("method") == "elicitation/create", timeout)

    def answer(self, elicit, action="accept"):
        self.send({"jsonrpc": "2.0", "id": elicit["id"], "result": {"action": action, "content": {}}})

    def text(self, mid, timeout=15.0):
        res = self.response(mid, timeout)
        assert "result" in res, res
        return res["result"]["content"][0]["text"], res["result"]["isError"]

    def nothing(self, pred, seconds=0.4):
        """Pastikan tidak ada pesan yang cocok dalam `seconds`."""
        try:
            msg = self.wait(pred, seconds)
        except AssertionError:
            return
        raise AssertionError(f"pesan tak terduga: {msg}")

    def eof(self, timeout=10.0):
        self.inp.q.put(None)
        self.thread.join(timeout)
        return not self.thread.is_alive()


class Proc:
    """Server sebagai subprocess sungguhan (stdio)."""

    def __init__(self, env: Env, **extra_env):
        self.p = subprocess.Popen(env.argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", env=env.environ(**extra_env), cwd=str(env.tmp))
        self.q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.backlog = []
        self.next_id = 0

    def _pump(self):
        for line in self.p.stdout:
            if line.strip():
                self.q.put(json.loads(line))
        self.q.put(None)

    def send(self, message):
        self.p.stdin.write(json.dumps(message) + "\n")
        self.p.stdin.flush()

    def post(self, method, params=None):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        return self.next_id

    def wait(self, pred, timeout=20.0):
        for i, msg in enumerate(self.backlog):
            if pred(msg):
                return self.backlog.pop(i)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if msg is None:
                break
            if pred(msg):
                return msg
            self.backlog.append(msg)
        raise AssertionError(f"tidak ada pesan yang cocok; antrean: {self.backlog}")

    def response(self, mid, timeout=20.0):
        return self.wait(lambda m: m.get("id") == mid and "method" not in m, timeout)

    def initialize(self, elicitation=True, protocol="2025-06-18"):
        mid = self.post("initialize", {"protocolVersion": protocol,
                                       "capabilities": {"elicitation": {}} if elicitation else {},
                                       "clientInfo": {"name": "uji", "version": "0"}})
        res = self.response(mid)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res

    def close(self, timeout=15.0):
        """Tutup stdin (EOF) dan kembalikan (kode keluar, stderr)."""
        self.p.stdin.close()
        try:
            code = self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            raise AssertionError("server tidak berhenti setelah EOF")
        return code, self.p.stderr.read()
