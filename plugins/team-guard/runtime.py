"""Runtime bersama per proses (satu per state_dir): roster, state, rate limit, antrean, jembatan host."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from . import shared
from .ratelimit import AdmissionQueue, RateLimiter, TurnTracker
from .roster import Roster, RosterCache
from .state import StateStore


class Runtime:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.roster_cache = RosterCache(self.state_dir / "roster.json")
        self.state = StateStore(self.state_dir / "onboarding.json")
        self.limiter = RateLimiter()
        self.turns = TurnTracker()
        self.queue = AdmissionQueue()
        self.started = time.monotonic()          # dipakai aturan "jangan restart berulang"
        self.host: Any = None                    # HostBridge, diisi saat adapter Discord tersambung
        self.dispatcher: Any = None
        self.profiles: dict[str, str] = {}       # profile → jenis (host/member/…) yang sudah memuat plugin

    def roster(self, force: bool = False) -> Roster:
        return self.roster_cache.get(force)


def runtime_for(state_dir: Any) -> Runtime:
    path = Path(str(state_dir)).expanduser()
    return shared.slot(f"runtime:{path}", lambda: Runtime(path))


def existing_runtime(state_dir: Any) -> Optional[Runtime]:
    return shared.peek(f"runtime:{Path(str(state_dir)).expanduser()}")
