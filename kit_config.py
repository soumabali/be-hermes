#!/usr/bin/env python3
"""kit_config.py — SKEMA team.yaml: satu tabel `FIELDS`, satu validator, satu sumber default.

Sebelumnya default tersebar (SCALE_DEFAULTS, SECRETS_DEFAULTS, ONBOARDING_DEFAULTS, ...), validasi per bagian
ditulis tangan, dan setelan yang ditegakkan plugin disalin manual ke roster.json. Kini:

  * tiap setelan = SATU baris `Field` (path YAML, tipe, default, batas/pilihan, deskripsi, dan — bila plugin
    team-guard yang menegakkannya — `plugin_key` + `plugin_section`);
  * `parse_section()` / `parse_top_level()` mengubah YAML mentah menjadi nilai bertipe, dengan pesan
    `TeamError` yang menyebut key-nya;
  * `unknown_key_warnings()` memberi peringatan key tak dikenal + "maksudnya …?" (difflib);
  * `plugin_view(team, "settings"|"onboarding"|"usage"|"root")` MENURUNKAN isi roster.json dari tabel yang
    sama — menambah setelan yang ditegakkan plugin = satu baris di sini + titik pakainya;
  * `usage_view(raw)` memberi tampilan `usage.*` tervalidasi untuk alat ringan (usage_ledger, usage_post, ...);
  * `python3 kit_config.py --markdown` mencetak tabel dokumentasi; `--emit-example-section <bagian>` mencetak
    cuplikan YAML bawaan; `check_example()` dipakai uji agar team.example.yaml tidak ketinggalan.

Hanya stdlib (+ PyYAML hanya untuk `check_example` dan CLI-nya). Bentuk akses lama tetap: `team["scale"]`,
`team["secrets"]`, `team["strict"]`, `team["member_role"]`, ... diisi dari hasil skema ini.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from kit.constants import PROFILE_RE, REPORT_KINDS
from kit.profiles import MODEL_TARGETS, PROFILE_KINDS, SYSTEM_KINDS
from kit.templates import DEFAULT_WELCOME, DEFAULT_WELCOME_BACK
from kit_base import TeamError, _id


# --------------------------------------------------------------------------- pengurai nilai tunggal
def _profile(name: str, where: str) -> str:
    if not PROFILE_RE.match(name or "") or name in ("main", "default"):
        raise TeamError(f"{where}: nama profile '{name}' tidak valid "
                        "(huruf kecil, angka, '-' atau '_', maks 64 karakter)")
    return name


_TRUE_WORDS = {"true", "yes", "y", "on", "1", "ya", "aktif"}
_FALSE_WORDS = {"false", "no", "n", "off", "0", "tidak", "mati", ""}


def _bool(value: Any, where: str, default: bool = False) -> bool:
    """Boolean dari YAML. `bool("false")` bernilai True di Python, jadi string diurai eksplisit."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    raise TeamError(f"{where}: '{value}' bukan boolean (pakai true atau false)")


def _int(value: Any, where: str, minimum: Optional[int] = None) -> int:
    """Bilangan bulat dari YAML; salah ketik → TeamError yang menyebut key-nya (bukan traceback)."""
    number: Optional[int] = None
    if isinstance(value, bool) or value is None:
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and value == int(value):
        number = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        number = int(value.strip())
    if number is None:
        raise TeamError(f"{where}: '{value}' bukan bilangan bulat")
    if minimum is not None and number < minimum:
        raise TeamError(f"{where}: minimal {minimum} (sekarang {number})")
    return number


def _number(value: Any, where: str) -> Any:
    if isinstance(value, bool) or value is None:
        raise TeamError(f"{where}: '{value}' bukan angka")
    if isinstance(value, (int, float)):
        number: Any = value
    else:
        try:
            number = float(str(value).strip())
        except ValueError:
            raise TeamError(f"{where}: '{value}' bukan angka")
        if number == int(number):
            number = int(number)
    if number <= 0:
        raise TeamError(f"{where}: harus lebih dari 0")
    return number


def _report_time(value: Any, where: str) -> str:
    """Jam laporan "HH:MM" (24 jam, waktu lokal usage.timezone). Kosong/false = laporan itu mati."""
    if value is None or value is False or value == "":
        return ""
    if isinstance(value, (int, float)):
        # YAML 1.1 membaca 10:30 tanpa kutip sebagai bilangan (630), jadi jamnya sudah hilang di sini.
        raise TeamError(f"{where}: tulis jam di dalam tanda kutip, mis. \"08:00\" (sekarang {value!r})")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise TeamError(f"{where}: '{value}' bukan jam \"HH:MM\" 24 jam (mis. \"08:00\"); kosongkan untuk mematikan")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


