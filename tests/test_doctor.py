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


# ----------------------------------------------------------------------------- konfigurasi (key tak dikenal, ID contoh)
def test_config_warnings_and_placeholders_reach_doctor(env, monkeypatch):
    team, root, tmp = env
    monkeypatch.delenv("HERMES_TEAM_ALLOW_PLACEHOLDERS", raising=False)
    raw = dict(TEAM, scale={"max_concurent_sessions": 3})
    (tmp / "team" / "team.yaml").write_text(yaml.safe_dump(raw))
    team = ht.load_team(tmp / "team" / "team.yaml")
    rep = kd.Report()
    kd.check_config(team, rep, ht)
    warn = [i for i in rep.items if i["level"] == kd.WARN]
    assert len(warn) == 1 and "max_concurrent_sessions" in warn[0]["message"] and warn[0]["area"] == "konfigurasi"
    errors = [i for i in rep.items if i["level"] == kd.ERROR]
    assert len(errors) == 1 and "ID contoh" in errors[0]["message"] and "guild_id" in errors[0]["message"]
    monkeypatch.setenv("HERMES_TEAM_ALLOW_PLACEHOLDERS", "1")
    rep = kd.Report()
    kd.check_config(team, rep, ht)
    assert rep.errors == 0


# ----------------------------------------------------------------------------- managed scope (/etc/hermes)
LLM = {"providers": {"td": {"api": "https://llm.example.internal/v1", "key_env": "TD_API_KEY"},
                     "cadangan": {"api": "https://b.example.internal/v1", "key_env": "BACKUP_API_KEY"}},
       "models": {"td-simple": {"provider": "custom:td", "model": "td-simple"}}, "default_model": "td-simple"}


def managed_report(team, folder):
    rep = kd.Report()
    kd.check_managed(team, rep, ht, managed_dir=folder)
    return rep


def test_managed_file_missing_is_error_with_install_command(env):
    team, _root, tmp = env
    rep = managed_report(team, tmp / "etc-hermes")
    item = next(i for i in rep.items if i["area"] == "managed")
    assert item["level"] == kd.ERROR and "belum ada" in item["message"]
    assert "sudo" in item["fix"] and "managed --install" in item["fix"] and team["paths"]["team_file"] in item["fix"]


def test_managed_drift_is_warning_listing_keys_and_clean_file_is_silent(env):
    team, _root, tmp = env
    folder = tmp / "etc-hermes"
    ht.install_managed(team, target_dir=folder, euid=0, allow_user_owned_kit=True)
    assert [i for i in managed_report(team, folder).items if i["area"] == "managed"] == []
    data = yaml.safe_load((folder / "config.yaml").read_text())
    data["agent"]["max_turns"] = 7
    del data["cron"]
    data["security"]["tambahan"] = True
    (folder / "config.yaml").write_text(yaml.safe_dump(data))
    rep = managed_report(team, folder)
    assert rep.errors == 0
    item = next(i for i in rep.items if i["area"] == "managed")
    assert item["level"] == kd.WARN
    for key in ("berbeda agent.max_turns", "kurang cron.max_parallel_jobs", "berlebih security.tambahan"):
        assert key in item["message"], key
    assert "managed --diff" in item["fix"]


def test_keys_moved_out_of_managed_are_an_error_with_the_three_step_fix(env):
    team, _root, tmp = env
    folder = tmp / "etc-hermes"
    ht.install_managed(team, target_dir=folder, euid=0, allow_user_owned_kit=True)
    data = yaml.safe_load((folder / "config.yaml").read_text())
    data["terminal"].update({"container_cpu": 1, "container_memory": 1024})
    data["compression"]["threshold_tokens"] = 64000
    (folder / "config.yaml").write_text(yaml.safe_dump(data))
    rep = managed_report(team, folder)
    errors = [i for i in rep.items if i["level"] == kd.ERROR]
    assert len(errors) == 1
    for key in ("terminal.container_cpu", "terminal.container_memory", "compression.threshold_tokens"):
        assert key in errors[0]["message"]
    fix = errors[0]["fix"]
    assert fix.index("apply") < fix.index("managed") < fix.rindex("apply") and "sekali lagi" in fix
    assert not [i for i in rep.items if i["level"] == kd.WARN]          # key pindahan tidak dilaporkan dua kali


