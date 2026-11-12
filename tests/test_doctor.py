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
