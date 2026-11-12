"""Hook `pre_gateway_dispatch`: gerbang akses (ditahan/nonaktif), penyamaran token di chat,
anggaran token, rate limit per orang, batas sesi per orang, dan antrean masuk yang adil.

Hook ini didokumentasikan boleh `async` dan TIDAK dibatasi plugins.hook_callback_timeout
(website/docs/user-guide/features/hooks.md → pre_gateway_dispatch), jadi menunda pesan di sini aman.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from .ratelimit import LoadProbe, wait_for_slot
from .redact import scrub_tokens
from .scale import as_int, effective_max_active
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
MSG_QUEUE_BEHIND = ("⏳ <@{uid}> pesanmu yang sebelumnya masih di antrean; pesan ini menyusul setelahnya dan "
                    "diproses otomatis.")
MSG_QUEUE_CANCELLED = ("♻️ <@{uid}> Hermes sedang dimuat ulang, jadi pesanmu yang menunggu di antrean tidak "
                       "diproses. Kirim ulang sebentar lagi.")
MSG_USER_LIMIT = ("⏳ <@{uid}> kamu sudah punya {limit} sesi Hermes yang berjalan (batas per orang). Pesan ini "
                  "diproses otomatis setelah salah satunya selesai.")
MSG_USER_LIMIT_FULL = ("😔 <@{uid}> sesimu yang lain masih berjalan setelah {wait} detik menunggu, jadi pesan ini "
                       "tidak diproses. Tunggu sampai selesai (atau `/stop` di thread-nya), lalu kirim lagi.")
MSG_BUDGET_WARN = ("📊 <@{uid}> pemakaian token-mu hari ini sudah {pct}% dari jatah harian "
                   "({used} dari {limit} token). Setelah 100% pesan baru ditolak sampai tengah malam.")
MSG_BUDGET_OVER = ("🚫 <@{uid}> jatah token harianmu sudah habis ({used} dari {limit} token), jadi pesan ini "
                   "tidak diproses. Jatah kembali penuh setelah tengah malam; admin tim bisa menambah kuota.")
MSG_BUDGET_TEAM_OVER = ("🚫 <@{uid}> jatah token harian TIM sudah habis ({used} dari {limit} token), jadi pesan ini "
                        "tidak diproses. Jatah kembali penuh setelah tengah malam.")
ADMIN_BUDGET_OVER = ("📊 Jatah token harian <@{uid}> habis ({used} dari {limit} token). Pesan barunya ditolak "
                     "sampai tengah malam. Tambah kuota lewat teamctl bila perlu.")
ADMIN_BUDGET_TEAM_WARN = "📊 Pemakaian token tim hari ini sudah {pct}% dari anggaran ({used} dari {limit} token)."
ADMIN_BUDGET_TEAM_OVER = ("📊 Anggaran token harian tim habis ({used} dari {limit} token). Pesan anggota ditolak "
                          "sampai tengah malam.")
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
        if not await self._within_budget(uid, source, gateway, roster):
            self.rt.metrics.refuse("budget")
            return {"action": "skip", "reason": "team-guard: anggaran token habis"}
        wait, ok = self.rt.limiter.reserve(uid, settings.get("rate_per_minute", 0), settings.get("burst", 1),
                                           self.clock(), REFUSE_WAIT)
        if not ok:
            self.rt.metrics.refuse("rate")
            await self._say(gateway, source, "rate", MSG_RATE.format(
                uid=uid, wait=int(wait + 0.999), rate=settings.get("rate_per_minute")))
            return {"action": "skip", "reason": "team-guard: rate limit"}
        if wait > 0:
            await self.sleep(wait)
        refused = await self._admit(uid, source, gateway, settings)
        if refused:
            return {"action": "skip", "reason": f"team-guard: {refused}"}
        return result

    # ------------------------------------------------------------------ anggaran token
    async def _notify_admin(self, gateway: Any, text: str) -> None:
        """Kabar ke #hermes-admin: lewat jembatan host bila ada, kalau tidak lewat adapter gateway."""
        try:
            host = self.rt.host
            if host is not None:
                await host.notify_admin(text)
                return
            roster = self.rt.roster()
            if not roster.admin_channel or not roster.onboarding.get("admin_notices", True):
                return
            adapters = getattr(gateway, "adapters", None) or {}
            adapter = next((a for p, a in adapters.items()
                            if str(getattr(p, "value", p)).lower() == "discord"), None)
            if adapter is not None:
                await adapter.send(str(roster.admin_channel), text)
        except Exception as exc:
            log_once("dispatch-admin", "kabar ke channel admin gagal (%s: %s)", type(exc).__name__, exc)

    async def _within_budget(self, uid: str, source: Any, gateway: Any, roster: Any) -> bool:
        """False → pesan ditolak (jatah orang ini atau jatah tim habis). Buku besar tidak terbaca → True."""
        verdict = self.rt.budget.check(roster, uid)
        if verdict.team_warn:
            await self._notify_admin(gateway, ADMIN_BUDGET_TEAM_WARN.format(
                pct=int(100 * verdict.team_used / max(1, verdict.team_limit)), used=_n(verdict.team_used),
                limit=_n(verdict.team_limit)))
        if verdict.state == "warn":
            await self._say(gateway, source, "budget-warn", MSG_BUDGET_WARN.format(
                uid=uid, pct=int(100 * verdict.used / max(1, verdict.limit)), used=_n(verdict.used),
                limit=_n(verdict.limit)))
        elif verdict.state == "over":
            await self._say(gateway, source, "budget", MSG_BUDGET_OVER.format(
                uid=uid, used=_n(verdict.used), limit=_n(verdict.limit)))
            if verdict.first:
                await self._notify_admin(gateway, ADMIN_BUDGET_OVER.format(
                    uid=uid, used=_n(verdict.used), limit=_n(verdict.limit)))
        elif verdict.state == "team_over":
            await self._say(gateway, source, "budget", MSG_BUDGET_TEAM_OVER.format(
                uid=uid, used=_n(verdict.team_used), limit=_n(verdict.team_limit)))
            if verdict.first:
                await self._notify_admin(gateway, ADMIN_BUDGET_TEAM_OVER.format(
                    used=_n(verdict.team_used), limit=_n(verdict.team_limit)))
        return not verdict.refused

    # ------------------------------------------------------------------ antrean
    def _runner_count(self, gateway: Any) -> Optional[int]:
        fn = getattr(gateway, "_running_agent_count", None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:
                return None
        return None

    def user_load(self, uid: str, gateway: Any, now: float) -> int:
        """Sesi orang ini yang berjalan + yang baru diloloskan. Hitungan berjalan berasal dari hook
        (`pre_llm_call` membawa `sender_id`), jadi hanya dipakai setelah hook terbukti hidup, dan tidak
        pernah melebihi jumlah sesi gateway (mengoreksi hook yang bocor)."""
        turns = self.rt.turns
        running = 0
        if turns.trusted():
            running = turns.user_active(uid, now)
            total = self._runner_count(gateway)
            if total is not None:
                running = min(running, total)
        elif turns.starts > 0:
            log_once("user-limit-unproven", "batas sesi per orang baru aktif penuh setelah hook on_session_end "
                     "terlihat sekali; sampai itu hanya pesan yang baru diloloskan yang dihitung",
                     level=20)
        return running + turns.user_reserved(uid, now)

    async def _admit(self, uid: str, source: Any, gateway: Any, settings: dict) -> str:
        """"" = lolos; selain itu alasan penolakan (untuk `reason` hook)."""
        try:
            max_active = int(settings.get("max_active") or 0)
            max_wait = float(settings.get("queue_max_wait") or 0)
        except (TypeError, ValueError):
            return ""
        user_max, llm_max = as_int(settings.get("user_max_concurrent")), as_int(settings.get("max_llm_parallel"))
        sessions_gated = max_active > 0 and self.probe(gateway, self.clock()) is not None
        if not sessions_gated and user_max <= 0 and llm_max <= 0:
            return ""
        if self._session_running(gateway, source):
            return ""
        turns, queue, metrics = self.rt.turns, self.rt.queue, self.rt.metrics
        # Thread = sesi sendiri (auto-thread Discord memberi tiap pesan baru thread_id baru), jadi kunci
        # "pesan susulan" memakai thread bila ada. (Urutan pembuatan thread vs hook ini tidak terkonfirmasi
        # dari sumber; tanpa thread_id perilakunya sama seperti sebelumnya: per channel.)
        chat = str(getattr(source, "thread_id", None) or getattr(source, "chat_id", "") or "")
        if turns.holds(uid, chat, self.clock()):
            return ""                         # pesan beruntun ke sesi yang baru saja diloloskan

        def free() -> Optional[bool]:
            now = self.clock()
            reserved = turns.reserved(now)
            if llm_max > 0 and self.rt.llm.inflight(now) + reserved >= llm_max:
                return False                  # panggilan LLM paralel (termasuk cron, /goal, subagent) penuh
            if max_active <= 0:
                return True
            load = self.probe(gateway, now)
            if load is None:
                return True                   # beban sesi tidak terukur → hanya gerbang lain yang berlaku
            return load + reserved < effective_max_active(self.rt, settings)

        def blocked(who: str) -> bool:
            return user_max > 0 and self.user_load(who, gateway, self.clock()) >= user_max

        def on_wait(pos: int) -> Any:
            if blocked(uid):
                return self._say(gateway, source, "user-limit", MSG_USER_LIMIT.format(uid=uid, limit=user_max))
            if queue.behind_own(ticket) > 0:
                return self._say(gateway, source, "queue-behind", MSG_QUEUE_BEHIND.format(uid=uid))
            return self._say(gateway, source, "queue", MSG_QUEUE.format(uid=uid, pos=pos))

        ticket = queue.enter(uid, self.clock())
        try:
            admitted = await wait_for_slot(queue, ticket, free=free, max_wait=max_wait, clock=self.clock,
                                           sleep=self.sleep, on_wait=on_wait, blocked=blocked)
            if admitted:
                now = self.clock()
                turns.reserve(uid, now, chat)
                metrics.observe_wait(now, now - ticket.t0)
                return ""
            if ticket.cancelled:
                await self._say(gateway, source, "queue-cancelled", MSG_QUEUE_CANCELLED.format(uid=uid))
                return "antrean dibatalkan"
            if blocked(uid):
                metrics.refuse("user_concurrent")
                await self._say(gateway, source, "user-limit-full",
                                MSG_USER_LIMIT_FULL.format(uid=uid, wait=int(max_wait)))
                return "batas sesi per orang"
            metrics.refuse("queue_full")
            await self._say(gateway, source, "queue-full", MSG_QUEUE_FULL.format(uid=uid, wait=int(max_wait)))
            return "antrean penuh"
        except asyncio.CancelledError:
            # Gateway berhenti saat pesan ini masih menunggu: kabari pemiliknya sebisanya, lalu teruskan
            # pembatalan apa adanya.
            try:
                await asyncio.wait_for(self._say(gateway, source, "queue-cancelled",
                                                 MSG_QUEUE_CANCELLED.format(uid=uid)), 2.0)
            except BaseException:
                pass
            raise
        finally:
            queue.leave(ticket)

    def cancel_waiters(self) -> int:
        """Plugin di-unload: semua penunggu berhenti dan mengabari pemiliknya (lihat `_admit`)."""
        return len(self.rt.queue.cancel_all())


def _n(value: Any) -> str:
    """Angka token yang enak dibaca: 1.234.567."""
    try:
        return f"{int(value):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(value)
