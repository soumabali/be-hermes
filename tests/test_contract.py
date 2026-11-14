"""Uji kontrak antar komponen: kit (hermes_team) → roster.json → plugin team-guard, buku besar pemakaian
(usage_ledger) → anggaran plugin, dan tool admin (teamctl) → budget_overrides.json → plugin.

Tiap komponen diuji sendiri di berkas lain dengan data tiruan; berkas ini memastikan keluaran ASLI satu
komponen benar-benar terbaca komponen berikutnya, supaya nama key tidak bergeser diam-diam."""
import json
import sqlite3
import time

import pytest

import plugin_stubs as S
import kit_config
import kit_contracts as kc
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


# Bawaan plugin untuk roster LAMA (tanpa key itu) sengaja berbeda dari bawaan kit: roster lama harus berperilaku
# seperti dulu (fitur baru mati). Selain daftar ini, bawaan keduanya harus sama.
INTENTIONAL_PLUGIN_DEFAULTS = {"user_max_concurrent", "adaptive_throttle", "cron_min_interval_minutes",
                               "welcome_message", "welcome_back_message"}


def test_every_plugin_flagged_field_reaches_the_plugin_and_defaults_stay_in_sync(tc):
    """Tabel kit_config.FIELDS (baris ber-plugin_key) = daftar kunci yang dibaca plugin: tak ada kunci yang
    hilang di salah satu sisi, dan bawaan ganda di plugin tidak menyimpang diam-diam."""
    _team, roster = kit_roster(tc)
    kit_settings = {f.plugin_key: f for f in kit_config.plugin_fields("settings")}
    kit_onboarding = {f.plugin_key: f for f in kit_config.plugin_fields("onboarding")}
    assert set(kit_settings) <= set(roster.settings) and set(kit_onboarding) <= set(roster.onboarding)
    problems = kit_config.plugin_default_problems(roster_mod.SETTINGS_DEFAULTS, roster_mod.ONBOARDING_DEFAULTS)
    assert not problems, "\n".join(problems)
    for plugin_defaults, fields in ((roster_mod.SETTINGS_DEFAULTS, kit_settings),
                                    (roster_mod.ONBOARDING_DEFAULTS, kit_onboarding)):
        drift = {key for key, f in fields.items() if plugin_defaults[key] != f.default} - INTENTIONAL_PLUGIN_DEFAULTS
        assert not drift, f"bawaan plugin menyimpang dari kit_config.FIELDS: {sorted(drift)}"


def test_plugin_default_check_still_detects_missing_and_stray_keys():
    """Bukti uji sinkron di atas tidak tumpul: kunci hilang / berlebih di plugin tetap dilaporkan."""
    settings, onboarding = dict(roster_mod.SETTINGS_DEFAULTS), dict(roster_mod.ONBOARDING_DEFAULTS)
    assert not kit_config.plugin_default_problems(settings, onboarding)
    missing = next(iter(settings))
    del settings[missing]
    settings["hantu"] = 1
    problems = kit_config.plugin_default_problems(settings, onboarding)
    assert any(missing in p and "tambahkan" in p for p in problems)
    assert any("hantu" in p for p in problems)


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


def real_metrics(tmp_path):
    """metrics.json ASLI dari kelas plugin (simulasi kecil dengan jam tiruan, tests/loadtest/sim.py)."""
    from loadtest import sim
    shared = S.sub(P, "shared")
    shared.reset()
    try:
        workdir = tmp_path / "sim"
        workdir.mkdir(exist_ok=True)
        out = sim.run_simulation(S.Env(workdir, P), sim.Config(users=3, duration=90.0, degraded=(1e9, 1e9, 2),
                                                                cron_every=1e9, flood_every=1e9))
    finally:
        shared.reset()
    return out.metrics


def test_metrics_shape_matches_the_typed_contract(tmp_path):
    """Kunci yang dijanjikan kit_contracts.Metrics = kunci yang BENAR-BENAR ditulis Monitor plugin (bukan grep sumber)."""
    metrics = real_metrics(tmp_path)
    assert set(metrics) == kc.keys_of(kc.Metrics)
    assert metrics["schema"] == kc.METRICS_SCHEMA == 1
    assert set(metrics["budget"]) <= kc.keys_of(kc.MetricsBudget)
    assert set(metrics["refused"]) == {"rate", "queue_full", "budget", "user_concurrent"}


def test_admin_tool_reads_the_metrics_shape_the_plugin_writes(tc, tmp_path):
    """Monitor plugin menulis metrics.json; `team_status` harus bisa menampilkannya."""
    assert hasattr(S.sub(P, "scale"), "Monitor")
    state = tc.team_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    metrics = dict(real_metrics(tmp_path), ts=time.time(), active_sessions=5, http_429_last_minute=6)
    (state / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    out = tc.ctl().call("team_status")
    assert "Beban sekarang" in out and "5" in out and "429" in out
    (state / "metrics.json").write_text(json.dumps({"ts": time.time() - 3600}), encoding="utf-8")
    assert "DATA BASI" in tc.ctl().call("team_status")            # basi → diberi label jelas, tidak galat


def test_doctor_reads_the_capabilities_file_the_plugin_really_writes(tc):
    """Keluaran ASLI `HermesGateway.write_capabilities` → `kit_doctor.check_capabilities` (dulu doctor mengira
    `generated_at` angka padahal plugin menulis teks ISO)."""
    import kit_doctor
    upstream = S.sub(P, "upstream")
    gateway = upstream.HermesGateway()
    team = ht.load_team(tc.team_file, tc.members_file)
    state = team["paths"]["state_dir"]
    written = gateway.write_capabilities(state)
    assert written is not None and written.exists(), "plugin tidak menulis capabilities.json"
    doc = json.loads(written.read_text(encoding="utf-8"))
    assert doc["schema"] == 1 and isinstance(doc["generated_at"], str) and isinstance(doc["capabilities"], dict)
    rep = kit_doctor.Report()
    kit_doctor.check_capabilities(team, rep)                      # tidak boleh melempar galat
    assert rep.errors == 0
    unavailable = sorted(k for k, v in doc["capabilities"].items() if v["available"] is False)
    reported = sorted(i["message"].split(": ", 1)[1].split(" (")[0] for i in rep.items if "tidak aktif" in i["message"])
    assert reported == unavailable