def test_managed_env_must_exist_be_private_and_hold_every_key_env(env):
    team, _root, tmp = env
    (tmp / "team" / "team.yaml").write_text(yaml.safe_dump(dict(TEAM, llm=LLM)))
    team = ht.load_team(tmp / "team" / "team.yaml")
    folder = tmp / "etc-hermes"
    ht.install_managed(team, target_dir=folder, euid=0, allow_user_owned_kit=True)
    env_file = folder / ".env"
    rep = managed_report(team, folder)
    item = next(i for i in rep.items if ".env" in i["message"])
    assert item["level"] == kd.ERROR and "belum ada" in item["message"]
    assert "BACKUP_API_KEY, TD_API_KEY" in item["message"] and "chmod 640" in item["fix"]

    secret = "sk-RAHASIA-jangan-tercetak-123"
    env_file.write_text(f"TD_API_KEY={secret}\nBACKUP_API_KEY=\n")
    env_file.chmod(0o644)
    rep = managed_report(team, folder)
    messages = [i["message"] for i in rep.items if i["level"] == kd.ERROR]
    assert any("terbaca SEMUA user" in m and "0o644" in m for m in messages)
    missing = next(m for m in messages if "tidak memuat" in m)
    assert "BACKUP_API_KEY" in missing and "TD_API_KEY" not in missing     # ada tapi kosong = belum diisi
    assert secret not in rep.render("text") and secret not in rep.render("json")

    env_file.write_text(f"TD_API_KEY={secret}\nBACKUP_API_KEY=x-cadangan-456\n")
    env_file.chmod(0o640)
    assert managed_report(team, folder).items == []


def test_managed_env_per_user_keys_downgrades_missing_key_to_warning(env):
    team, _root, tmp = env
    (tmp / "team" / "team.yaml").write_text(yaml.safe_dump(dict(TEAM, llm=dict(LLM, per_user_keys=True))))
    team = ht.load_team(tmp / "team" / "team.yaml")
    folder = tmp / "etc-hermes"
    ht.install_managed(team, target_dir=folder, euid=0, allow_user_owned_kit=True)
    rep = managed_report(team, folder)
    assert rep.errors == 0 and any("per_user_keys" in i["message"] for i in rep.items)


# ----------------------------------------------------------------------------- systemd: linger & timer
def runner(table, calls=None):
    def run(cmd):
        if calls is not None:
            calls.append(cmd)
        result = table.get(cmd[0] if cmd[0] == "loginctl" else cmd[-1])
        if isinstance(result, Exception):
            raise result
        return result if result is not None else (1, "not-found")
    return run


def test_linger_states(env):
    calls = []
    rep = kd.Report()
    kd.check_linger(rep, runner({"loginctl": (0, "Linger=yes\n")}, calls), user="hermes")
    assert rep.items == [] and calls == [["loginctl", "show-user", "hermes", "-p", "Linger"]]
    rep = kd.Report()
    kd.check_linger(rep, runner({"loginctl": (0, "Linger=no\n")}), user="hermes")
    assert rep.items[0]["level"] == kd.WARN and rep.items[0]["fix"] == "sudo loginctl enable-linger hermes"
    rep = kd.Report()
    kd.check_linger(rep, runner({"loginctl": (1, "Failed to get user: User ID 1001 is not logged in or lingering")}),
                    user="hermes")
    assert rep.items[0]["level"] == kd.WARN and "not logged in or lingering" in rep.items[0]["message"]
    rep = kd.Report()
    kd.check_linger(rep, runner({"loginctl": FileNotFoundError(2, "loginctl")}), user="hermes")
    assert [i["level"] for i in rep.items] == [kd.INFO] and rep.errors == 0          # tanpa loginctl: ditoleransi


