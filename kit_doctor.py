#!/usr/bin/env python3
"""doctor: bandingkan members.yaml ↔ profile di disk ↔ rute gateway ↔ channel Discord.

Hanya membaca. Mengembalikan kode keluar 0 bila tidak ada masalah ber-level "error", 1 bila ada.

  python3 kit_doctor.py                    # cek lokal (tanpa Discord)
  python3 kit_doctor.py --discord          # + preflight Discord (token, server, role, izin bot, channel
                                           #   bersama/admin) dan channel tiap anggota; bisa dijalankan
                                           #   SEBELUM gateway dinyalakan (hanya butuh token bot)
  python3 kit_doctor.py --docker           # sandbox per profile: status & RAM container, ukuran state.db
  python3 kit_doctor.py --format json

Yang diperiksa:
  profile      profile anggota aktif ada; profile yatim (ada di disk, tak ada di members.yaml)
  rute         rute di config host dan roster.json sama dengan yang diturunkan dari team.yaml
  parkir       profile aktif tidak berpenanda gateway.parked; profile nonaktif sebaiknya tidak dirutekan
  berkas       .env profile bermode 0600; plugin team-guard terpasang di tiap profile
  konfigurasi  key tak dikenal / salah ketik di team.yaml & members.yaml; ID contoh yang belum diganti
  managed      /etc/hermes/config.yaml ada dan sama dengan `hermes_team.py managed`; key yang sudah dipindah
               kit ke config per profile tidak lagi dipin; /etc/hermes/.env ada, tidak terbaca semua orang,
               dan memuat tiap `key_env` di llm.providers (hanya NAMA yang diperiksa, nilai tidak dicetak)
  systemd      linger user aktif (gateway tetap hidup setelah logout); timer hermes-usage-collect,
               hermes-usage-report (bila laporan terjadwal diisi), hermes-sandbox-idle
  discord      (--discord) preflight tim: token bot, bot ada di server, role member/admin/entry, urutan role
               bot, kategori (< 50 channel), channel bersama & admin, izin bot, channel laporan; lalu
               channel tiap anggota: ada, di server yang benar, bertipe teks, @everyone tidak bisa melihat
  sandbox      (--docker) container berlabel hermes-profile: status dan RAM per profile, total RAM sandbox
               terhadap RAM server (peringatan di atas 80%), ukuran state.db tiap profile (peringatan di
               atas 500 MB). Hanya menjalankan `docker ps` dan `docker stats --no-stream`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Folder skrip harus ada di sys.path walau Python dijalankan dengan -I / PYTHONSAFEPATH (launcher tertentu
# memakainya); modul kit lain (kit_*, kit/, teamctl/) berada di folder yang sama.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import yaml

import kit_paths
from kit.profiles import SYSTEM_KINDS

ERROR, WARN, INFO = "error", "warn", "info"
SYSTEM_PROFILES_KEYS = SYSTEM_KINDS      # jenis profile ber-channel sendiri (kit/profiles.py)


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, level: str, area: str, message: str, fix: str = "") -> None:
        self.items.append({"level": level, "area": area, "message": message, "fix": fix})

    @property
    def errors(self) -> int:
        return sum(1 for i in self.items if i["level"] == ERROR)

    def render(self, fmt: str = "text") -> str:
        if fmt == "json":
            return json.dumps({"ok": self.errors == 0, "items": self.items}, indent=2, ensure_ascii=False)
        if not self.items:
            return "✓ Tidak ada masalah ditemukan."
        icon = {ERROR: "✗", WARN: "!", INFO: "·"}
        lines = []
        for it in self.items:
            lines.append(f"{icon[it['level']]} [{it['area']}] {it['message']}")
            if it["fix"]:
                lines.append(f"    → {it['fix']}")
        lines.append(f"\n{self.errors} error, {sum(1 for i in self.items if i['level'] == WARN)} peringatan.")
        return "\n".join(lines)


def _norm_routes(routes: Any) -> list[tuple]:
    out = []
    for r in routes or []:
        if isinstance(r, dict):
            out.append(tuple(sorted((k, str(v)) for k, v in r.items() if k != "name")))
    return sorted(out)


def _home(root: Path, profile: str) -> Path:
    return root if profile == "default" else root / "profiles" / profile


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def check_local(team: dict, root: Path, rep: Report, ht: Any) -> None:
    active = ht.active_members(team)
    inactive = [m for m in team["members"] if not m.get("active", True)]
    known = {m["profile"] for m in team["members"]} | {team[k]["profile"] for k in SYSTEM_KINDS} | {team["template"]}
    # profile
    for m in active:
        if not _home(root, m["profile"]).is_dir():
            rep.add(ERROR, "profile", f"profile {m['profile']} milik {m['name']} tidak ada di disk",
                    "jalankan: hermes_team.py apply --only " + m["name"])
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        for p in sorted(profiles_dir.iterdir()):
            if p.is_dir() and p.name not in known:
                rep.add(WARN, "profile", f"profile yatim {p.name} (tidak ada di members.yaml)",
                        "bila sisa anggota lama: hermes profile delete " + p.name + " (backup dulu bila perlu)")
    # rute
    want = _norm_routes(ht.build_routes(team))
    host_cfg = _read_yaml(root / "config.yaml")
    gw = host_cfg.get("gateway") if isinstance(host_cfg.get("gateway"), dict) else {}
    have = _norm_routes(gw.get("profile_routes") if gw else host_cfg.get("profile_routes"))
    if have != want:
        missing, extra = [r for r in want if r not in have], [r for r in have if r not in want]
        rep.add(ERROR, "rute", f"profile_routes di {root}/config.yaml tidak sama dengan team.yaml "
                f"({len(missing)} kurang, {len(extra)} berlebih)", "jalankan: hermes_team.py apply")
    roster = Path(team["paths"]["state_dir"]) / "roster.json"
    try:
        rdata = json.loads(roster.read_text(encoding="utf-8"))
        if _norm_routes((rdata.get("gateway") or {}).get("profile_routes")) != want:
            rep.add(ERROR, "rute", "roster.json usang (rute berbeda dari team.yaml)", "jalankan: hermes_team.py apply")
    except (OSError, ValueError):
        rep.add(ERROR, "rute", f"roster.json tidak terbaca di {roster}", "jalankan: hermes_team.py apply")
    # parkir
    for m in active:
        if (_home(root, m["profile"]) / "gateway.parked").exists():
            rep.add(WARN, "parkir", f"profile aktif {m['profile']} berpenanda gateway.parked (tidak dilayani)",
                    f"hermes -p {m['profile']} gateway start")
    for m in inactive:
        if m["channel_id"] in {r.get("chat_id") for r in ht.build_routes(team)}:
            rep.add(ERROR, "rute", f"anggota nonaktif {m['name']} masih punya rute", "jalankan: hermes_team.py apply")
        home = _home(root, m["profile"])
        if home.is_dir() and not (home / "gateway.parked").exists():
            rep.add(WARN, "parkir", f"{m['profile']} nonaktif tetapi belum diparkir (cron job-nya masih bisa jalan)",
                    "jalankan: hermes_team.py apply (memarkir otomatis)")
    # berkas
    for name in sorted(known - {team["template"]} | {"default"}):
        home = _home(root, name)
        env = home / ".env"
        if env.exists():
            mode = stat.S_IMODE(env.stat().st_mode)
            if mode & 0o077:
                rep.add(ERROR, "berkas", f"{env} bermode {oct(mode)} (harus 0600)", f"chmod 600 {env}")
        if home.is_dir() and name != "default" or name == "default":
            if home.is_dir() and not (home / "plugins" / ht.PLUGIN_NAME).exists():
                rep.add(WARN, "berkas", f"plugin {ht.PLUGIN_NAME} belum terpasang di {name}", "jalankan: hermes_team.py apply")
    # model
    llm = team.get("llm")
    if llm:
        for m in active:
            alias = m.get("model") or llm["default_model"]
            if alias not in llm["models"]:
                rep.add(ERROR, "model", f"{m['name']} memakai model '{alias}' yang tidak ada di katalog")
    check_usage(team, rep)
    check_capabilities(team, rep)


def check_usage(team: dict, rep: Report, now: Optional[float] = None) -> None:
    """Buku besar pemakaian: ada, privat, dan masih dipotret (timer systemd terpasang)."""
    import sqlite3
    import time
    ledger = Path(team["paths"]["state_dir"]) / "usage.db"
    fix = "pasang timer: " + timer_install_command("hermes-usage-collect")
    if not ledger.exists():
        rep.add(WARN, "pemakaian", "buku besar pemakaian belum ada (token & request per orang tidak tercatat)", fix)
        return
    mode = stat.S_IMODE(ledger.stat().st_mode)
    if mode & 0o077:
        rep.add(ERROR, "pemakaian", f"{ledger} bermode {oct(mode)} (harus 0600)", f"chmod 600 {ledger}")
    try:
        con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='last_collect'").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        rep.add(ERROR, "pemakaian", f"buku besar pemakaian tidak terbaca: {exc}")
        return
    age = (time.time() if now is None else now) - float(row[0]) if row else None
    if age is None or age > 3600:
        last = "belum pernah" if age is None else f"{int(age // 60)} menit lalu"
        rep.add(WARN, "pemakaian", f"potret pemakaian terakhir: {last} (seharusnya tiap 15 menit)", fix)


def check_capabilities(team: dict, rep: Report, now: Optional[float] = None) -> None:
    """`capabilities.json` ditulis plugin team-guard saat start: fitur mana yang benar-benar aktif di Hermes
    ini. Belum ada = gateway belum pernah menyala dengan plugin; fitur tak tersedia = Hermes berubah dan plugin
    jatuh ke jalur cadangan (live reload → restart halus, antrean nonaktif, dst.)."""
    import time
    path = Path(team["paths"]["state_dir"]) / "capabilities.json"
    if not path.exists():
        rep.add(INFO, "plugin", "capabilities.json belum ada (gateway belum pernah dijalankan dengan plugin team-guard)")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        caps = data["capabilities"]
        if not isinstance(caps, dict):
            raise ValueError("capabilities bukan pemetaan")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        rep.add(WARN, "plugin", f"capabilities.json tidak terbaca ({type(exc).__name__}); jalankan ulang gateway")
        return
    missing = sorted(k for k, v in caps.items() if isinstance(v, dict) and v.get("available") is False)
    for name in missing:
        reason = str(caps[name].get("reason") or "tidak tersedia")[:160]
        rep.add(WARN, "plugin", f"fitur plugin tidak aktif: {name} ({reason})",
                "biasanya Hermes berubah; jalankan tests/selftest.sh dan lihat docs/BATASAN.md")
    ver = data.get("hermes_version")
    if ver and not missing:
        rep.add(INFO, "plugin", f"semua {len(caps)} API Hermes yang dipakai plugin tersedia (Hermes {ver})")
    stamp = data.get("generated_at")        # plugin menulis teks ISO UTC, mis. 2026-11-13T15:00:00Z
    try:
        written = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return
    age = (time.time() if now is None else now) - written
    if age > 14 * 86400:
        rep.add(INFO, "plugin", f"capabilities.json berumur {int(age // 86400)} hari; ditulis ulang tiap gateway start")


KIT_DIR = Path(__file__).resolve().parent


def timer_install_command(unit: str) -> str:
    """Perintah pasang satu timer user systemd dari folder kit (folder unit user belum tentu ada)."""
    return (f"mkdir -p ~/.config/systemd/user && cp {KIT_DIR}/systemd/{unit}.* ~/.config/systemd/user/ && "
            f"systemctl --user daemon-reload && systemctl --user enable --now {unit}.timer")


# ----------------------------------------------------------------------------- konfigurasi
def check_config(team: dict, rep: Report, ht: Any) -> None:
    """Peringatan pemuatan (key tak dikenal) dan ID contoh yang belum diganti."""
    for line in team.get("warnings") or []:
        rep.add(WARN, "konfigurasi", line, "perbaiki ejaan key itu (atau hapus), lalu: hermes_team.py check")
    if not ht.placeholders_allowed():
        found = ht.placeholder_ids(team)
        if found:
            rep.add(ERROR, "konfigurasi", "masih memakai ID contoh dari *.example.yaml: " + ", ".join(found),
                    "ganti dengan ID asli (Discord → Developer Mode → klik kanan → Copy ID)")


# ----------------------------------------------------------------------------- managed scope (/etc/hermes)
def check_managed(team: dict, rep: Report, ht: Any, managed_dir: Optional[Path] = None) -> None:
    """Berkas managed terpasang vs `hermes_team.managed_config(team)`, dan /etc/hermes/.env. Hanya membaca."""
    folder = Path(managed_dir) if managed_dir else ht.managed_dir()
    install = "jalankan: " + ht.managed_install_command(team)
    diff = ht.managed_diff(team, folder / "config.yaml")
    if diff["missing_file"]:
        rep.add(ERROR, "managed", f"{diff['path']} belum ada: kebijakan tim (sandbox Docker tanpa mount host, "
                "batas langkah, provider LLM) tidak dipin dan bisa diubah dari profile mana pun", install)
    elif diff["unreadable"]:
        rep.add(ERROR, "managed", f"{diff['path']} tidak terbaca ({diff['unreadable']})",
                f"perbaiki izin/isi berkas (seharusnya 0644 root), atau pasang ulang — {install}")
    else:
        if diff["moved"]:
            rep.add(ERROR, "managed", f"{diff['path']} masih mem-pin {', '.join(diff['moved'])} (berkas dari kit "
                    "versi lama). Key itu sekarang ditulis per profile; selama masih dipin, batas sandbox per "
                    "orang dan ambang kompresi per model TIDAK berlaku",
                    "urutan: hermes_team.py apply → managed (" + ht.managed_install_command(team)
                    + ") → hermes_team.py apply sekali lagi")
        drift = [f"kurang {k}" for k in diff["missing"]] + [f"berbeda {k}" for k in diff["changed"]]
        drift += [f"berlebih {k}" for k in diff["extra"] if k not in diff["moved"]]
        if drift:
            rep.add(WARN, "managed", f"{diff['path']} tidak sama dengan team.yaml: " + ", ".join(drift),
                    "lihat rinciannya: hermes_team.py managed --diff; pasang ulang — " + install)
    check_managed_env(team, rep, ht, folder / ".env")


def check_managed_env(team: dict, rep: Report, ht: Any, env_path: Path) -> None:
    """.env managed: ada, tidak terbaca semua orang, memuat tiap key_env. Nilai TIDAK pernah dicetak."""
    providers = (team.get("llm") or {}).get("providers") or {}
    wanted = sorted({str(b["key_env"]) for b in providers.values() if isinstance(b, dict) and b.get("key_env")})
    per_user = bool((team.get("llm") or {}).get("per_user_keys"))
    level = WARN if per_user else ERROR
    note = " (llm.per_user_keys aktif: key boleh ada di .env tiap profile)" if per_user else ""
    try:
        mode = stat.S_IMODE(env_path.stat().st_mode)
    except OSError:
        if wanted:
            rep.add(level, "managed", f"{env_path} belum ada, padahal llm.providers memakai: {', '.join(wanted)}{note}",
                    f"sebagai root: buat {env_path} berisi NAMA=nilai per baris, lalu "
                    f"chown root:$(id -gn) {env_path} && chmod 640 {env_path}")
        return
    if mode & 0o004:
        rep.add(ERROR, "managed", f"{env_path} bermode {oct(mode)}: terbaca SEMUA user di server (berisi API key)",
                f"sudo chmod 640 {env_path} (pemilik root, grup = grup user Hermes)")
    if not wanted:
        return
    try:
        present = {k for k, v in ht.parse_env_text(env_path.read_text(encoding="utf-8", errors="replace")).items()
                   if str(v).strip()}
    except OSError as exc:
        rep.add(ERROR, "managed", f"{env_path} tidak bisa dibaca user ini ({type(exc).__name__}); Hermes juga "
                "tidak akan bisa membaca API key di dalamnya",
                f"sudo chown root:$(id -gn) {env_path} && sudo chmod 640 {env_path}")
        return
    missing = [name for name in wanted if name not in present]
    if missing:
        rep.add(level, "managed", f"{env_path} tidak memuat (atau nilainya kosong): {', '.join(missing)}{note}",
                f"sebagai root tambahkan baris {missing[0]}=… ke {env_path}, lalu restart gateway")


# ----------------------------------------------------------------------------- systemd (linger & timer)
def _current_user() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — tanpa entri passwd (container)
        return os.environ.get("USER") or str(os.getuid())


def check_linger(rep: Report, run: "Runner", user: Optional[str] = None) -> None:
    """Tanpa linger, systemd --user (gateway + timer) mati saat user logout dan tidak hidup saat boot."""
    user = user or _current_user()
    fix = f"sudo loginctl enable-linger {user}"
    try:
        code, out = run(["loginctl", "show-user", user, "-p", "Linger"])
    except (OSError, subprocess.SubprocessError):
        rep.add(INFO, "systemd", "loginctl tidak tersedia; linger tidak diperiksa")
        return
    text = (out or "").strip()
    if code == 0 and text.lower().endswith("linger=yes"):
        return
    if code == 0 and "linger=no" in text.lower():
        rep.add(WARN, "systemd", f"linger user {user} mati: gateway dan timer berhenti saat {user} logout dan "
                "tidak hidup otomatis setelah reboot", fix)
        return
    last = text.splitlines()[-1] if text else f"exit {code}"
    rep.add(WARN, "systemd", f"linger user {user} tidak bisa dipastikan ({last}); bila belum aktif, gateway "
            "berhenti saat logout", fix)


def check_timers(team: dict, rep: Report, run: "Runner") -> None:
    """Timer user: hermes-usage-collect (selalu), hermes-usage-report (bila laporan terjadwal diisi),
    hermes-sandbox-idle (opsional). systemctl tidak ada / tanpa sesi user → satu catatan, bukan kegagalan."""
    usage = team.get("usage") or {}
    reports_on = bool(usage.get("report_channel_id")) and any((usage.get("reports") or {}).values())
    units = [("hermes-usage-collect", WARN, "pemakaian token per orang tidak dipotret"),
             ("hermes-usage-report", WARN if reports_on else None,
              "laporan terjadwal (usage.reports) tidak akan terkirim"),
             ("hermes-sandbox-idle", INFO, "sandbox menganggur tidak dihentikan otomatis (opsional)")]
    for unit, level, why in units:
        if level is None:
            continue
        try:
            code, out = run(["systemctl", "--user", "is-enabled", f"{unit}.timer"])
        except (OSError, subprocess.SubprocessError):
            rep.add(INFO, "systemd", "systemctl tidak tersedia; timer tidak diperiksa")
            return
        text = (out or "").strip()
        if code == 0 and text.splitlines()[:1] and text.splitlines()[0].strip() in ("enabled", "enabled-runtime",
                                                                                   "static", "linked"):
            continue
        if "failed to connect to bus" in text.lower() or "no medium found" in text.lower():
            rep.add(INFO, "systemd", "systemd --user tidak terjangkau dari sesi ini (jalankan sebagai user Hermes "
                    "lewat login/ssh, bukan `su`); timer tidak diperiksa")
            return
        state = text.splitlines()[0].strip() if text else f"exit {code}"
        rep.add(level, "systemd", f"timer {unit}.timer belum aktif ({state}): {why}", timer_install_command(unit))


def check_system(team: dict, rep: Report, ht: Any, run: Optional["Runner"] = None,
                 managed_dir: Optional[Path] = None, user: Optional[str] = None) -> None:
    """Pemeriksaan di luar folder Hermes: konfigurasi, managed scope, linger, timer."""
    run = run or run_command
    check_config(team, rep, ht)
    check_managed(team, rep, ht, managed_dir)
    check_linger(rep, run, user)
    check_timers(team, rep, run)


# ----------------------------------------------------------------------------- sandbox Docker & state.db
STATE_DB_WARN_BYTES = 500 * 1024 * 1024     # di atas ini Hermes mulai terasa lambat (FTS, /resume)
SANDBOX_RAM_WARN_RATIO = 0.80               # total RAM sandbox terhadap RAM server
PROFILE_LABEL = "hermes-profile"            # label yang dipasang Hermes pada container sandbox
_UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
          "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}

Runner = Callable[[list], tuple]            # (argv) → (kode keluar, stdout atau stderr)


def run_command(cmd: list, timeout: int = 30) -> tuple:
    """Jalankan perintah baca-saja. OSError (mis. docker tidak terpasang) dan timeout diteruskan ke pemanggil."""
    proc = subprocess.run(cmd, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
    return proc.returncode, (proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout))


def parse_size(text: str) -> Optional[int]:
    """'312.5MiB' / '1.2GiB' / '0B' → byte. Tidak dikenali → None."""
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", str(text or ""))
    if not match or match.group(2).lower() not in _UNITS:
        return None
    return int(float(match.group(1)) * _UNITS[match.group(2).lower()])


def _mb(size: float) -> str:
    return f"{size / (1024 * 1024):.0f} MB"


def host_memory_bytes(meminfo: Path = Path("/proc/meminfo")) -> Optional[int]:
    try:
        for line in Path(meminfo).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def docker_containers(run: Runner) -> list[dict]:
    """Container berlabel hermes-profile (semua status) + RAM yang sedang dipakai untuk yang berjalan.
    Gagal menjalankan docker → RuntimeError dengan alasan singkat."""
    fmt = "{{.ID}}\t{{.Label \"" + PROFILE_LABEL + "\"}}\t{{.State}}\t{{.Names}}"
    try:
        code, out = run(["docker", "ps", "-a", "--filter", f"label={PROFILE_LABEL}", "--format", fmt])
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}")
    if code != 0:
        raise RuntimeError((out or "").strip().splitlines()[-1] if (out or "").strip() else f"exit {code}")
    found = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].strip():
            found.append({"id": parts[0].strip(), "profile": parts[1].strip(), "state": parts[2].strip().lower(),
                          "name": parts[3].strip() if len(parts) > 3 else "", "memory": None})
    running = [c for c in found if c["state"] == "running"]
    if running:
        try:
            code, out = run(["docker", "stats", "--no-stream", "--format", "{{.ID}}\t{{.MemUsage}}",
                             *[c["id"] for c in running]])
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"docker stats: {type(exc).__name__}: {exc}")
        if code != 0:
            raise RuntimeError("docker stats: " + ((out or "").strip().splitlines() or [f"exit {code}"])[-1])
        for line in out.splitlines():
            cid, _, usage = line.partition("\t")
            used = parse_size(usage.split("/")[0])
            for c in running:
                if cid.strip() and (c["id"].startswith(cid.strip()) or cid.strip().startswith(c["id"])):
                    c["memory"] = used
    return found


def state_db_size(home: Path) -> Optional[int]:
    """Ukuran state.db profile (berikut -wal). Tidak ada → None."""
    db = home / "state.db"
    if not db.is_file():
        return None
    size = db.stat().st_size
    wal = home / "state.db-wal"
    return size + (wal.stat().st_size if wal.is_file() else 0)


def check_docker(team: dict, root: Path, rep: Report, run: Runner = run_command,
                 meminfo: Path = Path("/proc/meminfo")) -> None:
    """Sandbox per profile (status, RAM) dan ukuran state.db. Hanya membaca; docker hilang → satu peringatan
    dan pemeriksaan state.db tetap jalan."""
    profiles = [m["profile"] for m in team["members"]] + [team[k]["profile"] for k in SYSTEM_KINDS] + ["default"]
    by_profile: dict[str, list] = {}
    docker_ok = True
    try:
        for c in docker_containers(run):
            by_profile.setdefault(c["profile"], []).append(c)
    except RuntimeError as exc:
        docker_ok = False
        rep.add(WARN, "sandbox", f"docker tidak bisa diperiksa ({exc}); status dan RAM container dilewati",
                "pastikan docker terpasang dan user ini anggota grup docker")
    total = 0
    for profile in profiles + sorted(set(by_profile) - set(profiles)):
        parts = []
        if docker_ok:
            containers = by_profile.get(profile, [])
            if not containers:
                parts.append("container: belum ada")
            for c in containers:
                text = f"container {c['name'] or c['id'][:12]}: {c['state']}"
                if c["memory"] is not None:
                    total += c["memory"]
                    text += f", RAM {_mb(c['memory'])}"
                parts.append(text)
        size = state_db_size(_home(root, profile))
        if size is not None:
            parts.append(f"state.db {_mb(size)}")
            if size > STATE_DB_WARN_BYTES:
                rep.add(WARN, "sandbox", f"state.db profile {profile} sudah {_mb(size)} (ambang "
                        f"{_mb(STATE_DB_WARN_BYTES)}); riwayat sesi membuat Hermes melambat",
                        "set scale.session_retention_days di team.yaml lalu pasang ulang managed config, atau "
                        f"jalankan: hermes -p {profile} sessions prune")
        if parts:
            unknown = "" if profile in profiles else " (profile tidak ada di members.yaml)"
            rep.add(INFO, "sandbox", f"{profile}{unknown}: " + "; ".join(parts))
    if docker_ok:
        host = host_memory_bytes(meminfo)
        note = f"total RAM sandbox yang berjalan: {_mb(total)}"
        if host:
            ratio = total / host
            note += f" dari {_mb(host)} RAM server ({ratio * 100:.0f}%)"
            if ratio > SANDBOX_RAM_WARN_RATIO:
                rep.add(WARN, "sandbox", note + f" — di atas {SANDBOX_RAM_WARN_RATIO * 100:.0f}%",
                        "hentikan sandbox yang menganggur (tools/sandbox_idle.py) atau turunkan sandbox.memory_mb")
                return
        rep.add(INFO, "sandbox", note)


# Izin server yang dibutuhkan bot: (nama bit, label di Discord, untuk apa, wajib?(team) → bool).
BOT_GUILD_PERMISSIONS = (
    ("MANAGE_ROLES", "Manage Roles", "memberi/mencabut role dan mengatur izin channel pribadi", None),
    ("MANAGE_CHANNELS", "Manage Channels", "membuat channel pribadi saat onboarding", None),
    ("VIEW_AUDIT_LOG", "View Audit Log", "memeriksa siapa pemberi role @backend (onboarding.verify_actor)",
     lambda team: team["onboarding"]["enabled"] and team["onboarding"]["verify_actor"]),
    ("MANAGE_MESSAGES", "Manage Messages", "menghapus pesan berisi secret (secrets.delete_secret_messages)",
     lambda team: team["secrets"]["delete_secret_messages"]),
    ("MANAGE_THREADS", "Manage Threads", "mengelola thread percakapan", None),
    ("CREATE_PUBLIC_THREADS", "Create Public Threads", "membuka thread per percakapan", None),
    ("SEND_MESSAGES_IN_THREADS", "Send Messages in Threads", "membalas di dalam thread", None),
)
CATEGORY_CHANNEL_LIMIT = 50      # batas Discord: channel per kategori
TEXT_CHANNEL_TYPES = (0, 5)      # teks, announcement


def _bits(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def guild_permissions(guild_id: str, role_ids: Any, roles: dict, ht: Any) -> int:
    """Izin tingkat server: @everyone ∪ semua role yang dipegang. Administrator = semua izin."""
    perms = _bits((roles.get(str(guild_id)) or {}).get("permissions"))
    for rid in role_ids or []:
        perms |= _bits((roles.get(str(rid)) or {}).get("permissions"))
    return ~0 if perms & ht.PERM["ADMINISTRATOR"] else perms


def channel_permissions(base: int, channel: dict, guild_id: str, role_ids: Any, user_id: str, ht: Any) -> int:
    """Izin efektif di satu channel (aturan Discord: @everyone → role → member; Administrator melewati semua)."""
    if base & ht.PERM["ADMINISTRATOR"]:
        return ~0
    ows = {str(o.get("id")): o for o in channel.get("permission_overwrites") or []}
    perms = base
    everyone = ows.get(str(guild_id))
    if everyone:
        perms = (perms & ~_bits(everyone.get("deny"))) | _bits(everyone.get("allow"))
    allow = deny = 0
    for rid in role_ids or []:
        ow = ows.get(str(rid))
        if ow:
            allow |= _bits(ow.get("allow"))
            deny |= _bits(ow.get("deny"))
    perms = (perms & ~deny) | allow
    mine = ows.get(str(user_id))
    if mine:
        perms = (perms & ~_bits(mine.get("deny"))) | _bits(mine.get("allow"))
    return perms


ROLES_MENU = "Discord → Server Settings → Roles"


class GuildState:
    """Keadaan server yang dibaca sekali: id bot, role bot, semua role, semua channel."""

    def __init__(self, bot_id: str, my_roles: list, roles: dict, channels: dict):
        self.bot_id, self.my_roles, self.roles, self.channels = bot_id, my_roles, roles, channels


def _fetch_guild_state(team: dict, rep: Report, ht: Any, api: Any) -> Optional[GuildState]:
    """Token valid dan bot ada di server? Bila tidak: catat galat beserta perbaikannya dan kembalikan None."""
    guild = team["guild_id"]
    try:
        bot_id = str(api.bot_user_id())
    except ht.TeamError as exc:
        rep.add(ERROR, "discord", f"token bot ditolak atau Discord tidak terjangkau: {exc}",
                "periksa DISCORD_BOT_TOKEN di ~/.hermes/.env (Developer Portal → Applications → Bot → Reset Token), "
                "lalu ulangi")
        return None
    try:
        me = api.guild_member(guild, bot_id) or {}
        roles = {str(r.get("id")): r for r in api.guild_roles(guild)}
        channels = {str(c.get("id")): c for c in api.guild_channels(guild)}
    except ht.TeamError as exc:
        rep.add(ERROR, "discord", f"bot belum ada di server guild_id {guild} (atau guild_id salah): {exc}",
                "undang bot: Developer Portal → OAuth2 → URL Generator (scope bot + applications.commands) → buka "
                "URL-nya dan pilih server tim; pastikan guild_id di team.yaml = ID server itu")
        return None
    return GuildState(bot_id, [str(r) for r in me.get("roles") or []], roles, channels)


def _check_roles(team: dict, rep: Report, state: GuildState) -> None:
    """Role tim ada, dan role tertinggi bot berada di ATAS role yang harus ia beri/cabut."""
    wanted = [("roles.member", team["member_role"]), ("roles.admin", team["admin_role"])]
    if team.get("entry_role"):
        wanted.append(("roles.entry", team["entry_role"]))
    for key, rid in wanted:
        if rid not in state.roles:
            rep.add(ERROR, "discord", f"role {key} ({rid}) tidak ada di server",
                    f"{ROLES_MENU} → klik kanan role yang benar → Copy ID → isi {key} di team.yaml")
    top = max((int((state.roles.get(r) or {}).get("position") or 0) for r in state.my_roles), default=0)
    for key, rid in wanted:
        if key == "roles.admin" or rid not in state.roles:
            continue
        role = state.roles[rid]
        if int(role.get("position") or 0) >= top:
            rep.add(ERROR, "discord", f"role tertinggi bot (posisi {top}) tidak berada di ATAS {key} "
                    f"'{role.get('name')}' (posisi {role.get('position')}): bot tidak bisa memberi "
                    "atau mencabut role itu",
                    f"{ROLES_MENU} → seret (drag-and-drop) role bot ke ATAS role '{role.get('name')}' → "
                    "Save Changes")


def _check_guild_permissions(team: dict, rep: Report, ht: Any, state: GuildState) -> int:
    """Izin tingkat server yang dibutuhkan kit; mengembalikan bit izin dasar bot (dipakai pemeriksaan channel)."""
    base = guild_permissions(team["guild_id"], state.my_roles, state.roles, ht)
    for bit, label, why, needed in BOT_GUILD_PERMISSIONS:
        if base & ht.PERM[bit]:
            continue
        level = ERROR if needed is None or needed(team) else WARN
        rep.add(level, "discord", f"bot tidak punya izin {label} ({why})",
                f"{ROLES_MENU} → role bot → Permissions → aktifkan {label} (atau undang ulang bot dengan izin itu)")
    return base


def _check_category(team: dict, rep: Report, state: GuildState) -> None:
    category = team.get("category_id")
    if not category:
        return
    cat = state.channels.get(category)
    if cat is None:
        rep.add(ERROR, "discord", f"category_id {category} tidak ada di server (channel pribadi baru tidak bisa dibuat)",
                "klik kanan kategori tujuan → Copy ID → isi category_id di team.yaml")
    elif cat.get("type") != 4:
        rep.add(ERROR, "discord", f"category_id {category} bukan kategori (tipe {cat.get('type')})",
                "isi category_id dengan ID KATEGORI, bukan channel")
    else:
        used = sum(1 for c in state.channels.values() if str(c.get("parent_id")) == category)
        if used >= CATEGORY_CHANNEL_LIMIT:
            rep.add(ERROR, "discord", f"kategori {category} sudah berisi {used} channel (batas Discord "
                    f"{CATEGORY_CHANNEL_LIMIT}): onboarding berikutnya gagal membuat channel",
                    "buat kategori baru dan isi category_id dengan ID-nya (channel lama boleh tetap di kategori lama)")
        elif used >= CATEGORY_CHANNEL_LIMIT - 5:
            rep.add(WARN, "discord", f"kategori {category} hampir penuh: {used}/{CATEGORY_CHANNEL_LIMIT} channel",
                    "siapkan kategori baru sebelum penuh")


def _team_channel_targets(team: dict) -> list:
    """[(key di team.yaml, id channel, harus privat?)] untuk channel bersama, admin, dan laporan."""
    targets = [(f"{k}.channel_id", team[k]["channel_id"], True) for k in SYSTEM_KINDS]
    report_channel = (team.get("usage") or {}).get("report_channel_id")
    if report_channel and report_channel not in (team[k]["channel_id"] for k in SYSTEM_KINDS):
        targets.append(("usage.report_channel_id", report_channel, False))
    return targets


def _check_one_team_channel(team: dict, rep: Report, ht: Any, api: Any, state: GuildState, base: int,
                            key: str, cid: str, must_be_private: bool) -> None:
    guild = team["guild_id"]
    view, send = ht.PERM["VIEW_CHANNEL"], ht.PERM["SEND_MESSAGES"]
    ch = state.channels.get(cid)
    if ch is None:
        try:
            other = api.get_channel(cid)
        except ht.TeamError:
            other = None
        if other is None:
            rep.add(ERROR, "discord", f"channel {key} ({cid}) tidak ada (atau bot tidak bisa melihatnya)",
                    f"klik kanan channel yang benar → Copy ID → isi {key} di team.yaml")
        else:
            rep.add(ERROR, "discord", f"channel {key} ({cid}) bukan di server tim (guild {other.get('guild_id')})",
                    f"isi {key} dengan channel di server {guild}")
        return
    if ch.get("type") not in TEXT_CHANNEL_TYPES:
        rep.add(ERROR, "discord", f"channel {key} ({cid}) bukan channel teks (tipe {ch.get('type')})",
                f"isi {key} dengan ID channel teks")
        return
    edit = f"Edit Channel #{ch.get('name') or cid} → Permissions"
    everyone_base = _bits((state.roles.get(guild) or {}).get("permissions"))
    if must_be_private and channel_permissions(everyone_base, ch, guild, [], "", ht) & view:
        who = "role member" if key.startswith("shared") else "role admin"
        rep.add(ERROR, "discord", f"channel {key} #{ch.get('name')} bisa dilihat @everyone: siapa pun di server "
                "dilayani profile itu",
                f"{edit} → @everyone: matikan View Channel; lalu beri View Channel ke {who} dan ke bot")
    mine = channel_permissions(base, ch, guild, state.my_roles, state.bot_id, ht)
    # Aturan Discord: tanpa View Channel, Send Messages ikut tidak berlaku (izin implisit).
    lacking = [label for bit, label in ((view, "View Channel"), (send, "Send Messages"))
               if not mine & bit or not mine & view]
    if lacking:
        rep.add(ERROR, "discord", f"bot tidak bisa {' / '.join(lacking)} di {key} #{ch.get('name')}",
                f"{edit} → tambah bot (atau role-nya) → izinkan View Channel dan Send Messages")


def check_discord_setup(team: dict, rep: Report, ht: Any, api: Any) -> bool:
    """Preflight tingkat TIM (bukan per anggota): cukup token bot, gateway tidak perlu jalan.
    Kembalikan False bila pemeriksaan lanjutan tidak mungkin (token salah / bot tidak di server)."""
    state = _fetch_guild_state(team, rep, ht, api)
    if state is None:
        return False
    _check_roles(team, rep, state)
    base = _check_guild_permissions(team, rep, ht, state)
    _check_category(team, rep, state)
    for key, cid, must_be_private in _team_channel_targets(team):
        _check_one_team_channel(team, rep, ht, api, state, base, key, cid, must_be_private)
    return True


def check_discord(team: dict, rep: Report, ht: Any, api: Any) -> None:
    guild = team["guild_id"]
    everyone = guild            # id role @everyone = id server
    view = ht.PERM["VIEW_CHANNEL"]
    seen: dict[str, str] = {}
    for m in ht.active_members(team):
        cid = m["channel_id"]
        if cid in seen:
            rep.add(ERROR, "discord", f"channel {cid} dipakai {m['name']} dan {seen[cid]}")
        seen[cid] = m["name"]
        ch = api.get_channel(cid)
        if ch is None:
            rep.add(ERROR, "discord", f"channel {cid} milik {m['name']} tidak ada",
                    f"hermes_team.py onboard --user-id {m['user_id']} (membuat ulang)")
            continue
        if str(ch.get("guild_id")) != str(guild):
            rep.add(ERROR, "discord", f"channel {cid} ({m['name']}) bukan di server tim")
        if ch.get("type") != 0:
            rep.add(ERROR, "discord", f"channel {cid} ({m['name']}) bukan channel teks")
        ows = {str(o.get("id")): o for o in ch.get("permission_overwrites") or []}
        ev = ows.get(everyone)
        if not ev or not (int(ev.get("deny") or 0) & view):
            rep.add(ERROR, "discord", f"channel pribadi {m['name']} ({cid}) bisa dilihat @everyone",
                    "jalankan onboard ulang untuk anggota itu (menegaskan izin privat)")
        extra = [i for i, o in ows.items() if i not in (everyone, m["user_id"], api.bot_user_id())
                 and int(o.get("allow") or 0) & view]
        if extra:
            rep.add(WARN, "discord", f"channel {m['name']} dapat dilihat pihak lain: {', '.join(extra)}")


def main(argv: Optional[list[str]] = None, *, run: Optional[Runner] = None, api: Any = None) -> int:
    """`run` (perintah sistem) dan `api` (klien Discord) bisa disuntik untuk uji."""
    import hermes_team as ht      # folder skrip otomatis ada di sys.path saat dijalankan sebagai skrip

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="team.yaml (default: $HERMES_TEAM_DIR/team.yaml, kalau tidak "
                                     "~/.hermes-team/team.yaml)")
    ap.add_argument("--members")
    ap.add_argument("--hermes-root", default=os.environ.get("HERMES_HOME") or "~/.hermes")
    ap.add_argument("--discord", action="store_true",
                    help="preflight Discord (token, server, role, izin bot, channel bersama/admin) + channel anggota")
    ap.add_argument("--docker", action="store_true",
                    help="periksa sandbox Docker per profile (status, RAM) dan ukuran state.db")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    team_file = kit_paths.team_file(args.config)
    notice = None if args.config else kit_paths.cwd_notice(team_file)
    if notice:
        print(notice, file=sys.stderr)
    try:
        team = ht.load_team(team_file, Path(args.members).expanduser() if args.members else None)
    except ht.TeamError as exc:
        print(f"konfigurasi: {exc}", file=sys.stderr)
        return 2
    root = Path(args.hermes_root).expanduser()
    rep = Report()
    check_local(team, root, rep, ht)
    check_system(team, rep, ht, run=run)
    if args.discord:
        try:
            client = api or ht.DiscordAPI(ht.discord_token(root))
            if check_discord_setup(team, rep, ht, client):
                check_discord(team, rep, ht, client)
        except ht.TeamError as exc:
            rep.add(ERROR, "discord", f"tidak bisa memeriksa Discord: {exc}")
    if args.docker:
        check_docker(team, root, rep)
    print(rep.render(args.format))
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
