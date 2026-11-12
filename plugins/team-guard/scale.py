"""Skala lanjutan: pelacak panggilan LLM, pembatas adaptif (AIMD), metrik, dan peringatan admin.

Murni Python; semua jam disuntikkan supaya bisa diuji tanpa menunggu. Tidak ada yang di sini boleh
menjatuhkan gateway: tiap jalur yang menyentuh berkas atau Hermes dibungkus dan gagal dengan satu baris log.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .shared import log, log_once

REFUSAL_KINDS = ("rate", "queue_full", "budget", "user_concurrent")
RATE_LIMIT_REASONS = ("rate_limit", "upstream_rate_limit")       # agent/error_classifier.py::FailoverReason

MSG_THROTTLED = ("🐢 Endpoint LLM mengembalikan {hits} kali 429 dalam {window} detik. Batas sesi anggota "
                 "diturunkan sementara dari {configured} ke {cap}; dinaikkan lagi otomatis setelah tenang.")
MSG_RECOVERED = "✅ Endpoint LLM sudah tenang: batas sesi anggota kembali ke {configured}."
MSG_QUEUE_LONG = ("⚠️ Antrean Hermes berisi {n} pesan selama lebih dari {minutes} menit. Pertimbangkan menaikkan "
                  "`scale.max_concurrent_sessions` atau periksa endpoint LLM.")
MSG_QUEUE_FULL_ALERT = ("⚠️ Ada pesan anggota yang ditolak karena antrean penuh (menunggu melewati "
                        "`queue_max_wait`). Total penolakan sejak gateway start: {n}.")


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------------- panggilan LLM
class LlmTracker:
    """Panggilan LLM yang sedang berjalan, dari hook pengamat `pre_api_request` (mulai) dan
    `post_api_request` / `api_request_error` (selesai). Mencakup cron, `/goal`, dan subagent, yang tidak
    lewat `pre_gateway_dispatch`.

    Panggilan yang tidak pernah "selesai" (giliran diinterupsi, hook tidak terpanggil) dibuang setelah
    `ttl` detik atau saat sesinya berakhir, supaya hitungan tidak bocor dan menutup gerbang selamanya."""

    def __init__(self, ttl: float = 600.0, peak_window: float = 300.0):
        self.ttl, self.peak_window = ttl, peak_window
        self._lock = threading.Lock()
        self._calls: dict[str, tuple[str, float]] = {}        # id request → (session_id, mulai)
        self._samples: deque[tuple[float, int]] = deque(maxlen=4096)
        self.started = self.finished = 0
        self._anon = 0

    def _expire(self, now: float) -> None:
        for key in [k for k, (_s, t) in self._calls.items() if now - t > self.ttl]:
            del self._calls[key]

    def start(self, request_id: Any, session_id: Any, now: float) -> None:
        with self._lock:
            self._expire(now)
            key = str(request_id or "")
            if not key:
                self._anon += 1
                key = f"anon:{session_id}:{self._anon}"
            self._calls[key] = (str(session_id or ""), now)
            self.started += 1
            self._samples.append((now, len(self._calls)))

    def finish(self, request_id: Any, session_id: Any = None) -> None:
        with self._lock:
            self.finished += 1
            key = str(request_id or "")
            if key and self._calls.pop(key, None) is not None:
                return
            # ID tidak dikenal / kosong: lepaskan panggilan tertua sesi itu supaya hitungan tidak bocor.
            sid = str(session_id or "")
            oldest = min((k for k, (s, _t) in self._calls.items() if s == sid and (not key or k.startswith("anon:"))),
                         key=lambda k: self._calls[k][1], default=None)
            if oldest is not None:
                del self._calls[oldest]

    def end_session(self, session_id: Any) -> None:
        sid = str(session_id or "")
        if not sid:
            return
        with self._lock:
            for key in [k for k, (s, _t) in self._calls.items() if s == sid]:
                del self._calls[key]

    def inflight(self, now: float) -> int:
        with self._lock:
            self._expire(now)
            return len(self._calls)

    def peak(self, now: float) -> int:
        """Puncak panggilan paralel dalam `peak_window` detik terakhir."""
        with self._lock:
            self._expire(now)
            while self._samples and now - self._samples[0][0] > self.peak_window:
                self._samples.popleft()
            return max([len(self._calls)] + [n for _t, n in self._samples])


def is_rate_limit_error(status_code: Any = None, reason: Any = None, error: Any = None) -> bool:
    """429 atau jenis galat rate limit pada payload `api_request_error`."""
    try:
        if int(status_code) == 429:
            return True
    except (TypeError, ValueError):
        pass
    if str(getattr(reason, "value", reason) or "").lower() in RATE_LIMIT_REASONS:
        return True
    kind = error.get("type") if isinstance(error, dict) else getattr(error, "type", None)
    kind = str(kind or "").lower().replace("_", "")
    return "ratelimit" in kind or "toomanyrequests" in kind


# ----------------------------------------------------------------------------- pembatas adaptif
class AdaptiveThrottle:
    """AIMD atas batas sesi efektif. `threshold` kali 429 dalam `window` detik → batas dibagi dua
    (paling rendah 1). Setelah `quiet` detik tanpa 429, batas naik 1 tiap `step_every` detik sampai kembali
    ke nilai konfigurasi. Kejadian ("throttled" sekali per episode, "recovered" saat pulih) dikumpulkan di
    `drain()` untuk dikabarkan ke admin."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, *, threshold: int = 3, window: float = 60.0,
                 quiet: float = 120.0, step_every: float = 60.0):
        self.clock = clock
        self.threshold, self.window, self.quiet, self.step_every = threshold, window, quiet, step_every
        self._lock = threading.Lock()
        self._hits: deque[float] = deque(maxlen=1024)
        self._cap: Optional[int] = None                       # None = tidak sedang diturunkan
        self._last_hit = self._last_change = -1e18
        self._events: list[dict] = []

    def record_429(self) -> None:
        now = self.clock()
        with self._lock:
            self._hits.append(now)
            self._last_hit = now

    def effective(self, configured: int, enabled: bool = True) -> int:
        """Batas sesi yang berlaku sekarang (≤ `configured`). Aman dipanggil sesering apa pun."""
        configured = int(configured)
        now = self.clock()
        with self._lock:
            if not enabled or configured <= 0:
                self._cap = None
                self._hits.clear()
                return configured
            while self._hits and now - self._hits[0] > self.window:
                self._hits.popleft()
            if self._cap is not None and self._cap >= configured:
                self._cap = None                              # konfigurasi diturunkan di bawah batas sementara
            if len(self._hits) >= self.threshold:
                hits = len(self._hits)
                self._hits.clear()                            # penurunan berikutnya butuh `threshold` 429 baru
                first = self._cap is None
                self._cap = max(1, (configured if first else self._cap) // 2)
                self._last_change = now
                if first and self._cap < configured:
                    self._event("throttled", configured, hits)
                elif first:
                    self._cap = None                          # configured = 1: tidak ada yang bisa diturunkan
            elif (self._cap is not None and now - self._last_hit >= self.quiet
                  and now - self._last_change >= self.step_every):
                self._cap += 1
                self._last_change = now
                if self._cap >= configured:
                    self._cap = None
                    self._event("recovered", configured, 0)
            return configured if self._cap is None else self._cap

    def _event(self, kind: str, configured: int, hits: int) -> None:
        self._events.append({"kind": kind, "configured": configured, "hits": hits,
                             "cap": configured if self._cap is None else self._cap})
        del self._events[:-10]

    @property
    def throttled(self) -> bool:
        return self._cap is not None

    def drain(self) -> list[dict]:
        with self._lock:
            events, self._events = self._events, []
        return events


# ----------------------------------------------------------------------------- metrik
class Metrics:
    """Penghitung kumulatif sejak gateway start + sampel lama tunggu antrean dan 429."""

    def __init__(self, wait_window: float = 300.0):
        self.wait_window = wait_window
        self._lock = threading.Lock()
        self.refused: dict[str, int] = {k: 0 for k in REFUSAL_KINDS}
        self._waits: deque[tuple[float, float]] = deque(maxlen=2000)
        self._hits: deque[float] = deque(maxlen=4096)

    def refuse(self, kind: str) -> None:
        with self._lock:
            self.refused[kind] = self.refused.get(kind, 0) + 1

    def observe_wait(self, now: float, seconds: float) -> None:
        with self._lock:
            self._waits.append((now, max(0.0, float(seconds))))

    def hit_429(self, now: float) -> None:
        with self._lock:
            self._hits.append(now)

    def wait_p95(self, now: float) -> float:
        with self._lock:
            while self._waits and now - self._waits[0][0] > self.wait_window:
                self._waits.popleft()
            values = sorted(w for _t, w in self._waits)
        if not values:
            return 0.0
        return round(values[min(len(values) - 1, int(0.95 * len(values)))], 3)

    def hits_last_minute(self, now: float) -> int:
        with self._lock:
            return sum(1 for t in self._hits if 0 <= now - t < 60.0)


class AlertRules:
    """Peringatan sekali per kejadian: antrean > `queue_len` selama `queue_for` detik, dan penolakan
    antrean-penuh pertama setelah `quiet_for` detik tanpa penolakan."""

    def __init__(self, clock: Callable[[], float], *, queue_len: int = 5, queue_for: float = 120.0,
                 quiet_for: float = 600.0):
        self.clock, self.queue_len, self.queue_for, self.quiet_for = clock, queue_len, queue_for, quiet_for
        self._high_since: Optional[float] = None
        self._high_told = False
        self._full_seen = 0
        self._full_last: Optional[float] = None

    def observe(self, queue_len: int, queue_full_total: int) -> list[str]:
        now, out = self.clock(), []
        if queue_len > self.queue_len:
            if self._high_since is None:
                self._high_since = now
            if not self._high_told and now - self._high_since >= self.queue_for:
                self._high_told = True
                out.append(MSG_QUEUE_LONG.format(n=queue_len, minutes=int(self.queue_for // 60)))
        else:
            self._high_since, self._high_told = None, False
        if queue_full_total > self._full_seen:
            if self._full_last is None or now - self._full_last >= self.quiet_for:
                out.append(MSG_QUEUE_FULL_ALERT.format(n=queue_full_total))
            self._full_seen, self._full_last = queue_full_total, now
        return out


def write_json_atomic(path: Path, data: dict) -> bool:
    """Tulis JSON lewat berkas sementara + os.replace, mode 0600."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log_once(f"write-{path.name}", "%s tidak bisa ditulis (%s: %s)", path.name, type(exc).__name__, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def configured_max_active(settings: dict) -> int:
    return max(0, as_int(settings.get("max_active")))


def effective_max_active(rt: Any, settings: dict) -> int:
    return rt.throttle.effective(configured_max_active(settings), bool(settings.get("adaptive_throttle")))


def build_metrics(rt: Any, gateway: Any, probe: Any = None, wall: Callable[[], float] = time.time) -> dict:
    """Isi metrics.json (lihat kontrak di docs/RENCANA-SKALA.md #7)."""
    now = rt.clock()
    roster = rt.roster()
    settings = roster.settings
    active: Optional[int] = None
    if probe is not None:
        try:
            active = probe(gateway, now)
        except Exception:
            active = None
    if active is None:
        active = rt.turns.active(now)
    configured = configured_max_active(settings)
    effective = effective_max_active(rt, settings)
    return {
        "ts": int(wall()),
        "active_sessions": int(active),
        "active_by_user": rt.turns.by_user(now),
        "llm_inflight": rt.llm.inflight(now),
        "llm_inflight_peak_5m": rt.llm.peak(now),
        "queue_len": len(rt.queue),
        "queue_wait_p95_s": rt.metrics.wait_p95(now),
        "refused": {k: int(rt.metrics.refused.get(k, 0)) for k in REFUSAL_KINDS},
        "http_429_last_minute": rt.metrics.hits_last_minute(now),
        "effective_max_active": effective,
        "configured_max_active": configured,
        "throttled": bool(rt.throttle.throttled),
        "budget": rt.budget.snapshot(roster),
    }


class Monitor:
    """Tugas latar di profile host: menulis `<state_dir>/metrics.json` tiap `every` detik dan mengirim
    peringatan (pembatas adaptif, antrean panjang, antrean penuh) ke #hermes-admin sekali per kejadian."""

    def __init__(self, rt: Any, *, get_gateway: Callable[[], Any], notify: Callable[[str], Awaitable[None]],
                 probe: Any = None, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] = asyncio.sleep, every: float = 30.0, poll: float = 5.0):
        self.rt, self.get_gateway, self.notify, self.probe = rt, get_gateway, notify, probe
        self.wall, self.sleep, self.every, self.poll = wall, sleep, every, poll
        self.alerts = AlertRules(lambda: rt.clock())
        self.path = Path(rt.state_dir) / "metrics.json"
        self._written: Optional[float] = None

    async def tick(self, force: bool = False) -> Optional[dict]:
        """Satu putaran: kejadian pembatas → admin, aturan peringatan, lalu metrik bila sudah waktunya."""
        rt = self.rt
        settings = rt.roster().settings
        effective_max_active(rt, settings)                    # majukan AIMD walau tidak ada pesan masuk
        texts = []
        for event in rt.throttle.drain():
            if event["kind"] == "throttled":
                texts.append(MSG_THROTTLED.format(hits=event["hits"], window=int(rt.throttle.window),
                                                  configured=event["configured"], cap=event["cap"]))
            else:
                texts.append(MSG_RECOVERED.format(configured=event["configured"]))
        texts += self.alerts.observe(len(rt.queue), int(rt.metrics.refused.get("queue_full", 0)))
        for text in texts:
            try:
                await self.notify(text)
            except Exception as exc:
                log_once("monitor-notify", "peringatan ke admin gagal (%s: %s)", type(exc).__name__, exc)
        now = rt.clock()
        if not force and self._written is not None and now - self._written < self.every:
            return None
        self._written = now
        data = build_metrics(rt, self.get_gateway(), self.probe, self.wall)
        write_json_atomic(self.path, data)
        return data

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("team-guard: pemantau metrik gagal sekali (%s: %s)", type(exc).__name__, exc)
            await self.sleep(self.poll)
