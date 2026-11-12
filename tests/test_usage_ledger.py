import json
import os
import sqlite3
import stat
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import usage_ledger as ul  # noqa: E402

UTC = timezone.utc
ANDI, BUDI = "500000000000000001", "500000000000000002"
OWNERS = {"be-andi": ANDI, "be-budi": BUDI}
PEOPLE = {ANDI: {"name": "andi", "display_name": "Andi"}, BUDI: {"name": "budi", "display_name": "Budi"}}


def ts(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=UTC).timestamp()


USAGE_DDL = """CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '', billing_mode TEXT NOT NULL DEFAULT '', task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0, actual_cost_usd REAL NOT NULL DEFAULT 0,
    first_seen REAL, last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task))"""
SESSIONS_DDL = """CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT,
    parent_session_id TEXT, started_at REAL NOT NULL, title TEXT)"""


class Profile:
    def __init__(self, root: Path, name: str, sessions_table=True):
        self.home = root / "profiles" / name
        self.home.mkdir(parents=True)
        self.db = self.home / "state.db"
        con = sqlite3.connect(self.db)
        con.execute(USAGE_DDL)
        if sessions_table:
            con.execute(SESSIONS_DDL)
        con.commit()
        con.close()

    def session(self, sid, user_id=None, source="discord", parent=None):
        con = sqlite3.connect(self.db)
        con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?)", (sid, source, user_id, parent, 0, "judul RAHASIA"))
        con.commit()
        con.close()

    def usage(self, sid, calls, tin, tout, seen, model="td-simple", cost=0.0, actual=0.0):
        """Set angka KUMULATIF sesi (seperti Hermes)."""
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO session_model_usage (session_id, model, api_call_count, input_tokens, output_tokens,"
                    " estimated_cost_usd, actual_cost_usd, last_seen) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT DO UPDATE SET api_call_count=excluded.api_call_count, input_tokens=excluded.input_tokens,"
                    " output_tokens=excluded.output_tokens, estimated_cost_usd=excluded.estimated_cost_usd,"
                    " actual_cost_usd=excluded.actual_cost_usd, last_seen=excluded.last_seen",
                    (sid, model, calls, tin, tout, cost, actual, seen))
        con.commit()
        con.close()

    def delete_session(self, sid):
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM session_model_usage WHERE session_id=?", (sid,))
        con.commit()
        con.close()


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "hermes"
    root.mkdir()
    return root, tmp_path / "team" / "state" / "usage.db"


def collect(env, now):
    root, ledger = env
    return ul.collect(ledger, root, owners=OWNERS, now=now, tz=UTC)


def q(env, start, end, **kw):
    return ul.query(env[1], start, end, people=PEOPLE, **kw)


def user(rep, key):
    return next(u for u in rep["users"] if u["user_key"] == key)


# ----------------------------------------------------------------------------- potret & selisih
def test_delta_is_recorded_on_the_day_it_is_collected(env):
    p = Profile(env[0], "be-andi")
    p.session("s1", ANDI)
    p.usage("s1", 2, 1000, 100, ts(2026, 11, 10))
    collect(env, ts(2026, 11, 10, 13))
    p.usage("s1", 5, 4000, 400, ts(2026, 11, 11))          # kumulatif naik di hari berikutnya
    collect(env, ts(2026, 11, 11, 13))
    d10 = user(q(env, date(2026, 11, 10), date(2026, 11, 10)), ANDI)["total"]
    d11 = user(q(env, date(2026, 11, 11), date(2026, 11, 11)), ANDI)["total"]
    assert (d10["requests"], d10["input_tokens"], d10["output_tokens"]) == (2, 1000, 100)
    assert (d11["requests"], d11["input_tokens"], d11["output_tokens"]) == (3, 3000, 300)


def test_collect_twice_without_new_usage_adds_nothing(env):
    p = Profile(env[0], "be-andi")
    p.usage("s1", 2, 1000, 100, ts(2026, 11, 10))
    assert collect(env, ts(2026, 11, 10, 13))["rows"] == 1
    again = collect(env, ts(2026, 11, 10, 14))
    assert again["rows"] == 0 and again["requests"] == 0
    assert user(q(env, date(2026, 11, 10), date(2026, 11, 10)), ANDI)["total"]["requests"] == 2


def test_first_collect_backfills_history_by_last_activity_day(env):
    p = Profile(env[0], "be-andi")
    p.usage("lama", 4, 800, 80, ts(2026, 10, 1))
    p.usage("baru", 1, 200, 20, ts(2026, 11, 12))
    collect(env, ts(2026, 11, 12, 13))
    assert user(q(env, date(2026, 10, 1), date(2026, 10, 1)), ANDI)["total"]["requests"] == 4
    assert user(q(env, date(2026, 11, 12), date(2026, 11, 12)), ANDI)["total"]["requests"] == 1


