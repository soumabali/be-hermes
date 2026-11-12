"""Live reload: terapkan rute & daftar channel dari roster.json ke gateway yang sedang jalan,
verifikasi dengan resolver Hermes sendiri, dan jatuh ke restart halus / manual bila gagal.

Yang disentuh di Hermes (semuanya dibungkus getattr/try; perubahan upstream → jalur fallback):
  gateway/config.py       GatewayConfig.profile_routes   (dibaca per pesan oleh
                          gateway/session_ingress_context.py dan GatewayRunner._profile_name_for_source)
  gateway/profile_routing.py  parse_profile_routes, match_profile_route
  plugins/platforms/discord/adapter.py  DiscordAdapter._gate_env_snapshot, .config.extra,
                          ._get_allowed_channels(), ._discord_free_response_channels()
  gateway/control_socket.py   rescan_gateway_profiles     (profile baru dilayani tanpa restart)
  gateway/run.py              _multiplex_profile_homes, GatewayRunner._running_agent_count
  gateway/run_shutdown.py     GatewayRunner.request_restart (keluar dengan kode 75 → supervisor)
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable, Iterable, Optional

from .shared import log, log_once

ENV_ALLOWED = "DISCORD_ALLOWED_CHANNELS"
ENV_FREE = "DISCORD_FREE_RESPONSE_CHANNELS"
BOOT_GRACE = 180.0            # gagal verifikasi secepat ini setelah start → restart tidak akan menolong
RESTART_COOLDOWN = 900.0      # jarak minimal antar restart otomatis
IDLE_MAX_WAIT = 600.0
IDLE_POLL = 15.0
SERVE_WAIT = 40.0


def csv(ids: Iterable[Any]) -> str:
    return ",".join(str(i) for i in ids)


def expected_routes(roster: Any) -> list[tuple[str, str, Optional[str]]]:
    """(channel, profile yang seharusnya, user contoh) untuk tiap channel terdaftar."""
    admin = sorted(roster.admins)[0] if roster.admins else None
    someone = next(iter(sorted(roster.users)), None) or admin
    out = []
    for cid, entry in sorted(roster.channels.items()):
        kind, profile = entry.get("kind"), str(entry.get("profile") or "")
        if not profile:
            continue
        user = entry.get("owner") if kind == "personal" else (admin if kind == "admin" else someone)
        out.append((cid, profile, str(user) if user else None))
    return out


class Upstream:
    """Akses ke internal Hermes, diimpor malas. Uji memakai tiruan dengan antarmuka yang sama."""

    def parse_profile_routes(self, raw: list) -> list:
        from gateway.profile_routing import parse_profile_routes  # type: ignore[import-not-found]
        return parse_profile_routes(raw)

    def match_profile_route(self, routes: list, **kwargs: Any) -> Any:
        from gateway.profile_routing import match_profile_route  # type: ignore[import-not-found]
        return match_profile_route(routes, "discord", **kwargs)

    def rescan(self, home: Any) -> Optional[dict]:
        """Blocking (soket kontrol) — panggil dari thread, bukan dari event loop."""
        from pathlib import Path
        from gateway.control_socket import rescan_gateway_profiles  # type: ignore[import-not-found]
        return rescan_gateway_profiles(Path(str(home)))

    def served_profiles(self, config: Any) -> Optional[set]:
        from gateway.run import _multiplex_profile_homes  # type: ignore[import-not-found]
        return {name for name, _home in _multiplex_profile_homes(config)}

    def restart(self, runner: Any) -> None:
        from gateway.restart import (  # type: ignore[import-not-found]
            is_container_restart_context, is_gateway_supervisor_process)
        via_service = bool(is_gateway_supervisor_process() or is_container_restart_context())
        runner.request_restart(detached=not via_service, via_service=via_service)


class LiveReloader:
    def __init__(self, rt: Any, *, get_runner: Callable[[], Any], get_adapter: Callable[[], Any],
                 notify: Callable[[str], Awaitable[Any]], upstream: Any = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Any] = asyncio.sleep,
                 spawn: Optional[Callable[[Any], Any]] = None):
        self.rt = rt
        self.get_runner, self.get_adapter, self.notify = get_runner, get_adapter, notify
        self.upstream = upstream or Upstream()
        self.clock, self.sleep = clock, sleep
        self.spawn = spawn or (lambda coro: asyncio.get_running_loop().create_task(coro))
        self._lock: Optional[asyncio.Lock] = None
        self._restart_task: Any = None
        self.manual_only = False          # setelah gagal tepat sesudah start: tidak ada restart otomatis lagi
        self._last_failure = ""

    # ------------------------------------------------------------------ terapkan
    def apply_routes(self, runner: Any, roster: Any) -> tuple[bool, str]:
        config = getattr(runner, "config", None)
        if config is None or not hasattr(config, "profile_routes"):
            return False, "GatewayRunner.config.profile_routes tidak ditemukan"
        try:
            parsed = list(self.upstream.parse_profile_routes(roster.profile_routes))
        except Exception as exc:
            return False, f"parse_profile_routes gagal ({type(exc).__name__}: {exc})"
        if len(parsed) != len(roster.profile_routes):
            return False, f"{len(roster.profile_routes) - len(parsed)} rute ditolak parse_profile_routes"
        try:
            current = config.profile_routes
            if isinstance(current, list):
                current[:] = parsed       # di tempat: pemegang referensi lama ikut melihat rute baru
            else:
                config.profile_routes = parsed
        except Exception as exc:
            return False, f"profile_routes tidak bisa ditulis ({type(exc).__name__}: {exc})"
        return True, f"{len(parsed)} rute"

    def apply_gates(self, adapter: Any, roster: Any) -> tuple[bool, str]:
        """Gerbang channel bisa hidup di dua tempat (DiscordAdapter._gate_raw): snapshot env per-adapter
        (menang untuk allowed_channels) dan PlatformConfig.extra (menang untuk free_response_channels).
        Keduanya ditulis; os.environ hanya diperbarui bila key-nya memang sudah ada di sana."""
        allowed, free = csv(roster.allowed_channels), csv(roster.free_response_channels)
        touched = []
        snapshot = getattr(adapter, "_gate_env_snapshot", None)
        if isinstance(snapshot, dict):
            snapshot[ENV_ALLOWED], snapshot[ENV_FREE] = allowed, free
            touched.append("snapshot")
        extra = getattr(getattr(adapter, "config", None), "extra", None)
        if isinstance(extra, dict):
            extra["allowed_channels"], extra["free_response_channels"] = allowed, free
            touched.append("extra")
        for key, value in ((ENV_ALLOWED, allowed), (ENV_FREE, free)):
            if key in os.environ:
                os.environ[key] = value
        if not touched:
            return False, "adapter Discord tidak punya _gate_env_snapshot maupun config.extra"
        return True, "+".join(touched)

    # ------------------------------------------------------------------ verifikasi
    def verify(self, runner: Any, adapter: Any, roster: Any) -> tuple[bool, str]:
        try:
            routes = runner.config.profile_routes
        except Exception:
            return False, "GatewayRunner.config.profile_routes tidak ditemukan"
        for cid, profile, user in expected_routes(roster):
            try:
                match = self.upstream.match_profile_route(routes, guild_id=roster.guild_id, chat_id=cid, user_id=user)
            except Exception as exc:
                return False, f"match_profile_route gagal ({type(exc).__name__}: {exc})"
            got = getattr(match, "profile", None)
            if got != profile:
                return False, f"channel {cid} dirutekan ke {got or 'lobby'}, seharusnya {profile}"
        for uid, info in sorted(roster.inactive_users.items()):
            cid = str(info.get("channel_id") or "")
            if not cid or cid in roster.channels:
                continue
            try:
                match = self.upstream.match_profile_route(routes, guild_id=roster.guild_id, chat_id=cid, user_id=uid)
            except Exception as exc:
                return False, f"match_profile_route gagal ({type(exc).__name__}: {exc})"
            if match is not None:
                return False, f"channel anggota nonaktif {cid} masih dirutekan ke {getattr(match, 'profile', '?')}"
        for label, getter, want in (("allowed_channels", "_get_allowed_channels", roster.allowed_channels),
                                    ("free_response_channels", "_discord_free_response_channels",
                                     roster.free_response_channels)):
            fn = getattr(adapter, getter, None)
            if not callable(fn):
                return False, f"DiscordAdapter.{getter} tidak ditemukan"
            try:
                got_set = {str(x) for x in fn()}
            except Exception as exc:
                return False, f"DiscordAdapter.{getter} gagal ({type(exc).__name__}: {exc})"
            if got_set != {str(x) for x in want}:
                missing = sorted({str(x) for x in want} - got_set)
                return False, (f"{label} adapter belum sesuai" + (f" (kurang {', '.join(missing[:3])})" if missing
                                                                    else " (masih memuat channel lama)"))
        return True, f"{len(roster.profile_routes)} rute, {len(roster.allowed_channels)} channel"

    async def _ensure_served(self, runner: Any, roster: Any, wait: bool = True) -> tuple[bool, str]:
        """Profile yang dirutekan harus termasuk yang dilayani multiplexer. Profile baru biasanya sudah
        dilayani (rescan 30 dtk); soket kontrol `rescan-profiles` mempercepatnya."""
        wanted = roster.profiles() - {"default"}
        deadline = self.clock() + (SERVE_WAIT if wait else 0.0)
        while True:
            served: Optional[set] = None
            try:
                answer = await asyncio.to_thread(self.upstream.rescan, roster.hermes_root)
                if isinstance(answer, dict) and isinstance(answer.get("served_profiles"), (list, set, tuple)):
                    served = {str(p) for p in answer["served_profiles"]}
            except Exception as exc:
                log_once("rescan", "soket kontrol rescan-profiles tidak bisa dipakai (%s); mengandalkan "
                         "rescan berkala Hermes", exc, level=20)
            if served is None:
                try:
                    served = self.upstream.served_profiles(runner.config)
                except Exception:
                    served = None
            if served is None:
                return True, "daftar profile terlayani tidak bisa diperiksa"
            missing = sorted(wanted - {str(p) for p in served})
            if not missing:
                return True, ""
            if self.clock() >= deadline:
                return False, f"profile belum dilayani gateway: {', '.join(missing[:3])}"
            await self.sleep(5.0)

    # ------------------------------------------------------------------ alur utama
    async def apply(self, reason: str = "", *, startup: bool = False, wait_served: bool = True) -> tuple[bool, str]:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            roster = self.rt.roster(force=True)
            if not roster:
                return False, "roster.json belum ada"
            if not roster.live_reload:
                self.rt.state.set_live(False, "live reload dimatikan (team.yaml → live_reload: false)")
                return False, "live reload dimatikan"
            runner, adapter = self.get_runner(), self.get_adapter()
            if runner is None or adapter is None:
                ok, detail = False, "gateway/adapter Discord belum tersambung"
            else:
                ok, detail = self.apply_routes(runner, roster)
                if ok:
                    ok, detail = self.apply_gates(adapter, roster)
                if ok:
                    ok, detail = await self._ensure_served(runner, roster, wait_served)
                if ok:
                    ok, detail = self.verify(runner, adapter, roster)
            if ok:
                self._last_failure = ""
                self.rt.state.set_live(True, detail, reason=reason or "sinkron")
                log.info("team-guard: live reload OK (%s) — %s", reason or "sinkron", detail)
                return True, detail
            self.rt.state.set_live(False, detail, reason=reason or "sinkron")
            await self._fallback(detail, roster, runner)
            return False, detail

    async def _tell(self, key: str, text: str) -> None:
        if self._last_failure == key:
            return                         # satu kabar per jenis kegagalan, bukan tiap kali watcher mencoba
        self._last_failure = key
        try:
            await self.notify(text)
        except Exception as exc:
            log.warning("team-guard: kabar admin gagal dikirim (%s)", exc)

    async def _fallback(self, detail: str, roster: Any, runner: Any) -> None:
        log.error("team-guard: live reload GAGAL — %s (fallback: %s)", detail, roster.restart_fallback)
        since_boot = self.clock() - self.rt.started
        last_restart = self.rt.state.seconds_since_auto_restart()
        if since_boot < BOOT_GRACE or (last_restart is not None and last_restart < RESTART_COOLDOWN):
            self.manual_only = True
        if self.manual_only:
            await self._tell("boot:" + detail, (
                f"⚠️ Live reload rute gagal tepat setelah gateway start: {detail}. Restart tidak akan menolong, "
                "jadi kit TIDAK me-restart lagi. Kemungkinan update Hermes mengubah internal gateway — jalankan "
                "`bash tests/selftest.sh`, periksa `hermes logs`, lalu ketik /restart setelah diperbaiki."))
            return
        if roster.restart_fallback != "idle" or runner is None:
            await self._tell("manual:" + detail, f"⚠️ Live reload rute gagal: {detail}. Ketik /restart supaya "
                             "rute & channel baru berlaku.")
            return
        if self._restart_task is not None and not self._restart_task.done():
            return
        await self._tell("idle:" + detail, f"♻️ Live reload rute gagal: {detail}. Gateway akan restart halus "
                         "begitu tidak ada sesi aktif (maks ±10 menit); sesi berjalan tidak diputus.")
        self._restart_task = self.spawn(self._restart_when_idle(runner))

    async def _restart_when_idle(self, runner: Any) -> None:
        deadline = self.clock() + IDLE_MAX_WAIT
        count = getattr(runner, "_running_agent_count", None)
        if not callable(count) or not callable(getattr(runner, "request_restart", None)):
            log_once("restart-api", "GatewayRunner._running_agent_count/request_restart tidak ada; restart manual")
            await self.notify("⚠️ Restart otomatis tidak tersedia di versi Hermes ini. Ketik /restart supaya rute "
                              "baru berlaku.")
            return
        while self.clock() < deadline:
            try:
                if int(count()) <= 0:
                    break
            except Exception:
                break
            await self.sleep(IDLE_POLL)
        # Verifikasi ulang: mungkin watcher sudah berhasil sementara kita menunggu.
        roster = self.rt.roster()
        if self.verify(runner, self.get_adapter(), roster)[0]:
            self.rt.state.set_live(True, "pulih sebelum restart", reason="fallback")
            return
        self.rt.state.mark_auto_restart()
        try:
            self.upstream.restart(runner)
            log.warning("team-guard: meminta restart halus gateway (live reload gagal)")
        except Exception as exc:
            log_once("restart-call", "request_restart gagal (%s: %s)", type(exc).__name__, exc)
            await self.notify(f"⚠️ Restart otomatis gagal ({type(exc).__name__}). Ketik /restart supaya rute baru "
                              "berlaku.")
