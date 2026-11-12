"""Penyimpanan status plugin: <state_dir>/onboarding.json (tulis atomik, mode 0600).

Dibaca juga oleh tool admin (teamctl: "status onboarding") — kunci `held` dan `live` adalah kontrak.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


def now_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def file_lock(path: Path):
    """Kunci antar proses (flock) pada file pendamping; tanpa fcntl hanya menjadi no-op."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class StateStore:
    """held (onboarding ditahan), live (status live reload), welcomed, failed, pending, notified."""

    SECTIONS = ("held", "live", "welcomed", "failed", "pending", "notified")

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {}
        self.baseline_needed = False
        self.load()

    # ------------------------------------------------------------------ berkas
    def load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            self.baseline_needed = not (isinstance(raw, dict) and isinstance(raw.get("welcomed"), dict))
            self.data = raw if isinstance(raw, dict) else {}
            for key in self.SECTIONS:
                if not isinstance(self.data.get(key), dict):
                    self.data[key] = {}
            self.data.setdefault("version", 1)

    def save(self) -> None:
        with self._lock:
            self.data["updated"] = now_iso(self.clock())
            text = json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
            try:
                atomic_write(self.path, text, 0o600)
            except OSError:
                # State tetap benar di memori; kegagalan disk tidak boleh menjatuhkan gateway.
                from .shared import log_once
                log_once("state-write", "onboarding.json tidak bisa ditulis (%s)", self.path)

    # ------------------------------------------------------------------ ditahan
    def hold(self, user_id: Any, reason: str, **extra: Any) -> bool:
        """Tandai ditahan. True bila baru (atau alasannya berubah) → admin perlu dikabari."""
        uid = str(user_id)
        with self._lock:
            old = self.data["held"].get(uid)
            fresh = not old or old.get("reason") != reason
            self.data["held"][uid] = {"reason": reason, "ts": now_iso(self.clock()),
                                      **{k: v for k, v in extra.items() if v is not None}}
            self.save()
            return fresh

    def unhold(self, user_id: Any) -> bool:
        with self._lock:
            if self.data["held"].pop(str(user_id), None) is None:
                return False
            self.save()
            return True

    def is_held(self, user_id: Any) -> bool:
        return str(user_id) in self.data["held"]

    # ------------------------------------------------------------------ live reload
    def set_live(self, ok: bool, detail: str, **extra: Any) -> None:
        with self._lock:
            keep = {k: v for k, v in self.data["live"].items() if k in ("auto_restart_ts",)}
            self.data["live"] = {**keep, "ok": bool(ok), "detail": str(detail), "ts": now_iso(self.clock()), **extra}
            self.save()

    def live(self) -> dict:
        return dict(self.data["live"])

    def mark_auto_restart(self) -> None:
        with self._lock:
            self.data["live"]["auto_restart_ts"] = float(self.clock())
            self.save()

    def seconds_since_auto_restart(self) -> Optional[float]:
        ts = self.data["live"].get("auto_restart_ts")
        try:
            return max(0.0, float(self.clock()) - float(ts)) if ts is not None else None
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ sambutan
    def baseline(self, users: dict) -> None:
        """Pemasangan pertama: anggota yang sudah ada dianggap sudah disambut (tidak dikirimi ulang)."""
        with self._lock:
            for uid, info in users.items():
                self.data["welcomed"].setdefault(str(uid), {
                    "channel_id": str(info.get("channel_id") or ""), "ts": now_iso(self.clock()),
                    "active": True, "baseline": True})
            self.baseline_needed = False
            self.save()

    def mark_welcomed(self, user_id: Any, channel_id: Any) -> None:
        with self._lock:
            self.data["welcomed"][str(user_id)] = {"channel_id": str(channel_id or ""),
                                                   "ts": now_iso(self.clock()), "active": True}
            self.data["failed"].pop(str(user_id), None)
            self.save()

    def was_welcomed(self, user_id: Any) -> bool:
        entry = self.data["welcomed"].get(str(user_id))
        return bool(entry and entry.get("active", True))

    def known_before(self, user_id: Any) -> bool:
        return str(user_id) in self.data["welcomed"]

    def mark_inactive(self, user_id: Any) -> bool:
        with self._lock:
            entry = self.data["welcomed"].get(str(user_id))
            if not entry or not entry.get("active", True):
                return False
            entry["active"] = False
            self.save()
            return True

    # ------------------------------------------------------------------ lain-lain
    def set_failed(self, user_id: Any, reason: str) -> None:
        with self._lock:
            self.data["failed"][str(user_id)] = {"reason": reason, "ts": now_iso(self.clock())}
            self.save()

    def has_failed(self, user_id: Any) -> bool:
        return str(user_id) in self.data["failed"]

    def set_pending(self, user_id: Any, channel_id: Any, message_id: Any) -> None:
        """Pesan "⏳ sedang disiapkan" yang masih harus dihapus (bertahan lintas restart)."""
        with self._lock:
            self.data["pending"][str(user_id)] = {"channel_id": str(channel_id), "message_id": str(message_id)}
            self.save()

    def pop_pending(self, user_id: Any) -> Optional[dict]:
        with self._lock:
            entry = self.data["pending"].pop(str(user_id), None)
            if entry is not None:
                self.save()
            return entry

    def notify_once(self, key: str) -> bool:
        """True hanya pertama kali untuk `key` (kabar admin yang tidak boleh berulang tiap susulan)."""
        with self._lock:
            if key in self.data["notified"]:
                return False
            self.data["notified"][key] = now_iso(self.clock())
            self.save()
            return True

    def clear_notified(self, prefix: str) -> None:
        with self._lock:
            gone = [k for k in self.data["notified"] if k.startswith(prefix)]
            for key in gone:
                del self.data["notified"][key]
            if gone:
                self.save()