# --------------------------------------------------------------------------- tabel
@dataclass(frozen=True)
class Field:
    """Satu setelan team.yaml.

    path         path YAML bertitik, mis. "scale.user_rate_per_minute" atau "guild_id".
    type         bool | int | number | str | choice | id | id_opt | ids | strs | text | minutes | time | profile |
                 custom (divalidasi kode khusus di kit/config.py: llm, extra_routes, user_allowed_commands).
    default      nilai bawaan (atau fungsi(ctx) → nilai bila bergantung pada setelan lain).
    desc         satu kalimat untuk dokumentasi.
    min / max    batas untuk int (dan panjang untuk text).
    choices      pilihan untuk type "choice"; lower=True → dinormalkan huruf kecil dulu.
    plugin_key   nama key di roster.json bagian `plugin_section` bila setelan ini DITEGAKKAN plugin team-guard.
    plugin_section  "settings" | "onboarding" | "usage" | "root" (key tingkat atas roster.json).
    team_key     letak hasil di dict `team` bila bukan `path` (mis. strict_personal_routes → "strict").
    doc          False = tidak wajib ada di team.example.yaml (setelan lama/tersembunyi).
    """
    path: str
    type: str
    default: Any
    desc: str
    min: Optional[int] = None
    max: Optional[int] = None
    choices: tuple = ()
    lower: bool = False
    strip: bool = True
    keep_empty: bool = False
    plugin_key: str = ""
    plugin_section: str = "settings"
    team_key: str = ""
    doc: bool = True

    @property
    def section(self) -> str:
        return self.path.rpartition(".")[0]

    @property
    def key(self) -> str:
        return self.path.rpartition(".")[2]

    @property
    def target(self) -> str:
        """Letak hasil di dict `team` (path bertitik)."""
        return self.team_key or self.path

    def default_value(self, ctx: Optional[dict] = None) -> Any:
        value = self.default(ctx or {}) if callable(self.default) else self.default
        return copy.deepcopy(value)

    def default_text(self) -> str:
        return self.default.__doc__ or "(turunan)" if callable(self.default) else repr(self.default)


def _prefixed(suffix: str) -> Callable[[dict], str]:
    def default(ctx: dict) -> str:
        return f"{ctx.get('profile_prefix', 'be-')}{suffix}"
    default.__doc__ = f"<profile_prefix>{suffix}"
    return default


def _row(path: str, type: str, default: Any, desc: str, **kw: Any) -> Field:
    return Field(path, type, default, desc, **kw)


def _plugin(path: str, type: str, default: Any, desc: str, key: str = "", section: str = "settings", **kw: Any) -> Field:
    return Field(path, type, default, desc, plugin_key=key or path.rpartition(".")[2], plugin_section=section, **kw)


