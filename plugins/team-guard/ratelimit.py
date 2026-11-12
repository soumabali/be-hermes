"""Skala: token bucket per orang, pelacak giliran aktif (total dan per orang), dan antrean masuk adil
(satu tiket per orang di FIFO global; pesan lain orang itu menunggu di barisnya sendiri).

Murni Python; jam disuntikkan supaya bisa diuji tanpa menunggu.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from .shared import log_once


class RateLimiter:
    """Token bucket per user: `burst` pesan langsung, lalu `rate_per_minute`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}     # uid → (token, waktu terakhir)

    def reserve(self, user_id: str, rate_per_minute: float, burst: float, now: float,
                max_wait: float = 45.0) -> tuple[float, bool]:
        """(detik tunggu, diterima?). Diterima → satu token dipakai (boleh "berutang" sampai max_wait).
        Ditolak → tidak ada token yang dipakai."""
        rate, burst = float(rate_per_minute or 0), max(1.0, float(burst or 1))
        if rate <= 0:
            return 0.0, True
        per_second = rate / 60.0
        with self._lock:
            tokens, last = self._buckets.get(user_id, (burst, now))
            tokens = min(burst, tokens + max(0.0, now - last) * per_second)
            wait = 0.0 if tokens >= 1.0 else (1.0 - tokens) / per_second
            if wait > max_wait:
                self._buckets[user_id] = (tokens, now)
                return wait, False
            self._buckets[user_id] = (tokens - 1.0, now)
            return wait, True

    def forget_idle(self, now: float, idle: float = 3600.0) -> None:
        with self._lock:
            for uid in [u for u, (_t, last) in self._buckets.items() if now - last > idle]:
                del self._buckets[uid]


class TurnTracker:
    """Giliran anggota yang sedang berjalan, dihitung dari hook terdokumentasi:
    `pre_llm_call` (mulai) dan `on_session_end` (selesai), plus "reservasi" singkat untuk pesan yang
    baru saja diloloskan antrean tetapi gilirannya belum mulai."""

    def __init__(self, reserve_ttl: float = 30.0, turn_ttl: float = 3600.0):
        self.reserve_ttl, self.turn_ttl = reserve_ttl, turn_ttl
        self._lock = threading.Lock()
        self._reserved: list[tuple[str, float]] = []
        self._active: dict[str, tuple[str, float]] = {}        # session_id → (uid, mulai)
        self.starts = self.ends = 0

    def reserve(self, user_id: str, now: float, chat_id: str = "") -> None:
        with self._lock:
            self._reserved.append((f"{user_id}:{chat_id}", now))

    def holds(self, user_id: str, chat_id: str, now: float) -> bool:
        """Orang ini sudah memegang reservasi untuk chat yang sama (pesan susulan → sesi yang sama)."""
        key = f"{user_id}:{chat_id}"
        with self._lock:
            return any(k == key and now - t < self.reserve_ttl for k, t in self._reserved)

    def reserved(self, now: float) -> int:
        with self._lock:
            self._reserved = [(k, t) for k, t in self._reserved if now - t < self.reserve_ttl]
            return len(self._reserved)

    def start(self, session_id: str, user_id: str, now: float, counted: bool = True) -> None:
        with self._lock:
            self.starts += 1
            prefix = f"{user_id}:"
            for i, (key, _t) in enumerate(self._reserved):
                if key.startswith(prefix):
                    del self._reserved[i]
                    break
            if counted and session_id:
                self._active[str(session_id)] = (str(user_id), now)

    def end(self, session_id: str) -> None:
        with self._lock:
            self.ends += 1
            self._active.pop(str(session_id), None)

    def active(self, now: float) -> int:
        with self._lock:
            for sid in [s for s, (_u, t) in self._active.items() if now - t > self.turn_ttl]:
                del self._active[sid]
            return len(self._active)

    def trusted(self) -> bool:
        """Hook terbukti hidup di Hermes ini (pernah melihat mulai DAN selesai)."""
        return self.starts > 0 and self.ends > 0

    # ------------------------------------------------------------------ per orang
    def user_active(self, user_id: str, now: float) -> int:
        """Sesi anggota ini yang sedang berjalan (menurut hook)."""
        uid = str(user_id)
        with self._lock:
            return sum(1 for u, t in self._active.values() if u == uid and now - t <= self.turn_ttl)

    def user_reserved(self, user_id: str, now: float) -> int:
        """Pesan orang ini yang sudah diloloskan tetapi gilirannya belum mulai."""
        prefix = f"{user_id}:"
        with self._lock:
            return sum(1 for k, t in self._reserved if k.startswith(prefix) and now - t < self.reserve_ttl)

    def by_user(self, now: float) -> dict[str, int]:
        """uid → jumlah sesi aktif (untuk metrik)."""
        out: dict[str, int] = {}
        with self._lock:
            for uid, t in self._active.values():
                if now - t <= self.turn_ttl:
                    out[uid] = out.get(uid, 0) + 1
        return out


