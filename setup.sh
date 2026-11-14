#!/usr/bin/env bash
# Wizard pemasangan kit tim Hermes:  sudo bash setup.sh [opsi]   (opsi: lihat --help)
# Berkas ini hanya bootstrap: pastikan root + python3 + PyYAML, lalu serahkan ke kit_setup.py.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Wizard ini harus dijalankan sebagai root. Jalankan:" >&2
  echo "  sudo bash setup.sh" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import yaml' >/dev/null 2>&1; then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "python3 / PyYAML belum ada dan apt-get tidak ditemukan. Pasang python3 dan PyYAML dulu, lalu ulangi." >&2
    exit 1
  fi
  echo "Memasang python3 dan python3-yaml (apt-get) ..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3 python3-yaml
fi

# Dijalankan sebagai root dari folder yang bisa saja dimiliki user lain: jangan menulis bytecode ke sana dan jangan
# biarkan lingkungan memuat kode tambahan ke interpreter ini.
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH PYTHONSTARTUP PYTHONHOME PYTHONINSPECT

cd "$HERE"
exec python3 "$HERE/kit_setup.py" "$@"