def _build_fields() -> tuple[Field, ...]:
    """Seluruh tabel. Baris per jenis profile (`<kind>.*`, `llm.<kind>_model`) DIBANGUN dari registri
    kit.profiles setiap dipanggil, jadi jenis yang terdaftar ikut dapat blok + baris model tanpa baris manual."""
    return (
    # --- tingkat atas
    _row("team_name", "str", "Backend", "Nama tim (muncul di SOUL.md dan pesan sambutan).", strip=False),
    _row("guild_id", "id", None, "ID server Discord."),
    _row("roles.member", "id", None, "Role anggota (@backend); diberikan → onboarding otomatis.", team_key="member_role"),
    _row("roles.admin", "id", None, "Role admin (@hermes-admin).", team_key="admin_role"),
    _row("roles.entry", "id_opt", "", "Role untuk user baru di server (opsional).", team_key="entry_role"),
    _row("admins", "ids", [], "User ID admin tim (minimal satu): boleh /update, /restart, dan semua perintah."),
    _row("category_id", "id_opt", "", "Kategori Discord untuk channel pribadi baru (opsional)."),
    _row("channel_prefix", "str", "hermes-", "Awalan nama channel pribadi (#hermes-andi).", strip=False),
    _row("profile_prefix", "str", "be-", "Awalan nama profile anggota (be-andi).", strip=False, keep_empty=True,
         team_key="prefix"),
    _row("template_profile", "profile", _prefixed("template"), "Profile sumber clone untuk profile pribadi.",
         team_key="template"),
    *(f for kind in SYSTEM_KINDS for f in (
        _row(f"{kind}.profile", "profile", f"team-{kind}", f"Nama profile {kind}."),
        _row(f"{kind}.channel_id", "id", None, f"ID channel profile {kind}."),
        _row(f"{kind}.auto_skills", "strs", [], f"Skill yang dimuat tiap sesi baru di channel {kind}."),
    )),
    _row("team_skills_dir", "str", "", "Folder skill tim (read-only untuk anggota); kosong = tidak ada."),
    _row("team_skills_in_personal", "bool", True, "Skill tim juga tersedia di profile pribadi."),
    _row("strict_personal_routes", "bool", False, "Rute channel pribadi juga mengunci user_id pemiliknya.",
         team_key="strict"),
    _row("strict_admin_routes", "bool", True, "Rute channel admin dikunci per user_id admin.", team_key="admin_strict"),
    _row("lobby_restrict", "bool", True, "Profile default (lobby) hanya teks: toolset dimatikan, tanpa memory."),
    _row("allow_browser", "bool", False, "Izinkan browser/computer-use (berjalan di HOST, bukan sandbox)."),
    _row("threads.personal", "bool", True, "Channel pribadi: tiap pesan baru membuka thread baru."),
    _row("threads.admin", "bool", False, "#hermes-admin ikut membuat thread (false = satu aliran)."),
    _plugin("live_reload", "bool", True,
            "Rute & channel baru diterapkan plugin ke gateway yang jalan (tanpa restart).", section="root"),
    _plugin("restart_fallback", "choice", "idle",
            "Bila live reload gagal: idle = restart halus otomatis saat sepi; manual = admin mengetik /restart.",
            section="root", choices=("idle", "manual"), lower=True),
    _row("user_allowed_commands", "custom", [], "Daftar perintah slash anggota (menggantikan bawaan kit).",
         team_key="user_commands"),
    _row("extra_routes", "custom", [], "Rute gateway tambahan (diteruskan apa adanya)."),
    _row("members", "custom", [], "(lama) anggota di team.yaml; sekarang di members.yaml.", doc=False),
    # --- llm (divalidasi kit/config.py::_load_llm karena saling merujuk)
    _row("llm.providers", "custom", {}, "Provider LLM OpenAI-compatible: nama → {api, key_env, ...}."),
    _row("llm.models", "custom", {}, "Katalog model: alias → {provider, model, [compression_threshold_tokens, "
         "proactive_prune_tokens]}."),
    _row("llm.default_model", "custom", "", "Model anggota tanpa `model` di members.yaml."),
    *(_row(f"llm.{kind}_model", "custom", "", f"Model profile {PROFILE_KINDS[kind].model_label or kind}.")
      for kind in MODEL_TARGETS if PROFILE_KINDS[kind].alias_kind == kind),
    _row("llm.aux_model", "custom", "", "Model murah untuk tugas auxiliary (kompresi, judul, review, cron)."),
    _row("llm.fallback", "custom", [], "Rantai model cadangan saat model utama error/throttle."),
    _row("llm.per_user_keys", "custom", False, "true = tiap profile boleh punya API key sendiri (llm-key)."),
    # --- sandbox
    _row("sandbox.image", "str", "", "Image Docker sandbox; kosong = bawaan Hermes."),
    _row("sandbox.cpu", "number", 1, "CPU bawaan per profile (pengecualian per anggota: 0.25..64)."),
    _row("sandbox.memory_mb", "int", 1024, "RAM bawaan per profile (MB); plafon, bukan cadangan.", min=64),
    _row("sandbox.network", "str", "", "Jaringan Docker untuk sandbox; kosong = bawaan."),
    _row("sandbox.http_proxy", "str", "", "Proxy HTTP(S) untuk sandbox; kosong = tanpa proxy."),
    _row("sandbox.no_proxy", "str", "localhost,127.0.0.1,::1", "Daftar NO_PROXY sandbox.", strip=False),
    # --- scale (sebagian ditegakkan plugin lewat roster.json → settings)
    _plugin("scale.max_concurrent_sessions", "int", 8, "Slot sesi aktif bersamaan untuk anggota (seluruh gateway).",
            key="max_active", min=0),
    _row("scale.admin_reserved_slots", "int", 2, "Slot tambahan khusus admin (tidak ikut antre).", min=0),
    _plugin("scale.user_rate_per_minute", "int", 6, "Pesan per menit per orang (0 = tanpa batas).",
            key="rate_per_minute", min=0),
    _plugin("scale.user_burst", "int", 4, "Pesan beruntun yang langsung lolos.", key="burst", min=0),
    _plugin("scale.queue_max_wait_seconds", "int", 180, "Lama antre saat slot penuh sebelum pesan ditolak sopan.",
            key="queue_max_wait", min=0),
    _row("scale.agent_max_turns", "int", 60, "Langkah tool per giliran.", min=0),
    _row("scale.goals_max_turns", "int", 4, "Lanjutan otomatis /goal (× agent_max_turns langkah dari satu pesan).",
         min=0),
    _row("scale.delegation_max_children", "int", 1, "Subagent paralel per giliran (tiap subagent = satu percakapan LLM).",
         min=0),
    _row("scale.compression_threshold_tokens", "int", 64000, "Kompres konteks paling lambat di angka ini "
         "(bawaan; per model: llm.models).", min=0),
    _row("scale.proactive_prune_tokens", "int", 48000, "Pangkas output tool lama tanpa LLM di angka ini.", min=0),
    _row("scale.idle_compact_after_seconds", "int", 3600, "Sesi yang dibuka lagi setelah sekian detik diringkas dulu.",
         min=0),
    _row("scale.busy_input_mode", "choice", "queue", "Pesan susulan saat bot sibuk: queue, interrupt, atau steer.",
         choices=("queue", "interrupt", "steer")),
    _row("scale.title_llm", "bool", False, "Judul sesi pakai LLM? false = judul instan, tanpa request."),
    _plugin("scale.user_max_concurrent", "int", 2, "Sesi aktif per orang (minimal 1); pesan berikutnya antre.", min=1),
    _plugin("scale.max_llm_parallel", "int", 0, "Panggilan LLM paralel seluruh tim (0 = tidak dibatasi); "
            "isi dari tools/probe_llm.py.", min=0),
    _plugin("scale.daily_token_budget", "int", 0, "Token per orang per hari (0 = tanpa batas).", min=0),
    _plugin("scale.team_daily_token_budget", "int", 0, "Token seluruh tim per hari (0 = tanpa batas).", min=0),
    _plugin("scale.adaptive_throttle", "bool", True, "Turunkan batas sesi efektif saat 429 beruntun, pulih perlahan."),
    _row("scale.allow_member_cron", "bool", False, "false = toolset cronjob mati untuk anggota & profile bersama."),
    _plugin("scale.cron_min_interval_minutes", "int", 15, "Jadwal cron lebih rapat dari ini ditolak plugin "
            "(0 = tanpa batas).", min=0),
    _row("scale.allow_goals", "bool", False, "false = /goal dan /subgoal keluar dari daftar perintah bawaan anggota."),
    _row("scale.session_retention_days", "int", 90, "Sesi berakhir yang lebih tua dipangkas Hermes "
         "(0 = kit tidak mengatur).", min=0),
    # --- usage (plugin membaca timezone + lokasi buku besar lewat roster.json → usage)
    _plugin("usage.timezone", "str", "", "Zona waktu batas hari laporan & anggaran (mis. Asia/Makassar); "
            "kosong = zona waktu server.", section="usage"),
    _row("usage.report_channel_id", "id_opt", "", "Channel tujuan laporan pemakaian terjadwal (bukan channel pribadi)."),
    *(_row(f"usage.reports.{kind}", "time", "", f"Jam laporan {kind} \"HH:MM\"; kosong = mati.") for kind in REPORT_KINDS),
    # --- secrets (seluruhnya ditegakkan plugin)
    _plugin("secrets.max_keys", "int", 40, "Jumlah secret per profile.", min=1),
    _plugin("secrets.min_length", "int", 6, "Nilai lebih pendek ditolak (tidak bisa disamarkan dengan aman).", min=1),
    _plugin("secrets.scrub_chat", "bool", True, "Kredensial yang diketik di chat disamarkan sebelum sampai ke AI."),
    _plugin("secrets.delete_secret_messages", "bool", True, "Pesan aslinya dihapus (bot butuh Manage Messages)."),
    _plugin("secrets.block_env_dump", "bool", True, "Blokir env/printenv/set/echo $SECRET di terminal."),
    _plugin("secrets.hint", "bool", True, "Beri tahu AI NAMA secret yang tersedia (bukan nilainya)."),
    _plugin("secrets.allow_names", "strs", [], "Pengecualian nama yang biasanya dilarang."),
    _plugin("secrets.deny_names", "strs", [], "Nama tambahan yang dilarang."),
    # --- onboarding (seluruhnya ditegakkan plugin lewat roster.json → onboarding)
    _plugin("onboarding.enabled", "bool", True, "Role @backend diberikan → channel + profile + sambutan otomatis.",
            section="onboarding"),
    _plugin("onboarding.give_entry_role", "bool", True, "User baru di server mendapat roles.entry.",
            section="onboarding"),
    _plugin("onboarding.remove_entry_role", "bool", True, "Role entry dicabut setelah onboarding selesai.",
            section="onboarding"),
    _plugin("onboarding.verify_actor", "bool", True, "Hanya jika pemberi @backend adalah admin tim (audit log Discord).",
            section="onboarding"),
    _plugin("onboarding.trusted_actor_ids", "ids", [], "Bot lain yang boleh memberi @backend (mis. bot verifikasi).",
            section="onboarding"),
    _plugin("onboarding.offboard_on_role_remove", "bool", True, "@backend dicabut / user keluar → anggota dinonaktifkan.",
            section="onboarding"),
    _plugin("onboarding.hide_channel_on_offboard", "bool", True, "Izin melihat channel pribadi dicabut saat offboard.",
            section="onboarding"),
    _plugin("onboarding.entry_role_on_offboard", "bool", True, "User yang masih di server kembali mendapat roles.entry.",
            section="onboarding"),
    _plugin("onboarding.reconcile_minutes", "minutes", 10, "Cek berkala @backend yang terlewat saat bot offline "
            "(0 = mati).", section="onboarding"),
    _plugin("onboarding.admin_notices", "bool", True, "Kabar onboarding ke channel admin.", section="onboarding"),
    _plugin("onboarding.welcome_message", "text", DEFAULT_WELCOME, "Pesan sambutan di channel pribadi anggota baru "
            "(maks 1900 karakter; placeholder {mention} {display_name} {name} {team_name} {model} {channel} "
            "{shared_channel} {admin_channel} {profile}).", section="onboarding", max=1900),
    _plugin("onboarding.welcome_back_message", "text", DEFAULT_WELCOME_BACK, "Pesan saat anggota diaktifkan kembali.",
            section="onboarding", max=1900),
    # --- server_admin
    _row("server_admin.enabled", "bool", True, "Tool discord_* di #hermes-admin."),
    _row("server_admin.allow_moderation", "bool", True, "Timeout & kick (ban selalu manual)."),
    _row("server_admin.allow_elevated_permissions", "bool", False,
         "Izin tinggi (Administrator, Manage Roles, ...) lewat Hermes."),
    _row("server_admin.read_messages", "bool", True, "discord_read_messages (channel pribadi selalu ditolak)."),
    )

