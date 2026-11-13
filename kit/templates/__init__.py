"""Teks SOUL.md per jenis profile dan pesan sambutan. Isinya ada di berkas `*.md` di folder ini (mudah
disunting tanpa menyentuh kode); dimuat sekali saat impor, persis apa adanya (placeholder `{...}` untuk
`str.format`). Berkas sambutan disimpan TANPA baris baru penutup."""
from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent


def _read(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8")


SOUL_MEMBER = _read("soul_member.md")
SOUL_TEAM_SKILLS_NOTE = _read("soul_team_skills_note.md")
SOUL_SHARED = _read("soul_shared.md")
SOUL_ADMIN = _read("soul_admin.md")
SOUL_LOBBY = _read("soul_lobby.md")
DEFAULT_WELCOME = _read("welcome.md")
DEFAULT_WELCOME_BACK = _read("welcome_back.md")


del _read, _DIR     # pembantu impor saja; tidak ikut diekspor lewat fasad hermes_team
