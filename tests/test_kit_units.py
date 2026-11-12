"""Uji unit hermes_team.py: helper murni + regresi untuk temuan audit (nomor di nama kelas/komentar).

Tanpa binary `hermes` asli dan tanpa jaringan — lihat conftest.py (hermes tiruan, Discord tiruan).
Jalankan:  python -m pytest tests/test_kit_units.py -q
"""
import base64
import io
import json
import os
import socket
import stat
import subprocess
import urllib.error

import pytest
import yaml

import fake_discord as fd
import hermes_team as ht
from conftest import BE1, BOT_TOKEN, BUDI, CITRA, DODI, EKA, HOST_ONLY_SECRET

VIEW = ht.PERM["VIEW_CHANNEL"]
BE1_UID, BE1_CH = BE1["discord_user_id"], BE1["channel_id"]
BUDI_UID, BUDI_CH = BUDI["discord_user_id"], BUDI["channel_id"]
CITRA_ROW = {"name": "citra", "display_name": "Citra", "discord_user_id": CITRA, "channel_id": "600000000000000003"}


# --------------------------------------------------------------------------- pembantu
def overwrites(discord, channel_id):
    return {str(o["id"]): (int(o["allow"]), int(o["deny"]), int(o["type"]))
            for o in discord.channels[channel_id]["permission_overwrites"]}


def private_set(owner):
    return {fd.GUILD: (0, VIEW, 0), owner: (ht.MEMBER_ALLOW, 0, 1), fd.BOT: (ht.BOT_ALLOW, 0, 1)}


def channels_named(discord, name):
    return [c for c in discord.channels.values() if c["name"] == name]


def channel_posts(discord):
    return discord.calls_matching("POST", f"/guilds/{fd.GUILD}/channels")


def cli(kit, *args):
    return [*args, "-c", str(kit.team_file), "--hermes-bin", str(kit.bin), "--hermes-root", str(kit.root),
            "--allow-root"]


def routes_for(kit, profile):
    return [r for r in kit.config("default")["gateway"]["profile_routes"] if r["profile"] == profile]


@pytest.fixture
def skills_kit(kit, monkeypatch, tmp_path):
    """Profile be1/budi/shared sudah ada (tanpa apply), pemindai Hermes tiruan menjawab "safe"."""
    for profile in ("be-be1", "be-budi", "team-shared"):
        kit.make_profile(profile)
    kit.fake_scanner(monkeypatch, tmp_path, "safe")
    return kit


# =========================================================================== helper murni
class TestIds:
    def test_plain_snowflake(self):
        assert ht._id("100000000000000001", "x") == "100000000000000001"

    def test_unquoted_yaml_int(self):
        assert ht._id(100000000000000001, "x") == "100000000000000001"

    @pytest.mark.parametrize("bad", [
        "１００００００００００００００００１",            # digit lebar penuh (\d cocok, [0-9] tidak) — #14
        "١٠٠٠٠٠٠٠٠٠٠٠٠٠٠٠٠١",            # digit Arab-Indic
        "12345", "abc", "", None, True, "1000000000000000010000000",
    ])
    def test_rejects_non_ascii_or_malformed(self, bad):
        with pytest.raises(ht.TeamError):
            ht._id(bad, "guild_id")

    def test_id_re_rejects_trailing_newline_and_unicode_digits(self):
        assert ht.ID_RE.match("100000000000000001")
        assert not ht.ID_RE.match("100000000000000001\n")
        assert not ht.ID_RE.match("१००००००००००००००००१")

    def test_error_names_the_field(self):
        with pytest.raises(ht.TeamError, match="roles.member"):
            ht._id("x", "roles.member")


class TestDeriveMemberName:
    @pytest.mark.parametrize("username,expected", [
        ("dodi.pratama", "dodi-pratama"), ("Budi Santoso", "budi-santoso"), ("EKA_99", "eka_99"),
        ("Çıtra", "ctra"), ("..--..", "user0005"), ("", "user0005"), ("日本語", "user0005"),
        ("a..b__c", "a-b-c"),
    ])
    def test_normalisation(self, username, expected):
        assert ht.derive_member_name(username, "500000000000000005", set()) == expected

    @pytest.mark.parametrize("reserved", ["admin", "shared", "all", "default", "template", "lobby"])
    def test_reserved_names_get_suffix(self, reserved):
        assert ht.derive_member_name(reserved, "1", set()) == f"{reserved}-2"

    def test_taken_names_get_next_free_suffix(self):
        assert ht.derive_member_name("budi", "1", {"budi", "budi-2"}) == "budi-3"

    def test_long_name_stays_valid(self):
        name = ht.derive_member_name("x" * 80, "1", {"x" * 30})
        assert ht.NAME_RE.match(name) and len(name) <= 30 and name.endswith("-2")

    def test_result_always_passes_check_new_name(self, kit):
        team = kit.team()
        for username in ("Dodi.Pratama", "admin", "  ", "ÀÉÎ", "a" * 99):
            assert ht._check_new_name(team, ht.derive_member_name(username, "500000000000000004", set()))


class TestCleanDisplayName:
    def test_strips_control_characters(self):
        assert ht.clean_display_name("Ci\x00tra\r\n\tX\x7f!\u2028Y") == "Ci tra X ! Y"

    def test_caps_at_64(self):
        assert len(ht.clean_display_name("n" * 500)) == 64

    def test_fallback_when_empty(self):
        assert ht.clean_display_name("\x00\n", "Citra") == "Citra"
        assert ht.clean_display_name(None, "x") == "x"


class TestEnvText:
    def test_bare_quoted_export_and_comments(self):
        env = ht.parse_env_text('# c\nA=1\nexport B="two words"\nC=\'single # not comment\'\nD=bare # comment\n\nbroken\n')
        assert env == {"A": "1", "B": "two words", "C": "single # not comment", "D": "bare"}

    def test_double_quote_escapes(self):
        assert ht.parse_env_text(r'K="a\"b\\c"')["K"] == 'a"b\\c'

    def test_multiline_double_quoted_value(self):          # #9
        pem = "-----BEGIN KEY-----\nabc123\ndef456\n-----END KEY-----"
        env = ht.parse_env_text(f'BEFORE=1\nPEM="{pem}"\nAFTER=2\n')
        assert env == {"BEFORE": "1", "PEM": pem, "AFTER": "2"}

    def test_multiline_single_quoted_value(self):
        assert ht.parse_env_text("K='baris1\nbaris2'\nZ=9")["K"] == "baris1\nbaris2"

    def test_multiline_value_lines_are_not_parsed_as_keys(self):
        env = ht.parse_env_text('PEM="x\nFAKE=1\ny"\n')
        assert list(env) == ["PEM"]

    def test_unterminated_quote_does_not_swallow_rest_of_file(self):
        env = ht.parse_env_text('A="tidak ditutup\nB=2\n')
        assert env["B"] == "2" and env["A"] == '"tidak ditutup'

    def test_trailing_comment_after_quoted_value(self):
        assert ht.parse_env_text('A="v" # catatan')["A"] == "v"

    def test_rewrite_replaces_in_place_and_keeps_others(self):
        out = ht.rewrite_env_text("A=1\nB=2\nC=3\n", "B", "baru")
        assert out == 'A=1\nB="baru"\nC=3\n'

    def test_rewrite_appends_and_removes(self):
        assert ht.rewrite_env_text("A=1\n", "B", "x") == 'A=1\nB="x"\n'
        assert ht.rewrite_env_text("A=1\nB=2\n", "B", None) == "A=1\n"
        assert ht.rewrite_env_text("", "B", None) == ""

    @pytest.mark.parametrize("value", ['pa"ss', "back\\slash", "sp ace # hash", "multi\nline", "'kutip'"])
    def test_rewrite_roundtrip(self, value):
        assert ht.parse_env_text(ht.rewrite_env_text("X=1\n", "K", value)) == {"X": "1", "K": value}

    def test_rewrite_drops_whole_multiline_entry(self):
        text = 'A=1\nPEM="l1\nl2\nl3"\nZ=9\n'
        assert ht.parse_env_text(ht.rewrite_env_text(text, "PEM", "satu")) == {"A": "1", "PEM": "satu", "Z": "9"}
        assert ht.rewrite_env_text(text, "PEM", None) == "A=1\nZ=9\n"

    def test_rewrite_collapses_duplicates(self):
        assert ht.rewrite_env_text("K=1\nK=2\n", "K", "3") == 'K="3"\n'


class TestSecretVariants:
    def test_contains_encoded_forms(self):
        value = "s3cret/Val+ue"
        variants = ht.secret_variants(value)
        raw = value.encode()
        assert {value, base64.b64encode(raw).decode(), raw.hex(), value[::-1], value.upper(), value.lower(),
                "s3cret%2FVal%2Bue"} <= variants

    def test_short_variants_dropped(self):
        assert all(len(v) >= 6 for v in ht.secret_variants("abc"))
        assert "abc" not in ht.secret_variants("abc")


class TestPermissions:
    def test_bits_match_discord_table(self):
        assert ht.PERM["VIEW_CHANNEL"] == 1 << 10 and ht.PERM["SEND_MESSAGES"] == 1 << 11
        assert ht.PERM["ADMINISTRATOR"] == 8 and ht.PERM["MANAGE_ROLES"] == 1 << 28

    def test_permission_bits_names_aliases_and_spacing(self):
        assert ht.permission_bits(["view_channel", "SEND_MESSAGES", "pin messages"]) == \
            (1 << 10) | (1 << 11) | (1 << 51)
        assert ht.permission_bits(["read"]) == 1 << 10
        assert ht.permission_bits(["Manage-Server"]) == 1 << 5
        assert ht.permission_bits([]) == 0 and ht.permission_bits(None) == 0

    def test_unknown_permission_raises(self):
        with pytest.raises(ht.TeamError, match="tidak dikenal"):
            ht.permission_bits(["fly"])

    def test_names_roundtrip(self):
        names = ["view_channel", "attach_files", "moderate_members"]
        assert ht.permission_names(ht.permission_bits(names)) == sorted(names, key=lambda n: ht.PERMISSIONS[n])

    def test_member_allow_has_no_elevated_bits(self):
        assert ht.MEMBER_ALLOW & ht.ELEVATED_MASK == 0
        assert ht.MEMBER_ALLOW & VIEW and ht.MEMBER_ALLOW & ht.PERM["USE_APPLICATION_COMMANDS"]

    def test_bot_allow_is_member_plus_manage_messages_and_threads(self):
        assert ht.BOT_ALLOW == ht.MEMBER_ALLOW | ht.PERM["MANAGE_MESSAGES"] | ht.PERM["MANAGE_THREADS"]
        assert ht.BOT_ALLOW & ht.PERM["ADMINISTRATOR"] == 0

    def test_elevated_mask(self):
        for name in ("administrator", "manage_roles", "kick_members", "mention_everyone"):
            assert ht.ELEVATED_MASK & (1 << ht.PERMISSIONS[name])
        assert ht.ELEVATED_MASK & ht.PERM["SEND_MESSAGES"] == 0

    def test_private_overwrites_shape(self):
        assert ht.private_overwrites("1", "2", "3") == [
            {"id": "1", "type": 0, "allow": "0", "deny": str(VIEW)},
            {"id": "2", "type": 1, "allow": str(ht.MEMBER_ALLOW), "deny": "0"},
            {"id": "3", "type": 1, "allow": str(ht.BOT_ALLOW), "deny": "0"}]