FIELDS: tuple[Field, ...] = ()
FIELD_BY_PATH: dict[str, Field] = {}


@dataclass(frozen=True)
class Section:
    hint: bool = False                  # pesan "harus mapping" menyebut daftar key-nya
    unknown: str = "warn"               # "warn" (diabaikan + peringatan) | "error"
    falsy_ok: bool = False              # `false`/null dibaca sebagai mapping kosong


SECTIONS: dict[str, Section] = {}
WARN_ORDER: tuple[str, ...] = ()


def rebuild_schema() -> None:
    """Bangun ulang FIELDS / FIELD_BY_PATH / SECTIONS / WARN_ORDER dari registri jenis profile.

    Dipanggil sekali saat impor. Uji yang mendaftarkan jenis palsu di kit.profiles memanggilnya lagi (dan sekali
    lagi setelah registri dipulihkan); kode produksi tidak perlu."""
    global FIELDS, FIELD_BY_PATH, SECTIONS, WARN_ORDER
    FIELDS = _build_fields()
    FIELD_BY_PATH = {f.path: f for f in FIELDS}
    assert len(FIELD_BY_PATH) == len(FIELDS), "path ganda di FIELDS"
    SECTIONS = {
        "roles": Section(),
        **{kind: Section() for kind in SYSTEM_KINDS},
        "threads": Section(hint=True),
        "sandbox": Section(),
        "scale": Section(),
        "usage": Section(hint=True),
        "usage.reports": Section(hint=True, unknown="error", falsy_ok=True),
        "secrets": Section(),
        "onboarding": Section(unknown="error"),
        "server_admin": Section(),
        "llm": Section(),
    }
    # Urutan bagian saat melaporkan key tak dikenal (sama dengan perilaku lama).
    WARN_ORDER = ("roles", *SYSTEM_KINDS, "threads", "sandbox", "scale", "usage", "secrets", "server_admin", "llm")


