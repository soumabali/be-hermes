#!/usr/bin/env python3
"""sandbox_idle.py — hentikan (docker stop) container sandbox Hermes yang menganggur.

TERBUKTI dengan Docker sungguhan (Docker 29.9.2, container berlabel `hermes-profile`): container diam dihentikan pada
sapuan kedua, container yang sibuk tidak disentuh, dan yang dihentikan tetap ada (Exited; tidak dihapus).

!!! BELUM TERBUKTI — UJI DI SERVER STAGING DULU !!!
Apakah Hermes menyalakan lagi container sandbox yang sudah dihentikan (status Exited) saat profile itu
dipakai berikutnya BELUM DIVERIFIKASI. Dokumentasi Hermes hanya menyebut ia "menempel ke container yang
ada"; perilakunya untuk container yang berhenti belum pernah diuji dengan kit ini. Kalau ternyata tidak
dinyalakan lagi, anggota akan mendapat error terminal sampai container-nya di-`docker start` manual.
Karena itu skrip ini bawaannya HANYA MELAPOR (--dry-run). Jalankan dengan --apply baru setelah di server
staging terbukti: hentikan satu container → kirim pesan yang memakai terminal → container hidup lagi dan
/workspace utuh.

Yang dilakukan:
- Hanya container berstatus running yang berlabel `hermes-profile` (label yang dipasang Hermes).
- "Menganggur" = selama --idle-hours jam (bawaan 6) KETIGA tanda ini diam:
    1. pemakaian CPU kumulatif cgroup container (`usage_usec`, presisi mikrodetik) tidak bertambah antar-jalan.
       Dibaca dari cgroup di host (/sys/fs/cgroup/…/docker-<id>.scope/cpu.stat; tanpa mengganggu container);
       bila tidak terbaca, lewat `docker exec <id> cat /sys/fs/cgroup/cpu.stat` — cara ini sendiri memakai
       sedikit CPU container, jadi kenaikan ≤ --cpu-noise-ms (bawaan 250 md) per jalan dianggap derau.
       Penghitung kumulatif menangkap SEMUA pemakaian di antara dua jalan, bukan hanya keadaan saat dicuplik
       (kolom TIME `docker top` berpresisi 1 detik dan bisa melewatkan perintah singkat);
    2. daftar proses (`docker top`: pid + TIME) tidak berubah — proses lahir/mati berarti dipakai;
    3. `state.db` profile-nya (label `hermes-profile` → <hermes-root>/profiles/<profile>/state.db, juga
       berkas -wal) tidak diubah dalam jendela itu — percakapan yang baru saja terjadi berarti dipakai.
  Tanda yang tidak terbaca (cgroup CPU atau state.db) membuat container dianggap AKTIF. Karena dibandingkan
  antar-jalan, skrip harus dijalankan berkala (timer systemd tiap 30 menit); container yang baru pertama
  kali terlihat tidak pernah langsung dihentikan.
- Hanya `docker stop`. TIDAK PERNAH `docker rm`: container, volume, dan /workspace tetap ada.
- Catatan pengamatan disimpan di <state>/sandbox_idle.json (bawaan ~/.hermes-team/state).

Pakai:
  python3 tools/sandbox_idle.py                      # laporan saja (dry-run)
  python3 tools/sandbox_idle.py --idle-hours 12 --exclude team-admin,team-shared
  python3 tools/sandbox_idle.py --apply              # sungguh menghentikan (setelah diuji di staging)
  python3 tools/sandbox_idle.py --hermes-root /home/hermes/.hermes    # lokasi profile (bawaan: $HERMES_HOME)

Hanya pustaka standar. User yang menjalankan harus boleh memakai docker (grup docker).
Kode keluar: 0 = selesai; 1 = docker tidak bisa dijalankan atau ada `docker stop` yang gagal.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

PROFILE_LABEL = "hermes-profile"
Runner = Callable[[list], tuple]            # (argv) → (kode keluar, stdout atau stderr)
ReadFile = Callable[[str], Optional[str]]   # (path di host) → isi atau None
Mtime = Callable[[Path], Optional[float]]   # (path) → mtime, None bila tidak ada; OSError lain diteruskan
EXEC_NOISE_USEC = 250_000                   # CPU yang dipakai `docker exec … cat` sendiri (derau per jalan)
# Lokasi cgroup container di host: (pola path, pembagi ke mikrodetik). v2 systemd, v2 cgroupfs, lalu v1.
HOST_CGROUP_PATHS = (
    ("/sys/fs/cgroup/system.slice/docker-{id}.scope/cpu.stat", None),
    ("/sys/fs/cgroup/docker/{id}/cpu.stat", None),
    ("/sys/fs/cgroup/cpu,cpuacct/system.slice/docker-{id}.scope/cpuacct.usage", 1000),
    ("/sys/fs/cgroup/cpu,cpuacct/docker/{id}/cpuacct.usage", 1000),
    ("/sys/fs/cgroup/cpuacct/docker/{id}/cpuacct.usage", 1000),
)
EXEC_CGROUP_PATHS = (("/sys/fs/cgroup/cpu.stat", None), ("/sys/fs/cgroup/cpuacct/cpuacct.usage", 1000))


def run_command(cmd: list, timeout: int = 60) -> tuple:
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout))


# ----------------------------------------------------------------------------- baca keadaan docker
def parse_ps(output: str) -> list[dict]:
    """Keluaran `docker ps --format '{{.ID}}\\t{{.Label "hermes-profile"}}\\t{{.Names}}'`."""
    found = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
            found.append({"id": parts[0].strip(), "profile": parts[1].strip(),
                          "name": parts[2].strip() if len(parts) > 2 else ""})
    return found


def cpu_fingerprint(top_output: str) -> Optional[str]:
    """Sidik jari pemakaian CPU dari `docker top <id> -eo pid,time`: hash daftar (pid, waktu CPU).
    Berubah bila ada proses yang memakai CPU, lahir, atau mati. Keluaran tak dikenali → None."""
    lines = [line.split() for line in top_output.splitlines() if line.strip()]
    if not lines or [c.upper() for c in lines[0][:2]] != ["PID", "TIME"]:
        return None
    rows = sorted((cols[0], cols[1]) for cols in lines[1:] if len(cols) >= 2)
    if not rows:
        return None
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()[:32]


def read_host_file(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def file_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except (FileNotFoundError, NotADirectoryError):
        return None


def parse_cpu_usage(text: Optional[str], divisor: Optional[int] = None) -> Optional[int]:
    """Mikrodetik CPU kumulatif. `divisor=None`: isi cpu.stat cgroup v2 (baris `usage_usec N`);
    selain itu isi cpuacct.usage cgroup v1 (nanodetik) dibagi `divisor`. Tak dikenali → None."""
    if not isinstance(text, str):
        return None
    try:
        if divisor:
            return int(text.strip().split()[0]) // divisor
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "usage_usec":
                return int(parts[1])
    except (ValueError, IndexError):
        return None
    return None


def cpu_usage(run: Runner, container_id: str, read_file: ReadFile = read_host_file) -> tuple:
    """(mikrodetik CPU kumulatif | None, sumber "host" | "exec" | ""). Host dulu (tanpa efek samping), lalu
    `docker exec <id> cat …` (hanya `cat`; tidak pernah shell)."""
    for pattern, divisor in HOST_CGROUP_PATHS:
        usage = parse_cpu_usage(read_file(pattern.format(id=container_id)), divisor)
        if usage is not None:
            return usage, "host"
    for path, divisor in EXEC_CGROUP_PATHS:
        code, out = run(["docker", "exec", container_id, "cat", path])
        usage = parse_cpu_usage(out, divisor) if code == 0 else None
        if usage is not None:
            return usage, "exec"
    return None, ""


def profile_home(hermes_root: Path, profile: str) -> Path:
    """Home profile Hermes: `default` = root itu sendiri, selain itu <root>/profiles/<profile>."""
    root = Path(hermes_root)
    return root if profile in ("", "default") else root / "profiles" / profile


def profile_activity(hermes_root: Optional[Path], profile: str, mtime: Mtime = file_mtime) -> tuple:
    """(mtime terbaru state.db/-wal profile | None bila berkasnya tidak ada, terbaca?). Tidak bisa diperiksa
    (izin, I/O) → (None, False): pemanggil menganggap container aktif."""
    if hermes_root is None:
        return None, True
    if not profile or "/" in profile or "\\" in profile or profile in (".", ".."):
        return None, False                    # label aneh → jangan menebak path
    home = profile_home(hermes_root, profile)
    latest: Optional[float] = None
    try:
        for name in ("state.db", "state.db-wal"):
            stamp = mtime(home / name)
            if stamp is not None and (latest is None or stamp > latest):
                latest = float(stamp)
    except OSError:
        return None, False
    return latest, True


def observe(run: Runner, *, read_file: ReadFile = read_host_file, hermes_root: Optional[Path] = None,
            mtime: Mtime = file_mtime) -> list[dict]:
    """Container sandbox yang berjalan + sidik jari proses, CPU cgroup, dan aktivitas state.db profile-nya.
    docker gagal → RuntimeError."""
    fmt = "{{.ID}}\t{{.Label \"" + PROFILE_LABEL + "\"}}\t{{.Names}}"
    code, out = run(["docker", "ps", "--no-trunc", "--filter", f"label={PROFILE_LABEL}",
                     "--filter", "status=running", "--format", fmt])
    if code != 0:
        raise RuntimeError((out or "").strip().splitlines()[-1] if (out or "").strip() else f"exit {code}")
    containers = parse_ps(out)
    for c in containers:
        code, top = run(["docker", "top", c["id"], "-eo", "pid,time"])
        c["fingerprint"] = cpu_fingerprint(top) if code == 0 else None
        c["cpu_usec"], c["cpu_source"] = cpu_usage(run, c["id"], read_file)
        c["activity"], c["activity_known"] = profile_activity(hermes_root, c["profile"], mtime)
    return containers


# ----------------------------------------------------------------------------- pilih yang menganggur
def select_idle(containers: list, state: dict, now: float, idle_seconds: float,
                exclude: frozenset = frozenset(), exec_noise_usec: int = EXEC_NOISE_USEC) -> tuple:
    """Logika murni. → (daftar yang layak dihentikan, state baru).

    `state`: {container_id: {"fingerprint", "cpu_usec", "since", "profile"}} dari jalan sebelumnya. Sebuah
    container layak dihentikan hanya bila SEMUA ini benar selama ≥ idle_seconds:
      - sidik jari proses SAMA dengan yang tercatat;
      - CPU cgroup kumulatif tidak bertambah (sumber "exec": bertambah ≤ exec_noise_usec per jalan);
      - state.db profile-nya (`activity`) tidak diubah dalam jendela itu.
    Baru terlihat, catatan lama tanpa CPU, sidik jari/CPU berubah, CPU mundur (container di-restart), atau
    tanda yang tidak terbaca → dianggap aktif (jam diulang). Profile di `exclude` tetap diamati tetapi tidak
    pernah dipilih."""
    new_state: dict = {}
    idle: list = []
    for c in containers:
        previous = state.get(c["id"]) if isinstance(state.get(c["id"]), dict) else None
        fingerprint, cpu = c.get("fingerprint"), c.get("cpu_usec")
        readable = bool(fingerprint) and isinstance(cpu, int) and not isinstance(cpu, bool)
        since = now
        if readable and previous and previous.get("fingerprint") == fingerprint \
                and isinstance(previous.get("since"), (int, float)) and previous["since"] <= now \
                and isinstance(previous.get("cpu_usec"), int) and not isinstance(previous.get("cpu_usec"), bool):
            noise = int(exec_noise_usec) if c.get("cpu_source") == "exec" else 0
            if 0 <= cpu - previous["cpu_usec"] <= noise:
                since = float(previous["since"])
        if readable:         # tidak terbaca → tidak dicatat, jadi jam mulai dari nol saat terbaca lagi
            new_state[c["id"]] = {"fingerprint": fingerprint, "cpu_usec": cpu, "since": since,
                                  "profile": c["profile"]}
        idle_for = now - since
        activity = c.get("activity")
        activity_known = c.get("activity_known", True)
        if isinstance(activity, (int, float)) and not isinstance(activity, bool):
            idle_for = min(idle_for, max(0.0, now - float(activity)))   # percakapan terakhir di profile itu
        c = dict(c, idle_seconds=idle_for)
        if readable and activity_known and idle_for >= idle_seconds and c["profile"] not in exclude:
            idle.append(c)
    return idle, new_state


# ----------------------------------------------------------------------------- state
def default_state_file() -> Path:
    team_dir = Path(os.environ.get("HERMES_TEAM_DIR") or "~/.hermes-team").expanduser()
    return team_dir / "state" / "sandbox_idle.json"


def load_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ----------------------------------------------------------------------------- main
def default_hermes_root() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()


def sweep(run: Runner, state_file: Path, *, idle_hours: float, apply: bool, exclude: frozenset = frozenset(),
          now: Optional[float] = None, out: Callable[[str], None] = print,
          hermes_root: Optional[Path] = None, read_file: ReadFile = read_host_file, mtime: Mtime = file_mtime,
          exec_noise_usec: int = EXEC_NOISE_USEC) -> int:
    """`hermes_root=None` → aktivitas state.db tidak diperiksa (hanya untuk uji/penggunaan khusus; `main`
    selalu mengisinya)."""
    now = time.time() if now is None else now
    try:
        containers = observe(run, read_file=read_file, hermes_root=hermes_root, mtime=mtime)
    except RuntimeError as exc:
        out(f"sandbox_idle: docker tidak bisa dijalankan ({exc}); tidak ada yang diubah")
        return 1
    idle, new_state = select_idle(containers, load_state(state_file), now, idle_hours * 3600, exclude,
                                  exec_noise_usec)
    chosen = {c["id"] for c in idle}
    out(f"{len(containers)} container sandbox berjalan; {len(idle)} menganggur ≥ {idle_hours:g} jam"
        + ("" if apply else "  [dry-run: tidak ada yang dihentikan; pakai --apply setelah diuji di staging]"))
    failed = 0
    for c in sorted(containers, key=lambda x: x["profile"]):
        label = f"{c['profile']} ({c['name'] or c['id'][:12]})"
        if c["id"] not in chosen:
            seen = new_state.get(c["id"])
            if c["profile"] in exclude:
                out(f"  = {label}: dikecualikan")
            elif not seen:
                out(f"  ? {label}: pemakaian CPU (cgroup / docker top) tidak terbaca → dibiarkan")
            elif not c.get("activity_known", True):
                out(f"  ? {label}: state.db profile tidak bisa diperiksa → dibiarkan")
            else:
                quiet = now - seen["since"]
                recent = c.get("activity")
                if isinstance(recent, (int, float)) and now - recent < quiet:
                    out(f"  = {label}: profile dipakai {max(0.0, now - recent) / 3600:.1f} jam lalu (state.db) → aktif")
                else:
                    out(f"  = {label}: aktif atau belum cukup lama diam ({quiet / 3600:.1f} jam)")
            continue
        hours = next(x["idle_seconds"] for x in idle if x["id"] == c["id"]) / 3600
        if not apply:
            out(f"  ~ {label}: diam {hours:.1f} jam → AKAN dihentikan (dry-run)")
            continue
        code, text = run(["docker", "stop", c["id"]])      # hanya stop; tidak pernah rm
        if code == 0:
            out(f"  - {label}: diam {hours:.1f} jam → dihentikan (docker stop; data tetap)")
            new_state.pop(c["id"], None)
        else:
            failed += 1
            out(f"  ! {label}: docker stop gagal: {(text or '').strip().splitlines()[-1] if (text or '').strip() else code}")
    try:
        save_state(state_file, new_state)
    except OSError as exc:
        out(f"sandbox_idle: catatan pengamatan tidak bisa disimpan di {state_file} ({exc})")
        return 1
    return 1 if failed else 0


def main(argv: Optional[list] = None, run: Runner = run_command) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--idle-hours", type=float, default=6.0,
                    help="lama tanpa pemakaian CPU dan tanpa percakapan di profile-nya (bawaan 6 jam)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="hanya melapor (bawaan)")
    mode.add_argument("--apply", action="store_true", help="sungguh menjalankan docker stop")
    ap.add_argument("--exclude", default="", help="nama profile yang tidak pernah dihentikan, dipisah koma")
    ap.add_argument("--state-file", default="", help="bawaan: ~/.hermes-team/state/sandbox_idle.json")
    ap.add_argument("--hermes-root", default="", help="folder Hermes (profiles/<nama>/state.db dipakai sebagai "
                    "tanda aktivitas); bawaan: $HERMES_HOME atau ~/.hermes")
    ap.add_argument("--cpu-noise-ms", type=float, default=EXEC_NOISE_USEC / 1000.0,
                    help="kenaikan CPU per jalan yang dianggap derau saat CPU dibaca lewat `docker exec` "
                         "(bawaan 250 md; pembacaan dari cgroup host tidak memakai toleransi)")
    args = ap.parse_args(argv)
    if args.idle_hours <= 0:
        print("sandbox_idle: --idle-hours harus lebih dari 0", file=sys.stderr)
        return 2
    if args.cpu_noise_ms < 0:
        print("sandbox_idle: --cpu-noise-ms tidak boleh negatif", file=sys.stderr)
        return 2
    state_file = Path(args.state_file).expanduser() if args.state_file else default_state_file()
    exclude = frozenset(p.strip() for p in args.exclude.split(",") if p.strip())
    hermes_root = Path(args.hermes_root).expanduser() if args.hermes_root else default_hermes_root()
    return sweep(run, state_file, idle_hours=args.idle_hours, apply=bool(args.apply), exclude=exclude,
                 hermes_root=hermes_root, exec_noise_usec=int(args.cpu_noise_ms * 1000))


if __name__ == "__main__":
    sys.exit(main())
