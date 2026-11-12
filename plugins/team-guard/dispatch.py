"""Hook `pre_gateway_dispatch`: gerbang akses (ditahan/nonaktif), penyamaran token di chat,
rate limit per orang, dan antrean masuk.

Hook ini didokumentasikan boleh `async` dan TIDAK dibatasi plugins.hook_callback_timeout
(website/docs/user-guide/features/hooks.md → pre_gateway_dispatch), jadi menunda pesan di sini aman.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from .ratelimit import LoadProbe, wait_for_slot
from .redact import scrub_tokens
from .shared import log, log_once

REFUSE_WAIT = 45.0          # tunggu rate limit lebih lama dari ini → ditolak sopan
NOTICE_EVERY = 60.0         # jeda minimal antar pemberitahuan sejenis ke orang yang sama

MSG_HELD = ("⏸️ <@{uid}> onboarding-mu masih menunggu persetujuan admin tim, jadi Hermes belum bisa melayanimu. "
            "Admin sudah diberi tahu.")
MSG_INACTIVE = "<@{uid}> akses Hermes-mu sedang nonaktif. Hubungi admin tim untuk mengaktifkannya lagi."
MSG_RATE = ("⏳ <@{uid}> kamu mengirim pesan terlalu cepat. Coba lagi dalam ±{wait} detik "
            "(batas: {rate} pesan/menit).")
MSG_QUEUE = "⏳ <@{uid}> semua slot Hermes sedang terpakai. Kamu di antrean nomor {pos}; pesanmu diproses otomatis."
MSG_QUEUE_FULL = ("😔 <@{uid}> Hermes masih penuh setelah {wait} detik menunggu, jadi pesanmu tidak diproses. "
                  "Coba kirim lagi sebentar lagi.")
MSG_SCRUB = ("🔒 <@{uid}> pesanmu memuat kredensial ({kinds}). Nilainya disamarkan sebelum sampai ke AI{deleted}. "
             "Simpan secret lewat `/env-set`, lalu sebut namanya saja (mis. `$GH_PAT`). "
             "Anggap token itu sudah bocor: ganti (rotate) sekarang.")


def platform_name(source: Any) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").lower()


def source_channel_ids(source: Any) -> tuple[str, ...]:
    ids = (getattr(source, "chat_id", None), getattr(source, "parent_chat_id", None),
           getattr(source, "thread_id", None))
    return tuple(str(i) for i in ids if i)


class Dispatcher:
    def __init__(self, rt: Any, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep):
        self.rt = rt
        self.clock, self.sleep = clock, sleep
        self.probe = LoadProbe(rt.turns)
        self._told: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------ util
    async def _say(self, gateway: Any, source: Any, kind: str, text: str) -> None:
        """Balasan sampingan lewat jalur terdokumentasi `gateway.adapters[platform].send(...)`."""
        uid = str(getattr(source, "user_id", "") or "")
        now = self.clock()
        if now - self._told.get((uid, kind), -1e9) < NOTICE_EVERY:
            return
        self._told[(uid, kind)] = now
        try:
            adapters = getattr(gateway, "adapters", None) or {}
            adapter = adapters.get(getattr(source, "platform", None))
            if adapter is None:
                adapter = next((a for p, a in adapters.items()
                                if str(getattr(p, "value", p)).lower() == "discord"), None)
            if adapter is None:
                return
            thread_id = getattr(source, "thread_id", None)
            await adapter.send(str(source.chat_id), text, metadata={"thread_id": str(thread_id)} if thread_id else None)
        except Exception as exc:
            log_once("dispatch-say", "pemberitahuan ke channel gagal (%s: %s)", type(exc).__name__, exc)

    @staticmethod
    async def _delete(event: Any) -> bool:
        raw = getattr(event, "raw_message", None)
        delete = getattr(raw, "delete", None)
        if not callable(delete):
            return False
        try:
            await delete()
            return True
        except Exception as exc:
            log_once("dispatch-delete", "pesan berisi secret tidak bisa dihapus (bot butuh Manage Messages): %s", exc)
            return False

    def _session_running(self, gateway: Any, source: Any) -> bool:
        """Pesan susulan ke sesi yang sedang jalan masuk antrean sesi Hermes sendiri → tidak ikut antre."""
        try:
            key = gateway._session_key_for_source(source)
            return bool(gateway._is_session_running(key))
        except Exception:
            return False

    # ------------------------------------------------------------------ hook
    async def handle(self, event: Any = None, gateway: Any = None, **_kwargs: Any) -> Optional[dict]:
        try:
            return await self._handle(event, gateway)
        except Exception as exc:  # hook kebijakan tidak boleh menjatuhkan dispatch
            log.warning("team-guard: pre_gateway_dispatch gagal (%s: %s); pesan diteruskan apa adanya",
                        type(exc).__name__, exc)
            return None

    async def _handle(self, event: Any, gateway: Any) -> Optional[dict]:
        source = getattr(event, "source", None)
        if source is None or getattr(event, "internal", False) or platform_name(source) != "discord":
            return None
        roster = self.rt.roster()
        if not roster:
            return None
        uid = str(getattr(source, "user_id", "") or "")
        if not uid or getattr(source, "is_bot", False):
            return None
        if str(getattr(source, "chat_type", "") or "") == "dm":
            return None                                   # DM ditolak Hermes sendiri (unauthorized_dm_behavior)
        guild = getattr(source, "guild_id", None)
        if guild and roster.guild_id and str(guild) != roster.guild_id:
            return None
        if roster.channel_entry(*source_channel_ids(source)) is None:
            return None                                   # bukan channel tim → dibiarkan ke gerbang channel Hermes
        settings = roster.settings
        admin = roster.is_admin(uid)

        # E. Gerbang akses: ditahan / nonaktif ditolak di semua channel, termasuk #hermes-shared.
        if not admin:
            if self.rt.state.is_held(uid) and roster.user_status(uid) != "active":
                await self._say(gateway, source, "held", MSG_HELD.format(uid=uid))
                return {"action": "skip", "reason": "team-guard: onboarding ditahan"}
            if roster.user_status(uid) == "inactive":
                await self._say(gateway, source, "inactive", MSG_INACTIVE.format(uid=uid))
                return {"action": "skip", "reason": "team-guard: anggota nonaktif"}

        # C. Token yang jelas di chat disamarkan sebelum sampai ke AI; pesan aslinya dihapus.
        text = getattr(event, "text", None)
        rewritten: Optional[str] = None
        if settings.get("scrub_chat") and isinstance(text, str) and text:
            new, kinds = scrub_tokens(text)
            if kinds:
                rewritten = new
                deleted = bool(settings.get("delete_secret_messages")) and await self._delete(event)
                await self._say(gateway, source, "scrub", MSG_SCRUB.format(
                    uid=uid, kinds=", ".join(sorted(set(kinds))),
                    deleted=" dan pesan aslinya dihapus" if deleted else ""))
        result = {"action": "rewrite", "text": rewritten} if rewritten is not None else None

        # D. Skala. Admin dan perintah slash (/stop, /approve, …) tidak pernah ditunda.
        is_command = isinstance(text, str) and text.lstrip().startswith("/")
        if admin or is_command:
            return result
        wait, ok = self.rt.limiter.reserve(uid, settings.get("rate_per_minute", 0), settings.get("burst", 1),
                                           self.clock(), REFUSE_WAIT)
        if not ok:
            await self._say(gateway, source, "rate", MSG_RATE.format(
                uid=uid, wait=int(wait + 0.999), rate=settings.get("rate_per_minute")))
            return {"action": "skip", "reason": "team-guard: rate limit"}
        if wait > 0:
            await self.sleep(wait)
        if not await self._admit(uid, source, gateway, settings):
            return {"action": "skip", "reason": "team-guard: antrean penuh"}
        return result

    async def _admit(self, uid: str, source: Any, gateway: Any, settings: dict) -> bool:
        try:
            max_active = int(settings.get("max_active") or 0)
            max_wait = float(settings.get("queue_max_wait") or 0)
        except (TypeError, ValueError):
            return True
        if max_active <= 0 or self.probe(gateway, self.clock()) is None or self._session_running(gateway, source):
            return True
        turns, queue = self.rt.turns, self.rt.queue
        chat = str(getattr(source, "chat_id", "") or "")
        if turns.holds(uid, chat, self.clock()):
            return True                       # pesan beruntun ke sesi yang baru saja diloloskan

        def free() -> Optional[bool]:
            now = self.clock()
            load = self.probe(gateway, now)
            return None if load is None else load + turns.reserved(now) < max_active

        ticket = queue.enter(uid, self.clock())
        try:
            admitted = await wait_for_slot(
                queue, ticket, free=free, max_wait=max_wait, clock=self.clock, sleep=self.sleep,
                on_wait=lambda pos: self._say(gateway, source, "queue", MSG_QUEUE.format(uid=uid, pos=pos)))
            if admitted:
                turns.reserve(uid, self.clock(), chat)
            else:
                await self._say(gateway, source, "queue-full", MSG_QUEUE_FULL.format(uid=uid, wait=int(max_wait)))
            return admitted
        finally:
            queue.leave(ticket)
