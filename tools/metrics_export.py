#!/usr/bin/env python3
"""metrics_export.py — ubah <folder tim>/state/metrics.json menjadi format teks Prometheus.

Untuk textfile collector milik node_exporter: jalankan berkala (cron/timer, mis. tiap menit) dengan
`--output /var/lib/node_exporter/textfile_collector/hermes_team.prom`. metrics.json ditulis plugin
team-guard (plugins/team-guard/scale.py, kelas Monitor) tiap beberapa detik selama gateway hidup.

Berkas keluaran bermode 0644 supaya node_exporter (user lain) bisa membacanya. ISINYA HANYA ANGKA dan
kunci pemakai buku besar di label `user` (ID user Discord, atau `profile:<nama>` untuk sesi tanpa
pengirim) — tidak ada nama orang, isi percakapan, token, maupun secret. metrics.json sendiri bermode
0600, jadi skrip ini harus dijalankan oleh user pemilik folder tim (user hermes), dan folder textfile
collector harus bisa ditulisi user itu. Nama berkas keluaran harus berakhiran `.prom`.

Metrik:
  hermes_team_up                       1 = metrics.json ada, terbaca, dan segar; 0 = tidak
  hermes_team_metrics_age_seconds      umur metrics.json
  hermes_team_active_sessions, hermes_team_llm_inflight, hermes_team_llm_inflight_peak_5m,
  hermes_team_queue_length, hermes_team_queue_wait_p95_seconds, hermes_team_http_429_last_minute,
  hermes_team_effective_max_active, hermes_team_configured_max_active, hermes_team_throttled (0/1),
  hermes_team_refused_total{reason="rate|queue_full|budget|user_concurrent"}   (counter; nol lagi saat gateway restart)
  hermes_team_budget_used_tokens{user="<id>"}, hermes_team_budget_team_used_tokens

Bila metrics.json lebih tua dari --max-age detik (bawaan 120; gateway mati atau macet), hanya umur dan
`hermes_team_up 0` yang dikeluarkan supaya angka basi tidak tampak seperti keadaan sekarang. Nilai yang
hilang atau bukan angka dilewati, tidak membuat skrip gagal.

Pakai:
  python3 tools/metrics_export.py                                   # ke stdout
  python3 tools/metrics_export.py --output /var/lib/node_exporter/textfile_collector/hermes_team.prom
  python3 tools/metrics_export.py --input /lain/metrics.json --max-age 300

Hanya pustaka standar. Kode keluar: 0 (juga bila metrics.json tidak ada / rusak / basi → `hermes_team_up 0`);
1 hanya bila --output tidak bisa ditulis; 2 = argumen salah.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

PREFIX = "hermes_team_"
DEFAULT_MAX_AGE = 120.0
# (kunci metrics.json, nama metrik, HELP)
GAUGES = (
    ("active_sessions", "active_sessions", "Sesi yang sedang dilayani"),
    ("llm_inflight", "llm_inflight", "Permintaan LLM yang sedang berjalan"),
    ("llm_inflight_peak_5m", "llm_inflight_peak_5m", "Puncak permintaan LLM paralel dalam 5 menit terakhir"),
    ("queue_len", "queue_length", "Pesan yang menunggu di antrean"),
    ("queue_wait_p95_s", "queue_wait_p95_seconds", "p95 lama menunggu di antrean (detik)"),
    ("http_429_last_minute", "http_429_last_minute", "Jawaban 429 dari endpoint LLM dalam 1 menit terakhir"),
    ("effective_max_active", "effective_max_active", "Batas sesi aktif yang sedang berlaku (setelah pembatas adaptif)"),
    ("configured_max_active", "configured_max_active", "Batas sesi aktif menurut konfigurasi"),
)


# ----------------------------------------------------------------------------- format eksposisi
def number(value: Any) -> Optional[float]:
    """int/float berhingga → nilai; bool, teks, None, NaN, Inf → None (dilewati)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    return value


def fmt_value(value: float) -> str:
    if isinstance(value, int):
        return str(value)
    return str(int(value)) if value == int(value) and abs(value) < 1e15 else repr(float(value))


def escape_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


