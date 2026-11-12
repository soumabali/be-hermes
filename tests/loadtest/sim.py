"""Simulasi kejadian-diskret: 20 pengguna terhadap kelas plugin yang SEBENARNYA.

Yang asli : `Dispatcher` (hook pre_gateway_dispatch), `TurnTracker`, `AdmissionQueue`, `RateLimiter`,
            `LlmTracker`, `AdaptiveThrottle`, `Budget`, `Monitor`, dan `ProfileHooks` (lewat `register()`).
Yang tiruan: jam (`SimClock`), gateway (`FakeRunner`), provider LLM (`Provider`: latensi, 429 di atas N
            paralel), kolektor pemakaian (menulis buku besar sqlite saat plugin "menjalankan" kolektor),
            dan para pengirim (anggota biasa yang kadang beruntun, satu "pembanjir" yang membuka 6 thread
            sekaligus, satu "boros" token, cron di team-shared, dan admin).

Tidak ada `sleep` sungguhan dan tidak ada sumber acak selain `random.Random(seed)`, jadi hasilnya sama
persis di tiap eksekusi. Ini BUKAN uji terhadap Hermes sungguhan: urutan hook meniru dokumentasi upstream
(pre_llm_call → pre/post_api_request | api_request_error → on_session_end).
"""
from __future__ import annotations

import asyncio
import heapq
import json
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import plugin_stubs as S

WALL0 = 1_800_000_000.0          # 2027-01-15 08:00 UTC — jauh dari tengah malam, hari tidak berganti
DAY = "2027-01-15"


class SimClock:
    """Jam virtual: `sleep` mendaftar di heap; `run` melompat dari kejadian ke kejadian."""

    def __init__(self) -> None:
        self.now = 0.0
        self._heap: list[tuple[float, int, asyncio.Future]] = []
        self._seq = 0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._heap, (self.now + max(0.0, float(seconds)), self._seq, future))
        await future

    @staticmethod
    async def settle(rounds: int = 12) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)

    async def run(self, until: float) -> None:
        while True:
            await self.settle()
            if not self._heap or self._heap[0][0] > until:
                break
            self.now = self._heap[0][0]
            while self._heap and self._heap[0][0] == self.now:
                _t, _n, future = heapq.heappop(self._heap)
                if not future.done():
                    future.set_result(None)
        self.now = max(self.now, until)


@dataclass
class Config:
    seed: int = 7
    users: int = 20
    duration: float = 2700.0
    # setelan tim (roster.settings)
    max_active: int = 8
    user_max_concurrent: int = 2
    max_llm_parallel: int = 9
    queue_max_wait: int = 180
    daily_token_budget: int = 1_500_000
    adaptive_throttle: bool = True
    # provider tiruan
    provider_parallel: int = 11                  # 429 di atas ini (= max_active + cron_parallel)
    degraded: tuple = (900.0, 1020.0, 2)         # (mulai, selesai, paralel selama gangguan)
    retries: int = 12
    # beban
    cron_every: float = 60.0
    cron_parallel: int = 3
    flood_every: float = 240.0
    think: tuple = (60, 240)                     # jeda antar kiriman anggota biasa (detik, acak merata)
    flood_threads: int = 6
    tokens_per_call: int = 2_000
    hog_tokens_per_call: int = 100_000


@dataclass
class Message:
    uid: str
    role: str
    thread: str
    sent: float
    admitted: Optional[float] = None
    reason: str = ""
    followup: bool = False

    @property
    def wait(self) -> float:
        return (self.admitted if self.admitted is not None else self.sent) - self.sent


@dataclass
class Result:
    cfg: Config
    messages: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    admin_notices: list = field(default_factory=list)
    cap_samples: list = field(default_factory=list)          # (waktu, batas efektif)
    peak_user: dict = field(default_factory=dict)            # uid → sesi bersamaan terbanyak
    peak_sessions: int = 0
    peak_llm: int = 0
    http_429: list = field(default_factory=list)             # waktu tiap 429
    failed_calls: int = 0
    collects: list = field(default_factory=list)             # waktu kolektor "dijalankan"
    hog_crossed: Optional[float] = None
    metrics: dict = field(default_factory=dict)
    final: dict = field(default_factory=dict)
    uids: dict = field(default_factory=dict)                 # peran → uid
    adapter_sent: list = field(default_factory=list)         # semua kiriman bot ke Discord tiruan

    def of(self, role: str) -> list:
        return [m for m in self.messages if m.role == role]