def test_timers_collect_always_report_only_when_configured(env):
    team, _root, tmp = env
    calls = []
    rep = kd.Report()
    kd.check_timers(team, rep, runner({"hermes-usage-collect.timer": (0, "enabled\n"),
                                       "hermes-sandbox-idle.timer": (0, "enabled\n")}, calls))
    assert rep.items == []
    assert [c[-1] for c in calls] == ["hermes-usage-collect.timer", "hermes-sandbox-idle.timer"]
    assert calls[0][:3] == ["systemctl", "--user", "is-enabled"]

    rep = kd.Report()
    kd.check_timers(team, rep, runner({}))
    levels = {i["message"].split()[1]: i["level"] for i in rep.items}
    assert levels == {"hermes-usage-collect.timer": kd.WARN, "hermes-sandbox-idle.timer": kd.INFO}
    fix = rep.items[0]["fix"]
    assert fix.startswith("mkdir -p ~/.config/systemd/user && cp ")
    assert f"{kd.KIT_DIR}/systemd/hermes-usage-collect.*" in fix and "enable --now hermes-usage-collect.timer" in fix

    raw = dict(TEAM, usage={"report_channel_id": "400000000000000002", "reports": {"daily": "08:00"}})
    (tmp / "team" / "team.yaml").write_text(yaml.safe_dump(raw))
    with_reports = ht.load_team(tmp / "team" / "team.yaml")
    calls.clear()
    rep = kd.Report()
    kd.check_timers(with_reports, rep, runner({"hermes-usage-collect.timer": (0, "enabled\n"),
                                               "hermes-usage-report.timer": (1, "disabled\n")}, calls))
    assert "hermes-usage-report.timer" in [c[-1] for c in calls]
    item = next(i for i in rep.items if "hermes-usage-report.timer" in i["message"])
    assert item["level"] == kd.WARN and "(disabled)" in item["message"] and "hermes-usage-report.timer" in item["fix"]


def test_timers_tolerate_missing_systemctl_and_missing_user_bus(env):
    team, *_ = env
    rep = kd.Report()
    kd.check_timers(team, rep, runner({"hermes-usage-collect.timer": FileNotFoundError(2, "systemctl")}))
    assert [i["level"] for i in rep.items] == [kd.INFO]
    rep = kd.Report()
    kd.check_timers(team, rep, runner({"hermes-usage-collect.timer": (1, "Failed to connect to bus: No medium found")}))
    assert [i["level"] for i in rep.items] == [kd.INFO] and rep.errors == 0


def test_usage_fix_creates_the_user_unit_directory(env):
    team, *_ = env
    rep = kd.Report()
    kd.check_usage(team, rep)
    assert rep.items[0]["fix"].startswith("pasang timer: mkdir -p ~/.config/systemd/user && cp ")


def test_main_runs_system_checks_with_injected_runner(env, monkeypatch, capsys):
    team, root, tmp = env
    sync(team, root)
    folder = tmp / "etc-hermes"
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(folder))
    monkeypatch.setenv("HERMES_TEAM_ALLOW_PLACEHOLDERS", "1")
    calls = []
    run = runner({"loginctl": (0, "Linger=yes"), "hermes-usage-collect.timer": (0, "enabled"),
                  "hermes-sandbox-idle.timer": (0, "enabled")}, calls)
    argv = ["--config", team["paths"]["team_file"], "--members", team["paths"]["members_file"],
            "--hermes-root", str(root), "--format", "json"]
    assert kd.main(argv, run=run) == 1                                   # managed belum terpasang → error
    out = json.loads(capsys.readouterr().out)
    assert [i["area"] for i in out["items"] if i["level"] == "error"] == ["managed"]
    assert {c[0] for c in calls} == {"loginctl", "systemctl"}
    ht.install_managed(team, target_dir=folder, euid=0, allow_user_owned_kit=True)
    assert kd.main(argv, run=run) == 0


# ----------------------------------------------------------------------------- preflight Discord (--discord)
import fake_discord as fd  # noqa: E402

BOT_ROLE = "200000000000000099"
NEEDED = ("MANAGE_ROLES", "MANAGE_CHANNELS", "VIEW_AUDIT_LOG", "MANAGE_MESSAGES", "MANAGE_THREADS",
          "CREATE_PUBLIC_THREADS", "SEND_MESSAGES_IN_THREADS")


@pytest.fixture
def server(monkeypatch):
    state, base, srv = fd.start()
    monkeypatch.setenv("TEAMCTL_DISCORD_API", base)
    yield state, ht.DiscordAPI("token-tiruan", sleep=lambda _s: None)
    srv.shutdown()
    srv.server_close()


def healthy(state):
    """Server tiruan dalam kondisi yang diminta kit: izin bot lengkap, channel bersama tertutup untuk @everyone."""
    state.roles[BOT_ROLE]["permissions"] = str(sum(ht.PERM[p] for p in NEEDED))
    hidden = {"id": fd.GUILD, "type": 0, "allow": "0", "deny": str(ht.PERM["VIEW_CHANNEL"])}
    bot = {"id": fd.BOT, "type": 1, "allow": str(ht.PERM["VIEW_CHANNEL"] | ht.PERM["SEND_MESSAGES"]), "deny": "0"}
    for cid in (fd.CH_SHARED, fd.CH_ADMIN):
        state.channels[cid]["permission_overwrites"] = [dict(hidden), dict(bot)]


