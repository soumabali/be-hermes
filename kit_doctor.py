#!/usr/bin/env python3
"""doctor: bandingkan members.yaml ↔ profile di disk ↔ rute gateway ↔ channel Discord.

Hanya membaca. Mengembalikan kode keluar 0 bila tidak ada masalah ber-level "error", 1 bila ada.

  python3 kit_doctor.py                    # cek lokal (tanpa Discord)
  python3 kit_doctor.py --discord          # sekaligus cek channel di Discord (butuh token bot)
  python3 kit_doctor.py --docker           # sandbox per profile: status & RAM container, ukuran state.db
  python3 kit_doctor.py --format json

Yang diperiksa:
  profile      profile anggota aktif ada; profile yatim (ada di disk, tak ada di members.yaml)
  rute         rute di config host dan roster.json sama dengan yang diturunkan dari team.yaml
  parkir       profile aktif tidak berpenanda gateway.parked; profile nonaktif sebaiknya tidak dirutekan
  berkas       .env profile bermode 0600; plugin team-guard terpasang di tiap profile
  discord      channel ada, di server yang benar, bertipe teks, @everyone tidak bisa melihat (opsional)
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
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

ERROR, WARN, INFO = "error", "warn", "info"
SYSTEM_PROFILES_KEYS = ("shared", "admin")


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
    known = {m["profile"] for m in team["members"]} | {team["shared"]["profile"], team["admin"]["profile"],
                                                      team["template"]}
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


def check_usage(team: dict, rep: Report, now: Optional[float] = None) -> None:
    """Buku besar pemakaian: ada, privat, dan masih dipotret (timer systemd terpasang)."""
    import sqlite3
    import time
    ledger = Path(team["paths"]["state_dir"]) / "usage.db"
    fix = ("pasang timer: cp /opt/hermes-team/systemd/hermes-usage-collect.* ~/.config/systemd/user/ && "
           "systemctl --user daemon-reload && systemctl --user enable --now hermes-usage-collect.timer")
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
    profiles = [m["profile"] for m in team["members"]] + [team["shared"]["profile"], team["admin"]["profile"],
                                                          "default"]
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


def main(argv: Optional[list[str]] = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hermes_team as ht

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", help="team.yaml (default: ~/.hermes-team/team.yaml)")
    ap.add_argument("--members")
    ap.add_argument("--hermes-root", default=os.environ.get("HERMES_HOME") or "~/.hermes")
    ap.add_argument("--discord", action="store_true", help="periksa juga channel di Discord")
    ap.add_argument("--docker", action="store_true",
                    help="periksa sandbox Docker per profile (status, RAM) dan ukuran state.db")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        team = ht.load_team(Path(args.config).expanduser() if args.config else ht.default_team_file(),
                            Path(args.members).expanduser() if args.members else None)
    except ht.TeamError as exc:
        print(f"konfigurasi: {exc}", file=sys.stderr)
        return 2
    root = Path(args.hermes_root).expanduser()
    rep = Report()
    check_local(team, root, rep, ht)
    if args.discord:
        try:
            check_discord(team, rep, ht, ht.DiscordAPI(ht.discord_token(root)))
        except ht.TeamError as exc:
            rep.add(ERROR, "discord", f"tidak bisa memeriksa Discord: {exc}")
    if args.docker:
        check_docker(team, root, rep)
    print(rep.render(args.format))
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
