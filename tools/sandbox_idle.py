#!/usr/bin/env python3
"""sandbox_idle.py — hentikan (docker stop) container sandbox Hermes yang menganggur.

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
- "Menganggur" = waktu CPU semua proses di dalamnya (`docker top`, kolom TIME) tidak berubah sama sekali
  selama --idle-hours jam (bawaan 6). Proses yang masih memakai CPU sekecil apa pun (server dev, watcher)
  membuat container dianggap aktif. Karena dibandingkan antar-jalan, skrip harus dijalankan berkala
  (timer systemd tiap 30 menit); container yang baru pertama kali terlihat tidak pernah langsung dihentikan.
- Hanya `docker stop`. TIDAK PERNAH `docker rm`: container, volume, dan /workspace tetap ada.
- Catatan pengamatan disimpan di <state>/sandbox_idle.json (bawaan ~/.hermes-team/state).

Pakai:
  python3 tools/sandbox_idle.py                      # laporan saja (dry-run)
  python3 tools/sandbox_idle.py --idle-hours 12 --exclude team-admin,team-shared
  python3 tools/sandbox_idle.py --apply              # sungguh menghentikan (setelah diuji di staging)

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


def observe(run: Runner) -> list[dict]:
    """Container sandbox yang berjalan + sidik jari CPU-nya. docker gagal → RuntimeError."""
    fmt = "{{.ID}}\t{{.Label \"" + PROFILE_LABEL + "\"}}\t{{.Names}}"
    code, out = run(["docker", "ps", "--no-trunc", "--filter", f"label={PROFILE_LABEL}",
                     "--filter", "status=running", "--format", fmt])
    if code != 0:
        raise RuntimeError((out or "").strip().splitlines()[-1] if (out or "").strip() else f"exit {code}")
    containers = parse_ps(out)
    for c in containers:
        code, top = run(["docker", "top", c["id"], "-eo", "pid,time"])
        c["fingerprint"] = cpu_fingerprint(top) if code == 0 else None
    return containers


# ----------------------------------------------------------------------------- pilih yang menganggur
def select_idle(containers: list, state: dict, now: float, idle_seconds: float,
                exclude: frozenset = frozenset()) -> tuple:
    """Logika murni. → (daftar yang layak dihentikan, state baru).

    `state`: {container_id: {"fingerprint", "since", "profile"}} dari jalan sebelumnya. Sebuah container
    layak dihentikan hanya bila sidik jari CPU-nya SAMA dengan yang tercatat dan sudah begitu selama
    ≥ idle_seconds. Baru terlihat, sidik jari berubah, atau tidak terbaca → dianggap aktif (jam diulang).
    Profile di `exclude` tetap diamati tetapi tidak pernah dipilih."""
    new_state: dict = {}
    idle: list = []
    for c in containers:
        previous = state.get(c["id"]) if isinstance(state.get(c["id"]), dict) else None
        fingerprint = c.get("fingerprint")
        since = now
        if fingerprint and previous and previous.get("fingerprint") == fingerprint \
                and isinstance(previous.get("since"), (int, float)) and previous["since"] <= now:
            since = float(previous["since"])
        if fingerprint:      # tidak terbaca → tidak dicatat, jadi jam mulai dari nol saat terbaca lagi
            new_state[c["id"]] = {"fingerprint": fingerprint, "since": since, "profile": c["profile"]}
        idle_for = now - since
        c = dict(c, idle_seconds=idle_for)
        if fingerprint and idle_for >= idle_seconds and c["profile"] not in exclude:
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
def sweep(run: Runner, state_file: Path, *, idle_hours: float, apply: bool, exclude: frozenset = frozenset(),
          now: Optional[float] = None, out: Callable[[str], None] = print) -> int:
    now = time.time() if now is None else now
    try:
        containers = observe(run)
    except RuntimeError as exc:
        out(f"sandbox_idle: docker tidak bisa dijalankan ({exc}); tidak ada yang diubah")
        return 1
    idle, new_state = select_idle(containers, load_state(state_file), now, idle_hours * 3600, exclude)
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
                out(f"  ? {label}: pemakaian CPU tidak terbaca → dibiarkan")
            else:
                out(f"  = {label}: aktif atau belum cukup lama diam ({(now - seen['since']) / 3600:.1f} jam)")
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
    ap.add_argument("--idle-hours", type=float, default=6.0, help="lama tanpa pemakaian CPU (bawaan 6 jam)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="hanya melapor (bawaan)")
    mode.add_argument("--apply", action="store_true", help="sungguh menjalankan docker stop")
    ap.add_argument("--exclude", default="", help="nama profile yang tidak pernah dihentikan, dipisah koma")
    ap.add_argument("--state-file", default="", help="bawaan: ~/.hermes-team/state/sandbox_idle.json")
    args = ap.parse_args(argv)
    if args.idle_hours <= 0:
        print("sandbox_idle: --idle-hours harus lebih dari 0", file=sys.stderr)
        return 2
    state_file = Path(args.state_file).expanduser() if args.state_file else default_state_file()
    exclude = frozenset(p.strip() for p in args.exclude.split(",") if p.strip())
    return sweep(run, state_file, idle_hours=args.idle_hours, apply=bool(args.apply), exclude=exclude)


if __name__ == "__main__":
    sys.exit(main())
