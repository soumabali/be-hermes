#!/usr/bin/env python3
"""probe_llm.py — ukur berapa permintaan paralel yang sanggup dilayani endpoint LLM (OpenAI-compatible).

Mengirim permintaan chat-completion pendek pada tingkat paralel 1, 2, 4, 8, 16, 32 (bisa diatur), mencatat
berhasil / 429 / error, latensi p50 dan p95, serta token per detik bila endpoint mengembalikan `usage`.
Berhenti menaikkan paralel setelah satu tingkat menghasilkan lebih dari 10% 429/error, lalu mencetak saran:

  max_llm_parallel        = tingkat BERSIH tertinggi (tanpa 429/error) dengan p95 ≤ 2× p95 tingkat dasar
  max_concurrent_sessions = max_llm_parallel ÷ (1 + delegation_max_children), dibulatkan ke bawah, minimal 1

Pakai:
  export TD_API_KEY=...        # key HANYA dibaca dari environment; tidak pernah lewat argumen, tidak dicetak
  python3 tools/probe_llm.py --base-url https://llm.example.internal/v1 --model td-simple --key-env TD_API_KEY
  python3 tools/probe_llm.py --team ~/.hermes-team/team.yaml --alias td-simple        # ambil dari team.yaml
  python3 tools/probe_llm.py ... --levels 1,2,4,8 --requests-per-level 16 --max-tokens 32 --json

Catatan:
- Ini MEMAKAI kuota endpoint (permintaan sungguhan, walau pendek). Jalankan saat instalasi dan tiap ganti
  endpoint/kuota, di luar jam sibuk; angka yang didapat saat tim sedang memakai bot akan lebih rendah.
- Tingkat yang diuji melompat (1, 2, 4, …), jadi kapasitas sebenarnya bisa berada di antara dua tingkat;
  pakai --levels untuk memperhalus (mis. 8,10,12,14,16).
- Hanya pustaka standar Python. `--team` membutuhkan PyYAML (sudah dipakai kit) untuk membaca team.yaml.

Kode keluar: 0 = ada saran; 1 = tidak ada tingkat yang bersih (lihat tabel); 2 = argumen/konfigurasi salah.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

DEFAULT_LEVELS = (1, 2, 4, 8, 16, 32)
BAD_RATIO_STOP = 0.10          # >10% 429/error pada satu tingkat → berhenti menaikkan paralel
P95_FACTOR = 2.0               # p95 tingkat yang disarankan ≤ 2× p95 tingkat dasar
PROMPT = "Balas dengan satu kata: ok"


class ProbeError(Exception):
    """Argumen atau konfigurasi salah (kode keluar 2)."""


# ----------------------------------------------------------------------------- satu permintaan
def chat_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ProbeError("--base-url harus URL http(s), mis. https://llm.example.internal/v1")
    return base_url.strip().rstrip("/") + "/chat/completions"


def send_once(url: str, model: str, key: str, max_tokens: int, timeout: float) -> dict:
    """Satu permintaan. → {"status": "ok" | "429" | "error", "latency": detik, "tokens": int, "detail": str}.
    `detail` tidak pernah memuat key (hanya kode HTTP / jenis kesalahan)."""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": PROMPT}],
                       "max_tokens": max_tokens, "stream": False}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "probe-llm"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        latency = time.monotonic() - started
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {"status": "error", "latency": latency, "tokens": 0, "detail": "jawaban bukan JSON"}
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
            return {"status": "error", "latency": latency, "tokens": 0, "detail": "jawaban tanpa `choices`"}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        tokens = usage.get("completion_tokens")
        tokens = int(tokens) if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) else 0
        return {"status": "ok", "latency": latency, "tokens": tokens, "detail": ""}
    except urllib.error.HTTPError as exc:
        exc.close()
        status = "429" if exc.code == 429 else "error"
        return {"status": status, "latency": time.monotonic() - started, "tokens": 0, "detail": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 — timeout, DNS, TLS, koneksi ditolak: semuanya "error" untuk tabel
        return {"status": "error", "latency": time.monotonic() - started, "tokens": 0,
                "detail": type(exc).__name__}


# ----------------------------------------------------------------------------- satu tingkat paralel
def percentile(values: list, pct: float) -> Optional[float]:
    """Persentil nearest-rank. Daftar kosong → None."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(-(-pct * len(ordered) // 100)))     # ceil tanpa float
    return ordered[min(rank, len(ordered)) - 1]


