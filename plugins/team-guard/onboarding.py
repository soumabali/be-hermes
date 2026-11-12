"""Onboarding/offboarding otomatis: role @entry, @backend → `hermes_team.py onboard`, sambutan,
penahanan (pemberi role tidak tepercaya), susulan berkala, dan pengawas roster.json.

Logika di sini tidak mengimpor discord.py: objek Discord diakses lewat `port` (discord_events.DiscordPort)
dan atribut bergaya discord.py (member.id, member.roles, guild.audit_logs, …), jadi bisa diuji dengan tiruan.
hermes_team.py TIDAK pernah diimpor (folder kit milik root); selalu lewat subprocess `--events`.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from .shared import log, log_once

DEFAULT_WELCOME = """Halo {mention}, selamat datang di tim {team_name}! 👋

Ini ruang kerja Hermes pribadimu: hanya kamu dan bot yang bisa melihat channel ini. Memory, skill, \
secret, dan sandbox-mu terpisah dari anggota lain.

• Tulis langsung di sini, tidak perlu mention bot.
• Topik khusus? Buat thread dari pesanmu; thread tetap memakai Hermes-mu.
• Token/password simpan lewat /env-set (jangan ditempel di chat). Daftar namanya: /env-list.
• Skill dan konteks tim ada di {shared_channel} (mention bot di sana).
• Model AI-mu: {model}, diatur admin.