rebuild_schema()

# Key opsional di llm.models.<alias> dan di members.yaml (hanya untuk peringatan key tak dikenal + dokumentasi).
LLM_MODEL_KEYS = ("provider", "model", "compression_threshold_tokens", "proactive_prune_tokens")
MEMBERS_FILE_KEYS = ("members", "models")
MEMBER_KEYS = ("name", "display_name", "discord_user_id", "channel_id", "model", "active", "forward_env", "sandbox")


def llm_model_keys() -> tuple[str, ...]:
    """Key `llm.*` yang menamai model: default, satu per jenis target set-model (dari registri), aux."""
    return ("default_model", *(f"{k}_model" for k in MODEL_TARGETS if PROFILE_KINDS[k].alias_kind == k), "aux_model")


def fields_in(section: str) -> list[Field]:
    """Baris tabel langsung di bawah `section` ("" = tingkat atas)."""
    return [f for f in FIELDS if f.section == section]


def section_keys(section: str) -> tuple[str, ...]:
    """Key yang dikenal langsung di `section` ("" = tingkat atas): field-nya dan sub-bagiannya."""
    prefix = section + "." if section else ""
    return tuple(dict.fromkeys(f.path[len(prefix):].partition(".")[0] for f in FIELDS if f.path.startswith(prefix)))


