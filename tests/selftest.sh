#!/usr/bin/env bash
# Uji kit terhadap Hermes yang terpasang di server ini. Aman dijalankan kapan saja:
# memakai HOME sementara dan Discord tiruan, tidak menyentuh ~/.hermes maupun server Discord.
# Jalankan setelah `hermes update` dan sebelum memasang kit versi baru.
#   bash /opt/hermes-team/tests/selftest.sh [argumen pytest]
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Python milik Hermes (supaya uji integrasi bisa `import gateway`); kalau tidak ketemu pakai python3.
PY="${HERMES_PYTHON:-}"
if [ -z "$PY" ]; then
  HB="$(command -v hermes || true)"
  if [ -n "$HB" ]; then
    SHEBANG="$(head -n1 "$(readlink -f "$HB")" | sed 's/^#!//' | awk '{print $1}')"
    [ -x "$SHEBANG" ] && "$SHEBANG" -c 'import gateway' 2>/dev/null && PY="$SHEBANG"
  fi
fi
PY="${PY:-python3}"

"$PY" -c 'import pytest, yaml' 2>/dev/null || {
  echo "pytest/PyYAML belum ada di $PY — pasang: $PY -m pip install -r $KIT/requirements-dev.txt" >&2; exit 2; }
"$PY" -c 'import gateway' 2>/dev/null \
  && echo "Hermes terdeteksi di $PY: uji integrasi ikut dijalankan." \
  || echo "PERINGATAN: Hermes tidak bisa di-import dari $PY — uji integrasi (live reload, loader plugin) DILEWATI." >&2

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cd "$KIT"
HOME="$TMP/home" HERMES_HOME="$TMP/home/.hermes" HERMES_TEAM_DIR="$TMP/team" \
  exec "$PY" -m pytest tests -q -rs -p no:cacheprovider "$@"
