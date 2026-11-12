import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import usage_report as ur  # noqa: E402

DDL = """CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '', billing_mode TEXT NOT NULL DEFAULT '', task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0, actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT, cost_source TEXT, first_seen REAL, last_seen REAL)"""
NOW = 1_800_000_000.0


def make_db(home: Path, rows):
    home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home / "state.db")
    con.execute(DDL)
    for sid, model, calls, tin, tout, est, act, seen in rows:
        con.execute("INSERT INTO session_model_usage (session_id, model, api_call_count, input_tokens, output_tokens,"
                    " estimated_cost_usd, actual_cost_usd, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                    (sid, model, calls, tin, tout, est, act, seen))
    con.commit()
    con.close()


def test_aggregates_per_profile_and_sorts_by_cost(tmp_path):
    make_db(tmp_path / "profiles" / "be-a", [("s1", "td-simple", 3, 1000, 200, 0.01, 0, NOW - 100),
                                             ("s2", "td-normal", 1, 500, 100, 0.05, 0.07, NOW - 200)])
    make_db(tmp_path / "profiles" / "be-b", [("s3", "td-simple", 2, 100, 10, 0.5, 0, NOW - 50)])
    rep = ur.build_report(tmp_path, days=7, now=NOW, owners={"be-a": "Andi"})
    assert [p["profile"] for p in rep["profiles"]] == ["be-b", "be-a"]
    a = rep["profiles"][1]
    assert a["owner"] == "Andi" and a["total"]["api_call_count"] == 4
    assert a["total"]["cost_usd"] == 0.08          # biaya sebenarnya menang atas estimasi
    assert rep["totals"]["input_tokens"] == 1600


def test_period_filter_excludes_old_rows(tmp_path):
    make_db(tmp_path / "profiles" / "be-a", [("old", "m", 1, 10, 1, 1.0, 0, NOW - 40 * 86400),
                                             ("new", "m", 1, 20, 2, 2.0, 0, NOW - 86400)])
    rep = ur.build_report(tmp_path, days=7, now=NOW)
    assert rep["totals"]["input_tokens"] == 20


def test_profile_without_table_is_skipped_not_fatal(tmp_path):
    (tmp_path / "profiles" / "be-x").mkdir(parents=True)
    sqlite3.connect(tmp_path / "profiles" / "be-x" / "state.db").close()
    make_db(tmp_path / "profiles" / "be-ok", [("s", "m", 1, 1, 1, 0.1, 0, NOW - 1)])
    rep = ur.build_report(tmp_path, days=1, now=NOW)
    assert [s["profile"] for s in rep["skipped"]] == ["be-x"]
    assert [p["profile"] for p in rep["profiles"]] == ["be-ok"]


def test_default_profile_included_and_db_not_modified(tmp_path):
    make_db(tmp_path, [("s", "m", 1, 1, 1, 0.1, 0, NOW - 1)])
    before = (tmp_path / "state.db").read_bytes()
    rep = ur.build_report(tmp_path, days=1, now=NOW)
    assert rep["profiles"][0]["profile"] == "default"
    assert (tmp_path / "state.db").read_bytes() == before


def test_render_formats(tmp_path):
    make_db(tmp_path / "profiles" / "be-a", [("s", "m", 1, 2_500_000, 1500, 1.2345, 0, NOW - 1)])
    rep = ur.build_report(tmp_path, days=7, now=NOW)
    txt, md = ur.render(rep), ur.render(rep, "markdown")
    assert "2.50M" in txt and "1.5k" in txt and "TOTAL" in txt
    assert md.startswith("**") and "|---|" in md
    assert json.loads(ur.render(rep, "json"))["totals"]["cost_usd"] == 1.2345


def test_owners_from_members_file(tmp_path):
    f = tmp_path / "members.yaml"
    f.write_text("members:\n  - {name: andi, display_name: Andi P}\n  - {name: budi, profile: custom-budi}\n")
    assert ur._owners_from(str(f)) == {"be-andi": "Andi P", "custom-budi": "budi"}
    assert ur._owners_from(str(tmp_path / "nope.yaml")) == {}


def test_cli_rejects_nonpositive_days(tmp_path, capsys):
    try:
        ur.main(["--hermes-root", str(tmp_path), "--days", "0"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("harus keluar dengan kode 2")
