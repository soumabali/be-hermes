#!/usr/bin/env python3
"""teamctl_mcp.py — server MCP (stdio) berisi tool admin untuk tim Hermes (kit v3).

Dipasang hermes_team.py HANYA di profile team-admin (mcp_servers.teamctl), dan rute
#hermes-admin hanya melayani user admin. Tool di sini dipanggil oleh Hermes ketika admin
meminta lewat chat, mis. "jadikan @citra anggota tim" atau "buat channel #incident privat untuk @backend".

Isi:
- Tim: status (termasuk beban sekarang dari metrics.json plugin dan anggaran token hari ini), model,
  onboarding (onboard_member, onboarding_status), add_member, set_model, set_member_active,
  share_skill / unshare_skill, tambahan anggaran token harian (set/clear_usage_budget_extra).
- Server Discord (discord_*): ringkasan server & anggota, baca pesan channel non-pribadi, buat/ubah/
  hapus channel & role, atur izin channel, beri/cabut role, timeout/kick, kirim pengumuman.

Keamanan:
- Setiap aksi yang mengubah sesuatu meminta konfirmasi lewat MCP elicitation. Hermes
  menampilkannya sebagai tombol Approve/Deny di #hermes-admin; tanpa klik admin, tidak ada
  yang berubah. Klien yang tidak mendukung elicitation → aksi ditolak (fail closed).
  TIDAK ADA cara mematikan konfirmasi: variabel TEAMCTL_REQUIRE_CONSENT sudah dihapus (kalau masih
  diset, diabaikan dengan peringatan di stderr) dan tidak ada flag CLI penggantinya. Uji otomatis
  mengganti TeamCtl.confirm di dalam proses uji, bukan lewat environment.
- Yang disetujui = yang dijalankan: tiap tool menyusun rencana (plan) dari keadaan server saat itu.
  Setelah admin menyetujui, rencana disusun ULANG dari keadaan terbaru (di dalam kunci kerja); kalau
  ringkasannya berbeda dari yang disetujui, aksi dibatalkan ("keadaan berubah, ulangi").
  Permintaan yang dibatalkan klien (notifications/cancelled) atau klien yang terputus (EOF) membatalkan
  konfirmasi yang sedang menunggu — Approve yang terlambat tidak menjalankan apa pun.
- Ringkasan konfirmasi selalu memuat ID di samping nama (role/user/channel), semua nilai yang berubah,
  dan teks dari AI/user (nama, topik, alasan, isi pesan) yang sudah dibersihkan dari karakter kontrol,
  dikutip, dan dibatasi panjangnya. Isi discord_send_message ditampilkan utuh; yang dikirim persis
  yang ditampilkan.
- Pagar tetap (tidak bisa dibujuk lewat chat): channel pribadi anggota (dan thread di dalamnya) tidak
  bisa dibaca, diubah, dikirimi pesan, diubah izinnya, atau dihapus; channel/kategori/role tim tidak
  bisa dihapus, diganti nama, dipindah, atau dilepas izin khususnya; izin tinggi (Administrator,
  Manage Roles/Channels/Server/Webhooks, ban/kick, …) dan role admin tidak bisa diberikan — juga tidak
  lewat role yang membuka channel admin/pribadi; role berizin tinggi tidak bisa dicabut/dihapus/
  dilucuti; admin tim, pemilik server, bot, dan pemegang izin tinggi tidak bisa di-timeout/kick atau
  dicabut rolenya. Ban selalu manual.
- User dicocokkan persis (ID, mention, atau username persis); hasil pencarian yang hanya mirip
  dikembalikan sebagai daftar kandidat, bukan dipilih diam-diam.
- Hasil baca dari Discord (pesan, daftar anggota, ringkasan server) diapit penanda data ber-nonce;
  isi pesan ditulis per baris dengan awalan "| " sehingga tidak bisa memalsukan pesan/penanda lain.
- Tidak pernah mengembalikan atau menampilkan isi .env siapa pun. Nilai secret pemilik skill hanya
  dibaca di server untuk memastikan skill yang dibagikan tidak memuatnya. Token bot Discord dibaca
  dari .env host oleh proses ini saja (tidak pernah masuk scope profile atau konteks AI).
- Semua aksi dicatat ke <state>/audit.log beserta hasilnya (outcome ok/failed), termasuk aksi yang
  ditolak pagar (…-blocked), ditolak/tidak dijawab admin (…-refused), dan yang batal karena keadaan
  berubah (…-stale). Gagal menulis audit dilaporkan ke stderr. Alasan audit log Discord:
  "Hermes (teamctl): …".

Protokol: JSON-RPC 2.0 per baris (MCP stdio), tanpa dependensi selain PyYAML (lewat hermes_team).

Struktur kode (paket teamctl/, di folder yang sama):
- protocol.py (JSON-RPC, elicitation, pembatalan), confirm.py (Plan, _guarded, audit, pembersih teks),
  schema.py (validate_args), registry.py (@tool + panduan menambah tool), discord_guard.py (Server),
  tools_team.py / tools_usage.py / tools_discord.py (isi tool).
- Berkas ini = pintu masuk (`python3 teamctl_mcp.py --team …`) + fasad: nama lama (TeamCtl, TOOLS, ALL_TOOLS,
  ToolError, Server, validate_args, …) tetap bisa diimpor dari sini.
"""
from __future__ import annotations

