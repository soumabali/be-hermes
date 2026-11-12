"""Tiruan untuk menguji plugin team-guard TANPA Hermes.

- `load_plugin()`      memuat plugin persis seperti loader Hermes (hermes_cli/plugins_loader.py::
                       _load_directory_module): paket `hermes_plugins.team_guard[__home_<x>]`, impor relatif.
- `StubContext`        tiruan minimal PluginContext (hermes_cli/plugins_context.py): mencatat pendaftaran
                       dan memicu hook dengan kwargs yang didokumentasikan upstream.
- `FakeRunner` / `FakeDiscordAdapter` / `FakeUpstream`
                       meniru atribut gateway yang dipakai plugin (config.profile_routes, gerbang channel
                       `_gate_raw`, `_running_agent_count`, `request_restart`, resolver rute).
- `FakeBot` / `FakeGuild` / `FakeMember` / …   objek bergaya discord.py.
- `fake_kit_main`      `hermes_team.py onboard|offboard --events` tiruan (dijalankan sebagai subprocess).
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import os
import re
import sys
import time
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "plugins" / "team-guard"
NS = "hermes_plugins"

GUILD = "100000000000000001"
BOT = "999000000000000001"
OWNER = "300000000000000009"
ADMIN = "300000000000000001"
ROLE_MEMBER, ROLE_ADMIN, ROLE_ENTRY = "200000000000000001", "200000000000000002", "200000000000000003"
CH_SHARED, CH_ADMIN = "400000000000000001", "400000000000000002"
U_BE1, U_BUDI, U_CITRA, U_DODI, U_EKA, U_MOD = (f"50000000000000000{i}" for i in range(1, 7))
CH_BE1, CH_BUDI = "600000000000000001", "600000000000000002"


# ----------------------------------------------------------------------------- pemuat plugin
def load_plugin(suffix: str = "") -> types.ModuleType:
    """Muat (ulang) plugin sebagai paket `hermes_plugins.team_guard<suffix>` seperti loader Hermes."""
    name = f"{NS}.team_guard{suffix}"
    if NS not in sys.modules:
        ns_pkg = types.ModuleType(NS)
        ns_pkg.__path__ = []          # type: ignore[attr-defined]
        ns_pkg.__package__ = NS
        sys.modules[NS] = ns_pkg
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name
    module.__path__ = [str(PLUGIN_DIR)]   # type: ignore[attr-defined]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sub(plugin: types.ModuleType, name: str) -> types.ModuleType:
    return importlib.import_module(f"{plugin.__name__}.{name}")


# ----------------------------------------------------------------------------- PluginContext tiruan
VALID_HOOKS = {
    "pre_tool_call", "post_tool_call", "transform_terminal_output", "transform_tool_result",
    "transform_llm_output", "pre_llm_call", "post_llm_call", "pre_api_request", "post_api_request",
    "api_request_error", "pre_auxiliary_call", "post_auxiliary_call",
    "on_session_start", "on_session_end", "on_session_finalize", "on_session_reset",
    "pre_gateway_dispatch", "post_gateway_admission", "gateway_platform_event", "pre_command",
}
VALID_MIDDLEWARE = {"tool_request", "tool_execution", "llm_request", "llm_execution"}
_SECTION_ID = re.compile(r"^[a-z0-9._-]{1,128}$")


class StubContext:
    """Hanya permukaan yang dipakai plugin; validasinya mengikuti upstream."""

    def __init__(self, settings: Optional[dict] = None, profile_name: str = "default"):
        self.settings = dict(settings or {})
        self._profile_name = profile_name
        self.hooks: dict[str, list[Callable]] = {}
        self.hook_options: dict[str, dict] = {}
        self.middleware: dict[str, list[Callable]] = {}
        self.platform_handlers: dict[str, list[Callable]] = {}
        self.prompt_sections: dict[str, dict] = {}
        self.redaction_patterns: list[str] = []
        self.unload_callbacks: list[Callable] = []
        self.spawned: list[asyncio.Task] = []
        self.unknown_hooks: list[str] = []

    @property
    def profile_name(self) -> str:
        return self._profile_name

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def register_hook(self, hook_name: str, callback: Callable, *, hold_stream: bool = False):
        if hook_name not in VALID_HOOKS:
            self.unknown_hooks.append(hook_name)
        if not callable(callback):
            raise TypeError("hook callback must be callable")
        self.hooks.setdefault(hook_name, []).append(callback)
        self.hook_options[hook_name] = {"hold_stream": hold_stream}

    def register_middleware(self, kind: str, callback: Callable):
        if kind not in VALID_MIDDLEWARE:
            self.unknown_hooks.append(kind)
        self.middleware.setdefault(kind, []).append(callback)

    def register_platform_handler(self, platform: str, factory: Callable) -> None:
        if not callable(factory):
            raise ValueError("non-callable factory")
        key = (platform or "").strip().lower()
        if not key:
            raise ValueError("empty platform name")
        self.platform_handlers.setdefault(key, []).append(factory)

    def register_system_prompt_section(self, id: str, content: Any, *, position: str = "after_memory",
                                       max_chars: int = 4000):
        if not _SECTION_ID.match(id):
            raise ValueError("invalid section id")
        if not isinstance(content, str) and not callable(content):
            raise TypeError("content must be str or callable")
        if position != "after_memory":
            raise ValueError("invalid position")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 0 < max_chars <= 4000:
            raise ValueError("invalid max_chars")
        if id in self.prompt_sections:
            raise ValueError("duplicate section id")
        self.prompt_sections[id] = {"content": content, "max_chars": max_chars}

    def register_redaction_patterns(self, patterns) -> int:
        accepted = 0
        for pattern in patterns or []:
            re.compile(pattern)
            if "|" in re.sub(r"\([^)]*\)|\[[^\]]*\]", "", pattern):
                continue                       # alternasi tingkat atas ditolak upstream
            if not re.match(r"[A-Za-z0-9_-]{2}", pattern):
                continue                       # wajib >= 2 karakter literal di depan
            self.redaction_patterns.append(pattern)
            accepted += 1
        return accepted

    def on_unload(self, callback: Callable):
        if not callable(callback):
            raise TypeError("on_unload callback must be callable")
        self.unload_callbacks.append(callback)

    def spawn_task(self, coro, *, name: Optional[str] = None) -> asyncio.Task:
        if not asyncio.iscoroutine(coro):
            raise TypeError("spawn_task expects a coroutine")
        task = asyncio.get_running_loop().create_task(coro, name=name or "plugin:task")
        self.spawned.append(task)
        return task

    # --- pemicu (meniru invoke_hook: kwargs + telemetry_schema_version, hasil None dibuang)
    def fire(self, hook_name: str, **kwargs: Any) -> list:
        kwargs.setdefault("telemetry_schema_version", "hermes.observer.v1")
        return [r for r in (cb(**kwargs) for cb in self.hooks.get(hook_name, [])) if r is not None]

    async def afire(self, hook_name: str, **kwargs: Any) -> list:
        kwargs.setdefault("telemetry_schema_version", "hermes.observer.v1")
        out = []
        for cb in self.hooks.get(hook_name, []):
            result = cb(**kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            if result is not None:
                out.append(result)
        return out

    def fire_middleware(self, kind: str, **kwargs: Any) -> list:
        kwargs.setdefault("middleware_schema_version", "hermes.middleware.v1")
        return [r for r in (cb(**kwargs) for cb in self.middleware.get(kind, [])) if r is not None]

    def render_prompt_section(self, id: str, **info: Any) -> str:
        content = self.prompt_sections[id]["content"]
        return content(info) if callable(content) else content


# ----------------------------------------------------------------------------- gateway tiruan
class FakePlatform:
    def __init__(self, value: str):
        self.value = value

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, FakePlatform) and other.value == self.value


DISCORD = FakePlatform("discord")
TELEGRAM = FakePlatform("telegram")


@dataclass
class FakeSource:
    user_id: Optional[str] = None
    chat_id: str = ""
    platform: Any = DISCORD
    chat_type: str = "group"
    guild_id: Optional[str] = GUILD
    thread_id: Optional[str] = None
    parent_chat_id: Optional[str] = None
    is_bot: bool = False
    profile: Optional[str] = None


@dataclass
class FakeEvent:
    text: str = ""
    source: Any = None
    raw_message: Any = None
    internal: bool = False
    message_id: Optional[str] = None


@dataclass(frozen=True)
class FakeRoute:
    """Salinan semantik gateway/profile_routing.py::ProfileRoute."""
    name: str
    platform: str
    profile: str
    guild_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    enabled: bool = True
    bot_profile: Optional[str] = None
    user_id: Optional[str] = None

    @property
    def specificity(self) -> int:
        return 2 * bool(self.guild_id) + 4 * bool(self.chat_id) + 8 * bool(self.thread_id) + 16 * bool(self.user_id)

    def matches(self, platform, guild_id=None, chat_id=None, thread_id=None, parent_chat_id=None,
                adapter_profile=None, user_id=None) -> bool:
        if not self.enabled or self.platform != platform:
            return False
        if (self.bot_profile or None) != (adapter_profile or None):
            return False
        if self.user_id is not None and (not self.user_id.strip() or self.user_id != user_id):
            return False
        if self.thread_id and self.thread_id != thread_id:
            return False
        if self.chat_id and self.chat_id not in (chat_id, parent_chat_id):
            return False
        return not (self.guild_id and self.guild_id != guild_id)


class FakeUpstream:
    """Antarmuka yang sama dengan livereload.Upstream, tanpa Hermes."""

    def __init__(self, served: Optional[set] = None):
        self.served = served            # None → "tidak bisa diperiksa"
        self.rescans = 0
        self.restarts: list = []
        self.break_parse = False
        self.break_match = False
        self.rescan_answers = True

    def parse_profile_routes(self, raw):
        if self.break_parse:
            raise ImportError("No module named 'gateway.profile_routing'")
        routes = []
        for entry in raw or []:
            if not isinstance(entry, dict) or not entry.get("platform") or not entry.get("profile"):
                continue
            routes.append(FakeRoute(
                name=entry.get("name", ""), platform=entry["platform"], profile=entry["profile"],
                guild_id=_s(entry.get("guild_id")), chat_id=_s(entry.get("chat_id")),
                thread_id=_s(entry.get("thread_id")), user_id=_s(entry.get("user_id")),
                enabled=entry.get("enabled", True)))
        routes.sort(key=lambda r: r.specificity, reverse=True)
        return routes

    def match_profile_route(self, routes, **kwargs):
        if self.break_match:
            raise TypeError("match_profile_route() got an unexpected keyword argument")
        for route in routes:
            if route.matches("discord", **kwargs):
                return route
        return None

    def rescan(self, home):
        self.rescans += 1
        if not self.rescan_answers or self.served is None:
            return None
        return {"served_profiles": sorted(self.served), "added": [], "removed": []}

    def served_profiles(self, config):
        return None if self.served is None else set(self.served)

    def restart(self, runner):
        self.restarts.append(runner)
        runner.request_restart(detached=False, via_service=True)


def _s(value: Any) -> Optional[str]:
    return None if value is None else str(value)


class FakeGatewayConfig:
    def __init__(self, routes: Optional[list] = None):
        self.profile_routes = list(routes or [])
        self.multiplex_profiles = True


class FakePlatformConfig:
    def __init__(self, extra: Optional[dict] = None):
        self.extra = dict(extra or {})


class FakeDiscordAdapter:
    """Meniru gerbang DiscordAdapter (plugins/platforms/discord/adapter.py): `_gate_raw` membaca snapshot
    env dulu lalu config.extra; free_response_channels membaca extra dulu lalu snapshot."""

    def __init__(self, runner: Any = None, *, snapshot: Optional[dict] = None, extra: Optional[dict] = None):
        self.gateway_runner = runner
        self._gate_env_snapshot = snapshot
        self.config = FakePlatformConfig(extra)
        self.sent: list[dict] = []

    def _gate_env(self, name: str, default: str = "") -> str:
        snap = getattr(self, "_gate_env_snapshot", None)
        if snap is not None and name in snap:
            return snap[name] or default
        return os.environ.get(name, default)

    def _gate_raw(self, extra_key: str, env_key: str):
        val = self._gate_env(env_key)
        if val:
            return val
        extra = getattr(getattr(self, "config", None), "extra", None)
        return extra.get(extra_key) if isinstance(extra, dict) else None

    @staticmethod
    def _gate_csv_set(raw) -> set:
        if raw is None:
            return set()
        if isinstance(raw, list):
            return {str(p).strip() for p in raw if str(p).strip()}
        return {p.strip() for p in str(raw).split(",") if p.strip()}

    def _get_allowed_channels(self) -> set:
        return self._gate_csv_set(self._gate_raw("allowed_channels", "DISCORD_ALLOWED_CHANNELS"))

    def _discord_free_response_channels(self) -> set:
        raw = self.config.extra.get("free_response_channels")
        if raw is None:
            raw = self._gate_env("DISCORD_FREE_RESPONSE_CHANNELS")
        return self._gate_csv_set(raw)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": str(chat_id), "content": content, "metadata": metadata})
        return types.SimpleNamespace(success=True)


class FakeRunner:
    def __init__(self, routes: Optional[list] = None):
        self.config = FakeGatewayConfig(routes)
        self.adapters: dict = {}
        self._running_agents: dict = {}
        self.restart_requests: list[dict] = []
        self.session_store = None

    def _running_agent_count(self) -> int:
        return len(self._running_agents)

    def request_restart(self, *, detached: bool = False, via_service: bool = False) -> bool:
        self.restart_requests.append({"detached": detached, "via_service": via_service})
        return True

    def _session_key_for_source(self, source) -> str:
        return f"{source.chat_id}:{source.user_id}"

    def _is_session_running(self, key: str) -> bool:
        return key in self._running_agents


# ----------------------------------------------------------------------------- Discord tiruan
class FakeRole:
    def __init__(self, role_id: Any):
        self.id = int(role_id)


class FakeMessage:
    _ids = iter(range(900000000000000001, 990000000000000000))

    def __init__(self, channel: "FakeChannel", content: str, allowed_mentions: Any = None):
        self.id = next(FakeMessage._ids)
        self.channel, self.content, self.allowed_mentions = channel, content, allowed_mentions
        self.deleted = False

    async def delete(self) -> None:
        if self.channel.forbid_delete:
            raise PermissionError("Missing Permissions")
        self.deleted = True


class FakeChannel:
    def __init__(self, channel_id: Any, bot: Optional["FakeBot"] = None, parent_id: Any = None):
        self.id = int(channel_id)
        self.parent_id = int(parent_id) if parent_id else None
        self.bot = bot
        self.messages: list[FakeMessage] = []
        self.forbid_send = False
        self.forbid_delete = False

    async def send(self, content: str = "", *, allowed_mentions: Any = None, **_kw: Any) -> FakeMessage:
        if self.forbid_send:
            raise PermissionError("Missing Access")
        message = FakeMessage(self, content, allowed_mentions)
        self.messages.append(message)
        if self.bot is not None:
            self.bot.sent.append(message)
        return message

    def get_partial_message(self, message_id: int) -> FakeMessage:
        for message in self.messages:
            if message.id == int(message_id):
                return message
        return FakeMessage(self, "")

    def visible(self) -> list[FakeMessage]:
        return [m for m in self.messages if not m.deleted]


class FakeMember:
    def __init__(self, user_id: Any, name: str = "", roles: tuple = (), *, guild: Any = None, bot: bool = False,
                 pending: bool = False, display_name: str = ""):
        self.id = int(user_id)
        self.name = name or f"user{str(user_id)[-3:]}"
        self.display_name = display_name or self.name.title()
        self.roles = [FakeRole(r) for r in roles]
        self.guild, self.bot, self.pending = guild, bot, pending
        self.role_log: list[tuple[str, str, str]] = []
        self.forbid_roles = False

    def role_ids(self) -> set[str]:
        return {str(r.id) for r in self.roles}

    def copy(self, roles: Optional[tuple] = None, pending: Optional[bool] = None) -> "FakeMember":
        clone = FakeMember(self.id, self.name, tuple(self.role_ids()) if roles is None else roles, guild=self.guild,
                           bot=self.bot, pending=self.pending if pending is None else pending,
                           display_name=self.display_name)
        return clone

    async def add_roles(self, *roles: Any, reason: Optional[str] = None) -> None:
        if self.forbid_roles:
            raise PermissionError("Missing Permissions")
        for role in roles:
            if str(role.id) not in self.role_ids():
                self.roles.append(FakeRole(role.id))
            self.role_log.append(("add", str(role.id), reason or ""))

    async def remove_roles(self, *roles: Any, reason: Optional[str] = None) -> None:
        if self.forbid_roles:
            raise PermissionError("Missing Permissions")
        gone = {str(r.id) for r in roles}
        self.roles = [r for r in self.roles if str(r.id) not in gone]
        for role in roles:
            self.role_log.append(("remove", str(role.id), reason or ""))


class FakeAuditEntry:
    def __init__(self, target_id: Any, actor_id: Any, role_ids: tuple, *, age: timedelta = timedelta(seconds=2),
                 action: Any = None):
        self.target = types.SimpleNamespace(id=int(target_id))
        self.user = types.SimpleNamespace(id=int(actor_id)) if actor_id else None
        self.after = types.SimpleNamespace(roles=[FakeRole(r) for r in role_ids])
        self.before = types.SimpleNamespace(roles=[])
        self.created_at = datetime.now(timezone.utc) - age
        self.action = action


class FakeGuild:
    def __init__(self, guild_id: Any = GUILD, owner_id: Any = OWNER):
        self.id = int(guild_id)
        self.owner_id = int(owner_id)
        self._members: dict[int, FakeMember] = {}
        self.audit: list[FakeAuditEntry] = []
        self.audit_forbidden = False
        self.audit_calls: list[dict] = []
        self.chunked = True

    @property
    def members(self) -> list[FakeMember]:
        return list(self._members.values())

    def add(self, member: FakeMember) -> FakeMember:
        member.guild = self
        self._members[member.id] = member
        return member

    def drop(self, user_id: Any) -> Optional[FakeMember]:
        return self._members.pop(int(user_id), None)

    def get_member(self, user_id: int) -> Optional[FakeMember]:
        return self._members.get(int(user_id))

    async def fetch_member(self, user_id: int) -> FakeMember:
        member = self._members.get(int(user_id))
        if member is None:
            raise LookupError("Unknown Member")
        return member

    async def chunk(self) -> None:
        self.chunked = True

    def grant(self, target_id: Any, actor_id: Any, role_id: Any = ROLE_MEMBER, **kw: Any) -> None:
        """Catat pemberian role di audit log (seperti yang dilakukan Discord)."""
        self.audit.append(FakeAuditEntry(target_id, actor_id, (role_id,), **kw))

    async def audit_logs(self, *, limit: int = 100, after: Any = None, action: Any = None,
                         oldest_first: Any = None, **_kw: Any):
        self.audit_calls.append({"limit": limit, "after": after, "action": action})
        if self.audit_forbidden:
            raise PermissionError("Missing Permissions")
        entries = sorted(self.audit, key=lambda e: e.created_at, reverse=not oldest_first)
        for entry in entries[:limit]:
            if after is not None and entry.created_at < after:
                continue
            yield entry


class FakeIntents:
    def __init__(self, members: bool = True):
        self.members = members


class FakeBot:
    """Bagian `discord.ext.commands.Bot` yang dipakai plugin."""

    def __init__(self, guild: Optional[FakeGuild] = None):
        self.user = types.SimpleNamespace(id=int(BOT))
        self.guild = guild or FakeGuild()
        self.channels: dict[int, FakeChannel] = {}
        self.listeners: dict[str, list[Callable]] = {}
        self.sent: list[FakeMessage] = []
        self.intents = FakeIntents(True)
        self.tree = None
        self.fetches = 0

    def get_guild(self, guild_id: int) -> Optional[FakeGuild]:
        return self.guild if int(guild_id) == self.guild.id else None

    def channel(self, channel_id: Any) -> FakeChannel:
        cid = int(channel_id)
        if cid not in self.channels:
            self.channels[cid] = FakeChannel(cid, self)
        return self.channels[cid]

    def get_channel(self, channel_id: int) -> Optional[FakeChannel]:
        return self.channels.get(int(channel_id))

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        self.fetches += 1
        return self.channel(channel_id)

    def add_listener(self, func: Callable, name: Optional[str] = None) -> None:
        if not inspect.iscoroutinefunction(func):
            raise TypeError("Listeners must be coroutines")
        self.listeners.setdefault(name or func.__name__, []).append(func)

    def remove_listener(self, func: Callable, name: Optional[str] = None) -> None:
        items = self.listeners.get(name or func.__name__, [])
        if func in items:
            items.remove(func)

    async def wait_until_ready(self) -> None:
        return None

    async def dispatch(self, event: str, *args: Any) -> None:
        for listener in list(self.listeners.get("on_" + event, [])):
            await listener(*args)

    def texts(self, channel_id: Any, include_deleted: bool = False) -> list[str]:
        channel = self.channels.get(int(channel_id))
        if channel is None:
            return []
        return [m.content for m in channel.messages if include_deleted or not m.deleted]


# ----------------------------------------------------------------------------- roster & kit tiruan
def base_roster(state_dir: Path, hermes_root: Path, kit_script: Optional[Path] = None, **overrides: Any) -> dict:
    members = [("be1", U_BE1, CH_BE1, "td-normal"), ("budi", U_BUDI, CH_BUDI, "td-simple")]
    data: dict[str, Any] = {
        "version": 3, "kit_version": "3.0.0", "team_name": "Backend", "guild_id": GUILD,
        "hermes_root": str(hermes_root), "hermes_bin": "hermes", "admins": [ADMIN],
        "roles": {"member": ROLE_MEMBER, "admin": ROLE_ADMIN, "entry": ROLE_ENTRY},
        "shared_channel": CH_SHARED, "admin_channel": CH_ADMIN, "category_id": "400000000000000009",
        "channels": {}, "users": {}, "inactive_users": {}, "reserved_env": ["TD_API_KEY"],
        "gateway": {}, "live_reload": True, "restart_fallback": "idle",
        "onboarding": {
            "enabled": True, "give_entry_role": True, "remove_entry_role": True, "verify_actor": True,
            "trusted_actor_ids": [], "offboard_on_role_remove": True, "hide_channel_on_offboard": True,
            "entry_role_on_offboard": True, "reconcile_minutes": 10, "admin_notices": True,
            "welcome_message": "Halo {mention}, selamat datang di tim {team_name}! Model: {model}. "
                               "Tim: {shared_channel}. Profile {profile}.",
            "welcome_back_message": "Halo lagi {mention}, ruang kerja Hermes-mu aktif kembali ({model}).",
        },
        "kit": {"python": sys.executable, "script": str(kit_script or state_dir / "fake_hermes_team.py"),
                "team_file": str(state_dir.parent / "team.yaml"),
                "members_file": str(state_dir.parent / "members.yaml")},
        "settings": {"rate_per_minute": 6, "burst": 4, "max_active": 8, "queue_max_wait": 180, "max_keys": 40,
                     "min_length": 6, "scrub_chat": True, "delete_secret_messages": True, "block_env_dump": True,
                     "hint": True, "allow_names": [], "deny_names": []},
    }
    for name, uid, cid, model in members:
        roster_add(data, uid, name, cid, model=model)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    roster_rebuild(data)
    return data


def roster_add(data: dict, uid: str, name: str, channel_id: str, model: str = "td-simple",
               display_name: str = "") -> dict:
    data["inactive_users"].pop(uid, None)
    user = {"name": name, "profile": f"be-{name}", "channel_id": channel_id,
            "display_name": display_name or name.title(), "model": model}
    data["users"][uid] = user
    roster_rebuild(data)
    return user


def roster_deactivate(data: dict, uid: str) -> Optional[dict]:
    user = data["users"].pop(uid, None)
    if user is not None:
        data["inactive_users"][uid] = {"name": user["name"], "channel_id": user["channel_id"]}
    roster_rebuild(data)
    return user


def roster_rebuild(data: dict) -> None:
    """Hitung ulang channels + gateway persis seperti roster_data()/build_routes() di hermes_team.py."""
    channels: dict[str, dict] = {}
    routes = []
    for uid, user in data["users"].items():
        channels[user["channel_id"]] = {"profile": user["profile"], "kind": "personal", "owner": uid,
                                        "name": user["name"]}
        routes.append({"name": f"personal-{user['name']}", "platform": "discord", "guild_id": data["guild_id"],
                       "chat_id": user["channel_id"], "profile": user["profile"]})
    channels[data["shared_channel"]] = {"profile": "team-shared", "kind": "shared"}
    channels[data["admin_channel"]] = {"profile": "team-admin", "kind": "admin"}
    routes.append({"name": "team-shared", "platform": "discord", "guild_id": data["guild_id"],
                   "chat_id": data["shared_channel"], "profile": "team-shared"})
    for i, admin in enumerate(data["admins"]):
        routes.append({"name": f"team-admin-{i + 1}", "platform": "discord", "guild_id": data["guild_id"],
                       "chat_id": data["admin_channel"], "user_id": admin, "profile": "team-admin"})
    personal = [u["channel_id"] for u in data["users"].values()]
    data["channels"] = channels
    data["gateway"] = {"profile_routes": routes,
                       "allowed_channels": personal + [data["shared_channel"], data["admin_channel"]],
                       "free_response_channels": personal + [data["admin_channel"]]}


def write_roster(state_dir: Path, data: dict) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "roster.json"
    tmp = state_dir / f".roster.{os.getpid()}.{time.monotonic_ns()}.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_roster(state_dir: Path) -> dict:
    return json.loads((state_dir / "roster.json").read_text(encoding="utf-8"))


KIT_LAUNCHER = """import sys
sys.path.insert(0, {tests!r})
import plugin_stubs
plugin_stubs.fake_kit_main(sys.argv[1:], {state!r})
"""


def install_fake_kit(state_dir: Path, **plan: Any) -> Path:
    """Tulis `hermes_team.py` tiruan + rencananya. plan: fail=[uid], partial=[uid], crash=[uid], delay=detik."""
    state_dir.mkdir(parents=True, exist_ok=True)
    script = state_dir / "fake_hermes_team.py"
    script.write_text(KIT_LAUNCHER.format(tests=str(Path(__file__).resolve().parent), state=str(state_dir)),
                      encoding="utf-8")
    set_kit_plan(state_dir, **plan)
    return script


def set_kit_plan(state_dir: Path, **plan: Any) -> None:
    (state_dir / "kit_plan.json").write_text(json.dumps(plan), encoding="utf-8")


def kit_calls(state_dir: Path) -> list[dict]:
    path = state_dir / "kit_calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fake_kit_main(argv: list[str], state_dir: str) -> None:
    """Protokol `--events` hermes_team.py (_cmd_membership): satu objek JSON per baris di stdout;
    kode keluar 0 sukses, 3 bila ada `failures`, 1 bila TeamError (event `error`)."""
    import argparse
    state = Path(state_dir)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["onboard", "offboard"])
    parser.add_argument("-c", "--config")
    parser.add_argument("--members")
    parser.add_argument("--hermes-bin")
    parser.add_argument("--hermes-root")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--username", default="")
    parser.add_argument("--display-name", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--keep-channel-access", action="store_true")
    parser.add_argument("--events", action="store_true")
    args = parser.parse_args(argv)
    plan = json.loads((state / "kit_plan.json").read_text(encoding="utf-8"))
    uid = args.user_id
    started = time.time()

    def emit(event: str, **fields: Any) -> None:
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)

    def done(code: int) -> None:
        with open(state / "kit_calls.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"command": args.command, "user_id": uid, "username": args.username,
                                 "display_name": args.display_name, "events": args.events, "start": started,
                                 "end": time.time(), "code": code, "config": args.config,
                                 "members": args.members, "hermes_root": args.hermes_root,
                                 "keep": args.keep_channel_access,
                                 "hermes_home": os.environ.get("HERMES_HOME")}) + "\n")
        sys.exit(code)

    if uid in plan.get("crash", []):
        print("Traceback (most recent call last):\nRuntimeError: disk penuh", file=sys.stderr)
        done(1)
    data = read_roster(state)
    member, status, notes = None, "", []
    if args.command == "onboard":
        if uid in plan.get("fail", []):
            emit("error", message=f"user {uid} tidak ditemukan di server Discord (404)", log=[])
            done(1)
        if uid in data["users"]:
            member, status = data["users"][uid], "exists"
        else:
            old = data["inactive_users"].get(uid)
            name = (old or {}).get("name") or re.sub(r"[^a-z0-9_-]+", "-", args.username.lower()).strip("-") \
                or f"user{uid[-4:]}"
            channel = (old or {}).get("channel_id") or "61" + uid[-16:]
            emit("channel", channel_id=channel, created=old is None)
            time.sleep(float(plan.get("delay", 0)))
            member = roster_add(data, uid, name, channel, model=args.model or "td-simple",
                                display_name=args.display_name or args.username)
            status = "reactivated" if old else "created"
            notes.append(f"channel <#{channel}> dibuat" if not old else "channel lama dipakai lagi")
            write_roster(state, data)
    else:
        time.sleep(float(plan.get("delay", 0)))
        if uid in data["users"]:
            member = roster_deactivate(data, uid)
            status = "deactivated"
            write_roster(state, data)
        elif uid in data["inactive_users"]:
            member, status = data["inactive_users"][uid], "already-inactive"
        else:
            status = "not-member"
    failures = ["hermes -p be-x config set model.default  →  exit 1"] if uid in plan.get("partial", []) else []
    member = member or {}
    emit("result", status=status, user_id=uid, name=member.get("name"), display_name=member.get("display_name"),
         profile=member.get("profile") or (f"be-{member['name']}" if member.get("name") else None),
         channel_id=member.get("channel_id"), model=member.get("model", ""), notes=notes,
         restart_needed=False, live_changed=["gateway.profile_routes"], failures=failures)
    done(3 if failures else 0)


# ----------------------------------------------------------------------------- buku besar pemakaian tiruan
LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    day TEXT NOT NULL, user_key TEXT NOT NULL, profile TEXT NOT NULL, model TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    requests INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_key, profile, model, source));
"""


