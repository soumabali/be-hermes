"""Lokasi berkas tim — SATU sumber kebenaran untuk hermes_team.py, kit_doctor.py, usage_ledger.py, usage_report.py.

Hanya stdlib (PyYAML dimuat malas hanya saat berkas dibaca). Urutan penentuan team.yaml:
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


# --------------------------------------------------------------------------- membaca team.yaml (SATU pembaca)
def load_raw(team_yaml: Optional[PathLike] = None, *, strict: bool = False) -> dict:
    """Isi MENTAH team.yaml (dict) — satu-satunya pembaca YAML team.yaml untuk alat ringan (usage_ledger,
    usage_report, tools/usage_post, kit_doctor). Berkas hilang / rusak / bukan mapping → {} kecuali
    `strict=True` (→ kit_base.TeamError berisi alasannya). Validasi nilai ada di kit_config (skema)."""
    path = team_file(team_yaml)
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:        # noqa: BLE001 — OSError, YAMLError, ImportError: semuanya = tidak terbaca
        if strict:
            from kit_base import TeamError
            raise TeamError(f"{path} tidak terbaca ({type(exc).__name__}: {exc})")
        return {}
    if not isinstance(data, dict):
        if strict:
            from kit_base import TeamError
            raise TeamError(f"{path}: isi teratas harus mapping")
        return {}
    return data


def usage_view(team_yaml: Optional[PathLike] = None, *, raw: Optional[dict] = None) -> dict:
    """Bagian `usage.*` TERVALIDASI ({"timezone", "report_channel_id", "reports": {daily, weekly, monthly}}).
    Skemanya dari kit_config (diimpor malas supaya alat ringan tetap ringan); nilai salah → TeamError."""
    import kit_config
    return kit_config.usage_view(raw if raw is not None else load_raw(team_yaml))


def usage_timezone(team_yaml: Optional[PathLike] = None) -> str:
    """`usage.timezone` apa adanya (longgar: tidak pernah gagal, untuk penentu zona waktu di alat ringan)."""
    usage = load_raw(team_yaml).get("usage")
    return str((usage.get("timezone") if isinstance(usage, dict) else "") or "").strip()


def profile_prefix(team_yaml: Optional[PathLike] = None, default: str = "be-") -> str:
    """`profile_prefix` dari team.yaml: key tidak ada → `default`; string kosong sah (profile = nama anggota)."""
    value = load_raw(team_yaml).get("profile_prefix", default)
    return default if value is None else str(value)
