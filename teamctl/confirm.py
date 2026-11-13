"""Konfirmasi admin: Plan, Planner, `_guarded` (pagar → konfirmasi → susun ulang → jalankan → audit),
penulisan audit log, dan pembersih teks untuk ringkasan konfirmasi.

Tool yang mengubah sesuatu TIDAK memanggil modul ini langsung: ia mengembalikan fungsi `plan()` (lihat
registry.py) dan kerangka tool yang menjalankannya lewat `GuardMixin._guarded`.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional, Union

import hermes_team as ht

from .errors import ToolError

# Karakter tak terlihat yang bisa menyamarkan teks (zero-width, pembalik arah tulisan, BOM).
_INVISIBLE = set("​⁠﻿­؜‎‏‪‫‬‭‮⁦⁧⁨⁩")


class Plan:
    """Rencana satu aksi: ringkasan yang dikonfirmasi admin + fungsi yang menjalankannya.

    Disusun dua kali (sebelum konfirmasi dan sesudah disetujui); hanya dijalankan kalau ringkasannya sama.
    """

    def __init__(self, team: dict, preview: str, run: Callable[[], str], **audit: Any):
        self.team, self.preview, self.run, self.audit = team, preview, run, audit


# Fungsi tanpa argumen yang menyusun Plan (atau string bila tidak ada yang perlu dilakukan).
PlanFn = Callable[[], Union[Plan, str]]


class Planner:
    """Rencana + pembersih opsional. Dikembalikan handler tool yang butuh membersihkan sesuatu setelah
    konfirmasi selesai (mis. salinan staging skill); handler biasa cukup mengembalikan fungsi plan()."""

    def __init__(self, plan: PlanFn, cleanup: Optional[Callable[[], None]] = None):
        self.plan, self.cleanup = plan, cleanup


def _clean(value: Any, limit: int = 100) -> str:
    """Satu baris aman untuk ringkasan: tanpa karakter kontrol/format/newline, spasi dirapatkan, dibatasi."""
    out = []
    for ch in str("" if value is None else value):
        cat = unicodedata.category(ch)
        if ch.isspace() or cat in ("Zl", "Zp"):
            out.append(" ")
        elif cat[0] == "C" or ch in _INVISIBLE:
            continue
        else:
            out.append(ch)
    text = re.sub(r" +", " ", "".join(out)).strip()
    return text if len(text) <= limit else text[:max(1, limit - 1)].rstrip() + "…"


def _q(value: Any, limit: int = 100) -> str:
    """Teks dari AI/user, dikutip — tanda kutip di dalamnya diganti supaya kutipan tidak bisa 'ditutup'."""
    return '"' + _clean(value, limit).replace('"', "'") + '"'


def _name(value: Any, limit: int = 64) -> str:
    """Nama (role/user/channel) untuk label; kurung diganti supaya satu-satunya '(ID)' adalah ID asli."""
    return _clean(value, limit).replace("(", "[").replace(")", "]") or "?"


def _clean_message(value: Any) -> str:
    """Isi pesan yang akan dikirim: newline dipertahankan, karakter kontrol & tak terlihat dibuang.

    Hasil fungsi ini yang ditampilkan di konfirmasi DAN yang dikirim (bukan teks mentahnya)."""
    out = []
    for ch in str("" if value is None else value).replace("\r\n", "\n").replace("\r", "\n"):
        cat = unicodedata.category(ch)
        if ch == "\n":
            out.append(ch)
        elif ch == "\t" or cat in ("Zl", "Zp"):
            out.append(" " if ch == "\t" else "\n")
        elif cat in ("Cc", "Cs", "Co") or ch in _INVISIBLE:
            continue
        else:
            out.append(ch)
    lines = [line.rstrip() for line in "".join(out).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class GuardMixin:
    """Audit log + pagar konfirmasi untuk TeamCtl. Butuh `self.team()`, `self.confirm()`, `self.work_lock`,
    `self._abort_reason()` dari kelas induknya."""

    def audit(self, team: dict, event: str, **fields: Any) -> None:
        path = Path(team["paths"]["state_dir"]) / "audit.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": ht.now_iso(), "event": event, "via": "teamctl", **fields},
                                    ensure_ascii=False, default=str) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            print(f"teamctl: GAGAL menulis audit log {path} (event {event}): {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)

    @staticmethod
    def _tail(hermes: ht.Hermes, n: int = 6) -> str:
        if not hermes.failures:
            return ""
        return "\nKegagalan:\n" + "\n".join(f"- {f}" for f in hermes.failures[-n:])

    def _guarded(self, event: str, plan: Callable[[], Union[Plan, str]]) -> str:
        """Pagar → konfirmasi admin → susun ulang dari keadaan terbaru → jalankan → audit.

        `plan()` me-resolve nama, menjalankan semua pagar (ToolError = ditolak), dan mengembalikan Plan
        (ringkasan + run) atau string bila tidak ada yang perlu dilakukan. Ia dipanggil DUA kali: untuk
        ringkasan yang ditampilkan ke admin, dan lagi setelah disetujui (di dalam work_lock). `run` yang
        dijalankan adalah milik rencana kedua, dan hanya kalau ringkasannya identik dengan yang disetujui.
        """
        team = self.team()
        try:
            first = plan()
        except (ToolError, ht.TeamError) as exc:
            self.audit(team, event + "-blocked", reason=_clean(exc, 500))
            raise
        if isinstance(first, str):
            return first
        ok, why = self.confirm(first.preview)
        if not ok:
            self.audit(first.team, event + "-refused", reason=why, **first.audit)
            return f"Tidak dijalankan: {why}."
        with self.work_lock:
            abort = self._abort_reason()   # batal/EOF saat menunggu giliran
            if abort:
                self.audit(first.team, event + "-refused", reason=abort, **first.audit)
                return f"Tidak dijalankan: {abort}."
            try:
                fresh = plan()
            except (ToolError, ht.TeamError) as exc:
                self.audit(first.team, event + "-stale", reason=_clean(exc, 500), **first.audit)
                return (f"Tidak dijalankan: keadaan berubah, ulangi permintaannya (sejak konfirmasi: "
                        f"{_clean(exc, 300)}). Tidak ada yang diubah.")
            if isinstance(fresh, str) or fresh.preview != first.preview:
                self.audit(first.team, event + "-stale", reason="ringkasan berbeda dari yang disetujui", **first.audit)
                return ("Tidak dijalankan: keadaan berubah, ulangi permintaannya — yang akan dilakukan sekarang "
                        "berbeda dari ringkasan yang disetujui. Tidak ada yang diubah.")
            try:
                result = fresh.run()
            except Exception as exc:
                self.audit(fresh.team, event, outcome="failed", error=_clean(f"{type(exc).__name__}: {exc}", 500),
                           **fresh.audit)
                raise
        self.audit(fresh.team, event, outcome="ok", **fresh.audit)
        return result