def ledger_add(path: Path, day: str, user_key: str, input_tokens: int = 0, output_tokens: int = 0,
               profile: str = "be-x", model: str = "td-simple") -> None:
    """Tambah pemakaian ke buku besar tiruan (skema tabel `daily` sama dengan usage_ledger.py)."""
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(LEDGER_SCHEMA)
        con.execute(
            "INSERT INTO daily (day, user_key, profile, model, source, requests, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, '', 1, ?, ?) ON CONFLICT(day, user_key, profile, model, source) DO UPDATE SET "
            "requests = requests + 1, input_tokens = input_tokens + excluded.input_tokens, "
            "output_tokens = output_tokens + excluded.output_tokens",
            (day, str(user_key), profile, model, int(input_tokens), int(output_tokens)))
        con.commit()
    finally:
        con.close()


def api_request(ctx: "StubContext", request_id: str, session_id: str = "s", **extra: Any) -> None:
    """Meniru agent/turn_api_request.py: hook pengamat sebelum request ke provider."""
    ctx.fire("pre_api_request", **{**dict(
        task_id="t", turn_id="turn", api_request_id=request_id, session_id=session_id, platform="discord",
        model="td-simple", provider="custom:td", api_call_count=1, retry_count=0, message_count=3,
        request={"method": "POST", "body": {}}), **extra})