def top_level_keys() -> tuple[str, ...]:
    return section_keys("")


def defaults(section: str) -> dict:
    """{key: default} untuk field langsung di `section` (salinan baru; sub-bagian ikut sebagai dict)."""
    out: dict = {}
    for f in fields_in(section):
        out[f.key] = f.default_value()
    for name in SECTIONS:
        if name.rpartition(".")[0] == section:
            out[name.rpartition(".")[2]] = defaults(name)
    return out


# --------------------------------------------------------------------------- validator generik
def _choice_error(f: Field, where: str) -> TeamError:
    names = list(f.choices)
    text = " atau ".join(names) if len(names) == 2 else ", ".join(names[:-1]) + ", atau " + names[-1]
    return TeamError(f"{where} harus {text}")


def _parse_value(f: Field, value: Any, where: str, present: bool = True) -> Any:
    kind = f.type
    if kind == "bool":
        return _bool(value, where, bool(f.default))
    if kind == "int":
        return _int(value, where, f.min)
    if kind == "number":
        return _number(value, where)
    if kind == "minutes":
        return max(0, _int(value or 0, where))
    if kind == "str":
        if f.keep_empty and present and value is not None:
            text = str(value)
        else:
            text = str(value or f.default or "")
        return text.strip() if f.strip else text
    if kind == "choice":
        if f.lower:
            value = str(value or f.default).strip().lower()
        if value not in f.choices:
            raise _choice_error(f, where)
        return value
    if kind == "id":
        return _id(value, where)
    if kind == "id_opt":
        return _id(value, where) if value else ""
    if kind == "ids":
        return [_id(v, f"{where}[{i}]") for i, v in enumerate(value or [])]
    if kind == "strs":
        if value and not isinstance(value, (list, tuple)):          # "", {}, 0, False = kosong (perilaku lama)
            raise TeamError(f"{where}: harus daftar")
        return [str(v) for v in (value or [])]
    if kind == "text":
        text = str(value or "").strip()
        if not text or len(text) > (f.max or 1900):
            raise TeamError(f"{where}: wajib diisi, maks {f.max or 1900} karakter (batas Discord 2000)")
        return text
    if kind == "time":
        return _report_time(value, where)
    if kind == "profile":
        return _profile(str(value or ""), where)
    raise ValueError(f"tipe field tidak dikenal: {kind} ({where})")


def _blank_profile(f: Field, value: Any) -> bool:
    """Nama profile yang dikosongkan (None, "", 0, false, [], {}) = pakai bawaan, seperti kit lama.
    Nilai tak-kosong yang salah tetap ditolak dengan pesan yang menyebut key-nya."""
    return f.type == "profile" and not value


def _mapping(raw: Any, section: str) -> dict:
    spec = SECTIONS.get(section, Section())
    if raw is None or (spec.falsy_ok and raw is False):
        return {}
    if not isinstance(raw, dict):
        hint = f" ({', '.join(section_keys(section))})" if spec.hint else ""
        raise TeamError(f"{section}: harus mapping{hint}")
    return raw


def parse_section(section: str, raw: Any, ctx: Optional[dict] = None) -> dict:
    """Validasi satu bagian (dan sub-bagiannya) → {path_lengkap: nilai} untuk SEMUA field di bawahnya.

    Key yang tak dikenal: ditolak (TeamError) untuk bagian `unknown="error"`, selain itu hanya dilaporkan
    `unknown_key_warnings`. Field bertipe "custom" dilewati (divalidasi kode khusus)."""
    mapping = _mapping(raw, section)
    spec = SECTIONS.get(section, Section())
    if spec.unknown == "error":
        known = section_keys(section)
        unknown = sorted(str(k) for k in mapping if k not in known)
        if unknown:
            raise TeamError(f"{section}.{unknown[0]}: tidak dikenal (pilihan: "
                            f"{', '.join(known) if section != 'usage.reports' else ', '.join(REPORT_KINDS)})")
    ctx = ctx if ctx is not None else {}
    out: dict = {}
    for f in fields_in(section):
        if f.type == "custom":
            continue
        present = f.key in mapping
        value = mapping[f.key] if present and not _blank_profile(f, mapping[f.key]) else f.default_value(ctx)
        out[f.path] = _parse_value(f, value, f.path, present)
    for name in SECTIONS:
        if name.rpartition(".")[0] == section and name != "llm":
            key = name.rpartition(".")[2]
            out.update(parse_section(name, mapping.get(key), ctx))
    return out