Mulai saja dengan memperkenalkan diri dan apa yang sedang kamu kerjakan."""

DEFAULT_WELCOME_BACK = """Halo lagi {mention}, ruang kerja Hermes-mu aktif kembali. Memory, skill, dan \
secret lamamu masih ada. Model AI-mu: {model}."""

PREPARING = "⏳ Ruang kerja Hermes-mu sedang disiapkan… (biasanya 30–90 detik)"
PLACEHOLDERS = ("mention", "display_name", "name", "team_name", "model", "channel", "shared_channel",
                "admin_channel", "profile")
AUDIT_WINDOW_LIVE = timedelta(minutes=10)
AUDIT_WINDOW_RECONCILE = timedelta(days=45)
KIT_TIMEOUT = 900.0


# ----------------------------------------------------------------------------- murni
def render_welcome(template: str, **fields: Any) -> str:
    """Isi placeholder yang dikenal; yang tidak dikenal dibiarkan apa adanya. Sebutan massal dinetralkan
    (lapisan kedua setelah AllowedMentions) dan panjang dipotong ke batas Discord."""
    def _sub(match: re.Match) -> str:
        key = match.group(1)
        return str(fields.get(key, "")) if key in PLACEHOLDERS else match.group(0)

    text = re.sub(r"\{(\w+)\}", _sub, template or "")
    text = text.replace("@everyone", "@​everyone").replace("@here", "@​here")
    text = re.sub(r"<@&(\d+)>", "@role", text)
    return text[:2000]


def welcome_fields(roster: Any, user_id: str, info: dict) -> dict:
    user = roster.users.get(str(user_id), {})
    channel = str(info.get("channel_id") or user.get("channel_id") or "")
    display = str(info.get("display_name") or user.get("display_name") or info.get("name") or "")
    # Nama tampilan dikendalikan user Discord → buang markup sebutan.
    display = re.sub(r"[@<>`]", "", display)[:64]
    return {
        "mention": f"<@{user_id}>", "display_name": display,
        "name": str(info.get("name") or user.get("name") or ""), "team_name": roster.team_name,
        "model": str(info.get("model") or user.get("model") or "-"),
        "channel": f"<#{channel}>" if channel else "", "shared_channel": f"<#{roster.shared_channel}>",
        "admin_channel": f"<#{roster.admin_channel}>",
        "profile": str(info.get("profile") or user.get("profile") or ""),
    }


def welcome_text(roster: Any, user_id: str, info: dict, back: bool) -> str:
    conf = roster.onboarding
    template = (conf.get("welcome_back_message") or DEFAULT_WELCOME_BACK) if back \
        else (conf.get("welcome_message") or DEFAULT_WELCOME)
    return render_welcome(str(template), **welcome_fields(roster, user_id, info))


def actor_trusted(actor_id: Any, *, roster: Any, owner_id: Any = None, bot_id: Any = None) -> bool:
    """Pemberi @backend tepercaya: admin tim, pemilik server, bot ini, atau trusted_actor_ids.
    `verify_actor: false` → semua dipercaya (termasuk yang tidak diketahui)."""
    if not roster.onboarding.get("verify_actor", True):
        return True
    if actor_id in (None, ""):
        return False
    actor = str(actor_id)
    trusted = set(roster.admins) | {str(x) for x in roster.onboarding.get("trusted_actor_ids") or []}
    trusted |= {str(x) for x in (owner_id, bot_id) if x}
    return actor in trusted


def _role_ids(obj: Any) -> set[str]:
    return {str(getattr(r, "id", r)) for r in (getattr(obj, "roles", None) or [])}


async def find_role_actor(guild: Any, user_id: Any, role_id: Any, *, window: timedelta,
                          action: Any = None, limit: int = 100) -> tuple[Optional[str], str]:
    """Siapa yang terakhir MEMBERI `role_id` ke `user_id` menurut audit log. (actor_id | None, keterangan)."""
    after = datetime.now(timezone.utc) - window
    best: Optional[tuple[Any, str]] = None
    try:
        kwargs: dict[str, Any] = {"limit": limit, "after": after, "oldest_first": False}
        if action is not None:
            kwargs["action"] = action
        async for entry in guild.audit_logs(**kwargs):
            if str(getattr(getattr(entry, "target", None), "id", "")) != str(user_id):
                continue
            if str(role_id) not in _role_ids(getattr(entry, "after", None)):
                continue
            actor = getattr(entry, "user", None)
            actor_id = str(getattr(actor, "id", "") or getattr(entry, "user_id", "") or "")
            if not actor_id:
                continue
            created = getattr(entry, "created_at", None)
            if created is not None and created < after:
                continue
            if best is None or (created is not None and best[0] is not None and created > best[0]):
                best = (created, actor_id)
    except Exception as exc:
        return None, f"audit log tidak bisa dibaca ({type(exc).__name__}; bot butuh izin View Audit Log)"
    if best is None:
        return None, "pemberi role tidak ditemukan di audit log"
    return best[1], ""


def kit_argv(roster: Any, command: str, args: list[str]) -> list[str]:
    kit = roster.kit
    argv = [kit.get("python") or "python3", kit.get("script") or "hermes_team.py", command]
    if kit.get("team_file"):
        argv += ["-c", kit["team_file"]]
    if kit.get("members_file"):
        argv += ["--members", kit["members_file"]]
    argv += ["--hermes-bin", roster.hermes_bin, "--hermes-root", str(roster.hermes_root), *args, "--events"]
    return argv


class KitResult:
    def __init__(self, returncode: int, result: Optional[dict], error: str, events: list[dict]):
        self.returncode, self.result, self.error, self.events = returncode, result, error, events

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.result is not None and not self.result.get("failures")

    def describe(self) -> str:
        if self.error:
            return self.error
        failures = (self.result or {}).get("failures") or []
        if failures:
            return "; ".join(str(f) for f in failures[:3])
        return f"hermes_team.py keluar dengan kode {self.returncode}"


async def run_kit(roster: Any, command: str, args: list[str],
                  on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
                  timeout: float = KIT_TIMEOUT) -> KitResult:
    """Jalankan `hermes_team.py <command> --events` dan baca satu objek JSON per baris."""
    argv = kit_argv(roster, command, args)
    env = dict(os.environ)
    env["HERMES_HOME"] = str(roster.hermes_root)
    events: list[dict] = []
    result: Optional[dict] = None
    error = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
    except OSError as exc:
        return KitResult(127, None, f"hermes_team.py tidak bisa dijalankan ({type(exc).__name__}: {exc})", [])

    async def _pump() -> bytes:
        nonlocal result, error
        assert proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.ensure_future(proc.stderr.read())
        async for raw in proc.stdout:
            try:
                event = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            kind = event.get("event")
            if kind == "result":
                result = event
            elif kind == "error":
                error = str(event.get("message") or "gagal")
            if on_event is not None:
                try:
                    await on_event(event)
                except Exception as exc:
                    log.warning("team-guard: penanganan event kit gagal (%s)", exc)
        err = await stderr_task
        await proc.wait()
        return err

    try:
        stderr = await asyncio.wait_for(_pump(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return KitResult(124, result, f"hermes_team.py {command} melewati {int(timeout)} detik", events)
    code = int(proc.returncode or 0)
    if code != 0 and not error and result is None:
        tail = stderr.decode("utf-8", "replace").strip().splitlines()
        error = tail[-1][:300] if tail else f"kode keluar {code}"
    return KitResult(code, result, error, events)


# ----------------------------------------------------------------------------- alur
class Onboarder:
    """Semua pekerjaan keanggotaan lewat SATU antrean (satu per satu), supaya beberapa role yang
    diberikan bersamaan tidak saling menimpa dan sambutan tidak pernah terkirim ganda."""

    def __init__(self, rt: Any, port: Any, live: Any, *, kit: Callable[..., Awaitable[KitResult]] = run_kit,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Any] = asyncio.sleep):
        self.rt, self.port, self.live = rt, port, live
        self.kit, self.clock, self.sleep = kit, clock, sleep
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        self._queued: set[tuple[str, str]] = set()
        self.max_parallel_seen = 0
        self._running = 0

    # ------------------------------------------------------------------ antrean
    def submit(self, kind: str, user_id: Any, **payload: Any) -> bool:
        key = (kind, str(user_id))
        if key in self._queued:
            return False
        if self._queue is None:
            self._queue = asyncio.Queue()
        self._queued.add(key)
        self._queue.put_nowait((kind, str(user_id), payload))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._work(), name="team-guard:onboarding")
        return True

    async def _work(self) -> None:
        assert self._queue is not None
        while True:
            kind, uid, payload = await self._queue.get()
            self._queued.discard((kind, uid))
            self._running += 1
            self.max_parallel_seen = max(self.max_parallel_seen, self._running)
            try:
                handler = {"onboard": self._onboard, "offboard": self._offboard, "activate": self._activate}[kind]
                await handler(uid, **payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("team-guard: pekerjaan %s untuk %s gagal", kind, uid)
                await self.notify(f"❌ Pekerjaan {kind} untuk <@{uid}> gagal ({type(exc).__name__}: {exc}). "
                                  "Lihat `hermes logs`.")
            finally:
                self._running -= 1
                self._queue.task_done()

    async def drain(self) -> None:
        """Tunggu antrean kosong (uji & penutupan)."""
        if self._queue is not None:
            await self._queue.join()

    def close(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    # ------------------------------------------------------------------ kabar
    async def notify(self, text: str) -> None:
        roster = self.rt.roster()
        if not roster.onboarding.get("admin_notices", True) or not roster.admin_channel:
            return
        try:
            await self.port.send(roster.admin_channel, text)
        except Exception as exc:
            log.warning("team-guard: kabar ke #hermes-admin gagal (%s)", exc)

    # ------------------------------------------------------------------ event Discord
    async def on_member_join(self, member: Any) -> None:
        if getattr(member, "bot", False) or not self._our_guild(member):
            return
        await self._give_entry(member, "user baru masuk server")

    async def on_member_update(self, before: Any, after: Any) -> None:
        if getattr(after, "bot", False) or not self._our_guild(after):
            return
        roster = self.rt.roster()
        role = roster.member_role
        had, has = role in _role_ids(before), role in _role_ids(after)
        if getattr(before, "pending", False) and not getattr(after, "pending", False):
            await self._give_entry(after, "lolos membership screening")
        if role and has and not had:
            self.submit("onboard", after.id, member=after, source="event")
        elif role and had and not has:
            self.rt.state.unhold(after.id)
            self.submit("offboard", after.id, member=after, left=False, reason="role @backend dicabut")

    async def on_member_remove(self, member: Any) -> None:
        if getattr(member, "bot", False) or not self._our_guild(member):
            return
        self.rt.state.unhold(member.id)
        self.submit("offboard", member.id, member=None, left=True, reason="keluar dari server")

    def _our_guild(self, member: Any) -> bool:
        gid = str(getattr(getattr(member, "guild", None), "id", "") or "")
        return not gid or gid == self.rt.roster().guild_id

    async def _give_entry(self, member: Any, why: str) -> bool:
        roster = self.rt.roster()
        conf = roster.onboarding
        if not (conf.get("enabled") and conf.get("give_entry_role") and roster.entry_role):
            return False
        if getattr(member, "pending", False):
            return False                      # belum menyetujui aturan server (membership screening)
        roles = _role_ids(member) - {roster.guild_id}
        if roles & {roster.member_role, roster.entry_role, roster.admin_role}:
            return False
        try:
            await self.port.add_role(member, roster.entry_role, f"Hermes: {why}")
            return True
        except Exception as exc:
            log_once("entry-role", "role entry tidak bisa diberikan (%s: %s) — pastikan role bot di atas @entry "
                     "dan bot punya Manage Roles", type(exc).__name__, exc)
            return False

    # ------------------------------------------------------------------ onboard
    async def _verify_actor(self, uid: str, roster: Any, window: timedelta) -> tuple[bool, Optional[str], str]:
        if not roster.onboarding.get("verify_actor", True) or uid in roster.admins:
            return True, None, ""             # admin tim yang memberi dirinya @backend = sudah disetujui admin
        guild = self.port.guild()
        if guild is None:
            return False, None, "server Discord belum termuat"
        actor, why = None, ""
        for attempt in range(3):              # entri audit log bisa terlambat sedikit dari event role
            actor, why = await find_role_actor(guild, uid, roster.member_role, window=window,
                                               action=self.port.audit_action())
            if actor or "tidak bisa dibaca" in why:
                break
            if attempt < 2:
                await self.sleep(1.5)
        trusted = actor_trusted(actor, roster=roster, owner_id=getattr(guild, "owner_id", None),
                                bot_id=self.port.bot_id())
        if trusted:
            return True, actor, ""
        return False, actor, (f"role diberikan oleh <@{actor}>, bukan admin tim" if actor else why)

    async def _onboard(self, uid: str, member: Any = None, source: str = "event") -> None:
        roster = self.rt.roster(force=True)
        conf = roster.onboarding
        if not conf.get("enabled", True) or not roster.member_role:
            return
        if member is None:
            member = await self.port.get_member(uid)
        if member is None or getattr(member, "bot", False) or roster.member_role not in _role_ids(member):
            return                            # role sudah dicabut lagi / user sudah keluar sebelum giliran ini
        if uid in roster.users:
            self.rt.state.unhold(uid)
            if not self.rt.state.was_welcomed(uid):
                await self._activate(uid)
            return
        window = AUDIT_WINDOW_RECONCILE if source == "reconcile" else AUDIT_WINDOW_LIVE
        ok, actor, why = await self._verify_actor(uid, roster, window)
        if not ok:
            fresh = self.rt.state.hold(uid, why, actor=actor, username=str(getattr(member, "name", "") or ""))
            if fresh:
                await self.notify(
                    f"⏸️ Onboarding <@{uid}> **ditahan**: {why}. Selama ditahan ia tidak dilayani Hermes di channel "
                    "mana pun. Setujui dengan menulis di sini: \"lanjutkan onboarding "
                    f"<@{uid}>\" — atau cabut role @backend-nya.")
            return
        self.rt.state.unhold(uid)
        started = self.clock()
        pending: dict[str, Any] = {}

        async def on_event(event: dict) -> None:
            if event.get("event") == "channel" and event.get("channel_id") and "message" not in pending:
                message = await self.port.send(str(event["channel_id"]), PREPARING)
                pending["message"] = message
                if message is not None:
                    self.rt.state.set_pending(uid, event["channel_id"], getattr(message, "id", ""))

        args = ["--user-id", uid, "--username", str(getattr(member, "name", "") or ""),
                "--display-name", str(getattr(member, "display_name", "") or getattr(member, "name", "") or "")]
        outcome = await self.kit(roster, "onboard", args, on_event)
        if not outcome.ok:
            reason = outcome.describe()
            self.rt.state.set_failed(uid, reason)
            await self._drop_pending(uid, pending.get("message"))
            await self.notify(f"❌ Onboarding <@{uid}> gagal: {reason}. Tidak ada sambutan yang dikirim. Perbaiki "
                              f"lalu tulis \"lanjutkan onboarding <@{uid}>\" (detail: audit.log).")
            return
        await self._activate(uid, info=outcome.result, started=started, pending=pending.get("message"),
                             status=str(outcome.result.get("status") or ""))

    async def _drop_pending(self, uid: str, message: Any = None) -> None:
        stored = self.rt.state.pop_pending(uid)
        try:
            if message is not None:
                await self.port.delete(message)
            elif stored:
                await self.port.delete_by_id(stored.get("channel_id"), stored.get("message_id"))
        except Exception:
            pass

    async def _activate(self, uid: str, info: Optional[dict] = None, started: Optional[float] = None,
                        pending: Any = None, status: str = "") -> None:
        """Rute berlaku → sambutan → bersih-bersih. Dipakai alur onboarding DAN pengawas roster
        (anggota yang ditambahkan lewat tool admin / CLI, termasuk "lanjutkan onboarding")."""
        ok, detail = await self.live.apply(f"onboarding {uid}")
        roster = self.rt.roster()
        user = roster.users.get(uid)
        if user is None:
            return                            # sudah dinonaktifkan lagi sebelum giliran ini
        if self.rt.state.was_welcomed(uid):
            await self._drop_pending(uid, pending)
            return
        info = {**user, **{k: v for k, v in (info or {}).items() if v not in (None, "")}}
        if not ok:
            # Sambutan baru dikirim setelah rute benar-benar berlaku; setelah restart, pengawas melanjutkan.
            if self.rt.state.notify_once(f"route-pending:{uid}"):
                await self.notify(f"🕓 <@{uid}> sudah dibuatkan profile `{info.get('profile')}`, tetapi rute belum "
                                  f"aktif ({detail}). Sambutan dikirim otomatis begitu rute berlaku.")
            return
        self.rt.state.unhold(uid)
        self.rt.state.clear_notified(f"route-pending:{uid}")
        back = status == "reactivated" or (status != "created" and self.rt.state.known_before(uid))
        channel = str(info.get("channel_id") or "")
        sent = None
        try:
            sent = await self.port.send(channel, welcome_text(roster, uid, info, back), mention_user=uid)
        except Exception as exc:
            log.warning("team-guard: sambutan untuk %s gagal dikirim (%s)", uid, exc)
        if sent is None:
            await self.notify(f"⚠️ <@{uid}> aktif (<#{channel}>, profile `{info.get('profile')}`), tetapi pesan "
                              "sambutan gagal dikirim — periksa izin bot di channel itu.")
        self.rt.state.mark_welcomed(uid, channel)
        self.rt.state.clear_notified(f"lost-role:{uid}")
        await self._drop_pending(uid, pending)
        if roster.onboarding.get("remove_entry_role") and roster.entry_role:
            member = await self.port.get_member(uid)
            if member is not None and roster.entry_role in _role_ids(member):
                try:
                    await self.port.remove_role(member, roster.entry_role, "Hermes: onboarding selesai")
                except Exception as exc:
                    log_once("entry-remove", "role entry tidak bisa dicabut (%s)", exc)
        took = f" · {int(self.clock() - started)} dtk" if started is not None else ""
        verb = "aktif kembali" if back else "aktif"
        await self.notify(f"✅ <@{uid}> {verb}: <#{channel}> · profile `{info.get('profile')}` · model "
                          f"{info.get('model') or '-'}{took}")

    # ------------------------------------------------------------------ offboard
    async def _offboard(self, uid: str, member: Any = None, left: bool = False, reason: str = "") -> None:
        self.rt.state.unhold(uid)
        roster = self.rt.roster(force=True)
        conf = roster.onboarding
        if not left and member is not None and roster.member_role in _role_ids(member):
            return                            # role sudah diberikan lagi sebelum giliran ini
        if uid in roster.users and conf.get("enabled", True) and conf.get("offboard_on_role_remove", True):
            started = self.clock()
            args = ["--user-id", uid] + ([] if conf.get("hide_channel_on_offboard", True)
                                         else ["--keep-channel-access"])
            outcome = await self.kit(roster, "offboard", args, None)
            if not outcome.ok:
                await self.notify(f"❌ Penonaktifan <@{uid}> ({reason}) gagal: {outcome.describe()}. Jalankan "
                                  f"\"nonaktifkan\" dari sini atau `hermes_team.py offboard --user-id {uid}`.")
                return
            ok, detail = await self.live.apply(f"offboarding {uid}")
            self.rt.state.mark_inactive(uid)
            name = str((outcome.result or {}).get("name") or roster.users[uid].get("name") or uid)
            tail = "" if ok else f" Rute belum tercabut dari gateway yang berjalan ({detail})."
            await self.notify(f"👋 <@{uid}> (`{name}`) dinonaktifkan — {reason}. Rute dicabut; profile, memory, "
                              f"secret, dan isi channel disimpan · {int(self.clock() - started)} dtk.{tail}")
        elif uid in roster.users:
            if self.rt.state.notify_once(f"lost-role:{uid}"):
                await self.notify(f"ℹ️ <@{uid}> {reason}, tetapi penonaktifan otomatis dimatikan "
                                  "(onboarding.offboard_on_role_remove). Nonaktifkan manual bila perlu.")
        if not left and member is not None and conf.get("entry_role_on_offboard", True):
            await self._give_entry(member, "kembali ke entry setelah @backend dicabut")

    # ------------------------------------------------------------------ susulan & pengawas
    async def reconcile(self) -> dict:
        """Susulan untuk perubahan saat bot offline. TIDAK PERNAH menonaktifkan anggota."""
        roster = self.rt.roster(force=True)
        conf = roster.onboarding
        summary = {"onboard": 0, "entry": 0, "lost": 0}
        guild = self.port.guild()
        if guild is None or not conf.get("enabled", True):
            return summary
        members = await self.port.members(guild)
        present = set()
        for member in members:
            if getattr(member, "bot", False):
                continue
            uid = str(member.id)
            present.add(uid)
            roles = _role_ids(member)
            if roster.member_role and roster.member_role in roles:
                self.rt.state.clear_notified(f"lost-role:{uid}")
                if uid not in roster.users and not self.rt.state.is_held(uid):
                    summary["onboard"] += self.submit("onboard", uid, member=member, source="reconcile")
            elif uid in roster.users:
                if uid not in roster.admins and self.rt.state.notify_once(f"lost-role:{uid}"):
                    summary["lost"] += 1
                    await self.notify(f"ℹ️ <@{uid}> masih anggota aktif tetapi tidak lagi punya role @backend "
                                      "(berubah saat bot offline). Susulan tidak menonaktifkan siapa pun — "
                                      "cabut/beri lagi role-nya atau \"nonaktifkan\" dari sini.")
            elif await self._give_entry(member, "susulan: user tanpa role"):
                summary["entry"] += 1
        if members:
            for uid in sorted(set(roster.users) - present - set(roster.admins)):
                if self.rt.state.notify_once(f"lost-role:{uid}"):
                    summary["lost"] += 1
                    await self.notify(f"ℹ️ <@{uid}> masih anggota aktif tetapi sudah tidak ada di server (keluar "
                                      "saat bot offline). Susulan tidak menonaktifkan siapa pun.")
        return summary

    async def sync_once(self, startup: bool = False) -> None:
        """Samakan dengan roster.json: live reload + sambutan untuk anggota baru dari jalur lain."""
        roster = self.rt.roster(force=True)
        if not roster:
            return
        state = self.rt.state
        if state.baseline_needed:
            state.baseline(roster.users)
        await self.live.apply("gateway start" if startup else "roster.json berubah", startup=startup,
                              wait_served=not startup)
        for uid in sorted(roster.users):
            if not state.was_welcomed(uid):
                self.submit("activate", uid)
        for uid, entry in list(state.data["welcomed"].items()):
            if entry.get("active", True) and uid not in roster.users:
                state.mark_inactive(uid)

    async def watch(self, poll: float = 3.0) -> None:
        """Pengawas: roster.json berubah (CLI / teamctl / onboarding) → terapkan; susulan berkala."""
        last_reconcile = self.clock()
        while True:
            await self.sleep(poll)
            try:
                if self.rt.roster_cache.changed():
                    await self.sync_once()
                minutes = int(self.rt.roster().onboarding.get("reconcile_minutes") or 0)
                if minutes > 0 and self.clock() - last_reconcile >= minutes * 60:
                    last_reconcile = self.clock()
                    await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("team-guard: pengawas roster gagal sekali (%s: %s)", type(exc).__name__, exc)