def api_response(ctx: "StubContext", request_id: str, session_id: str = "s", **extra: Any) -> None:
    ctx.fire("post_api_request", **{**dict(
        task_id="t", turn_id="turn", api_request_id=request_id, session_id=session_id, platform="discord",
        model="td-simple", provider="custom:td", api_duration=1.0,
        usage={"prompt_tokens": 10, "completion_tokens": 5}), **extra})


def api_error(ctx: "StubContext", request_id: str, session_id: str = "s", status_code: Any = 429,
              reason: Any = "rate_limit", error: Any = None, **extra: Any) -> None:
    """Meniru agent/api_request_hooks.py::_invoke_api_request_error_hook."""
    ctx.fire("api_request_error", **{**dict(
        task_id="t", turn_id="turn", api_request_id=request_id, session_id=session_id, platform="discord",
        model="td-simple", provider="custom:td", status_code=status_code, retry_count=0, max_retries=3,
        retryable=True, reason=reason,
        error=error if error is not None else {"type": "RateLimitError", "message": "Too Many Requests"},
        request={"method": "POST", "body": {}}), **extra})


# ----------------------------------------------------------------------------- lingkungan uji siap pakai
async def fast_sleep(delay: float) -> None:
    await asyncio.sleep(min(max(delay, 0.0), 0.01))