class Exposition:
    def __init__(self) -> None:
        self.lines: list = []

    def family(self, name: str, kind: str, help_text: str, samples: list) -> None:
        """samples: [(labels dict, nilai)]. Keluarga tanpa sampel tidak ditulis."""
        if not samples:
            return
        name = PREFIX + name
        self.lines.append(f"# HELP {name} {escape_help(help_text)}")
        self.lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            label_text = ",".join(f'{key}="{escape_label(val)}"' for key, val in labels.items())
            self.lines.append(f"{name}{{{label_text}}} {fmt_value(value)}" if label_text else f"{name} {fmt_value(value)}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def render(data: Any, age: Optional[float], max_age: float = DEFAULT_MAX_AGE) -> str:
    """Logika murni. `data`: isi metrics.json (atau None bila tidak ada/rusak); `age`: umurnya (detik)
    atau None bila tidak diketahui."""
    exp = Exposition()
    fresh = isinstance(data, dict) and age is not None and age <= max_age
    exp.family("up", "gauge", "1 bila metrics.json ada, terbaca, dan segar; 0 bila tidak", [({}, 1 if fresh else 0)])
    if age is not None:
        exp.family("metrics_age_seconds", "gauge", "Umur metrics.json (detik)", [({}, round(float(age), 3))])
    if not fresh:
        return exp.text()
    for key, name, help_text in GAUGES:
        value = number(data.get(key))
        if value is not None:
            exp.family(name, "gauge", help_text, [({}, value)])
    throttled = data.get("throttled")
    if isinstance(throttled, bool) or throttled in (0, 1):
        exp.family("throttled", "gauge", "1 bila pembatas adaptif sedang menurunkan batas", [({}, int(bool(throttled)))])
    refused = data.get("refused") if isinstance(data.get("refused"), dict) else {}
    exp.family("refused_total", "counter", "Pesan yang ditolak sejak gateway hidup, per alasan",
               [({"reason": str(reason)}, number(count)) for reason, count in sorted(refused.items(), key=lambda kv: str(kv[0]))
                if number(count) is not None and number(count) >= 0])
    budget = data.get("budget") if isinstance(data.get("budget"), dict) else {}
    used = budget.get("used_by_user") if isinstance(budget.get("used_by_user"), dict) else {}
    exp.family("budget_used_tokens", "gauge", "Token terpakai hari ini per pemakai (ID user Discord)",
               [({"user": str(user)}, number(count)) for user, count in sorted(used.items(), key=lambda kv: str(kv[0]))
                if number(count) is not None])
    team_used = number(budget.get("team_used"))
    if team_used is not None:
        exp.family("budget_team_used_tokens", "gauge", "Token terpakai hari ini oleh seluruh tim", [({}, team_used)])
    return exp.text()


# ----------------------------------------------------------------------------- berkas
def read_metrics(path: Path, now: float) -> tuple:
    """→ (data atau None, umur detik atau None). Umur dari `ts` di dalam berkas; bila `ts` tidak ada
    atau berkas rusak, dari waktu ubah berkas."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
        mtime: Optional[float] = path.stat().st_mtime
    except OSError:
        return None, None
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = None
    stamp = number(data.get("ts")) if data else None
    if stamp is None:
        stamp = mtime
    return data, max(0.0, now - stamp)


def write_atomic(path: Path, text: str) -> None:
    """tmp + rename di folder yang sama, mode 0644 (node_exporter membacanya sebagai user lain). Nama
    berkas sementara tidak berakhiran .prom, jadi tidak pernah terbaca setengah jadi."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ----------------------------------------------------------------------------- main
def main(argv: Optional[list] = None, *, now: Optional[float] = None, out=None) -> int:
    team_dir = Path(os.environ.get("HERMES_TEAM_DIR") or "~/.hermes-team").expanduser()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", default="", help="bawaan: ~/.hermes-team/state/metrics.json")
    ap.add_argument("--output", default="", help="berkas .prom (ditulis atomik, 0644); bawaan: stdout")
    ap.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE,
                    help="detik; lebih tua dari ini → hanya umur + hermes_team_up 0 (bawaan 120)")
    args = ap.parse_args(argv)
    if not args.max_age >= 0:
        print("metrics_export: --max-age tidak boleh negatif", file=sys.stderr)
        return 2
    source = Path(args.input).expanduser() if args.input else team_dir / "state" / "metrics.json"
    data, age = read_metrics(source, time.time() if now is None else now)
    text = render(data, age, args.max_age)
    if not args.output:
        (out or sys.stdout).write(text)
        return 0
    try:
        write_atomic(Path(args.output).expanduser(), text)
    except OSError as exc:
        print(f"metrics_export: {args.output} tidak bisa ditulis ({type(exc).__name__}: {exc.strerror or exc})",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
