#!/usr/bin/env bash
# Uji kit terhadap Hermes yang terpasang di server ini. Aman dijalankan kapan saja:
# memakai HOME sementara dan Discord tiruan, tidak menyentuh ~/.hermes maupun server Discord.
# Jalankan setelah `hermes update` dan sebelum memasang kit versi baru.
#
#   bash /opt/hermes-team/tests/selftest.sh [--allow-skip] [--detect] [argumen pytest]
#
# Uji INTEGRASI (resolver rute, loader plugin, live reload) butuh Python milik Hermes DAN direktori
# sumber Hermes di PYTHONPATH. Keduanya dicari otomatis:
#   HERMES_PYTHON   interpreter Hermes (menang atas deteksi)
#   HERMES_SRC      direktori sumber Hermes, yang berisi gateway/ (menang atas deteksi)
#   HERMES_BIN      launcher `hermes` (default: yang ada di PATH)
# Tanpa itu launcher `hermes` dibaca: symlink diikuti, lalu shebang `#!<python> -I` atau pembungkus
# `/bin/sh` berisi `exec <python> -I -c '…'` (dua bentuk yang dibuat installer Hermes). Direktori sumber
# diambil dari isi launcher (`sys.path.insert(0, '<dir>')`), dari `import hermes_cli`, atau dari lokasi
# pasang bawaan installer: ${HERMES_HOME:-~/.hermes}/hermes-agent.
#
# pytest/PyYAML/discord.py TIDAK dipasang ke Python sistem atau venv Hermes (PEP 668): bila belum ada,
# dibuat venv sekali pakai (--system-site-packages) di folder sementara dan dihapus setelah selesai.
#
# Kode keluar: 0 lulus (uji integrasi ikut jalan, atau dilewati dengan --allow-skip)
#              1 ada uji yang gagal
#              2 prasyarat tidak terpenuhi (python/venv/pip)
#              3 uji unit lulus tetapi uji integrasi DILEWATI (tanpa --allow-skip)
#   --detect   hanya cetak hasil deteksi (python, sumber, alasan), tidak menjalankan uji
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ALLOW_SKIP=0
DETECT=0
PYTEST_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --allow-skip) ALLOW_SKIP=1 ;;
    --detect) DETECT=1 ;;
    *) PYTEST_ARGS+=("$arg") ;;
  esac
done

say() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

# Ikuti symlink sampai berkas aslinya (readlink -f tidak ada di semua sistem).
resolve_link() {
  local target="$1" next dir hops=0
  while [ -L "$target" ] && [ "$hops" -lt 40 ]; do
    next="$(readlink "$target")"
    dir="$(cd "$(dirname "$target")" && pwd -P)"
    case "$next" in
      /*) target="$next" ;;
      *) target="$dir/$next" ;;
    esac
    hops=$((hops + 1))
  done
  printf '%s\n' "$target"
}

# Kata pertama sebuah baris perintah; menghormati kutip tunggal/ganda di awal (path berisi spasi).
first_word() {
  local text="$1"
  text="${text#"${text%%[![:space:]]*}"}"
  case "$text" in
    \'*) text="${text#\'}"; printf '%s\n' "${text%%\'*}" ;;
    \"*) text="${text#\"}"; printf '%s\n' "${text%%\"*}" ;;
    *) printf '%s\n' "${text%%[[:space:]]*}" ;;
  esac
}

# Interpreter Python dari launcher hermes: shebang `#!<python> [-I]`, `#!/usr/bin/env python3`,
# atau pembungkus sh berisi baris `exec <python> …`. Kosong bila tidak dikenali.
python_from_launcher() {
  local file="$1" head line interp rest base
  # `|| true`: dengan pipefail, pembaca yang selesai lebih dulu (SIGPIPE) tidak boleh menggagalkan deteksi.
  head="$( { LC_ALL=C head -c 4096 "$file" | LC_ALL=C tr -d '\000\r'; } 2>/dev/null || true)"
  line="${head%%$'\n'*}"
  case "$line" in
    '#!'*) ;;
    *) return 0 ;;
  esac
  line="${line#\#!}"
  interp="$(first_word "$line")"
  base="$(basename "$interp")"
  case "$base" in
    env)
      rest="${line#*env}"
      interp="$(first_word "$rest")"
      case "$interp" in
        python*) command -v "$interp" 2>/dev/null || true ;;
      esac
      ;;
    python*|pypy*) printf '%s\n' "$interp" ;;
    sh|bash|dash|ash)
      line="$(printf '%s\n' "$head" | sed -n 's/^[[:space:]]*exec[[:space:]][[:space:]]*//p' || true)"
      line="${line%%$'\n'*}"
      [ -n "$line" ] || return 0
      interp="$(first_word "$line")"
      case "$(basename "$interp")" in
        python*|pypy*) printf '%s\n' "$interp" ;;
      esac
      ;;
  esac
}