class TestRoutes:
    def test_non_strict_personal_routes_have_no_user_id(self, kit):
        routes = ht.build_routes(kit.team())
        personal = [r for r in routes if r["name"].startswith("personal-")]
        assert [r["chat_id"] for r in personal] == [BE1_CH, BUDI_CH]
        assert all(set(r) == {"name", "platform", "guild_id", "chat_id", "profile"} for r in personal)
        assert personal[0]["profile"] == "be-be1" and personal[0]["guild_id"] == fd.GUILD

    def test_strict_personal_routes_pin_owner(self, kit):
        kit.write_team(strict_personal_routes=True)
        personal = [r for r in ht.build_routes(kit.team()) if r["name"].startswith("personal-")]
        assert [(r["chat_id"], r["user_id"]) for r in personal] == [(BE1_CH, BE1_UID), (BUDI_CH, BUDI_UID)]

    def test_admin_strict_one_route_per_admin_user(self, kit):
        kit.write_team(admins=[fd.ADMIN, fd.OWNER])
        admin = [r for r in ht.build_routes(kit.team()) if r["profile"] == "team-admin"]
        assert [(r["name"], r["user_id"]) for r in admin] == [("team-admin-1", fd.ADMIN), ("team-admin-2", fd.OWNER)]
        assert all(r["chat_id"] == fd.CH_ADMIN for r in admin)

    def test_admin_non_strict_is_channel_route(self, kit):
        kit.write_team(strict_admin_routes=False)
        admin = [r for r in ht.build_routes(kit.team()) if r["profile"] == "team-admin"]
        assert len(admin) == 1 and "user_id" not in admin[0]

    def test_shared_route_never_pins_user(self, kit):
        kit.write_team(strict_personal_routes=True)
        shared = [r for r in ht.build_routes(kit.team()) if r["profile"] == "team-shared"]
        assert shared == [{"name": "team-shared", "platform": "discord", "guild_id": fd.GUILD,
                           "chat_id": fd.CH_SHARED, "profile": "team-shared"}]

    def test_inactive_members_not_routed_and_extra_routes_last(self, kit):
        extra = {"name": "t", "platform": "discord", "guild_id": fd.GUILD, "thread_id": "700000000000000001",
                 "profile": "be-be1"}
        kit.write_team(extra_routes=[extra])
        kit.write_members([BE1, {**BUDI, "active": False}])
        routes = ht.build_routes(kit.team())
        assert "personal-budi" not in [r["name"] for r in routes] and routes[-1] == extra

    def test_gateway_channels(self, kit):
        allowed, free = ht.gateway_channels(kit.team())
        assert allowed == [BE1_CH, BUDI_CH, fd.CH_SHARED, fd.CH_ADMIN]
        assert free == [BE1_CH, BUDI_CH, fd.CH_ADMIN]          # shared wajib @mention

    def test_model_alias(self, kit):
        kit.write_members([BE1, BUDI], models={"shared": "td-normal"})
        team = kit.team()
        assert ht.model_alias(team, "member", team["members"][0]) == "td-normal"
        assert ht.model_alias(team, "member", team["members"][1]) == "td-simple"
        assert ht.model_alias(team, "shared") == "td-normal" and ht.model_alias(team, "admin") == "td-normal"


class TestCliValueAndSame:
    def test_cli_value(self):
        assert ht.cli_value("a", True) == "true" and ht.cli_value("a", False) == "false"
        assert ht.cli_value("discord.allowed_channels", ["1", "2"]) == "1,2"
        assert ht.cli_value("other", ["1", "2"]) == '["1", "2"]'
        assert json.loads(ht.cli_value("p", {"a": {"b": 1}})) == {"a": {"b": 1}}
        assert ht.cli_value("n", 10) == "10"

    def test_same_csv_forms(self):
        key = "discord.allowed_channels"
        assert ht._same(key, "1, 2", ["1", "2"]) and ht._same(key, ["1", "2"], ["1", "2"])
        assert ht._same(key, None, []) and not ht._same(key, "1", ["1", "2"])
        assert ht._same(key, 400000000000000001, ["400000000000000001"])   # satu ID terbaca int

    def test_same_scalars_and_empties(self):
        assert ht._same("k", None, []) and ht._same("k", None, {}) and ht._same("k", None, "")
        assert ht._same("k", True, True) and not ht._same("k", None, False) and not ht._same("k", 1, 2)
        assert ht._same("k", {"a": [1]}, {"a": [1]})

    def test_get_nested(self):
        assert ht._get_nested({"a": {"b": 1}}, "a.b") == 1
        assert ht._get_nested({"a": 1}, "a.b") is None and ht._get_nested({}, "x") is None

    def test_nested_yaml_separates_env_keys(self, kit):
        text = ht.nested_yaml(ht.host_settings(kit.team()))
        body = yaml.safe_load(text)
        assert body["gateway"]["multiplex_profiles"] is True and "DISCORD_ALLOWED_ROLES" not in body
        assert f"#   DISCORD_ALLOWED_ROLES={fd.ROLE_MEMBER},{fd.ROLE_ADMIN}" in text


class TestManagedConfigAndRoster:
    def test_managed_policy(self, kit):
        kit.write_team(sandbox={"image": "team/img:1", "cpu": 2, "memory_mb": 4096, "network": "hermes-sandbox",
                                "http_proxy": "http://egress:3128"})
        policy = ht.managed_config(kit.team())
        term = policy["terminal"]
        assert term["backend"] == "docker" and term["docker_mount_cwd_to_workspace"] is False
        assert term["docker_volumes"] == [] and term["docker_extra_args"] == []
        assert (term["container_cpu"], term["container_memory"], term["docker_image"]) == (2, 4096, "team/img:1")
        assert term["docker_env"]["HTTPS_PROXY"] == "http://egress:3128" and term["docker_network"] == "hermes-sandbox"
        assert policy["security"] == {"redact_secrets": True}
        assert policy["agent"]["max_turns"] == 60 and policy["delegation"]["max_spawn_depth"] == 1
        assert policy["providers"]["td"]["key_env"] == "TD_API_KEY"
        assert set(policy["auxiliary"]) == set(ht.AUX_TASKS)
        assert policy["auxiliary"]["title_generation"]["model_upgrade_enabled"] is False

    def test_managed_yaml_is_valid_yaml_without_anchors(self, kit):
        text = ht.managed_yaml(kit.team())
        assert "&id" not in text and yaml.safe_load(text)["terminal"]["backend"] == "docker"

    def test_roster_structure_is_backward_compatible(self, kit):
        kit.write_members([BE1, {**BUDI, "active": False}])
        data = ht.roster_data(kit.team(), kit.root, str(kit.bin))
        assert {"version", "kit_version", "generated", "team_name", "guild_id", "hermes_root", "hermes_bin", "admins",
                "roles", "shared_channel", "admin_channel", "category_id", "channels", "users", "inactive_users",
                "reserved_env", "gateway", "live_reload", "restart_fallback", "onboarding", "kit",
                "settings"} <= set(data)
        assert data["version"] == 3
        assert data["channels"][BE1_CH] == {"profile": "be-be1", "kind": "personal", "owner": BE1_UID, "name": "be1"}
        assert data["users"][BE1_UID]["model"] == "td-normal" and BUDI_UID not in data["users"]
        assert data["inactive_users"] == {BUDI_UID: {"name": "budi", "channel_id": BUDI_CH}}
        assert data["reserved_env"] == ["TD_API_KEY"]
        assert set(data["gateway"]) == {"profile_routes", "allowed_channels", "free_response_channels"}

    def test_teamctl_public_names_still_exist(self):
        for name in ("BOT_ALLOW", "DiscordAPI", "DiscordHTTPError", "ELEVATED_MASK", "Hermes", "ID_RE",
                     "MEMBER_ALLOW", "NAME_RE", "PERM", "TeamError", "_check_new_name", "add_member",
                     "derive_member_name", "describe", "discord_token", "list_skills", "load_team", "model_alias",
                     "now_iso", "onboard_member", "permission_bits", "permission_names", "resolve_hermes_bin",
                     "scan_skill", "set_member_active", "set_model", "share_skill", "shared_skill_registry",
                     "stage_skill", "unshare_skill"):
            assert hasattr(ht, name), name


# =========================================================================== team.yaml (#10, #18)
class TestLoadTeam:
    @pytest.mark.parametrize("raw,expected", [("false", False), ("False", False), ("no", False), ("off", False),
                                              ("true", True), ("yes", True), (True, True), (0, False)])
    def test_string_booleans_are_parsed_not_truthy(self, kit, raw, expected):        # #18
        kit.write_team(strict_personal_routes=raw)
        assert kit.team()["strict"] is expected

    def test_member_active_string_false_means_inactive(self, kit):                     # #18 (≈L570)
        kit.write_members([BE1, {**BUDI, "active": "false"}])
        team = kit.team()
        assert team["members"][1]["active"] is False and [m["name"] for m in ht.active_members(team)] == ["be1"]

    def test_invalid_boolean_names_the_key(self, kit):
        kit.write_team(lobby_restrict="mungkin")
        with pytest.raises(ht.TeamError, match="lobby_restrict"):
            kit.team()

    def test_onboarding_and_server_admin_booleans(self, kit):
        kit.write_team(onboarding={"enabled": "false", "verify_actor": "true"}, server_admin={"enabled": "off"},
                       live_reload="false")
        team = kit.team()
        assert team["onboarding"]["enabled"] is False and team["onboarding"]["verify_actor"] is True
        assert team["server_admin"]["enabled"] is False and team["live_reload"] is False

    @pytest.mark.parametrize("override,key", [
        ({"sandbox": {"memory_mb": "banyak"}}, "sandbox.memory_mb"),
        ({"sandbox": {"cpu": "dua"}}, "sandbox.cpu"),
        ({"scale": {"agent_max_turns": "enam puluh"}}, "scale.agent_max_turns"),
        ({"scale": {"max_concurrent_sessions": None}}, "scale.max_concurrent_sessions"),
        ({"secrets": {"min_length": "x"}}, "secrets.min_length"),
        ({"onboarding": {"reconcile_minutes": "sepuluh"}}, "onboarding.reconcile_minutes"),
    ])
    def test_bad_numbers_raise_team_error_naming_key(self, kit, override, key):        # #18 (≈L480)
        kit.write_team(**override)
        with pytest.raises(ht.TeamError, match=key.replace(".", r"\.")):
            kit.team()

    def test_numeric_strings_are_accepted(self, kit):
        kit.write_team(sandbox={"memory_mb": "4096"}, scale={"agent_max_turns": "30"})
        team = kit.team()
        assert team["sandbox"]["memory_mb"] == 4096 and team["scale"]["agent_max_turns"] == 30

    @pytest.mark.parametrize("key", ["api_key", "API_KEY", "key", "token", "secret", "password", "Api-Key"])
    def test_provider_block_rejects_secret_looking_keys(self, kit, key):               # #10
        llm = json.loads(json.dumps(ht_llm()))
        llm["providers"]["td"][key] = "sk-live-jangan-di-sini-123456"
        kit.write_team(llm=llm)
        with pytest.raises(ht.TeamError) as err:
            kit.team()
        assert "/etc/hermes/.env" in str(err.value) and "sk-live" not in str(err.value)

    def test_provider_block_rejects_nested_secret_keys(self, kit):
        llm = ht_llm()
        llm["providers"]["td"]["headers"] = {"Authorization": "Bearer abcdef"}
        kit.write_team(llm=llm)
        with pytest.raises(ht.TeamError, match="headers.Authorization"):
            kit.team()

    def test_key_env_is_allowed(self, kit):
        assert kit.team()["llm"]["providers"]["td"]["key_env"] == "TD_API_KEY"

    def test_duplicate_channel_rejected(self, kit):
        kit.write_members([BE1, {**BUDI, "channel_id": fd.CH_SHARED}])
        with pytest.raises(ht.TeamError, match="dipakai dua kali"):
            kit.team()

    def test_duplicate_user_rejected(self, kit):
        kit.write_members([BE1, {**BUDI, "discord_user_id": BE1_UID}])
        with pytest.raises(ht.TeamError, match="dua anggota"):
            kit.team()

    def test_unknown_member_model_rejected(self, kit):
        kit.write_members([{**BE1, "model": "gpt-entah"}])
        with pytest.raises(ht.TeamError, match="gpt-entah"):
            kit.team()

    def test_admins_required(self, kit):
        kit.write_team(admins=[])
        with pytest.raises(ht.TeamError, match="admins"):
            kit.team()