import argparse
import itertools  # noqa: F401  (fasad: nama lama tetap ada di modul ini)
import json  # noqa: F401
import os
import re  # noqa: F401
import secrets  # noqa: F401
import shutil  # noqa: F401
import sqlite3  # noqa: F401
import sys
import threading  # noqa: F401
import time  # noqa: F401
import traceback  # noqa: F401
import unicodedata  # noqa: F401
from concurrent.futures import Future, ThreadPoolExecutor  # noqa: F401
from concurrent.futures import TimeoutError as FutureTimeout  # noqa: F401
from datetime import datetime, timedelta, timezone  # noqa: F401
from pathlib import Path
from typing import Any, Callable, Optional, Union  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Folder skrip harus ada di sys.path walau Python dijalankan dengan -I / PYTHONSAFEPATH (launcher tertentu
# memakainya); modul kit lain (kit_*, kit/, teamctl/) berada di folder yang sama.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import hermes_team as ht  # noqa: E402
from teamctl import tools_discord, tools_team, tools_usage  # noqa: E402,F401  (mendaftarkan semua @tool)
from teamctl.confirm import (GuardMixin, Plan, PlanFn, Planner, _clean, _clean_message, _INVISIBLE, _name,  # noqa: E402,F401
                             _q)
from teamctl.discord_guard import (ADMINISTRATOR, ALL_PERMS, CHANNEL_TYPES, GRANT_BLOCKED_MASK, GuardedAPI, THREAD_TYPES, TYPE_NAMES, VIEW,  # noqa: E402,F401
                                   VOICE_ALLOW, Server, _data_block, _parse_color, _perm_list, _pos, _TOKEN_RE)
from teamctl.errors import ToolError  # noqa: E402,F401
from teamctl.protocol import (ELICITATION_SINCE, HANDSHAKE_VERSIONS, INSTRUCTIONS, MAX_PENDING_ACTIONS,  # noqa: E402,F401
                              SERVER_VERSION, ProtocolMixin, _Call)
from teamctl.registry import REGISTRY, ToolSpec, group_enabled, tool  # noqa: E402,F401
from teamctl.schema import _STR, _STR_LIST, validate_args  # noqa: E402,F401
from teamctl.tools_discord import MAX_MESSAGE_CHARS  # noqa: E402,F401
from teamctl.tools_usage import (MAX_BUDGET_EXTRA, METRICS_MAX_AGE_SECONDS, UsageMixin, _age_text, _count,  # noqa: E402,F401
                                 _epoch, _tokens)

# Skema tools/list per grup, urutannya ditentukan registry (order=…); diturunkan dari dekorator @tool.
TOOLS: list[dict] = REGISTRY.tools("team")
DISCORD_TOOLS: list[dict] = REGISTRY.tools("discord")
ALL_TOOLS = TOOLS + DISCORD_TOOLS


class TeamCtl(UsageMixin, GuardMixin, ProtocolMixin):
    registry = REGISTRY

    def __init__(self, team_file: Path, members_file: Optional[Path], hermes_bin: str, hermes_root: Path,
                 consent_timeout: float = 330.0, out=None, inp=None, registry=None):
        self.team_file, self.members_file = team_file, members_file
        self.hermes_bin, self.hermes_root = hermes_bin, hermes_root
        if registry is not None:
            self.registry = registry
        self._init_protocol(consent_timeout, out, inp)

    # ------------------------------------------------------------------ helper
    def team(self) -> dict:
        return ht.load_team(self.team_file, self.members_file)

    def hermes(self) -> ht.Hermes:
        return ht.Hermes(self.hermes_bin, self.hermes_root, quiet=True)

    def api(self) -> ht.DiscordAPI:
        """Klien Discord. Selama tool BACA berjalan (run_tool, thread ini) klien ini baca-saja: non-GET ditolak."""
        read_only = getattr(self.local, "readonly_depth", 0) > 0
        return GuardedAPI(ht.discord_token(self.hermes_root), read_only=read_only)

    def server(self, team: dict) -> "Server":
        if not team["server_admin"]["enabled"]:
            raise ToolError("kelola server dimatikan (team.yaml → server_admin.enabled: false)")
        return Server(team, self.api())

    @staticmethod
    def _plugin_state(team: dict) -> dict:
        try:
            data = json.loads((Path(team["paths"]["state_dir"]) / "onboarding.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _activation_note(self, team: dict, result: dict) -> str:
        if result.get("restart_needed"):
            return "Setelan gateway berubah → ketik /restart supaya berlaku."
        live = self._plugin_state(team).get("live") or {}
        if team["live_reload"] and live.get("ok", True):
            return ("Rute aktif otomatis dalam beberapa detik (live reload, tanpa restart); bot mengabari di channel "
                    "ini dan mengirim pesan sambutan ke channel pribadinya.")
        return ("Live reload sedang tidak berjalan" + (f" ({live.get('detail')})" if live.get("detail") else "")
                + "; kalau channel baru belum dijawab bot dalam 1 menit, ketik /restart.")

    def _channel_note(self, team: dict, channel_id: str) -> str:
        """`#nama (id)` + siapa yang kini bisa melihatnya, untuk ringkasan (de)aktivasi. Tidak pernah gagal."""
        try:
            srv = Server(team, self.api())
            ch = srv.channel(channel_id)
            return f"{srv.channel_label(ch)} — saat ini bisa dilihat oleh: {'; '.join(srv.viewers(ch))}"
        except ToolError:
            return f"channel {channel_id} (SUDAH TIDAK ADA di server)"
        except ht.TeamError as exc:
            return f"channel {channel_id} (nama tidak bisa dicek di Discord: {_clean(exc, 120)})"


def _install_tool_methods(cls: type, registry=REGISTRY) -> None:
    """Kompatibilitas: tiap tool terdaftar juga tersedia sebagai `TeamCtl.tool_<nama>(self, args)`."""
    def make(spec: ToolSpec) -> Callable[[Any, dict], str]:
        def method(self, args: dict) -> str:
            return self.run_tool(spec, args)
        method.__name__ = f"tool_{spec.name}"
        return method
    for spec in registry.specs():
        setattr(cls, f"tool_{spec.name}", make(spec))


_install_tool_methods(TeamCtl)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Server MCP admin tim Hermes (stdio)")
    parser.add_argument("--team", required=True, help="path team.yaml")
    parser.add_argument("--members", default=None, help="path members.yaml")
    parser.add_argument("--hermes-bin", default=os.environ.get("HERMES_BIN") or None)
    parser.add_argument("--hermes-root", default=os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    args = parser.parse_args(argv)
    if os.environ.get("TEAMCTL_REQUIRE_CONSENT", "").strip().lower() in ("0", "false", "no", "off"):
        # Dulu mematikan semua konfirmasi. Sengaja dihapus: tidak ada mode tanpa konfirmasi.
        print("teamctl: PERINGATAN — TEAMCTL_REQUIRE_CONSENT diabaikan; konfirmasi admin SELALU wajib dan tidak "
              "bisa dimatikan.", file=sys.stderr, flush=True)
    server = TeamCtl(Path(args.team).expanduser(), Path(args.members).expanduser() if args.members else None,
                     ht.resolve_hermes_bin(args.hermes_bin), Path(args.hermes_root).expanduser())
    server.serve()


if __name__ == "__main__":
    main()
