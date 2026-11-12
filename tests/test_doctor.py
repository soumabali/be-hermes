import json
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import hermes_team as ht  # noqa: E402
import kit_doctor as kd  # noqa: E402

G = "100000000000000001"
TEAM = {
    "team_name": "T", "guild_id": G,
    "roles": {"member": "200000000000000001", "admin": "200000000000000002"},
    "admins": ["300000000000000001"], "category_id": "400000000000000009",
    "shared": {"profile": "team-shared", "channel_id": "400000000000000001"},
    "admin": {"profile": "team-admin", "channel_id": "400000000000000002"},
}
MEMBERS = {"members": [
    {"name": "andi", "discord_user_id": "500000000000000001", "channel_id": "600000000000000001", "display_name": "Andi"},
    {"name": "budi", "discord_user_id": "500000000000000002", "channel_id": "600000000000000002", "active": False},
]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    td = tmp_path / "team"
    td.mkdir()
    monkeypatch.setenv("HERMES_TEAM_DIR", str(td))
    (td / "team.yaml").write_text(yaml.safe_dump(TEAM))
    (td / "members.yaml").write_text(yaml.safe_dump(MEMBERS))
    team = ht.load_team(td / "team.yaml", td / "members.yaml")
    root = tmp_path / "hermes"
    for p in ("be-andi", "be-budi", "team-shared", "team-admin", "be-template"):
        (root / "profiles" / p).mkdir(parents=True)
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    return team, root, tmp_path


def sync(team, root):
    """Tulis config host + roster seakan `apply` sudah dijalankan."""
    routes = ht.build_routes(team)
    (root / "config.yaml").write_text(yaml.safe_dump({"gateway": {"profile_routes": routes}}))
    state = Path(team["paths"]["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "roster.json").write_text(json.dumps({"gateway": {"profile_routes": routes}}))
    for p in (root / "profiles").iterdir():
        (p / "plugins").mkdir(exist_ok=True)
        (p / "plugins" / ht.PLUGIN_NAME).mkdir(exist_ok=True)
    (root / "plugins" / ht.PLUGIN_NAME).mkdir(exist_ok=True)


def run(team, root):
    rep = kd.Report()
    kd.check_local(team, root, rep, ht)
    return rep


def areas(rep, level=None):
    return [(i["level"], i["area"]) for i in rep.items if level is None or i["level"] == level]


def test_clean_state_has_no_errors(env):
    team, root, _ = env
    sync(team, root)
    assert run(team, root).errors == 0


def test_missing_profile_dir_is_error(env):
    team, root, _ = env
    sync(team, root)
    (root / "profiles" / "be-andi" / "plugins" / ht.PLUGIN_NAME).rmdir()
    os.rmdir(root / "profiles" / "be-andi" / "plugins")
    os.rmdir(root / "profiles" / "be-andi")
    rep = run(team, root)
    assert any("be-andi" in i["message"] and i["level"] == kd.ERROR for i in rep.items)


def test_orphan_profile_warns(env):
    team, root, _ = env
    (root / "profiles" / "be-lama").mkdir()
    sync(team, root)
    rep = run(team, root)
    assert any("yatim" in i["message"] and "be-lama" in i["message"] for i in rep.items)
    assert rep.errors == 0


def test_stale_routes_and_roster_are_errors(env):
    team, root, _ = env
    sync(team, root)
    (root / "config.yaml").write_text(yaml.safe_dump({"gateway": {"profile_routes": []}}))
    rep = run(team, root)
    assert ("error", "rute") in areas(rep)


def test_missing_roster_is_error(env):
    team, root, _ = env
    sync(team, root)
    (Path(team["paths"]["state_dir"]) / "roster.json").unlink()
    assert any("roster.json" in i["message"] for i in run(team, root).items)


def test_parked_active_profile_warns(env):
    team, root, _ = env
    sync(team, root)
    (root / "profiles" / "be-andi" / "gateway.parked").touch()
    assert any(i["area"] == "parkir" and i["level"] == kd.WARN for i in run(team, root).items)


def test_env_permissions_checked(env):
    team, root, _ = env
    sync(team, root)
    f = root / "profiles" / "be-andi" / ".env"
    f.write_text("A=1\n")
    f.chmod(0o644)
    assert any(".env" in i["message"] and i["level"] == kd.ERROR for i in run(team, root).items)
    f.chmod(0o600)
    assert run(team, root).errors == 0


class FakeAPI:
    def __init__(self, channels):
        self.channels = channels

    def get_channel(self, cid):
        return self.channels.get(cid)

    def bot_user_id(self):
        return "999000000000000001"


def chan(cid, deny_everyone=True, guild=G, type_=0, extra=()):
    ows = [{"id": G, "type": 0, "allow": "0", "deny": str(ht.PERM["VIEW_CHANNEL"] if deny_everyone else 0)},
           {"id": "500000000000000001", "type": 1, "allow": str(ht.MEMBER_ALLOW), "deny": "0"}]
    ows += [{"id": i, "type": 1, "allow": str(ht.PERM["VIEW_CHANNEL"]), "deny": "0"} for i in extra]
    return {"id": cid, "guild_id": guild, "type": type_, "permission_overwrites": ows}


def discord(team, channels):
    rep = kd.Report()
    kd.check_discord(team, rep, ht, FakeAPI(channels))
    return rep


def test_discord_ok(env):
    team, *_ = env
    assert discord(team, {"600000000000000001": chan("600000000000000001")}).errors == 0


def test_discord_channel_missing(env):
    team, *_ = env
    rep = discord(team, {})
    assert rep.errors == 1 and "tidak ada" in rep.items[0]["message"]


def test_discord_channel_visible_to_everyone(env):
    team, *_ = env
    rep = discord(team, {"600000000000000001": chan("600000000000000001", deny_everyone=False)})
    assert any("@everyone" in i["message"] for i in rep.items)


def test_discord_wrong_guild_type_and_extra_viewers(env):
    team, *_ = env
    rep = discord(team, {"600000000000000001": chan("600000000000000001", guild="1", type_=2, extra=("777000000000000001",))})
    msgs = " ".join(i["message"] for i in rep.items)
    assert "bukan di server tim" in msgs and "bukan channel teks" in msgs and "777000000000000001" in msgs


def test_render_and_exit_code_json(env):
    team, root, _ = env
    rep = run(team, root)
    out = json.loads(rep.render("json"))
    assert out["ok"] is False and out["items"]
    assert "error" in rep.render("text")
    assert kd.Report().render() .startswith("✓")


# ----------------------------------------------------------------------------- buku besar pemakaian
def test_usage_ledger_missing_warns_with_install_command(env):
    team, *_ = env
    rep = kd.Report()
    kd.check_usage(team, rep)
    assert rep.errors == 0 and rep.items[0]["area"] == "pemakaian"
    assert "hermes-usage-collect.timer" in rep.items[0]["fix"]


def test_usage_ledger_fresh_is_clean_stale_warns_and_loose_mode_errors(env):
    import usage_ledger as ul
    team, root, _ = env
    ledger = Path(team["paths"]["state_dir"]) / "usage.db"
    ul.collect(ledger, root, now=1_800_000_000.0)
    rep = kd.Report()
    kd.check_usage(team, rep, now=1_800_000_000.0 + 600)
    assert rep.items == []
    rep = kd.Report()
    kd.check_usage(team, rep, now=1_800_000_000.0 + 7200)
    assert [i["level"] for i in rep.items] == [kd.WARN] and "120 menit lalu" in rep.items[0]["message"]
    ledger.chmod(0o644)
    rep = kd.Report()
    kd.check_usage(team, rep, now=1_800_000_000.0 + 600)
    assert rep.errors == 1 and "0600" in rep.items[0]["message"]


# ----------------------------------------------------------------------------- --docker (sandbox & state.db)
PS = ("aaa111aaa111\tbe-andi\trunning\thermes-be-andi\n"
      "bbb222bbb222\tteam-shared\texited\thermes-team-shared\n"
      "ccc333ccc333\tbe-lama\trunning\thermes-be-lama\n")
STATS = "aaa111aaa111\t512MiB / 2GiB\nccc333ccc333\t1.5GiB / 2GiB\n"


def fake_docker(ps=PS, stats=STATS, calls=None):
    def run(cmd):
        if calls is not None:
            calls.append(cmd)
        if cmd[:2] == ["docker", "ps"]:
            return 0, ps
        if cmd[:2] == ["docker", "stats"]:
            return 0, stats
        return 1, "perintah tak terduga"
    return run


def meminfo(tmp, gib):
    path = tmp / "meminfo"
    path.write_text(f"MemTotal:       {gib * 1024 * 1024} kB\nMemFree:  1 kB\n")
    return path


def docker_report(team, root, tmp, run, gib=16):
    rep = kd.Report()
    kd.check_docker(team, root, rep, run=run, meminfo=meminfo(tmp, gib))
    return rep


def test_parse_size_units():
    assert kd.parse_size("512MiB") == 512 * 1024 ** 2 and kd.parse_size("1.5GiB") == int(1.5 * 1024 ** 3)
    assert kd.parse_size("0B") == 0 and kd.parse_size(" 2 kB ") == 2000
    assert kd.parse_size("banyak") is None and kd.parse_size("") is None and kd.parse_size("5XB") is None


def test_docker_reports_state_and_ram_per_profile(env):
    team, root, tmp = env
    calls = []
    rep = docker_report(team, root, tmp, fake_docker(calls=calls))
    info = [i["message"] for i in rep.items if i["level"] == kd.INFO]
    assert any(m.startswith("be-andi: container hermes-be-andi: running, RAM 512 MB") for m in info)
    assert any(m.startswith("team-shared: container hermes-team-shared: exited") and "RAM" not in m for m in info)
    assert any(m.startswith("team-admin: container: belum ada") for m in info)
    assert any(m.startswith("be-lama (profile tidak ada di members.yaml)") and "RAM 1536 MB" in m for m in info)
    assert any("total RAM sandbox yang berjalan: 2048 MB dari 16384 MB RAM server (12%)" in m for m in info)
    assert rep.errors == 0 and not [i for i in rep.items if i["level"] == kd.WARN]
    # hanya perintah baca: `docker ps` berlabel hermes-profile, lalu `docker stats --no-stream` untuk yang berjalan
    assert [c[:2] for c in calls] == [["docker", "ps"], ["docker", "stats"]]
    assert "label=hermes-profile" in calls[0] and "--no-stream" in calls[1]
    assert calls[1][-2:] == ["aaa111aaa111", "ccc333ccc333"]


def test_docker_missing_is_a_single_warning_and_state_db_still_checked(env):
    team, root, tmp = env
    (root / "profiles" / "be-andi" / "state.db").write_bytes(b"x" * 2048)

    def missing(cmd):
        raise FileNotFoundError(2, "No such file or directory: 'docker'")

    rep = docker_report(team, root, tmp, missing)
    warns = [i for i in rep.items if i["level"] == kd.WARN]
    assert len(warns) == 1 and "docker tidak bisa diperiksa" in warns[0]["message"] and rep.errors == 0
    assert any(i["message"] == "be-andi: state.db 0 MB" for i in rep.items)
    assert not any("total RAM" in i["message"] for i in rep.items)

    rep = docker_report(team, root, tmp, lambda cmd: (1, "Cannot connect to the Docker daemon"))
    warns = [i for i in rep.items if i["level"] == kd.WARN]
    assert len(warns) == 1 and "Cannot connect" in warns[0]["message"]


def test_docker_warns_on_big_state_db_and_high_ram(env, monkeypatch):
    team, root, tmp = env
    db = root / "profiles" / "be-andi" / "state.db"
    db.write_bytes(b"x" * 4096)
    (root / "profiles" / "be-andi" / "state.db-wal").write_bytes(b"x" * 4096)
    monkeypatch.setattr(kd, "STATE_DB_WARN_BYTES", 6000)                   # 4096 + 4096 (wal ikut dihitung) > 6000
    rep = docker_report(team, root, tmp, fake_docker(), gib=2)               # 2048 MB dari 2048 MB = 100%
    warns = [i["message"] for i in rep.items if i["level"] == kd.WARN]
    assert len(warns) == 2
    assert any("state.db profile be-andi" in w for w in warns)
    assert any("total RAM sandbox" in w and "di atas 80%" in w for w in warns)
    monkeypatch.setattr(kd, "STATE_DB_WARN_BYTES", 500 * 1024 * 1024)
    rep = docker_report(team, root, tmp, fake_docker(), gib=3)               # 68% → tidak diperingatkan
    assert not [i for i in rep.items if i["level"] == kd.WARN]


def test_docker_without_running_containers_skips_stats(env):
    team, root, tmp = env
    calls = []
    rep = docker_report(team, root, tmp, fake_docker(ps="bbb222bbb222\tteam-shared\texited\tx\n", calls=calls))
    assert [c[:2] for c in calls] == [["docker", "ps"]]
    assert any("total RAM sandbox yang berjalan: 0 MB" in i["message"] for i in rep.items)


def test_main_docker_flag_is_wired(env, monkeypatch, capsys):
    team, root, _ = env
    sync(team, root)
    seen = []
    monkeypatch.setattr(kd, "check_docker", lambda team, root, rep: seen.append(root) or rep.add(kd.INFO, "sandbox", "ok"))
    argv = ["--config", team["paths"]["team_file"], "--members", team["paths"]["members_file"],
            "--hermes-root", str(root)]
    kd.main(argv)
    assert seen == []
    kd.main(argv + ["--docker", "--format", "json"])
    assert seen == [root] and '"sandbox"' in capsys.readouterr().out
