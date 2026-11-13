#!/usr/bin/env python3
"""hermes_team.py — provisioning & operasi Hermes Agent untuk satu tim di Discord.

Satu instalasi Hermes, satu bot Discord, satu gateway (multiplex), satu profile per orang.

File (di $HERMES_TEAM_DIR, bawaan ~/.hermes-team/; atau folder team.yaml yang ditunjuk --config.
./team.yaml di direktori kerja TIDAK dipakai diam-diam):
  team.yaml     setelan tim, ditulis manusia: guild, role, admin, channel bersama/admin,
                katalog model, sandbox, batas skala, kebijakan secret, onboarding.
  members.yaml  daftar anggota + model per orang. Ditulis manusia ATAU otomatis: onboarding
                (role @backend diberikan), tool admin di chat (teamctl), add-member / set-model.
  state/        ditulis script: roster.json (dibaca plugin team-guard), shared-skills/, audit.log.

Perintah:
  check          validasi + peta channel → profile → model
  apply          buat/perbarui profile, plugin team-guard, MCP admin, setelan gateway, roster
                 (--only andi,budi untuk sebagian anggota; --host-only untuk gateway saja)
  managed        cetak /etc/hermes/config.yaml (kebijakan global yang tidak bisa diubah user);
                 --diff bandingkan dengan yang terpasang, --install pasang (sebagai root)
  host-yaml      cetak setelan gateway sebagai YAML (untuk dicek / ditempel manual)
  add-member     tambah anggota (opsional: buat channel Discord privat otomatis)
  onboard        anggota dari user Discord: channel privat + profile + rute (idempoten; dipanggil
                 plugin saat role @backend diberikan, juga oleh tool admin)
  offboard       nonaktifkan anggota dari user Discord (data tetap; dipanggil plugin saat role dicabut)
  set-model      ganti model AI anggota / shared / admin / lobby
  set-sandbox    batas RAM/CPU sandbox khusus satu anggota (--memory-mb, --cpu, --reset)
  set-active     aktif/nonaktifkan anggota berdasarkan nama
  remove-member  hapus PERMANEN anggota yang sudah nonaktif dari members.yaml
                 (--delete-profile, --delete-channel, --yes)
  list-skills    daftar skill milik seorang anggota
  share-skill    bagikan skill seorang anggota ke anggota lain atau semua anggota (ditolak bila pemindai
                 keamanan Hermes tidak memberi verdict "safe"; --allow-unscanned bila pemindai tidak ada)
  unshare-skill  tarik kembali skill yang pernah dibagikan
  llm-key        simpan API key LLM khusus satu profile (input tersembunyi; --stdin untuk pipe)
  status         ringkasan kondisi instalasi

Semua perintah yang mengubah sesuatu memegang kunci tim (<team_dir>/.team.lock), jadi onboarding
otomatis, tool admin, dan CLI tidak pernah saling menimpa.

Kode inti ada di paket `kit/` (constants, templates, profiles, util, config, derive, hermes_cli, skills,
provision, membership, cli); berkas ini pintu masuk CLI sekaligus FASAD: `import hermes_team as ht` tetap
menyediakan semua nama publik maupun privat yang dipakai teamctl, kit_setup, kit_doctor, plugin, dan uji.

Jalankan sebagai user Linux yang menjalankan Hermes (bukan root). Kesalahan tak terduga dicetak satu
baris; set HERMES_TEAM_DEBUG=1 untuk traceback lengkap.
"""
from __future__ import annotations

import argparse  # noqa: F401  (fasad: nama modul ini dulu terbuka lewat `hermes_team.<modul>`)
import base64  # noqa: F401
import contextlib  # noqa: F401
import copy  # noqa: F401
import difflib  # noqa: F401
import getpass  # noqa: F401
import hashlib  # noqa: F401
import http.client  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import shlex  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import tempfile  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
import traceback  # noqa: F401
import unicodedata  # noqa: F401
import urllib.error  # noqa: F401
import urllib.parse  # noqa: F401
import urllib.request  # noqa: F401
from datetime import datetime, timezone  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Callable, Iterable, Optional  # noqa: F401

# Folder skrip harus ada di sys.path walau Python dijalankan dengan -I / PYTHONSAFEPATH (launcher tertentu
# memakainya); modul kit lain (kit_*, kit/, teamctl/) berada di folder yang sama.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import kit_paths  # noqa: F401
from kit_base import (ID_RE, KIT_VERSION, TeamError, _closing_quote, _env_entries, _id,  # noqa: F401
                      _unquote_double, parse_env_text, rewrite_env_text)
from kit_discord import (PERMISSIONS, PERMISSION_ALIASES, ELEVATED_PERMISSIONS, PERM, MEMBER_ALLOW, BOT_ALLOW,  # noqa: F401
                         ELEVATED_MASK, permission_bits, permission_names, DiscordHTTPError, discord_token,
                         private_overwrites, MemberList, DiscordAPI)

try:
    import yaml  # noqa: F401
except ImportError:  # pragma: no cover
    sys.exit("PyYAML belum ada. Pasang dulu: `sudo apt install python3-yaml` "
             "atau `python3 -m pip install --user pyyaml`.")

try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# --- Fasad: salin semua nama modul kit/ ke namespace ini. Urutan = urutan lapisan. Catatan untuk uji:
# monkeypatch pada nama di sini TIDAK terlihat oleh kode di kit/ (yang mencari nama di modulnya sendiri);
# uji yang perlu menimpa fungsi memakai `patch_kit` (tests/conftest.py) yang menimpa di semua modul kit/.
from kit import (cli as _m_cli, config as _m_config, constants as _m_constants, derive as _m_derive,  # noqa: E402
                 hermes_cli as _m_hermes_cli, membership as _m_membership, provision as _m_provision,
                 skills as _m_skills, templates as _m_templates, util as _m_util)

for _mod in (_m_constants, _m_templates, _m_util, _m_config, _m_derive, _m_hermes_cli, _m_skills, _m_provision,
             _m_membership, _m_cli):
    for _name, _value in vars(_mod).items():
        if not _name.startswith("__"):
            globals()[_name] = _value
del _mod, _name, _value

main = _m_cli.main

if __name__ == "__main__":
    main()
