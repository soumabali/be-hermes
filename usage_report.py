#!/usr/bin/env python3
"""Laporan pemakaian LLM per anggota (token & biaya) dari state.db tiap profile Hermes.

Hanya membaca (SQLite mode read-only); tidak mengubah apa pun dan tidak memuat isi percakapan.

  python3 usage_report.py                       # 7 hari terakhir, teks
  python3 usage_report.py --days 30 --format markdown
  python3 usage_report.py --format json --members-file ~/.hermes-team/members.yaml

Tabel sumber: `session_model_usage` (Hermes ≥ schema v20). Profile tanpa tabel itu dilewati dan
dilaporkan di bagian "dilewati". `last_seen` dipakai sebagai penanda waktu, jadi sesi yang
berjalan melewati batas periode dihitung penuh pada periode tempat ia terakhir aktif.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

COLUMNS = ("api_call_count", "input_tokens", "output_tokens", "cache_read_tokens",
           "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd")


def _profile_dirs(root: Path) -> list[tuple[str, Path]]:
    found = [("default", root)] if (root / "state.db").exists() else []
    base = root / "profiles"
    if base.is_dir():
        found += [(p.name, p) for p in sorted(base.iterdir()) if (p / "state.db").exists()]
    return found


def _usage_for(db: Path, since: float, until: float) -> dict[str, dict]:
    """Agregasi per model untuk satu state.db. Lempar sqlite3.Error bila tabel tidak ada."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT model, task, " + ", ".join(f"SUM({c}) AS {c}" for c in COLUMNS) +
            " FROM session_model_usage WHERE COALESCE(last_seen, first_seen, 0) >= ? "
            "AND COALESCE(last_seen, first_seen, 0) < ? GROUP BY model, task",
            (since, until)).fetchall()
    finally:
        con.close()
    out: dict[str, dict] = {}
    for r in rows:
        m = out.setdefault(r["model"], {c: 0 for c in COLUMNS})
        for c in COLUMNS:
            m[c] += r[c] or 0
    return out


def _cost(row: dict) -> float:
    """Biaya sebenarnya bila tersedia, kalau tidak estimasi."""
    return row["actual_cost_usd"] or row["estimated_cost_usd"] or 0.0


def build_report(root: Path, days: float = 7, now: Optional[float] = None,
                 owners: Optional[dict[str, str]] = None) -> dict:
    """Kembalikan {period, profiles: [...], totals, skipped}. `owners`: profile → nama tampilan."""
    now = time.time() if now is None else now
    since = now - days * 86400
    profiles, skipped = [], []
    for name, home in _profile_dirs(Path(root).expanduser()):
        try:
            per_model = _usage_for(home / "state.db", since, now)
        except sqlite3.Error as exc:
            skipped.append({"profile": name, "reason": str(exc)})
            continue
        total = {c: sum(m[c] for m in per_model.values()) for c in COLUMNS}
        total["cost_usd"] = round(sum(_cost(m) for m in per_model.values()), 6)
        profiles.append({"profile": name, "owner": (owners or {}).get(name, ""), "total": total,
                         "models": {k: {**v, "cost_usd": round(_cost(v), 6)} for k, v in per_model.items()}})
    profiles.sort(key=lambda p: p["total"]["cost_usd"], reverse=True)
    grand = {c: sum(p["total"][c] for p in profiles) for c in (*COLUMNS, "cost_usd")}
    grand["cost_usd"] = round(grand["cost_usd"], 6)
    return {"period": {"days": days, "since": since, "until": now}, "profiles": profiles,
            "totals": grand, "skipped": skipped}


def _fmt_tokens(n: float) -> str:
    n = int(n)
    return f"{n / 1_000_000:.2f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def render(report: dict, fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps(report, indent=2, ensure_ascii=False)
    rows = [("Profile", "Pemilik", "Panggilan", "Token masuk", "Token keluar", "Biaya (USD)")]
    for p in report["profiles"]:
        t = p["total"]
        rows.append((p["profile"], p["owner"] or "-", str(int(t["api_call_count"])),
                     _fmt_tokens(t["input_tokens"]), _fmt_tokens(t["output_tokens"]), f"{t['cost_usd']:.4f}"))
    g = report["totals"]
    rows.append(("TOTAL", "", str(int(g["api_call_count"])), _fmt_tokens(g["input_tokens"]),
                 _fmt_tokens(g["output_tokens"]), f"{g['cost_usd']:.4f}"))
    head = f"Pemakaian LLM {report['period']['days']:g} hari terakhir"
    if fmt == "markdown":
        lines = [f"**{head}**", "", "| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    else:
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        lines = [head, ""] + ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in rows]
    if report["skipped"]:
        lines += ["", "Dilewati: " + ", ".join(f"{s['profile']} ({s['reason']})" for s in report["skipped"])]
    return "\n".join(lines)


def _owners_from(members_file: Optional[str]) -> dict[str, str]:
    if not members_file:
        return {}
    try:
        import yaml
        data = yaml.safe_load(Path(members_file).expanduser().read_text(encoding="utf-8")) or {}
    except (OSError, ImportError, ValueError):
        return {}
    prefix = str(data.get("profile_prefix") or "be-")
    out = {}
    for m in data.get("members") or []:
        if isinstance(m, dict) and m.get("name"):
            out[str(m.get("profile") or f"{prefix}{m['name']}")] = str(m.get("display_name") or m["name"])
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hermes-root", default=os.environ.get("HERMES_HOME") or "~/.hermes")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    ap.add_argument("--members-file", help="members.yaml untuk menampilkan nama pemilik profile")
    args = ap.parse_args(argv)
    if args.days <= 0:
        ap.error("--days harus > 0")
    print(render(build_report(Path(args.hermes_root), args.days, owners=_owners_from(args.members_file)),
                 args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
