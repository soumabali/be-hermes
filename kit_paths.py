"""Lokasi berkas tim — SATU sumber kebenaran untuk hermes_team.py, kit_doctor.py, usage_ledger.py, usage_report.py.

Hanya stdlib. Urutan penentuan team.yaml:
  1. `--config <berkas>` (argumen `explicit`)
  2. env `HERMES_TEAM_DIR` (folder)
  3. `~/.hermes-team`

`./team.yaml` di direktori kerja TIDAK lagi dipakai diam-diam: unit systemd selalu memakai
`~/.hermes-team`, jadi CLI yang dijalankan dari folder lain dulu bisa menulis state ke tempat berbeda
(state terbelah). Bila berkas seperti itu ada dan berbeda dari yang dipakai, `cwd_notice()` mengembalikan
satu baris pemberitahuan.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

DEFAULT_DIR = "~/.hermes-team"
ENV_VAR = "HERMES_TEAM_DIR"
PathLike = Union[str, "os.PathLike[str]"]


def team_dir() -> Path:
    """Folder tim: `HERMES_TEAM_DIR` bila diisi, kalau tidak `~/.hermes-team`. Dibaca tiap dipanggil."""
    return Path(os.environ.get(ENV_VAR) or DEFAULT_DIR).expanduser()


def team_file(explicit: Optional[PathLike] = None) -> Path:
    """team.yaml yang dipakai: `explicit` (--config) > HERMES_TEAM_DIR > ~/.hermes-team."""
    if explicit:
        return Path(explicit).expanduser()
    return team_dir() / "team.yaml"


def _base(team_yaml: Optional[PathLike]) -> Path:
    return Path(team_yaml).expanduser().parent if team_yaml else team_dir()


def members_file(team_yaml: Optional[PathLike] = None) -> Path:
    """members.yaml: di samping team.yaml yang dipakai."""
    return _base(team_yaml) / "members.yaml"


def state_dir(team_yaml: Optional[PathLike] = None) -> Path:
    """Folder state (roster.json, usage.db, shared-skills/): di samping team.yaml yang dipakai."""
    return _base(team_yaml) / "state"


def ledger_path(team_yaml: Optional[PathLike] = None) -> Path:
    """Buku besar pemakaian (usage.db)."""
    return state_dir(team_yaml) / "usage.db"


def cwd_notice(resolved: PathLike, cwd: Optional[PathLike] = None) -> Optional[str]:
    """Satu baris pemberitahuan bila `./team.yaml` ada tetapi BUKAN berkas yang dipakai; selain itu None."""
    local = Path(cwd) / "team.yaml" if cwd else Path("team.yaml")
    try:
        if not local.is_file():
            return None
        used = Path(resolved).expanduser()
        if used.exists() and local.resolve() == used.resolve():
            return None
        shown = local.resolve()
    except OSError:
        return None
    return (f"catatan: memakai {used}; ./team.yaml di folder ini TIDAK dipakai "
            f"(pilih dengan --config {shown} atau {ENV_VAR}={shown.parent})")