def test_history_survives_session_pruning_in_hermes(env):
    p = Profile(env[0], "be-andi")
    p.usage("s1", 3, 900, 90, ts(2026, 11, 10))
    collect(env, ts(2026, 11, 10, 13))
    p.delete_session("s1")                                  # hermes sessions prune
    collect(env, ts(2026, 11, 20, 13))
    assert user(q(env, date(2026, 11, 1), date(2026, 11, 30)), ANDI)["total"]["requests"] == 3


def test_counter_reset_is_counted_from_zero_not_negative(env):
    p = Profile(env[0], "be-andi")
    p.usage("s1", 10, 5000, 500, ts(2026, 11, 10))
    collect(env, ts(2026, 11, 10, 13))
    p.usage("s1", 2, 600, 60, ts(2026, 11, 11))             # sesi dibuat ulang dengan id sama
    collect(env, ts(2026, 11, 11, 13))
    assert user(q(env, date(2026, 11, 11), date(2026, 11, 11)), ANDI)["total"]["requests"] == 2
    assert user(q(env, date(2026, 11, 1), date(2026, 11, 30)), ANDI)["total"]["input_tokens"] == 5600


def test_actual_cost_wins_over_estimate(env):
    p = Profile(env[0], "be-andi")
    p.usage("s1", 1, 10, 1, ts(2026, 11, 10), cost=0.05, actual=0.08)
    p.usage("s2", 1, 10, 1, ts(2026, 11, 10), cost=0.02)
    collect(env, ts(2026, 11, 10, 13))
    assert user(q(env, date(2026, 11, 10), date(2026, 11, 10)), ANDI)["total"]["cost_usd"] == 0.1


# ----------------------------------------------------------------------------- atribusi per orang
def test_shared_profile_usage_is_split_by_discord_user(env):
    p = Profile(env[0], "team-shared")
    p.session("sa", ANDI)
    p.session("sb", BUDI)
    p.usage("sa", 2, 100, 10, ts(2026, 11, 10))
    p.usage("sb", 7, 700, 70, ts(2026, 11, 10))
    collect(env, ts(2026, 11, 10, 13))
    rep = q(env, date(2026, 11, 10), date(2026, 11, 10))
    assert user(rep, ANDI)["total"]["requests"] == 2 and user(rep, BUDI)["total"]["requests"] == 7


def test_sessions_without_sender_fall_back_to_profile_owner_then_profile(env):
    a = Profile(env[0], "be-andi")
    a.session("cron1", None, source="cron")
    a.usage("cron1", 3, 300, 30, ts(2026, 11, 10))
    s = Profile(env[0], "team-shared")
    s.session("cron2", None, source="cron")
    s.usage("cron2", 1, 50, 5, ts(2026, 11, 10))
    collect(env, ts(2026, 11, 10, 13))
    rep = q(env, date(2026, 11, 10), date(2026, 11, 10))
    assert user(rep, ANDI)["total"]["requests"] == 3
    assert user(rep, "profile:team-shared")["display_name"] == "(team-shared, tanpa pengirim)"


def test_subagent_session_inherits_parent_user(env):
    s = Profile(env[0], "team-shared")
    s.session("induk", BUDI)
    s.session("anak", None, parent="induk")
    s.usage("anak", 4, 400, 40, ts(2026, 11, 10))
    collect(env, ts(2026, 11, 10, 13))
    assert user(q(env, date(2026, 11, 10), date(2026, 11, 10)), BUDI)["total"]["requests"] == 4


def test_works_without_sessions_table_or_with_missing_table(env):
    Profile(env[0], "be-andi", sessions_table=False).usage("s", 1, 10, 1, ts(2026, 11, 10))
    (env[0] / "profiles" / "rusak").mkdir()
    sqlite3.connect(env[0] / "profiles" / "rusak" / "state.db").close()
    summary = collect(env, ts(2026, 11, 10, 13))
    assert summary["profiles"] == 1 and [x["profile"] for x in summary["skipped"]] == ["rusak"]
    assert user(q(env, date(2026, 11, 10), date(2026, 11, 10)), ANDI)["total"]["requests"] == 1


