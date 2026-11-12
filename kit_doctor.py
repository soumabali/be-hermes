#!/usr/bin/env python3
"""doctor: bandingkan members.yaml ↔ profile di disk ↔ rute gateway ↔ channel Discord.

Hanya membaca. Mengembalikan kode keluar 0 bila tidak ada masalah ber-level "error", 1 bila ada.

  python3 kit_doctor.py                    # cek lokal (tanpa Discord)
  python3 kit_doctor.py --discord          # sekaligus cek channel di Discord (butuh token bot)
  python3 kit_doctor.py --format json

Yang diperiksa:
  profile      profile anggota aktif ada; profile yatim (ada di disk, tak ada di members.yaml)
  rute         rute di config host dan roster.json sama dengan yang diturunkan dari team.yaml
  parkir       profile aktif tidak berpenanda gateway.parked; profile nonaktif sebaiknya tidak dirutekan
  berkas       .env profile bermode 0600; plugin team-guard terpasang di tiap profile
  discord      channel ada, di server yang benar, bertipe teks, @everyone tidak bisa melihat (opsional)
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Optional

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
            rep.add(INFO, "parkir", f"{m['profile']} nonaktif tetapi belum diparkir",
                    f"hermes -p {m['profile']} gateway stop (opsional; tanpa rute ia tak menerima pesan)")
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
    print(rep.render(args.format))
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
