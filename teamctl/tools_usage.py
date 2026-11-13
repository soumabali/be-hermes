"""Tool pemakaian & anggaran token: usage_report, tambahan jatah harian (set/clear_usage_budget_extra),
dan bagian "beban sekarang" + "anggaran hari ini" untuk team_status (dibaca dari metrics.json plugin team-guard).

`UsageMixin` memegang helper yang dipakai bersama (dan diuji lewat `TeamCtl._usage_day`, dst.)."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import hermes_team as ht

from .confirm import Plan, PlanFn, _clean, _q
from .errors import ToolError
from .registry import tool

# Plugin team-guard menulis <state>/metrics.json tiap ±30 detik; lebih tua dari ini = gateway/plugin tidak jalan.
METRICS_MAX_AGE_SECONDS = 120
MAX_BUDGET_EXTRA = 1_000_000_000


def _count(value: Any) -> int:
    """Angka dari berkas yang ditulis proses lain (metrics.json): apa pun yang bukan angka wajar → 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) > 1e15:
        return 0
    return int(value)


def _tokens(value: Any) -> str:
    return f"{_count(value):,}".replace(",", ".")


def _epoch(value: Any) -> Optional[float]:
    """`ts` di metrics.json: detik epoch atau ISO-8601. Tidak bisa dibaca → None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if 0 < value < 1e11 else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} dtk"
    if seconds < 7200:
        return f"{seconds // 60} menit"
    return f"{seconds // 3600} jam"


class UsageMixin:
    """Helper anggaran/beban untuk TeamCtl (butuh `self.team()` dari kelas induknya)."""

    @staticmethod
    def _read_metrics(team: dict, now: Optional[float] = None) -> tuple[Optional[dict], str]:
        """(isi metrics.json | None, keterangan). None = tidak ada/rusak; keterangan menyebut umurnya dan
        apakah datanya basi. Berkas ini ditulis plugin team-guard; teamctl hanya membacanya."""
        path = Path(team["paths"]["state_dir"]) / "metrics.json"
        now = time.time() if now is None else now
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return None, (f"belum ada data: {path} tidak ada. Plugin team-guard menulisnya saat gateway berjalan — "
                          "gateway belum jalan, atau plugin belum versi yang menulis metrik.")
        except (OSError, ValueError) as exc:
            return None, f"metrics.json tidak bisa dibaca ({_clean(exc, 120)})."
        if not isinstance(data, dict):
            return None, "metrics.json isinya tidak dikenali."
        age = now - (_epoch(data.get("ts")) or mtime)
        if age > METRICS_MAX_AGE_SECONDS:
            return data, (f"DATA BASI: metrik terakhir ditulis {_age_text(age)} lalu (seharusnya tiap ±30 detik) — "
                          "gateway atau plugin kemungkinan tidak berjalan. Angka di bawah BUKAN keadaan sekarang.")
        return data, f"metrik plugin {_age_text(age)} lalu"

    def _load_section(self, team: dict, metrics: Optional[dict], note: str) -> list[str]:
        if metrics is None:
            return ["Beban sekarang: " + note]
        scale = team["scale"]
        names = {m["user_id"]: m["name"] for m in team["members"]}
        configured = _count(metrics.get("configured_max_active")) or int(scale["max_concurrent_sessions"])
        effective = _count(metrics.get("effective_max_active")) or configured
        throttled = bool(metrics.get("throttled")) or effective < configured
        lines = [f"Beban sekarang ({note}):" if not note.startswith("DATA BASI") else f"Beban — {note}"]
        lines.append(f"- Sesi aktif: {_count(metrics.get('active_sessions'))} dari {configured}"
                     + (f" — batas efektif DITURUNKAN ke {effective} (429 beruntun; pulih sendiri)" if throttled else ""))
        limit = int(scale["max_llm_parallel"])
        lines.append(f"- Panggilan LLM berjalan: {_count(metrics.get('llm_inflight'))} "
                     f"(puncak 5 menit: {_count(metrics.get('llm_inflight_peak_5m'))}; "
                     f"batas {limit if limit else 'tidak diatur'})")
        wait = metrics.get("queue_wait_p95_s")
        wait = round(float(wait), 1) if isinstance(wait, (int, float)) and not isinstance(wait, bool) \
            and 0 <= wait < 1e6 else 0
        lines.append(f"- Antrean: {_count(metrics.get('queue_len'))} pesan, tunggu p95 {wait} dtk")
        refused = metrics.get("refused") if isinstance(metrics.get("refused"), dict) else {}
        lines.append(f"- Ditolak: rate limit {_count(refused.get('rate'))}, antrean penuh "
                     f"{_count(refused.get('queue_full'))}, anggaran {_count(refused.get('budget'))}, "
                     f"batas sesi per orang {_count(refused.get('user_concurrent'))}")
        lines.append(f"- HTTP 429 semenit terakhir: {_count(metrics.get('http_429_last_minute'))}")
        by_user = metrics.get("active_by_user") if isinstance(metrics.get("active_by_user"), dict) else {}
        busy = sorted(((_count(n), str(uid)) for uid, n in by_user.items() if _count(n) > 0), reverse=True)[:10]
        if busy:
            lines.append("- Sesi aktif per orang: " + ", ".join(
                f"{names.get(uid) or 'user ' + _clean(uid, 24)} {n}" for n, uid in busy))
        return lines

    @staticmethod
    def _usage_day(team: dict) -> str:
        """Hari ini (YYYY-MM-DD) di zona waktu pemakaian — sama dengan batas hari buku besar dan plugin."""
        import usage_ledger as ul
        try:
            return ul.day_of(time.time(), ul.resolve_tz(team["usage"]["timezone"] or None))
        except ul.LedgerError as exc:
            raise ToolError(str(exc))

    @staticmethod
    def _overrides_path(team: dict) -> Path:
        return Path(team["paths"]["state_dir"]) / "budget_overrides.json"

    def _read_overrides(self, team: dict) -> dict:
        """Isi budget_overrides.json yang bentuknya benar: {user_id: {"day": ..., "extra_tokens": int > 0}}."""
        try:
            raw = json.loads(self._overrides_path(team).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out: dict = {}
        for uid, entry in (raw.items() if isinstance(raw, dict) else []):
            if isinstance(entry, dict) and isinstance(entry.get("day"), str) and _count(entry.get("extra_tokens")) > 0:
                out[str(uid)] = {"day": entry["day"], "extra_tokens": _count(entry["extra_tokens"])}
        return out

    def _budget_section(self, team: dict, metrics: Optional[dict], fresh: bool) -> list[str]:
        scale = team["scale"]
        per_user, per_team = int(scale["daily_token_budget"]), int(scale["team_daily_token_budget"])
        if not per_user and not per_team:
            return ["Anggaran token: tidak aktif (scale.daily_token_budget dan team_daily_token_budget = 0)."]
        try:
            day = self._usage_day(team)
        except ToolError as exc:
            return [f"Anggaran token: tidak bisa dihitung ({_clean(exc, 160)})."]
        used: dict = {}
        team_used: Optional[int] = None
        budget = metrics.get("budget") if isinstance((metrics or {}).get("budget"), dict) else None
        if fresh and budget and budget.get("day") == day and isinstance(budget.get("used_by_user"), dict):
            used = {str(uid): _count(n) for uid, n in budget["used_by_user"].items()}
            team_used = _count(budget.get("team_used"))
            source = "hitungan plugin"
        else:
            import usage_ledger as ul
            try:
                data = ul.query(Path(team["paths"]["state_dir"]) / "usage.db",
                                datetime.strptime(day, "%Y-%m-%d").date(), datetime.strptime(day, "%Y-%m-%d").date())
                used = {u["user_key"]: _count(u["total"]["tokens"]) for u in data["users"]}
                team_used = _count(data["totals"]["tokens"])
                last = data.get("last_collect")
                source = "buku besar usage.db" + (f", potret {_age_text(time.time() - last)} lalu" if last else "")
            except (ul.LedgerError, sqlite3.Error, OSError) as exc:
                source = f"pemakaian belum bisa dibaca: {_clean(exc, 120)}"
        extras = {uid: e["extra_tokens"] for uid, e in self._read_overrides(team).items() if e["day"] == day}
        lines = [f"Anggaran token hari ini ({day}; {source}):",
                 f"- Batas: per orang {_tokens(per_user) if per_user else 'tanpa batas'}, "
                 f"tim {_tokens(per_team) if per_team else 'tanpa batas'}"]
        if team_used is not None:
            lines.append(f"- Terpakai tim: {_tokens(team_used)}"
                         + (f" ({team_used * 100 // per_team}% dari batas tim)" if per_team else ""))
        rows = []
        for m in ht.active_members(team):
            spent, extra = used.get(m["user_id"], 0), extras.get(m["user_id"], 0)
            if not spent and not extra:
                continue
            text = f"- {m['name']} ({m['user_id']}): {_tokens(spent)}"
            if per_user:
                limit = per_user + extra
                text += f" dari {_tokens(limit)} ({spent * 100 // limit}%)"
                text += " — HABIS" if spent >= limit else (" — di atas 80%" if spent * 5 >= limit * 4 else "")
            if extra:
                text += f" [termasuk tambahan hari ini +{_tokens(extra)}]"
            rows.append((spent, text))
        lines += [text for _, text in sorted(rows, reverse=True)] or ["- Belum ada pemakaian anggota hari ini."]
        return lines

    @staticmethod
    def _budget_member(team: dict, ref: Any) -> dict:
        """Anggota AKTIF dari nama, user ID, <@mention>, atau nama tampilan — selalu cocok PERSIS; yang
        hanya mirip tidak pernah dipilih diam-diam."""
        text = str(ref or "").strip()
        mention = re.fullmatch(r"<@!?([0-9]{15,21})>", text)
        key = mention.group(1) if mention else text.lstrip("@").strip()
        if not key:
            raise ToolError("argumen `user` wajib diisi (nama anggota, user ID, atau <@mention>)")
        members = team["members"]
        found = [m for m in members if m["user_id"] == key or m["name"] == key.lower()]
        if not found:
            found = [m for m in members if m["display_name"].casefold() == key.casefold()]
        if len(found) > 1:
            raise ToolError("lebih dari satu anggota cocok dengan " + _q(key, 60) + "; pakai nama anggota atau "
                            "user ID: " + ", ".join(f"{m['name']} ({m['user_id']})" for m in found))
        if not found:
            raise ToolError(f"anggota {_q(key, 60)} tidak ada (harus cocok persis). Anggota: "
                            + (", ".join(f"{m['name']} ({m['user_id']})" for m in members[:40]) or "-"))
        if not found[0].get("active", True):
            raise ToolError(f"anggota {found[0]['name']} ({found[0]['user_id']}) sedang nonaktif")
        return found[0]

    def _write_override(self, team: dict, user_id: str, day: str, extra: Optional[int]) -> None:
        """Tulis (extra) atau hapus (None) tambahan hari ini milik satu user. Baca-ubah-tulis di bawah kunci
        berkas, atomik, mode 0600. Entri hari lain sudah hangus → ikut dibuang supaya berkas tetap kecil."""
        path = self._overrides_path(team)
        with ht.file_lock(path.with_name(path.name + ".lock")):
            data = {uid: e for uid, e in self._read_overrides(team).items() if e["day"] == day}
            if extra is None:
                data.pop(user_id, None)
            else:
                data[user_id] = {"day": day, "extra_tokens": int(extra)}
            ht.atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)

    def _require_same_day(self, team: dict, day: str) -> None:
        """Dipanggil tepat sebelum menulis: tanggal pemakaian SAAT EKSEKUSI harus sama dengan yang ada di
        ringkasan yang disetujui. Kalau tengah malam baru saja lewat, tidak ada yang ditulis."""
        today = self._usage_day(team)
        if today != day:
            raise ToolError(f"hari sudah berganti sejak ringkasan disetujui (ringkasan: {day}, sekarang: {today}); "
                            "tidak ada yang ditulis — ulangi permintaannya untuk tanggal yang baru")


# ---------------------------------------------------------------------- tool
@tool(
    "usage_report",
    description="Pemakaian LLM per orang (request ke model, token masuk/keluar, jumlah sesi, biaya) untuk satu "
                "periode: harian, mingguan (Senin–Minggu), atau bulanan. Hanya-baca dan tanpa konfirmasi; data "
                "dipotret dulu supaya terbaru. Tidak memuat isi percakapan. Contoh: minggu lalu → period=week, "
                "offset=1; 7 hari terakhir per hari → period=day, last=7.",
    schema={
        "type": "object",
        "properties": {
            "period": {"type": "string", "enum": ["day", "week", "month"], "description": "bawaan: day"},
            "offset": {"type": "integer", "description": "0 = periode berjalan, 1 = periode sebelumnya, dst."},
            "last": {"type": "integer", "description": "N hari terakhir dengan rincian per hari (hanya period=day, 1–92)"},
            "user": {"type": "string", "description": "saring satu orang: nama anggota, nama tampilan, atau ID user"},
            "format": {"type": "string", "enum": ["markdown", "text", "json"], "description": "bawaan: markdown"},
        },
        "additionalProperties": False,
    },
    order=20,
)
def usage_report(ctl, args: dict) -> str:
    import usage_ledger as ul
    team = ctl.team()
    ledger = Path(team["paths"]["state_dir"]) / "usage.db"
    period = str(args.get("period") or "day")
    offset, last = int(args.get("offset") or 0), int(args.get("last") or 0)
    if not 0 <= offset <= 36:
        raise ToolError("offset harus 0–36")
    try:
        tz = ul.resolve_tz(team["usage"]["timezone"] or None)
        owners, people = ul.load_members(ctl.members_file)
        note = ""
        try:
            ul.collect(ledger, ctl.hermes_root, owners=owners, tz=tz)
        except (OSError, sqlite3.Error) as exc:      # tetap laporkan data terakhir
            note = f"\n\n(Potret terbaru gagal: {_clean(str(exc), 120)} — angka di atas dari potret sebelumnya.)"
        text = ul.report(ledger, period, offset=offset, last=last, user=(str(args["user"]) if args.get("user") else None),
                         fmt=str(args.get("format") or "markdown"), people=people, tz=tz)
    except ul.LedgerError as exc:
        raise ToolError(str(exc))
    return text + note


@tool(
    "set_usage_budget_extra",
    description="Tambah jatah token HARI INI untuk satu anggota di atas scale.daily_token_budget (mis. "
                "\"tambah kuota budi 2 juta token\" → extra_tokens=2000000). Menggantikan tambahan hari ini "
                "yang sudah ada (bukan menjumlahkan) dan hangus sendiri saat hari berganti. Hanya menaikkan batas "
                "orang itu; anggaran harian tim (team_daily_token_budget) TIDAK ikut naik. Butuh konfirmasi.",
    schema={
        "type": "object",
        "properties": {
            "user": {"type": "string", "description": "nama anggota, user ID, <@mention>, atau nama tampilan PERSIS"},
            "extra_tokens": {"type": "integer", "minimum": 1, "maximum": 1_000_000_000,
                             "description": "token tambahan untuk hari ini"},
        },
        "required": ["user", "extra_tokens"],
        "additionalProperties": False,
    },
    mutating=True, event="usage-budget-extra", order=100,
)
def set_usage_budget_extra(ctl, args: dict) -> PlanFn:
    extra = args.get("extra_tokens")
    if isinstance(extra, bool) or not isinstance(extra, int) or not 1 <= extra <= MAX_BUDGET_EXTRA:
        raise ToolError(f"extra_tokens harus bilangan bulat 1–{MAX_BUDGET_EXTRA}")

    def plan() -> Union[Plan, str]:
        team = ctl.team()
        member = ctl._budget_member(team, args.get("user"))
        base = int(team["scale"]["daily_token_budget"])
        if not base:
            raise ToolError("anggaran per orang tidak aktif (team.yaml → scale.daily_token_budget = 0), jadi "
                            "tidak ada batas yang perlu dilonggarkan")
        # `day` dihitung tiap kali rencana disusun: sekali untuk ringkasan, sekali lagi SETELAH disetujui.
        # Kalau tengah malam lewat di antaranya, ringkasan kedua memuat tanggal lain → tidak dijalankan
        # (lihat _guarded); `run` memeriksanya sekali lagi tepat sebelum menulis.
        day = ctl._usage_day(team)
        per_team = int(team["scale"]["team_daily_token_budget"])
        current = ctl._read_overrides(team).get(member["user_id"])
        previous = current["extra_tokens"] if current and current["day"] == day else 0
        who = f"{_q(member['display_name'], 64)} ({member['name']}, Discord user {member['user_id']})"
        if previous == extra:
            return f"{who} sudah punya tambahan {_tokens(extra)} token untuk {day}; tidak ada yang diubah."
        preview = (f"Tambah jatah token untuk tanggal {day}"
                   + (f" (zona {_clean(team['usage']['timezone'], 40)})" if team["usage"]["timezone"] else "")
                   + f" bagi {who}: +{_tokens(extra)} token di atas batas harian {_tokens(base)} → batas "
                   f"orang ini pada {day} menjadi {_tokens(base + extra)}."
                   + (f" Menggantikan tambahan sebelumnya (+{_tokens(previous)})." if previous else "")
                   + f" Hanya berlaku untuk tanggal {day} (hangus saat hari berganti). HANYA batas orang ini "
                   "yang naik: " + (f"anggaran harian tim tetap {_tokens(per_team)} dan tetap batas keras — "
                                    "kalau tim sudah mencapainya, tambahan ini tidak membuka apa-apa."
                                    if per_team else "anggaran tim tidak aktif dan tidak diubah."))

        def run() -> str:
            ctl._require_same_day(team, day)
            ctl._write_override(team, member["user_id"], day, extra)
            return (f"Jatah token {member['display_name']} untuk {day} sekarang "
                    f"{_tokens(base + extra)} (batas harian {_tokens(base)} + tambahan {_tokens(extra)}). "
                    "Plugin membacanya dalam ±1 menit; hari berikutnya kembali ke batas harian. "
                    "Anggaran tim tidak berubah.")

        return Plan(team, preview, run, member=member["name"], user=member["user_id"], day=day,
                    extra_tokens=extra, previous_extra=previous)

    return plan


@tool(
    "clear_usage_budget_extra",
    description="Cabut tambahan jatah token hari ini milik satu anggota (kembali ke scale.daily_token_budget). "
                "Butuh konfirmasi.",
    schema={
        "type": "object",
        "properties": {
            "user": {"type": "string", "description": "nama anggota, user ID, <@mention>, atau nama tampilan PERSIS"},
        },
        "required": ["user"],
        "additionalProperties": False,
    },
    mutating=True, event="usage-budget-extra-clear", order=110,
)
def clear_usage_budget_extra(ctl, args: dict) -> PlanFn:
    def plan() -> Union[Plan, str]:
        team = ctl.team()
        member = ctl._budget_member(team, args.get("user"))
        day = ctl._usage_day(team)
        current = ctl._read_overrides(team).get(member["user_id"])
        who = f"{_q(member['display_name'], 64)} ({member['name']}, Discord user {member['user_id']})"
        if not current or current["day"] != day:
            return f"{who} tidak punya tambahan jatah token untuk hari ini ({day}); tidak ada yang diubah."
        previous = current["extra_tokens"]
        base = int(team["scale"]["daily_token_budget"])
        preview = (f"Cabut tambahan jatah token hari ini ({day}) milik {who}: +{_tokens(previous)} dihapus → "
                   f"batas hari ini kembali {_tokens(base) if base else 'tanpa batas'}.")

        def run() -> str:
            ctl._require_same_day(team, day)
            ctl._write_override(team, member["user_id"], day, None)
            return (f"Tambahan jatah token {member['display_name']} untuk {day} dicabut "
                    f"(sebelumnya +{_tokens(previous)}).")

        return Plan(team, preview, run, member=member["name"], user=member["user_id"], day=day,
                    previous_extra=previous)

    return plan
