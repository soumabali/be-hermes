"""Bagian dari kit tim (dipecah dari hermes_team.py). Lihat docs/KONTRAK.md dan README."""
from __future__ import annotations
import re
from pathlib import Path
import kit_paths


KIT_DIR = Path(__file__).resolve().parent.parent      # folder kit (berisi hermes_team.py, kit/, plugins/, ...)


PLUGIN_NAME = "team-guard"


PLUGIN_SRC = KIT_DIR / "plugins" / PLUGIN_NAME


TEAMCTL = KIT_DIR / "teamctl_mcp.py"


KIT_SKILLS = KIT_DIR / "skills"


DEFAULT_TEAM_DIR = kit_paths.team_dir()     # nilai saat impor; pakai kit_paths.team_dir() untuk nilai terkini


PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")     # aturan nama profile Hermes


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}\Z")         # nama anggota


ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")


ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}\Z")




# Perintah slash untuk anggota non-admin. Admin selalu boleh semuanya.
# Sengaja TIDAK termasuk: model, moa, reasoning, fast, provider-ish, tools, toolsets, skills (hub),
# config, reload, update, restart, yolo, approvals, busy, voice, memory, reload-mcp, kanban, loop,
# heartbeat, cron, sethome, debug, egress, plugins, pause, platform(s), snapshot, export, import.
DEFAULT_USER_COMMANDS = [
    "new", "reset", "retry", "undo", "status", "stop", "steer", "queue", "compress", "title",
    "resume", "sessions", "usage", "context", "thread", "bg", "btw", "plan", "branch", "goal",
    "subgoal", "approve", "deny", "learn", "init", "diff", "rollback", "reload-skills",
    "personality", "refine", "review", "profile",
]


# Lobby = teks saja. Hermes menambah toolset baru dari waktu ke waktu dan mengaktifkannya otomatis,
# jadi daftar ini HARUS menutup semua toolset yang ada; tests/test_hermes_config.py (uji integrasi,
# jalan di CI terhadap Hermes yang dipin dan main) gagal bila ada toolset upstream yang belum tercakup.
LOBBY_DISABLED_TOOLSETS = [
    "terminal", "file", "code_execution", "browser", "web", "search", "x_search", "vision", "video",
    "image_gen", "video_gen", "tts", "computer_use", "skills", "memory", "session_search", "todo",
    "cronjob", "delegation", "connections", "kanban", "discord", "discord_admin",
    "project", "bot_room", "desktop_ui", "onboarding", "catalog", "clarify", "context_engine",
    "feishu_doc", "feishu_drive", "debugging", "safe", "coding",
]


# Toolset Discord bisa membaca channel lain yang terlihat bot — selalu matikan untuk pekerja.
WORKER_DISABLED_TOOLSETS = ["discord", "discord_admin"]


# Browser lokal & computer use berjalan di HOST (bukan di sandbox Docker) dan berat untuk 20 orang;
# mati kecuali team.yaml `allow_browser: true`.
HOST_SIDE_TOOLSETS = ["browser", "computer_use"]


# Tugas auxiliary yang dialihkan ke model "murah" supaya beban & throttle di model utama turun.
# Setiap nama harus ada di DEFAULT_CONFIG["auxiliary"] Hermes (dijaga tests/test_hermes_config.py).
# `vision` sengaja tidak dialihkan: model murah belum tentu mendukung gambar.
AUX_TASKS = ["compression", "title_generation", "approval", "mcp", "memory_query_rewrite",
             "background_review", "goal_judge", "curator", "profile_describer"]


DM_DECLINE_MESSAGE = ("Hermes tidak melayani DM. Pakai channel pribadimu "
                      "(#hermes-<nama>) atau #hermes-shared.")


SECRET_LIKE_KEYS = {"api_key", "apikey", "key", "token", "secret", "password", "access_token", "auth_token",
                    "client_secret", "authorization"}


# Key opsional di llm.models.<alias>: (ambang kompresi, ambang pangkas output tool) khusus model itu.
MODEL_COMPRESSION_KEYS = ("compression_threshold_tokens", "proactive_prune_tokens")


# Batas wajar untuk sandbox satu profile (pengecualian per anggota di members.yaml / set-sandbox).
SANDBOX_MEMORY_MB_RANGE = (256, 65536)


SANDBOX_CPU_RANGE = (0.25, 64)


SANDBOX_DEFAULT_MEMORY_MB = 1024


# Key yang DULU dipin di /etc/hermes/config.yaml dan sekarang ditulis per profile (lihat managed_config).
PROFILE_SCOPED_KEYS = ("terminal.container_cpu", "terminal.container_memory",
                       "compression.threshold_tokens", "compression.proactive_prune_tokens")


REPORT_KINDS = ("daily", "weekly", "monthly")


# Perintah yang membuat agent melanjutkan sendiri tanpa pesan baru → di luar pembatas pesan.
GOAL_COMMANDS = ("goal", "subgoal")


# Toolset penjadwalan: cron job berjalan tanpa pesan, jadi tidak lewat rate limit maupun antrean.
CRON_TOOLSET = "cronjob"


# ID contoh dari team.example.yaml / members.example.yaml: satu digit, deretan nol, lalu 1–2 digit
# (100000000000000001, 200000000000000002, 400000000000000009, …). Snowflake Discord asli tidak berbentuk begitu.
PLACEHOLDER_ID_RE = re.compile(r"^[1-9]0{10,}[0-9]{1,2}\Z")


ALLOW_PLACEHOLDERS_ENV = "HERMES_TEAM_ALLOW_PLACEHOLDERS"     # khusus uji otomatis (ID tiruan)

