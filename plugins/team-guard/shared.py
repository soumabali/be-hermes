"""State bersama satu proses.

Hermes memuat plugin direktori SEKALI PER PROFILE, masing-masing dengan salinan modul sendiri
(`hermes_plugins.team_guard`, `hermes_plugins.team_guard__home_<digest>`; lihat
hermes_cli/plugins_loader.py::_directory_module_name). Variabel global modul jadi TIDAK terbagi
antar profile. Bagian yang harus tunggal per proses (listener Discord, antrean, rate limit, status
"ditahan") disimpan di modul sintetis di ``sys.modules`` supaya semua salinan melihat objek yang sama.
"""
from __future__ import annotations

import logging
import sys
import threading
import types
from typing import Any, Callable

VERSION = "3.1.0"
_NAME = "_team_guard_shared"

log = logging.getLogger("team-guard")


def _mod() -> types.ModuleType:
    mod = sys.modules.get(_NAME)
    if mod is None:
        new = types.ModuleType(_NAME)
        new.lock = threading.RLock()          # type: ignore[attr-defined]
        new.slots = {}                        # type: ignore[attr-defined]
        new.logged = set()                    # type: ignore[attr-defined]
        mod = sys.modules.setdefault(_NAME, new)
    return mod


def slot(key: str, factory: Callable[[], Any]) -> Any:
    """Objek tunggal per proses untuk `key` (dibuat sekali oleh salinan plugin yang pertama tiba)."""
    mod = _mod()
    with mod.lock:
        if key not in mod.slots:
            mod.slots[key] = factory()
        return mod.slots[key]


def peek(key: str) -> Any:
    return _mod().slots.get(key)


def reset() -> None:
    """Hanya untuk uji: buang semua state bersama."""
    mod = _mod()
    with mod.lock:
        mod.slots.clear()
        mod.logged.clear()


def log_once(key: str, message: str, *args: Any, level: int = logging.WARNING) -> bool:
    """Satu baris log per `key` per proses (supaya internal Hermes yang berubah tidak membanjiri log)."""
    mod = _mod()
    with mod.lock:
        if key in mod.logged:
            return False
        mod.logged.add(key)
    log.log(level, "team-guard: " + message, *args)
    return True