def ht_llm():
    from conftest import LLM
    return json.loads(json.dumps(LLM))


# =========================================================================== pembungkus CLI hermes
class TestHermesWrapper:
    def test_second_apply_issues_zero_config_set(self, kit):
        team = kit.team()
        first = ht.cmd_apply(team, kit.hermes())
        assert first["failures"] == [] and len(kit.config_sets()) > 20
        assert set(first["created"]) == {"be-template", "be-be1", "be-budi"}
        kit.clear_log()
        second = ht.cmd_apply(team, kit.hermes())
        assert kit.calls() == []                               # tidak ada `config set`, create, atau plugins enable
        assert second == {"created": [], "restart_needed": False, "live_changed": [], "failures": []}

    def test_apply_writes_expected_host_config(self, kit):
        ht.cmd_apply(kit.team(), kit.hermes())
        cfg = kit.config("default")
        assert cfg["gateway"]["multiplex_profiles"] is True
        assert [r["profile"] for r in cfg["gateway"]["profile_routes"]] == ["be-be1", "be-budi", "team-shared", "team-admin"]
        env = ht.parse_env_text((kit.root / ".env").read_text())
        assert env["DISCORD_ALLOWED_ROLES"] == f"{fd.ROLE_MEMBER},{fd.ROLE_ADMIN}" and env["DISCORD_BOT_TOKEN"] == BOT_TOKEN
        assert kit.config("be-be1")["model"]["default"] == "td-normal"
        assert kit.config("be-budi")["model"]["default"] == "td-simple"
        assert "teamctl" in kit.config("team-admin")["mcp_servers"]
        assert "mcp_servers" not in kit.config("be-be1")
        assert "team-guard" in kit.config("be-be1")["plugins"]["enabled"]
        assert (kit.home("be-template") / "gateway.parked").exists()

    def test_provider_block_on_argv_never_contains_key_material(self, kit):
        ht.cmd_apply(kit.team(), kit.hermes())
        provider_sets = [v for _p, k, v in kit.config_sets() if k == "providers.td"]
        assert provider_sets and all(json.loads(v) == {"api": "https://llm.example.internal/v1", "key_env": "TD_API_KEY"}
                                     for v in provider_sets)

    def test_failed_config_set_is_not_recorded_as_changed(self, kit, monkeypatch):     # #19
        monkeypatch.setenv("FAKE_HERMES_FAIL_KEYS", "gateway.profile_routes")
        hermes = kit.hermes()
        changed = hermes.config_set("default", [("gateway.profile_routes", [{"a": 1}]), ("group_sessions_per_user", True)])
        assert changed == 1
        assert hermes.changed_keys == {"default": {"group_sessions_per_user"}}
        assert len(hermes.failures) == 1 and "gateway.profile_routes" in hermes.failures[0]

    def test_failed_route_update_does_not_claim_live_change(self, kit, monkeypatch):   # #19, lewat apply
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        kit.write_members([BE1])                                # budi hilang → rute berubah
        monkeypatch.setenv("FAKE_HERMES_FAIL_KEYS", "gateway.profile_routes")
        result = ht.cmd_apply(team, kit.hermes(), host_only=True)
        assert "gateway.profile_routes" not in result["live_changed"] and result["failures"]

    def test_failed_plugin_enable_is_not_recorded(self, kit, monkeypatch):
        monkeypatch.setenv("FAKE_HERMES_FAIL_KEYS", "plugins.enabled")
        hermes = kit.hermes()
        hermes.install_plugin("default")
        assert "plugins.enabled" not in hermes.changed_keys.get("default", set()) and hermes.failures

    def test_dry_run_runs_nothing(self, kit):
        hermes = kit.hermes(dry_run=True)
        ht.cmd_apply(kit.team(), hermes)
        assert kit.calls() == [] and not (kit.root / "profiles").exists() and not kit.state.exists()
        assert any("profile create be-be1" in line for line in hermes.log)

    def test_write_file_is_atomic(self, kit, monkeypatch):                             # #19
        target = kit.root / "SOUL.md"
        target.write_text("lama", encoding="utf-8")

        def boom(*_a, **_k):
            raise OSError("disk penuh")
        monkeypatch.setattr(ht.os, "replace", boom)
        with pytest.raises(OSError):
            kit.hermes().write_file(target, "baru", force=True)
        assert target.read_text(encoding="utf-8") == "lama"               # tidak pernah setengah tertulis
        assert [p.name for p in kit.root.iterdir() if p.name.startswith(".SOUL.md.")] == []

    def test_write_file_keeps_mode_and_backs_up(self, kit):
        target = kit.root / "SOUL.md"
        target.write_text("lama", encoding="utf-8")
        target.chmod(0o600)
        kit.hermes().write_file(target, "baru", force=True)
        assert target.read_text(encoding="utf-8") == "baru" and stat.S_IMODE(target.stat().st_mode) == 0o600
        assert (kit.root / "SOUL.md.bak").read_text(encoding="utf-8") == "lama"

    def test_write_file_new_file_and_no_force(self, kit):
        target = kit.root / "sub" / "NEW.md"
        hermes = kit.hermes()
        hermes.write_file(target, "isi", force=False)
        assert target.read_text() == "isi" and stat.S_IMODE(target.stat().st_mode) == 0o644
        hermes.write_file(target, "lain", force=False)
        assert target.read_text() == "isi"                                # tanpa force tidak ditimpa

    def test_restart_needed_rules(self, kit):
        hermes = kit.hermes()
        hermes.changed_keys = {"default": {"gateway.profile_routes", "discord.allowed_channels"}}
        assert ht.restart_needed(hermes, [], live_reload=True) is False
        assert ht.restart_needed(hermes, [], live_reload=False) is True
        hermes.changed_keys = {"default": {"DISCORD_ALLOWED_ROLES"}}
        assert ht.restart_needed(hermes, [], live_reload=True) is True
        hermes.changed_keys = {"be-x": {"plugins.enabled", "model.default"}}
        assert ht.restart_needed(hermes, ["be-x"], live_reload=True) is False
        assert ht.restart_needed(hermes, ["be-x"], live_reload=False) is True


class TestCloneHygiene:
    """#5 — upstream `profile create --clone-from` menyalin .env; profile baru tidak boleh mewarisinya."""

    def test_new_profiles_do_not_inherit_env_secrets(self, kit):
        ht.cmd_apply(kit.team(), kit.hermes())
        for profile in ("be-template", "be-be1", "be-budi", "team-shared", "team-admin"):
            path = kit.home(profile) / ".env"
            text = path.read_text(encoding="utf-8")
            assert HOST_ONLY_SECRET not in text and BOT_TOKEN not in text, profile
            assert ht.parse_env_text(text) == {} and stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_host_env_is_untouched(self, kit):
        ht.cmd_apply(kit.team(), kit.hermes())
        env = ht.parse_env_text((kit.root / ".env").read_text())
        assert env["HOST_ONLY_SECRET"] == HOST_ONLY_SECRET and env["DISCORD_BOT_TOKEN"] == BOT_TOKEN

    def test_template_secret_does_not_reach_members(self, kit):
        kit.make_profile("be-template", env="TEMPLATE_SECRET=bocor-dari-template-42\n")
        ht.cmd_apply(kit.team(), kit.hermes())
        assert "bocor-dari-template" not in (kit.home("be-be1") / ".env").read_text()
        # template yang sudah ada sebelum apply TIDAK disentuh
        assert "bocor-dari-template" in (kit.home("be-template") / ".env").read_text()

    def test_existing_member_env_is_never_scrubbed(self, kit):
        kit.make_profile("be-be1", env="GH_PAT=milik-be1-sendiri-777\n")
        ht.cmd_apply(kit.team(), kit.hermes())
        ht.cmd_apply(kit.team(), kit.hermes())
        assert ht.parse_env_text((kit.home("be-be1") / ".env").read_text()) == {"GH_PAT": "milik-be1-sendiri-777"}
        assert "be-be1" not in kit.creates()

    def test_cloned_memory_is_wiped(self, kit):
        ht.cmd_apply(kit.team(), kit.hermes())
        assert not (kit.home("be-be1") / "memories" / "MEMORY.md").exists()
        assert (kit.root / "memories" / "MEMORY.md").exists()

    def test_soul_is_personalised(self, kit):
        ht.cmd_apply(kit.team(), kit.hermes())
        assert "asisten pribadi Budi" in (kit.home("be-budi") / "SOUL.md").read_text(encoding="utf-8")
        assert "lobby" in (kit.root / "SOUL.md").read_text(encoding="utf-8")


class TestApply:
    def test_unknown_only_name_is_an_error(self, kit):                                 # #15
        with pytest.raises(ht.TeamError, match="bedu"):
            ht.cmd_apply(kit.team(), kit.hermes(), only=["bedu"])
        assert kit.calls() == []

    def test_inactive_only_name_is_an_error(self, kit):
        kit.write_members([BE1, {**BUDI, "active": False}])
        with pytest.raises(ht.TeamError, match="nonaktif"):
            ht.cmd_apply(kit.team(), kit.hermes(), only=["budi"])

    def test_only_limits_member_profiles(self, kit):
        ht.cmd_apply(kit.team(), kit.hermes(), only=["budi"])
        assert kit.creates() == ["be-template", "be-budi"]

    def test_apply_rereads_team_inside_lock(self, kit):                                # #4
        stale = kit.team()                                         # snapshot sebelum proses lain menulis
        kit.write_members([BE1, BUDI, CITRA_ROW])
        result = ht.cmd_apply(stale, kit.hermes())
        assert "be-citra" in result["created"] and [m["name"] for m in stale["members"]] == ["be1", "budi", "citra"]
        assert any(r["profile"] == "be-citra" for r in kit.config("default")["gateway"]["profile_routes"])

    def test_nested_apply_does_not_reread(self, kit):
        team = kit.team()
        with ht.team_lock(team):
            kit.write_members([BE1])                                # perubahan di disk diabaikan saat bersarang
            ht.cmd_apply(team, kit.hermes(), only=["budi"])
        assert "be-budi" in kit.creates()

    def test_team_lock_is_reentrant_and_tracks_depth(self, kit):
        team = kit.team()
        assert ht._lock_depth() == 0
        with ht.team_lock(team):
            assert ht._lock_depth() == 1
            with ht.team_lock(team):
                assert ht._lock_depth() == 2
            assert ht._lock_depth() == 1
        assert ht._lock_depth() == 0

    def test_roster_not_written_when_profile_creation_fails(self, kit, monkeypatch):   # #3 (roster terakhir)
        monkeypatch.setenv("FAKE_HERMES_FAIL_CREATE", "be-budi")
        with pytest.raises(ht.TeamError, match="disk penuh"):
            ht.cmd_apply(kit.team(), kit.hermes())
        assert not (kit.state / "roster.json").exists()
        assert "gateway" not in kit.config("default")              # rute belum menunjuk ke profile yang tidak ada

    def test_roster_written_after_profiles_exist(self, kit, monkeypatch):
        seen = {}
        real = ht.write_roster

        def spy(team, hermes):
            seen["profiles"] = sorted(p.name for p in (kit.root / "profiles").iterdir())
            seen["routes"] = "gateway" in kit.config("default")
            return real(team, hermes)
        monkeypatch.setattr(ht, "write_roster", spy)
        ht.cmd_apply(kit.team(), kit.hermes())
        assert {"be-be1", "be-budi", "team-admin", "team-shared"} <= set(seen["profiles"]) and seen["routes"]
        assert kit.roster()["users"][BE1_UID]["profile"] == "be-be1"

    def test_set_model_updates_members_yaml_and_profile(self, kit):
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        result = ht.set_model(team, kit.hermes(), "budi", "td-normal")
        assert result["profiles"] == ["be-budi"] and kit.config("be-budi")["model"]["default"] == "td-normal"
        assert [m.get("model") for m in kit.members_yaml()] == ["td-normal", "td-normal"]
        with pytest.raises(ht.TeamError, match="katalog"):
            ht.set_model(team, kit.hermes(), "budi", "tidak-ada")


