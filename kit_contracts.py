"""kit_contracts.py — bentuk berkas/baris yang dipertukarkan antar komponen (kit, plugin team-guard, teamctl, tools).

Hanya stdlib. Ini DOKUMENTASI YANG BISA DIUJI: `TypedDict` di bawah dipakai tests/test_contract.py untuk memeriksa
keluaran ASLI produsen terhadap kunci yang dijanjikan, dan docs/KONTRAK.md merinci produsen, pembaca, serta aturan
versinya. Aturan versi untuk semua kontrak berkas:

  * pembaca MENGABAIKAN kunci yang tidak dikenal;
  * pembaca yang melihat `schema` LEBIH BESAR dari yang didukungnya mencatat satu kali di log lalu tetap berjalan
    sebisanya (best-effort), bukan menolak;
  * produsen hanya menaikkan `schema` untuk perubahan yang merusak (kunci dihapus / arti berubah); menambah kunci
    tidak menaikkan schema.
"""
from __future__ import annotations

from typing import Any, Optional, TypedDict, Union

ROSTER_SCHEMA = 1            # state/roster.json (kit → plugin, teamctl, tools); "version": 3 lama tetap ada
METRICS_SCHEMA = 1           # state/metrics.json (plugin → teamctl, tools/metrics_export)
BUDGET_OVERRIDES_SCHEMA = 1  # state/budget_overrides.json: dipetakan per user_id, tanpa kunci "schema" (lihat KONTRAK.md)
REMOVED_CHANNELS_SCHEMA = 1  # state/removed_channels.json (kit remove-member → teamctl Server.personal)
EVENTS_SCHEMA = 1            # baris JSON `--events` (kit → plugin onboarding); tanpa kunci "schema" di baris


# --------------------------------------------------------------------------- roster.json
class RosterChannel(TypedDict, total=False):
    profile: str
    kind: str          # personal | shared | admin
    owner: str         # hanya kind personal
    name: str          # hanya kind personal


class RosterUser(TypedDict):
    name: str
    profile: str
    channel_id: str
    display_name: str
    model: str


class RosterInactiveUser(TypedDict):
    name: str
    channel_id: str


class RosterGateway(TypedDict):
    profile_routes: list
    allowed_channels: list
    free_response_channels: list


class RosterKit(TypedDict):
    python: str
    script: str
    team_file: str
    members_file: str


class RosterUsage(TypedDict):
    timezone: str
    ledger: str


class Roster(TypedDict):
    version: int                       # lama (3); jangan dipakai untuk keputusan baru
    schema: int                        # ROSTER_SCHEMA
    kit_version: str
    generated: str
    team_name: str
    guild_id: str
    hermes_root: str
    hermes_bin: str
    admins: list
    roles: dict                        # member, admin, entry
    shared_channel: str
    admin_channel: str
    category_id: str
    channels: dict                     # channel_id → RosterChannel
    users: dict                        # user_id → RosterUser
    inactive_users: dict               # user_id → RosterInactiveUser
    reserved_env: list
    gateway: RosterGateway
    live_reload: bool
    restart_fallback: str
    onboarding: dict                   # kunci = baris plugin_section="onboarding" di kit_config.FIELDS
    kit: RosterKit
    settings: dict                     # kunci = baris plugin_section="settings" di kit_config.FIELDS
    usage: RosterUsage


# --------------------------------------------------------------------------- metrics.json
class MetricsBudget(TypedDict, total=False):
    day: str
    used_by_user: dict
    team_used: int
    stale: bool
    last_collect: Optional[float]


class Metrics(TypedDict, total=False):
    schema: int                        # METRICS_SCHEMA
    ts: float
    active_sessions: int
    active_by_user: dict
    llm_inflight: int
    llm_inflight_peak_5m: int
    queue_len: int
    queue_wait_p95_s: float
    refused: dict                      # rate, queue_full, budget, user_concurrent
    http_429_last_minute: int
    effective_max_active: int
    configured_max_active: int
    throttled: bool
    budget: MetricsBudget


# --------------------------------------------------------------------------- budget_overrides.json
class BudgetOverride(TypedDict):
    day: str                           # YYYY-MM-DD di usage.timezone; hanya berlaku pada hari itu
    extra_tokens: int                  # > 0; menaikkan batas ORANG itu saja, bukan plafon tim


BudgetOverrides = dict   # user_id → BudgetOverride


# --------------------------------------------------------------------------- removed_channels.json
class RemovedChannel(TypedDict):
    name: str                          # nama channel bila diketahui, selain itu ""
    member: str                        # nama anggota yang dihapus
    removed_at: str                    # ISO UTC "…Z"


class RemovedChannels(TypedDict):
    schema: int                        # REMOVED_CHANNELS_SCHEMA
    channels: dict                     # channel_id → RemovedChannel


# --------------------------------------------------------------------------- --events (JSON lines)
class ChannelEvent(TypedDict, total=False):
    event: str                         # "channel"
    channel_id: str
    created: bool
    reused: bool
    user_id: str                       # hanya di onboard-batch


class ResultEvent(TypedDict):
    event: str                         # "result"
    status: str
    user_id: str
    name: Optional[str]
    display_name: Optional[str]
    profile: Optional[str]
    channel_id: Optional[str]
    model: str
    notes: list
    restart_needed: bool
    live_changed: list
    failures: list


class ErrorEvent(TypedDict, total=False):
    event: str                         # "error"
    message: str
    log: list                          # hanya dari subcommand membership (15 baris log terakhir)


class SummaryEvent(TypedDict):
    event: str                         # "summary" (hanya onboard-batch)
    total: int
    counts: dict
    failed: list
    restart_needed: bool
    live_changed: list
    failures: list


EventLine = Union[ChannelEvent, ResultEvent, ErrorEvent, SummaryEvent]


def keys_of(typed: Any) -> frozenset[str]:
    """Kunci yang dijanjikan sebuah TypedDict (untuk uji kontrak)."""
    return frozenset(typed.__annotations__)


def required_keys_of(typed: Any) -> frozenset[str]:
    return frozenset(getattr(typed, "__required_keys__", typed.__annotations__))
