"""Bagian dari kit tim (dipecah dari hermes_team.py). Lihat docs/KONTRAK.md dan README."""
from __future__ import annotations
import contextlib
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import yaml
import kit_paths
from kit_base import TeamError
from kit.constants import PROFILE_RE
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _profile(name: str, where: str) -> str:
    if not PROFILE_RE.match(name or "") or name in ("main", "default"):
        raise TeamError(f"{where}: nama profile '{name}' tidak valid "
                        "(huruf kecil, angka, '-' atau '_', maks 64 karakter)")
    return name


_TRUE_WORDS = {"true", "yes", "y", "on", "1", "ya", "aktif"}


_FALSE_WORDS = {"false", "no", "n", "off", "0", "tidak", "mati", ""}


def _bool(value: Any, where: str, default: bool = False) -> bool:
    """Boolean dari YAML. `bool("false")` bernilai True di Python, jadi string diurai eksplisit."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    raise TeamError(f"{where}: '{value}' bukan boolean (pakai true atau false)")


def _int(value: Any, where: str, minimum: Optional[int] = None) -> int:
    """Bilangan bulat dari YAML; salah ketik → TeamError yang menyebut key-nya (bukan traceback)."""
    number: Optional[int] = None
    if isinstance(value, bool) or value is None:
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and value == int(value):
        number = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        number = int(value.strip())
    if number is None:
        raise TeamError(f"{where}: '{value}' bukan bilangan bulat")
    if minimum is not None and number < minimum:
        raise TeamError(f"{where}: minimal {minimum} (sekarang {number})")
    return number


def _number(value: Any, where: str) -> Any:
    if isinstance(value, bool) or value is None:
        raise TeamError(f"{where}: '{value}' bukan angka")
    if isinstance(value, (int, float)):
        number: Any = value
    else:
        try:
            number = float(str(value).strip())
        except ValueError:
            raise TeamError(f"{where}: '{value}' bukan angka")
        if number == int(number):
            number = int(number)
    if number <= 0:
        raise TeamError(f"{where}: harus lebih dari 0")
    return number


def clean_display_name(text: Any, fallback: str = "") -> str:
    """Nama tampilan dikendalikan user Discord dan masuk ke SOUL.md/status/topik channel → buang
    karakter kontrol, rapikan spasi, maks 64 karakter."""
    cleaned = re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", str(text or "")).strip()[:64].strip()
    return cleaned or fallback


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
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class _PlainDumper(yaml.SafeDumper):
    """SafeDumper tanpa anchor/alias (&id001) supaya YAML mudah dibaca dan ditempel."""
    def ignore_aliases(self, data):
        return True


def dump_yaml(data: Any) -> str:
    return yaml.dump(data, Dumper=_PlainDumper, sort_keys=False, allow_unicode=True, width=100)


# --------------------------------------------------------------------------- muat team.yaml + members.yaml
def default_team_file() -> Path:
    """team.yaml bawaan: $HERMES_TEAM_DIR/team.yaml, kalau tidak ~/.hermes-team/team.yaml (kit_paths).
    `./team.yaml` di direktori kerja TIDAK dipakai diam-diam (dulu menang, dan state ikut folder itu
    sementara unit systemd selalu memakai ~/.hermes-team → state terbelah)."""
    return kit_paths.team_file()


def managed_dir() -> Path:
    """Folder managed scope Hermes: env HERMES_MANAGED_DIR (uji / instalasi tak lazim) atau /etc/hermes."""
    override = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    return Path(override).expanduser() if override else Path("/etc/hermes")


def _read_yaml_file(path: Path, what: str) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise TeamError(f"{path} tidak ditemukan (salin dari {what}.example.yaml)")
    except yaml.YAMLError as exc:
        raise TeamError(f"{path}: YAML tidak valid: {exc}")
    if not isinstance(raw, dict):
        raise TeamError(f"{path}: isi teratas harus mapping")
    return raw