def run_level(send: Callable[[], dict], parallel: int, requests: int) -> dict:
    """Jalankan `requests` permintaan dengan `parallel` thread yang selalu sibuk; kembalikan ringkasannya."""
    results: list[dict] = []
    lock = threading.Lock()
    remaining = [requests]

    def worker() -> None:
        while True:
            with lock:
                if remaining[0] <= 0:
                    return
                remaining[0] -= 1
            outcome = send()
            with lock:
                results.append(outcome)

    started = time.monotonic()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(parallel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = max(time.monotonic() - started, 1e-9)
    ok = [r for r in results if r["status"] == "ok"]
    limited = sum(1 for r in results if r["status"] == "429")
    errors = sum(1 for r in results if r["status"] == "error")
    tokens = sum(r["tokens"] for r in ok)
    details: dict = {}
    for r in results:
        if r["detail"]:
            details[r["detail"]] = details.get(r["detail"], 0) + 1
    latencies = [r["latency"] for r in ok]
    total = len(results)
    return {
        "parallel": parallel, "requests": total, "ok": len(ok), "http_429": limited, "errors": errors,
        "bad_ratio": (limited + errors) / total if total else 1.0,
        "p50_s": percentile(latencies, 50), "p95_s": percentile(latencies, 95),
        "tokens_per_s": round(tokens / wall, 2) if tokens else None,
        "wall_s": round(wall, 3), "details": details,
    }


def probe(send: Callable[[], dict], levels: list, requests_per_level: int = 0,
          progress: Optional[Callable[[dict], None]] = None) -> list:
    """Naikkan paralel tingkat demi tingkat; berhenti setelah tingkat pertama dengan >10% 429/error."""
    out = []
    for parallel in levels:
        level = run_level(send, parallel, requests_per_level or max(4, parallel * 2))
        out.append(level)
        if progress:
            progress(level)
        if level["bad_ratio"] > BAD_RATIO_STOP:
            break
    return out


def recommend(levels: list, delegation_max_children: int = 1) -> dict:
    """Saran dari hasil ukur. Tingkat bersih = tanpa 429 dan tanpa error. Dasar = p95 tingkat pertama."""
    children = max(0, int(delegation_max_children))
    base = levels[0] if levels else None
    result = {"max_llm_parallel": None, "max_concurrent_sessions": None, "baseline_p95_s": None,
              "delegation_max_children": children, "reason": ""}
    if not base or base["ok"] == 0 or base["http_429"] or base["errors"] or base["p95_s"] is None:
        result["reason"] = ("tingkat dasar (paralel terendah) sudah bermasalah — periksa URL, model, key, "
                            "atau kuota endpoint sebelum mengukur lagi")
        return result
    result["baseline_p95_s"] = base["p95_s"]
    limit = base["p95_s"] * P95_FACTOR
    clean = [lv for lv in levels if lv["ok"] and not lv["http_429"] and not lv["errors"]
             and lv["p95_s"] is not None and lv["p95_s"] <= limit]
    best = max(lv["parallel"] for lv in clean)
    result["max_llm_parallel"] = best
    result["max_concurrent_sessions"] = max(1, best // (1 + children))
    tested = max(lv["parallel"] for lv in levels)
    if best == tested and levels[-1]["bad_ratio"] <= BAD_RATIO_STOP:
        result["reason"] = (f"paralel tertinggi yang diuji ({tested}) masih bersih; kapasitas sebenarnya bisa "
                            "lebih besar (uji --levels yang lebih tinggi bila perlu)")
    else:
        result["reason"] = f"tingkat bersih tertinggi dengan p95 ≤ {P95_FACTOR:g}× dasar ({limit:.2f} dtk)"
    return result


# ----------------------------------------------------------------------------- keluaran
def _sec(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"


def render_table(levels: list) -> str:
    head = ("paralel", "request", "ok", "429", "error", "p50 dtk", "p95 dtk", "token/dtk")
    rows = [(str(lv["parallel"]), str(lv["requests"]), str(lv["ok"]), str(lv["http_429"]), str(lv["errors"]),
             _sec(lv["p50_s"]), _sec(lv["p95_s"]),
             "-" if lv["tokens_per_s"] is None else f"{lv['tokens_per_s']:.1f}") for lv in levels]
    widths = [max(len(head[i]), *(len(r[i]) for r in rows)) if rows else len(head[i]) for i in range(len(head))]
    lines = ["  ".join(h.rjust(w) for h, w in zip(head, widths)), "  ".join("-" * w for w in widths)]
    lines += ["  ".join(c.rjust(w) for c, w in zip(row, widths)) for row in rows]
    for lv in levels:
        if lv["details"]:
            lines.append(f"  paralel {lv['parallel']}: " + ", ".join(f"{k} ×{n}" for k, n in sorted(lv["details"].items())))
    return "\n".join(lines)


def render_advice(advice: dict) -> str:
    if advice["max_llm_parallel"] is None:
        return "Tidak ada saran: " + advice["reason"] + "."
    return (f"Saran untuk team.yaml → scale ({advice['reason']}):\n"
            f"  max_llm_parallel: {advice['max_llm_parallel']}\n"
            f"  max_concurrent_sessions: {advice['max_concurrent_sessions']}"
            f"        # {advice['max_llm_parallel']} ÷ (1 + delegation_max_children {advice['delegation_max_children']})")


# ----------------------------------------------------------------------------- argumen
def parse_levels(text: str) -> list:
    try:
        levels = sorted({int(part) for part in str(text).split(",") if part.strip()})
    except ValueError:
        raise ProbeError("--levels harus daftar bilangan bulat dipisah koma, mis. 1,2,4,8")
    if not levels or levels[0] < 1 or levels[-1] > 256:
        raise ProbeError("--levels: tiap tingkat harus 1–256")
    return levels


def target_from_team(team_file: str, alias: str) -> dict:
    """base URL, nama model, nama env key, dan delegation_max_children dari team.yaml (lewat hermes_team)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import hermes_team as ht
    try:
        team = ht.load_team(Path(team_file).expanduser())
    except ht.TeamError as exc:
        raise ProbeError(f"team.yaml: {exc}")
    llm = team["llm"]
    if not llm:
        raise ProbeError("team.yaml belum punya bagian llm")
    alias = alias or llm["default_model"]
    if alias not in llm["models"]:
        raise ProbeError(f"alias '{alias}' tidak ada di llm.models (pilihan: {', '.join(llm['models'])})")
    model = llm["models"][alias]
    if not model["provider"].startswith("custom:"):
        raise ProbeError(f"alias '{alias}' memakai provider {model['provider']} yang bukan llm.providers; "
                         "pakai --base-url/--model/--key-env")
    provider = llm["providers"][model["provider"][7:]]
    return {"base_url": str(provider["api"]), "model": model["model"], "key_env": str(provider.get("key_env") or ""),
            "delegation_max_children": int(team["scale"]["delegation_max_children"])}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="", help="base URL OpenAI-compatible, mis. https://llm.example.internal/v1")
    ap.add_argument("--model", default="", help="nama model di endpoint")
    ap.add_argument("--key-env", default="", help="NAMA variabel environment berisi API key (bukan key-nya)")
    ap.add_argument("--team", default="", help="team.yaml: ambil base URL, model, dan key_env dari sini")
    ap.add_argument("--alias", default="", help="alias model di llm.models (bersama --team; bawaan: default_model)")
    ap.add_argument("--levels", default=",".join(map(str, DEFAULT_LEVELS)), help="tingkat paralel, dipisah koma")
    ap.add_argument("--requests-per-level", type=int, default=0,
                    help="jumlah permintaan per tingkat (bawaan: 2× paralel, minimal 4)")
    ap.add_argument("--max-tokens", type=int, default=16, help="max_tokens tiap permintaan (bawaan 16)")
    ap.add_argument("--timeout", type=float, default=60.0, help="batas waktu satu permintaan, detik")
    ap.add_argument("--delegation-max-children", type=int, default=None,
                    help="subagent per giliran untuk menghitung saran sesi (bawaan: dari team.yaml, atau 1)")
    ap.add_argument("--json", action="store_true", help="keluaran JSON")
    return ap


def main(argv: Optional[list] = None, env: Optional[dict] = None) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    try:
        children = args.delegation_max_children
        base_url, model, key_env = args.base_url, args.model, args.key_env
        if args.team:
            target = target_from_team(args.team, args.alias)
            base_url, model = base_url or target["base_url"], model or target["model"]
            key_env = key_env or target["key_env"]
            children = target["delegation_max_children"] if children is None else children
        elif args.alias:
            raise ProbeError("--alias hanya dipakai bersama --team")
        if not base_url or not model:
            raise ProbeError("isi --base-url dan --model, atau --team (dengan --alias)")
        url = chat_url(base_url)
        key = ""
        if key_env:
            if not key_env.replace("_", "").isalnum() or key_env[0].isdigit():
                raise ProbeError("--key-env harus NAMA variabel environment, bukan nilainya")
            key = str(env.get(key_env) or "").strip()
            if not key:
                raise ProbeError(f"variabel environment {key_env} kosong; export dulu (key tidak diterima lewat argumen)")
        levels = parse_levels(args.levels)
        if args.requests_per_level < 0 or not 1 <= args.max_tokens <= 4096 or args.timeout <= 0:
            raise ProbeError("--requests-per-level ≥ 0, --max-tokens 1–4096, --timeout > 0")
        children = 1 if children is None else children
        if children < 0:
            raise ProbeError("--delegation-max-children tidak boleh negatif")
    except ProbeError as exc:
        print(f"probe_llm: {exc}", file=sys.stderr)
        return 2

    def send() -> dict:
        return send_once(url, model, key, args.max_tokens, args.timeout)

    def progress(level: dict) -> None:
        if not args.json:
            print(f"  paralel {level['parallel']}: {level['ok']}/{level['requests']} ok, "
                  f"{level['http_429']} ×429, {level['errors']} error", file=sys.stderr, flush=True)

    if not args.json:
        print(f"Mengukur {url} (model {model}; key dari "
              f"{'env ' + key_env if key_env else 'tidak ada'}) pada paralel {', '.join(map(str, levels))} …",
              file=sys.stderr, flush=True)
    results = probe(send, levels, args.requests_per_level, progress)
    advice = recommend(results, children)
    if args.json:
        print(json.dumps({"url": url, "model": model, "key_env": key_env, "levels": results,
                          "recommendation": advice}, indent=2, ensure_ascii=False))
    else:
        print(render_table(results))
        if results and results[-1]["bad_ratio"] > BAD_RATIO_STOP and results[-1]["parallel"] != levels[-1]:
            print(f"\nBerhenti di paralel {results[-1]['parallel']}: lebih dari {BAD_RATIO_STOP * 100:.0f}% "
                  "permintaan mendapat 429/error.")
        print("\n" + render_advice(advice))
    return 0 if advice["max_llm_parallel"] is not None else 1


if __name__ == "__main__":
    sys.exit(main())
