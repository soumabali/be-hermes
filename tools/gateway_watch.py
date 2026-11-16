#!/usr/bin/env python3
"""gateway_watch.py — periksa apakah gateway Hermes hidup; kabari webhook bila mati.

Dijalankan berkala oleh timer user systemd (systemd/hermes-gateway-watch.*), sebagai user Hermes:

  python3 tools/gateway_watch.py              # satu pemeriksaan (dipanggil timer)
  python3 tools/gateway_watch.py --test       # kirim pesan uji ke webhook, lalu keluar
  python3 tools/gateway_watch.py --dry-run    # periksa dan cetak pesan yang AKAN dikirim; tidak mengirim, tidak mencatat

Cara kerja:
- "Hidup" = `systemctl --user is-active <unit>` menjawab `active`. Unit = hermes-gateway.service, atau satu-satunya
  hermes-gateway-*.service di ~/.config/systemd/user (sama dengan cara wizard mencarinya); atau --unit.
- Pesan MATI dikirim setelah gateway terlihat mati pada --confirm-runs pemeriksaan berturut-turut (bawaan 2, supaya
  `/restart` dan `hermes update` tidak memicu alarm), diulang tiap --remind-minutes (bawaan 60) selama masih mati,
  dan satu pesan PULIH saat hidup lagi. Catatan antar-jalan: <state>/gateway_watch.json (bawaan ~/.hermes-team/state).
- URL webhook dibaca dari env HERMES_WATCH_WEBHOOK_URL (unit systemd memuatnya dari ~/.hermes-team/gateway-watch.env;
  saat dijalankan manual berkas itu dibaca langsung). URL itu RAHASIA: tidak pernah dicetak, hanya nama host-nya.
  Isi yang dikirim: JSON {"content": "<pesan>"} — bentuk webhook Discord; layanan lain yang menerima bentuk itu juga bisa.
  Hanya https:// (http:// hanya ke localhost). Tanpa URL: hanya mencetak (terbaca di journal).

BATAS — baca sebelum mengandalkannya:
- Berjalan di server yang sama. Server mati, jaringan putus, atau systemd --user milik user Hermes tidak hidup
  (linger mati) → TIDAK ADA pesan. Untuk itu butuh pemantau dari luar server.
- Hanya memeriksa proses/unit. Gateway yang hidup tetapi tidak tersambung ke Discord (token dicabut, intent mati
  setelah start) tetap terlihat "active".
- BELUM pernah dijalankan terhadap systemd dan webhook Discord sungguhan; diuji dengan runner dan HTTP tiruan.

Hanya pustaka standar. Kode keluar: 0 = gateway hidup (atau --test terkirim); 1 = gateway mati;
2 = keadaan tidak bisa ditentukan (systemctl tidak menjawab) atau salah pakai / --test gagal.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Tuple

ENV_URL = "HERMES_WATCH_WEBHOOK_URL"
ENV_FILE_NAME = "gateway-watch.env"
STATE_NAME = "gateway_watch.json"
DEFAULT_UNIT = "hermes-gateway.service"
UNIT_RE = re.compile(r"^hermes-gateway(-[a-z0-9_-]{1,64})?\.service$")
DOWN_WORDS = {"inactive", "failed", "activating", "deactivating", "reloading", "maintenance"}
USER_AGENT = "hermes-team-kit-gateway-watch/1"
CONTENT_LIMIT = 1900           # batas Discord 2000
Runner = Callable[[list], Tuple[int, str]]
Poster = Callable[[str, dict], Tuple[bool, str]]


def team_dir() -> Path:
    return Path(os.environ.get("HERMES_TEAM_DIR") or Path.home() / ".hermes-team")


def run_command(argv: list) -> Tuple[int, str]:
    try:
        proc = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=30)
    except FileNotFoundError:
        return 127, f"perintah tidak ditemukan: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "melewati batas waktu"
    return proc.returncode, proc.stdout or ""


def find_unit(unit_dir: Path) -> str:
    """Nama unit gateway yang terpasang: bawaan, atau satu-satunya varian berakhiran profil/hash."""
    try:
        found = sorted(n for n in os.listdir(unit_dir) if UNIT_RE.match(n))
    except OSError:
        found = []
    if DEFAULT_UNIT in found or len(found) != 1:
        return DEFAULT_UNIT
    return found[0]


def gateway_state(run: Runner, unit: str) -> Tuple[str, str]:
    """("up" | "down" | "unknown", kata status systemd atau penjelasan)."""
    code, out = run(["systemctl", "--user", "is-active", unit])
    word = (out.strip().splitlines() or [""])[0].strip().lower()
    if word == "active":
        return "up", word
    if word in DOWN_WORDS:
        return "down", word
    return "unknown", f"systemctl --user is-active {unit} → kode {code}: {word[:120] or '(tanpa keluaran)'}"


def read_env_file(path: Path) -> dict:
    """KEY=value sederhana (baris kosong dan # diabaikan; kutip di tepi nilai dibuang)."""
    values = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def webhook_problem(url: str) -> Optional[str]:
    """Alasan URL ditolak, atau None. Pesannya TIDAK memuat URL (rahasia)."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or parts.scheme not in ("http", "https"):
        return "URL webhook harus berupa https://host/…"
    if parts.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        return f"URL webhook ke {host} memakai http://; hanya https:// yang diterima"
    return None


def url_host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or "?"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):      # noqa: D401 — None = jangan ikuti
        return None


def post_webhook(url: str, payload: dict) -> Tuple[bool, str]:
    """POST JSON. (berhasil, keterangan tanpa URL). Redirect tidak diikuti: URL-nya memuat token."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=15) as resp:
            return True, f"HTTP {getattr(resp, 'status', 200)}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 — jaringan/DNS/TLS: semuanya "tidak terkirim"
        return False, type(exc).__name__


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def down_message(host: str, unit: str, word: str, since: float, reminder: bool) -> str:
    head = "[MASIH MATI]" if reminder else "[MATI]"
    return (f"{head} Gateway Hermes di server `{host}` tidak berjalan (unit `{unit}`: {word}) sejak ±{_stamp(since)}. "
            "Bot tidak menjawab siapa pun.\n"
            f"Periksa sebagai user Hermes: `systemctl --user status {unit}` · `hermes logs gateway -n 80` · "
            "`hermes gateway start`\nRunbook: docs/OPERASI.md, bagian \"Gateway mati\".")[:CONTENT_LIMIT]


def up_message(host: str, unit: str, since: float, now: float) -> str:
    minutes = max(1, int((now - since) // 60))
    return f"[PULIH] Gateway Hermes di server `{host}` hidup lagi (unit `{unit}`; mati ±{minutes} menit)."


def decide(prev: dict, status: str, word: str, now: float, confirm_runs: int, remind_seconds: float,
           host: str, unit: str) -> Tuple[dict, Optional[str]]:
    """Keadaan baru + pesan yang perlu dikirim (None = tidak ada). Murni: tanpa I/O."""
    if status == "up":
        message = None
        if prev.get("alerted"):
            message = up_message(host, unit, float(prev.get("down_since") or now), now)
        return {"status": "up", "checked_at": now}, message
    runs = int(prev.get("down_runs") or 0) + 1 if prev.get("status") == "down" else 1
    since = float(prev.get("down_since") or now) if prev.get("status") == "down" else now
    state = {"status": "down", "word": word, "down_since": since, "down_runs": runs, "checked_at": now,
             "alerted": bool(prev.get("alerted")) if prev.get("status") == "down" else False,
             "last_alert": float(prev.get("last_alert") or 0) if prev.get("status") == "down" else 0.0}
    message = None
    if runs >= confirm_runs:
        if not state["alerted"]:
            message = down_message(host, unit, word, since, reminder=False)
        elif remind_seconds > 0 and now - state["last_alert"] >= remind_seconds:
            message = down_message(host, unit, word, since, reminder=True)
    return state, message


def watch(*, run: Runner, post: Poster, url: str, unit: str, state_path: Path, now: float, host: str,
          confirm_runs: int = 2, remind_minutes: float = 60, dry_run: bool = False,
          out: Callable[[str], None] = print) -> int:
    status, word = gateway_state(run, unit)
    if status == "unknown":
        out(f"? keadaan gateway tidak bisa ditentukan: {word}")
        out("  (jalankan sebagai user Hermes dengan systemd --user yang hidup; periksa `loginctl enable-linger`)")
        return 2
    prev = load_state(state_path)
    state, message = decide(prev, status, word, now, max(1, confirm_runs), remind_minutes * 60, host, unit)
    out(f"{'✓' if status == 'up' else '✗'} {unit}: {word}"
        + (f" (pemeriksaan ke-{state['down_runs']} berturut-turut)" if status == "down" else ""))
    if message:
        if dry_run:
            out("(dry-run) pesan yang akan dikirim:\n" + message)
        elif not url:
            out(f"  {ENV_URL} belum diisi: tidak ada pesan yang dikirim. Isi pesannya:\n" + message)
        else:
            problem = webhook_problem(url)
            ok, detail = (False, problem) if problem else post(url, {"content": message})
            out(f"  {'✓ pesan terkirim' if ok else '✗ pesan TIDAK terkirim'} ke {url_host(url)} ({detail})")
            if ok and status == "down":
                state["alerted"], state["last_alert"] = True, now
            elif not ok and status == "up":
                state = dict(prev, checked_at=now)        # pesan PULIH belum terkirim: coba lagi di jalan berikutnya
    if not dry_run:
        save_state(state_path, state)
    return 0 if status == "up" else 1


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Periksa gateway Hermes; kabari webhook bila mati (lihat docstring).")
    ap.add_argument("--unit", help=f"nama unit systemd user (bawaan: dicari; biasanya {DEFAULT_UNIT})")
    ap.add_argument("--state-dir", help="folder catatan antar-jalan (bawaan: ~/.hermes-team/state)")
    ap.add_argument("--env-file", help=f"berkas KEY=value berisi {ENV_URL} (bawaan: ~/.hermes-team/{ENV_FILE_NAME})")
    ap.add_argument("--confirm-runs", type=int, default=2, help="mati sekian pemeriksaan berturut-turut baru dikabarkan (bawaan 2)")
    ap.add_argument("--remind-minutes", type=float, default=60, help="ulangi pesan tiap sekian menit selama mati (0 = sekali saja)")
    ap.add_argument("--test", action="store_true", help="kirim pesan uji ke webhook, lalu keluar")
    ap.add_argument("--dry-run", action="store_true", help="periksa dan cetak pesannya; tidak mengirim, tidak mencatat")
    args = ap.parse_args(argv)
    base = team_dir()
    env_file = Path(args.env_file) if args.env_file else base / ENV_FILE_NAME
    url = (os.environ.get(ENV_URL) or read_env_file(env_file).get(ENV_URL) or "").strip()
    host = socket.gethostname()
    if args.test:
        if not url:
            print(f"{ENV_URL} belum diisi (env, atau berkas {env_file}).", file=sys.stderr)
            return 2
        problem = webhook_problem(url)
        ok, detail = (False, problem) if problem else post_webhook(
            url, {"content": f"[UJI] Pemantau gateway Hermes di server `{host}` bisa mengirim pesan ke sini."})
        print(f"{'✓ pesan uji terkirim' if ok else '✗ pesan uji TIDAK terkirim'} ke {url_host(url)} ({detail})")
        return 0 if ok else 2
    unit = args.unit or find_unit(Path.home() / ".config" / "systemd" / "user")
    state_dir = Path(args.state_dir) if args.state_dir else base / "state"
    return watch(run=run_command, post=post_webhook, url=url, unit=unit, state_path=state_dir / STATE_NAME,
                 now=time.time(), host=host, confirm_runs=args.confirm_runs, remind_minutes=args.remind_minutes,
                 dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
