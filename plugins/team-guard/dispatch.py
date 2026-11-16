"""Hook `pre_gateway_dispatch`: gerbang akses (ditahan/nonaktif/belum terdaftar), penyamaran token di chat,
anggaran token, rate limit per orang, batas sesi per orang, dan antrean masuk yang adil.

Tiap tahap berdiri sendiri (try sendiri), dan arah gagalnya SENGAJA berbeda — lihat `Dispatcher._handle`:
  gerbang akses, penyamaran token → gagal-TERTUTUP (galat di tahap itu = pesan tidak diteruskan);
  tiap pembatas (anggaran, rate, antrean) → gagal-TERBUKA sendiri-sendiri (galat satu pembatas tidak
  mematikan pembatas lain dan tidak menghentikan tim), tercatat di log + capabilities.json.

Hook ini didokumentasikan boleh `async` dan TIDAK dibatasi plugins.hook_callback_timeout
(website/docs/user-guide/features/hooks.md → pre_gateway_dispatch), jadi menunda pesan di sini aman.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable, Optional

from .limiters import (ADMIN_BUDGET_OVER, ADMIN_BUDGET_TEAM_OVER, ADMIN_BUDGET_TEAM_WARN, MSG_BUDGET_OVER,  # noqa: F401
                       MSG_BUDGET_TEAM_OVER, MSG_BUDGET_WARN, MSG_QUEUE, MSG_QUEUE_BEHIND, MSG_QUEUE_CANCELLED,
                       MSG_QUEUE_FULL, MSG_RATE, MSG_REFUSED_GENERIC, MSG_USER_LIMIT, MSG_USER_LIMIT_FULL, REFUSALS,
                       REFUSE_WAIT, RESERVE_GRACE, LimitCtx, SessionLoad, Verdict, default_limiters, refusal_for)
from .ratelimit import LoadProbe
from .redact import scrub_tokens
from .shared import log, log_once
from .upstream import HermesGateway

NOTICE_EVERY = 60.0         # jeda minimal antar pemberitahuan sejenis ke orang yang sama

# Perintah kendali yang TIDAK PERNAH memulai giliran LLM → bebas anggaran, rate limit, batas per orang, dan
# antrean. Daftar izin eksplisit, diperiksa satu per satu terhadap Hermes hulu (commit 5d87ea2):
#   stop                → gateway/slash_commands.py::_handle_stop_command (hanya menginterupsi)
#   approve, deny       → ::_handle_approve_command/_handle_deny_command. TIDAK memulai giliran: keduanya hanya
#                         memanggil tools/approval.py::resolve_gateway_approval, yang men-set `threading.Event`
#                         milik thread agen yang SUDAH berjalan dan sedang menunggu di dalam sebuah tool
#                         (`has_blocking_approval(session_key)`; tanpa itu balasannya hanya "tidak ada yang menunggu").
#                         Kelanjutannya adalah giliran yang sama, yang anggarannya sudah diperiksa saat diterima.
#                         Memeriksa anggaran di sini justru membuat giliran itu menunggu sampai timeout approval
#                         (auto-deny) — kebuntuan yang dihindari Hermes sendiri (run_inbound: «never a queued turn
#                         (auto-deny deadlock)»). Dipaku oleh TestApproveDenyExemption.
#   status, context/ctx, usage → gateway/slash_commands_status.py (hanya membaca)
#   help, commands, whoami     → gateway/slash_commands.py (katalog / info akses)
#   new/reset           → gateway/run_inbound.py::_hm_cmd_new → _handle_reset_command (sesi baru, tanpa giliran)
#   title, sessions     → gateway/slash_commands_session.py (judul / daftar / pindah sesi)
# SENGAJA TIDAK masuk: queue/q, steer, btw, bg, retry, undo, plan, goal, subgoal, loop, heartbeat, moa,
# learn, init, blueprint, refine, review, compress, /<skill>, /<bundle>, quick command — semuanya memulai
# (atau menjadwalkan) panggilan LLM. Perintah lain yang tidak dikenal di sini juga lewat gerbang biasa.
EXEMPT_COMMANDS = frozenset({
    "stop", "approve", "deny", "status", "context", "ctx", "usage", "help", "commands", "whoami",
    "new", "reset", "title", "sessions",
})
# Sama dengan gateway/platforms/event.py::_ATTACHMENT_REF_RE (awalan lampiran Desktop sebelum teks).
_ATTACHMENT_REF_RE = re.compile(r"^(?:@(?:image|file|url):[^\n]+\n?)+", re.IGNORECASE)


_UPSTREAM = HermesGateway()


def parse_command(text: Any, allow_control: bool = True) -> Optional[str]:
    """Nama perintah slash seperti `MessageEvent.get_command()` di Hermes hulu: token pertama tanpa "/",
    huruf kecil, tanpa "@bot"; None bila bukan perintah (termasuk path berkas seperti "/tmp/a").
    "/ kerjakan X" → "" (bukan perintah yang dikenal)."""
    if not allow_control or not isinstance(text, str):
        return None
    body = _ATTACHMENT_REF_RE.sub("", text.lstrip()).lstrip()
    if not body.startswith("/"):
        return None
    raw = body.split(maxsplit=1)[0][1:].lower().split("@", 1)[0]
    return None if "/" in raw else raw


def event_command(event: Any, text: Any = None) -> Optional[str]:
    """Perintah slash pada `event`: `event.get_command()` milik Hermes bila ada, kalau tidak pengurai setara.
    Gagal → None (diperlakukan sebagai pesan biasa, yaitu lewat semua gerbang)."""
    found, got = _UPSTREAM.event_get_command(event)
    if found:
        return got
    return parse_command(getattr(event, "text", None) if text is None else text,
                         bool(getattr(event, "allow_gateway_control", True)))


def is_exempt_command(event: Any, text: Any = None) -> bool:
    return (event_command(event, text) or "") in EXEMPT_COMMANDS

MSG_HELD = ("⏸️ <@{uid}> onboarding-mu masih menunggu persetujuan admin tim, jadi Hermes belum bisa melayanimu. "
            "Admin sudah diberi tahu.")
MSG_INACTIVE = "<@{uid}> akses Hermes-mu sedang nonaktif. Hubungi admin tim untuk mengaktifkannya lagi."
MSG_UNLISTED = ("⏳ <@{uid}> kamu belum terdaftar sebagai anggota tim di Hermes (onboarding belum selesai atau "
                "belum disetujui), jadi pesanmu belum dilayani. Kalau role-mu baru saja diberikan, tunggu sebentar; "
                "kalau tetap begini, hubungi admin tim.")
MSG_GATE_ERROR = ("⚠️ <@{uid}> penjaga akses tim sedang bermasalah, jadi pesanmu tidak diproses dulu. Coba lagi "
                  "sebentar lagi atau hubungi admin tim.")
MSG_SCRUB_ERROR = ("⚠️ <@{uid}> pesanmu tidak bisa diperiksa dari kredensial (penyamaran token sedang bermasalah), "
                   "jadi tidak diteruskan ke AI. Coba lagi sebentar lagi atau hubungi admin tim.")
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
                 sleep: Callable[[float], Any] = asyncio.sleep, upstream: Any = None):
        self.rt = rt
        self.clock, self.sleep = clock, sleep
        self.upstream = upstream or getattr(rt, "upstream", None) or HermesGateway()
        self.probe = LoadProbe(rt.turns, self.upstream)
        self.load = SessionLoad(rt, self.upstream, self.probe)
        self._told: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------ util
    async def _say(self, gateway: Any, source: Any, kind: str, text: str) -> None:
        """Balasan sampingan lewat jalur terdokumentasi `gateway.adapters[platform].send(...)` (adapter dicari
        `upstream.discord_adapter`). Tidak pernah melempar: kabar yang gagal tidak boleh mengubah keputusan."""
        try:
            uid = str(getattr(source, "user_id", "") or "")
            now = self.clock()
            if now - self._told.get((uid, kind), -1e9) < NOTICE_EVERY:
                return
            self._told[(uid, kind)] = now
            adapter = self.upstream.discord_adapter(gateway, getattr(source, "platform", None))
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

    def user_load(self, uid: str, gateway: Any, now: float) -> int:
        return self.load.user_load(uid, gateway, now)

    # ------------------------------------------------------------------ hook
    async def handle(self, event: Any = None, gateway: Any = None, **_kwargs: Any) -> Optional[dict]:
        carry: dict = {}
        try:
            return await self._handle(event, gateway, carry)
        except Exception as exc:  # hook kebijakan tidak boleh menjatuhkan dispatch
            # Jaring terakhir. Tahap-tahap di `_handle` sudah punya try masing-masing, jadi yang sampai ke sini
            # hanya galat di penentuan lingkup (sebelum diketahui pesan ini urusan tim atau bukan) atau di lem
            # antar-tahap. Di titik itu tidak diketahui apakah pesan berasal dari channel tim, jadi pesan TIDAK
            # ditolak (menolak di sini berarti satu bug mematikan Hermes untuk semua channel, termasuk yang
            # bukan milik tim) — tetapi token yang jelas tetap disamarkan: hasil tahap penyamaran bila sudah
            # dihitung, kalau belum dicoba sekali lagi di sini dengan pola bawaan.
            result = carry["result"] if "result" in carry else self._scrub_fallback(event)
            log.warning("team-guard: pre_gateway_dispatch gagal (%s: %s); pesan diteruskan %s",
                        type(exc).__name__, exc, "dengan token tersamar" if result else "apa adanya")
            return result

    @staticmethod
    def _scrub_fallback(event: Any) -> Optional[dict]:
        try:
            text = getattr(event, "text", None)
            new, kinds = scrub_tokens(text) if isinstance(text, str) and text else (text, [])
            return {"action": "rewrite", "text": new} if kinds else None
        except Exception:
            return None

    def _health(self, key: str, ok: bool, reason: str) -> None:
        """Catat kesehatan satu bagian di capabilities.json (dibaca kit_doctor); diam bila runtime tidak punya."""
        try:
            note = getattr(self.rt, "note_health", None)
            if callable(note):
                note(key, ok, reason)
        except Exception:
            pass

    async def _handle(self, event: Any, gateway: Any, carry: Optional[dict] = None) -> Optional[dict]:
        carry = carry if carry is not None else {}
        # ---- Lingkup: hanya pesan Discord dari manusia di channel tim. (`rt.roster()` total: roster.json
        # yang salah bentuk menghasilkan roster dengan nilai aman, bukan galat — lihat roster.Roster.)
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
        try:                                              # kabar skema: sampingan, tidak boleh memengaruhi gerbang
            await self._schema_notice(gateway, roster)
        except Exception as exc:
            log_once("dispatch-schema", "kabar skema roster gagal (%s: %s)", type(exc).__name__, exc)

        # ---- E. Gerbang akses: gagal-TERTUTUP. Galat di sini (bukan di tahap lain) = pesan non-admin
        # ditolak: gerbang ini batas keamanan (orang yang belum disetujui admin), isinya hanya pencarian di
        # roster + onboarding.json, dan meneruskan pesan saat gerbangnya sendiri rusak berarti melayani
        # siapa pun yang punya role.
        if not admin:
            try:
                denied = await self._access_gate(gateway, source, roster, uid)
            except Exception as exc:
                log_once("dispatch-gate", "gerbang akses gagal (%s: %s); pesan non-admin ditolak sampai pulih",
                         type(exc).__name__, exc)
                self._health("dispatch.access_gate", False, f"{type(exc).__name__}: {exc}")
                await self._say(gateway, source, "gate-error", MSG_GATE_ERROR.format(uid=uid))
                return {"action": "skip", "reason": "team-guard: gerbang akses gagal"}
            if denied is not None:
                return denied

        # ---- C. Token yang jelas di chat disamarkan sebelum sampai ke AI; pesan aslinya dihapus.
        # Gagal-TERTUTUP: bila penyamarannya sendiri melempar, pesan tidak diteruskan (lebih baik satu pesan
        # tertahan daripada kredensial sampai ke AI). Tahap ini berjalan SEBELUM pembatas mana pun dan hasilnya
        # disimpan di `carry`, jadi galat di pembatas tidak pernah bisa melewatkannya.
        try:
            result = await self._scrub(event, gateway, source, settings, uid, carry)
        except Exception as exc:
            log_once("dispatch-scrub", "penyamaran token di chat gagal (%s: %s); pesan tidak diteruskan",
                     type(exc).__name__, exc)
            self._health("dispatch.scrub_chat", False, f"{type(exc).__name__}: {exc}")
            await self._say(gateway, source, "scrub-error", MSG_SCRUB_ERROR.format(uid=uid))
            return {"action": "skip", "reason": "team-guard: penyamaran token gagal"}
        carry["result"] = result

        # ---- D. Skala. Admin dan perintah kendali di daftar izin (/stop, /approve, …) tidak pernah ditunda.
        # Teks lain yang diawali "/" (termasuk "/ kerjakan X", "/tmp/a jelaskan", /btw, /<skill>) adalah
        # pesan/giliran biasa bagi Hermes, jadi lewat semua pembatas seperti pesan lain.
        try:
            command = event_command(event)
        except Exception:
            command = None                                # tidak bisa diurai = pesan biasa = lewat semua pembatas
        if admin or (command or "") in EXEMPT_COMMANDS:
            return result
        ctx = LimitCtx(self, event, gateway, source, uid, admin, command, roster)
        # Tiap pembatas gagal-TERBUKA sendiri-sendiri: `check` yang melempar dilewati (log sekali per pembatas +
        # capabilities.json `limiter.<nama>`), pembatas BERIKUTNYA tetap diperiksa. Pilihan ini disengaja:
        # pembatas adalah pengatur beban/biaya, bukan batas keamanan — bug di satu pembatas tidak boleh
        # membuat seluruh tim tidak terlayani, dan batas keras Hermes sendiri (slot sesi gateway) tetap berlaku.
        # asyncio.CancelledError (gateway berhenti) bukan Exception, jadi tetap diteruskan apa adanya.
        for limiter in list(self.rt.limiters):     # anggaran → rate → batas per orang → antrean global
            try:
                verdict = await limiter.check(ctx)
            except Exception as exc:
                name = str(getattr(limiter, "name", "") or type(limiter).__name__)
                log_once(f"limiter-error-{name}", "pembatas '%s' gagal (%s: %s); pembatas ini dilewati, yang lain "
                         "tetap berlaku", name, type(exc).__name__, exc)
                try:
                    self.rt.metrics.limiter_error(name)
                except Exception:
                    pass
                self._health(f"limiter.{name}", False, f"{type(exc).__name__}: {exc}")
                continue
            if verdict is not None:
                return await self._refuse(ctx, verdict)
        return result

    async def _access_gate(self, gateway: Any, source: Any, roster: Any, uid: str) -> Optional[dict]:
        """Non-admin hanya dilayani bila TERDAFTAR aktif di roster (`users`). Ditahan / nonaktif / tidak
        dikenal ditolak di semua channel tim, termasuk #hermes-shared. "Tidak dikenal" mencakup orang yang
        punya role anggota tetapi belum ada di roster (role diberikan saat bot offline, sebelum susulan; atau
        onboarding.json hilang/rusak sehingga tanda "ditahan" lenyap): tanpa aturan ini mereka dilayani hanya
        karena Hermes mengizinkan role-nya."""
        status = roster.user_status(uid)
        if status == "active":
            return None                                   # disetujui admin; tahanan lama tidak berlaku
        if self.rt.state.is_held(uid):
            await self._say(gateway, source, "held", MSG_HELD.format(uid=uid))
            return {"action": "skip", "reason": "team-guard: onboarding ditahan"}
        if status == "inactive":
            await self._say(gateway, source, "inactive", MSG_INACTIVE.format(uid=uid))
            return {"action": "skip", "reason": "team-guard: anggota nonaktif"}
        await self._say(gateway, source, "unlisted", MSG_UNLISTED.format(uid=uid))
        return {"action": "skip", "reason": "team-guard: belum terdaftar di roster"}

    async def _scrub(self, event: Any, gateway: Any, source: Any, settings: dict, uid: str,
                     carry: dict) -> Optional[dict]:
        text = getattr(event, "text", None)
        if not (settings.get("scrub_chat") and isinstance(text, str) and text):
            return None
        new, kinds = scrub_tokens(text)
        if not kinds:
            return None
        result = {"action": "rewrite", "text": new}
        carry["result"] = result                  # dicatat SEBELUM hapus/kabar, supaya galat apa pun tetap menyamarkan
        deleted = bool(settings.get("delete_secret_messages")) and await self._delete(event)
        await self._say(gateway, source, "scrub", MSG_SCRUB.format(
            uid=uid, kinds=", ".join(sorted(set(kinds))),
            deleted=" dan pesan aslinya dihapus" if deleted else ""))
        return result

    async def _refuse(self, ctx: LimitCtx, verdict: Any) -> dict:
        """Satu jalur penolakan: pesan + metrik dari SATU tabel (`refusals.REFUSALS`), `reason` untuk hook.
        Sekali pembatas memutuskan menolak, hasilnya SELALU `skip`: `kind` yang tidak terdaftar, isian templat
        yang kurang, kabar yang gagal terkirim, atau `followup` yang melempar tidak pernah mengubah penolakan
        menjadi "diteruskan"."""
        reason = str(getattr(verdict, "reason", "") or "dibatasi")
        try:
            refusal = refusal_for(verdict)
            kind = getattr(verdict, "kind", None)
            if kind not in REFUSALS:
                log_once(f"refusal-unknown-{kind}", "jenis penolakan '%s' tidak terdaftar di refusals.REFUSALS; "
                         "pesan umum dipakai dan dihitung sebagai '%s'", kind, refusal.metric)
            try:
                text = refusal.template.format(uid=ctx.uid, **dict(getattr(verdict, "fmt", None) or {}))
            except Exception as exc:
                log_once(f"refusal-format-{kind}", "templat penolakan '%s' tidak bisa diisi (%s: %s); pesan umum "
                         "dipakai", kind, type(exc).__name__, exc)
                text = MSG_REFUSED_GENERIC.format(uid=ctx.uid)
            await ctx.say(refusal.say_key, text)
            followup = getattr(verdict, "followup", None)
            if followup is not None:
                try:
                    await followup()
                except Exception as exc:
                    log_once("refusal-followup", "kabar lanjutan penolakan gagal (%s: %s)", type(exc).__name__, exc)
            if refusal.metric:
                self.rt.metrics.refuse(refusal.metric)
        except Exception as exc:
            log_once("refusal-path", "jalur penolakan gagal sebagian (%s: %s); pesan tetap ditolak",
                     type(exc).__name__, exc)
        return {"action": "skip", "reason": f"team-guard: {reason}"}

    # ------------------------------------------------------------------ kabar admin
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
            adapter = self.upstream.discord_adapter(gateway)
            if adapter is not None:
                await adapter.send(str(roster.admin_channel), text)
        except Exception as exc:
            log_once("dispatch-admin", "kabar ke channel admin gagal (%s: %s)", type(exc).__name__, exc)

    async def _schema_notice(self, gateway: Any, roster: Any) -> None:
        """Roster bersekema lebih baru dari yang dipahami plugin: admin diberi tahu sekali sehari."""
        if not getattr(roster, "newer_schema", False):
            return
        text = self.rt.schema_notice(roster)
        if text:
            await self._notify_admin(gateway, text)

    def cancel_waiters(self) -> int:
        """Plugin di-unload: semua penunggu berhenti dan mengabari pemiliknya (lihat AdmissionLimiter)."""
        return len(self.rt.queue.cancel_all())
