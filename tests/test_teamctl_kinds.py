"""Jenis profile terdaftar (PROFILE_KINDS) ikut dikenali teamctl: target set_model dan perlindungan channel tim."""
import pytest

import kit.profiles as kit_profiles
from teamctl_helpers import Env, add_channel, fd, ht, tm

OPS_CH = "400000000000000077"


@pytest.fixture
def tc(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    yield env
    env.close()


@pytest.fixture
def ops(tc, register_kind):
    """Jenis palsu `ops` (needs_channel + model_target) terdaftar + channelnya ada di Discord tiruan."""
    kind = register_kind(kit_profiles.ProfileKind(
        "ops", ht.SOUL_SHARED, kit_profiles._shared_settings, needs_channel=True, reserved=True,
        model_target=True, note="ops, @mention", channel_label="channel ops", model_label="ops"))
    tc.patch_team(ops={"profile": "team-ops", "channel_id": OPS_CH})
    add_channel(tc.state, OPS_CH, "hermes-ops", parent=fd.CAT_HERMES)
    return kind


def test_set_model_target_message_for_builtin_kinds_is_unchanged(tc):
    with pytest.raises(tm.ToolError, match="pakai nama anggota, all, shared, admin, atau lobby"):
        tc.ctl().call("set_model", target="siapa", model="td-normal")


def test_set_model_accepts_registered_kind_as_target(tc, ops):
    ctl = tc.ctl(answer=False)
    assert ctl.call("set_model", target="ops", model="td-normal").startswith("Tidak dijalankan")
    assert "Ganti model ops" in ctl.previews[0]
    with pytest.raises(tm.ToolError, match="pakai nama anggota, all, shared, admin, lobby, atau ops"):
        tc.ctl().call("set_model", target="opz", model="td-normal")


def test_set_model_rejects_registered_kind_that_is_not_a_model_target(tc, register_kind):
    register_kind(kit_profiles.ProfileKind("ops", ht.SOUL_SHARED, kit_profiles._shared_settings,
                                           needs_channel=True, reserved=True, model_label="ops"))
    tc.patch_team(ops={"profile": "team-ops", "channel_id": OPS_CH})
    with pytest.raises(tm.ToolError, match="tidak dikenal"):
        tc.ctl().call("set_model", target="ops", model="td-normal")


def test_registered_kind_channel_is_a_protected_team_channel(tc, ops):
    ctl = tc.ctl()
    srv = ctl.server(ctl.team())
    assert srv.team_channels[OPS_CH] == "channel ops tim"
    assert srv.team_channels[fd.CH_SHARED] == "channel bersama tim"
    assert srv.team_channels[fd.CH_ADMIN] == "channel admin tim"
    assert srv.channel_kind(srv.channel(OPS_CH)) == "channel ops tim"


def test_registered_kind_channel_refuses_rename_and_permission_removal(tc, ops):
    ctl = tc.ctl()
    with pytest.raises(tm.ToolError, match="channel ops tim"):
        ctl.call("discord_edit_channel", channel=OPS_CH, name="lain")
    with pytest.raises(tm.ToolError, match="tidak bisa dihapus lewat Hermes"):
        ctl.call("discord_set_channel_permissions", channel=OPS_CH, target="@everyone", remove=True)
    assert ctl.previews == []


def test_admin_role_kind_channel_blocks_assign_role_like_the_builtin_admin_channel(tc, register_kind):
    """Penjaga channel admin mengikuti `role="admin"` di registri: role yang membuka channel jenis itu tidak bisa diberikan."""
    from teamctl_helpers import overwrite, mutating
    from teamctl.discord_guard import VIEW
    register_kind(kit_profiles.ProfileKind(
        "oncall", ht.SOUL_SHARED, kit_profiles._shared_settings, needs_channel=True, reserved=True,
        model_target=True, channel_label="channel oncall", role="admin"))
    tc.patch_team(oncall={"profile": "team-oncall", "channel_id": OPS_CH})
    add_channel(tc.state, OPS_CH, "hermes-oncall", parent=fd.CAT_HERMES,
                overwrites=[overwrite(fd.GUILD, deny=VIEW), overwrite(fd.ROLE_DESIGN, allow=VIEW)])
    ctl = tc.ctl()
    before = len(mutating(tc.state))
    with pytest.raises((tm.ToolError, ht.TeamError), match="channel oncall tim"):
        ctl.call("discord_assign_role", user="citra", role="designer")
    assert ctl.previews == [] and len(mutating(tc.state)) == before


# ---- jenis terdaftar yang bloknya TIDAK ada di team.yaml (jenis opsional yang tidak dipakai tim ini)
def _absent_kind(register_kind, **kw):
    return register_kind(kit_profiles.ProfileKind(
        "ops", ht.SOUL_SHARED, kit_profiles._shared_settings, needs_channel=True, reserved=True,
        model_target=True, channel_label="channel ops", model_label="ops", **kw))


def test_registered_kind_absent_from_team_does_not_break_server_guard(tc, register_kind):
    ctl = tc.ctl()
    team = ctl.team()                      # dimuat SEBELUM jenis didaftarkan → dict tim tanpa kunci `ops`
    _absent_kind(register_kind, role="admin")
    assert "ops" in kit_profiles.SYSTEM_KINDS and "ops" not in team
    srv = tm.Server(team, ctl.api())       # dulu: KeyError 'ops'
    assert set(srv.team_channels) == {fd.CH_SHARED, fd.CH_ADMIN} and "" not in srv.team_channels
    assert srv.admin_labels == {"channel admin tim"}
    for partial in ({}, {"profile": "team-ops"}, {"profile": "team-ops", "channel_id": ""}, None):
        srv = tm.Server({**team, "ops": partial}, ctl.api())
        assert set(srv.team_channels) == {fd.CH_SHARED, fd.CH_ADMIN} and srv.admin_labels == {"channel admin tim"}
    assert srv.channel_kind(srv.channel(fd.CH_ADMIN)) == "channel admin tim"   # pagar jenis yang ADA tetap utuh


def test_set_model_skips_registered_kind_absent_from_team(tc, register_kind, monkeypatch):
    ctl = tc.ctl()
    team = ctl.team()
    _absent_kind(register_kind)
    monkeypatch.setattr(ctl, "team", lambda: team)
    assert tm.tools_team._model_target_kinds(team) == ["shared", "admin", "lobby"]
    with pytest.raises(tm.ToolError, match="ops tidak dipakai tim ini.*shared, admin, atau lobby"):
        ctl.call("set_model", target="ops", model="td-normal")
    with pytest.raises(tm.ToolError, match="tidak dikenal; pakai nama anggota, all, shared, admin, atau lobby$"):
        ctl.call("set_model", target="opz", model="td-normal")
    assert ctl.previews == []
    present = {**team, "ops": {"profile": "team-ops", "channel_id": OPS_CH}}
    assert tm.tools_team._model_target_kinds(present) == ["shared", "admin", "lobby", "ops"]
