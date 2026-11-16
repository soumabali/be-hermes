#!/usr/bin/env python3
"""hermes_contract.py — kontrak antara kit ini dan installer + CLI Hermes hulu, di SATU tempat.

Wizard (kit_setup.py) dan kit (kit/hermes_cli.py, kit/membership.py) memanggil installer dan CLI Hermes dengan flag
dan subperintah tertentu. Uji unit hanya membuktikannya terhadap TIRUAN. Berkas ini memuat daftar itu (CLI_CONTRACT,
SOURCE_CONTRACT) dan memeriksanya terhadap Hermes ASLI:

  python3 tools/hermes_contract.py --list                      # cetak kontraknya
  python3 tools/hermes_contract.py                             # periksa Hermes milik user yang menjalankan
  sudo python3 tools/hermes_contract.py --user hermes          # periksa Hermes milik user lain (lewat runuser, seperti wizard)
  sudo python3 tools/hermes_contract.py --install --user X     # HANYA untuk CI / mesin sekali pakai: jalankan installer
                                                               # hulu PERSIS seperti wizard, lalu periksa

Yang diperiksa: launcher ada di tempat yang dicari wizard; `hermes --version` jalan; commit yang terpasang sama dengan
HERMES_REF (bila dipin); tiap subperintah/flag di CLI_CONTRACT diterima (`--help` keluar 0 dan memuat flag-nya — gateway
TIDAK dijalankan); tiap teks di SOURCE_CONTRACT (nama unit systemd, kalimat `hermes gateway status` yang ditafsir wizard)
masih ada di sumber Hermes.

Dipakai oleh CI job `installer-contract` (.github/workflows/ci.yml) dan oleh tests/test_setup.py (yang memastikan tiap
perintah `hermes …` yang dirangkai wizard tercantum di sini). Menambah panggilan CLI Hermes baru di wizard/kit = tambah
barisnya di sini. `--install` MENGUBAH mesin (memasang Hermes untuk user itu): jangan jalankan di server tim.

Hanya pustaka standar + kit_setup (PyYAML). Kode keluar: 0 = semua terpenuhi; 1 = ada yang dilanggar; 2 = salah pakai.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional, Tuple

KIT = Path(__file__).resolve().parent.parent
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

import kit_setup as ks  # noqa: E402

Runner = Callable[[List[str]], Tuple[int, str]]      # (argv) → (kode keluar, stdout + stderr)


class CliCheck(NamedTuple):
    argv: Tuple[str, ...]          # argumen setelah `hermes`
    needles: Tuple[str, ...]       # teks yang harus muncul di keluarannya
    used_by: str
    help: bool = True              # True = `--help` ditambahkan (perintahnya sendiri tidak dijalankan)


# Tiap subperintah/flag CLI Hermes yang dipakai wizard dan kit. Diperiksa lewat `--help`, kecuali yang help=False
# (perintah baca-saja yang aman dijalankan).
CLI_CONTRACT: Tuple[CliCheck, ...] = (
    CliCheck(("--version",), (), "wizard: hermes_install (versi tercatat di state)", help=False),
    CliCheck((), ("-p",), "kit/hermes_cli.py: `hermes -p <profile> …` (flag global profile)"),
    CliCheck(("doctor",), (), "wizard: hermes_install; docs/INSTALL.md"),
    CliCheck(("gateway", "install"), (), "wizard: gateway"),
    CliCheck(("gateway", "start"), (), "wizard: gateway"),
    CliCheck(("gateway", "status"), (), "wizard: gateway (teksnya ditafsir parse_gateway_status)"),
    CliCheck(("gateway", "stop"), (), "docs/OPERASI.md: backup, pindah server"),
    CliCheck(("gateway", "restart"), (), "docs/OPERASI.md; pesan kit setelah `managed --install`"),
    CliCheck(("gateway", "run"), (), "wizard: --no-systemd (perintah manual yang dicetak)"),
    CliCheck(("logs",), ("-n",), "wizard: gateway gagal → `hermes logs gateway -n 40`"),
    CliCheck(("profile", "create"), ("--clone-from", "--no-alias"), "kit/hermes_cli.py: create()"),
    CliCheck(("profile", "delete"), ("--yes",), "kit/membership.py: remove-member --delete-profile"),
    CliCheck(("config", "set"), (), "kit/hermes_cli.py: config_set()"),
    CliCheck(("plugins", "enable"), (), "kit/hermes_cli.py: install_plugin()"),
    CliCheck(("update",), (), "docs/OPERASI.md: update Hermes"),
)

# Teks di SUMBER Hermes yang diandalkan wizard dan tidak bisa dibuktikan lewat `--help`.
# (berkas relatif terhadap checkout Hermes, teks, dipakai oleh)
SOURCE_CONTRACT: Tuple[Tuple[str, str, str], ...] = (
    # `hermes logs list` hanya menampilkan berkas log yang SUDAH ada (kosong pada instalasi baru), jadi nama log
    # diperiksa di sumbernya.
    ("hermes_cli/logs.py", '"gateway": "gateway.log"', "wizard: `hermes logs gateway -n 40` (nama log `gateway`)"),
    ("hermes_cli/gateway.py", '_SERVICE_BASE = "hermes-gateway"', f"wizard: Gateway.DEFAULT_UNIT = {ks.Gateway.DEFAULT_UNIT}"),
    ("hermes_cli/gateway.py", "gateway service is running", "wizard: parse_gateway_status → running"),
    ("hermes_cli/gateway.py", "gateway service is stopped", "wizard: parse_gateway_status → stopped"),
    ("hermes_cli/gateway.py", "Gateway service is not installed", "wizard: parse_gateway_status → not_installed"),
    ("hermes_cli/gateway.py", "Gateway is not running", "wizard: parse_gateway_status → stopped"),
    ("hermes_cli/gateway.py", "Restart pending", "wizard: parse_gateway_status → stopped"),
)


def cli_argv(check: CliCheck) -> List[str]:
    return ["hermes", *check.argv] + (["--help"] if check.help else [])


def covers(command: List[str]) -> bool:
    """Apakah perintah `hermes …` (argv) tercakup oleh satu baris CLI_CONTRACT (awalan argumen yang sama)?"""
    if command[:1] != ["hermes"]:
        return False
    rest = tuple(command[1:])
    return any(check.argv and rest[:len(check.argv)] == check.argv for check in CLI_CONTRACT)


def run_checks(run: Runner, home: Path, expect_commit: str = "", out: Callable[[str], None] = print) -> List[str]:
    """Periksa Hermes yang terpasang di `home`. Mengembalikan daftar pelanggaran (kosong = kontrak terpenuhi)."""
    failures: List[str] = []

    def record(ok: bool, label: str, detail: str = "") -> None:
        out(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(f"{label}: {detail}" if detail else label)

    candidates = ks.hermes_launcher_candidates(home)
    found = [p for p in candidates if p.exists()]
    record(bool(found), "launcher ada di tempat yang dicari wizard",
           "tidak ada di: " + ", ".join(str(p) for p in candidates))
    if found:
        out(f"    launcher: {found[0]}")
    source = Path(home) / ".hermes" / "hermes-agent"
    if expect_commit:
        code, text = run(["git", "-C", str(source), "rev-parse", "HEAD"])
        head = (text.strip().splitlines() or [""])[0].strip()
        record(code == 0 and ks.hermes_ref_status(head, expect_commit) == "match",
               f"commit terpasang = HERMES_REF ({expect_commit[:12]})", f"git rev-parse HEAD → kode {code}: {head[:80]!r}")
    for check in CLI_CONTRACT:
        argv = cli_argv(check)
        code, text = run(argv)
        label = " ".join(argv) + (f"  [memuat: {', '.join(check.needles)}]" if check.needles else "")
        if code != 0:
            record(False, label, f"kode keluar {code}: {' | '.join(text.strip().splitlines()[-3:])[:300]} (dipakai: {check.used_by})")
            continue
        if check.argv == ("--version",) and not text.strip():
            record(False, label, "keluaran kosong")
            continue
        missing = [n for n in check.needles if n not in text]
        record(not missing, label, f"tidak memuat {', '.join(missing)} (dipakai: {check.used_by})")
    for rel, needle, used_by in SOURCE_CONTRACT:
        try:
            present = needle in (source / rel).read_text(encoding="utf-8", errors="replace")
            detail = f"teks itu tidak ada lagi di {rel} (dipakai: {used_by})"
        except OSError as exc:
            present, detail = False, f"{source / rel} tidak terbaca: {exc}"
        record(present, f"sumber {rel} memuat {needle!r}", detail)
    return failures


def make_runner(info: Optional[ks.UserInfo], timeout: int = 180) -> Runner:
    """Runner sungguhan. Dengan `info`: lewat runuser + env bersih — fungsi yang SAMA dengan wizard (ks.user_command)."""
    def run(argv: List[str]) -> Tuple[int, str]:
        full, cwd, env = list(argv), None, None
        if info is not None:
            full, cwd = ks.user_command(info, argv), info.home
        else:
            home = os.path.expanduser("~")
            env = dict(os.environ, PATH=f"{home}/.local/bin:{home}/.hermes/hermes-agent/.hermes/bin:" + os.environ.get("PATH", ""))
        try:
            proc = subprocess.run(full, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=timeout)
        except FileNotFoundError:
            return 127, f"perintah tidak ditemukan: {full[0]}"
        except subprocess.TimeoutExpired:
            return 124, f"melewati batas waktu {timeout} detik"
        return proc.returncode, proc.stdout or ""
    return run


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Periksa kontrak installer + CLI Hermes yang diandalkan kit (lihat docstring).")
    ap.add_argument("--list", action="store_true", help="cetak kontrak, lalu keluar")
    ap.add_argument("--user", help="periksa Hermes milik user ini lewat runuser (butuh root), seperti wizard")
    ap.add_argument("--install", action="store_true",
                    help="jalankan dulu installer hulu persis seperti wizard (MENGUBAH mesin; hanya untuk CI). Butuh --user dan root")
    ap.add_argument("--no-pin", action="store_true", help="dengan --install: tanpa --commit (kanal bawaan installer)")
    args = ap.parse_args(argv)
    ref = ks.read_hermes_ref(KIT)
    if args.list:
        print(f"Installer: {ks.hermes_install_command(ref)}")
        print("Launcher : " + " atau ".join(str(p) for p in ks.hermes_launcher_candidates("~")))
        for check in CLI_CONTRACT:
            print(f"CLI      : {' '.join(cli_argv(check)):<34} {('memuat ' + ', '.join(check.needles)) if check.needles else '':<34} ← {check.used_by}")
        for rel, needle, used_by in SOURCE_CONTRACT:
            print(f"Sumber   : {rel}: {needle!r} ← {used_by}")
        return 0
    info = None
    if args.user:
        if os.geteuid() != 0:
            print("--user butuh root (runuser).", file=sys.stderr)
            return 2
        info = ks.HostSystem().user(args.user)
        if info is None:
            print(f"user '{args.user}' tidak ada.", file=sys.stderr)
            return 2
    pinned = ""
    if args.install:
        if info is None:
            print("--install butuh --user (installer dijalankan sebagai user itu, seperti wizard).", file=sys.stderr)
            return 2
        pinned = "" if args.no_pin else (ref if ks.SHA_RE.match(ref) else "")
        command = ks.hermes_install_command(pinned)
        print(f"$ (sebagai {info.name}) bash -c {command!r}", flush=True)
        # Sama dengan HermesInstall.install(): `bash -c <perintah>` lewat user_command, stdin tertutup, keluaran mengalir.
        proc = subprocess.run(ks.user_command(info, ["bash", "-c", command]), cwd=info.home, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            print(f"✗ installer hulu gagal (kode keluar {proc.returncode}) dengan perintah yang dirangkai wizard", file=sys.stderr)
            return 1
    home = Path(info.home) if info else Path(os.path.expanduser("~"))
    print(f"Memeriksa kontrak Hermes di {home} ...")
    failures = run_checks(make_runner(info), home, expect_commit=pinned)
    if failures:
        print(f"\n✗ {len(failures)} butir kontrak dilanggar — wizard/kit perlu disesuaikan dengan Hermes ini:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print("\n✓ kontrak installer + CLI Hermes terpenuhi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