def preflight(team, api):
    rep = kd.Report()
    ok = kd.check_discord_setup(team, rep, ht, api)
    return rep, ok


def team_with(tmp, **extra):
    raw = dict(TEAM, roles={**TEAM["roles"], "entry": fd.ROLE_ENTRY}, **extra)
    (tmp / "team" / "team.yaml").write_text(yaml.safe_dump(raw))
    (tmp / "team" / "members.yaml").write_text("members: []\n")           # preflight tidak butuh anggota
    return ht.load_team(tmp / "team" / "team.yaml")


def errors_of(rep):
    return [i["message"] for i in rep.items if i["level"] == kd.ERROR]


def test_preflight_clean_server_has_no_findings_even_without_members(env, server):
    state, api = server
    healthy(state)
    team = team_with(env[2])
    assert team["members"] == []
    rep, ok = preflight(team, api)
    assert ok is True and rep.items == [], rep.render()
    assert state.calls and not [c for c in state.calls if c["method"] != "GET"]     # hanya membaca


def test_preflight_invalid_token_stops_early_with_fix(env, server):
    state, api = server
    state.fault("GET", "/users/@me", status=401, body={"message": "401: Unauthorized", "code": 0})
    rep, ok = preflight(team_with(env[2]), api)
    assert ok is False and len(rep.items) == 1
    assert "token bot ditolak" in rep.items[0]["message"] and "DISCORD_BOT_TOKEN" in rep.items[0]["fix"]


def test_preflight_bot_not_in_guild(env, server):
    state, api = server
    del state.members[fd.BOT]
    rep, ok = preflight(team_with(env[2]), api)
    assert ok is False and "belum ada di server" in rep.items[0]["message"] and "OAuth2" in rep.items[0]["fix"]


def test_preflight_missing_roles_and_role_hierarchy(env, server):
    state, api = server
    healthy(state)
    del state.roles[fd.ROLE_ADMIN]
    state.roles[fd.ROLE_MEMBER]["position"] = 9                          # di atas role bot (posisi 7)
    state.roles[fd.ROLE_ENTRY]["position"] = 7                           # sejajar = tetap tidak bisa dikelola
    rep, _ = preflight(team_with(env[2]), api)
    found = errors_of(rep)
    assert any("role roles.admin" in m and "tidak ada" in m for m in found)
    above = [i for i in rep.items if "tidak berada di ATAS" in i["message"]]
    assert sorted(i["message"].split("ATAS ")[1].split(" ")[0] for i in above) == ["roles.entry", "roles.member"]
    assert all("drag-and-drop" in i["fix"] and "ATAS" in i["fix"] for i in above)
    assert len(found) == 3


def test_preflight_guild_permissions_are_computed_from_roles(env, server):
    state, api = server
    healthy(state)
    state.roles[BOT_ROLE]["permissions"] = str(ht.PERM["MANAGE_ROLES"] | ht.PERM["MANAGE_THREADS"])
    state.roles[fd.GUILD]["permissions"] = str(int(state.roles[fd.GUILD]["permissions"])
                                               | ht.PERM["CREATE_PUBLIC_THREADS"])   # dari @everyone juga dihitung
    rep, _ = preflight(team_with(env[2]), api)
    missing = sorted(m.split("izin ")[1].split(" (")[0] for m in errors_of(rep) if "tidak punya izin" in m)
    assert missing == ["Manage Channels", "Manage Messages", "Send Messages in Threads", "View Audit Log"]
    assert all("Server Settings → Roles" in i["fix"] for i in rep.items)
    # fitur yang memakainya dimatikan → peringatan, bukan error
    team = team_with(env[2], onboarding={"verify_actor": False}, secrets={"delete_secret_messages": False})
    rep, _ = preflight(team, api)
    soft = sorted(i["message"].split("izin ")[1].split(" (")[0] for i in rep.items if i["level"] == kd.WARN)
    assert soft == ["Manage Messages", "View Audit Log"]
    # Administrator mencakup semuanya (termasuk izin channel)
    state.roles[BOT_ROLE]["permissions"] = str(ht.PERM["ADMINISTRATOR"])
    for cid in (fd.CH_SHARED, fd.CH_ADMIN):
        state.channels[cid]["permission_overwrites"] = state.channels[cid]["permission_overwrites"][:1]
    rep, _ = preflight(team_with(env[2]), api)
    assert rep.items == []