# =========================================================================== klien REST Discord (#6, #16)
class TestDiscordClient:
    def test_sends_bot_auth_and_audit_reason(self, api, discord):
        api.add_role(fd.GUILD, CITRA, fd.ROLE_MEMBER, reason="uji alasan")
        call = discord.calls[-1]
        assert call["auth"] == f"Bot {BOT_TOKEN}" and call["reason"] == "uji alasan"
        assert fd.ROLE_MEMBER in discord.members[CITRA]["roles"]

    def test_429_honours_retry_after_header(self, api, discord):
        discord.fault("GET", "/users/@me", status=429, body={"message": "rate limited"}, headers={"Retry-After": "7"})
        assert api.bot_user_id() == fd.BOT
        assert len(api.naps) == 1 and 7.0 <= api.naps[0] <= 8.0

    def test_429_honours_json_retry_after(self, api, discord):
        discord.fault("GET", "/users/@me", status=429, body={"message": "x", "retry_after": 3.5})
        api.bot_user_id()
        assert 3.5 <= api.naps[0] <= 4.0

    def test_429_uses_the_longer_of_header_and_body(self, api, discord):
        discord.fault("GET", "/users/@me", status=429, body={"retry_after": 0.25}, headers={"Retry-After": "12"})
        api.bot_user_id()
        assert api.naps[0] >= 12.0

    def test_429_wait_is_capped_per_attempt(self, api, discord):
        discord.fault("GET", "/users/@me", status=429, body={"retry_after": 86400}, headers={"Retry-After": "86400"})
        api.bot_user_id()
        assert api.naps == [60.0]

    def test_persistent_429_gives_up_with_http_error(self, api, discord):
        discord.fault("GET", "/users/@me", status=429, body={"retry_after": 1}, times=10)
        with pytest.raises(ht.DiscordHTTPError) as err:
            api.bot_user_id()
        assert err.value.status == 429 and len(api.naps) == api.max_attempts - 1

    @pytest.mark.parametrize("status", [502, 503, 504])
    def test_gateway_errors_are_retried_with_backoff(self, api, discord, status):
        discord.fault("GET", f"/guilds/{fd.GUILD}/roles", status=status, times=2)
        assert len(api.guild_roles(fd.GUILD)) == len(discord.roles)
        assert api.naps == [0.5, 1.0]

    def test_persistent_5xx_raises_http_error(self, api, discord):
        discord.fault("GET", "/users/@me", status=503, times=10)
        with pytest.raises(ht.DiscordHTTPError) as err:
            api.bot_user_id()
        assert err.value.status == 503

    def test_500_and_403_are_not_retried(self, api, discord):
        discord.fault("GET", "/users/@me", status=500)
        with pytest.raises(ht.DiscordHTTPError):
            api.bot_user_id()
        discord.forbid.add(("GET", f"/guilds/{fd.GUILD}/roles"))
        with pytest.raises(ht.DiscordHTTPError) as err:
            api.guild_roles(fd.GUILD)
        assert err.value.status == 403 and err.value.code == 50013 and api.naps == []

    def test_invalid_json_body_is_a_discord_error(self, api, discord):
        discord.fault("GET", "/users/@me", status=200, raw=b"<html>Cloudflare</html>")
        with pytest.raises(ht.DiscordHTTPError, match="JSON"):
            api.bot_user_id()

    def test_non_json_error_body_still_gives_http_error(self, api, discord):
        discord.fault("GET", "/users/@me", status=400, raw=b"bukan json")
        with pytest.raises(ht.DiscordHTTPError) as err:
            api.bot_user_id()
        assert err.value.status == 400

    def test_unreachable_host_raises_team_error_without_token(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()                                                # port tertutup → connection refused
        naps = []
        client = ht.DiscordAPI(BOT_TOKEN, base=f"http://127.0.0.1:{port}/api/v10", timeout=2, sleep=naps.append)
        with pytest.raises(ht.TeamError) as err:
            client.bot_user_id()
        assert "tidak terjangkau" in str(err.value) and BOT_TOKEN not in str(err.value)
        assert len(naps) == client.max_attempts - 1                 # GET idempoten → diulang
        naps.clear()
        with pytest.raises(ht.TeamError):
            client.create_channel(fd.GUILD, {"name": "x"}, reason="uji")
        assert naps == []                                           # POST tidak diulang (bisa dobel)

    @pytest.mark.parametrize("exc", [TimeoutError("timed out"), ConnectionResetError("reset by peer"),
                                     OSError("jaringan mati"), urllib.error.URLError("dns gagal"),
                                     __import__("http.client").client.IncompleteRead(b"")])
    def test_low_level_errors_become_team_error(self, monkeypatch, exc):
        def boom(*_a, **_k):
            raise exc
        monkeypatch.setattr(ht.urllib.request, "urlopen", boom)
        client = ht.DiscordAPI(BOT_TOKEN, base="http://127.0.0.1:1/api", sleep=lambda _s: None)
        with pytest.raises(ht.TeamError) as err:
            client.guild_member(fd.GUILD, CITRA)
        assert not isinstance(err.value, ht.DiscordHTTPError) and BOT_TOKEN not in str(err.value)

    def test_token_is_scrubbed_from_error_text(self, api, discord, monkeypatch):
        discord.fault("GET", "/users/@me", status=401, body={"message": f"invalid token {BOT_TOKEN}", "code": 0})
        with pytest.raises(ht.DiscordHTTPError) as err:
            api.bot_user_id()
        assert BOT_TOKEN not in str(err.value) and "401" in str(err.value)

        def boom(*_a, **_k):
            raise OSError(f"proxy menolak header Bot {BOT_TOKEN}")
        monkeypatch.setattr(ht.urllib.request, "urlopen", boom)
        with pytest.raises(ht.TeamError) as err:
            api.guild(fd.GUILD)
        assert BOT_TOKEN not in str(err.value)

    def test_get_channel_returns_none_when_deleted(self, api):
        assert api.get_channel("699999999999999999") is None
        assert api.get_channel(fd.CH_SHARED)["name"] == "hermes-shared"

    def test_list_members_paginates_past_1000(self, api, discord):                     # #16
        for i in range(1500):
            discord.add_member(510000000000000000 + i, f"user{i}")
        members = api.list_members(fd.GUILD)
        assert len(members) == 1509 and len({m["user"]["id"] for m in members}) == 1509
        assert members.truncated is False
        assert len(discord.calls_matching("GET", f"/guilds/{fd.GUILD}/members")) == 2

    def test_list_members_limit_exposes_truncation(self, api, discord):                # #16
        for i in range(1500):
            discord.add_member(510000000000000000 + i, f"user{i}")
        capped = api.list_members(fd.GUILD, limit=1000)              # cara teamctl memanggilnya
        assert len(capped) == 1000 and capped.truncated is True and isinstance(capped, list)

    def test_list_members_small_limit_and_exact_fit(self, api, discord):
        few = api.list_members(fd.GUILD, limit=5)
        assert len(few) == 5 and few.truncated is True
        exact = api.list_members(fd.GUILD, limit=len(discord.members))
        assert len(exact) == len(discord.members) and exact.truncated is False

    def test_discord_token_from_host_env_file(self, kit, monkeypatch):
        assert ht.discord_token(kit.root) == BOT_TOKEN
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "dari-env")
        assert ht.discord_token(kit.root) == "dari-env"
        monkeypatch.delenv("DISCORD_BOT_TOKEN")
        (kit.root / ".env").write_text("X=1\n")
        with pytest.raises(ht.TeamError, match="DISCORD_BOT_TOKEN"):
            ht.discord_token(kit.root)


# =========================================================================== onboarding / anggota
class TestOnboardNew:
    def test_creates_private_channel_profile_route_and_roster(self, kit, discord):
        events = []
        team = kit.team()
        result = ht.onboard_member(team, kit.hermes(), user_id=DODI, username="dodi.pratama",
                                   display_name="Dodi Pratama", emit=lambda e, **f: events.append((e, f)))
        member = result["member"]
        assert result["status"] == "created" and result["failures"] == []
        assert (member["name"], member["profile"], member["display_name"]) == ("dodi-pratama", "be-dodi-pratama", "Dodi Pratama")
        channel = discord.channels[member["channel_id"]]
        assert channel["name"] == "hermes-dodi-pratama" and channel["parent_id"] == fd.CAT_HERMES
        assert overwrites(discord, member["channel_id"]) == private_set(DODI)
        assert events == [("channel", {"channel_id": member["channel_id"], "created": True})]
        assert kit.members_yaml()[-1] == {"name": "dodi-pratama", "display_name": "Dodi Pratama",
                                          "discord_user_id": DODI, "channel_id": member["channel_id"]}
        assert routes_for(kit, "be-dodi-pratama")[0]["chat_id"] == member["channel_id"]
        assert kit.roster()["users"][DODI]["channel_id"] == member["channel_id"]
        assert member["channel_id"] in ht.parse_env_text((kit.root / ".env").read_text())["DISCORD_HOME_CHANNEL"] \
            or member["channel_id"] in kit.config("default")["discord"]["allowed_channels"]
        assert ht.parse_env_text((kit.home("be-dodi-pratama") / ".env").read_text()) == {}

    def test_second_call_is_idempotent_exists(self, kit, discord):
        team = kit.team()
        first = ht.onboard_member(team, kit.hermes(), user_id=DODI, username="dodi.pratama")
        kit.clear_log()
        before = len(discord.calls_matching("PUT")) + len(channel_posts(discord))
        again = ht.onboard_member(team, kit.hermes(), user_id=DODI, username="dodi.pratama")
        assert again["status"] == "exists" and again["member"]["channel_id"] == first["member"]["channel_id"]
        assert again["failures"] == [] and kit.calls() == []
        assert len(discord.calls_matching("PUT")) + len(channel_posts(discord)) == before
        assert len(channels_named(discord, "hermes-dodi-pratama")) == 1

    def test_dry_run_touches_nothing(self, kit, discord):
        before = kit.members_file.read_text()
        result = ht.onboard_member(kit.team(), kit.hermes(dry_run=True), user_id=DODI, username="dodi.pratama")
        assert result["status"] == "created" and result["member"]["channel_id"] == "9" * 18
        assert discord.calls == [] and kit.calls() == [] and kit.members_file.read_text() == before


