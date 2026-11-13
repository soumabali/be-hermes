"""Runtime bersama per proses (satu per state_dir): roster, state, rate limit, antrean, pelacak LLM,
pembatas adaptif, metrik, anggaran token, jembatan host."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from . import shared
from .budget import Budget
from .limiters import default_limiters
from .ratelimit import AdmissionQueue, RateLimiter, TurnTracker
from .roster import MAX_SUPPORTED_ROSTER_SCHEMA, Roster, RosterCache
from .scale import AdaptiveThrottle, LlmTracker, Metrics
from .state import StateStore, now_iso
from .upstream import HermesGateway

MSG_ROSTER_SCHEMA = ("⚠️ roster.json ditulis kit dengan skema {schema}, sedangkan plugin team-guard ini hanya "
                     "memahami sampai skema {supported}. Plugin tetap berjalan sebisanya (kunci yang tidak dikenal "
                     "diabaikan), tetapi fitur baru kit mungkin belum ditegakkan. Perbarui plugin bersama kit: "
                     "`hermes_team.py apply` lalu restart gateway.")


class Runtime:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.roster_cache = RosterCache(self.state_dir / "roster.json")
        self.state = StateStore(self.state_dir / "onboarding.json")
        self.upstream: Any = HermesGateway()      # satu-satunya jalan ke internal Hermes (upstream.py)
        self.limiter = RateLimiter()              # bucket rate limit per orang (dipakai RateLimitLimiter)
        self.limiters: list = default_limiters()  # pembatas pesan masuk, berurutan (limiters.py)
        self.turns = TurnTracker()
        self.queue = AdmissionQueue()
        self.clock: Any = time.monotonic         # jam untuk hook & pembatas (diganti jam tiruan di uji)
        self.llm = LlmTracker()                  # panggilan LLM yang sedang berjalan (semua profile)
        self.throttle = AdaptiveThrottle(lambda: self.clock())
        self.metrics = Metrics()
        self.budget = Budget(self.state_dir)
        self.wall: Any = time.time               # jam dinding (kabar harian; diganti jam tiruan di uji)
        self.started = time.monotonic()          # dipakai aturan "jangan restart berulang"
        self.host: Any = None                    # HostBridge, diisi saat adapter Discord tersambung
        self.detached: Any = None                # (native, adapter, loop) setelah plugin host di-unload
        self.dispatcher: Any = None
        self.profiles: dict[str, str] = {}       # profile → jenis (host/member/…) yang sudah memuat plugin

    def roster(self, force: bool = False) -> Roster:
        return self.roster_cache.get(force)

    def schema_notice(self, roster: Roster) -> str:
        """Teks kabar admin bila roster bersekema lebih baru dari yang dipahami plugin — dan hanya SEKALI per
        hari (tahan restart: dicatat di onboarding.json). "" bila tidak perlu."""
        if not getattr(roster, "newer_schema", False):
            return ""
        prefix = "roster-schema:"
        key = f"{prefix}{roster.schema}:{now_iso(self.wall())[:10]}"
        if key in self.state.data.get("notified", {}):
            return ""
        self.state.clear_notified(prefix)
        if not self.state.notify_once(key):
            return ""
        return MSG_ROSTER_SCHEMA.format(schema=roster.schema, supported=MAX_SUPPORTED_ROSTER_SCHEMA)


def runtime_for(state_dir: Any) -> Runtime:
    path = Path(str(state_dir)).expanduser()
    return shared.slot(f"runtime:{path}", lambda: Runtime(path))


def existing_runtime(state_dir: Any) -> Optional[Runtime]:
    return shared.peek(f"runtime:{Path(str(state_dir)).expanduser()}")