def test_preflight_category_missing_wrong_type_and_full(env, server):
    state, api = server
    healthy(state)
    rep, _ = preflight(team_with(env[2], category_id="400000000000000077"), api)
    assert any("category_id 400000000000000077 tidak ada" in m for m in errors_of(rep))
    rep, _ = preflight(team_with(env[2], category_id=fd.CH_GENERAL), api)
    assert any("bukan kategori" in m for m in errors_of(rep))
    for n in range(46):                                                   # 4 bawaan + 46 = 50
        state.add_channel(720000000000000000 + n, f"hermes-x{n}")
    rep, _ = preflight(team_with(env[2]), api)
    full = [i for i in rep.items if "sudah berisi 50 channel" in i["message"]]
    assert len(full) == 1 and full[0]["level"] == kd.ERROR and "kategori baru" in full[0]["fix"]
    del state.channels["720000000000000000"]
    rep, _ = preflight(team_with(env[2]), api)
    assert rep.errors == 0 and any("hampir penuh: 49/50" in i["message"] for i in rep.items)


def test_preflight_shared_and_admin_channels(env, server):
    state, api = server
    healthy(state)
    # seperti bawaan server tiruan: #hermes-shared tanpa overwrite → terlihat @everyone
    state.channels[fd.CH_SHARED]["permission_overwrites"] = []
    # admin: tertutup untuk @everyone, tetapi bot tidak diberi izin → bot tidak bisa melihat/mengirim
    state.channels[fd.CH_ADMIN]["permission_overwrites"] = state.channels[fd.CH_ADMIN]["permission_overwrites"][:1]
    rep, _ = preflight(team_with(env[2]), api)
    found = errors_of(rep)
    assert len(found) == 2
    assert any("shared.channel_id #hermes-shared bisa dilihat @everyone" in m for m in found)
    assert any("bot tidak bisa View Channel / Send Messages di admin.channel_id" in m for m in found)
    assert any("matikan View Channel" in i["fix"] for i in rep.items)

    healthy(state)
    state.channels[fd.CH_SHARED]["type"] = 2
    del state.channels[fd.CH_ADMIN]
    rep, _ = preflight(team_with(env[2]), api)
    found = errors_of(rep)
    assert any("shared.channel_id" in m and "bukan channel teks" in m for m in found)
    assert any("admin.channel_id" in m and "tidak ada" in m for m in found)


def test_preflight_channel_in_another_guild(env, server):
    state, api = server
    healthy(state)
    other = dict(state.channels.pop(fd.CH_SHARED), guild_id="111111111111111111")

    class OtherGuild:
        """Fake server hanya punya satu guild: channel milik server lain ditiru di lapisan API."""
        def __getattr__(self, name):
            return getattr(api, name)

        def get_channel(self, cid):
            return other if cid == fd.CH_SHARED else api.get_channel(cid)

    rep, _ = preflight(team_with(env[2]), OtherGuild())
    assert any("bukan di server tim (guild 111111111111111111)" in m for m in errors_of(rep))


def test_preflight_report_channel_must_exist_and_be_writable(env, server):
    state, api = server
    healthy(state)
    report = "730000000000000001"
    usage = {"report_channel_id": report, "reports": {"daily": "08:00"}}
    rep, _ = preflight(team_with(env[2], usage=usage), api)
    assert any("usage.report_channel_id" in m and "tidak ada" in m for m in errors_of(rep))
    state.add_channel(report, "hermes-usage", overwrites=[
        {"id": fd.GUILD, "type": 0, "allow": "0", "deny": str(ht.PERM["VIEW_CHANNEL"])},
        {"id": BOT_ROLE, "type": 0, "allow": str(ht.PERM["VIEW_CHANNEL"]), "deny": str(ht.PERM["SEND_MESSAGES"])}])
    rep, _ = preflight(team_with(env[2], usage=usage), api)
    found = errors_of(rep)
    assert len(found) == 1 and "bot tidak bisa Send Messages di usage.report_channel_id #hermes-usage" in found[0]
    state.channels[report]["permission_overwrites"][1]["deny"] = "0"
    state.channels[report]["permission_overwrites"][1]["allow"] = str(ht.PERM["VIEW_CHANNEL"] | ht.PERM["SEND_MESSAGES"])
    rep, _ = preflight(team_with(env[2], usage=usage), api)
    assert rep.items == []
    # channel laporan yang terbuka untuk semua bukan kesalahan (boleh channel umum); pakai channel admin → tidak dobel
    rep, _ = preflight(team_with(env[2], usage={"report_channel_id": fd.CH_ADMIN}), api)
    assert rep.items == []


