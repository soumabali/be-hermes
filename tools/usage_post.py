#!/usr/bin/env python3
"""usage_post.py — kirim laporan pemakaian LLM terjadwal ke satu channel Discord, TANPA LLM.

Angka diambil langsung dari buku besar (`usage_ledger.py`), jadi tidak ada yang bisa "dikarang" dan
laporan tetap terkirim walau endpoint LLM sedang mati. Dijalankan timer systemd tiap 15 menit
(systemd/hermes-usage-report.timer); tiap jalan ia menghitung laporan mana yang sudah jatuh tempo dan
belum terkirim, mengirimnya, lalu mencatatnya.

Konfigurasi di team.yaml (tulis jam DI DALAM TANDA KUTIP):
  guild_id: "100000000000000001"
  usage:
    timezone: "Asia/Makassar"            # kosong = zona waktu server
    report_channel_id: "400000000000000002"   # kosong = fitur mati
    reports:
      daily: "08:00"     # laporan KEMARIN, tiap hari mulai jam ini
      weekly: "08:05"    # laporan MINGGU LALU (Senin–Minggu), tiap Senin mulai jam ini
      monthly: "08:10"   # laporan BULAN LALU, tiap tanggal 1 mulai jam ini
                         # kosong / tidak ditulis = laporan itu mati

Semua jam memakai zona waktu `usage.timezone`. Tepat-sekali per periode: yang sudah terkirim dicatat di
<folder tim>/state/usage_posted.json ({jenis: label periode}, mode 0600), jadi tidak dobel walau timer
jalan berkali-kali atau server restart. Kalau server mati tepat pada jamnya, laporan dikirim pada jalan
berikutnya — tetapi hanya SATU periode terbaru per jenis; periode yang lebih lama tidak disusulkan.
Akibatnya, jalan pertama setelah fitur dinyalakan langsung mengirim periode terbaru tiap jenis yang aktif.

Keamanan:
- Token bot dibaca dari env / ~/.hermes/.env (`kit_discord.discord_token`), tidak pernah lewat argumen,
  tidak pernah dicetak, dan tidak pernah ditulis ke berkas catatan.
- Hanya mengirim ke `usage.report_channel_id`, dan hanya bila channel itu channel teks milik `guild_id`.
- Pesan dikirim dengan allowed_mentions kosong: tidak ada orang, role, maupun @everyone yang ter-ping.
- Isi pesan hanya angka dan nama tampilan anggota; tidak ada isi percakapan.

Pakai:
  python3 tools/usage_post.py                    # kirim yang jatuh tempo (dipakai timer)
  python3 tools/usage_post.py --list             # jadwal, jatuh tempo berikutnya, terakhir terkirim
  python3 tools/usage_post.py --dry-run          # cetak yang AKAN dikirim; tidak mengirim, tidak mencatat
  python3 tools/usage_post.py --force weekly     # kirim laporan minggu lalu sekarang juga (ikut dicatat)
  python3 tools/usage_post.py --team ~/.hermes-team/team.yaml --members-file ... --hermes-root ~/.hermes

Butuh PyYAML (sudah dipakai kit). Kode keluar: 0 = selesai / tidak ada yang jatuh tempo / fitur mati;
1 = pengiriman atau buku besar gagal (dicoba lagi pada jalan berikutnya); 2 = konfigurasi/argumen salah
atau channel bukan channel teks milik server tim.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

KIT = Path(__file__).resolve().parent.parent
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

import kit_discord  # noqa: E402
import usage_ledger  # noqa: E402
from kit_base import TeamError  # noqa: E402

KINDS = ("daily", "weekly", "monthly")
PERIOD = {"daily": "day", "weekly": "week", "monthly": "month"}
KIND_LABEL = {"daily": "harian", "weekly": "mingguan", "monthly": "bulanan"}
CHUNK_LIMIT = 1900             # batas Discord 2000; sisakan ruang
FENCE = "```"
TEXT_CHANNEL = 0               # GUILD_TEXT
STATE_NAME = "usage_posted.json"
Out = Callable[[str], None]


class ConfigError(Exception):
    """Konfigurasi atau argumen salah (kode keluar 2)."""


# ----------------------------------------------------------------------------- konfigurasi
def parse_hhmm(value: Any, where: str) -> Optional[tuple]:
    """"HH:MM" → (jam, menit); kosong → None. YAML 1.1 membaca 21:30 tanpa kutip sebagai bilangan
    basis-60 (1290), jadi bilangan bulat 0–1439 diterima sebagai menit sejak tengah malam."""
    if value is None or value is False or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1440:
        return divmod(value, 60)
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value).strip())
    if not match:
        raise ConfigError(f"{where}: '{value}' bukan jam HH:MM (mis. \"08:00\", tulis di dalam tanda kutip)")
    return int(match.group(1)), int(match.group(2))


def load_config(team_file: Path) -> dict:
    """Baca bagian yang diperlukan dari team.yaml. → {guild_id, timezone, channel_id, schedule}."""
    try:
        import yaml
    except ImportError:
        raise ConfigError("PyYAML tidak terpasang (pip install pyyaml)")
    try:
        data = yaml.safe_load(Path(team_file).read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigError(f"{team_file} tidak terbaca ({type(exc).__name__})")
    except yaml.YAMLError as exc:
        raise ConfigError(f"{team_file} bukan YAML yang valid ({type(exc).__name__})")
    if not isinstance(data, dict):
        raise ConfigError(f"{team_file}: isi harus berupa pemetaan")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    channel = str(usage.get("report_channel_id") or "").strip()
    if channel and not channel.isdigit():
        raise ConfigError("usage.report_channel_id harus ID channel Discord (angka, di dalam tanda kutip)")
    reports = usage.get("reports") if isinstance(usage.get("reports"), dict) else {}
    unknown = sorted(str(k) for k in reports if k not in KINDS)
    if unknown:
        raise ConfigError(f"usage.reports: jenis tidak dikenal: {', '.join(unknown)} (daily, weekly, monthly)")
    schedule = {kind: parse_hhmm(reports.get(kind), f"usage.reports.{kind}") for kind in KINDS}
    return {"guild_id": str(data.get("guild_id") or "").strip(), "timezone": str(usage.get("timezone") or "").strip(),
            "channel_id": channel, "schedule": schedule}


def usage_tz(name: str):
    """Sama seperti kolektor: HERMES_TEAM_TZ → usage.timezone → zona waktu server."""
    name = os.environ.get("HERMES_TEAM_TZ") or name
    if name:
        try:
            return usage_ledger.resolve_tz(name)
        except usage_ledger.LedgerError as exc:
            raise ConfigError(str(exc))
    return datetime.now().astimezone().tzinfo


def parse_now(text: Optional[str], tz) -> datetime:
    if not text:
        return datetime.now(tz)
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ConfigError(f"--now '{text}' bukan waktu ISO (mis. 2026-11-16T08:00 atau 2026-11-16T00:00:00+00:00)")
    return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)


# ----------------------------------------------------------------------------- jadwal (logika murni)
def _is_due_day(kind: str, day: date) -> bool:
    return kind == "daily" or (kind == "weekly" and day.weekday() == 0) or (kind == "monthly" and day.day == 1)


def last_due_day(kind: str, hhmm: tuple, now: datetime) -> date:
    """Tanggal jatuh tempo terakhir yang sudah lewat (≤ now), dalam zona waktu `now`."""
    for back in range(0, 63):
        day = now.date() - timedelta(days=back)
        if _is_due_day(kind, day) and datetime.combine(day, dtime(*hhmm), tzinfo=now.tzinfo) <= now:
            return day
    raise AssertionError("tidak ada jatuh tempo dalam 62 hari")     # pragma: no cover


def next_due(kind: str, hhmm: tuple, now: datetime) -> datetime:
    """Jatuh tempo berikutnya (> now)."""
    for ahead in range(0, 63):
        day = now.date() + timedelta(days=ahead)
        moment = datetime.combine(day, dtime(*hhmm), tzinfo=now.tzinfo)
        if _is_due_day(kind, day) and moment > now:
            return moment
    raise AssertionError("tidak ada jatuh tempo dalam 62 hari")     # pragma: no cover


def period_for(kind: str, anchor: date, today: date) -> dict:
    """Periode yang selesai tepat sebelum `anchor` (hari jatuh tempo) + offset-nya dihitung dari `today`."""
    start, end, label = usage_ledger.period_range(PERIOD[kind], anchor, 1)
    offset = next(k for k in range(0, 4) if usage_ledger.period_range(PERIOD[kind], today, k)[2] == label)
    return {"kind": kind, "label": label, "start": start, "end": end, "offset": offset}


def due_reports(schedule: dict, now: datetime, posted: dict) -> list:
    """Laporan yang jatuh tempo dan belum terkirim: paling banyak SATU (periode terbaru) per jenis.
    Label periode terurut secara leksikografis, jadi catatan yang lebih baru tidak pernah ditimpa mundur."""
    due = []
    for kind in KINDS:
        hhmm = schedule.get(kind)
        if not hhmm:
            continue
        item = period_for(kind, last_due_day(kind, hhmm, now), now.date())
        sent = posted.get(kind)
        if not (isinstance(sent, str) and sent >= item["label"]):
            due.append(item)
    return due


# ----------------------------------------------------------------------------- catatan terkirim
def load_posted(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in KINDS and isinstance(v, str)} if isinstance(data, dict) else {}


def save_posted(path: Path, posted: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({k: posted[k] for k in KINDS if k in posted}, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def run_lock(path: Path):
    """Satu pengirim pada satu waktu (timer vs --force manual). → True bila kunci didapat."""
    lock = Path(path).with_name(Path(path).name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # pragma: no cover - non-POSIX
            pass
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fh)


# ----------------------------------------------------------------------------- bentuk pesan Discord
def _clip(line: str, width: int) -> str:
    return line if len(line) <= width else line[: width - 1] + "…"


def chunk_messages(items: list, limit: int = CHUNK_LIMIT) -> list:
    """items: [(di_dalam_blok_kode, baris)] → daftar pesan ≤ `limit` karakter, dipotong di batas baris.
    Blok kode tidak pernah terbelah: tiap pesan menutup blok yang dibukanya, dan sambungannya membuka
    blok baru dengan mengulang baris judul kolom."""
    width = max(20, limit // 4)
    items = [(code, _clip(line.replace(FENCE, "'''") if code else line, width)) for code, line in items]
    header = next((line for code, line in items if code), None)
    chunks: list = []
    cur: list = []
    open_ = False

    def size(extra: list) -> int:
        return sum(len(x) + 1 for x in cur) + sum(len(x) + 1 for x in extra)

    def flush() -> None:
        nonlocal cur, open_
        if open_:
            cur.append(FENCE)
            open_ = False
        if cur:
            chunks.append("\n".join(cur))
        cur = []

    for code, line in items:
        add = ([FENCE] if code != open_ else []) + [line]
        if cur and size(add + ([FENCE] if code else [])) > limit:
            flush()
            add = ([FENCE] + ([header] if header and line != header else []) if code else []) + [line]
        cur.extend(add)
        open_ = code
    flush()
    return chunks


def discord_messages(text_report: str, note: str = "", limit: int = CHUNK_LIMIT) -> list:
    """Laporan format teks dari usage_ledger → pesan Discord: judul tebal, tabel di dalam blok kode
    (tabel markdown tidak dirender Discord), lalu catatan kaki."""
    lines = [line.rstrip() for line in text_report.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    title = lines[0] if lines else "Pemakaian LLM"
    rest = lines[1:]
    while rest and not rest[0]:
        rest.pop(0)
    cut = rest.index("") if "" in rest else len(rest)
    items = [(False, "**" + title.replace("*", "") + "**")]
    items += [(True, line) for line in rest[:cut]]
    items += [(False, line) for line in rest[cut:] if line]
    if note:
        items.append((False, note))
    return chunk_messages(items, limit)


# ----------------------------------------------------------------------------- Discord
def check_channel(api: Any, channel_id: str, guild_id: str) -> None:
    """Tolak bila channel bukan channel teks milik server tim. ConfigError → kode keluar 2."""
    if not guild_id:
        raise ConfigError("guild_id di team.yaml kosong; tidak bisa memastikan channel laporan milik server tim")
    channel = api.get_channel(channel_id)
    if not isinstance(channel, dict):
        raise ConfigError(f"channel laporan {channel_id} tidak ditemukan (usage.report_channel_id)")
    if str(channel.get("guild_id") or "") != guild_id:
        raise ConfigError(f"channel laporan {channel_id} bukan milik server tim (guild_id {guild_id}); tidak dikirim")
    if channel.get("type") != TEXT_CHANNEL:
        raise ConfigError(f"channel laporan {channel_id} bukan channel teks (type {channel.get('type')}); tidak dikirim")


# ----------------------------------------------------------------------------- main
def main(argv: Optional[list] = None, *, api_factory: Optional[Callable[[str], Any]] = None,
         collect: Optional[Callable[..., Any]] = None, out: Out = print, err: Optional[Out] = None) -> int:
    """`api_factory(token)` → objek ber-`get_channel` dan `send_message` (bawaan kit_discord.DiscordAPI);
    `collect` bawaan usage_ledger.collect. Keduanya bisa disuntikkan di uji."""
    err = err or (lambda text: print(text, file=sys.stderr))
    team_dir_default = Path(os.environ.get("HERMES_TEAM_DIR") or "~/.hermes-team").expanduser()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team", default="", help="team.yaml (bawaan: ~/.hermes-team/team.yaml)")
    ap.add_argument("--members-file", default="", help="members.yaml (bawaan: di samping team.yaml)")
    ap.add_argument("--hermes-root", default=os.environ.get("HERMES_HOME") or "~/.hermes")
    ap.add_argument("--now", default="", help="waktu ISO pengganti jam sekarang (untuk uji)")
    ap.add_argument("--dry-run", action="store_true", help="cetak yang akan dikirim; tidak mengirim, tidak mencatat")
    ap.add_argument("--force", choices=KINDS, help="kirim laporan itu sekarang, apa pun jadwal dan catatannya")
    ap.add_argument("--list", action="store_true", help="tampilkan jadwal, jatuh tempo berikutnya, terakhir terkirim")
    args = ap.parse_args(argv)

    team_file = Path(args.team).expanduser() if args.team else team_dir_default / "team.yaml"
    team_dir = team_file.parent
    members = Path(args.members_file).expanduser() if args.members_file else team_dir / "members.yaml"
    hermes_root = Path(args.hermes_root).expanduser()
    state_file = team_dir / "state" / STATE_NAME
    ledger = team_dir / "state" / "usage.db"
    token = ""

    def safe(text: Any) -> str:
        text = str(text)
        return text.replace(token, "«token»") if token else text

    try:
        cfg = load_config(team_file)
        tz = usage_tz(cfg["timezone"])
        now = parse_now(args.now, tz)
        posted = load_posted(state_file)
        schedule = cfg["schedule"]

        if args.list:
            out(f"Zona waktu: {getattr(tz, 'key', None) or now.tzname()}  |  sekarang: {now:%Y-%m-%d %H:%M}")
            out(f"Channel laporan: {cfg['channel_id'] or '(kosong — laporan terjadwal mati)'}")
            pending = {item["kind"]: item for item in due_reports(schedule, now, posted)}
            for kind in KINDS:
                hhmm = schedule[kind]
                sent = posted.get(kind) or "belum pernah"
                if not hhmm:
                    out(f"  {kind:<8} mati                 terakhir terkirim: {sent}")
                    continue
                upcoming = (f"SEKARANG (periode {pending[kind]['label']} belum terkirim)" if kind in pending
                            else f"{next_due(kind, hhmm, now):%Y-%m-%d %H:%M}")
                out(f"  {kind:<8} jam {hhmm[0]:02d}:{hhmm[1]:02d}  berikutnya: {upcoming}  terakhir terkirim: {sent}")
            return 0

        if not cfg["channel_id"]:
            if args.force:
                raise ConfigError("usage.report_channel_id kosong; isi dulu di team.yaml")
            if args.dry_run:
                out("usage_post: laporan terjadwal mati (usage.report_channel_id kosong)")
            return 0

        if args.force:
            todo = [period_for(args.force, now.date(), now.date())]
        else:
            todo = due_reports(schedule, now, posted)
        if not todo:
            if args.dry_run:
                out("usage_post: tidak ada laporan yang jatuh tempo")
            return 0

        # dry-run tidak menyentuh folder state sama sekali (tanpa berkas kunci)
        with (contextlib.nullcontext(True) if args.dry_run else run_lock(state_file)) as locked:
            if not locked:
                out("usage_post: jalan lain sedang mengirim; dilewati")
                return 0
            if not args.force:                      # catatan bisa berubah selagi menunggu kunci
                posted = load_posted(state_file)
                todo = due_reports(schedule, now, posted)
                if not todo:
                    return 0

            api = None
            if not args.dry_run:
                token = kit_discord.discord_token(hermes_root)
                api = api_factory(token) if api_factory else kit_discord.DiscordAPI(token)
                check_channel(api, cfg["channel_id"], cfg["guild_id"])

            owners, people = usage_ledger.load_members(members)
            note = ""
            if not args.dry_run:      # dry-run tidak mengubah apa pun, termasuk buku besar
                try:
                    (collect or usage_ledger.collect)(ledger, hermes_root, owners=owners, now=now.timestamp(), tz=tz)
                except Exception as exc:  # usaha terbaik: laporan tetap dikirim dari data terakhir
                    note = (f"Catatan: potret pemakaian terbaru gagal ({type(exc).__name__}); "
                            "angka di atas memakai data terakhir.")
                    err(f"usage_post: collect gagal ({type(exc).__name__}); memakai data terakhir")

            failed = 0
            for item in todo:
                kind, label = item["kind"], item["label"]
                try:
                    text = usage_ledger.report(ledger, PERIOD[kind], offset=item["offset"], fmt="text",
                                               people=people, now=now.timestamp(), tz=tz)
                except usage_ledger.LedgerError as exc:
                    err(f"usage_post: laporan {KIND_LABEL[kind]} {label} tidak bisa dibuat: {safe(exc)}")
                    failed += 1
                    continue
                messages = discord_messages(text, note)
                if args.dry_run:
                    out(f"--- [dry-run] laporan {KIND_LABEL[kind]} {label} → channel {cfg['channel_id']} "
                        f"({len(messages)} pesan; tidak dikirim, tidak dicatat, channel tidak diperiksa)")
                    for message in messages:
                        out(message)
                    continue
                try:
                    for message in messages:
                        api.send_message(cfg["channel_id"], message)       # allowed_mentions kosong
                except TeamError as exc:
                    err(f"usage_post: laporan {KIND_LABEL[kind]} {label} gagal dikirim: {safe(exc)}")
                    failed += 1
                    continue
                if not (isinstance(posted.get(kind), str) and posted[kind] > label):
                    posted[kind] = label
                try:
                    save_posted(state_file, posted)        # dicatat per laporan: gagal di tengah tidak mengulang yang sudah terkirim
                except OSError as exc:
                    err(f"usage_post: laporan terkirim tetapi {state_file} tidak bisa ditulis "
                        f"({type(exc).__name__}); laporan ini bisa terkirim ulang")
                    return 1
                out(f"usage_post: laporan {KIND_LABEL[kind]} {label} terkirim ({len(messages)} pesan)")
            return 1 if failed else 0
    except ConfigError as exc:
        err(f"usage_post: {safe(exc)}")
        return 2
    except kit_discord.DiscordHTTPError as exc:
        err(f"usage_post: {safe(exc)}")
        return 1
    except TeamError as exc:                       # token tidak ada, Discord tidak terjangkau
        err(f"usage_post: {safe(exc)}")
        return 2 if "DISCORD_BOT_TOKEN" in str(exc) else 1


if __name__ == "__main__":
    sys.exit(main())