class TestLeftoverProfiles:
    """#1 — anggota BARU tidak boleh diam-diam mewarisi .env/memory/skill profile lama."""

    def test_derived_name_avoids_leftover_profile_dir(self, kit, discord):
        old = kit.make_profile("be-dodi-pratama", env="GH_PAT=milik-dodi-lama-123456\n", memory="rahasia lama\n")
        result = ht.onboard_member(kit.team(), kit.hermes(), user_id=DODI, username="dodi.pratama")
        member = result["member"]
        assert member["name"] == "dodi-pratama-2" and member["profile"] == "be-dodi-pratama-2"
        new_home = kit.home("be-dodi-pratama-2")
        assert "milik-dodi-lama" not in (new_home / ".env").read_text()
        assert not (new_home / "memories" / "MEMORY.md").exists()
        assert "milik-dodi-lama" in (old / ".env").read_text()           # profile lama tidak disentuh
        assert (old / "memories" / "MEMORY.md").read_text() == "rahasia lama\n"

    def test_explicit_name_on_leftover_profile_is_refused_before_any_side_effect(self, kit, discord):
        kit.make_profile("be-dodi", env="GH_PAT=milik-dodi-lama-123456\n")
        before = kit.members_file.read_text()
        with pytest.raises(ht.TeamError) as err:
            ht.onboard_member(kit.team(), kit.hermes(), user_id=DODI, username="dodi.pratama", name="dodi")
        assert "be-dodi" in str(err.value) and "hermes profile delete" in str(err.value)
        assert channel_posts(discord) == [] and kit.calls() == [] and kit.members_file.read_text() == before

    def test_add_member_on_leftover_profile_is_refused(self, kit, discord):
        kit.make_profile("be-citra", env="X=1\n")
        with pytest.raises(ht.TeamError, match="be-citra"):
            ht.add_member(kit.team(), kit.hermes(), name="citra", user_id=CITRA)
        assert channel_posts(discord) == [] and len(kit.members_yaml()) == 2

    def test_empty_leftover_directory_also_counts(self, kit, discord):
        (kit.root / "profiles" / "be-eka").mkdir(parents=True)
        assert ht.onboard_member(kit.team(), kit.hermes(), user_id=EKA, username="eka")["member"]["name"] == "eka-2"

    def test_existing_member_keeps_its_own_profile(self, kit, discord):
        home = kit.make_profile("be-budi", env="GH_PAT=milik-budi-999999\n")
        kit.write_members([BE1, {**BUDI, "active": False}])
        result = ht.onboard_member(kit.team(), kit.hermes(), user_id=BUDI_UID)
        assert result["status"] == "reactivated" and result["member"]["profile"] == "be-budi"
        assert "milik-budi" in (home / ".env").read_text()

    def test_derived_name_avoids_system_profiles(self, kit, discord):
        kit.write_team(template_profile="be-base")
        team = kit.team()
        assert "base" in ht._taken_names(team, kit.hermes())
        assert ht.onboard_member(team, kit.hermes(), user_id=EKA, username="base")["member"]["name"] == "base-2"

    def test_explicit_name_colliding_with_system_profile_refused(self, kit, discord):
        kit.write_team(template_profile="be-base")
        with pytest.raises(ht.TeamError, match="be-base"):
            ht.add_member(kit.team(), kit.hermes(), name="base", user_id=CITRA)
        assert channel_posts(discord) == []


class TestOnboardTransactional:
    """#3 — validasi sebelum channel, pakai-ulang channel, rollback, roster setelah profile."""

    @pytest.mark.parametrize("kwargs,match", [
        ({"user_id": DODI, "username": "dodi", "model": "gpt-entah"}, "katalog"),
        ({"user_id": "599999999999999999", "username": "hantu"}, "tidak ditemukan"),
        ({"user_id": DODI, "username": "dodi", "name": "budi"}, "sudah ada"),
        ({"user_id": DODI, "username": "dodi", "name": "admin"}, "dipakai sistem"),
        ({"user_id": DODI, "username": "dodi", "name": "Tidak Valid!"}, "tidak valid"),
    ])
    def test_validation_failures_create_no_channel(self, kit, discord, kwargs, match):
        before = kit.members_file.read_text()
        with pytest.raises(ht.TeamError, match=match):
            ht.onboard_member(kit.team(), kit.hermes(), **kwargs)
        assert channel_posts(discord) == [] and kit.calls() == [] and kit.members_file.read_text() == before

    @pytest.mark.parametrize("kwargs,match", [
        ({"name": "be1", "user_id": CITRA}, "sudah ada"),
        ({"name": "citra", "user_id": BE1_UID}, "sudah terdaftar"),
        ({"name": "citra", "user_id": CITRA, "model": "x"}, "katalog"),
        ({"name": "citra", "user_id": "599999999999999999"}, "tidak ditemukan"),
        ({"name": "citra", "user_id": CITRA, "create_channel": False}, "channel_id"),
    ])
    def test_add_member_validation_failures_create_no_channel(self, kit, discord, kwargs, match):
        with pytest.raises(ht.TeamError, match=match):
            ht.add_member(kit.team(), kit.hermes(), **kwargs)
        assert channel_posts(discord) == [] and kit.calls() == [] and len(kit.members_yaml()) == 2

    def test_retry_reuses_matching_channel_instead_of_duplicating(self, kit, discord):
        discord.add_channel("650000000000000001", "hermes-eka",
                            overwrites=ht.private_overwrites(fd.GUILD, EKA, fd.BOT))
        result = ht.onboard_member(kit.team(), kit.hermes(), user_id=EKA, username="eka")
        assert result["member"]["channel_id"] == "650000000000000001"
        assert channel_posts(discord) == [] and len(channels_named(discord, "hermes-eka")) == 1
        assert any("dipakai lagi" in note for note in result["notes"])

    def test_same_name_channel_of_someone_else_is_not_reused(self, kit, discord):
        discord.add_channel("650000000000000002", "hermes-eka",
                            overwrites=ht.private_overwrites(fd.GUILD, DODI, fd.BOT))
        result = ht.onboard_member(kit.team(), kit.hermes(), user_id=EKA, username="eka")
        assert result["member"]["channel_id"] != "650000000000000002" and len(channel_posts(discord)) == 1
        assert overwrites(discord, "650000000000000002") == private_set(DODI)        # tidak disentuh

    def test_same_name_channel_outside_category_is_not_reused(self, kit, discord):
        discord.add_channel("650000000000000003", "hermes-eka", parent=None,
                            overwrites=ht.private_overwrites(fd.GUILD, EKA, fd.BOT))
        result = ht.onboard_member(kit.team(), kit.hermes(), user_id=EKA, username="eka")
        assert result["member"]["channel_id"] != "650000000000000003"

    def test_failure_after_channel_creation_rolls_everything_back(self, kit, discord, monkeypatch):
        monkeypatch.setenv("FAKE_HERMES_FAIL_CREATE", "be-eka")
        with pytest.raises(ht.TeamError) as err:
            ht.onboard_member(kit.team(), kit.hermes(), user_id=EKA, username="eka")
        assert "disk penuh" in str(err.value) and "dibatalkan" in str(err.value)
        assert len(channel_posts(discord)) == 1 and channels_named(discord, "hermes-eka") == []   # dibuat lalu dihapus
        assert [m["name"] for m in kit.members_yaml()] == ["be1", "budi"]
        assert not kit.home("be-eka").exists()
        assert not (kit.state / "roster.json").exists()
        # percobaan ulang setelah penyebabnya hilang: nama sama, tepat satu channel, tidak ada sisa
        monkeypatch.delenv("FAKE_HERMES_FAIL_CREATE")
        retry = ht.onboard_member(kit.team(), kit.hermes(), user_id=EKA, username="eka")
        assert retry["status"] == "created" and retry["member"]["name"] == "eka"
        assert len(channels_named(discord, "hermes-eka")) == 1

    def test_add_member_failure_rolls_back_channel_and_entry(self, kit, discord, monkeypatch):
        monkeypatch.setenv("FAKE_HERMES_FAIL_CREATE", "be-citra")
        with pytest.raises(ht.TeamError, match="dibatalkan"):
            ht.add_member(kit.team(), kit.hermes(), name="citra", user_id=CITRA)
        assert channels_named(discord, "hermes-citra") == [] and len(kit.members_yaml()) == 2
        assert fd.ROLE_MEMBER not in discord.members[CITRA]["roles"]     # role tidak diberikan untuk anggota gagal

    def test_reused_channel_is_not_deleted_on_rollback(self, kit, discord, monkeypatch):
        discord.add_channel("650000000000000004", "hermes-eka",
                            overwrites=ht.private_overwrites(fd.GUILD, EKA, fd.BOT))
        monkeypatch.setenv("FAKE_HERMES_FAIL_CREATE", "be-eka")
        with pytest.raises(ht.TeamError):
            ht.onboard_member(kit.team(), kit.hermes(), user_id=EKA, username="eka")
        assert "650000000000000004" in discord.channels                  # bukan buatan panggilan ini

    def test_rollback_reports_channel_it_could_not_delete(self, kit, discord, monkeypatch):
        monkeypatch.setenv("FAKE_HERMES_FAIL_CREATE", "be-eka")
        discord.forbid.add(("DELETE", "/channels/"))
        with pytest.raises(ht.TeamError, match="TIDAK bisa dihapus"):
            ht.onboard_member(kit.team(), kit.hermes(), user_id=EKA, username="eka")
        assert [m["name"] for m in kit.members_yaml()] == ["be1", "budi"]

    def test_add_member_success_gives_role_and_sanitises_display(self, kit, discord):  # #13
        dirty = "Ci\x00tra\n\tDewi" + "x" * 200
        result = ht.add_member(kit.team(), kit.hermes(), name="citra", user_id=CITRA, display_name=dirty,
                               model="td-normal")
        display = result["member"]["display_name"]
        assert display == ht.clean_display_name(dirty) and len(display) == 64 and "\x00" not in display
        assert display.startswith("Ci tra Dewi")
        assert "\x00" not in (kit.home("be-citra") / "SOUL.md").read_text(encoding="utf-8")
        assert "\n" not in discord.channels[result["member"]["channel_id"]]["topic"]
        assert fd.ROLE_MEMBER in discord.members[CITRA]["roles"] and "role anggota diberikan" in result["notes"]
        assert kit.config("be-citra")["model"]["default"] == "td-normal"

    def test_onboard_and_add_member_sanitise_identically(self, kit, discord):          # #13
        dirty = "E\x07ka\u2028Putri\r\n"
        onboarded = ht.onboard_member(kit.team(), kit.hermes(), user_id=EKA, username="eka", display_name=dirty)
        added = ht.add_member(kit.team(), kit.hermes(), name="citra", user_id=CITRA, display_name=dirty)
        assert onboarded["member"]["display_name"] == added["member"]["display_name"] == "E ka Putri"

    def test_add_member_default_display_name(self, kit, discord):
        assert ht.add_member(kit.team(), kit.hermes(), name="citra", user_id=CITRA)["member"]["display_name"] == "Citra"


