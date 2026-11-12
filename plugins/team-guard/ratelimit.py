"""Skala: token bucket per orang, pelacak giliran aktif, dan antrean masuk FIFO.

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
    __slots__ = ("user_id", "t0")

    def __init__(self, user_id: str, t0: float):
        self.user_id, self.t0 = user_id, t0


class AdmissionQueue:
    """Antrean FIFO: hanya tiket terdepan yang boleh mengambil slot kosong."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: list[Ticket] = []

    def enter(self, user_id: str, now: float) -> Ticket:
        ticket = Ticket(str(user_id), now)
        with self._lock:
            self._tickets.append(ticket)
        return ticket

    def position(self, ticket: Ticket) -> int:
        with self._lock:
            for i, item in enumerate(self._tickets):
                if item is ticket:
                    return i + 1
        return 0

    def is_head(self, ticket: Ticket) -> bool:
        return self.position(ticket) == 1

    def leave(self, ticket: Ticket) -> None:
        with self._lock:
            self._tickets = [t for t in self._tickets if t is not ticket]

    def __len__(self) -> int:
        with self._lock:
            return len(self._tickets)


async def wait_for_slot(queue: AdmissionQueue, ticket: Ticket, *, free: Callable[[], Optional[bool]],
                        max_wait: float, clock: Callable[[], float], sleep: Callable[[float], Any],
                        on_wait: Optional[Callable[[int], Any]] = None, poll: float = 1.0) -> bool:
    """Tunggu sampai `ticket` terdepan dan `free()` benar. False bila melewati `max_wait`.
    `free()` → None berarti antrean tidak bisa mengukur beban → loloskan."""
    told = False
    while True:
        state = free() if queue.is_head(ticket) else False
        if state is None or state:
            return True
        waited = clock() - ticket.t0
        if waited >= max_wait:
            return False
        if not told and on_wait is not None:
            told = True
            result = on_wait(queue.position(ticket))
            if hasattr(result, "__await__"):
                await result
        await sleep(min(poll, max(0.05, max_wait - waited)))