def test_main_discord_runs_preflight_before_gateway_and_with_no_members(env, server, monkeypatch, capsys):
    state, api = server
    team, root, tmp = env
    team = team_with(tmp)
    sync(team, root)
    folder = tmp / "etc-hermes"
    ht.install_managed(team, target_dir=folder, euid=0, allow_user_owned_kit=True)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(folder))
    monkeypatch.setenv("HERMES_TEAM_ALLOW_PLACEHOLDERS", "1")
    run = runner({"loginctl": (0, "Linger=yes"), "hermes-usage-collect.timer": (0, "enabled"),
                  "hermes-sandbox-idle.timer": (0, "enabled")})
    argv = ["--config", team["paths"]["team_file"], "--hermes-root", str(root), "--format", "json", "--discord"]
    assert kd.main(argv, run=run, api=api) == 1                           # server tiruan bawaan: banyak temuan
    items = json.loads(capsys.readouterr().out)["items"]
    assert sum(1 for i in items if i["area"] == "discord" and i["level"] == "error") >= 5
    assert all(i["fix"] for i in items if i["area"] == "discord")         # tiap kegagalan menyebut perbaikannya
    healthy(state)
    assert kd.main(argv, run=run, api=api) == 0
    # tanpa token sama sekali: satu error yang jelas, bukan traceback
    assert kd.main(argv, run=run) == 1
    assert "DISCORD_BOT_TOKEN" in capsys.readouterr().out


# ----------------------------------------------------------------------------- capabilities.json (plugin)
def _caps(team, payload):
    state = Path(team["paths"]["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "capabilities.json").write_text(payload if isinstance(payload, str) else json.dumps(payload))


def test_capabilities_missing_is_info_only(env):
    team, *_ = env
    rep = kd.Report()
    kd.check_capabilities(team, rep)
    assert rep.errors == 0 and [i["level"] for i in rep.items] == [kd.INFO]


def test_capabilities_unavailable_feature_warns_with_reason(env):
    team, *_ = env
    _caps(team, {"schema": 1, "hermes_version": "9.9", "generated_at": "2027-01-15T08:00:00Z",
                 "capabilities": {"live_reload.rescan": {"available": False, "reason": "simbol hilang"},
                                  "dispatch.get_command": {"available": True, "reason": ""}}})
    rep = kd.Report()
    kd.check_capabilities(team, rep, now=1_800_000_100)
    warns = [i for i in rep.items if i["level"] == kd.WARN]
    assert len(warns) == 1 and "live_reload.rescan" in warns[0]["message"] and "simbol hilang" in warns[0]["message"]


def test_capabilities_all_available_is_a_positive_info(env):
    team, *_ = env
    _caps(team, {"schema": 1, "hermes_version": "9.9", "generated_at": "2027-01-15T08:00:00Z",
                 "capabilities": {"a": {"available": True, "reason": ""}, "b": {"available": True, "reason": ""}}})
    rep = kd.Report()
    kd.check_capabilities(team, rep, now=1_800_000_100)
    assert [i["level"] for i in rep.items] == [kd.INFO] and "semua 2 API" in rep.items[0]["message"]


@pytest.mark.parametrize("payload", ["bukan json", "[]", json.dumps({"capabilities": "salah"})])
def test_capabilities_garbage_never_crashes(env, payload):
    team, *_ = env
    _caps(team, payload)
    rep = kd.Report()
    kd.check_capabilities(team, rep)
    assert rep.errors == 0 and rep.items and rep.items[0]["level"] == kd.WARN


def test_capabilities_stale_file_is_noted_using_the_real_timestamp_format(env):
    import calendar
    import time as _t
    team, *_ = env
    _caps(team, {"schema": 1, "hermes_version": "9.9", "generated_at": "2026-01-01T00:00:00Z",
                 "capabilities": {"a": {"available": True, "reason": ""}}})
    rep = kd.Report()
    kd.check_capabilities(team, rep, now=calendar.timegm(_t.strptime("2026-03-01", "%Y-%m-%d")))
    assert any("berumur 59 hari" in i["message"] for i in rep.items)