# Direktori sumber dari isi launcher: `sys.path.insert(0, '<dir>')` (di pembungkus sh kutipnya berbentuk '"'"').
src_from_launcher() {
  local file="$1"
  local found
  found="$( { LC_ALL=C head -c 65536 "$file" | LC_ALL=C tr -d '\000' \
    | sed -n "s/.*sys\.path\.insert(0, *[^\/]*\(\/[^'\"]*\).*/\1/p"; } 2>/dev/null || true)"
  printf '%s\n' "${found%%$'\n'*}"
}

has_gateway() { [ -n "$1" ] && { [ -d "$1/gateway" ] || [ -f "$1/gateway.py" ]; }; }

PY=""          # interpreter Hermes
SRC=""         # direktori sumber Hermes
WHY=""         # alasan uji integrasi dilewati (kosong = tidak dilewati)
LAUNCHER=""

if [ -n "${HERMES_PYTHON:-}" ]; then
  if [ -x "$HERMES_PYTHON" ]; then
    PY="$HERMES_PYTHON"
  else
    warn "HERMES_PYTHON=$HERMES_PYTHON bukan berkas yang bisa dijalankan."
    exit 2
  fi
fi
HB="${HERMES_BIN:-}"
[ -n "$HB" ] || HB="$(command -v hermes 2>/dev/null || true)"
if [ -n "$HB" ] && [ -e "$HB" ]; then
  LAUNCHER="$(resolve_link "$HB")"
  if [ -z "$PY" ]; then
    CAND="$(python_from_launcher "$LAUNCHER")"
    if [ -n "$CAND" ] && [ -x "$CAND" ]; then
      PY="$CAND"
    fi
  fi
fi

if [ -n "${HERMES_SRC:-}" ]; then
  SRC="$HERMES_SRC"
else
  if [ -n "$LAUNCHER" ]; then
    CAND="$(src_from_launcher "$LAUNCHER")"
    if has_gateway "$CAND"; then SRC="$CAND"; fi
  fi
  if [ -z "$SRC" ] && [ -n "$PY" ]; then
    CAND="$(cd / && "$PY" -c 'import hermes_cli, os; print(os.path.dirname(os.path.dirname(os.path.abspath(hermes_cli.__file__))))' 2>/dev/null || true)"
    if has_gateway "$CAND"; then SRC="$CAND"; fi
  fi
  if [ -z "$SRC" ]; then
    CAND="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"        # lokasi pasang bawaan scripts/install.sh
    if has_gateway "$CAND"; then SRC="$CAND"; fi
  fi
fi
# Launcher tidak terbaca tetapi sumbernya ketemu: pakai venv di dalamnya (bawaan installer).
if [ -z "$PY" ] && [ -n "$SRC" ]; then
  for CAND in "$SRC/.venv/bin/python" "$SRC/venv/bin/python"; do
    if [ -x "$CAND" ]; then PY="$CAND"; break; fi
  done
fi

HERMES_PATH="$SRC"
if [ -n "${PYTHONPATH:-}" ]; then
  if [ -n "$HERMES_PATH" ]; then HERMES_PATH="$HERMES_PATH:$PYTHONPATH"; else HERMES_PATH="$PYTHONPATH"; fi
fi

if [ -z "$PY" ]; then
  WHY="interpreter Python milik Hermes tidak ditemukan (launcher: ${LAUNCHER:-tidak ada di PATH}); set HERMES_PYTHON=<python venv Hermes>"
elif ! (cd / && PYTHONPATH="$HERMES_PATH" "$PY" -c 'import gateway, toolsets, hermes_cli.plugins' >/dev/null 2>&1); then
  if [ -z "$SRC" ]; then
    WHY="direktori sumber Hermes tidak ditemukan, jadi \`import gateway\` gagal dari $PY; set HERMES_SRC=<direktori berisi gateway/>"
  else
    WHY="\`import gateway, toolsets, hermes_cli.plugins\` gagal dari $PY dengan PYTHONPATH=$HERMES_PATH (coba: PYTHONPATH=$HERMES_PATH $PY -c 'import gateway')"
  fi
fi

BASE="$PY"
if [ -n "$WHY" ] || [ -z "$BASE" ]; then
  BASE="$(command -v python3 2>/dev/null || true)"
  HERMES_PATH="${PYTHONPATH:-}"
fi

if [ "$DETECT" = 1 ]; then
  say "launcher=${LAUNCHER}"
  say "python=${PY}"
  say "source=${SRC}"
  say "pythonpath=${HERMES_PATH}"
  if [ -z "$WHY" ]; then say "integration=run"; else say "integration=skip"; say "reason=${WHY}"; fi
  exit 0
fi

if [ -z "$BASE" ]; then
  warn "python3 tidak ditemukan."
  exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Dependensi uji. Sudah ada → pakai interpreter itu apa adanya. Belum → venv sekali pakai; Python
# sistem / venv Hermes tidak pernah diubah (PEP 668: pip ke Python yang dikelola distro ditolak).
NEED='import pytest, yaml'
[ -n "$WHY" ] || NEED='import pytest, yaml, discord'
RUNPY="$BASE"
if ! (cd / && PYTHONPATH="$HERMES_PATH" "$BASE" -c "$NEED" >/dev/null 2>&1); then
  say "Dependensi uji belum ada di $BASE → membuat venv sekali pakai di $TMP/venv (dihapus setelah selesai)."
  if ! "$BASE" -m venv --system-site-packages "$TMP/venv" >"$TMP/venv.log" 2>&1; then
    cat "$TMP/venv.log" >&2
    warn "Gagal membuat venv dari $BASE. Di Debian/Ubuntu: sudo apt install python3-venv, lalu ulangi."
    exit 2
  fi
  RUNPY="$TMP/venv/bin/python"
  # `venv` dari interpreter yang sudah berada di venv (venv Hermes) mewarisi site-packages Python DASAR,
  # bukan milik venv itu → paket Hermes ditambahkan lewat .pth supaya tetap bisa di-import.
  "$BASE" -c 'import sysconfig; p = sysconfig.get_paths(); print(p["purelib"]); print(p["platlib"])' 2>/dev/null \
    | sort -u > "$TMP/base-site.txt" || true
  SITE="$("$RUNPY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  cp "$TMP/base-site.txt" "$SITE/hermes-base-site.pth"
  if ! "$RUNPY" -m pip install -q --disable-pip-version-check -r "$KIT/requirements-dev.txt" >"$TMP/pip.log" 2>&1; then
    tail -n 20 "$TMP/pip.log" >&2
    warn "Gagal memasang dependensi uji ke venv sementara (butuh akses ke PyPI): $KIT/requirements-dev.txt"
    exit 2
  fi
fi

if [ -z "$WHY" ]; then
  say "Hermes terdeteksi: python $PY, sumber ${SRC:-(dari site-packages)} — uji integrasi ikut dijalankan."
else
  warn "PERINGATAN: uji integrasi akan DILEWATI — $WHY"
fi

[ "${#PYTEST_ARGS[@]}" -gt 0 ] || PYTEST_ARGS=(tests -q -rs)
cd "$KIT"
RC=0
if [ -z "$WHY" ]; then
  # KIT_REQUIRE_HERMES=1: conftest menghentikan sesi (kode 4) bila Hermes ternyata tidak bisa di-import,
  # jadi "lulus" di jalur ini selalu berarti uji integrasi benar-benar jalan.
  HOME="$TMP/home" HERMES_HOME="$TMP/home/.hermes" HERMES_TEAM_DIR="$TMP/team" PYTHONPATH="$HERMES_PATH" \
    KIT_REQUIRE_HERMES=1 "$RUNPY" -m pytest -p no:cacheprovider "${PYTEST_ARGS[@]}" || RC=$?
else
  HOME="$TMP/home" HERMES_HOME="$TMP/home/.hermes" HERMES_TEAM_DIR="$TMP/team" PYTHONPATH="$HERMES_PATH" \
    "$RUNPY" -m pytest -p no:cacheprovider "${PYTEST_ARGS[@]}" || RC=$?
fi

say ""
say "================ selftest: ringkasan ================"
if [ "$RC" -ne 0 ]; then
  say "✗ GAGAL: pytest keluar dengan kode $RC (lihat keluaran di atas)."
  if [ -z "$WHY" ]; then say "  Uji integrasi: DIJALANKAN (python $PY, sumber ${SRC:-site-packages})."; else say "  Uji integrasi: DILEWATI — $WHY"; fi
  [ "$RC" -eq 3 ] && RC=1        # 3 dicadangkan untuk "integrasi dilewati"
  exit "$RC"
fi
if [ -z "$WHY" ]; then
  say "✓ Lulus. Uji integrasi: DIJALANKAN terhadap Hermes di ${SRC:-site-packages} (python $PY)."
  exit 0
fi
say "! Uji unit lulus, tetapi uji integrasi DILEWATI: kecocokan kit dengan Hermes di server ini BELUM terbukti."
say "  Alasan: $WHY"
say "  Ulangi dengan: HERMES_PYTHON=<python venv Hermes> HERMES_SRC=<direktori sumber Hermes> bash $KIT/tests/selftest.sh"
if [ "$ALLOW_SKIP" = 1 ]; then
  say "  (--allow-skip: kode keluar 0)"
  exit 0
fi
exit 3
