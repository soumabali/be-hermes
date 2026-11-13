"""Hook `pre_gateway_dispatch`: gerbang akses (ditahan/nonaktif), penyamaran token di chat,
anggaran token, rate limit per orang, batas sesi per orang, dan antrean masuk yang adil.

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
                       MSG_QUEUE_FULL, MSG_RATE, MSG_USER_LIMIT, MSG_USER_LIMIT_FULL, REFUSAL_TEXT, REFUSE_WAIT,
                       RESERVE_GRACE, LimitCtx, SessionLoad, Verdict, default_limiters)
from .ratelimit import LoadProbe
from .redact import scrub_tokens
from .shared import log, log_once
from .upstream import HermesGateway

NOTICE_EVERY = 60.0         # jeda minimal antar pemberitahuan sejenis ke orang yang sama

# Perintah kendali yang TIDAK PERNAH memulai giliran LLM → bebas anggaran, rate limit, batas per orang, dan
# antrean. Daftar izin eksplisit, diperiksa satu per satu terhadap Hermes hulu (commit 5d87ea2):
#   stop                → gateway/slash_commands.py::_handle_stop_command (hanya menginterupsi)
#   approve, deny       → ::_handle_approve_command/_handle_deny_command (melanjutkan giliran yang menunggu)
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

    def user_load(self, uid: str, gateway: Any, now: float) -> int:
        return self.load.user_load(uid, gateway, now)

    # ------------------------------------------------------------------ hook
    async def handle(self, event: Any = None, gateway: Any = None, **_kwargs: Any) -> Optional[dict]:
        carry: dict = {}
        try:
            return await self._handle(event, gateway, carry)
        except Exception as exc:  # hook kebijakan tidak boleh menjatuhkan dispatch
            # Penyamaran token yang sudah dihitung TETAP dikembalikan: galat di tahap sesudahnya (anggaran,
            # rate limit, antrean) tidak boleh membuat token asli sampai ke AI.
            result = carry.get("result")
            log.warning("team-guard: pre_gateway_dispatch gagal (%s: %s); pesan diteruskan %s",
                        type(exc).__name__, exc, "dengan token tersamar" if result else "apa adanya")
            return result

    async def _handle(self, event: Any, gateway: Any, carry: Optional[dict] = None) -> Optional[dict]:
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
        await self._schema_notice(gateway, roster)

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
                if carry is not None:         # dicatat SEBELUM tahap lain, supaya galat apa pun tetap menyamarkan
                    carry["result"] = {"action": "rewrite", "text": new}
                deleted = bool(settings.get("delete_secret_messages")) and await self._delete(event)
                await self._say(gateway, source, "scrub", MSG_SCRUB.format(
                    uid=uid, kinds=", ".join(sorted(set(kinds))),
                    deleted=" dan pesan aslinya dihapus" if deleted else ""))
        result = {"action": "rewrite", "text": rewritten} if rewritten is not None else None

        # D. Skala. Admin dan perintah kendali di daftar izin (/stop, /approve, …) tidak pernah ditunda.
        # Teks lain yang diawali "/" (termasuk "/ kerjakan X", "/tmp/a jelaskan", /btw, /<skill>) adalah
        # pesan/giliran biasa bagi Hermes, jadi lewat semua pembatas seperti pesan lain.
        if admin or is_exempt_command(event):
            return result
        ctx = LimitCtx(self, event, gateway, source, uid, admin, event_command(event), roster)
        for limiter in self.rt.limiters:           # anggaran → rate → batas per orang → antrean global
            verdict = await limiter.check(ctx)
            if verdict is not None:
                return await self._refuse(ctx, verdict)
        return result

    async def _refuse(self, ctx: LimitCtx, verdict: Verdict) -> dict:
        """Satu jalur penolakan: pesan dari satu tabel, satu `metrics.refuse`, `reason` untuk hook."""
        key, template = REFUSAL_TEXT[verdict.kind]
        await ctx.say(key, template.format(uid=ctx.uid, **verdict.fmt))
        if verdict.followup is not None:
            await verdict.followup()
        if verdict.metric:
            self.rt.metrics.refuse(verdict.metric)
        return {"action": "skip", "reason": f"team-guard: {verdict.reason}"}

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
            adapters = getattr(gateway, "adapters", None) or {}
            adapter = next((a for p, a in adapters.items()
                            if str(getattr(p, "value", p)).lower() == "discord"), None)
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
