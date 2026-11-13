"""Pembatas pesan masuk sebagai daftar berurutan (`rt.limiters`), bukan urutan if di `Dispatcher._handle`.

Urutan bawaan (TIDAK boleh berubah; dijaga uji simulasi): anggaran token → rate limit → batas sesi per orang
+ antrean global (keduanya satu loop tunggu: orang yang sudah di batas pribadinya dilewati antrean supaya tidak
menahan orang lain, jadi tidak bisa dipecah menjadi dua pemeriksaan berurutan tanpa mengubah keadilan antrean).

Menambah pembatas = satu kelas dengan `name` dan `async check(ctx) -> Verdict | None` + satu baris di
`default_limiters()`. `None` = lolos. Penolakan selalu lewat SATU jalur (`Dispatcher._refuse`): satu tabel pesan
(`REFUSAL_TEXT`), satu `metrics.refuse(verdict.metric)`, dan `reason` hook `pre_gateway_dispatch`.
Pembatas boleh mengirim pemberitahuan sampingan (peringatan 80%, kabar admin) lewat `ctx.say` / `ctx.notify_admin`.

Murni Python; jam & tidur disuntikkan lewat `ctx` supaya bisa diuji tanpa menunggu.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional, Protocol

from .ratelimit import wait_for_slot
from .scale import as_int, effective_max_active

REFUSE_WAIT = 45.0          # tunggu rate limit lebih lama dari ini → ditolak sopan
RESERVE_GRACE = 60.0        # reservasi pesan biasa hidup sampai queue_max_wait + ini (atau gilirannya mulai)

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

# Satu tabel pesan penolakan: verdict.kind → (kunci jeda pemberitahuan, templat). Templat selalu dapat `uid`.
REFUSAL_TEXT: dict[str, tuple[str, str]] = {
    "budget_over": ("budget", MSG_BUDGET_OVER),
    "budget_team_over": ("budget", MSG_BUDGET_TEAM_OVER),
    "rate": ("rate", MSG_RATE),
    "user_limit_full": ("user-limit-full", MSG_USER_LIMIT_FULL),
    "queue_full": ("queue-full", MSG_QUEUE_FULL),
    "queue_cancelled": ("queue-cancelled", MSG_QUEUE_CANCELLED),
}


def _n(value: Any) -> str:
    """Angka token yang enak dibaca: 1.234.567."""
    try:
        return f"{int(value):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(value)


class Verdict:
    """Penolakan dari satu pembatas. `kind` = kunci `REFUSAL_TEXT`; `metric` = jenis untuk
    `metrics.refuse()` (None = tidak dihitung, mis. antrean dibatalkan); `reason` = teks untuk hook;
    `followup` = coroutine opsional yang dijalankan SETELAH pesan ke pengguna (mis. kabar admin)."""
    __slots__ = ("kind", "reason", "metric", "fmt", "followup")

    def __init__(self, kind: str, reason: str, metric: Optional[str] = None, fmt: Optional[dict] = None,
                 followup: Optional[Callable[[], Awaitable[Any]]] = None):
        self.kind, self.reason, self.metric = kind, reason, metric
        self.fmt, self.followup = dict(fmt or {}), followup


class Limiter(Protocol):
    name: str

    async def check(self, ctx: "LimitCtx") -> Optional[Verdict]: ...


class LimitCtx:
    """Semua yang dibutuhkan pembatas untuk satu pesan: orang, admin?, perintah, sumber, setelan roster, jam."""

    def __init__(self, dispatcher: Any, event: Any, gateway: Any, source: Any, uid: str, admin: bool,
                 command: Optional[str], roster: Any):
        self.dispatcher, self.rt = dispatcher, dispatcher.rt
        self.event, self.gateway, self.source = event, gateway, source
        self.uid, self.admin, self.command, self.roster = uid, admin, command, roster
        self.settings = roster.settings
        self.clock, self.sleep = dispatcher.clock, dispatcher.sleep     # dibaca saat pesan tiba (uji boleh menggantinya)
        self.load: "SessionLoad" = dispatcher.load
        self.upstream = dispatcher.upstream

    @property
    def slash(self) -> bool:
        """Perintah slash di luar daftar izin: belum tentu memulai giliran (TTL reservasi pendek)."""
        return bool(self.command)

    async def say(self, kind: str, text: str) -> None:
        await self.dispatcher._say(self.gateway, self.source, kind, text)

    async def notify_admin(self, text: str) -> None:
        await self.dispatcher._notify_admin(self.gateway, text)


# ---------------------------------------------------------------------------------------------------------
class SessionLoad:
    """Beban sesi dari sisi gateway + hook: dipakai antrean dan batas per orang."""

    def __init__(self, rt: Any, upstream: Any, probe: Any):
        self.rt, self.upstream, self.probe = rt, upstream, probe

    def sweep(self, gateway: Any, now: float) -> None:
        """Perbarui reservasi dari sisi gateway (sesi yang sudah berjalan / sudah selesai)."""
        self.rt.turns.sweep(now, self.upstream.session_probe(gateway))

    def user_load(self, uid: str, gateway: Any, now: float) -> int:
        """Sesi orang ini yang berjalan + yang baru diloloskan. Hitungan berjalan berasal dari hook
        (`pre_llm_call` membawa `sender_id`) dan dipercaya begitu SATU giliran terlihat mulai — sesudah
        restart gateway tidak ada sesi lama, jadi hitungan dari nol itu benar tanpa menunggu
        `on_session_end` pertama. Hitungan hook tidak pernah melebihi jumlah sesi gateway (mengoreksi hook
        yang bocor). Sebelum hook terlihat, yang dihitung reservasi: pesan yang diloloskan dan belum mulai,
        ditambah yang sesinya terlihat berjalan di gateway sampai sesi itu selesai."""
        turns = self.rt.turns
        self.sweep(gateway, now)
        running = 0
        if turns.starts > 0:
            running = turns.user_active(uid, now)
            total = self.upstream.running_agent_count(gateway)
            if total is not None:
                running = min(running, total)
        return running + turns.user_reserved(uid, now)

    def reserve_ttl(self, gateway: Any, max_wait: float, slash: bool) -> float:
        """Pesan biasa: reservasi bertahan sampai gilirannya mulai, paling lama queue_max_wait + 60 detik
        (giliran yang lambat mulai tidak boleh memberi slot tambahan). TTL panjang hanya dipakai bila ada
        cara mengakhirinya lebih awal (hook giliran pernah terlihat, atau gateway bisa ditanya); perintah
        slash di luar daftar izin belum tentu memulai giliran, jadi memakai TTL pendek."""
        turns = self.rt.turns
        if slash:
            return turns.reserve_ttl
        if turns.starts <= 0 and self.upstream.session_probe(gateway) is None:
            return turns.reserve_ttl
        return max(turns.reserve_ttl, max(0.0, max_wait) + RESERVE_GRACE)


# ---------------------------------------------------------------------------------------------------------
class BudgetLimiter:
    """Jatah token harian per orang / tim. Peringatan 80% dan kabar admin dikirim dari sini; hanya `over` dan
    `team_over` yang menolak. Buku besar tidak terbaca → lolos (Budget.check tidak pernah melempar)."""
    name = "budget"

    async def check(self, ctx: LimitCtx) -> Optional[Verdict]:
        verdict = ctx.rt.budget.check(ctx.roster, ctx.uid)
        uid = ctx.uid
        if verdict.stale_notice:
            await ctx.notify_admin(verdict.stale_notice)
        if verdict.team_warn:
            await ctx.notify_admin(ADMIN_BUDGET_TEAM_WARN.format(
                pct=int(100 * verdict.team_used / max(1, verdict.team_limit)), used=_n(verdict.team_used),
                limit=_n(verdict.team_limit)))
        if verdict.state == "warn":
            await ctx.say("budget-warn", MSG_BUDGET_WARN.format(
                uid=uid, pct=int(100 * verdict.used / max(1, verdict.limit)), used=_n(verdict.used),
                limit=_n(verdict.limit)))
        elif verdict.state == "over":
            fmt = {"used": _n(verdict.used), "limit": _n(verdict.limit)}
            follow = None
            if verdict.first:
                async def follow() -> None:
                    await ctx.notify_admin(ADMIN_BUDGET_OVER.format(uid=uid, **fmt))
            return Verdict("budget_over", "anggaran token habis", "budget", fmt, follow)
        elif verdict.state == "team_over":
            fmt = {"used": _n(verdict.team_used), "limit": _n(verdict.team_limit)}
            follow = None
            if verdict.first:
                async def follow() -> None:
                    await ctx.notify_admin(ADMIN_BUDGET_TEAM_OVER.format(**fmt))
            return Verdict("budget_team_over", "anggaran token habis", "budget", fmt, follow)
        return None


class RateLimitLimiter:
    """Token bucket per orang. Menunggu sebentar bila hanya perlu menunggu sebentar; menolak bila lebih dari
    REFUSE_WAIT."""
    name = "rate"

    async def check(self, ctx: LimitCtx) -> Optional[Verdict]:
        settings = ctx.settings
        wait, ok = ctx.rt.limiter.reserve(ctx.uid, settings.get("rate_per_minute", 0), settings.get("burst", 1),
                                          ctx.clock(), REFUSE_WAIT)
        if not ok:
            return Verdict("rate", "rate limit", "rate",
                           {"wait": int(wait + 0.999), "rate": settings.get("rate_per_minute")})
        if wait > 0:
            await ctx.sleep(wait)
        return None


class UserCap:
    """Batas sesi per orang (`user_max_concurrent`)."""
    name = "user_concurrency"

    @staticmethod
    def limit(settings: dict) -> int:
        return as_int(settings.get("user_max_concurrent"))

    @staticmethod
    def blocked(ctx: LimitCtx, who: str, limit: int) -> bool:
        return limit > 0 and ctx.load.user_load(who, ctx.gateway, ctx.clock()) >= limit


class GlobalSlots:
    """Slot sesi bersama (`max_active`, disesuaikan pembatas adaptif) dan panggilan LLM paralel
    (`max_llm_parallel`)."""
    name = "global_admission"

    @staticmethod
    def free(ctx: LimitCtx, max_active: int, llm_max: int) -> Optional[bool]:
        """True = ada slot; False = penuh. Beban sesi tidak terukur → True (hanya gerbang lain yang berlaku)."""
        rt, turns = ctx.rt, ctx.rt.turns
        now = ctx.clock()
        ctx.load.sweep(ctx.gateway, now)
        if llm_max > 0 and rt.llm.inflight(now) + turns.reserved(now) >= llm_max:
            return False                  # panggilan LLM paralel (termasuk cron, /goal, subagent) penuh
        if max_active <= 0:
            return True
        load = ctx.dispatcher.probe(ctx.gateway, now)
        if load is None:
            return True
        # Reservasi yang sesinya sudah berjalan di gateway sudah ada di hitungan gateway; hanya saat
        # beban diambil dari hook (yang bisa tertinggal sesaat) reservasi itu tetap ditambahkan.
        reserved = turns.reserved(now, include_seen=turns.trusted())
        return load + reserved < effective_max_active(rt, ctx.settings)


class AdmissionLimiter:
    """Batas sesi per orang + antrean global yang adil. Satu loop tunggu (`wait_for_slot`) memakai dua gerbang:
    `UserCap.blocked` (orang di batas pribadinya dilewati antrean) dan `GlobalSlots.free`."""
    name = "admission"

    def __init__(self, user_cap: Any = None, slots: Any = None):
        self.user_cap, self.slots = user_cap or UserCap(), slots or GlobalSlots()

    async def check(self, ctx: LimitCtx) -> Optional[Verdict]:
        settings, uid, source, gateway = ctx.settings, ctx.uid, ctx.source, ctx.gateway
        try:
            max_active = int(settings.get("max_active") or 0)
            max_wait = float(settings.get("queue_max_wait") or 0)
        except (TypeError, ValueError):
            return None
        user_max, llm_max = self.user_cap.limit(settings), as_int(settings.get("max_llm_parallel"))
        sessions_gated = max_active > 0 and ctx.dispatcher.probe(gateway, ctx.clock()) is not None
        if not sessions_gated and user_max <= 0 and llm_max <= 0:
            return None
        if ctx.upstream.session_running(gateway, source):
            return None
        turns, queue, metrics = ctx.rt.turns, ctx.rt.queue, ctx.rt.metrics
        # Thread = sesi sendiri (auto-thread Discord memberi tiap pesan baru thread_id baru), jadi kunci
        # "pesan susulan" memakai thread bila ada. (Urutan pembuatan thread vs hook ini tidak terkonfirmasi
        # dari sumber; tanpa thread_id perilakunya sama seperti sebelumnya: per channel.)
        chat = str(getattr(source, "thread_id", None) or getattr(source, "chat_id", "") or "")
        ctx.load.sweep(gateway, ctx.clock())
        if turns.holds(uid, chat, ctx.clock()):
            return None                       # pesan beruntun ke sesi yang baru saja diloloskan

        def free() -> Optional[bool]:
            return self.slots.free(ctx, max_active, llm_max)

        def blocked(who: str) -> bool:
            return self.user_cap.blocked(ctx, who, user_max)

        def on_wait(pos: int) -> Any:
            if blocked(uid):
                return ctx.say("user-limit", MSG_USER_LIMIT.format(uid=uid, limit=user_max))
            if queue.behind_own(ticket) > 0:
                return ctx.say("queue-behind", MSG_QUEUE_BEHIND.format(uid=uid))
            return ctx.say("queue", MSG_QUEUE.format(uid=uid, pos=pos))

        ticket = queue.enter(uid, ctx.clock())
        try:
            admitted = await wait_for_slot(queue, ticket, free=free, max_wait=max_wait, clock=ctx.clock,
                                           sleep=ctx.sleep, on_wait=on_wait, blocked=blocked)
            if admitted:
                now = ctx.clock()
                turns.reserve(uid, now, chat, ttl=ctx.load.reserve_ttl(gateway, max_wait, ctx.slash),
                              session_key=ctx.upstream.session_key(gateway, source))
                metrics.observe_wait(now, now - ticket.t0)
                return None
            if ticket.cancelled:
                return Verdict("queue_cancelled", "antrean dibatalkan", None)
            if blocked(uid):
                return Verdict("user_limit_full", "batas sesi per orang", "user_concurrent", {"wait": int(max_wait)})
            return Verdict("queue_full", "antrean penuh", "queue_full", {"wait": int(max_wait)})
        except asyncio.CancelledError:
            # Gateway berhenti saat pesan ini masih menunggu: kabari pemiliknya sebisanya, lalu teruskan
            # pembatalan apa adanya.
            try:
                await asyncio.wait_for(ctx.say("queue-cancelled", MSG_QUEUE_CANCELLED.format(uid=uid)), 2.0)
            except BaseException:
                pass
            raise
        finally:
            queue.leave(ticket)


def default_limiters() -> list:
    """Urutan penegakan. Menambah pembatas = satu baris di sini."""
    return [BudgetLimiter(), RateLimitLimiter(), AdmissionLimiter()]