def test_former_member_is_still_reported_by_id(env):
    s = Profile(env[0], "team-shared")
    s.session("x", "599999999999999999")
    s.usage("x", 1, 10, 1, ts(2026, 11, 10))
    collect(env, ts(2026, 11, 10, 13))
    u = q(env, date(2026, 11, 10), date(2026, 11, 10))["users"][0]
    assert "bukan anggota aktif" in u["display_name"]


# ----------------------------------------------------------------------------- periode
@pytest.mark.parametrize("period,offset,start,end", [
    ("day", 0, date(2026, 11, 12), date(2026, 11, 12)),
    ("day", 1, date(2026, 11, 11), date(2026, 11, 11)),
    ("week", 0, date(2026, 11, 9), date(2026, 11, 15)),       # 12 Nov 2026 = Kamis; minggu Senin–Minggu
    ("week", 1, date(2026, 11, 2), date(2026, 11, 8)),
    ("month", 0, date(2026, 11, 1), date(2026, 11, 30)),
    ("month", 1, date(2026, 10, 1), date(2026, 10, 31)),
    ("month", 11, date(2025, 12, 1), date(2025, 12, 31)),
])
def test_period_ranges(period, offset, start, end):
    assert ul.period_range(period, date(2026, 11, 12), offset)[:2] == (start, end)


def test_period_range_rejects_bad_input():
    with pytest.raises(ul.LedgerError):
        ul.period_range("year", date(2026, 1, 1))
    with pytest.raises(ul.LedgerError):
        ul.period_range("day", date(2026, 1, 1), -1)


def test_daily_weekly_monthly_reports_add_up(env):
    p = Profile(env[0], "be-andi")
    for day, calls in ((3, 1), (10, 2), (12, 4)):             # 3 Nov (minggu lalu-lalu), 10 & 12 Nov (minggu ini)
        p.usage(f"s{day}", calls, calls * 100, calls * 10, ts(2026, 11, day))
    collect(env, ts(2026, 11, 12, 13))
    now = ts(2026, 11, 12, 14)

    def total(period, **kw):
        return json.loads(ul.report(env[1], period, fmt="json", people=PEOPLE, now=now, tz=UTC, **kw))["totals"]["requests"]

    assert total("day") == 4 and total("week") == 6 and total("month") == 7
    assert total("month", offset=1) == 0


def test_last_n_days_breaks_down_per_day(env):
    p = Profile(env[0], "be-andi")
    p.usage("a", 1, 100, 10, ts(2026, 11, 11))
    p.usage("b", 2, 200, 20, ts(2026, 11, 12))
    collect(env, ts(2026, 11, 12, 13))
    out = json.loads(ul.report(env[1], "day", last=7, fmt="json", people=PEOPLE, now=ts(2026, 11, 12, 14), tz=UTC))
    assert set(out["users"][0]["days"]) == {"2026-11-11", "2026-11-12"}
    with pytest.raises(ul.LedgerError):
        ul.report(env[1], "week", last=7, now=ts(2026, 11, 12, 14), tz=UTC)


def test_timezone_decides_the_day_boundary(env):
    p = Profile(env[0], "be-andi")
    p.usage("s", 1, 10, 1, ts(2026, 11, 10, 20))
    wita = ul.resolve_tz("Asia/Makassar")                    # UTC+8: 20:00 UTC = 04:00 hari berikutnya
    ul.collect(env[1], env[0], owners=OWNERS, now=ts(2026, 11, 10, 20), tz=wita)
    assert q(env, date(2026, 11, 11), date(2026, 11, 11))["totals"]["requests"] == 1
    with pytest.raises(ul.LedgerError):
        ul.resolve_tz("Bukan/Zona")


# ----------------------------------------------------------------------------- saringan & tampilan
def test_user_filter_by_name_display_or_id(env):
    Profile(env[0], "be-andi").usage("a", 1, 10, 1, ts(2026, 11, 10))
    Profile(env[0], "be-budi").usage("b", 2, 20, 2, ts(2026, 11, 10))
    collect(env, ts(2026, 11, 10, 13))
    for needle in ("budi", "Budi", "@budi", BUDI):
        rep = q(env, date(2026, 11, 10), date(2026, 11, 10), user=needle)
        assert [u["name"] for u in rep["users"]] == ["budi"] and rep["totals"]["requests"] == 2
    assert q(env, date(2026, 11, 10), date(2026, 11, 10), user="tidakada")["users"] == []


