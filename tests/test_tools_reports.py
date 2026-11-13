"""Uji tools/usage_post.py (laporan pemakaian terjadwal ke Discord) dan tools/metrics_export.py
(metrics.json → format teks Prometheus).

Tanpa jaringan luar: Discord ditiru tests/fake_discord.py di 127.0.0.1 (lewat TEAMCTL_DISCORD_API) atau
objek API tiruan yang disuntikkan; jam disuntikkan lewat --now.
"""
import importlib.util
import json
import re
import stat
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

KIT = Path(__file__).resolve().parent.parent
for _p in (str(KIT), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fake_discord as fd  # noqa: E402
import usage_ledger as ul  # noqa: E402

TOKEN = "tiruan-token-laporan-JANGAN-BOCOR-9876543210"
WITA = ZoneInfo("Asia/Makassar")
ANDI, BUDI = "500000000000000001", "500000000000000002"
CH_REPORT = fd.CH_ADMIN
CH_VOICE = "400000000000000007"
CH_FOREIGN = "620000000000000001"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"kit_tools_{name}", KIT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


post = _load("usage_post")
export = _load("metrics_export")


def wita(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=WITA)


# ============================================================================ lingkungan tiruan
class Env:
    def __init__(self, tmp_path):
        self.team_dir = tmp_path / "team"
        self.root = tmp_path / "hermes-home"
        self.team_dir.mkdir()
        self.root.mkdir()
        (self.root / ".env").write_text(f"DISCORD_BOT_TOKEN={TOKEN}\n", encoding="utf-8")
        self.team_file = self.team_dir / "team.yaml"
        self.state_file = self.team_dir / "state" / "usage_posted.json"
        self.ledger = self.team_dir / "state" / "usage.db"
        self.write_team()
        self.write_members([("andi", "Andi", ANDI), ("budi", "Budi", BUDI)])
        self.out, self.err = [], []

    def write_team(self, channel=CH_REPORT, reports=None, tz="Asia/Makassar", guild=fd.GUILD, raw_usage=None):
        usage = {"timezone": tz, "report_channel_id": channel,
                 "reports": {"daily": "08:00", "weekly": "08:05", "monthly": "08:10"} if reports is None else reports}
        self.team_file.write_text(yaml.safe_dump({"team_name": "Backend", "guild_id": guild,
                                                  "usage": usage if raw_usage is None else raw_usage}), encoding="utf-8")

    def write_members(self, members):
        body = {"members": [{"name": n, "display_name": d, "discord_user_id": u} for n, d, u in members]}
        (self.team_dir / "members.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")

    def add_usage(self, day, user, tokens_in=1000, tokens_out=500, requests=3):
        con = ul.open_ledger(self.ledger)
        with con:
            con.execute("INSERT INTO daily (day, user_key, profile, model, source, requests, input_tokens, output_tokens)"
                        " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET input_tokens = input_tokens + excluded.input_tokens",
                        (day, user, "be-x", "td-simple", "discord", requests, tokens_in, tokens_out))
        con.close()

    def run(self, *argv, now=None, **kwargs):
        args = ["--team", str(self.team_file), "--hermes-root", str(self.root), *argv]
        if now is not None:
            args += ["--now", now.isoformat()]
        self.out, self.err = [], []
        return post.main(args, out=self.out.append, err=self.err.append, **kwargs)

    def posted(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def printed(self):
        return "\n".join(self.out + self.err)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("DISCORD_BOT_TOKEN", "HERMES_TEAM_TZ", "HERMES_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_TEAM_DIR", str(tmp_path / "bukan-folder-ini"))
    e = Env(tmp_path)
    e.add_usage("2026-11-15", ANDI, 120_000, 30_000)
    e.add_usage("2026-11-15", BUDI, 4000, 1000)
    e.add_usage("2026-11-10", ANDI, 50_000, 5000)
    e.add_usage("2026-10-20", BUDI, 2_000_000, 300_000)
    return e


@pytest.fixture
def discord(monkeypatch):
    state, base, server = fd.start()
    state.add_channel(CH_FOREIGN, "laporan-server-lain", guild_id="100000000000000777")
    monkeypatch.setenv("TEAMCTL_DISCORD_API", base)
    yield state
    server.shutdown()
    server.server_close()


def sent(state, channel=CH_REPORT):
    return [c["body"] for c in state.calls_matching("POST", f"/channels/{channel}/messages")]


class FakeAPI:
    """API tiruan yang disuntikkan (tanpa HTTP sama sekali)."""

    def __init__(self, channel=None, fail_after=None):
        self.channel = {"id": CH_REPORT, "guild_id": fd.GUILD, "type": 0} if channel is None else channel
        self.sent, self.fail_after = [], fail_after

    def get_channel(self, channel_id):
        return self.channel or None

    def send_message(self, channel_id, content, mention_users=()):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise post.kit_discord.DiscordHTTPError("POST", f"/channels/{channel_id}/messages", 403, 50013,
                                                    "Missing Permissions")
        self.sent.append((channel_id, content))
        return {"id": "1"}


# ============================================================================ jadwal (logika murni)
SCHEDULE = {"daily": (8, 0), "weekly": (8, 5), "monthly": (8, 10)}


def kinds(items):
    return {i["kind"]: i["label"] for i in items}


def test_daily_due_only_from_the_configured_time():
    assert post.due_reports({"daily": (8, 0)}, wita(2026, 11, 18, 8, 0), {}) and \
        kinds(post.due_reports({"daily": (8, 0)}, wita(2026, 11, 18, 8, 0), {})) == {"daily": "2026-11-17"}
    # Sebelum jam 08:00 yang jatuh tempo masih laporan hari sebelumnya; kalau itu sudah terkirim → kosong.
    assert post.due_reports({"daily": (8, 0)}, wita(2026, 11, 18, 7, 59), {"daily": "2026-11-16"}) == []
    assert kinds(post.due_reports({"daily": (8, 0)}, wita(2026, 11, 18, 8, 1), {"daily": "2026-11-16"})) == {"daily": "2026-11-17"}


def test_weekly_due_on_monday_and_monthly_on_day_one():
    posted = {"daily": "2026-11-14", "weekly": "2026-W45 (2026-11-02 s.d. 2026-11-08)", "monthly": "2026-10"}
    # Minggu 15 Nov: hanya harian.
    assert set(kinds(post.due_reports(SCHEDULE, wita(2026, 11, 15, 9), {**posted, "daily": "2026-11-13"}))) == {"daily"}
    # Senin 16 Nov 08:04: mingguan belum; 08:05: jatuh tempo, periode Senin–Minggu lalu.
    assert "weekly" not in kinds(post.due_reports(SCHEDULE, wita(2026, 11, 16, 8, 4), posted))
    due = {i["kind"]: i for i in post.due_reports(SCHEDULE, wita(2026, 11, 16, 8, 5), posted)}
    assert due["weekly"]["start"] == date(2026, 11, 9) and due["weekly"]["end"] == date(2026, 11, 15)
    assert due["weekly"]["label"].startswith("2026-W46") and due["weekly"]["offset"] == 1
    # 1 Des 08:10: bulanan = November.
    assert "monthly" not in kinds(post.due_reports(SCHEDULE, wita(2026, 12, 1, 8, 9), posted))
    due = {i["kind"]: i for i in post.due_reports(SCHEDULE, wita(2026, 12, 1, 8, 10), posted)}
    assert due["monthly"]["label"] == "2026-11" and due["monthly"]["start"] == date(2026, 11, 1)
    # Pergantian tahun: 1 Jan → Desember tahun lalu.
    assert kinds(post.due_reports({"monthly": (0, 0)}, wita(2027, 1, 1, 0, 0), {})) == {"monthly": "2026-12"}


def test_timezone_edge_makassar_vs_utc():
    """Senin 16 Nov 00:30 WITA masih Minggu 15 Nov 16:30 UTC: jadwal harus mengikuti WITA."""
    now_utc = datetime(2026, 11, 15, 16, 30, tzinfo=ZoneInfo("UTC"))
    in_wita = post.parse_now(now_utc.isoformat(), WITA)
    assert in_wita.date() == date(2026, 11, 16) and in_wita.utcoffset().total_seconds() == 8 * 3600
    due = kinds(post.due_reports({"daily": (0, 15), "weekly": (0, 15)}, in_wita, {}))
    assert due["daily"] == "2026-11-15" and due["weekly"].startswith("2026-W46")
    # Jam yang sama dibaca sebagai UTC: masih hari Minggu → harian untuk Sabtu, mingguan masih minggu sebelumnya.
    due_utc = kinds(post.due_reports({"daily": (0, 15), "weekly": (0, 15)}, now_utc, {}))
    assert due_utc["daily"] == "2026-11-14" and due_utc["weekly"].startswith("2026-W45")
    # --now tanpa zona dibaca dalam zona waktu pemakaian.
    assert post.parse_now("2026-11-16T08:00", WITA) == wita(2026, 11, 16, 8)


def test_catch_up_only_latest_period_and_never_backwards():
    # Server mati 10 hari; catatan terakhir 5 Nov. Yang disusulkan hanya periode terbaru.
    posted = {"daily": "2026-11-05", "weekly": "2026-W43 (2026-10-19 s.d. 2026-10-25)", "monthly": "2026-08"}
    due = post.due_reports(SCHEDULE, wita(2026, 11, 18, 14, 0), posted)
    assert kinds(due) == {"daily": "2026-11-17", "weekly": "2026-W46 (2026-11-09 s.d. 2026-11-15)", "monthly": "2026-10"}
    assert len(due) == 3
    # Rabu sebelum jam harian: jatuh tempo terakhir = kemarin → laporan lusa (offset 2 dari hari ini).
    item = post.due_reports({"daily": (8, 0)}, wita(2026, 11, 18, 7, 0), {})[0]
    assert item["label"] == "2026-11-16" and item["offset"] == 2
    # Catatan lebih baru daripada jam (jam mundur) → tidak mengirim ulang periode lama.
    assert post.due_reports({"daily": (8, 0)}, wita(2026, 11, 18, 9), {"daily": "2026-11-20"}) == []


def test_next_due_and_hhmm_parsing():
    assert post.next_due("daily", (8, 0), wita(2026, 11, 18, 8, 0)) == wita(2026, 11, 19, 8, 0)
    assert post.next_due("weekly", (8, 5), wita(2026, 11, 18, 9)) == wita(2026, 11, 23, 8, 5)
    assert post.next_due("monthly", (8, 10), wita(2026, 12, 1, 8, 10)) == wita(2027, 1, 1, 8, 10)
    assert post.parse_hhmm("08:00", "x") == (8, 0) and post.parse_hhmm("7:05", "x") == (7, 5)
    assert post.parse_hhmm("", "x") is None and post.parse_hhmm(None, "x") is None
    assert post.parse_hhmm(yaml.safe_load("21:30"), "x") == (21, 30)        # YAML 1.1: 21:30 → 1290
    for bad in ("24:00", "8", "08:60", "pagi"):
        with pytest.raises(post.ConfigError):
            post.parse_hhmm(bad, "usage.reports.daily")


# ============================================================================ bentuk pesan
def assert_valid_chunks(chunks):
    assert chunks
    for chunk in chunks:
        assert len(chunk) <= 2000 and len(chunk) <= post.CHUNK_LIMIT
        assert chunk.count("```") % 2 == 0, "blok kode terbelah"
        assert chunk.strip()


def test_chunking_never_splits_code_block():
    head = "Pengguna      Request  Total token"
    rows = [f"Anggota nomor {i:03d}  {i * 7:>7}  {i * 1234:>11}" for i in range(200)]
    text = "\n".join(["Pemakaian LLM — bulanan 2026-10", "", head, *rows, "", "Data dipotret 0 menit lalu."])
    chunks = post.discord_messages(text, note="Catatan: uji.")
    assert len(chunks) > 3
    assert_valid_chunks(chunks)
    assert chunks[0].startswith("**Pemakaian LLM — bulanan 2026-10**\n```\n" + head)
    assert all(c.startswith("```\n" + head + "\n") for c in chunks[1:-1])       # judul kolom diulang
    assert chunks[-1].endswith("Data dipotret 0 menit lalu.\nCatatan: uji.")
    body = "\n".join(chunks)
    assert all(body.count(row) == 1 for row in rows)                             # tidak ada baris hilang/dobel
    # batas kecil + baris sangat panjang + nama berisi pagar kode
    nasty = "\n".join(["Judul", "", "kolom", "x" * 5000, "nama ```jahat``` @everyone", "", "kaki " * 300])
    for limit in (120, 400, 1900):
        chunks = post.discord_messages(nasty, limit=limit)
        assert all(len(c) <= limit and c.count("```") % 2 == 0 for c in chunks)
    assert "```jahat" not in "\n".join(chunks)


def test_message_shape_from_real_ledger(env):
    text = ul.report(env.ledger, "day", offset=1, fmt="text", now=wita(2026, 11, 16, 9).timestamp(), tz=WITA,
                     people={ANDI: {"name": "andi", "display_name": "Andi"}})
    (message,) = post.discord_messages(text)
    lines = message.split("\n")
    assert lines[0] == "**Pemakaian LLM — harian 2026-11-15**" and lines[1] == "```"
    assert lines[2].startswith("Pengguna") and "|" not in message
    assert any(line.startswith("Andi") and "150.0k" in line for line in lines)
    assert any(line.startswith("TOTAL") for line in lines)


# ============================================================================ alur penuh ke Discord tiruan
def test_posts_once_per_period_with_fake_discord(env, discord):
    monday = wita(2026, 11, 16, 8, 20)
    assert env.run(now=monday) == 0, env.printed()
    bodies = sent(discord)
    assert len(bodies) == 3            # harian + mingguan + bulanan (jalan pertama: periode terbaru tiap jenis)
    assert bodies[0]["content"].startswith("**Pemakaian LLM — harian 2026-11-15**")
    assert bodies[1]["content"].startswith("**Pemakaian LLM — mingguan 2026-W46")
    assert "Andi" in bodies[0]["content"] and "150.0k" in bodies[0]["content"]
    assert "205.0k" in bodies[1]["content"]                    # Andi: 150k (15 Nov) + 55k (10 Nov)
    assert bodies[2]["content"].startswith("**Pemakaian LLM — bulanan 2026-10**")
    for body in bodies:
        assert body["allowed_mentions"] == {"parse": [], "users": []}
        assert len(body["content"]) <= 2000 and body["content"].count("```") % 2 == 0
    assert env.posted()["daily"] == "2026-11-15" and env.posted()["weekly"].startswith("2026-W46")
    assert stat.S_IMODE(env.state_file.stat().st_mode) == 0o600
    # token hanya di header Authorization
    assert all(c["auth"] == f"Bot {TOKEN}" for c in discord.calls)
    assert TOKEN not in env.printed() and TOKEN not in env.state_file.read_text(encoding="utf-8")
    # jalan berikutnya (15 menit kemudian, dan setelah "restart"): tidak ada yang dikirim lagi
    for later in (wita(2026, 11, 16, 8, 35), wita(2026, 11, 16, 23, 50), wita(2026, 11, 17, 7, 59)):
        assert env.run(now=later) == 0
    assert len(sent(discord)) == 3
    # Selasa 08:00 → hanya harian yang baru
    assert env.run(now=wita(2026, 11, 17, 8, 0)) == 0
    assert len(sent(discord)) == 4 and "harian 2026-11-16" in sent(discord)[-1]["content"]
    assert "Tidak ada pemakaian tercatat" in sent(discord)[-1]["content"]


def test_first_run_catches_up_latest_monthly_too(env, discord):
    env.state_file.parent.mkdir(exist_ok=True)
    env.state_file.write_text(json.dumps({"daily": "2026-11-15", "weekly": "2026-W46 (2026-11-09 s.d. 2026-11-15)"}))
    assert env.run(now=wita(2026, 11, 16, 9)) == 0, env.printed()
    (body,) = sent(discord)
    assert body["content"].startswith("**Pemakaian LLM — bulanan 2026-10**") and "2.30M" in body["content"]
    assert env.posted()["monthly"] == "2026-10"


def test_catch_up_after_downtime_sends_one_period_per_kind(env):
    env.state_file.parent.mkdir(exist_ok=True)
    env.state_file.write_text(json.dumps({"daily": "2026-11-01", "weekly": "2026-W43 (x)", "monthly": "2026-10"}))
    api = FakeAPI()
    assert env.run(now=wita(2026, 11, 18, 14), api_factory=lambda token: api) == 0, env.printed()
    titles = [content.split("\n")[0] for _, content in api.sent]
    assert titles == ["**Pemakaian LLM — harian 2026-11-17**",
                      "**Pemakaian LLM — mingguan 2026-W46 (2026-11-09 s.d. 2026-11-15)**"]
    assert env.posted()["daily"] == "2026-11-17"
    assert env.run(now=wita(2026, 11, 18, 14, 15), api_factory=lambda token: api) == 0 and len(api.sent) == 2


def test_nothing_due_exits_zero_without_touching_discord(env):
    env.write_team(reports={"daily": "", "weekly": "08:05"})
    called = []
    env.state_file.parent.mkdir(exist_ok=True)
    env.state_file.write_text(json.dumps({"weekly": "2026-W46 (2026-11-09 s.d. 2026-11-15)"}))
    assert env.run(now=wita(2026, 11, 18, 9), api_factory=called.append) == 0      # Rabu; mingguan sudah terkirim
    assert called == [] and env.printed() == ""
    # fitur mati: channel kosong → diam, kode 0, token tidak dibaca
    env.write_team(channel="")
    assert env.run(now=wita(2026, 11, 18, 9), api_factory=called.append) == 0 and called == []
    assert env.run("--force", "daily", now=wita(2026, 11, 18, 9), api_factory=called.append) == 2


def test_force_ignores_schedule_and_state(env):
    env.write_team(reports={})
    api = FakeAPI()
    now = wita(2026, 11, 16, 3, 0)
    assert env.run(now=now, api_factory=lambda t: api) == 0 and api.sent == []
    assert env.run("--force", "weekly", now=now, api_factory=lambda t: api) == 0, env.printed()
    assert env.run("--force", "weekly", now=now, api_factory=lambda t: api) == 0
    assert [c.split("\n")[0] for _, c in api.sent] == ["**Pemakaian LLM — mingguan 2026-W46 (2026-11-09 s.d. 2026-11-15)**"] * 2
    assert env.run("--force", "monthly", now=now, api_factory=lambda t: api) == 0
    assert "bulanan 2026-10" in api.sent[-1][1]
    assert env.posted() == {"weekly": "2026-W46 (2026-11-09 s.d. 2026-11-15)", "monthly": "2026-10"}
    # yang dipaksa ikut dicatat → jadwal tidak mengirim ulang periode yang sama
    env.write_team()
    assert env.run(now=wita(2026, 11, 16, 8, 6), api_factory=lambda t: api) == 0
    assert [c.split("\n")[0] for _, c in api.sent[3:]] == ["**Pemakaian LLM — harian 2026-11-15**"]


def test_dry_run_sends_nothing_records_nothing(env, discord):
    collected = []
    assert env.run("--dry-run", now=wita(2026, 11, 16, 9), collect=lambda *a, **k: collected.append(1)) == 0
    text = env.printed()
    assert "[dry-run] laporan harian 2026-11-15" in text and "laporan mingguan" in text and "laporan bulanan 2026-10" in text
    assert "**Pemakaian LLM — harian 2026-11-15**" in text and "```" in text
    assert discord.calls == [] and collected == []
    assert not env.state_file.exists()
    assert sorted(p.name for p in env.state_file.parent.iterdir()) == ["usage.db"]
    assert env.run("--dry-run", "--force", "monthly", now=wita(2026, 11, 20, 1)) == 0
    assert "bulanan 2026-10" in env.printed() and discord.calls == [] and not env.state_file.exists()
    assert TOKEN not in text


@pytest.mark.parametrize("channel,expect", [(CH_FOREIGN, "bukan milik server tim"), (CH_VOICE, "bukan channel teks"),
                                            ("499999999999999999", "tidak ditemukan"),
                                            (fd.CAT_HERMES, "bukan channel teks")])
def test_refuses_wrong_guild_or_non_text_channel(env, discord, channel, expect):
    env.write_team(channel=channel)
    assert env.run(now=wita(2026, 11, 16, 9)) == 2
    assert expect in "\n".join(env.err)
    assert discord.calls_matching("POST") == [] and not env.state_file.exists()
    assert env.run("--force", "daily", now=wita(2026, 11, 16, 9)) == 2
    assert discord.calls_matching("POST") == [] and TOKEN not in env.printed()


def test_refuses_thread_and_missing_guild_id(env):
    api = FakeAPI(channel={"id": CH_REPORT, "guild_id": fd.GUILD, "type": 11})
    assert env.run(now=wita(2026, 11, 16, 9), api_factory=lambda t: api) == 2 and api.sent == []
    env.write_team(guild="")
    api = FakeAPI()
    assert env.run(now=wita(2026, 11, 16, 9), api_factory=lambda t: api) == 2 and api.sent == []


def test_collector_failure_still_posts_with_note(env, discord):
    def boom(*args, **kwargs):
        raise RuntimeError(f"state.db rusak {TOKEN}")
    env.write_team(reports={"daily": "08:00"})
    assert env.run(now=wita(2026, 11, 17, 8, 0), collect=boom) == 0, env.printed()
    (body,) = sent(discord)
    assert "Tidak ada pemakaian tercatat" in body["content"] or "TOTAL" in body["content"]
    assert body["content"].split("\n")[-1].startswith("Catatan: potret pemakaian terbaru gagal (RuntimeError)")
    assert TOKEN not in body["content"] and TOKEN not in env.printed()
    assert any("collect gagal" in line for line in env.err)


def test_collect_runs_before_report_with_usage_timezone(env):
    seen = {}

    def collect(ledger, root, **kwargs):
        seen.update(kwargs, ledger=ledger, root=root)
    api = FakeAPI()
    assert env.run("--force", "daily", now=wita(2026, 11, 16, 9), api_factory=lambda t: api, collect=collect) == 0
    assert seen["ledger"] == env.ledger and seen["root"] == env.root and seen["tz"] == WITA
    assert seen["owners"] == {"be-andi": ANDI, "be-budi": BUDI} and seen["now"] == wita(2026, 11, 16, 9).timestamp()
    assert "Catatan" not in api.sent[0][1]


def test_token_never_leaks_on_discord_errors(env, discord):
    discord.fault("POST", f"/channels/{CH_REPORT}/messages", status=403,
                  body={"message": f"Missing Access untuk {TOKEN}", "code": 50001})
    assert env.run("--force", "daily", now=wita(2026, 11, 16, 9)) == 1
    assert "gagal dikirim" in "\n".join(env.err) and TOKEN not in env.printed()
    assert not env.state_file.exists()                        # tidak dicatat → dicoba lagi
    assert env.run("--force", "daily", now=wita(2026, 11, 16, 9, 15)) == 0
    assert env.posted() == {"daily": "2026-11-15"} and TOKEN not in env.state_file.read_text()


def test_send_failure_keeps_earlier_reports_recorded(env):
    api = FakeAPI(fail_after=1)
    env.state_file.parent.mkdir(exist_ok=True)
    env.state_file.write_text(json.dumps({"monthly": "2026-10"}))
    assert env.run(now=wita(2026, 11, 16, 9), api_factory=lambda t: api) == 1
    assert env.posted() == {"daily": "2026-11-15", "monthly": "2026-10"}       # mingguan gagal → belum dicatat
    api.fail_after = None
    assert env.run(now=wita(2026, 11, 16, 9, 15), api_factory=lambda t: api) == 0
    assert [c.split("\n")[0][:30] for _, c in api.sent] == ["**Pemakaian LLM — harian 2026-", "**Pemakaian LLM — mingguan 202"]


def test_missing_token_and_bad_config_exit_2(env, discord, tmp_path):
    (env.root / ".env").write_text("", encoding="utf-8")
    assert env.run(now=wita(2026, 11, 16, 9)) == 2 and "DISCORD_BOT_TOKEN" in "\n".join(env.err)
    assert discord.calls == []
    env.write_team(reports={"daily": "25:00"})
    assert env.run(now=wita(2026, 11, 16, 9)) == 2 and "HH:MM" in "\n".join(env.err)
    env.write_team(reports={"hourly": "08:00"})
    assert env.run(now=wita(2026, 11, 16, 9)) == 2
    env.write_team(tz="Mars/Olympus")
    assert env.run(now=wita(2026, 11, 16, 9)) == 2
    assert post.main(["--team", str(tmp_path / "tidak-ada.yaml")], out=lambda s: None, err=env.err.append) == 2
    env.write_team()
    assert env.run("--now", "kemarin") == 2


def test_long_report_is_chunked_end_to_end(env, discord):
    members = [(f"anggota{i:03d}", f"Anggota Dengan Nama Panjang {i:03d}", str(510000000000000000 + i)) for i in range(90)]
    env.write_members(members)
    for _, _, uid in members:
        env.add_usage("2026-11-15", uid, 1000 + int(uid[-3:]), 10)
    assert env.run("--force", "daily", now=wita(2026, 11, 16, 9)) == 0, env.printed()
    bodies = sent(discord)
    assert len(bodies) >= 3
    for body in bodies:
        assert len(body["content"]) <= 2000 and body["content"].count("```") % 2 == 0
        assert body["allowed_mentions"] == {"parse": [], "users": []}
    joined = "\n".join(b["content"] for b in bodies)
    assert all(joined.count(display) == 1 for _, display, _ in members)


def test_list_shows_schedule_next_due_and_last_sent(env):
    env.state_file.parent.mkdir(exist_ok=True)
    env.state_file.write_text(json.dumps({"daily": "2026-11-17", "monthly": "2026-10", "token": "x"}))
    env.write_team(reports={"daily": "08:00", "monthly": "08:10"})
    called = []
    assert env.run("--list", now=wita(2026, 11, 18, 9), api_factory=called.append) == 0
    text = "\n".join(env.out)
    assert "Asia/Makassar" in text and CH_REPORT in text
    assert re.search(r"daily\s+jam 08:00\s+berikutnya: 2026-11-19 08:00\s+terakhir terkirim: 2026-11-17", text)
    assert re.search(r"weekly\s+mati\s+terakhir terkirim: belum pernah", text)
    assert re.search(r"monthly\s+jam 08:10\s+berikutnya: 2026-12-01 08:10\s+terakhir terkirim: 2026-10", text)
    assert called == []
    assert env.run("--list", now=wita(2026, 11, 19, 9)) == 0
    assert "SEKARANG (periode 2026-11-18 belum terkirim)" in "\n".join(env.out)


def test_systemd_units_run_usage_post_every_15_minutes():
    service = (KIT / "systemd" / "hermes-usage-report.service").read_text(encoding="utf-8")
    timer = (KIT / "systemd" / "hermes-usage-report.timer").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/python3 /opt/hermes-team/tools/usage_post.py" in service and "Type=oneshot" in service
    assert "systemctl --user enable --now hermes-usage-report.timer" in service
    assert re.search(r"^OnCalendar=\*:\d+/15$", timer, re.M) and "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


# ============================================================================ metrics_export
METRICS = {
    "ts": 1_800_000_000, "active_sessions": 3, "active_by_user": {ANDI: 2}, "llm_inflight": 4,
    "llm_inflight_peak_5m": 7, "queue_len": 2, "queue_wait_p95_s": 1.25,
    "refused": {"rate": 5, "queue_full": 1, "budget": 0, "user_concurrent": 2}, "http_429_last_minute": 3,
    "effective_max_active": 4, "configured_max_active": 8, "throttled": True,
    "budget": {"day": "2026-11-16", "used_by_user": {ANDI: 150000, BUDI: 5000, "profile:team-shared": 12},
               "team_used": 155012},
}
NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
LABEL = r'[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\\\|\\"|\\n)*"'
SAMPLE = re.compile(rf"^({NAME})(?:\{{({LABEL}(?:,{LABEL})*)\}})? (-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)$")
COMMENT = re.compile(rf"^# (HELP|TYPE) ({NAME}) (.+)$")


def parse_exposition(text):
    """Pengurai ketat baris demi baris. → ({(nama, label mentah): nilai}, {nama: tipe})."""
    assert text.endswith("\n") and "\r" not in text
    samples, types, helps = {}, {}, set()
    for line in text[:-1].split("\n"):
        comment = COMMENT.match(line)
        if comment:
            kind, name, rest = comment.groups()
            if kind == "TYPE":
                assert rest in ("gauge", "counter") and name not in types
                types[name] = rest
            else:
                assert name not in helps
                helps.add(name)
            continue
        match = SAMPLE.match(line)
        assert match, f"baris tidak valid: {line!r}"
        name, labels, value = match.groups()
        assert name in types and name in helps, f"{name} tanpa HELP/TYPE sebelum sampel"
        assert (name, labels or "") not in samples
        samples[(name, labels or "")] = float(value)
    return samples, types


def test_export_full_output_is_valid_exposition():
    samples, types = parse_exposition(export.render(METRICS, age=4.0))
    assert samples[("hermes_team_up", "")] == 1 and samples[("hermes_team_metrics_age_seconds", "")] == 4
    expect = {"active_sessions": 3, "llm_inflight": 4, "llm_inflight_peak_5m": 7, "queue_length": 2,
              "queue_wait_p95_seconds": 1.25, "http_429_last_minute": 3, "effective_max_active": 4,
              "configured_max_active": 8, "throttled": 1, "budget_team_used_tokens": 155012}
    for name, value in expect.items():
        assert samples[(f"hermes_team_{name}", "")] == value
        assert types[f"hermes_team_{name}"] == "gauge"
    assert types["hermes_team_refused_total"] == "counter"
    for reason, value in METRICS["refused"].items():
        assert samples[("hermes_team_refused_total", f'reason="{reason}"')] == value
    assert samples[("hermes_team_budget_used_tokens", f'user="{ANDI}"')] == 150000
    assert samples[("hermes_team_budget_used_tokens", 'user="profile:team-shared"')] == 12
    assert len(samples) == 2 + len(expect) + 4 + 3
    assert all(name.startswith("hermes_team_") for name, _ in samples)
    assert "active_by_user" not in export.render(METRICS, age=4.0)
    off = dict(METRICS, throttled=False)
    assert parse_exposition(export.render(off, age=0))[0][("hermes_team_throttled", "")] == 0


def test_export_label_escaping():
    data = dict(METRICS, budget={"used_by_user": {'a"b\\c\nd': 7, "ünï}{": 1}, "team_used": 8},
                refused={'ra"te': 1})
    text = export.render(data, age=1)
    assert 'hermes_team_budget_used_tokens{user="a\\"b\\\\c\\nd"} 7' in text.split("\n")
    assert 'hermes_team_refused_total{reason="ra\\"te"} 1' in text.split("\n")
    parse_exposition(text)
    assert export.escape_label('\\"\n') == '\\\\\\"\\n'


def test_export_skips_missing_and_non_numeric_values():
    data = {"ts": 1, "active_sessions": "tiga", "llm_inflight": None, "queue_len": float("nan"),
            "queue_wait_p95_s": float("inf"), "http_429_last_minute": True, "effective_max_active": [4],
            "configured_max_active": 8, "throttled": "ya", "refused": {"rate": "banyak", "budget": 2, "x": -1},
            "budget": {"used_by_user": {ANDI: None, BUDI: 9}, "team_used": {}}}
    samples, _ = parse_exposition(export.render(data, age=0.5))
    assert set(samples) == {("hermes_team_up", ""), ("hermes_team_metrics_age_seconds", ""),
                            ("hermes_team_configured_max_active", ""), ("hermes_team_refused_total", 'reason="budget"'),
                            ("hermes_team_budget_used_tokens", f'user="{BUDI}"')}
    for junk in ({"refused": [1], "budget": "x"}, {}, {"budget": {"used_by_user": [1]}}):
        samples, _ = parse_exposition(export.render(junk, age=0))
        assert set(samples) == {("hermes_team_up", ""), ("hermes_team_metrics_age_seconds", "")}


def test_export_stale_and_missing_input(tmp_path, capsys):
    samples, _ = parse_exposition(export.render(METRICS, age=121.0))
    assert samples == {("hermes_team_up", ""): 0, ("hermes_team_metrics_age_seconds", ""): 121}
    assert parse_exposition(export.render(METRICS, age=120.0))[0][("hermes_team_up", "")] == 1
    assert parse_exposition(export.render(METRICS, age=121.0, max_age=300))[0][("hermes_team_up", "")] == 1
    assert parse_exposition(export.render(None, age=None))[0] == {("hermes_team_up", ""): 0}
    # CLI: berkas tidak ada → kode 0, up 0
    assert export.main(["--input", str(tmp_path / "tidak-ada.json")]) == 0
    assert parse_exposition(capsys.readouterr().out)[0] == {("hermes_team_up", ""): 0}
    # CLI: rusak → up 0 + umur dari mtime; basi lewat ts; segar
    source = tmp_path / "metrics.json"
    source.write_text("{bukan json", encoding="utf-8")
    assert export.main(["--input", str(source)]) == 0
    samples, _ = parse_exposition(capsys.readouterr().out)
    assert samples[("hermes_team_up", "")] == 0 and ("hermes_team_metrics_age_seconds", "") in samples
    source.write_text(json.dumps(METRICS), encoding="utf-8")
    assert export.main(["--input", str(source)], now=METRICS["ts"] + 500) == 0
    assert parse_exposition(capsys.readouterr().out)[0] == {("hermes_team_up", ""): 0,
                                                             ("hermes_team_metrics_age_seconds", ""): 500}
    assert export.main(["--input", str(source), "--max-age", "600"], now=METRICS["ts"] + 500) == 0
    assert parse_exposition(capsys.readouterr().out)[0][("hermes_team_active_sessions", "")] == 3
    assert export.main(["--input", str(source), "--max-age", "-1"]) == 2


def test_export_output_file_atomic_0644_and_default_input(tmp_path, monkeypatch, capsys):
    team = tmp_path / "team"
    (team / "state").mkdir(parents=True)
    (team / "state" / "metrics.json").write_text(json.dumps(METRICS), encoding="utf-8")
    monkeypatch.setenv("HERMES_TEAM_DIR", str(team))
    target = tmp_path / "textfile" / "hermes_team.prom"
    target.parent.mkdir()
    target.write_text("lama\n", encoding="utf-8")
    assert export.main(["--output", str(target)], now=METRICS["ts"] + 2) == 0
    assert capsys.readouterr().out == ""
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    samples, _ = parse_exposition(target.read_text(encoding="utf-8"))
    assert samples[("hermes_team_up", "")] == 1 and samples[("hermes_team_llm_inflight", "")] == 4
    assert [p.name for p in target.parent.iterdir()] == ["hermes_team.prom"]       # tidak ada berkas sementara tersisa
    # folder tujuan tidak ada → kode 1, pesan jelas, tidak crash
    assert export.main(["--output", str(tmp_path / "tidak-ada" / "x.prom")]) == 1
    assert "tidak bisa ditulis" in capsys.readouterr().err
