"""Uji kontrak antar komponen: kit (hermes_team) → roster.json → plugin team-guard, buku besar pemakaian
(usage_ledger) → anggaran plugin, dan tool admin (teamctl) → budget_overrides.json → plugin.

Tiap komponen diuji sendiri di berkas lain dengan data tiruan; berkas ini memastikan keluaran ASLI satu
komponen benar-benar terbaca komponen berikutnya, supaya nama key tidak bergeser diam-diam."""
import json
import sqlite3
import time

import pytest

import plugin_stubs as S
import usage_ledger as ul
from teamctl_helpers import BUDI, BE1, Env, ht

P = S.load_plugin("_contract")
roster_mod, budget_mod = S.sub(P, "roster"), S.sub(P, "budget")

NEW_SETTINGS = {"user_max_concurrent": int, "max_llm_parallel": int, "daily_token_budget": int,
                "team_daily_token_budget": int, "adaptive_throttle": bool, "cron_min_interval_minutes": int}
OLD_SETTINGS = ("rate_per_minute", "burst", "max_active", "queue_max_wait", "scrub_chat", "block_env_dump")


@pytest.fixture
def tc(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    yield env
    env.close()


def kit_roster(tc):
    team = ht.load_team(tc.team_file, tc.members_file)
    return team, roster_mod.Roster(json.loads(json.dumps(ht.roster_data(team, tc.root, str(tc.bin)))))


def test_plugin_reads_every_scale_setting_the_kit_writes(tc):
    tc.patch_team(scale={"user_max_concurrent": 3, "max_llm_parallel": 12, "daily_token_budget": 5000,
                         "team_daily_token_budget": 90000, "adaptive_throttle": False, "cron_min_interval_minutes": 30})
    _team, roster = kit_roster(tc)
    for key, kind in NEW_SETTINGS.items():
        assert isinstance(roster.settings.get(key), kind), key
    assert roster.settings["user_max_concurrent"] == 3 and roster.settings["max_llm_parallel"] == 12
    assert roster.settings["adaptive_throttle"] is False and roster.settings["cron_min_interval_minutes"] == 30
    for key in OLD_SETTINGS:
        assert key in roster.settings, key


def test_plugin_source_only_reads_setting_keys_the_kit_produces(tc):
    """Tiap `settings.get("x")` di plugin harus ada di roster buatan kit (salah ketik = fitur mati diam-diam)."""
    import re
    _team, roster = kit_roster(tc)
    used = set()
    for path in S.PLUGIN_DIR.glob("*.py"):
        used |= set(re.findall(r'settings\.get\("([a-z_]+)"', path.read_text(encoding="utf-8")))
    assert used and used <= set(roster.settings), sorted(used - set(roster.settings))


def test_usage_section_points_plugin_at_the_real_ledger(tc):
    tc.patch_team(usage={"timezone": "Asia/Makassar"})
    team, roster = kit_roster(tc)
    cfg = budget_mod.usage_config(roster)
    assert cfg["timezone"] == "Asia/Makassar"
    assert cfg["ledger"] == str(tc.team_dir / "state" / "usage.db") == str(ul.default_ledger_path())


def _spend(tc, profile, sid, user_id, tokens):
    home = tc.root / "profiles" / profile
    home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE IF NOT EXISTS session_model_usage (session_id TEXT, model TEXT, api_call_count INTEGER,"
                " input_tokens INTEGER, output_tokens INTEGER, last_seen REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT)")
    con.execute("INSERT INTO sessions VALUES (?,?,?)", (sid, "discord", user_id))
    con.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?)", (sid, "td-simple", 4, tokens, 0, time.time()))
    con.commit()
    con.close()