class Clock:
    """Jam yang bisa dimajukan; `sleep` memajukannya tanpa menunggu sungguhan."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@dataclass
class Env:
    """Satu instalasi tiruan: folder state, akar Hermes dengan profile, roster, kit, gateway, bot."""
    tmp: Path
    plugin: types.ModuleType
    state_dir: Path = field(init=False)
    hermes_root: Path = field(init=False)
    roster: dict = field(init=False)

    def __post_init__(self) -> None:
        self.state_dir = self.tmp / "hermes-team" / "state"
        self.hermes_root = self.tmp / "hermes"
        for profile in ("be-be1", "be-budi", "team-shared", "team-admin"):
            (self.hermes_root / "profiles" / profile).mkdir(parents=True, exist_ok=True)
        install_fake_kit(self.state_dir)
        self.roster = base_roster(self.state_dir, self.hermes_root)
        self.save()
        self.upstream = FakeUpstream(served=None)
        self.runner = FakeRunner(self.upstream.parse_profile_routes(self.roster["gateway"]["profile_routes"]))
        gw = self.roster["gateway"]
        self.adapter = FakeDiscordAdapter(
            self.runner,
            snapshot={"DISCORD_ALLOWED_CHANNELS": ",".join(gw["allowed_channels"]),
                      "DISCORD_FREE_RESPONSE_CHANNELS": ",".join(gw["free_response_channels"])},
            extra={"allowed_channels": ",".join(gw["allowed_channels"]),
                   "free_response_channels": ",".join(gw["free_response_channels"])})
        self.runner.adapters[DISCORD] = self.adapter
        self.bot = FakeBot()
        g = self.bot.guild
        g.add(FakeMember(OWNER, "owner"))
        g.add(FakeMember(ADMIN, "adminbe", (ROLE_ADMIN,)))
        g.add(FakeMember(U_BE1, "be1", (ROLE_MEMBER,)))
        g.add(FakeMember(U_BUDI, "budi", (ROLE_MEMBER,)))
        g.add(FakeMember(BOT, "hermes", (), bot=True))
        self.ctxs: dict[str, StubContext] = {}

    # --- roster
    def save(self) -> None:
        write_roster(self.state_dir, self.roster)

    def reload(self) -> dict:
        self.roster = read_roster(self.state_dir)
        return self.roster

    def set(self, section: str, **values: Any) -> None:
        self.roster[section].update(values)
        self.save()

    def home(self, profile: str = "default") -> Path:
        return self.hermes_root if profile == "default" else self.hermes_root / "profiles" / profile

    # --- plugin
    def rt(self) -> Any:
        return sub(self.plugin, "runtime").runtime_for(self.state_dir)

    def register(self, profile: str = "default", plugin: Optional[types.ModuleType] = None,
                 **settings: Any) -> StubContext:
        ctx = StubContext({"state_dir": str(self.state_dir), **settings}, profile_name=profile)
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home(profile))
        try:
            (plugin or self.plugin).register(ctx)
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old
        self.ctxs[profile] = ctx
        return ctx

    def attach(self, **bridge_kwargs: Any) -> Any:
        """Seperti `_wire_plugin_handlers`: panggil factory(native, adapter) di dalam event loop."""
        events = sub(self.plugin, "discord_events")
        factory = events.make_factory(self.rt(), self.ctxs.get("default"), upstream=self.upstream, **bridge_kwargs)
        factory(self.bot, self.adapter)
        bridge = self.rt().host
        bridge.onboarder.sleep = fast_sleep
        bridge.live.sleep = fast_sleep
        return bridge

    # --- Discord
    def member(self, uid: str, name: str = "", roles: tuple = (), **kw: Any) -> FakeMember:
        return self.bot.guild.add(FakeMember(uid, name, roles, **kw))

    async def grant(self, uid: str, actor: Optional[str] = ADMIN, role: str = ROLE_MEMBER) -> FakeMember:
        """Admin (atau siapa pun) memberi role: audit log + event member_update."""
        member = self.bot.guild.get_member(int(uid))
        before = member.copy()
        member.roles.append(FakeRole(role))
        if actor:
            self.bot.guild.grant(uid, actor, role)
        await self.bot.dispatch("member_update", before, member)
        return member

    async def revoke(self, uid: str, role: str = ROLE_MEMBER) -> FakeMember:
        member = self.bot.guild.get_member(int(uid))
        before = member.copy()
        member.roles = [r for r in member.roles if str(r.id) != role]
        await self.bot.dispatch("member_update", before, member)
        return member

    def admin_texts(self) -> list[str]:
        return self.bot.texts(CH_ADMIN)


async def settle(bridge: Any, rounds: int = 3) -> None:
    """Tunggu antrean onboarding kosong (termasuk pekerjaan yang dijadwalkan pengawas)."""
    for _ in range(rounds):
        await asyncio.sleep(0.03)
        await bridge.onboarder.drain()