def test_render_text_markdown_and_empty(env):
    Profile(env[0], "be-andi").usage("a", 3, 2_500_000, 1500, ts(2026, 11, 10), cost=1.5)
    collect(env, ts(2026, 11, 10, 13))
    now = ts(2026, 11, 10, 14)
    txt = ul.report(env[1], "day", people=PEOPLE, now=now, tz=UTC)
    md = ul.report(env[1], "day", fmt="markdown", people=PEOPLE, now=now, tz=UTC)
    assert "Andi" in txt and "2.50M" in txt and "TOTAL" in txt and "dipotret 60 menit lalu" in txt
    assert md.startswith("**Pemakaian LLM — harian 2026-11-10**") and "|---|" in md
    assert "Tidak ada pemakaian" in ul.report(env[1], "month", offset=3, people=PEOPLE, now=now, tz=UTC)


def test_models_and_session_count_reported(env):
    p = Profile(env[0], "be-andi")
    p.usage("a", 1, 100, 10, ts(2026, 11, 10), model="td-simple")
    p.usage("b", 2, 200, 20, ts(2026, 11, 10), model="td-normal")
    collect(env, ts(2026, 11, 10, 13))
    u = user(q(env, date(2026, 11, 10), date(2026, 11, 10)), ANDI)
    assert u["sessions"] == 2 and u["models"]["td-normal"] == {"requests": 2, "tokens": 220}


# ----------------------------------------------------------------------------- keamanan
def test_ledger_is_private_and_state_db_untouched(env):
    p = Profile(env[0], "be-andi")
    p.session("s1", ANDI)
    p.usage("s1", 1, 10, 1, ts(2026, 11, 10))
    before = p.db.read_bytes()
    collect(env, ts(2026, 11, 10, 13))
    assert p.db.read_bytes() == before                       # state.db hanya dibaca
    assert stat.S_IMODE(env[1].stat().st_mode) == 0o600
    assert stat.S_IMODE(env[1].parent.stat().st_mode) == 0o700


def test_ledger_stores_no_conversation_content(env):
    p = Profile(env[0], "be-andi")
    p.session("s1", ANDI)                                     # judul sesi berisi "RAHASIA"
    p.usage("s1", 1, 10, 1, ts(2026, 11, 10))
    collect(env, ts(2026, 11, 10, 13))
    assert b"RAHASIA" not in env[1].read_bytes()
    con = sqlite3.connect(env[1])
    assert {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
        "daily", "snapshot", "session_days", "meta"}
    con.close()


def test_report_without_ledger_gives_clear_error(env):
    with pytest.raises(ul.LedgerError, match="collect"):
        ul.query(env[1], date(2026, 11, 10), date(2026, 11, 10))


def test_reader_cannot_write(env):
    Profile(env[0], "be-andi").usage("a", 1, 10, 1, ts(2026, 11, 10))
    collect(env, ts(2026, 11, 10, 13))
    con = ul.open_ledger(env[1], readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM daily")
    con.close()


def test_old_rows_are_pruned_after_retention(env):
    p = Profile(env[0], "be-andi")
    p.usage("tua", 1, 10, 1, ts(2023, 1, 1))
    p.usage("baru", 1, 10, 1, ts(2026, 11, 10))
    collect(env, ts(2026, 11, 10, 13))
    assert q(env, date(2023, 1, 1), date(2023, 1, 1))["users"] == []
    assert q(env, date(2026, 11, 10), date(2026, 11, 10))["totals"]["requests"] == 1


# ----------------------------------------------------------------------------- CLI & members.yaml
def test_load_members_and_cli_roundtrip(env, tmp_path, capsys, monkeypatch):
    members = tmp_path / "members.yaml"
    members.write_text(f"members:\n  - {{name: andi, display_name: Andi P, discord_user_id: '{ANDI}'}}\n"
                       f"  - {{name: budi, discord_user_id: '{BUDI}', profile: khusus}}\n  - {{name: tanpa-id}}\n")
    owners, people = ul.load_members(members)
    assert owners == {"be-andi": ANDI, "khusus": BUDI} and people[ANDI]["display_name"] == "Andi P"
    assert ul.load_members(tmp_path / "tidak-ada.yaml") == ({}, {})
    Profile(env[0], "be-andi").usage("a", 2, 100, 10, 1_800_000_000)
    base = ["--ledger", str(env[1]), "--hermes-root", str(env[0]), "--members-file", str(members), "--tz", "UTC"]
    assert ul.main(base + ["collect"]) == 0
    assert "1 profile" in capsys.readouterr().out
    assert ul.main(base + ["report", "--period", "month", "--offset", "0", "--format", "json", "--no-collect"]) == 0
    json.loads(capsys.readouterr().out)
    assert ul.main(["--ledger", str(tmp_path / "x.db"), "--hermes-root", str(tmp_path / "kosong"),
                    "report", "--no-collect"]) == 2