class TestOnboardExisting:
    """#2 dan bagian self-heal dari #3."""

    def test_replacement_channel_id_is_stored_and_reported(self, kit, discord):        # #2
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        del discord.channels[BE1_CH]                                 # admin menghapus channel lama
        events = []
        result = ht.onboard_member(team, kit.hermes(), user_id=BE1_UID, emit=lambda e, **f: events.append((e, f)))
        new_id = result["member"]["channel_id"]
        assert result["status"] == "exists" and new_id != BE1_CH and new_id in discord.channels
        assert kit.members_yaml()[0]["channel_id"] == new_id         # dulu: ID baru dibuang, members.yaml basi
        assert routes_for(kit, "be-be1")[0]["chat_id"] == new_id
        assert kit.roster()["users"][BE1_UID]["channel_id"] == new_id and BE1_CH not in kit.roster()["channels"]
        assert any(new_id in note for note in result["notes"])
        assert events == [("channel", {"channel_id": new_id, "created": True})]
        assert overwrites(discord, new_id) == private_set(BE1_UID)
        assert "gateway.profile_routes" in result["live_changed"]

    def test_discord_error_is_reported_not_swallowed(self, kit, discord):              # #2
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        discord.forbid.add(("GET", "/channels/"))
        result = ht.onboard_member(team, kit.hermes(), user_id=BE1_UID)
        assert result["status"] == "exists" and result["member"]["channel_id"] == BE1_CH
        assert result["failures"] and "403" in result["failures"][0]
        assert any("belum bisa dipastikan" in note for note in result["notes"])

    def test_missing_profile_is_self_healed(self, kit, discord):                       # #3
        team = kit.team()                                            # be1 ada di members.yaml, belum pernah apply
        result = ht.onboard_member(team, kit.hermes(), user_id=BE1_UID)
        assert result["status"] == "exists" and "be-be1" in result["created"]
        assert kit.home("be-be1").is_dir() and routes_for(kit, "be-be1")
        assert any("diterapkan ulang" in note for note in result["notes"])

    def test_stale_routes_are_self_healed(self, kit, discord):                         # #3
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        cfg = kit.config("default")
        cfg["gateway"]["profile_routes"] = [r for r in cfg["gateway"]["profile_routes"] if r["profile"] != "be-be1"]
        (kit.root / "config.yaml").write_text(json.dumps(cfg))
        result = ht.onboard_member(team, kit.hermes(), user_id=BE1_UID)
        assert routes_for(kit, "be-be1") and "gateway.profile_routes" in result["live_changed"]

    def test_stale_roster_is_self_healed(self, kit, discord):
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        (kit.state / "roster.json").unlink()
        ht.onboard_member(team, kit.hermes(), user_id=BE1_UID)
        assert kit.roster()["users"][BE1_UID]["profile"] == "be-be1"

    def test_healthy_member_causes_no_writes(self, kit, discord):
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        ht.onboard_member(team, kit.hermes(), user_id=BE1_UID)       # merapikan izin channel bawaan tiruan
        kit.clear_log()
        writes = len([c for c in discord.calls if c["method"] != "GET"])
        result = ht.onboard_member(team, kit.hermes(), user_id=BE1_UID)
        assert result == {"status": "exists", "member": result["member"], "notes": [], "created": [],
                          "restart_needed": False, "live_changed": [], "failures": []}
        assert kit.calls() == [] and len([c for c in discord.calls if c["method"] != "GET"]) == writes


class TestChannelPrivacy:
    """#12 — privasi channel dipaksakan saat dipakai ulang / --channel-id."""

    def test_reused_channel_gets_full_private_set_and_extras_removed(self, kit, discord):
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        discord.channels[BE1_CH]["permission_overwrites"] = [
            {"id": fd.GUILD, "type": 0, "allow": str(VIEW), "deny": "0"},            # @everyone BISA melihat
            {"id": fd.ROLE_DESIGN, "type": 0, "allow": str(VIEW), "deny": "0"},
            {"id": DODI, "type": 1, "allow": str(VIEW), "deny": "0"},
            {"id": BE1_UID, "type": 1, "allow": str(VIEW), "deny": "0"}]
        result = ht.onboard_member(team, kit.hermes(), user_id=BE1_UID)
        assert overwrites(discord, BE1_CH) == private_set(BE1_UID)
        note = next(n for n in result["notes"] if "dihapus supaya tetap privat" in n)
        assert f"role {fd.ROLE_DESIGN}" in note and f"user {DODI}" in note

    def test_reactivation_reasserts_privacy(self, kit, discord):
        kit.write_members([BE1, {**BUDI, "active": False}])
        discord.channels[BUDI_CH]["permission_overwrites"] = [
            {"id": fd.ROLE_MEMBER, "type": 0, "allow": str(VIEW), "deny": "0"}]
        result = ht.onboard_member(kit.team(), kit.hermes(), user_id=BUDI_UID)
        assert result["status"] == "reactivated" and overwrites(discord, BUDI_CH) == private_set(BUDI_UID)
        assert kit.members_yaml()[1].get("active", True) is True

    def test_set_member_active_on_reasserts_privacy(self, kit, discord):
        kit.write_members([BE1, {**BUDI, "active": False}])
        discord.channels[BUDI_CH]["permission_overwrites"] = [
            {"id": DODI, "type": 1, "allow": str(VIEW), "deny": "0"}]
        result = ht.set_member_active(kit.team(), kit.hermes(), "budi", True)
        assert overwrites(discord, BUDI_CH) == private_set(BUDI_UID)
        assert any(f"user {DODI}" in n for n in result["notes"])

    @pytest.mark.parametrize("channel_id,match", [
        (fd.CH_SHARED, "channel bersama"), (fd.CH_ADMIN, "channel admin"), (BUDI_CH, "anggota 'budi'"),
        (fd.CAT_HERMES, "kategori"), ("400000000000000007", "bukan channel teks"),
        ("690000000000000001", "bukan milik server"), ("699999999999999999", "tidak ditemukan"),
    ])
    def test_explicit_channel_id_is_verified(self, kit, discord, channel_id, match):
        discord.add_channel("690000000000000001", "asing", guild_id="111111111111111111")
        before = json.dumps(discord.channels, sort_keys=True)
        with pytest.raises(ht.TeamError, match=match):
            ht.add_member(kit.team(), kit.hermes(), name="citra", user_id=CITRA, channel_id=channel_id,
                          create_channel=False)
        assert len(kit.members_yaml()) == 2 and kit.calls() == []
        assert json.dumps(discord.channels, sort_keys=True) == before     # tidak ada izin yang diubah

    def test_explicit_thread_is_rejected(self, kit, discord):
        discord.add_channel("690000000000000002", "utas", ctype=11, parent=fd.CH_GENERAL)
        with pytest.raises(ht.TeamError, match="type 11"):
            ht.add_member(kit.team(), kit.hermes(), name="citra", user_id=CITRA,
                          channel_id="690000000000000002", create_channel=False)

    def test_explicit_public_channel_is_made_private(self, kit, discord):
        random_ch = "400000000000000006"
        discord.channels[random_ch]["permission_overwrites"] = [
            {"id": fd.ROLE_MOD, "type": 0, "allow": str(VIEW), "deny": "0"}]
        result = ht.add_member(kit.team(), kit.hermes(), name="citra", user_id=CITRA, channel_id=random_ch,
                               create_channel=False)
        assert result["member"]["channel_id"] == random_ch and channel_posts(discord) == []
        assert overwrites(discord, random_ch) == private_set(CITRA)
        assert any(f"role {fd.ROLE_MOD}" in n for n in result["notes"])
        assert fd.ROLE_MEMBER not in discord.members[CITRA]["roles"]      # channel eksplisit: role tidak diberikan

    def test_already_private_channel_needs_no_writes(self, kit, discord, api):
        discord.add_channel("650000000000000009", "hermes-x", overwrites=ht.private_overwrites(fd.GUILD, CITRA, fd.BOT))
        notes = []
        ht._assert_private_channel(kit.team(), api, discord.channels["650000000000000009"], CITRA, notes, who="x")
        assert notes == [] and [c for c in discord.calls if c["method"] in ("PUT", "DELETE")] == []


class TestOffboardAndReactivation:
    def test_offboard_keeps_data_and_removes_route_and_access(self, kit, discord):
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        ht.onboard_member(team, kit.hermes(), user_id=BUDI_UID)
        result = ht.offboard_member(team, kit.hermes(), user_id=BUDI_UID)
        assert result["status"] == "deactivated" and kit.members_yaml()[1]["active"] is False
        assert routes_for(kit, "be-budi") == [] and BUDI_UID not in overwrites(discord, BUDI_CH)
        assert kit.home("be-budi").is_dir() and BUDI_UID in kit.roster()["inactive_users"]
        assert ht.offboard_member(team, kit.hermes(), user_id=BUDI_UID)["status"] == "already-inactive"
        assert ht.offboard_member(team, kit.hermes(), user_id=CITRA)["status"] == "not-member"

    def test_reactivated_member_receives_scope_all_skills(self, skills_kit, discord):  # #20
        kit = skills_kit
        kit.write_members([BE1, {**BUDI, "active": False}])
        team = kit.team()
        kit.make_skill("be-be1", "deploy-checklist")
        shared = ht.share_skill(team, kit.hermes(), "deploy-checklist", "be1", ["all"])
        assert shared["scope"] == "all" and "budi" not in shared["results"]      # budi nonaktif saat dibagikan
        result = ht.onboard_member(team, kit.hermes(), user_id=BUDI_UID)
        assert result["status"] == "reactivated" and "be-budi" not in result["created"]   # profile lama, bukan baru
        assert (kit.home("be-budi") / "skills" / "shared" / "deploy-checklist" / "SKILL.md").exists()
        reg = json.loads((kit.home("be-budi") / "skills" / "shared" / ".team-shared.json").read_text())
        assert reg["deploy-checklist"]["origin_profile"] == "be-be1"

    def test_set_active_on_also_syncs_shared_skills_without_overwriting(self, skills_kit, discord):  # #20
        kit = skills_kit
        kit.write_members([BE1, {**BUDI, "active": False}])
        team = kit.team()
        kit.make_skill("be-be1", "deploy-checklist")
        kit.make_skill("be-be1", "runbook")
        ht.share_skill(team, kit.hermes(), "deploy-checklist", "be1", ["all"])
        ht.share_skill(team, kit.hermes(), "runbook", "be1", ["all"])
        mine = kit.make_skill("be-budi", "runbook", body="versi budi sendiri\n", category="shared")
        ht.set_member_active(team, kit.hermes(), "budi", True)
        assert (kit.home("be-budi") / "skills" / "shared" / "deploy-checklist").is_dir()
        assert "versi budi sendiri" in (mine / "SKILL.md").read_text()       # yang sudah ada tidak ditimpa

    def test_new_member_receives_scope_all_skills(self, skills_kit, discord):
        kit = skills_kit
        kit.make_skill("be-be1", "deploy-checklist")
        ht.share_skill(kit.team(), kit.hermes(), "deploy-checklist", "be1", ["all"])
        ht.onboard_member(kit.team(), kit.hermes(), user_id=EKA, username="eka")
        assert (kit.home("be-eka") / "skills" / "shared" / "deploy-checklist" / "SKILL.md").exists()