def test_ledger_to_budget_to_admin_override_end_to_end(tc):
    """Pemakaian nyata di state.db → usage_ledger.collect → plugin menolak yang melewati anggaran →
    admin menambah jatah lewat teamctl → plugin melayani lagi. Orang lain tidak terpengaruh."""
    tc.patch_team(scale={"daily_token_budget": 1000}, usage={"timezone": "UTC"})
    team, roster = kit_roster(tc)
    _spend(tc, "team-shared", "s-budi", BUDI, 1500)          # di channel bersama: harus tetap milik Budi
    _spend(tc, "be-be1", "s-be1", BE1, 200)
    owners, _people = ul.load_members(tc.members_file)
    ul.collect(tc.team_dir / "state" / "usage.db", tc.root, owners=owners, tz=ul.resolve_tz("UTC"))

    def verdict(uid):
        budget = budget_mod.Budget(tc.team_dir / "state", spawn=lambda *_a, **_k: None)   # tanpa cache antar panggilan
        return budget.check(roster, uid)

    over, fine = verdict(BUDI), verdict(BE1)
    assert over.refused and over.used == 1500 and over.limit == 1000
    assert not fine.refused and fine.used == 200

    ctl = tc.ctl()
    ctl.call("set_usage_budget_extra", user="budi", extra_tokens=2000)
    assert ctl.previews and BUDI in ctl.previews[0]                                  # lewat konfirmasi, memuat ID
    stored = json.loads((tc.team_dir / "state" / "budget_overrides.json").read_text(encoding="utf-8"))
    assert stored[BUDI]["extra_tokens"] == 2000 and stored[BUDI]["day"] == budget_mod.Budget(tc.team_dir / "state").today(roster)
    again = verdict(BUDI)
    assert not again.refused and again.limit == 3000

    tc.ctl().call("clear_usage_budget_extra", user="budi")
    assert verdict(BUDI).refused


def test_admin_extra_matches_plugin_semantics_team_ceiling_is_untouched(tc):
    """Kontrak teks ↔ perilaku: ringkasan teamctl berkata anggaran tim tetap, dan plugin memang tidak
    menjumlahkan tambahan per orang ke plafon tim."""
    tc.patch_team(scale={"daily_token_budget": 1000, "team_daily_token_budget": 3000}, usage={"timezone": "UTC"})
    team, roster = kit_roster(tc)
    _spend(tc, "team-shared", "s-budi", BUDI, 2500)
    _spend(tc, "be-be1", "s-be1", BE1, 900)                                  # tim: 3.400 > 3.000
    owners, _people = ul.load_members(tc.members_file)
    ul.collect(tc.team_dir / "state" / "usage.db", tc.root, owners=owners, tz=ul.resolve_tz("UTC"))
    ctl = tc.ctl()
    ctl.call("set_usage_budget_extra", user="budi", extra_tokens=1_000_000)
    assert "anggaran harian tim tetap 3.000" in ctl.previews[0]
    for uid in (BUDI, BE1):
        verdict = budget_mod.Budget(tc.team_dir / "state", spawn=lambda *_a, **_k: None).check(roster, uid)
        assert verdict.team_limit == 3000 and verdict.refused, uid
    assert budget_mod.Budget(tc.team_dir / "state", spawn=lambda *_a, **_k: None).check(roster, BUDI).limit == 1_001_000


def test_admin_tool_reads_the_metrics_shape_the_plugin_writes(tc, tmp_path):
    """Monitor plugin menulis metrics.json; `team_status` harus bisa menampilkannya."""
    scale = S.sub(P, "scale")
    keys = set()
    src = (S.PLUGIN_DIR / "scale.py").read_text(encoding="utf-8")
    for key in ("active_sessions", "llm_inflight", "queue_len", "queue_wait_p95_s", "http_429_last_minute",
                "effective_max_active", "configured_max_active", "throttled", "refused", "budget"):
        assert f'"{key}"' in src, f"plugin tidak lagi menulis {key}"
        keys.add(key)
    assert hasattr(scale, "Monitor")
    state = tc.team_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "metrics.json").write_text(json.dumps({
        "ts": time.time(), "active_sessions": 5, "active_by_user": {BUDI: 2}, "llm_inflight": 7,
        "llm_inflight_peak_5m": 9, "queue_len": 3, "queue_wait_p95_s": 12.5,
        "refused": {"rate": 1, "queue_full": 0, "budget": 2, "user_concurrent": 4}, "http_429_last_minute": 6,
        "effective_max_active": 4, "configured_max_active": 8, "throttled": True,
        "budget": {"day": "2026-11-12", "used_by_user": {BUDI: 900}, "team_used": 900}}), encoding="utf-8")
    out = tc.ctl().call("team_status")
    assert "Beban sekarang" in out and "5" in out and "429" in out
    (state / "metrics.json").write_text(json.dumps({"ts": time.time() - 3600}), encoding="utf-8")
    assert "DATA BASI" in tc.ctl().call("team_status")            # basi → diberi label jelas, tidak galat
