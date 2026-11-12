"""Sisi host: listener Discord (join / role berubah / keluar), port ke discord.py, dan tugas latar.

Dipasang lewat `ctx.register_platform_handler("discord", factory)`. Hermes memanggil
`factory(native, adapter)` dari `BasePlatformAdapter._wire_plugin_handlers` (gateway/platforms/base.py)
SETELAH bot siap, dengan `native` = `discord.ext.commands.Bot` milik adapter dan `adapter` = DiscordAdapter
(punya `.gateway_runner`). Hanya profile pemilik bot yang punya adapter, jadi factory ini hanya berjalan di
host; penjaga `rt.host` memastikan tepat satu jembatan per client walau plugin dimuat di banyak profile.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from . import commands as env_commands
from .livereload import LiveReloader
from .onboarding import Onboarder, run_kit
from .scale import Monitor
from .shared import log, log_once


class DiscordPort:
    """Semua pemanggilan discord.py yang dibutuhkan onboarding, di satu tempat."""

    def __init__(self, native: Any, rt: Any):
        self.native, self.rt = native, rt

    def guild(self) -> Any:
        gid = self.rt.roster().guild_id
        try:
            return self.native.get_guild(int(gid)) if gid else None
        except Exception:
            return None

    def bot_id(self) -> Optional[str]:
        uid = getattr(getattr(self.native, "user", None), "id", None)
        return str(uid) if uid else None

    @staticmethod
    def audit_action() -> Any:
        try:
            import discord
            return discord.AuditLogAction.member_role_update
        except Exception:
            return None

    @staticmethod
    def allowed_mentions(user_id: Any = None) -> Any:
        """Hanya user target yang boleh ter-ping; @everyone/@here/role tidak pernah."""
        import discord
        users: Any = [discord.Object(id=int(user_id))] if user_id else False
        return discord.AllowedMentions(everyone=False, roles=False, replied_user=False, users=users)

    @staticmethod
    def _object(snowflake: Any) -> Any:
        import discord
        return discord.Object(id=int(snowflake))

    async def channel(self, channel_id: Any) -> Any:
        cid = int(channel_id)
        channel = self.native.get_channel(cid)
        if channel is None:
            channel = await self.native.fetch_channel(cid)
        return channel

    async def send(self, channel_id: Any, text: str, mention_user: Any = None) -> Any:
        channel = await self.channel(channel_id)
        return await channel.send(text[:2000], allowed_mentions=self.allowed_mentions(mention_user))

    @staticmethod
    async def delete(message: Any) -> None:
        await message.delete()

    async def delete_by_id(self, channel_id: Any, message_id: Any) -> None:
        if not channel_id or not message_id:
            return
        channel = await self.channel(channel_id)
        await channel.get_partial_message(int(message_id)).delete()

    async def add_role(self, member: Any, role_id: Any, reason: str) -> None:
        await member.add_roles(self._object(role_id), reason=reason)

    async def remove_role(self, member: Any, role_id: Any, reason: str) -> None:
        await member.remove_roles(self._object(role_id), reason=reason)

    async def get_member(self, user_id: Any) -> Any:
        guild = self.guild()
        if guild is None:
            return None
        member = guild.get_member(int(user_id))
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(user_id))
        except Exception:
            return None

    async def members(self, guild: Any) -> list:
        """Butuh Server Members Intent (aktif karena DISCORD_ALLOWED_ROLES diset kit)."""
        try:
            if not getattr(guild, "chunked", True):
                await guild.chunk()
        except Exception as exc:
            log_once("chunk", "daftar anggota server tidak bisa dimuat penuh (%s)", exc)
        return list(getattr(guild, "members", None) or [])


class HostBridge:
    def __init__(self, rt: Any, native: Any, adapter: Any, ctx: Any = None, *, upstream: Any = None,
                 kit: Any = run_kit):
        self.rt, self.native, self.adapter, self.ctx = rt, native, adapter, ctx
        self.port = DiscordPort(native, rt)
        self.tasks: list[asyncio.Task] = []
        self.live = LiveReloader(rt, get_runner=lambda: getattr(self.adapter, "gateway_runner", None),
                                 get_adapter=lambda: self.adapter, notify=self.notify_admin, upstream=upstream,
                                 spawn=self.spawn)
        self.onboarder = Onboarder(rt, self.port, self.live, kit=kit)
        self.monitor = Monitor(rt, get_gateway=lambda: getattr(self.adapter, "gateway_runner", None),
                               notify=self.notify_admin, probe=getattr(rt.dispatcher, "probe", None))
        self.command_guild: Any = None
        self.ready = False
        self.loop: Any = None

    # ------------------------------------------------------------------ tugas
    def spawn(self, coro: Any, name: str = "team-guard:task") -> Any:
        task = None
        spawner = getattr(self.ctx, "spawn_task", None)
        if callable(spawner):
            try:
                task = spawner(coro, name=name)      # diawasi Hermes: dibatalkan saat plugin di-unload
            except Exception:
                task = None
        if task is None:
            task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task

    async def notify_admin(self, text: str) -> None:
        await self.onboarder.notify(text)

    # ------------------------------------------------------------------ pasang
    def install(self) -> None:
        native = self.native
        self.loop = asyncio.get_running_loop()
        for name in ("on_member_join", "on_member_update", "on_member_remove"):
            try:
                native.add_listener(getattr(self, name), name)
            except Exception as exc:
                log_once(f"listener-{name}", "listener %s tidak bisa dipasang (%s: %s)", name,
                         type(exc).__name__, exc)
        if getattr(getattr(native, "intents", None), "members", True) is False:
            log_once("members-intent", "Server Members Intent mati: event join/role tidak akan diterima. Pastikan "
                     "DISCORD_ALLOWED_ROLES terisi dan intent diaktifkan di Developer Portal")
        try:
            self.command_guild = env_commands.install(native, self.rt)
        except Exception as exc:
            log_once("env-install", "/env-set tidak bisa dipasang (%s: %s)", type(exc).__name__, exc)
        self.spawn(self.startup(), "team-guard:startup")
        self.spawn(self.monitor.run(), "team-guard:metrics")

    async def on_member_join(self, member: Any) -> None:
        await self._guarded("member_join", self.onboarder.on_member_join(member))

    async def on_member_update(self, before: Any, after: Any) -> None:
        await self._guarded("member_update", self.onboarder.on_member_update(before, after))

    async def on_member_remove(self, member: Any) -> None:
        await self._guarded("member_remove", self.onboarder.on_member_remove(member))

    @staticmethod
    async def _guarded(name: str, coro: Any) -> None:
        try:
            await coro
        except Exception as exc:
            log.warning("team-guard: listener %s gagal (%s: %s)", name, type(exc).__name__, exc)

    async def startup(self) -> None:
        """Sinkron command, live reload awal, sambutan tertunda, susulan, lalu pengawas roster."""
        waiter = getattr(self.native, "wait_until_ready", None)
        if callable(waiter):
            try:
                await waiter()
            except Exception:
                pass
        self.ready = True
        if self.command_guild is not None:
            try:
                await self.native.tree.sync(guild=self.command_guild)
            except Exception as exc:
                log_once("env-sync", "sinkron /env-set ke server gagal (%s: %s) — bot butuh scope "
                         "applications.commands", type(exc).__name__, exc)
        for label, step in (("sinkron awal", lambda: self.onboarder.sync_once(startup=True)),
                            ("susulan awal", self.onboarder.reconcile)):
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("team-guard: %s gagal (%s: %s)", label, type(exc).__name__, exc)
        await self.onboarder.watch()

    def close(self) -> None:
        for name in ("on_member_join", "on_member_update", "on_member_remove"):
            try:
                self.native.remove_listener(getattr(self, name), name)
            except Exception:
                pass
        self.onboarder.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        self.tasks.clear()


def make_factory(rt: Any, ctx: Any = None, **bridge_kwargs: Any):
    """Factory untuk register_platform_handler: tepat satu HostBridge per client Discord per proses."""

    def team_guard_discord(native: Any, adapter: Any = None) -> None:
        if native is None:
            log_once("native-none", "adapter Discord tidak memberi client native; onboarding otomatis mati")
            return
        current = rt.host
        if current is not None and current.native is native:
            return                              # sudah terpasang (plugin dimuat di banyak profile / rewire)
        if current is not None:
            current.close()                     # client dibangun ulang (reconnect) → pasang lagi
        bridge = HostBridge(rt, native, adapter, ctx, **bridge_kwargs)
        rt.host = bridge
        bridge.install()
        log.info("team-guard: listener onboarding terpasang di bot Discord (host)")

    return team_guard_discord