# =========================================================================== skill (#4, #7, #8, #9)
class TestSkillGuard:
    """#7 — pemindai Hermes fail-closed."""

    def test_missing_scanner_refuses_by_default(self, skills_kit, monkeypatch):
        kit = skills_kit
        monkeypatch.delenv("PYTHONPATH")                             # tools.skills_guard tidak bisa di-import
        kit.make_skill("be-be1", "deploy")
        with pytest.raises(ht.TeamError) as err:
            ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["budi"])
        assert "--allow-unscanned" in str(err.value) and "tidak dibagikan" in str(err.value)
        assert not (kit.state / "shared-skills" / "deploy").exists()
        assert not (kit.home("be-budi") / "skills" / "shared" / "deploy").exists()
        assert ht.shared_skill_registry(kit.team())["skills"] == {}

    def test_missing_scanner_with_explicit_opt_out(self, skills_kit, monkeypatch):
        kit = skills_kit
        monkeypatch.delenv("PYTHONPATH")
        kit.make_skill("be-be1", "deploy")
        result = ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["budi"], allow_unscanned=True)
        assert result["results"] == {"budi": "terpasang"} and result["guard"] == "unknown"

    def test_scan_report_marks_unknown_and_opt_out(self, skills_kit, monkeypatch):
        kit = skills_kit
        monkeypatch.delenv("PYTHONPATH")
        skill = kit.make_skill("be-be1", "deploy")
        strict = ht.scan_skill(kit.hermes(), "be-be1", skill)
        assert strict["guard"]["verdict"] == "unknown" and len(strict["problems"]) == 1
        relaxed = ht.scan_skill(kit.hermes(), "be-be1", skill, allow_unscanned=True)
        assert relaxed["problems"] == [] and relaxed["guard"]["unscanned_allowed"] is True

    def test_safe_verdict_allows_sharing(self, skills_kit):
        kit = skills_kit
        kit.make_skill("be-be1", "deploy")
        result = ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["all"])
        assert result["guard"] == "safe" and result["results"] == {"budi": "terpasang", "shared": "terpasang"}
        assert (kit.state / "shared-skills" / "deploy" / "SKILL.md").exists()
        entry = ht.shared_skill_registry(kit.team())["skills"]["deploy"]
        assert entry["origin_profile"] == "be-be1" and entry["scope"] == "all" and entry["targets"] == ["budi", "shared"]

    @pytest.mark.parametrize("verdict", ["caution", "dangerous"])
    def test_non_safe_verdicts_block_even_with_opt_out(self, skills_kit, monkeypatch, tmp_path, verdict):
        kit = skills_kit
        kit.fake_scanner(monkeypatch, tmp_path, verdict)
        kit.make_skill("be-be1", "deploy")
        with pytest.raises(ht.TeamError, match=f"pemindai Hermes: {verdict}"):
            ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["budi"], allow_unscanned=True)
        assert not (kit.state / "shared-skills" / "deploy").exists()

    @pytest.mark.parametrize("body", [
        "raise ImportError('modul rusak')\n",
        "def scan_skill(path, source='x'):\n    raise RuntimeError('pemindai meledak')\n",
        "class R:\n    verdict='mungkin aman'\n    findings=[]\ndef scan_skill(path, source='x'):\n    return R()\n",
        "class R:\n    verdict=None\n    findings=[]\ndef scan_skill(path, source='x'):\n    return R()\n",
    ])
    def test_broken_scanner_is_unknown_and_blocks(self, skills_kit, monkeypatch, tmp_path, body):
        kit = skills_kit
        kit.fake_scanner(monkeypatch, tmp_path, "rusak", body=body)
        skill = kit.make_skill("be-be1", "deploy")
        report = ht.scan_skill(kit.hermes(), "be-be1", skill)
        assert report["guard"]["verdict"] == "unknown" and report["problems"]

    def test_scanner_timeout_blocks(self, skills_kit, monkeypatch):
        kit = skills_kit

        def slow(*_a, **_k):
            raise subprocess.TimeoutExpired("python", 120)
        monkeypatch.setattr(ht.subprocess, "run", slow)
        report = ht.scan_skill(kit.hermes(), "be-be1", kit.make_skill("be-be1", "deploy"))
        assert report["guard"] == {"verdict": "unknown", "findings": ["TimeoutExpired"]} and report["problems"]

    def test_hermes_binary_without_python_shebang_blocks(self, skills_kit):
        kit = skills_kit
        kit.bin.write_text("#!/bin/sh\nexit 0\n")
        report = ht.scan_skill(kit.hermes(), "be-be1", kit.make_skill("be-be1", "deploy"))
        assert report["guard"]["verdict"] == "unknown" and "tidak ditemukan" in report["guard"]["findings"][0]

    def test_scanner_is_not_imported_from_current_directory(self, skills_kit, monkeypatch, tmp_path):
        kit = skills_kit
        monkeypatch.delenv("PYTHONPATH")
        evil = tmp_path / "cwd-jahat"
        (evil / "tools").mkdir(parents=True)
        (evil / "tools" / "__init__.py").write_text("")
        (evil / "tools" / "skills_guard.py").write_text(
            "class R:\n    verdict='safe'\n    findings=[]\ndef scan_skill(path, source='x'):\n    return R()\n")
        monkeypatch.chdir(evil)                                      # dulu: cwd ada di sys.path → "safe" palsu
        report = ht.scan_skill(kit.hermes(), "be-be1", kit.make_skill("be-be1", "deploy"))
        assert report["guard"]["verdict"] == "unknown" and report["problems"]

    def test_scanner_subprocess_gets_safe_path_and_cwd(self, skills_kit, monkeypatch):
        kit = skills_kit
        seen = {}
        real = subprocess.run

        def spy(cmd, **kwargs):
            seen.update(cwd=kwargs.get("cwd"), safe=kwargs["env"].get("PYTHONSAFEPATH"), listing=os.listdir(kwargs["cwd"]))
            return real(cmd, **kwargs)
        monkeypatch.setattr(ht.subprocess, "run", spy)
        ht.scan_skill(kit.hermes(), "be-be1", kit.make_skill("be-be1", "deploy"))
        assert seen["safe"] == "1" and seen["listing"] == [] and seen["cwd"] != os.getcwd()
        assert not os.path.exists(seen["cwd"])                       # folder kerja sementara dibersihkan


class TestSkillSecretScan:
    """#9 — semua nilai .env pemilik diperiksa, bukan hanya nama yang 'mirip secret'."""

    def test_value_under_innocent_name_is_detected(self, skills_kit):
        kit = skills_kit
        (kit.home("be-be1") / ".env").write_text("STRIPE=wxyz-live-9981-abcd\nDB_URL=postgres://u:p@db/x\n")
        skill = kit.make_skill("be-be1", "bayar", body="curl -u wxyz-live-9981-abcd https://api\n")
        assert not ht.SECRET_NAME_RE.search("STRIPE")                # nama ini dulu dilewati
        report = ht.scan_skill(kit.hermes(), "be-be1", skill)
        assert report["problems"] == ["berisi nilai secret pemilik: STRIPE"]
        with pytest.raises(ht.TeamError, match="STRIPE"):
            ht.share_skill(kit.team(), kit.hermes(), "bayar", "be1", ["budi"])

    def test_encoded_forms_are_detected(self, skills_kit):
        kit = skills_kit
        (kit.home("be-be1") / ".env").write_text("DB_URL=postgres://u:p@db/x\n")
        encoded = base64.b64encode(b"postgres://u:p@db/x").decode()
        skill = kit.make_skill("be-be1", "db", body=f"echo {encoded} | base64 -d\n")
        assert "DB_URL" in ht.scan_skill(kit.hermes(), "be-be1", skill)["problems"][0]

    def test_min_length_is_respected(self, skills_kit):
        kit = skills_kit
        (kit.home("be-be1") / ".env").write_text("MODE=production\nSHORT=abcde\n")
        skill = kit.make_skill("be-be1", "x", body="abcde dan production\n")
        assert ht._profile_secret_values(kit.hermes(), "be-be1") == {"MODE": "production"}
        assert ht._profile_secret_values(kit.hermes(), "be-be1", min_length=12) == {}
        assert ht.scan_skill(kit.hermes(), "be-be1", skill, min_length=12)["problems"] == []
        assert "MODE" in ht.scan_skill(kit.hermes(), "be-be1", skill)["problems"][0]

    def test_share_uses_team_min_length(self, skills_kit):
        kit = skills_kit
        kit.write_team(secrets={"min_length": 12})
        (kit.home("be-be1") / ".env").write_text("MODE=production\n")
        kit.make_skill("be-be1", "x", body="jalankan di production\n")
        assert ht.share_skill(kit.team(), kit.hermes(), "x", "be1", ["budi"])["results"] == {"budi": "terpasang"}

    def test_multiline_secret_is_detected(self, skills_kit):
        kit = skills_kit
        pem = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\nBKcwggSjAgEAAoIBAQC7"
        (kit.home("be-be1") / ".env").write_text(f'A=1\nSSH_KEY="{pem}"\n')
        skill = kit.make_skill("be-be1", "ssh", body=f"kunci:\n{pem}\n")
        assert "SSH_KEY" in ht.scan_skill(kit.hermes(), "be-be1", skill)["problems"][0]

    @pytest.mark.parametrize("content", ["{bukan json", "[1, 2, 3]", '"teks"', '{"keys": 5}', "null", "\xff\xfe"])
    def test_malformed_secrets_index_is_tolerated(self, skills_kit, content):
        kit = skills_kit
        index = kit.home("be-be1") / "team-guard" / "secrets.json"
        index.parent.mkdir()
        index.write_bytes(content.encode("latin-1"))
        (kit.home("be-be1") / ".env").write_text("GH_PAT=nilai-rahasia-98765\n")
        skill = kit.make_skill("be-be1", "x", body="token nilai-rahasia-98765\n")
        assert ht.scan_skill(kit.hermes(), "be-be1", skill)["problems"] == ["berisi nilai secret pemilik: GH_PAT"]

    def test_registered_short_key_is_included(self, skills_kit):
        kit = skills_kit
        index = kit.home("be-be1") / "team-guard" / "secrets.json"
        index.parent.mkdir()
        index.write_text(json.dumps({"keys": {"PIN": {}}}))
        (kit.home("be-be1") / ".env").write_text("PIN=x9k2m7\nOTHER=x9k2m7\n")
        assert set(ht._profile_secret_values(kit.hermes(), "be-be1", min_length=10)) == {"PIN"}

    def test_credential_patterns_and_symlinks_block(self, skills_kit, tmp_path):
        kit = skills_kit
        skill = kit.make_skill("be-be1", "x", body="export GH=ghp_" + "A1b2C3d4" * 5 + "\n")
        outside = tmp_path / "luar.txt"
        outside.write_text("x")
        (skill / "tautan").symlink_to(outside)
        workdir, staged, skipped = ht.stage_skill(kit.team(), kit.hermes(), kit.team()["members"][0], "x")
        report = ht.scan_skill(kit.hermes(), "be-be1", staged, skipped_symlinks=skipped)
        assert skipped == ["tautan"] and not (staged / "tautan").exists()
        assert any("symlink" in p for p in report["problems"]) and any("pola kredensial" in p for p in report["problems"])


