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


def cli(kit, *args, placeholders=True):
    """argv CLI untuk kit tiruan. ID uji = ID Discord tiruan = pola ID contoh, jadi `--allow-placeholders`
    ikut dipasang; uji penolakan ID contoh memakai `placeholders=False`."""
    return [*args, "-c", str(kit.team_file), "--hermes-bin", str(kit.bin), "--hermes-root", str(kit.root),
            "--allow-root", *(["--allow-placeholders"] if placeholders else [])]


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

def hide_scanner(monkeypatch, kit):
    """Simulasikan instalasi tanpa pemindai Hermes: interpreter Hermes tidak ditemukan. Dulu uji ini hanya
    menghapus PYTHONPATH, yang tidak berpengaruh bila Hermes asli terpasang di Python uji (job integrasi CI):
    pemindai asli tetap ditemukan dan menjawab "safe". Menambal penentu interpreter berlaku di kedua keadaan."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(ht, "_hermes_python", lambda _binary: None)


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
        assert term["docker_image"] == "team/img:1" and term["container_persistent"] is True
        # Dipindah ke config per profile (managed menang atas semua profile, jadi tidak bisa dibedakan).
        assert "container_cpu" not in term and "container_memory" not in term
        assert policy["compression"] == {"idle_compact_after_seconds": 3600}
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
        hide_scanner(monkeypatch, kit)                               # tools.skills_guard tidak bisa di-import
        kit.make_skill("be-be1", "deploy")
        with pytest.raises(ht.TeamError) as err:
            ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["budi"])
        assert "--allow-unscanned" in str(err.value) and "tidak dibagikan" in str(err.value)
        assert not (kit.state / "shared-skills" / "deploy").exists()
        assert not (kit.home("be-budi") / "skills" / "shared" / "deploy").exists()
        assert ht.shared_skill_registry(kit.team())["skills"] == {}

    def test_missing_scanner_with_explicit_opt_out(self, skills_kit, monkeypatch):
        kit = skills_kit
        hide_scanner(monkeypatch, kit)
        kit.make_skill("be-be1", "deploy")
        result = ht.share_skill(kit.team(), kit.hermes(), "deploy", "be1", ["budi"], allow_unscanned=True)
        assert result["results"] == {"budi": "terpasang"} and result["guard"] == "unknown"

    def test_scan_report_marks_unknown_and_opt_out(self, skills_kit, monkeypatch):
        kit = skills_kit
        hide_scanner(monkeypatch, kit)
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
        """`tools/skills_guard.py` di direktori kerja tidak boleh menggantikan pemindai Hermes. Modul jahat
        meninggalkan jejak bila sampai di-import; berlaku baik dengan pemindai tiruan maupun Hermes asli."""
        kit = skills_kit
        evil, marker = tmp_path / "cwd-jahat", tmp_path / "TERIMPOR"
        (evil / "tools").mkdir(parents=True)
        (evil / "tools" / "__init__.py").write_text("")
        (evil / "tools" / "skills_guard.py").write_text(
            f"open({str(marker)!r}, 'w').close()\n"
            "class R:\n    verdict='safe'\n    findings=[]\ndef scan_skill(path, source='x'):\n    return R()\n")
        monkeypatch.chdir(evil)                                      # dulu: cwd ada di sys.path → "safe" palsu
        ht.scan_skill(kit.hermes(), "be-be1", kit.make_skill("be-be1", "deploy"))
        assert not marker.exists(), "pemindai diambil dari direktori kerja"

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
        hide_scanner(monkeypatch, kit)
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


# =========================================================================== parkir resmi (gateway.parked)
class TestParking:
    def test_offboard_parks_profile_and_reactivation_unparks(self, kit, discord):
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        ht.onboard_member(team, kit.hermes(), user_id=BUDI_UID)
        assert not (kit.home("be-budi") / "gateway.parked").exists()
        ht.offboard_member(team, kit.hermes(), user_id=BUDI_UID)
        assert (kit.home("be-budi") / "gateway.parked").exists()          # cron & adapter berhenti
        assert (kit.home("be-budi") / ".env").exists()                    # data utuh
        ht.onboard_member(team, kit.hermes(), user_id=BUDI_UID)
        assert not (kit.home("be-budi") / "gateway.parked").exists()

    def test_apply_heals_markers_to_match_members_yaml(self, kit, discord):
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        (kit.home("be-be1") / "gateway.parked").touch()                   # aktif tetapi terparkir
        kit.write_members([BE1, {**BUDI, "active": False}])
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        assert not (kit.home("be-be1") / "gateway.parked").exists()
        assert (kit.home("be-budi") / "gateway.parked").exists()

    def test_dry_run_does_not_touch_markers(self, kit, discord):
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        kit.write_members([BE1, {**BUDI, "active": False}])
        team = kit.team()
        ht.cmd_apply(team, ht.Hermes(str(kit.bin), kit.root, dry_run=True, quiet=True))
        assert not (kit.home("be-budi") / "gateway.parked").exists()

    def test_missing_profile_dir_is_skipped(self, kit, discord):
        kit.write_members([BE1, {**BUDI, "active": False}])
        team = kit.team()
        ht.sync_parking(team, kit.hermes())          # be-budi belum pernah dibuat: tidak membuat folder
        assert not kit.home("be-budi").exists()


# =========================================================================== thread per percakapan
class TestThreadsPerConversation:
    def _settings(self, kit, **threads):
        kit.write_team(**({"threads": threads} if threads else {}))
        return dict(ht.host_settings(kit.team()))

    def test_default_personal_channels_open_a_thread_per_message(self, kit):
        st = self._settings(kit)
        assert st["discord.free_response_auto_thread"] is True
        assert st["discord.auto_thread"] is True and st["discord.require_mention"] is True
        assert st["discord.no_thread_channels"] == [kit.team()["admin"]["channel_id"]]   # admin tetap inline

    def test_can_turn_threads_off(self, kit):
        assert self._settings(kit, personal=False)["discord.free_response_auto_thread"] is False

    def test_admin_threads_opt_in(self, kit):
        assert self._settings(kit, admin=True)["discord.no_thread_channels"] == []

    def test_invalid_threads_section_rejected(self, kit):
        kit.write_team(threads=["x"])
        with pytest.raises(ht.TeamError):
            kit.team()
        kit.write_team(threads={"personal": "mungkin"})
        with pytest.raises(ht.TeamError):
            kit.team()

    def test_apply_writes_keys_and_is_idempotent(self, kit, discord):
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        sets = {(p, k): v for p, k, v in kit.config_sets()}
        assert sets[("default", "discord.free_response_auto_thread")] == "true"
        assert sets[("default", "discord.no_thread_channels")] == team["admin"]["channel_id"]
        kit.clear_log()
        ht.cmd_apply(kit.team(), kit.hermes())
        assert kit.config_sets() == []                      # apply kedua: nol `config set`

    def test_changing_the_option_needs_a_gateway_restart(self, kit, discord):
        ht.cmd_apply(kit.team(), kit.hermes())
        kit.write_team(threads={"personal": False})
        result = ht.cmd_apply(kit.team(), kit.hermes())
        assert result["restart_needed"] is True


# =========================================================================== pencatatan pemakaian
class TestUsageWiring:
    def test_admin_profile_reads_kit_skills_only(self, kit):
        team = kit.team()
        admin = dict(ht.profile_settings(team, "admin"))["skills.external_dirs"]
        member = dict(ht.profile_settings(team, "member", team["members"][0]))["skills.external_dirs"]
        assert str(ht.KIT_SKILLS) in admin and str(ht.KIT_SKILLS) not in member

    def test_kit_skill_has_valid_frontmatter_and_names_the_tool(self):
        skill = ht.KIT_SKILLS / "team-usage-report" / "SKILL.md"
        meta = ht._frontmatter(skill)
        assert meta["name"] == "team-usage-report" and meta["description"]
        assert "usage_report" in skill.read_text(encoding="utf-8")

    def test_usage_timezone_is_loaded_and_validated(self, kit):
        kit.write_team(usage={"timezone": "Asia/Makassar"})
        assert kit.team()["usage"]["timezone"] == "Asia/Makassar"
        kit.write_team(usage=["x"])
        with pytest.raises(ht.TeamError):
            kit.team()

    def test_systemd_units_point_at_the_ledger(self):
        unit = (ht.KIT_DIR / "systemd" / "hermes-usage-collect.service").read_text(encoding="utf-8")
        timer = (ht.KIT_DIR / "systemd" / "hermes-usage-collect.timer").read_text(encoding="utf-8")
        assert "usage_ledger.py collect" in unit and "OnCalendar=*:0/15" in timer


# =========================================================================== skala (docs/RENCANA-SKALA.md)
class TestScalePlan:
    def test_new_scale_defaults(self, kit):
        scale = kit.team()["scale"]
        assert (scale["user_max_concurrent"], scale["max_llm_parallel"], scale["daily_token_budget"],
                scale["team_daily_token_budget"], scale["cron_min_interval_minutes"],
                scale["session_retention_days"]) == (2, 0, 0, 0, 15, 90)
        assert scale["adaptive_throttle"] is True
        assert scale["allow_member_cron"] is False and scale["allow_goals"] is False
        assert (scale["goals_max_turns"], scale["delegation_max_children"]) == (4, 1)

    def test_new_scale_settings_are_validated(self, kit):
        kit.write_team(scale={"user_max_concurrent": "3", "daily_token_budget": 3000000, "adaptive_throttle": "off",
                              "allow_goals": "ya"})
        scale = kit.team()["scale"]
        assert (scale["user_max_concurrent"], scale["daily_token_budget"]) == (3, 3000000)
        assert scale["adaptive_throttle"] is False and scale["allow_goals"] is True
        for bad in ({"user_max_concurrent": 0}, {"max_llm_parallel": -1}, {"daily_token_budget": "banyak"},
                    {"team_daily_token_budget": 1.5}, {"adaptive_throttle": "mungkin"},
                    {"cron_min_interval_minutes": -5}, {"session_retention_days": None}):
            kit.write_team(scale=bad)
            with pytest.raises(ht.TeamError, match="scale\\."):
                kit.team()
        kit.write_team(scale=["x"])
        with pytest.raises(ht.TeamError, match="scale: harus mapping"):
            kit.team()

    def test_example_file_documents_every_scale_setting(self):
        text = (ht.KIT_DIR / "team.example.yaml").read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
        assert set(raw["scale"]) == set(ht.SCALE_DEFAULTS)
        assert {k: raw["scale"][k] for k in ht.SCALE_DEFAULTS} == ht.SCALE_DEFAULTS      # contoh = bawaan
        assert "custom:backup" in text and "credential-pools" in text

    def test_cron_toolset_off_for_members_and_shared_but_not_admin(self, kit):
        team = kit.team()
        for role, member in (("member", team["members"][0]), ("template", None), ("shared", None)):
            assert "cronjob" in dict(ht.profile_settings(team, role, member))["agent.disabled_toolsets"]
        admin = dict(ht.profile_settings(team, "admin"))["agent.disabled_toolsets"]
        assert "cronjob" not in admin and "discord" in admin
        kit.write_team(scale={"allow_member_cron": True})
        team = kit.team()
        for role in ("member", "shared", "admin"):
            assert "cronjob" not in dict(ht.profile_settings(team, role))["agent.disabled_toolsets"]

    def test_goal_commands_are_opt_in_and_explicit_list_wins(self, kit):
        team = kit.team()
        assert "goal" not in team["user_commands"] and "subgoal" not in team["user_commands"]
        assert "new" in team["user_commands"] and "bg" in team["user_commands"]
        assert dict(ht.host_settings(team))["platforms.discord.extra.user_allowed_commands"] == team["user_commands"]
        assert {"goal", "subgoal"} <= set(ht.DEFAULT_USER_COMMANDS)          # konstanta tetap lengkap
        kit.write_team(scale={"allow_goals": True})
        assert kit.team()["user_commands"] == ht.DEFAULT_USER_COMMANDS
        kit.write_team(user_allowed_commands=["/new", "Goal"])                # eksplisit menang walau allow_goals false
        assert kit.team()["user_commands"] == ["new", "goal"]

    def test_managed_config_pins_cron_and_session_retention(self, kit):
        policy = ht.managed_config(kit.team())
        assert policy["cron"] == {"max_parallel_jobs": 1, "model": "td-simple", "model_provider": "custom:td"}
        assert policy["sessions"] == {"auto_prune": True, "retention_days": 90}
        assert policy["goals"] == {"max_turns": 4} and policy["delegation"]["max_concurrent_children"] == 1
        kit.write_team(scale={"session_retention_days": 0}, llm=None)
        policy = ht.managed_config(kit.team())
        assert "sessions" not in policy and policy["cron"] == {"max_parallel_jobs": 1}
        kit.write_team(scale={"session_retention_days": 30})
        assert ht.managed_config(kit.team())["sessions"]["retention_days"] == 30

    def test_roster_carries_limits_and_usage_for_the_plugin(self, kit):
        kit.write_team(scale={"user_max_concurrent": 3, "max_llm_parallel": 12, "daily_token_budget": 3000000,
                              "team_daily_token_budget": 40000000, "adaptive_throttle": False,
                              "cron_min_interval_minutes": 30}, usage={"timezone": "Asia/Makassar"})
        data = ht.roster_data(kit.team(), kit.root, str(kit.bin))
        settings = data["settings"]
        assert {k: settings[k] for k in ("user_max_concurrent", "max_llm_parallel", "daily_token_budget",
                                         "team_daily_token_budget", "adaptive_throttle",
                                         "cron_min_interval_minutes")} == {
            "user_max_concurrent": 3, "max_llm_parallel": 12, "daily_token_budget": 3000000,
            "team_daily_token_budget": 40000000, "adaptive_throttle": False, "cron_min_interval_minutes": 30}
        assert all(type(settings[k]) is int for k in ("user_max_concurrent", "max_llm_parallel",
                                                      "daily_token_budget", "team_daily_token_budget",
                                                      "cron_min_interval_minutes"))
        assert type(settings["adaptive_throttle"]) is bool
        assert data["usage"] == {"timezone": "Asia/Makassar", "ledger": str(kit.state / "usage.db")}
        # key lama tidak berubah (plugin versi lama tetap bisa membaca roster ini)
        assert data["version"] == 3 and {"rate_per_minute", "burst", "max_active", "queue_max_wait"} <= set(settings)


class TestFallbackWarnings:
    def test_same_provider_and_missing_fallback_are_reported_by_alias(self, kit):
        warnings = ht.fallback_warnings(kit.team())
        text = "\n".join(warnings)
        assert "tanpa fallback sama sekali: td-simple" in text
        assert "td-normal (custom:td)" in text and "provider yang sama" in text
        assert "team.example.yaml" in warnings[-1]

    def test_second_provider_in_chain_silences_the_warning(self, kit):
        llm = json.loads(json.dumps(kit.write_team()["llm"]))
        llm["providers"]["backup"] = {"api": "https://backup.example/v1", "key_env": "BACKUP_API_KEY"}
        llm["models"]["backup-simple"] = {"provider": "custom:backup", "model": "small"}
        llm["fallback"] = ["td-simple", "backup-simple"]
        kit.write_team(llm=llm)
        team = kit.team()
        assert ht.fallback_warnings(team) == []
        assert "backup-simple" not in ht.used_model_aliases(team)             # hanya cadangan → tidak diperiksa

    def test_only_models_in_use_are_checked_and_no_llm_means_no_warning(self, kit):
        kit.write_members([BUDI])                                             # tidak ada yang memakai td-normal…
        llm = json.loads(json.dumps(kit.write_team()["llm"]))
        llm["admin_model"] = "td-simple"                                      # …termasuk admin
        llm["fallback"] = []
        kit.write_team(llm=llm)
        assert ht.used_model_aliases(kit.team()) == ["td-simple"]
        assert "td-normal" not in "\n".join(ht.fallback_warnings(kit.team()))
        kit.write_team(llm=None)
        assert ht.fallback_warnings(kit.team()) == []

    def test_check_and_apply_print_warning_but_still_succeed(self, kit, capsys):
        ht.main(cli(kit, "check"))
        out = capsys.readouterr().out
        assert "Peringatan:" in out and "td-normal (custom:td)" in out and out.rstrip().endswith("Konfigurasi valid.")
        ht.main(cli(kit, "apply"))                                             # peringatan ≠ kegagalan (tanpa SystemExit)
        out = capsys.readouterr().out
        assert "Selesai." in out and "tanpa fallback sama sekali: td-simple" in out


# =========================================================================== onboarding massal (#11)
def _batch_file(tmp_path, entries, name="batch.yaml"):
    path = tmp_path / name
    path.write_text(json.dumps(entries) if name.endswith(".json") else yaml.safe_dump(entries), encoding="utf-8")
    return path


class TestOnboardBatch:
    ENTRIES = [{"user_id": DODI, "username": "dodi.pratama", "display_name": "Dodi Pratama"},
               {"user_id": EKA, "username": "eka", "model": "td-normal"},
               {"user_id": CITRA, "name": "citra"}]

    def test_all_channels_then_single_apply(self, kit, discord):
        events = []
        result = ht.onboard_batch(kit.team(), kit.hermes(), self.ENTRIES, emit=lambda e, **f: events.append((e, f)))
        assert [m["status"] for m in result["members"]] == ["created"] * 3
        assert result["failed"] == [] and result["failures"] == []
        names = [m["member"]["name"] for m in result["members"]]
        assert names == ["dodi-pratama", "eka", "citra"]
        for row in result["members"]:
            member = row["member"]
            assert overwrites(discord, member["channel_id"]) == private_set(row["user_id"])
            assert routes_for(kit, member["profile"])[0]["chat_id"] == member["channel_id"]
            assert kit.roster()["users"][row["user_id"]]["profile"] == member["profile"]
        assert [(e, f["user_id"], f["created"]) for e, f in events] == \
            [("channel", DODI, True), ("channel", EKA, True), ("channel", CITRA, True)]
        assert [m["name"] for m in kit.members_yaml()] == ["be1", "budi", *names]
        assert kit.members_yaml()[3]["model"] == "td-normal"
        # SATU apply: rute gateway ditulis sekali saja, sesudah SEMUA channel dibuat
        route_sets = [c for c in kit.config_sets() if c[1] == "gateway.profile_routes"]
        assert len(route_sets) == 1
        assert all(m["member"]["channel_id"] in route_sets[0][2] for m in result["members"])
        assert sorted(kit.creates()) == sorted(["be-template", *(m["member"]["profile"] for m in result["members"])])

    def test_validation_error_anywhere_changes_nothing(self, kit, discord):
        before = kit.members_file.read_text()
        kit.make_profile("be-sisa")
        bad = [{"user_id": DODI, "username": "dodi"},
               {"user_id": DODI, "username": "dodi-lagi"},                   # dobel di berkas
               {"user_id": "bukan-id"},
               {"user_id": EKA, "model": "gpt-rahasia"},
               {"user_id": CITRA, "name": "sisa"},                           # profile sisa anggota lama
               {"user_id": "500000000000000006", "nickname": "x"},           # key tak dikenal
               "bukan-mapping"]
        with pytest.raises(ht.TeamError) as err:
            ht.onboard_batch(kit.team(), kit.hermes(), bad)
        text = str(err.value)
        assert "tidak ada yang diubah" in text
        for where in ("entri[1]", "entri[2]", "entri[3]", "entri[4]", "entri[5]", "entri[6]"):
            assert where in text
        assert "entri[0]" not in text
        assert discord.calls == [] and kit.calls() == [] and kit.members_file.read_text() == before

    def test_names_derived_inside_one_file_do_not_collide(self, kit, discord):
        result = ht.onboard_batch(kit.team(), kit.hermes(), [{"user_id": DODI, "username": "sama"},
                                                             {"user_id": EKA, "username": "sama"}])
        assert [m["member"]["name"] for m in result["members"]] == ["sama", "sama-2"]
        with pytest.raises(ht.TeamError, match="entri\\[1\\]"):
            ht.onboard_batch(kit.team(), kit.hermes(), [{"user_id": CITRA, "name": "kembar"},
                                                        {"user_id": "500000000000000006", "name": "kembar"}])

    def test_member_missing_from_guild_fails_alone(self, kit, discord):
        ghost = "599999999999999999"
        result = ht.onboard_batch(kit.team(), kit.hermes(), [{"user_id": DODI, "username": "dodi"},
                                                             {"user_id": ghost, "username": "hantu"},
                                                             {"user_id": EKA, "username": "eka"}])
        assert [m["status"] for m in result["members"]] == ["created", "failed", "created"]
        assert [m["user_id"] for m in result["failed"]] == [ghost]
        assert "tidak ditemukan" in result["failed"][0]["failures"][0] and ghost in result["failures"][0]
        assert [m["name"] for m in kit.members_yaml()] == ["be1", "budi", "dodi", "eka"]
        assert channels_named(discord, "hermes-hantu") == [] and not kit.home("be-hantu").exists()
        assert routes_for(kit, "be-dodi") and routes_for(kit, "be-eka")

    def test_profile_failure_rolls_back_only_that_member(self, kit, discord, monkeypatch):
        monkeypatch.setenv("FAKE_HERMES_FAIL_CREATE", "be-eka")
        result = ht.onboard_batch(kit.team(), kit.hermes(), self.ENTRIES)
        assert [m["status"] for m in result["members"]] == ["created", "failed", "created"]
        failed = result["failed"][0]
        assert "disk penuh" in failed["failures"][0] and "dibatalkan" in failed["failures"][0]
        assert channels_named(discord, "hermes-eka") == [] and not kit.home("be-eka").exists()
        assert [m["name"] for m in kit.members_yaml()] == ["be1", "budi", "dodi-pratama", "citra"]
        # rute & roster akhir hanya memuat yang berhasil (tidak ada rute ke profile yang dibatalkan)
        assert routes_for(kit, "be-eka") == [] and EKA not in kit.roster()["users"]
        assert routes_for(kit, "be-dodi-pratama") and routes_for(kit, "be-citra")
        assert {DODI, CITRA} <= set(kit.roster()["users"])
        # percobaan ulang setelah penyebabnya hilang hanya menambah yang kurang
        monkeypatch.delenv("FAKE_HERMES_FAIL_CREATE")
        retry = ht.onboard_batch(kit.team(), kit.hermes(), self.ENTRIES)
        assert [m["status"] for m in retry["members"]] == ["exists", "created", "exists"] and retry["failed"] == []
        assert len(channels_named(discord, "hermes-eka")) == 1 and len(channels_named(discord, "hermes-citra")) == 1

    def test_existing_and_inactive_members_are_handled_like_single_onboarding(self, kit, discord):
        discord.add_channel(BUDI_CH, "hermes-budi", parent=fd.CAT_HERMES,
                            overwrites=ht.private_overwrites(fd.GUILD, BUDI_UID, fd.BOT))
        discord.add_channel(BE1_CH, "hermes-be1", parent=fd.CAT_HERMES,
                            overwrites=ht.private_overwrites(fd.GUILD, BE1_UID, fd.BOT))
        kit.write_members([BE1, {**BUDI, "active": False}])
        ht.cmd_apply(kit.team(), kit.hermes())
        result = ht.onboard_batch(kit.team(), kit.hermes(), [{"user_id": BE1_UID}, {"user_id": BUDI_UID},
                                                             {"user_id": DODI, "username": "dodi"}])
        assert [m["status"] for m in result["members"]] == ["exists", "reactivated", "created"]
        assert result["failed"] == []
        assert kit.team()["members"][1]["active"] is True and routes_for(kit, "be-budi")
        assert len(channel_posts(discord)) == 1                                 # hanya channel dodi yang dibuat

    def test_dry_run_touches_nothing(self, kit, discord):
        before = kit.members_file.read_text()
        result = ht.onboard_batch(kit.team(), kit.hermes(dry_run=True), self.ENTRIES)
        assert [m["status"] for m in result["members"]] == ["created"] * 3
        assert len({m["member"]["channel_id"] for m in result["members"]}) == 3
        assert discord.calls == [] and kit.calls() == [] and kit.members_file.read_text() == before

    def test_cli_events_protocol_and_exit_codes(self, kit, discord, capsys, tmp_path):
        path = _batch_file(tmp_path, [{"user_id": DODI, "username": "dodi.pratama"},
                                      {"user_id": "599999999999999999", "username": "hantu"}], "batch.json")
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "onboard-batch", "--file", str(path), "--events"))
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert exit_info.value.code == 3
        assert [e["event"] for e in lines] == ["channel", "result", "result", "summary"]
        assert lines[0]["user_id"] == DODI and lines[0]["created"] is True
        ok, failed, summary = lines[1], lines[2], lines[3]
        # bentuk baris `result` sama dengan `onboard --events`
        assert {"status", "user_id", "name", "display_name", "profile", "channel_id", "model", "notes",
                "restart_needed", "live_changed", "failures"} <= set(ok)
        assert (ok["status"], ok["name"], ok["model"], ok["failures"]) == ("created", "dodi-pratama", "td-simple", [])
        assert failed["status"] == "failed" and "tidak ditemukan" in failed["failures"][0]
        assert summary["total"] == 2 and summary["counts"] == {"created": 1, "failed": 1}
        assert summary["failed"][0]["user_id"] == "599999999999999999"

    def test_cli_success_text_and_bad_file(self, kit, discord, capsys, tmp_path):
        path = _batch_file(tmp_path, {"members": [{"discord_user_id": DODI, "username": "dodi"}]})
        ht.main(cli(kit, "onboard-batch", "--file", str(path)))               # sukses: tanpa SystemExit
        out = capsys.readouterr().out
        assert "✓ dodi" in out and "1 entri: 1 created" in out
        for content in ("[]", "satu: dua", "{rusak"):
            bad = tmp_path / "bad.yaml"
            bad.write_text(content, encoding="utf-8")
            with pytest.raises(SystemExit) as exit_info:
                ht.main(cli(kit, "onboard-batch", "--file", str(bad), "--events"))
            event = json.loads(capsys.readouterr().out.splitlines()[-1])
            assert exit_info.value.code == 1 and event["event"] == "error"
        with pytest.raises(SystemExit):
            ht.main(cli(kit, "onboard-batch", "--file", str(tmp_path / "tidak-ada.yaml")))

    def test_failure_outside_member_profiles_rolls_back_the_whole_batch(self, kit, discord, monkeypatch):
        monkeypatch.setenv("FAKE_HERMES_FAIL_CREATE", "be-template")
        before = kit.members_file.read_text()
        result = ht.onboard_batch(kit.team(), kit.hermes(), self.ENTRIES)
        assert [m["status"] for m in result["members"]] == ["failed"] * 3 and len(result["failed"]) == 3
        assert all("dibatalkan" in m["failures"][0] for m in result["members"])
        assert yaml.safe_load(kit.members_file.read_text()) == yaml.safe_load(before)
        assert len(channel_posts(discord)) == 3                               # dibuat, lalu dihapus lagi
        for name in ("dodi-pratama", "eka", "citra"):
            assert channels_named(discord, f"hermes-{name}") == []
            assert not kit.home(f"be-{name}").exists() and routes_for(kit, f"be-{name}") == []


# =========================================================================== sandbox per anggota (RENCANA-SKALA #6)
SCOPED = {"terminal.container_cpu", "terminal.container_memory",
          "compression.threshold_tokens", "compression.proactive_prune_tokens"}


def scoped(kit, profile):
    cfg = kit.config(profile)
    return (cfg["terminal"]["container_cpu"], cfg["terminal"]["container_memory"],
            cfg["compression"]["threshold_tokens"], cfg["compression"]["proactive_prune_tokens"])


class TestSandboxPerMember:
    BUDI_BIG = {**BUDI, "sandbox": {"memory_mb": 4096, "cpu": 2}}

    def test_default_memory_is_1024_and_managed_no_longer_pins_limits(self, kit):
        team = kit.team()
        assert team["sandbox"]["memory_mb"] == 1024 and team["sandbox"]["cpu"] == 1
        policy = ht.managed_config(team)
        assert not {"container_cpu", "container_memory"} & set(policy["terminal"])
        assert not {"threshold_tokens", "proactive_prune_tokens"} & set(policy["compression"])
        assert set(ht.PROFILE_SCOPED_KEYS) == SCOPED
        # sisanya tetap dipin
        assert policy["terminal"]["backend"] == "docker" and policy["terminal"]["docker_volumes"] == []
        assert policy["compression"]["idle_compact_after_seconds"] == 3600

    def test_every_profile_kind_gets_its_own_limits(self, kit):
        kit.write_team(sandbox={"cpu": 1.5, "memory_mb": 2048})
        kit.write_members([BE1, self.BUDI_BIG])
        ht.cmd_apply(kit.team(), kit.hermes())
        assert scoped(kit, "be-budi")[:2] == (2, 4096)                        # pengecualian anggota
        for profile in ("be-be1", "be-template", "team-shared", "team-admin", "default"):
            assert scoped(kit, profile)[:2] == (1.5, 2048), profile           # bawaan tim
        assert all(isinstance(scoped(kit, p)[1], int) for p in ("be-budi", "be-be1", "default"))

    def test_partial_override_falls_back_per_key(self, kit):
        kit.write_members([BE1, {**BUDI, "sandbox": {"memory_mb": 512}}])
        team = kit.team()
        budi = team["members"][1]
        assert budi["sandbox"] == {"memory_mb": 512} and ht.sandbox_limits(team, budi) == (1, 512)
        assert ht.sandbox_limits(team, team["members"][0]) == (1, 1024) and team["members"][0]["sandbox"] == {}
        settings = dict(ht.profile_settings(team, "member", budi))
        assert settings["terminal.container_memory"] == 512 and settings["terminal.container_cpu"] == 1
        # template tidak pernah mewarisi pengecualian orang
        assert dict(ht.profile_settings(team, "template", budi))["terminal.container_memory"] == 1024

    def test_override_survives_save_load_round_trip(self, kit):
        kit.write_members([BE1, {**BUDI, "sandbox": {"memory_mb": "4096", "cpu": 0.5}}])
        team = kit.team()
        ht.save_members(team)
        rows = kit.members_yaml()
        assert rows[1]["sandbox"] == {"memory_mb": 4096, "cpu": 0.5} and "sandbox" not in rows[0]
        assert kit.team()["members"][1]["sandbox"] == {"memory_mb": 4096, "cpu": 0.5}

    @pytest.mark.parametrize("block, key", [
        ({"memory_mb": 128}, "members[1].sandbox.memory_mb"), ({"memory_mb": 70000}, "members[1].sandbox.memory_mb"),
        ({"memory_mb": "banyak"}, "members[1].sandbox.memory_mb"), ({"cpu": 0.1}, "members[1].sandbox.cpu"),
        ({"cpu": 128}, "members[1].sandbox.cpu"), ({"cpu": "dua"}, "members[1].sandbox.cpu"),
        ({"disk_mb": 10}, "members[1].sandbox.disk_mb"), ("4096", "members[1].sandbox"),
    ])
    def test_invalid_override_names_the_key(self, kit, block, key):
        kit.write_members([BE1, {**BUDI, "sandbox": block}])
        with pytest.raises(ht.TeamError) as err:
            kit.team()
        assert key in str(err.value)

    def test_second_apply_issues_zero_config_set_with_overrides(self, kit):
        kit.write_team(llm={"models": {**ht_llm()["models"], "td-normal": {
            "provider": "custom:td", "model": "td-normal",
            "compression_threshold_tokens": 120000, "proactive_prune_tokens": 90000}}})
        kit.write_members([BE1, {**BUDI, "sandbox": {"memory_mb": 4096, "cpu": 0.5}}])
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        assert scoped(kit, "be-budi")[:2] == (0.5, 4096)
        kit.clear_log()
        second = ht.cmd_apply(team, kit.hermes())
        assert kit.calls() == [] and second["failures"] == [] and second["restart_needed"] is False

    def test_set_sandbox_updates_members_yaml_and_reapplies(self, kit):
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        kit.clear_log()
        result = ht.set_sandbox(team, kit.hermes(), "budi", memory_mb="4096", cpu="2")
        assert (result["memory_mb"], result["cpu"], result["applied"]) == (4096, 2, True)
        assert result["override"] == {"memory_mb": 4096, "cpu": 2} and result["failures"] == []
        assert result["worst_case_mb"] == 1024 + 4096 + 2 * 1024
        assert sorted(kit.config_sets()) == [("be-budi", "terminal.container_cpu", "2"),
                                             ("be-budi", "terminal.container_memory", "4096")]
        assert kit.members_yaml()[1]["sandbox"] == {"memory_mb": 4096, "cpu": 2}
        assert team["members"][1]["sandbox"] == {"memory_mb": 4096, "cpu": 2}       # `team` pemanggil ikut segar
        assert scoped(kit, "be-be1")[:2] == (1, 1024)                               # orang lain tidak tersentuh
        kit.clear_log()
        ht.cmd_apply(team, kit.hermes())
        assert kit.calls() == []                                                    # apply sesudahnya: tidak ada yang berubah
        # hanya satu key → key lain dipertahankan; --reset mengembalikan bawaan tim
        ht.set_sandbox(team, kit.hermes(), "budi", cpu=1)                           # sama dengan bawaan → tidak disimpan
        assert kit.members_yaml()[1]["sandbox"] == {"memory_mb": 4096}
        result = ht.set_sandbox(team, kit.hermes(), "budi", reset=True)
        assert result["override"] == {} and "sandbox" not in kit.members_yaml()[1]
        assert scoped(kit, "be-budi")[:2] == (1, 1024)

    def test_set_sandbox_rejects_bad_input_without_touching_anything(self, kit):
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        kit.clear_log()
        before = kit.members_file.read_text()
        for kwargs, needle in (({"memory_mb": 100}, "memory_mb"), ({"cpu": "0"}, "cpu"), ({"cpu": 65}, "cpu"),
                               ({}, "--reset")):
            with pytest.raises(ht.TeamError, match=needle):
                ht.set_sandbox(team, kit.hermes(), "budi", **kwargs)
        with pytest.raises(ht.TeamError, match="tidak-ada"):
            ht.set_sandbox(team, kit.hermes(), "tidak-ada", memory_mb=2048)
        assert kit.calls() == [] and kit.members_file.read_text() == before

    def test_set_sandbox_for_inactive_member_is_stored_but_not_applied(self, kit):
        kit.write_members([BE1, {**BUDI, "active": False}])
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        kit.clear_log()
        result = ht.set_sandbox(team, kit.hermes(), "budi", memory_mb=8192)
        assert result["applied"] is False and kit.config_sets() == []
        assert kit.members_yaml()[1]["sandbox"] == {"memory_mb": 8192} and kit.members_yaml()[1]["active"] is False
        assert result["worst_case_mb"] == 3 * 1024                                  # nonaktif tidak dihitung

    def test_cli_set_sandbox(self, kit, capsys):
        ht.cmd_apply(kit.team(), kit.hermes())
        ht.main(cli(kit, "set-sandbox", "budi", "--memory-mb", "2048", "--cpu", "0.5"))
        out = capsys.readouterr().out
        assert "Sandbox budi (be-budi): 2048 MB, 0.5 CPU (pengecualian)" in out
        assert scoped(kit, "be-budi")[:2] == (0.5, 2048)
        ht.main(cli(kit, "set-sandbox", "budi", "--reset"))
        assert "1024 MB, 1 CPU (bawaan tim)" in capsys.readouterr().out
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "set-sandbox", "budi", "--memory-mb", "12"))
        assert "set-sandbox.memory_mb" in str(exit_info.value)

    def test_check_shows_overrides_and_worst_case_total(self, kit, capsys, monkeypatch, tmp_path):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       16384000 kB\nMemFree:  1 kB\n")
        monkeypatch.setattr(ht, "MEMINFO_PATH", meminfo)
        kit.write_members([BE1, self.BUDI_BIG, {**CITRA_ROW, "active": False, "sandbox": {"memory_mb": 8192}}])
        team = kit.team()
        assert ht.host_memory_mb() == 16000 and ht.sandbox_worst_case_mb(team) == 1024 + 4096 + 2048
        assert ht.sandbox_warnings(team) == []
        ht.main(cli(kit, "check"))
        out = capsys.readouterr().out
        assert "plafon RAM terburuk 7168 MB (2 anggota aktif + shared + admin) dari RAM server 16000 MB" in out
        assert "budi: 4096 MB, 2 CPU (pengecualian)" in out and "be1:" not in out
        assert "citra: 8192 MB, 1 CPU (pengecualian)  [NONAKTIF, tidak dihitung]" in out
        assert "MELEBIHI" not in out and "melebihi RAM server" not in out

    def test_warning_when_worst_case_exceeds_host_ram_and_meminfo_may_be_absent(self, kit, capsys, monkeypatch,
                                                                              tmp_path):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:        4096000 kB\n")
        monkeypatch.setattr(ht, "MEMINFO_PATH", meminfo)
        kit.write_members([BE1, self.BUDI_BIG])
        ht.main(cli(kit, "check"))
        out = capsys.readouterr().out
        assert "MELEBIHI" in out and "Plafon RAM sandbox terburuk 7168 MB melebihi RAM server 4000 MB" in out
        assert out.rstrip().endswith("Konfigurasi valid.")                           # peringatan, bukan kegagalan
        monkeypatch.setattr(ht, "MEMINFO_PATH", tmp_path / "tidak-ada")
        assert ht.host_memory_mb() is None and ht.sandbox_warnings(kit.team()) == []
        ht.main(cli(kit, "check"))
        out = capsys.readouterr().out
        assert "plafon RAM terburuk 7168 MB" in out and "RAM server" not in out
        meminfo.write_text("isi aneh\n")
        monkeypatch.setattr(ht, "MEMINFO_PATH", meminfo)
        assert ht.host_memory_mb() is None

    def test_keys_still_pinned_by_an_old_managed_file_are_skipped_with_a_warning(self, kit, capsys):
        (kit.managed_dir / "config.yaml").write_text(yaml.safe_dump({
            "terminal": {"backend": "docker", "container_memory": 2048, "container_cpu": 1},
            "compression": {"threshold_tokens": 64000, "idle_compact_after_seconds": 3600}}))
        hermes = kit.hermes()
        result = ht.cmd_apply(kit.team(), hermes)
        assert result["failures"] == []
        assert hermes.managed_blocked == SCOPED - {"compression.proactive_prune_tokens"}
        written = {k for _p, k, _v in kit.config_sets()}
        assert written & SCOPED == {"compression.proactive_prune_tokens"}
        assert "pasang ulang" in ht.managed_blocked_warning(hermes)[0].lower()
        ht.main(cli(kit, "apply"))
        assert "masih mem-pin" in capsys.readouterr().out
        # setelah managed baru terpasang, apply berikutnya menulis nilainya
        (kit.managed_dir / "config.yaml").write_text(ht.managed_yaml(kit.team()))
        hermes = kit.hermes()
        ht.cmd_apply(kit.team(), hermes)
        assert hermes.managed_blocked == set() and scoped(kit, "be-budi") == (1, 1024, 64000, 48000)


# =========================================================================== ambang kompresi per model (#9)
class TestCompressionPerModel:
    @staticmethod
    def models(**normal):
        return {"models": {"td-simple": {"provider": "custom:td", "model": "td-simple"},
                           "td-normal": {"provider": "custom:td", "model": "td-normal", **normal}}}

    def test_each_profile_follows_its_model_and_falls_back_to_scale(self, kit):
        kit.write_team(llm=self.models(compression_threshold_tokens=120000, proactive_prune_tokens="90000"),
                       scale={"compression_threshold_tokens": 50000, "proactive_prune_tokens": 30000})
        team = kit.team()
        assert team["llm"]["models"]["td-normal"]["proactive_prune_tokens"] == 90000
        assert ht.compression_limits(team, "td-normal") == (120000, 90000)
        assert ht.compression_limits(team, "td-simple") == (50000, 30000) == ht.compression_limits(team)
        ht.cmd_apply(team, kit.hermes())
        assert scoped(kit, "be-be1")[2:] == (120000, 90000)                    # be1 → td-normal
        assert scoped(kit, "team-admin")[2:] == (120000, 90000)                # admin_model: td-normal
        for profile in ("be-budi", "be-template", "team-shared", "default"):   # td-simple → scale
            assert scoped(kit, profile)[2:] == (50000, 30000), profile

    def test_without_llm_block_scale_defaults_apply_everywhere(self, kit):
        kit.write_team(llm=None)
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        for profile in ("be-be1", "be-budi", "be-template", "team-shared", "team-admin", "default"):
            assert scoped(kit, profile) == (1, 1024, 64000, 48000), profile

    def test_set_model_reapplies_thresholds(self, kit):
        kit.write_team(llm=self.models(compression_threshold_tokens=120000, proactive_prune_tokens=90000))
        team = kit.team()
        ht.cmd_apply(team, kit.hermes())
        assert scoped(kit, "be-budi")[2:] == (64000, 48000)
        kit.clear_log()
        ht.set_model(team, kit.hermes(), "budi", "td-normal")
        assert scoped(kit, "be-budi")[2:] == (120000, 90000)
        assert {k for _p, k, _v in kit.config_sets()} >= {"compression.threshold_tokens",
                                                          "compression.proactive_prune_tokens", "model.default"}
        kit.clear_log()
        ht.cmd_apply(team, kit.hermes())
        assert kit.calls() == []
        ht.set_model(team, kit.hermes(), "be1", "td-simple")                    # kembali ke bawaan scale
        assert scoped(kit, "be-be1")[2:] == (64000, 48000)
        ht.set_model(team, kit.hermes(), "lobby", "td-normal")
        assert scoped(kit, "default")[2:] == (120000, 90000)
        ht.set_model(team, kit.hermes(), "shared", "td-normal")
        assert scoped(kit, "team-shared")[2:] == (120000, 90000)

    @pytest.mark.parametrize("normal, needle", [
        ({"compression_threshold_tokens": 0}, "llm.models.td-normal.compression_threshold_tokens"),
        ({"compression_threshold_tokens": "besar"}, "llm.models.td-normal.compression_threshold_tokens"),
        ({"proactive_prune_tokens": -5}, "llm.models.td-normal.proactive_prune_tokens"),
        ({"compression_threshold_tokens": 80000, "proactive_prune_tokens": 80000},
         "llm.models.td-normal.proactive_prune_tokens"),
        ({"compression_threshold_tokens": 40000}, "llm.models.td-normal.compression_threshold_tokens"),   # prune scale 48000
        ({"proactive_prune_tokens": 70000}, "llm.models.td-normal.proactive_prune_tokens"),                # threshold scale 64000
    ])
    def test_invalid_values_name_the_key(self, kit, normal, needle):
        kit.write_team(llm=self.models(**normal))
        with pytest.raises(ht.TeamError) as err:
            kit.team()
        assert needle in str(err.value)

    def test_single_threshold_is_fine_when_the_pair_stays_ordered(self, kit):
        kit.write_team(llm=self.models(compression_threshold_tokens=200000))
        assert ht.compression_limits(kit.team(), "td-normal") == (200000, 48000)
        kit.write_team(llm=self.models(proactive_prune_tokens=20000), scale={"proactive_prune_tokens": 0})
        assert ht.compression_limits(kit.team(), "td-normal") == (64000, 20000)
        assert ht.compression_limits(kit.team(), "td-simple") == (64000, 0)


# =========================================================================== laporan pemakaian terjadwal (kontrak)
class TestUsageReportConfig:
    def test_defaults(self, kit):
        usage = kit.team()["usage"]
        assert usage == {"timezone": "", "report_channel_id": "", "reports": {"daily": "", "weekly": "", "monthly": ""}}

    def test_valid_values_are_normalised(self, kit):
        kit.write_team(usage={"timezone": "Asia/Makassar", "report_channel_id": 400000000000000002,
                              "reports": {"daily": "8:00", "weekly": "23:59", "monthly": False}})
        usage = kit.team()["usage"]
        assert usage["report_channel_id"] == "400000000000000002" and usage["timezone"] == "Asia/Makassar"
        assert usage["reports"] == {"daily": "08:00", "weekly": "23:59", "monthly": ""}
        kit.write_team(usage={"report_channel_id": fd.CH_ADMIN, "reports": None})      # channel admin boleh
        assert kit.team()["usage"]["reports"] == {"daily": "", "weekly": "", "monthly": ""}

    @pytest.mark.parametrize("usage, needle", [
        ({"report_channel_id": "bukan-id"}, "usage.report_channel_id"),
        ({"report_channel_id": BE1_CH}, "usage.report_channel_id"),
        ({"reports": ["08:00"]}, "usage.reports"),
        ({"reports": {"hourly": "08:00"}}, "usage.reports.hourly"),
        ({"reports": {"daily": "24:00"}}, "usage.reports.daily"),
        ({"reports": {"weekly": "08:60"}}, "usage.reports.weekly"),
        ({"reports": {"monthly": "jam delapan"}}, "usage.reports.monthly"),
        ({"reports": {"daily": 630}}, "usage.reports.daily"),          # YAML: 10:30 tanpa kutip → 630
        ({"reports": {"daily": True}}, "usage.reports.daily"),
    ])
    def test_invalid_values_name_the_key(self, kit, usage, needle):
        kit.write_team(usage=usage)
        with pytest.raises(ht.TeamError) as err:
            kit.team()
        assert needle in str(err.value)

    def test_private_channel_message_names_the_member(self, kit):
        kit.write_team(usage={"report_channel_id": BE1_CH})
        with pytest.raises(ht.TeamError, match="channel pribadi anggota 'be1'"):
            kit.team()

    def test_example_files_load(self, kit, tmp_path):
        from pathlib import Path
        root = Path(ht.__file__).resolve().parent
        team = ht.load_team(root / "team.example.yaml", root / "members.example.yaml")
        assert team["sandbox"]["memory_mb"] == 1024
        assert team["usage"]["reports"] == {"daily": "", "weekly": "", "monthly": ""}
        assert team["warnings"] == []                    # contoh tidak boleh memuat key yang tidak dikenal


# =========================================================================== audit kepraktisan operator
REAL = {  # ID berbentuk snowflake asli (bukan pola ID contoh)
    "guild_id": "912345678901234567",
    "roles": {"member": "923456789012345678", "admin": "934567890123456789", "entry": "945678901234567890"},
    "admins": ["956789012345678901"], "category_id": "967890123456789012",
    "shared": {"profile": "team-shared", "channel_id": "978901234567890123"},
    "admin": {"profile": "team-admin", "channel_id": "989012345678901234"},
}
REAL_MEMBER = {"name": "andi", "display_name": "Andi", "discord_user_id": "811111111111111111",
               "channel_id": "822222222222222222"}


class TestUnknownKeys:                                                                # butir 1
    def test_typos_everywhere_are_collected_with_suggestions(self, kit):
        kit.write_team(scale={"max_concurent_sessions": 3, "daily_token_bugdet": 9}, secrets={"max_key": 5},
                       server_admin={"allow_moderasi": False}, sandbox={"memori_mb": 512},
                       roles={"entri": fd.ROLE_ENTRY}, shared={"auto_skill": ["x"]}, threads={"personel": False},
                       usage={"time_zone": "UTC"}, llm={"defualt_model": "td-normal"}, tema_gelap=True,
                       channel_prefiks="x-")
        kit.write_members([{**BE1, "modle": "td-simple", "actve": False}], models={"shared": "td-normal"})
        team = kit.team()                                           # tetap termuat: peringatan, bukan error
        text = "\n".join(team["warnings"])
        for key, hint in (("scale.max_concurent_sessions", "max_concurrent_sessions"),
                          ("scale.daily_token_bugdet", "daily_token_budget"), ("secrets.max_key", "max_keys"),
                          ("server_admin.allow_moderasi", "allow_moderation"), ("sandbox.memori_mb", "memory_mb"),
                          ("roles.entri", "entry"), ("shared.auto_skill", "auto_skills"),
                          ("threads.personel", "personal"), ("usage.time_zone", "timezone"),
                          ("llm.defualt_model", "default_model"), ("channel_prefiks", "channel_prefix"),
                          ("members[0] (be1).modle", "model"), ("members[0] (be1).actve", "active")):
            line = next((w for w in team["warnings"] if f"'{key}'" in w), "")
            assert line and f"maksudnya '{hint}'?" in line and "DIABAIKAN" in line, (key, text)
        assert any("'tema_gelap'" in w and "pilihan:" in w for w in team["warnings"])   # tanpa kandidat mirip
        assert sum("members.yaml" in w for w in team["warnings"]) == 2
        # nilai yang salah ketik memang tidak dipakai (itu sebabnya harus terlihat)
        assert team["scale"]["max_concurrent_sessions"] == 8 and team["members"][0]["active"] is True

    def test_llm_model_block_and_clean_config(self, kit):
        assert kit.team()["warnings"] == []
        data = kit.write_team()
        data["llm"]["models"]["td-normal"]["compresion_threshold_tokens"] = 9
        kit.team_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        assert ["compression_threshold_tokens" in w and "llm.models.td-normal" in w
                for w in kit.team()["warnings"]] == [True]

    def test_existing_hard_errors_stay_hard(self, kit):
        kit.write_team(onboarding={"enabeld": True})
        with pytest.raises(ht.TeamError, match="onboarding.enabeld"):
            kit.team()
        kit.write_team(usage={"reports": {"dayly": "08:00"}})
        with pytest.raises(ht.TeamError, match="usage.reports.dayly"):
            kit.team()

    def test_check_apply_and_status_print_them(self, kit, capsys):
        kit.write_team(scale={"max_concurent_sessions": 3})
        for command in ("check", "status", "apply"):
            ht.main(cli(kit, command))
            out = capsys.readouterr().out
            assert "Peringatan" in out and "scale.max_concurent_sessions" in out, command
            assert "maksudnya 'max_concurrent_sessions'?" in out


class TestPlaceholderIds:                                                             # butir 2
    def test_load_team_still_accepts_the_example(self):
        team = ht.load_team(ht.KIT_DIR / "team.example.yaml", ht.KIT_DIR / "members.example.yaml")
        found = ht.placeholder_ids(team)
        assert [f.split(" ")[0] for f in found] == ["guild_id", "roles.member", "roles.admin", "roles.entry",
                                                    "category_id", "shared.channel_id", "admin.channel_id",
                                                    "admins[0]"]

    def test_pattern_matches_shipped_ids_only(self):
        for sample in ("100000000000000001", "200000000000000003", "300000000000000001", "400000000000000009",
                       "500000000000000002", "600000000000000001"):
            assert ht.PLACEHOLDER_ID_RE.match(sample), sample
        for real in ("912345678901234567", "710000000000000001", "1234567890123456789", "999000000000000001"):
            assert not ht.PLACEHOLDER_ID_RE.match(real), real

    def test_check_exits_nonzero_and_lists_keys(self, kit, capsys):
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "check", placeholders=False))
        message = str(exit_info.value.code)
        assert exit_info.value.code not in (0, None) and "ID contoh" in message
        for key in ("guild_id (100000000000000001)", "roles.member", "admins[0]", "shared.channel_id",
                    "members.yaml be1.discord_user_id (500000000000000001)", "members.yaml budi.channel_id"):
            assert key in message, key
        assert "Konfigurasi valid" not in capsys.readouterr().out

    def test_apply_refuses_without_touching_hermes(self, kit, capsys):
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "apply", placeholders=False))
        assert "apply ditolak" in str(exit_info.value.code) and "guild_id" in str(exit_info.value.code)
        assert kit.calls() == [] and not (kit.state / "roster.json").exists()
        with pytest.raises(SystemExit):                                  # dry-run pun ditolak
            ht.main(cli(kit, "apply", "--dry-run", placeholders=False))

    def test_flag_and_env_allow_them_for_tests(self, kit, capsys, monkeypatch):
        ht.main(cli(kit, "apply"))
        assert kit.creates()
        monkeypatch.setenv(ht.ALLOW_PLACEHOLDERS_ENV, "1")
        ht.main(cli(kit, "check", placeholders=False))
        assert "Konfigurasi valid." in capsys.readouterr().out

    def test_real_ids_pass_and_one_leftover_is_named(self, kit, capsys):
        kit.write_team(**REAL)
        kit.write_members([REAL_MEMBER])
        assert ht.placeholder_ids(kit.team()) == []
        ht.main(cli(kit, "check", placeholders=False))
        assert capsys.readouterr().out.rstrip().endswith("Konfigurasi valid.")
        kit.write_team(**{**REAL, "usage": {"report_channel_id": "400000000000000002"}})
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "check", placeholders=False))
        assert "usage.report_channel_id (400000000000000002)" in str(exit_info.value.code)
        assert "guild_id" not in str(exit_info.value.code)


class TestExampleMembers:                                                             # butir 3
    def test_example_ships_no_members(self):
        path = ht.KIT_DIR / "members.example.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw == {"members": []}
        team = ht.load_team(ht.KIT_DIR / "team.example.yaml", path)
        assert team["members"] == [] and ht.active_members(team) == []
        assert [r["profile"] for r in ht.build_routes(team)] == ["team-shared", "team-admin"]

    def test_commented_example_is_valid_once_uncommented(self, tmp_path):
        text = (ht.KIT_DIR / "members.example.yaml").read_text(encoding="utf-8")
        start = text.index("# members:\n")
        block = "\n".join(line[2:] if line.startswith("# ") else line.lstrip("#")
                          for line in text[start:].splitlines() if not line.startswith("# Opsional"))
        members = tmp_path / "members.yaml"
        members.write_text(block, encoding="utf-8")
        team = ht.load_team(ht.KIT_DIR / "team.example.yaml", members)
        assert [(m["name"], m["active"]) for m in team["members"]] == [("andi", True), ("budi", True), ("citra", False)]
        assert team["members"][0]["model"] == "td-normal" and team["members"][1]["sandbox"] == {"memory_mb": 4096, "cpu": 2}
        assert team["model_overrides"] == {"shared": "td-simple", "admin": "td-normal", "lobby": "td-simple"}
        assert team["warnings"] == []


class TestKitPaths:                                                                   # butir 4
    def test_resolution_order(self, tmp_path, monkeypatch):
        import kit_paths
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("HERMES_TEAM_DIR", raising=False)
        base = tmp_path / "home" / ".hermes-team"
        assert kit_paths.team_dir() == base and kit_paths.team_file() == base / "team.yaml"
        assert kit_paths.members_file() == base / "members.yaml" and kit_paths.state_dir() == base / "state"
        assert kit_paths.ledger_path() == base / "state" / "usage.db"
        monkeypatch.setenv("HERMES_TEAM_DIR", str(tmp_path / "lain"))
        assert kit_paths.team_file() == tmp_path / "lain" / "team.yaml"
        assert kit_paths.ledger_path() == tmp_path / "lain" / "state" / "usage.db"
        explicit = tmp_path / "x" / "tim.yaml"                       # --config menang atas env
        assert kit_paths.team_file(explicit) == explicit
        assert kit_paths.members_file(explicit) == tmp_path / "x" / "members.yaml"
        assert kit_paths.state_dir(explicit) == tmp_path / "x" / "state"
        assert ht.default_team_file() == tmp_path / "lain" / "team.yaml"

    def test_cwd_team_yaml_is_not_picked_up_and_a_notice_names_both(self, kit, tmp_path, monkeypatch, capsys):
        import kit_paths
        elsewhere = tmp_path / "proyek"
        elsewhere.mkdir()
        raw = yaml.safe_load(kit.team_file.read_text(encoding="utf-8"))
        (elsewhere / "team.yaml").write_text(yaml.safe_dump({**raw, "team_name": "SALAH"}), encoding="utf-8")
        monkeypatch.chdir(elsewhere)
        assert ht.default_team_file() == kit.team_file              # HERMES_TEAM_DIR, bukan ./team.yaml
        argv = ["check", "--hermes-bin", str(kit.bin), "--hermes-root", str(kit.root), "--allow-placeholders"]
        ht.main(argv)
        captured = capsys.readouterr()
        assert "Tim Backend" in captured.out and "SALAH" not in captured.out
        notice = [line for line in captured.err.splitlines() if line.startswith("catatan:")]
        assert len(notice) == 1 and str(kit.team_file) in notice[0]
        assert f"--config {elsewhere / 'team.yaml'}" in notice[0] and f"HERMES_TEAM_DIR={elsewhere}" in notice[0]
        ht.main(["check", "-c", "team.yaml", "--hermes-bin", str(kit.bin), "--allow-placeholders"])
        captured = capsys.readouterr()                               # dipilih eksplisit → dipakai, tanpa catatan
        assert "Tim SALAH" in captured.out and "catatan:" not in captured.err
        monkeypatch.chdir(kit.team_dir)                              # berkas yang sama → tanpa catatan
        assert kit_paths.cwd_notice(kit.team_file) is None
        monkeypatch.chdir(tmp_path)                                  # tidak ada ./team.yaml → tanpa catatan
        assert kit_paths.cwd_notice(kit.team_file) is None

    def test_state_follows_the_resolved_file_not_the_cwd(self, kit, tmp_path, monkeypatch):
        elsewhere = tmp_path / "proyek"
        elsewhere.mkdir()
        (elsewhere / "team.yaml").write_text(kit.team_file.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.chdir(elsewhere)
        ht.main(["apply", "--hermes-bin", str(kit.bin), "--hermes-root", str(kit.root), "--allow-root",
                 "--allow-placeholders"])
        assert (kit.state / "roster.json").exists() and not (elsewhere / "state").exists()


class TestManagedDiffAndInstall:                                                      # butir 7
    def test_diff_reports_missing_changed_extra_and_moved(self, kit):
        team = kit.team()
        target = kit.managed_dir / "config.yaml"
        assert ht.managed_diff(team)["missing_file"] is True
        target.write_text(ht.managed_yaml(team))
        diff = ht.managed_diff(team)
        assert ht.managed_diff_clean(diff) and diff["path"] == str(target)
        data = yaml.safe_load(target.read_text())
        data["agent"]["max_turns"] = 5
        del data["goals"]
        data["terminal"]["container_memory"] = 2048
        data["display"]["tema"] = "gelap"
        target.write_text(yaml.safe_dump(data))
        diff = ht.managed_diff(team)
        assert diff["changed"] == ["agent.max_turns"] and diff["missing"] == ["goals.max_turns"]
        assert diff["moved"] == ["terminal.container_memory"]
        assert diff["extra"] == ["display.tema", "terminal.container_memory"]
        assert not ht.managed_diff_clean(diff)
        target.write_text(": bukan yaml [")
        assert ht.managed_diff(team)["unreadable"]

    def test_cli_diff_exit_codes_and_output(self, kit, capsys):
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "managed", "--diff"))
        out = capsys.readouterr().out
        assert exit_info.value.code == 1 and "belum ada" in out and "managed --install" in out and "sudo" in out
        (kit.managed_dir / "config.yaml").write_text(ht.managed_yaml(kit.team()))
        ht.main(cli(kit, "managed", "--diff"))
        assert "sama dengan kebijakan" in capsys.readouterr().out
        kit.write_team(scale={"agent_max_turns": 30})
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "managed", "--diff"))
        assert exit_info.value.code == 1 and "berbeda: agent.max_turns" in capsys.readouterr().out

    def test_install_requires_root_and_prints_exact_sudo_command(self, kit, monkeypatch, capsys):
        team = kit.team()
        with pytest.raises(ht.TeamError) as err:
            ht.install_managed(team, euid=1000)
        text = str(err.value)
        assert "harus sebagai root" in text and "sudo" in text and "managed --install" in text
        assert f"-c {kit.team_file}" in text and str(ht.KIT_DIR / "hermes_team.py") in text
        assert not (kit.managed_dir / "config.yaml").exists()
        monkeypatch.setattr(ht.os, "geteuid", lambda: 1000)
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(kit, "managed", "--install"))
        assert "sudo" in str(exit_info.value.code) and not (kit.managed_dir / "config.yaml").exists()

    def test_install_writes_atomically_with_0644(self, kit, monkeypatch, capsys):
        team = kit.team()
        target = kit.managed_dir / "sub" / "config.yaml"
        path = ht.install_managed(team, target_dir=target.parent, euid=0)
        assert path == target and stat.S_IMODE(target.stat().st_mode) == 0o644
        assert target.read_text() == ht.managed_yaml(team)
        assert [p.name for p in target.parent.iterdir()] == ["config.yaml"]          # tanpa berkas sementara
        assert ht.install_managed(team, target_dir=kit.managed_dir / "kering", euid=0, dry_run=True)
        assert not (kit.managed_dir / "kering").exists()
        monkeypatch.setattr(ht.os, "geteuid", lambda: 0)
        ht.main(cli(kit, "managed", "--install"))
        assert "ditulis (0644)" in capsys.readouterr().out
        assert ht.managed_diff_clean(ht.managed_diff(team))
        ht.main(cli(kit, "managed"))                                                 # tanpa flag: tetap mencetak YAML
        assert yaml.safe_load(capsys.readouterr().out) == yaml.safe_load(ht.managed_yaml(team))
        with pytest.raises(SystemExit):                                              # saling eksklusif
            ht.main(cli(kit, "managed", "--diff", "--install"))


class TestRemoveMember:                                                               # butir 9
    @pytest.fixture
    def applied(self, kit):
        ht.cmd_apply(kit.team(), kit.hermes())
        kit.write_members([BE1, {**BUDI, "active": False}])
        ht.cmd_apply(kit.team(), kit.hermes(), only=[], host_only=True)
        kit.clear_log()
        return kit

    def test_active_member_must_be_offboarded_first(self, applied):
        with pytest.raises(ht.TeamError) as err:
            ht.remove_member(applied.team(), applied.hermes(), "be1")
        assert "masih AKTIF" in str(err.value) and "set-active be1 off" in str(err.value)
        with pytest.raises(ht.TeamError, match="tidak ada di members.yaml"):
            ht.remove_member(applied.team(), applied.hermes(), "hantu")
        assert [m["name"] for m in applied.members_yaml()] == ["be1", "budi"]

    def test_default_removes_entry_only_and_lists_what_is_kept(self, applied):
        result = ht.remove_member(applied.team(), applied.hermes(), "budi")
        assert [m["name"] for m in applied.members_yaml()] == ["be1"]
        assert applied.home("be-budi").is_dir()                                       # profile dibiarkan
        assert not [c for c in applied.calls() if c[:2] == ["profile", "delete"]]
        kept = "\n".join(result["kept"])
        assert "profile be-budi" in kept and f"channel Discord {BUDI_CH}" in kept
        assert 'label=hermes-profile=be-budi' in kept and "docker rm -f" in kept and "docker volume rm" in kept
        assert BUDI_UID not in applied.roster()["inactive_users"]                     # roster ikut diselaraskan
        assert any("members.yaml" in line for line in result["done"])

    def test_dry_run_changes_nothing(self, applied):
        result = ht.remove_member(applied.team(), applied.hermes(dry_run=True), "budi", delete_profile=True,
                                  delete_channel=True)
        assert result["dry_run"] and result["done"] == []
        assert any("hermes profile delete" in line for line in result["will_delete"])
        assert any(BUDI_CH in line for line in result["will_delete"])
        assert [m["name"] for m in applied.members_yaml()] == ["be1", "budi"] and applied.home("be-budi").is_dir()

    def test_delete_profile_and_channel(self, applied, discord, api):
        assert BUDI_CH in discord.channels
        result = ht.remove_member(applied.team(), applied.hermes(), "budi", delete_profile=True,
                                  delete_channel=True, api=api)
        assert ["profile", "delete", "be-budi", "--yes"] in applied.calls()
        assert not applied.home("be-budi").exists() and BUDI_CH not in discord.channels
        assert BE1_CH in discord.channels and applied.home("be-be1").is_dir()         # orang lain utuh
        done = "\n".join(result["done"])
        assert "profile be-budi dihapus" in done and f"channel Discord {BUDI_CH} dihapus" in done
        assert not any("profile be-budi" in line and "hapus:" in line for line in result["kept"])
        assert any("docker" in line for line in result["kept"])                       # sandbox tetap disebut

    def test_failures_are_reported_not_hidden(self, applied, discord, api, monkeypatch):
        monkeypatch.setenv("FAKE_HERMES_FAIL_DELETE", "be-budi")
        discord.fault("DELETE", f"/channels/{BUDI_CH}", status=403, body={"message": "Missing Permissions", "code": 50013})
        result = ht.remove_member(applied.team(), applied.hermes(), "budi", delete_profile=True,
                                  delete_channel=True, api=api)
        notes = "\n".join(result["notes"])
        assert "GAGAL dihapus" in notes and "hermes profile delete be-budi" in notes
        assert "TIDAK terhapus" in notes and BUDI_CH in discord.channels
        assert [m["name"] for m in applied.members_yaml()] == ["be1"]                 # entri tetap terhapus

    def test_shared_skill_registry_references_are_removed(self, applied, monkeypatch, tmp_path):
        kit = applied
        kit.fake_scanner(monkeypatch, tmp_path, "safe")
        kit.write_members([BE1, BUDI])
        kit.make_skill("be-budi", "deploy")
        kit.make_skill("be-be1", "lint")
        ht.share_skill(kit.team(), kit.hermes(), "deploy", "budi", ["all"])
        ht.share_skill(kit.team(), kit.hermes(), "lint", "be1", ["budi"])
        registry = ht.shared_skill_registry(kit.team())["skills"]
        assert registry["deploy"]["origin_member"] == "budi" and registry["lint"]["targets"] == ["budi"]
        assert (kit.home("be-be1") / "skills" / "shared" / "deploy").is_dir()
        kit.write_members([BE1, {**BUDI, "active": False}])
        plan = ht.remove_member_plan(kit.team(), kit.hermes(), "budi")
        assert plan["origin_skills"] == ["deploy"] and plan["target_skills"] == ["lint"]
        ht.remove_member(kit.team(), kit.hermes(), "budi")
        registry = ht.shared_skill_registry(kit.team())["skills"]
        assert "deploy" not in registry and registry["lint"]["targets"] == []
        assert not (kit.home("be-be1") / "skills" / "shared" / "deploy").exists()
        assert not (kit.state / "shared-skills" / "deploy").exists()
        assert (kit.home("be-be1") / "skills" / "lint").is_dir()                      # skill asli be1 utuh

    def test_holds_the_team_lock(self, applied, monkeypatch):
        depths = []
        real = ht.save_members
        monkeypatch.setattr(ht, "save_members", lambda team: (depths.append(ht._lock_depth()), real(team))[1])
        ht.remove_member(applied.team(), applied.hermes(), "budi")
        assert depths and min(depths) >= 1

    def test_cli_shows_plan_and_needs_confirmation(self, applied, capsys, monkeypatch):
        monkeypatch.setattr(ht.sys, "stdin", io.StringIO(""))                         # bukan TTY, tanpa --yes
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(applied, "remove-member", "budi", "--delete-profile"))
        out = capsys.readouterr().out
        assert "butuh konfirmasi" in str(exit_info.value.code) and "--yes" in str(exit_info.value.code)
        assert "AKAN DIHAPUS" in out and "TIDAK dihapus" in out and "hermes profile delete" in out
        assert [m["name"] for m in applied.members_yaml()] == ["be1", "budi"]
        ht.main(cli(applied, "remove-member", "budi", "--dry-run"))
        assert "(dry-run) tidak ada yang diubah" in capsys.readouterr().out
        assert [m["name"] for m in applied.members_yaml()] == ["be1", "budi"]
        ht.main(cli(applied, "remove-member", "budi", "--delete-profile", "--yes"))
        out = capsys.readouterr().out
        assert "Sudah dilakukan" in out and "profile be-budi dihapus" in out and "docker volume rm" in out
        assert [m["name"] for m in applied.members_yaml()] == ["be1"] and not applied.home("be-budi").exists()
        with pytest.raises(SystemExit) as exit_info:
            ht.main(cli(applied, "remove-member", "be1", "--yes"))
        assert "masih AKTIF" in str(exit_info.value.code)


class TestVersionLabels:                                                              # butir 11
    def test_version_flag_prints_kit_version(self, capsys):
        import kit_base
        with pytest.raises(SystemExit) as exit_info:
            ht.main(["--version"])
        assert exit_info.value.code == 0
        assert capsys.readouterr().out.strip() == f"hermes-team-kit {kit_base.KIT_VERSION}"

    def test_no_stale_labels_in_help_or_example_header(self, capsys):
        import kit_base
        with pytest.raises(SystemExit):
            ht.main(["--help"])
        text = capsys.readouterr().out
        assert f"Versi kit: {kit_base.KIT_VERSION}" in text and "remove-member" in text
        header = (ht.KIT_DIR / "team.example.yaml").read_text(encoding="utf-8").split("\n\n")[0]
        for stale in ("kit v3", "3.0.0", "v3)"):
            assert stale not in text and stale not in header and stale not in (ht.__doc__ or "")
        assert "hermes_team.py --version" in header

    def test_changelog_documents_the_current_version(self):
        import kit_base
        text = (ht.KIT_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
        assert f"## [{kit_base.KIT_VERSION}]" in text and "## [Belum dirilis]" in text
        assert text.index("## [Belum dirilis]") < text.index(f"## [{kit_base.KIT_VERSION}]")


# =========================================================================== tests/selftest.sh (butir 10)
SELFTEST = ht.KIT_DIR / "tests" / "selftest.sh"


def _fake_hermes_source(root):
    """Direktori sumber Hermes tiruan: cukup supaya `import gateway, toolsets, hermes_cli.*, discord` lolos."""
    for package in ("gateway", "hermes_cli", "discord"):
        (root / package).mkdir(parents=True)
        (root / package / "__init__.py").write_text("", encoding="utf-8")
    for module in ("toolsets.py", "hermes_cli/plugins.py", "hermes_cli/config_defaults.py", "hermes_cli/commands.py"):
        (root / module).write_text("", encoding="utf-8")
    return root


def _selftest(tmp_path, *args, launcher=None, **env):
    import sys
    base = {"PATH": f"{tmp_path / 'kosong'}:/usr/bin:/bin", "HOME": str(tmp_path / "rumah"), "LC_ALL": "C.UTF-8"}
    (tmp_path / "kosong").mkdir(exist_ok=True)
    if launcher is not None:
        base["HERMES_BIN"] = str(launcher)
    base.update(env)
    return subprocess.run(["bash", str(SELFTEST), *args], env=base, text=True, capture_output=True, timeout=600,
                          cwd=str(tmp_path)), sys.executable


def _detected(proc):
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)



def _needs_python_without_hermes():
    """Uji ini meniru instalasi yang Python-nya ketemu tetapi sumber Hermes tidak. Itu tidak bisa ditiru bila
    Python penguji sendiri adalah Python milik Hermes (job integrasi CI): `import gateway` selalu berhasil."""
    import importlib.util
    if importlib.util.find_spec("gateway") is not None:
        pytest.skip("Python penguji sudah memuat Hermes; jalur 'sumber Hermes tidak ditemukan' diuji di job unit")


class TestSelftestScript:
    def test_bash_syntax(self):
        assert subprocess.run(["bash", "-n", str(SELFTEST)], capture_output=True, text=True).returncode == 0
        text = SELFTEST.read_text(encoding="utf-8")
        assert "--system-site-packages" in text and "pip install" in text
        assert "KIT_REQUIRE_HERMES=1" in text and "exit 3" in text

    def test_shell_wrapper_launcher_with_spaces_and_symlink(self, tmp_path):
        import shlex
        import sys
        source = _fake_hermes_source(tmp_path / "sumber hermes")
        script = f"import os, re, sys\nsys.path.insert(0, {str(source)!r})\nimport hermes_bootstrap\n"
        real = tmp_path / "pasang" / "hermes"
        real.parent.mkdir()
        # bentuk persis upstream hermes_cli/_launchers.py::_write_shell
        real.write_text(f'#!/bin/sh\nexec {shlex.join([sys.executable, "-I", "-c", script])} "$@"\n', encoding="utf-8")
        real.chmod(0o755)
        link = tmp_path / "bin" / "hermes"
        link.parent.mkdir()
        link.symlink_to(real)
        proc, python = _selftest(tmp_path, "--detect", launcher=link)
        found = _detected(proc)
        assert proc.returncode == 0, proc.stderr
        assert found["launcher"] == str(real) and found["python"] == python
        assert found["source"] == str(source) and found["pythonpath"] == str(source)
        assert found["integration"] == "run"

    def test_shebang_launcher_needs_source_dir_and_honours_overrides(self, tmp_path):
        import sys
        _needs_python_without_hermes()
        source = _fake_hermes_source(tmp_path / "src")
        launcher = tmp_path / "hermes"
        launcher.write_bytes(f"#!{sys.executable} -I\n".encode() + b"PK\x03\x04\x00\x00biner zip\x00\n")
        launcher.chmod(0o755)
        proc, python = _selftest(tmp_path, "--detect", launcher=launcher)
        found = _detected(proc)
        assert found["python"] == python and found["source"] == "" and found["integration"] == "skip"
        assert "HERMES_SRC" in found["reason"]
        proc, _ = _selftest(tmp_path, "--detect", launcher=launcher, HERMES_SRC=str(source))
        assert _detected(proc)["integration"] == "run" and _detected(proc)["pythonpath"] == str(source)
        # lokasi pasang bawaan installer: $HERMES_HOME/hermes-agent
        home = tmp_path / "hh"
        home.mkdir()
        (home / "hermes-agent").symlink_to(source)
        proc, _ = _selftest(tmp_path, "--detect", launcher=launcher, HERMES_HOME=str(home))
        assert _detected(proc)["source"] == str(home / "hermes-agent") and _detected(proc)["integration"] == "run"
        # HERMES_PYTHON menang atas launcher; nilai yang salah → kode 2
        proc, _ = _selftest(tmp_path, "--detect", launcher=tmp_path / "tidak-ada", HERMES_PYTHON=python,
                            HERMES_SRC=str(source))
        assert _detected(proc)["python"] == python and _detected(proc)["integration"] == "run"
        proc, _ = _selftest(tmp_path, "--detect", HERMES_PYTHON=str(tmp_path / "bukan-python"))
        assert proc.returncode == 2 and "HERMES_PYTHON" in proc.stderr

    def test_skip_is_loud_and_exits_3_unless_allowed(self, tmp_path):
        import sys
        _needs_python_without_hermes()
        launcher = tmp_path / "hermes"
        launcher.write_text(f"#!{sys.executable} -I\n", encoding="utf-8")       # python ketemu, sumber tidak
        launcher.chmod(0o755)
        proc, _ = _selftest(tmp_path, "tests/test_usage_report.py", "-q", launcher=launcher)
        last = proc.stdout.strip().splitlines()
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "passed" in proc.stdout and "uji integrasi DILEWATI" in proc.stdout
        assert any(line.startswith("  Alasan:") for line in last[-4:]) and "PERINGATAN" in proc.stderr
        proc, _ = _selftest(tmp_path, "--allow-skip", "tests/test_usage_report.py", "-q", launcher=launcher)
        assert proc.returncode == 0 and "DILEWATI" in proc.stdout and "--allow-skip" in proc.stdout

    def test_integration_run_is_reported_at_the_end_and_failures_win(self, tmp_path):
        source = _fake_hermes_source(tmp_path / "src")
        import sys
        proc, python = _selftest(tmp_path, "tests/test_usage_report.py", "-q", HERMES_PYTHON=sys.executable,
                                 HERMES_SRC=str(source))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "Hermes terdeteksi" in proc.stdout and "DILEWATI" not in proc.stdout + proc.stderr
        assert proc.stdout.strip().splitlines()[-1].startswith("✓ Lulus. Uji integrasi: DIJALANKAN")
        assert not (ht.KIT_DIR / "venv").exists()                    # tidak ada venv/pip di luar folder sementara
        proc, _ = _selftest(tmp_path, "tests/tidak-ada-berkas-ini.py", "-q", HERMES_PYTHON=sys.executable,
                            HERMES_SRC=str(source))
        assert proc.returncode not in (0, 3) and "✗ GAGAL" in proc.stdout
