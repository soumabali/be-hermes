"""Paket server MCP admin tim (teamctl_mcp.py adalah pintu masuknya).

Modul: protocol (JSON-RPC/elicitation/pembatalan), confirm (Plan, _guarded, audit, pembersih teks),
schema (validasi argumen), registry (@tool), tools_team / tools_usage / tools_discord (isi tool),
discord_guard (Server + pagar Discord). Dipasang sebagai folder di samping teamctl_mcp.py; tanpa instalasi.
"""
import sys
from pathlib import Path

# Modul kit (hermes_team, kit_base, usage_ledger) ada di folder induk paket ini.
_KIT_DIR = str(Path(__file__).resolve().parent.parent)
if _KIT_DIR not in sys.path:
    sys.path.insert(0, _KIT_DIR)