class TestSharedSkillArchive:
    """#8 dan bagian skill dari #4."""

    def test_other_origin_cannot_replace_archive_without_overwrite(self, skills_kit):
        kit = skills_kit
        kit.write_members([BE1, BUDI, CITRA_ROW])
        kit.make_profile("be-citra")
        kit.make_skill("be-be1", "deploy", body="versi be1\n")
        kit.make_skill("be-budi", "deploy", body="versi budi\n")
        team = kit.team()
        ht.share_skill(team, kit.hermes(), "deploy", "be1", ["citra"])
        with pytest.raises(ht.TeamError) as err:
            ht.share_skill(team, kit.hermes(), "deploy", "budi", ["citra"])
        assert "be1" in str(err.value) and "overwrite" in str(err.value)
        assert "versi be1" in (kit.state / "shared-skills" / "deploy" / "SKILL.md").read_text()
        assert ht.shared_skill_registry(team)["skills"]["deploy"]["origin_profile"] == "be-be1"
        assert "versi be1" in (kit.home("be-citra") / "skills" / "shared" / "deploy" / "SKILL.md").read_text()

    def test_overwrite_replaces_and_retracts_old_origin_copies(self, skills_kit):
        kit = skills_kit
        kit.write_members([BE1, BUDI, CITRA_ROW])
        kit.make_profile("be-citra")
        kit.make_skill("be-be1", "deploy", body="versi be1\n")
        kit.make_skill("be-budi", "deploy", body="versi budi\n")
        team = kit.team()
        ht.share_skill(team, kit.hermes(), "deploy", "be1", ["citra", "shared"])
        result = ht.share_skill(team, kit.hermes(), "deploy", "budi", ["citra"], overwrite=True)
        assert result["results"] == {"citra": "terpasang"}
        entry = ht.shared_skill_registry(team)["skills"]["deploy"]
        assert entry["origin_profile"] == "be-budi" and entry["targets"] == ["citra"]
        assert "versi budi" in (kit.home("be-citra") / "skills" / "shared" / "deploy" / "SKILL.md").read_text()
        assert not (kit.home("team-shared") / "skills" / "shared" / "deploy").exists()   # salinan be1 ditarik

    def test_same_origin_can_update_without_overwrite(self, skills_kit):
        kit = skills_kit
        skill = kit.make_skill("be-be1", "deploy", body="v1\n")
        ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["budi"])
        (skill / "SKILL.md").write_text("---\nname: deploy\n---\nv2\n")
        ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["budi"])
        assert "v2" in (kit.home("be-budi") / "skills" / "shared" / "deploy" / "SKILL.md").read_text()

    def test_orphan_archive_without_registry_entry_is_protected(self, skills_kit):
        kit = skills_kit
        orphan = kit.state / "shared-skills" / "deploy"
        orphan.mkdir(parents=True)
        (orphan / "SKILL.md").write_text("asal tidak diketahui\n")
        kit.make_skill("be-be1", "deploy")
        with pytest.raises(ht.TeamError, match="asal tidak tercatat"):
            ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["budi"])
        assert (orphan / "SKILL.md").read_text() == "asal tidak diketahui\n"

    def test_skill_named_like_registry_file_is_rejected(self, skills_kit):
        kit = skills_kit
        kit.make_skill("be-be1", "deploy")
        ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["budi"])
        registry_file = kit.state / "shared-skills" / "registry.json"
        before = registry_file.read_text()
        kit.make_skill("be-be1", "registry.json")
        with pytest.raises(ht.TeamError, match="registry.json"):
            ht.share_skill(kit.team(), kit.hermes(), "registry.json", "be1", ["budi"], overwrite=True)
        assert registry_file.is_file() and registry_file.read_text() == before
        assert "deploy" in ht.shared_skill_registry(kit.team())["skills"]

    def test_share_rereads_team_inside_lock(self, skills_kit):                         # #4
        kit = skills_kit
        stale = kit.team()                                           # belum ada citra
        kit.write_members([BE1, BUDI, CITRA_ROW])
        kit.make_profile("be-citra")
        kit.make_skill("be-be1", "deploy")
        result = ht.share_skill(stale, kit.hermes(), "deploy", "be1", ["all"])
        assert result["results"] == {"budi": "terpasang", "citra": "terpasang", "shared": "terpasang"}

    def test_share_rejects_target_deactivated_since_snapshot(self, skills_kit):       # #4
        kit = skills_kit
        stale = kit.team()
        kit.write_members([BE1, {**BUDI, "active": False}])
        kit.make_skill("be-be1", "deploy")
        with pytest.raises(ht.TeamError, match="nonaktif"):
            ht.share_skill(stale, kit.hermes(), "deploy", "be1", ["budi"])

    def test_unshare_rereads_team_and_removes_everything(self, skills_kit):            # #4
        kit = skills_kit
        kit.make_skill("be-be1", "deploy")
        stale = kit.team()
        kit.write_members([BE1, BUDI, CITRA_ROW])
        kit.make_profile("be-citra")
        ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["all"])
        result = ht.unshare_skill(stale, kit.hermes(), "deploy")
        assert result["results"] == {"budi": "dihapus", "citra": "dihapus", "shared": "dihapus"}
        assert not (kit.home("be-citra") / "skills" / "shared" / "deploy").exists()
        assert not (kit.state / "shared-skills" / "deploy").exists()
        assert ht.shared_skill_registry(kit.team())["skills"] == {}
        with pytest.raises(ht.TeamError, match="tidak ada di daftar"):
            ht.unshare_skill(stale, kit.hermes(), "deploy")

    def test_recipient_own_skill_is_never_overwritten(self, skills_kit):
        kit = skills_kit
        kit.make_skill("be-be1", "deploy")
        own = kit.make_skill("be-budi", "deploy", body="punya budi\n")
        result = ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["budi"], overwrite=True)
        assert result["results"]["budi"].startswith("dilewati") and "punya budi" in (own / "SKILL.md").read_text()

    def test_received_skill_cannot_be_reshared(self, skills_kit):
        kit = skills_kit
        kit.make_skill("be-be1", "deploy")
        ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["budi"])
        with pytest.raises(ht.TeamError, match="pemilik aslinya"):
            ht.share_skill(kit.team(), kit.hermes(), "shared/deploy", "budi", ["shared"])

    def test_list_skills(self, skills_kit):
        kit = skills_kit
        kit.make_skill("be-be1", "deploy", category="ops")
        kit.make_skill("be-be1", "tersembunyi", category=".archive")
        assert [(s["name"], s["path"], s["shared"]) for s in ht.list_skills(kit.hermes(), "be-be1")] == \
            [("deploy", "ops/deploy", False)]


# =========================================================================== CLI (#11, #15, #17)
class TestMain:
    def test_check_and_managed(self, kit, capsys):
        ht.main(cli(kit, "check"))
        assert "Konfigurasi valid." in capsys.readouterr().out
        ht.main(cli(kit, "managed"))
        assert yaml.safe_load(capsys.readouterr().out)["terminal"]["backend"] == "docker"

    def test_apply_only_typo_exits_with_message(self, kit, capsys):                    # #15
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "apply", "--only", "bedu"))
        assert "--only" in str(exit_info.value.code) and "bedu" in str(exit_info.value.code)
        assert kit.calls() == []

    def test_unexpected_exception_is_normalised(self, kit, monkeypatch, capsys):       # #17
        def boom(*_a, **_k):
            raise RuntimeError("kejutan internal")
        monkeypatch.setattr(ht, "cmd_apply", boom)
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "apply"))
        captured = capsys.readouterr()
        assert exit_info.value.code not in (0, None)
        assert "tak terduga" in str(exit_info.value.code) and "RuntimeError: kejutan internal" in str(exit_info.value.code)
        assert "Traceback" not in captured.err and "Traceback" not in captured.out

    def test_debug_flag_shows_traceback(self, kit, monkeypatch, capsys):               # #17
        def boom(*_a, **_k):
            raise RuntimeError("kejutan internal")
        monkeypatch.setattr(ht, "cmd_apply", boom)
        monkeypatch.setenv("HERMES_TEAM_DEBUG", "1")
        with pytest.raises(SystemExit):
            ht.main(cli(kit, "apply"))
        assert "Traceback (most recent call last)" in capsys.readouterr().err

    def test_unexpected_exception_with_events_emits_error_event(self, kit, monkeypatch, capsys):   # #17
        def boom(*_a, **_k):
            raise KeyError("kolom hilang")
        monkeypatch.setattr(ht, "onboard_member", boom)
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "onboard", "--user-id", DODI, "--events"))
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert exit_info.value.code not in (0, None)
        assert lines[-1]["event"] == "error" and "KeyError" in lines[-1]["message"]

    def test_config_error_with_events_emits_error_event(self, kit, capsys):
        kit.team_file.write_text("guild_id: bukan-id\n")
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "onboard", "--user-id", DODI, "--events"))
        event = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert exit_info.value.code == 1 and event["event"] == "error" and "guild_id" in event["message"]

    def test_onboard_events_protocol(self, kit, discord, capsys):
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "onboard", "--user-id", DODI, "--username", "dodi.pratama", "--events"))
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]   # setiap baris = satu JSON
        assert exit_info.value.code == 0 and [e["event"] for e in lines] == ["channel", "result"]
        assert lines[0]["created"] is True and lines[0]["channel_id"] in discord.channels
        result = lines[1]
        assert {"status", "user_id", "name", "display_name", "profile", "channel_id", "model", "notes",
                "restart_needed", "live_changed", "failures"} <= set(result)
        assert (result["status"], result["name"], result["profile"], result["model"]) == \
            ("created", "dodi-pratama", "be-dodi-pratama", "td-simple")
        assert result["channel_id"] == lines[0]["channel_id"]

    def test_onboard_events_team_error(self, kit, discord, capsys):
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "onboard", "--user-id", "599999999999999999", "--events"))
        event = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert exit_info.value.code == 1 and event["event"] == "error" and "tidak ditemukan" in event["message"]

    def test_share_skill_cli_flag(self, skills_kit, monkeypatch, capsys):              # #7
        kit = skills_kit
        monkeypatch.delenv("PYTHONPATH")
        kit.make_skill("be-be1", "deploy")
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "share-skill", "deploy", "--from", "be1", "--to", "budi"))
        assert "--allow-unscanned" in str(exit_info.value.code)
        ht.main(cli(kit, "share-skill", "deploy", "--from", "be1", "--to", "budi", "--allow-unscanned"))
        assert json.loads(capsys.readouterr().out)["results"] == {"budi": "terpasang"}


class TestLlmKey:
    """#11"""

    def test_refuses_when_stdin_is_not_a_tty(self, kit, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("sk-dari-pipe-123456\n"))
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "llm-key", "be1"))
        assert "--stdin" in str(exit_info.value.code)
        assert not (kit.home("be-be1") / ".env").exists()

    def test_stdin_flag_reads_one_line_and_preserves_other_keys(self, kit, monkeypatch, capsys):
        kit.make_profile("be-be1", env='GH_PAT="punya be1"\nTD_API_KEY=lama\n')
        monkeypatch.setattr("sys.stdin", io.StringIO("sk-dari-pipe-123456\nbaris kedua diabaikan\n"))
        ht.main(cli(kit, "llm-key", "be1", "--stdin"))
        path = kit.home("be-be1") / ".env"
        assert ht.parse_env_text(path.read_text()) == {"GH_PAT": "punya be1", "TD_API_KEY": "sk-dari-pipe-123456"}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "sk-dari-pipe" not in capsys.readouterr().out          # nilai tidak pernah dicetak

    def test_empty_stdin_is_refused(self, kit, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "llm-key", "be1", "--stdin"))
        assert "kosong" in str(exit_info.value.code)

    def test_read_modify_write_happens_under_team_lock(self, kit, monkeypatch):
        depths = []
        real = ht.atomic_write

        def spy(path, text, mode=0o600):
            depths.append(ht._lock_depth())
            return real(path, text, mode=mode)
        monkeypatch.setattr(ht, "atomic_write", spy)
        monkeypatch.setattr("sys.stdin", io.StringIO("sk-dari-pipe-123456\n"))
        ht.main(cli(kit, "llm-key", "shared", "--stdin"))
        assert depths == [1]
        assert "TD_API_KEY" in (kit.home("team-shared") / ".env").read_text()

    def test_tty_path_uses_hidden_prompt(self, kit, monkeypatch):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        monkeypatch.setattr("sys.stdin", Tty(""))
        monkeypatch.setattr(ht.getpass, "getpass", lambda prompt: "  sk-interaktif-98765  ")
        ht.main(cli(kit, "llm-key", "default"))
        assert ht.parse_env_text((kit.root / ".env").read_text())["TD_API_KEY"] == "sk-interaktif-98765"

    def test_unknown_target(self, kit, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("x\n"))
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "llm-key", "siapa", "--stdin"))
        assert "tidak dikenal" in str(exit_info.value.code)