class LoadProbe:
    """Jumlah sesi aktif. Sumber: hook (TurnTracker, hanya anggota) dan/atau internal gateway
    `GatewayRunner._running_agent_count()` (semua sesi). Keduanya hilang → None: antrean menonaktifkan
    diri dengan SATU baris log dan yang berlaku batas bawaan Hermes (max_concurrent_sessions)."""

    def __init__(self, turns: TurnTracker):
        self.turns = turns

    def __call__(self, gateway: Any, now: float) -> Optional[int]:
        runner_count: Optional[int] = None
        fn = getattr(gateway, "_running_agent_count", None)
        if callable(fn):
            try:
                runner_count = int(fn())
            except Exception:
                runner_count = None
        hook_count = self.turns.active(now) if self.turns.trusted() else None
        if runner_count is None and hook_count is None:
            log_once("queue-disabled", "antrean sesi nonaktif: GatewayRunner._running_agent_count tidak ada dan "
                     "hook pre_llm_call/on_session_end belum terlihat; batas bawaan Hermes yang berlaku")
            return None
        if runner_count is None:
            return hook_count
        if hook_count is None:
            return runner_count
        return min(hook_count, runner_count)


class Ticket:
    __slots__ = ("user_id", "t0", "cancelled")

    def __init__(self, user_id: str, t0: float):
        self.user_id, self.t0 = user_id, t0
        self.cancelled = False


Blocked = Optional[Callable[[str], bool]]


class AdmissionQueue:
    """Antrean adil. Tiap orang punya satu baris; di FIFO global yang bersaing hanya tiket TERDEPAN tiap
    orang, jadi orang yang mengirim 6 pesan tetap menempati satu posisi. Setelah tiket terdepannya keluar,
    orang itu pindah ke belakang (bergiliran dengan yang lain).

    `blocked(uid)` (opsional) menandai orang yang sedang tidak boleh mengambil slot (sudah di batas sesi per
    orang): ia dilewati, supaya tidak menahan orang lain di belakangnya."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: dict[str, list[Ticket]] = {}
        self._order: list[str] = []                          # urutan orang di FIFO global

    def enter(self, user_id: str, now: float) -> Ticket:
        ticket = Ticket(str(user_id), now)
        with self._lock:
            line = self._lines.get(ticket.user_id)
            if line is None:
                line = self._lines[ticket.user_id] = []
                self._order.append(ticket.user_id)
            line.append(ticket)
        return ticket

    def _snapshot(self, ticket: Ticket) -> tuple[int, list[str]]:
        """(urutan tiket di baris pemiliknya mulai 0, atau -1; orang-orang di depan pemiliknya)."""
        with self._lock:
            line = self._lines.get(ticket.user_id) or []
            own = next((i for i, item in enumerate(line) if item is ticket), -1)
            if own < 0:
                return -1, []
            return own, self._order[:self._order.index(ticket.user_id)]

    def position(self, ticket: Ticket, blocked: Blocked = None) -> int:
        """Posisi orang ini di FIFO global (1 = terdepan); 0 bila tiket tidak ada di antrean."""
        own, ahead = self._snapshot(ticket)
        if own < 0:
            return 0
        return 1 + sum(1 for uid in ahead if blocked is None or not blocked(uid))

    def behind_own(self, ticket: Ticket) -> int:
        """Jumlah pesan orang yang sama yang masih di depan tiket ini."""
        return max(0, self._snapshot(ticket)[0])

    def is_head(self, ticket: Ticket, blocked: Blocked = None) -> bool:
        own, ahead = self._snapshot(ticket)
        if own != 0 or (blocked is not None and blocked(ticket.user_id)):
            return False
        return all(blocked is not None and blocked(uid) for uid in ahead)

    def leave(self, ticket: Ticket) -> None:
        with self._lock:
            line = self._lines.get(ticket.user_id)
            if not line:
                return
            was_head = line[0] is ticket
            line[:] = [t for t in line if t is not ticket]
            if not line:
                del self._lines[ticket.user_id]
                self._order.remove(ticket.user_id)
            elif was_head:
                self._order.remove(ticket.user_id)           # giliran berikutnya: antre lagi dari belakang
                self._order.append(ticket.user_id)

    def cancel_all(self) -> list[Ticket]:
        """Tandai semua tiket batal (plugin di-unload / gateway berhenti); penunggunya berhenti di
        pemeriksaan berikutnya dan mengabari pemiliknya."""
        with self._lock:
            tickets = [t for line in self._lines.values() for t in line]
            for ticket in tickets:
                ticket.cancelled = True
        return tickets

    def users(self) -> int:
        with self._lock:
            return len(self._order)

    def __len__(self) -> int:
        with self._lock:
            return sum(len(line) for line in self._lines.values())


async def wait_for_slot(queue: AdmissionQueue, ticket: Ticket, *, free: Callable[[], Optional[bool]],
                        max_wait: float, clock: Callable[[], float], sleep: Callable[[float], Any],
                        on_wait: Optional[Callable[[int], Any]] = None, poll: float = 1.0,
                        blocked: Blocked = None) -> bool:
    """Tunggu sampai `ticket` terdepan dan `free()` benar. False bila melewati `max_wait` atau tiket
    dibatalkan (`ticket.cancelled`). `free()` → None berarti antrean tidak bisa mengukur beban → loloskan."""
    told = False
    while True:
        if ticket.cancelled:
            return False
        state = free() if queue.is_head(ticket, blocked) else False
        if state is None or state:
            return True
        waited = clock() - ticket.t0
        if waited >= max_wait:
            return False
        if not told and on_wait is not None:
            told = True
            result = on_wait(queue.position(ticket, blocked))
            if hasattr(result, "__await__"):
                await result
        await sleep(min(poll, max(0.05, max_wait - waited)))