def _put(node: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _dig(node: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def parse_top_level(raw: dict) -> dict:
    """Validasi semua field non-custom di `raw` → dict `team` parsial dalam bentuk lama
    (`team["scale"]`, `team["member_role"]`, `team["strict"]`, `team["shared"]["channel_id"]`, ...).

    Bagian yang bergantung pada setelan lain (llm, user_commands, extra_routes, members) diisi kit/config.py."""
    team: dict = {}
    ctx: dict = {}
    parsed_sections: set[str] = set()
    for f in FIELDS:
        if f.type == "custom":
            continue
        root = f.path.partition(".")[0]
        if f.section == "":
            present = f.key in raw
            given = raw[f.key] if present and not _blank_profile(f, raw[f.key]) else f.default_value(ctx)
            value = parse_one(f, given, present)
            _put(team, f.target, value)
            ctx[f.key] = value
        elif root not in parsed_sections:
            parsed_sections.add(root)
            for path, value in parse_section_tree(root, raw.get(root), ctx).items():
                _put(team, FIELD_BY_PATH[path].target, value)
    return team


def parse_one(f: Field, value: Any, present: bool = True) -> Any:
    return _parse_value(f, value, f.path, present)


def parse_section_tree(root: str, raw: Any, ctx: Optional[dict] = None) -> dict:
    return parse_section(root, raw, ctx)


# --------------------------------------------------------------------------- key tak dikenal
def _unknown_keys(block: Any, known: Iterable[str], where: str, source: str, warnings: list[str]) -> None:
    """Catat key di `block` yang tidak ada di `known`, dengan saran "maksudnya …?" (difflib)."""
    if not isinstance(block, dict):
        return
    known = [str(k) for k in known]
    for key in block:
        if str(key) in known:
            continue
        label = f"{where}.{key}" if where else str(key)
        near = difflib.get_close_matches(str(key), known, n=1, cutoff=0.6)
        hint = f"maksudnya '{near[0]}'?" if near else "pilihan: " + ", ".join(sorted(known))
        warnings.append(f"{source}: key '{label}' tidak dikenal dan DIABAIKAN ({hint})")


def unknown_key_warnings(raw: dict) -> list[str]:
    """Peringatan key tak dikenal di team.yaml (teratas, per bagian, dan llm.models.<alias>)."""
    warnings: list[str] = []
    src = "team.yaml"
    _unknown_keys(raw, top_level_keys(), "", src, warnings)
    for section in WARN_ORDER:
        _unknown_keys(raw.get(section), section_keys(section), section, src, warnings)
    llm = raw.get("llm")
    if isinstance(llm, dict) and isinstance(llm.get("models"), dict):
        for alias, block in llm["models"].items():
            _unknown_keys(block, LLM_MODEL_KEYS, f"llm.models.{alias}", src, warnings)
    return warnings


def members_unknown_key_warnings(mraw: dict) -> list[str]:
    warnings: list[str] = []
    src = "members.yaml"
    _unknown_keys(mraw, MEMBERS_FILE_KEYS, "", src, warnings)
    for i, entry in enumerate(mraw.get("members") or []):
        if isinstance(entry, dict):
            _unknown_keys(entry, MEMBER_KEYS, f"members[{i}]" + (f" ({entry['name']})" if entry.get("name") else ""),
                          src, warnings)
    return warnings


# --------------------------------------------------------------------------- turunan untuk plugin
def _cast(f: Field, value: Any) -> Any:
    if f.type == "int" or f.type == "minutes":
        return int(value)
    if f.type == "bool":
        return bool(value)
    if f.type in ("strs", "ids"):
        return list(value or [])
    return value


def plugin_fields(section: str = "settings") -> list[Field]:
    return [f for f in FIELDS if f.plugin_key and f.plugin_section == section]


def plugin_view(team: dict, section: str = "settings") -> dict:
    """Isi bagian roster.json untuk plugin, DITURUNKAN dari tabel (hanya field ber-`plugin_key`)."""
    return {f.plugin_key: _cast(f, _dig(team, f.target)) for f in plugin_fields(section)}


def usage_view(raw: Any) -> dict:
    """`usage.*` tervalidasi dari isi MENTAH team.yaml (dict utuh) → {"timezone", "report_channel_id", "reports"}.
    Dipakai usage_ledger, usage_report, tools/usage_post, kit_doctor lewat kit_paths.usage_view()."""
    block = raw.get("usage") if isinstance(raw, dict) else None
    flat = parse_section("usage", block)
    out: dict = {}
    for path, value in flat.items():
        _put(out, path[len("usage."):], value)
    return out


# --------------------------------------------------------------------------- dokumentasi
def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if value is None:
        return '""'
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def markdown_table() -> str:
    """Tabel Markdown seluruh FIELDS: key, tipe, default, ditegakkan plugin?, deskripsi."""
    lines = ["| key | tipe | default | plugin | deskripsi |", "|---|---|---|---|---|"]
    for f in FIELDS:
        if f.type == "custom" and f.path == "members":
            continue
        default = "wajib" if f.default is None else (f.default_text() if callable(f.default) else _yaml_scalar(f.default))
        if f.type == "text":
            default = "(bawaan kit)"
        kind = f.type + (f" ({'/'.join(f.choices)})" if f.choices else "")
        if f.type == "int" and f.min is not None and f.min > 0:
            kind += f" ≥{f.min}"
        plug = f"{f.plugin_section}.{f.plugin_key}" if f.plugin_key else ""
        desc = f.desc.replace("|", "\\|")
        lines.append(f"| `{f.path}` | {kind} | `{default}` | {plug} | {desc} |")
    return "\n".join(lines) + "\n"


def example_section(section: str) -> str:
    """Cuplikan YAML bawaan satu bagian (default + deskripsi sebagai komentar). Komentar tulisan tangan di
    team.example.yaml tidak diganti; ini hanya bahan untuk menyalin baris yang kurang."""
    chosen = [f for f in FIELDS if f.path == section or f.path.startswith(section + ".")]
    if not chosen:
        raise TeamError(f"bagian '{section}' tidak ada (pilihan: {', '.join(top_level_keys())})")
    lines: list[str] = []
    stack: list[str] = []
    for f in chosen:
        parts = f.path.split(".")
        while stack and stack != parts[:len(stack)]:
            stack.pop()
        for depth in range(len(stack), len(parts) - 1):
            lines.append("  " * depth + f"{parts[depth]}:")
            stack.append(parts[depth])
        value = "" if f.default is None or callable(f.default) else _yaml_scalar(f.default_value())
        lines.append("  " * (len(parts) - 1) + f"{parts[-1]}: {value}".rstrip() + f"   # {f.desc}")
    return "\n".join(lines) + "\n"


def check_example(text: str) -> tuple[list[str], list[str]]:
    """Bandingkan isi team.example.yaml dengan FIELDS → (field yang belum didokumentasikan, key di contoh
    yang tidak ada di skema). Key yang hanya muncul sebagai komentar `# key:` dihitung terdokumentasi."""
    import yaml
    data = yaml.safe_load(text) or {}
    commented = set(re.findall(r"^\s*#\s*([A-Za-z_][A-Za-z0-9_]*):", text, re.MULTILINE))
    documented: set[str] = set()
    unknown: list[str] = []

    def walk(node: Any, trail: str) -> None:
        for key, value in node.items():
            path = f"{trail}{key}"
            if path in FIELD_BY_PATH:
                documented.add(path)
            elif path in SECTIONS and isinstance(value, dict):
                walk(value, path + ".")
            else:
                unknown.append(path)

    if isinstance(data, dict):
        walk(data, "")
    # llm.* adalah bagian khusus: anak-anaknya didaftar sebagai field "custom".
    missing = [f.path for f in FIELDS
               if f.doc and f.path not in documented and f.key not in commented]
    return missing, unknown


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Skema team.yaml (kit_config.FIELDS).")
    parser.add_argument("--markdown", action="store_true", help="cetak tabel field sebagai Markdown")
    parser.add_argument("--emit-example-section", metavar="BAGIAN",
                        help="cetak cuplikan YAML bawaan satu bagian (mis. scale, secrets, onboarding)")
    args = parser.parse_args(argv)
    try:
        if args.emit_example_section:
            sys.stdout.write(example_section(args.emit_example_section))
        else:
            sys.stdout.write(markdown_table())
    except TeamError as exc:
        print(f"kesalahan: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