class Provider:
    """Endpoint LLM tiruan: paling banyak `limit` request paralel; lebihnya dijawab 429."""

    def __init__(self, sim: "Simulation"):
        self.sim, self.inflight, self.open, self.n = sim, 0, 0, 0

    def limit(self) -> int:
        start, end, parallel = self.sim.cfg.degraded
        return parallel if start <= self.sim.clock.now < end else self.sim.cfg.provider_parallel

    async def call(self, ctx: Any, session_id: str, platform: str, uid: str, tokens: int, latency: float) -> bool:
        sim, clock = self.sim, self.sim.clock
        for attempt in range(sim.cfg.retries + 1):
            self.n += 1
            rid = f"req-{self.n}"
            self.open += 1
            ctx.fire("pre_api_request", task_id=session_id, turn_id=session_id, api_request_id=rid,
                     session_id=session_id, platform=platform, model="td-simple", provider="custom:td",
                     api_call_count=attempt + 1, retry_count=attempt, message_count=4,
                     request={"method": "POST", "body": {}})
            if self.inflight >= self.limit():
                await clock.sleep(0.5)
                self.open -= 1
                sim.result.http_429.append(clock.now)
                ctx.fire("api_request_error", task_id=session_id, turn_id=session_id, api_request_id=rid,
                         session_id=session_id, platform=platform, model="td-simple", provider="custom:td",
                         status_code=429, retry_count=attempt, max_retries=sim.cfg.retries, retryable=True,
                         reason="rate_limit", error={"type": "RateLimitError", "message": "Too Many Requests"},
                         request={"method": "POST", "body": {}})
                await clock.sleep(min(20.0, 2.0 * (attempt + 1)) + sim.rng.choice((0.0, 0.5, 1.0)))
                continue
            self.inflight += 1
            sim.result.peak_llm = max(sim.result.peak_llm, self.inflight)
            await clock.sleep(latency)
            self.inflight -= 1
            self.open -= 1
            ctx.fire("post_api_request", task_id=session_id, turn_id=session_id, api_request_id=rid,
                     session_id=session_id, platform=platform, model="td-simple", provider="custom:td",
                     api_duration=latency, usage={"prompt_tokens": tokens // 2, "completion_tokens": tokens // 2})
            sim.record_usage(uid, tokens)
            return True
        sim.result.failed_calls += 1
        return False


class Simulation:
    def __init__(self, env: Any, cfg: Optional[Config] = None):
        self.env, self.cfg = env, cfg or Config()
        self.rng = random.Random(self.cfg.seed)
        self.clock = SimClock()
        self.result = Result(self.cfg)
        self.provider = Provider(self)
        self.plugin = env.plugin
        self.scale = S.sub(self.plugin, "scale")
        self.dispatch = S.sub(self.plugin, "dispatch")
        self._pending_usage: dict[str, int] = {}
        self._true_usage: dict[str, int] = {}
        self._tasks: list[asyncio.Task] = []
        self._n = 0
        self._active_by_user: dict[str, int] = {}
        self._active_members = 0
        self._running_threads: dict[str, list[str]] = {}
        self._stopped = False
        self._setup()

    # ------------------------------------------------------------------ pemasangan
    def _setup(self) -> None:
        env, cfg = self.env, self.cfg
        self.members: list[tuple[str, str, str]] = []                    # (uid, channel, profile)
        data = env.roster
        data["users"].clear()
        for i in range(cfg.users):
            uid, channel, name = f"5100000000000000{i:02d}", f"6100000000000000{i:02d}", f"u{i:02d}"
            S.roster_add(data, uid, name, channel)
            self.members.append((uid, channel, data["users"][uid]["profile"]))
            (env.hermes_root / "profiles" / data["users"][uid]["profile"]).mkdir(parents=True, exist_ok=True)
        self.ledger = env.state_dir / "usage.db"
        data["usage"] = {"timezone": "UTC", "ledger": str(self.ledger)}
        data["settings"].update(
            max_active=cfg.max_active, user_max_concurrent=cfg.user_max_concurrent,
            max_llm_parallel=cfg.max_llm_parallel, queue_max_wait=cfg.queue_max_wait,
            daily_token_budget=cfg.daily_token_budget, adaptive_throttle=cfg.adaptive_throttle,
            rate_per_minute=6, burst=6)
        S.roster_rebuild(data)
        env.save()
        S.ledger_add(self.ledger, DAY, "profile:team-shared", 0, 0)       # buku besar sudah ada (kolektor pernah jalan)

        rt = self.rt = env.rt()
        rt.clock = self.clock
        rt.budget.wall = self.wall
        rt.budget.spawn = self._collector
        rt.dispatcher = self.dispatch.Dispatcher(rt, clock=self.clock, sleep=self.clock.sleep)
        self.ctx = {profile: env.register(profile) for profile in
                    ["default", "team-shared", "team-admin"] + [m[2] for m in self.members]}
        self.hook = self.ctx["default"].hooks["pre_gateway_dispatch"][0]
        self.monitor = self.scale.Monitor(rt, get_gateway=lambda: env.runner, notify=self._notify,
                                          probe=rt.dispatcher.probe, wall=self.wall, sleep=self.clock.sleep)
        self.roles = {"flooder": self.members[0][0], "hog": self.members[1][0]}
        # Satu giliran admin sudah selesai sebelum simulasi mulai: hook giliran terbukti hidup, jadi batas sesi
        # per orang berlaku sejak pesan pertama (lihat Dispatcher.user_load).
        self.ctx["team-admin"].fire("pre_llm_call", session_id="sess-awal", sender_id=S.ADMIN, platform="discord")
        self.ctx["team-admin"].fire("on_session_end", session_id="sess-awal", completed=True, interrupted=False,
                                    model="td-simple", platform="discord")
        self.result.uids = dict(self.roles, admin=S.ADMIN)

    def wall(self) -> float:
        return WALL0 + self.clock.now

    async def _notify(self, text: str) -> None:
        self.result.admin_notices.append((self.clock.now, text))

    def _collector(self, argv: list, environ: dict) -> None:
        """Pengganti subprocess `usage_ledger.py collect`: pemakaian yang tertunda masuk buku besar."""
        assert argv[-1] == "collect" and argv[1].endswith("usage_ledger.py")
        self.result.collects.append(self.clock.now)
        pending, self._pending_usage = self._pending_usage, {}
        con = sqlite3.connect(self.ledger)
        try:
            for key, tokens in pending.items():
                con.execute(
                    "INSERT INTO daily (day, user_key, profile, model, source, requests, input_tokens, output_tokens) "
                    "VALUES (?, ?, 'p', 'td-simple', '', 1, ?, ?) ON CONFLICT(day, user_key, profile, model, source) "
                    "DO UPDATE SET input_tokens = input_tokens + excluded.input_tokens, "
                    "output_tokens = output_tokens + excluded.output_tokens",
                    (DAY, key, tokens // 2, tokens - tokens // 2))
            con.commit()
        finally:
            con.close()

    def record_usage(self, uid: str, tokens: int) -> None:
        self._pending_usage[uid] = self._pending_usage.get(uid, 0) + tokens
        self._true_usage[uid] = self._true_usage.get(uid, 0) + tokens
        if (uid == self.roles["hog"] and self.result.hog_crossed is None
                and self._true_usage[uid] >= self.cfg.daily_token_budget):
            self.result.hog_crossed = self.clock.now

    # ------------------------------------------------------------------ satu pesan
    def spawn(self, coro: Any) -> None:
        self._tasks.append(asyncio.ensure_future(coro))

    async def message(self, uid: str, channel: str, profile: str, role: str, thread: Optional[str] = None) -> None:
        clock, runner, result = self.clock, self.env.runner, self.result
        followup = thread is not None
        if thread is None:
            self._n += 1
            thread = f"7{self._n:017d}"
        source = S.FakeSource(user_id=uid, chat_id=thread, thread_id=thread, parent_chat_id=channel)
        msg = Message(uid, role, thread, clock.now, followup=followup)
        result.messages.append(msg)
        got = await self.hook(event=S.FakeEvent(text="tolong kerjakan", source=source), gateway=runner,
                              session_store=None)
        if isinstance(got, dict) and got.get("action") == "skip":
            msg.reason = str(got.get("reason") or "")
            return
        msg.admitted = clock.now
        key = runner._session_key_for_source(source)
        if key in runner._running_agents:
            return                                       # pesan susulan: masuk antrean sesi Hermes sendiri
        if followup:
            return
        sid = f"sess-{thread}"
        admin = role == "admin"
        settings = self.rt.roster().settings
        if not admin:
            cap = self.scale.effective_max_active(self.rt, settings)
            self._active_by_user[uid] = self._active_by_user.get(uid, 0) + 1
            self._active_members += 1
            result.peak_user[uid] = max(result.peak_user.get(uid, 0), self._active_by_user[uid])
            result.peak_sessions = max(result.peak_sessions, self._active_members)
            if self._active_by_user[uid] > self.cfg.user_max_concurrent:
                result.violations.append(f"t={clock.now:.1f}: {role} {uid} punya {self._active_by_user[uid]} sesi")
            if self._active_members > cap:
                result.violations.append(f"t={clock.now:.1f}: {self._active_members} sesi anggota > batas {cap}")
            if self.provider.open >= self.cfg.max_llm_parallel:
                result.violations.append(f"t={clock.now:.1f}: diloloskan saat {self.provider.open} panggilan LLM")
        runner._running_agents[key] = sid
        self._running_threads.setdefault(uid, []).append(thread)
        ctx = self.ctx[profile]
        ctx.fire("pre_llm_call", session_id=sid, task_id=sid, turn_id=sid, user_message="tolong kerjakan",
                 conversation_history=[], is_first_turn=True, model="td-simple", platform="discord",
                 sender_id=uid)
        try:
            tokens = self.cfg.hog_tokens_per_call if role == "hog" else self.cfg.tokens_per_call
            for _ in range(self.rng.randint(2, 5)):
                await self.provider.call(ctx, sid, "discord", uid, tokens, float(self.rng.randint(2, 5)))
                await clock.sleep(float(self.rng.randint(1, 3)))              # eksekusi tool
        finally:
            ctx.fire("on_session_end", session_id=sid, completed=True, interrupted=False, model="td-simple",
                     platform="discord")
            del runner._running_agents[key]
            self._running_threads[uid].remove(thread)
            if not admin:
                self._active_by_user[uid] -= 1
                self._active_members -= 1

    # ------------------------------------------------------------------ pengirim
    async def member(self, index: int) -> None:
        uid, channel, profile = self.members[index]
        role = {0: "flooder", 1: "hog"}.get(index, "normal")
        rng, clock = self.rng, self.clock
        await clock.sleep(float(rng.randint(1, 60)))
        while not self._stopped:
            running = self._running_threads.get(uid) or []
            if running and rng.random() < 0.15:
                self.spawn(self.message(uid, channel, profile, role, thread=running[0]))      # susulan
            else:
                for _ in range(rng.choice((1, 1, 1, 2, 3))):                                  # kadang beruntun
                    self.spawn(self.message(uid, channel, profile, role))
                    await clock.sleep(1.0)
            await clock.sleep(float(rng.randint(*self.cfg.think)))

    async def flooder(self) -> None:
        uid, channel, profile = self.members[0]
        await self.clock.sleep(45.0)
        while not self._stopped:
            for _ in range(self.cfg.flood_threads):                       # 6 thread pada detik yang sama
                self.spawn(self.message(uid, channel, profile, "flooder"))
            await self.clock.sleep(self.cfg.flood_every)

    async def admin(self) -> None:
        await self.clock.sleep(1.0)                                       # giliran pertama: hook terbukti hidup
        while not self._stopped:
            self.spawn(self.message(S.ADMIN, S.CH_ADMIN, "team-admin", "admin"))
            await self.clock.sleep(200.0)

    async def cron(self) -> None:
        """Cron team-shared: panggilan LLM yang TIDAK lewat pre_gateway_dispatch."""
        ctx, n = self.ctx["team-shared"], 0
        await self.clock.sleep(20.0)
        while not self._stopped:
            for _ in range(self.cfg.cron_parallel):
                n += 1
                self.spawn(self.provider.call(ctx, f"cron_{n}", "cron", "profile:team-shared",
                                              self.cfg.tokens_per_call, float(self.rng.randint(3, 6))))
            await self.clock.sleep(self.cfg.cron_every)

    async def watcher(self) -> None:
        """Pemantau host (Monitor asli) + pemeriksaan silang pelacak plugin terhadap keadaan sebenarnya."""
        result, rt = self.result, self.rt
        while True:
            await self.monitor.tick()
            now = self.clock.now
            settings = rt.roster().settings
            result.cap_samples.append((now, self.scale.effective_max_active(rt, settings)))
            if rt.llm.inflight(now) != self.provider.open:
                result.violations.append(f"t={now:.1f}: pelacak LLM {rt.llm.inflight(now)} ≠ {self.provider.open}")
            if rt.turns.active(now) != self._active_members:
                result.violations.append(f"t={now:.1f}: pelacak sesi {rt.turns.active(now)} ≠ {self._active_members}")
            if self._active_members > self.cfg.max_active:
                result.violations.append(f"t={now:.1f}: {self._active_members} sesi > max_active")
            await self.clock.sleep(5.0)

    # ------------------------------------------------------------------ jalankan
    async def run(self) -> Result:
        sources = [asyncio.ensure_future(c) for c in
                   [self.admin(), self.cron(), self.flooder()] + [self.member(i) for i in range(1, self.cfg.users)]]
        watcher = asyncio.ensure_future(self.watcher())
        await self.clock.run(self.cfg.duration)
        self._stopped = True                                              # tidak ada pesan baru; habiskan sisanya
        for task in sources:
            task.cancel()
        await self.clock.run(self.cfg.duration + 900.0)
        watcher.cancel()
        await asyncio.gather(*sources, watcher, return_exceptions=True)
        pending = [t for t in self._tasks if not t.done()]
        errors = [repr(t.exception()) for t in self._tasks if t.done() and not t.cancelled() and t.exception()]
        self.result.violations += [f"tugas gagal: {e}" for e in errors[:5]]
        now = self.clock.now
        self.result.final = {
            "pending_tasks": len(pending), "queue_len": len(self.rt.queue), "active": self.rt.turns.active(now),
            "llm_inflight": self.rt.llm.inflight(now), "provider_inflight": self.provider.inflight,
            "running_agents": len(self.env.runner._running_agents), "refused": dict(self.rt.metrics.refused),
            "throttled": self.rt.throttle.throttled,
            "effective": self.scale.effective_max_active(self.rt, self.rt.roster().settings),
        }
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        path = Path(self.rt.state_dir) / "metrics.json"
        self.result.adapter_sent = list(self.env.adapter.sent)
        self.result.metrics = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return self.result


def run_simulation(env: Any, cfg: Optional[Config] = None) -> Result:
    async def main() -> Result:
        return await Simulation(env, cfg).run()

    return asyncio.run(main())


def summary(result: Result) -> dict:
    """Ringkasan untuk dicetak saat menyetel skenario (python -m pytest -s)."""
    def waits(role: str) -> list:
        return sorted(m.wait for m in result.of(role) if m.admitted is not None and not m.followup)

    out: dict = {"messages": len(result.messages), "final": result.final, "peak_sessions": result.peak_sessions,
                 "peak_llm": result.peak_llm, "http_429": len(result.http_429), "failed_calls": result.failed_calls,
                 "hog_crossed": result.hog_crossed, "collects": len(result.collects),
                 "caps": sorted({c for _t, c in result.cap_samples}),
                 "notices": [(round(t), text[:40]) for t, text in result.admin_notices],
                 "violations": result.violations[:5]}
    start, end, _n = result.cfg.degraded
    calm = sorted(m.wait for m in result.of("normal") if not m.followup and (m.sent < start - 60 or m.sent > end + 600))
    out["normal_calm"] = {"n": len(calm), "max_wait": calm[-1] if calm else 0,
                          "p95": calm[int(0.95 * (len(calm) - 1))] if calm else 0,
                          "waited": sum(1 for w in calm if w > 0)}
    for role in ("normal", "flooder", "hog", "admin"):
        w = waits(role)
        out[role] = {"sent": len(result.of(role)), "admitted": len(w), "max_wait": w[-1] if w else 0,
                     "p95": w[int(0.95 * (len(w) - 1))] if w else 0,
                     "refused": sorted({m.reason for m in result.of(role) if m.reason})}
    return out
