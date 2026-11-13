"""Bagian dari kit tim (dipecah dari hermes_team.py). Lihat docs/KONTRAK.md dan README."""
from __future__ import annotations
import contextlib
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional
import yaml
from kit_base import TeamError, parse_env_text
from kit.constants import PLUGIN_NAME, PLUGIN_SRC, PROFILE_SCOPED_KEYS
from kit.util import atomic_write, managed_dir
from kit.derive import _get_nested, _same, cli_value


# --------------------------------------------------------------------------- CLI hermes
ENV_PLACEHOLDER = ("# Secret milik profile ini saja (mode 0600). Diisi lewat /env-set di Discord atau\n"
                   "# `hermes_team.py llm-key`. Sengaja kosong saat profile dibuat: tidak mewarisi profile lain.\n")


class Hermes:
    """Pembungkus CLI `hermes`. Hanya menjalankan `config set` untuk nilai yang berbeda."""

    def __init__(self, binary: str, root: Path, dry_run: bool = False, quiet: bool = False):
        self.binary, self.root, self.dry_run, self.quiet = binary, Path(root).expanduser(), dry_run, quiet
        self.failures: list[str] = []
        self.log: list[str] = []
        self.changed_keys: dict[str, set] = {}
        self.managed_blocked: set = set()          # key per-profile yang dilewati karena masih dipin managed
        self._managed_pins: Optional[set] = None

    def say(self, line: str) -> None:
        self.log.append(line)
        if not self.quiet:
            print(line)

    def home(self, profile: str) -> Path:
        return self.root if profile == "default" else self.root / "profiles" / profile

    def exists(self, profile: str) -> bool:
        home = self.home(profile)
        return any((home / f).exists() for f in ("config.yaml", ".env", "SOUL.md", "profile.yaml"))

    def dir_exists(self, profile: str) -> bool:
        """Folder profile ada (walau kosong/setengah jadi) → namanya dianggap terpakai."""
        home = self.home(profile)
        return home.exists() or home.is_symlink()

    def profile_dirs(self) -> set[str]:
        """Nama semua folder di <root>/profiles (termasuk sisa profile lama yang tidak ada di members.yaml)."""
        try:
            return {d.name for d in (self.root / "profiles").iterdir()}
        except OSError:
            return set()

    def env(self) -> dict:
        env = dict(os.environ)
        env["HERMES_HOME"] = str(self.root)
        return env

    def run(self, *args: str, fatal: bool = False, timeout: int = 180) -> bool:
        cmd = [self.binary, *args]
        self.say("  $ " + shlex.join(cmd))
        if self.dry_run:
            return True
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, stdin=subprocess.DEVNULL,
                                  env=self.env(), timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self.failures.append(f"{shlex.join(cmd)}  →  {detail}")
            self.say(f"    ✗ {detail}")
            if fatal:
                raise TeamError(detail)
            return False
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).strip().splitlines()
            detail = msg[-1] if msg else f"exit {proc.returncode}"
            self.failures.append(f"{shlex.join(cmd)}  →  {detail}")
            self.say(f"    ✗ {detail}")
            if fatal:
                raise TeamError(detail)
            return False
        return True

    @staticmethod
    def profile_args(profile: str) -> list[str]:
        return ["-p", profile]

    def read_config(self, profile: str) -> dict:
        try:
            data = yaml.safe_load((self.home(profile) / "config.yaml").read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return {}
        return data if isinstance(data, dict) else {}

    def read_env(self, profile: str) -> dict:
        try:
            return parse_env_text((self.home(profile) / ".env").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return {}

    def managed_pins(self) -> set:
        """PROFILE_SCOPED_KEYS yang MASIH dipin managed config terpasang (file dari kit versi lama).
        Hermes menolak `config set` untuk key yang dipin, dan nilai managed tetap menang, jadi key itu
        dilewati (bukan kegagalan) sampai admin memasang ulang /etc/hermes/config.yaml."""
        if self._managed_pins is None:
            try:
                data = yaml.safe_load((managed_dir() / "config.yaml").read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                data = None
            self._managed_pins = {k for k in PROFILE_SCOPED_KEYS if _get_nested(data, k) is not None}
        return self._managed_pins

    def config_set(self, profile: str, settings: Iterable[tuple[str, object]]) -> int:
        cfg, env = self.read_config(profile), self.read_env(profile)
        changed = attempted = 0
        for key, value in settings:
            if key in PROFILE_SCOPED_KEYS and key in self.managed_pins():
                self.managed_blocked.add(key)
                continue
            if key.isupper():
                same = env.get(key) == cli_value(key, value)
            else:
                same = _same(key, _get_nested(cfg, key), value)
            if same:
                continue
            attempted += 1
            # Key yang gagal di-set TIDAK dicatat sebagai berubah (kegagalannya ada di self.failures).
            if self.run(*self.profile_args(profile), "config", "set", key, cli_value(key, value)):
                changed += 1
                self.changed_keys.setdefault(profile, set()).add(key)
        if attempted == 0:
            self.say(f"  = {profile}: setelan sudah sesuai")
        return changed

    def write_file(self, path: Path, text: str, *, force: bool, backup: bool = True) -> None:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            return
        if current is not None and not force:
            self.say(f"  = {path} sudah ada, dibiarkan (pakai --force-soul untuk menimpa)")
            return
        backup = backup and current is not None
        self.say(f"  + tulis {path}" + (" (versi lama → .bak)" if backup else ""))
        if self.dry_run:
            return
        mode = 0o644
        if current is not None:
            with contextlib.suppress(OSError):
                mode = path.stat().st_mode & 0o777
        if backup:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        atomic_write(path, text, mode=mode)   # atomik: pembaca tidak pernah melihat file setengah tertulis

    def wipe_memory(self, profile: str) -> None:
        """Clone ikut menyalin MEMORY.md/USER.md; profile baru harus mulai kosong."""
        for name in ("MEMORY.md", "USER.md"):
            path = self.home(profile) / "memories" / name
            if path.exists():
                self.say(f"  - kosongkan {path}")
                if not self.dry_run:
                    path.unlink()

    def create(self, profile: str, clone_from: str, *, alias: bool = False) -> bool:
        if self.exists(profile):
            return False
        args = ["profile", "create", profile, "--clone-from", clone_from]
        if not alias:
            args.append("--no-alias")
        self.run(*args, fatal=True)
        self.wipe_memory(profile)
        self.scrub_env(profile)
        return True

    def scrub_env(self, profile: str) -> None:
        """`hermes profile create --clone-from` ikut menyalin `.env` sumber (hanya kredensial channel yang
        dibuang). Profile baru tidak boleh mewarisi secret profile lain → kosongkan. HANYA dipanggil
        tepat setelah profile dibuat; `.env` profile yang sudah ada tidak pernah disentuh."""
        if profile == "default":
            return
        path = self.home(profile) / ".env"
        self.say(f"  - kosongkan {path} (secret hasil clone tidak diwarisi)")
        if not self.dry_run:
            atomic_write(path, ENV_PLACEHOLDER, mode=0o600)

    def park(self, profile: str) -> None:
        marker = self.home(profile) / "gateway.parked"
        if marker.exists():
            return
        self.say(f"  + parkir {profile} ({marker.name})")
        if not self.dry_run:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()

    def unpark(self, profile: str) -> None:
        marker = self.home(profile) / "gateway.parked"
        if not marker.exists():
            return
        self.say(f"  - lepas parkir {profile} ({marker.name})")
        if not self.dry_run:
            marker.unlink()

    def install_plugin(self, profile: str) -> None:
        """Symlink plugin team-guard ke <home>/plugins lalu aktifkan (sekali)."""
        link = self.home(profile) / "plugins" / PLUGIN_NAME
        target = PLUGIN_SRC
        if link.is_symlink():
            if Path(os.readlink(link)) != target:
                self.say(f"  ~ arahkan ulang {link} → {target}")
                if not self.dry_run:
                    link.unlink()
                    link.symlink_to(target, target_is_directory=True)
        elif link.exists():
            self.say(f"  ! {link} sudah ada (bukan symlink) — dibiarkan; pastikan isinya versi terbaru")
        else:
            self.say(f"  + symlink {link} → {target}")
            if not self.dry_run:
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
        enabled = _get_nested(self.read_config(profile), "plugins.enabled") or []
        if PLUGIN_NAME not in enabled:
            if self.run(*self.profile_args(profile), "plugins", "enable", PLUGIN_NAME):
                self.changed_keys.setdefault(profile, set()).add("plugins.enabled")


def resolve_hermes_bin(explicit: Optional[str] = None) -> str:
    candidate = explicit or os.environ.get("HERMES_BIN") or shutil.which("hermes") \
        or str(Path("~/.local/bin/hermes").expanduser())
    found = shutil.which(candidate) or candidate
    return str(Path(found).expanduser().resolve()) if Path(found).expanduser().exists() else found

