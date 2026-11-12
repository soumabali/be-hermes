"""Uji onboarding/offboarding otomatis plugin team-guard dengan Discord tiruan dan `hermes_team.py`
tiruan (subprocess sungguhan yang berbicara protokol `--events`).

Jalankan:  python -m pytest tests/test_onboarding.py -q
"""
from __future__ import annotations

import asyncio
import json
import types
from datetime import timedelta

import pytest

import plugin_stubs as S

P = S.load_plugin()
onboarding = S.sub(P, "onboarding")
events = S.sub(P, "discord_events")
dispatch = S.sub(P, "dispatch")
rostermod = S.sub(P, "roster")
shared = S.sub(P, "shared")

MOD = S.U_MOD                      # punya Manage Roles di Discord, tapi bukan admin tim
NEW_CH = "61" + S.U_DODI[-16:]     # channel yang dibuat kit tiruan untuk dodi


@pytest.fixture(autouse=True)
def _clean_shared():
    shared.reset()
    yield
    shared.reset()


@pytest.fixture
def env(tmp_path):
    e = S.Env(tmp_path, P)
    e.member(S.U_DODI, "dodi.pratama", (S.ROLE_ENTRY,), display_name="Dodi Pratama")
    e.member(S.U_EKA, "eka", (S.ROLE_ENTRY,))
    e.member(S.U_CITRA, "citra", (S.ROLE_ENTRY,))
    e.member(MOD, "fajar", ("200000000000000004",))
    return e


def scenario(env, body, **attach_kw):
    """Daftarkan plugin di host, sambungkan bot tiruan, tunggu startup, jalankan `body(bridge)`."""
    async def main():
        env.register("default")
        bridge = env.attach(**attach_kw)
        await S.settle(bridge)
        try:
            return await body(bridge)
        finally:
            bridge.close()

    return asyncio.run(main())


def onboard_calls(env):
    return [c for c in S.kit_calls(env.state_dir) if c["command"] == "onboard"]


def offboard_calls(env):
    return [c for c in S.kit_calls(env.state_dir) if c["command"] == "offboard"]


# ============================================================================= logika murni
class TestPureLogic:
    def roster(self, **onboarding_conf):
        return rostermod.Roster({"admins": [S.ADMIN], "team_name": "Backend", "shared_channel": S.CH_SHARED,
                                 "admin_channel": S.CH_ADMIN, "onboarding": onboarding_conf,
                                 "users": {S.U_DODI: {"name": "dodi", "profile": "be-dodi", "channel_id": NEW_CH,
                                                      "display_name": "Dodi", "model": "td-simple"}}})

    def test_render_all_documented_placeholders(self):
        template = ("{mention}|{display_name}|{name}|{team_name}|{model}|{channel}|{shared_channel}|"
                    "{admin_channel}|{profile}")
        fields = onboarding.welcome_fields(self.roster(), S.U_DODI, {})
        assert onboarding.render_welcome(template, **fields) == (
            f"<@{S.U_DODI}>|Dodi|dodi|Backend|td-simple|<#{NEW_CH}>|<#{S.CH_SHARED}>|<#{S.CH_ADMIN}>|be-dodi")

    def test_unknown_placeholders_and_braces_are_left_alone(self):
        assert onboarding.render_welcome("Halo {mention} {tidak_ada} {} {{x}} json={\"a\":1}", mention="<@1>") == \
            "Halo <@1> {tidak_ada} {} {{x}} json={\"a\":1}"

    def test_mass_mentions_are_neutralised_and_length_capped(self):
        text = onboarding.render_welcome("@everyone @here <@&200000000000000001> {mention} " + "x" * 3000,
                                         mention="<@1>")
        assert "@everyone" not in text and "@here" not in text and "<@&" not in text
        assert "<@1>" in text and len(text) == 2000

    def test_display_name_cannot_inject_mentions(self):
        fields = onboarding.welcome_fields(self.roster(), S.U_DODI, {"display_name": "<@&2> @everyone `x`"})
        assert "<" not in fields["display_name"] and "@" not in fields["display_name"]

    def test_default_templates_used_when_roster_has_none(self):
        roster = self.roster(welcome_message="", welcome_back_message="")
        first = onboarding.welcome_text(roster, S.U_DODI, {}, back=False)
        again = onboarding.welcome_text(roster, S.U_DODI, {}, back=True)
        assert first.startswith(f"Halo <@{S.U_DODI}>, selamat datang di tim Backend") and "/env-set" in first
        assert again.startswith(f"Halo lagi <@{S.U_DODI}>") and "td-simple" in again

    def test_custom_templates_from_roster(self):
        roster = self.roster(welcome_message="Hai {mention}!", welcome_back_message="Balik lagi {name}")
        assert onboarding.welcome_text(roster, S.U_DODI, {}, back=False) == f"Hai <@{S.U_DODI}>!"
        assert onboarding.welcome_text(roster, S.U_DODI, {}, back=True) == "Balik lagi dodi"

    @pytest.mark.parametrize("actor,trusted", [
        (S.ADMIN, True), (S.OWNER, True), (S.BOT, True), ("777000000000000001", True), (MOD, False),
        (None, False), ("", False),
    ])
    def test_actor_trusted(self, actor, trusted):
        roster = self.roster(verify_actor=True, trusted_actor_ids=["777000000000000001"])
        assert onboarding.actor_trusted(actor, roster=roster, owner_id=int(S.OWNER), bot_id=S.BOT) is trusted

    def test_verify_actor_false_trusts_everyone(self):
        roster = self.roster(verify_actor=False)
        assert onboarding.actor_trusted(MOD, roster=roster) and onboarding.actor_trusted(None, roster=roster)

    def test_find_role_actor_picks_latest_matching_grant(self):
        guild = S.FakeGuild()
        guild.grant(S.U_DODI, MOD, S.ROLE_MEMBER, age=timedelta(minutes=5))
        guild.grant(S.U_DODI, S.ADMIN, S.ROLE_MEMBER, age=timedelta(seconds=3))        # terbaru
        guild.grant(S.U_DODI, MOD, S.ROLE_ENTRY, age=timedelta(seconds=1))             # role lain
        guild.grant(S.U_EKA, MOD, S.ROLE_MEMBER, age=timedelta(seconds=1))             # orang lain
        actor, why = asyncio.run(onboarding.find_role_actor(guild, S.U_DODI, S.ROLE_MEMBER,
                                                            window=timedelta(minutes=10)))
        assert (actor, why) == (S.ADMIN, "")

    def test_find_role_actor_respects_window_and_reports_missing_permission(self):
        guild = S.FakeGuild()
        guild.grant(S.U_DODI, S.ADMIN, S.ROLE_MEMBER, age=timedelta(days=50))
        run = lambda window: asyncio.run(onboarding.find_role_actor(guild, S.U_DODI, S.ROLE_MEMBER, window=window))
        assert run(timedelta(days=45)) == (None, "pemberi role tidak ditemukan di audit log")
        assert run(timedelta(days=60))[0] == S.ADMIN
        guild.audit_forbidden = True
        actor, why = run(timedelta(days=60))
        assert actor is None and "View Audit Log" in why

    def test_kit_argv_matches_hermes_team_cli(self):
        roster = rostermod.Roster({"hermes_root": "/home/hermes/.hermes", "hermes_bin": "/usr/bin/hermes",
                                   "kit": {"python": "/usr/bin/python3", "script": "/opt/hermes-team/hermes_team.py",
                                           "team_file": "/t/team.yaml", "members_file": "/t/members.yaml"}})
        assert onboarding.kit_argv(roster, "onboard", ["--user-id", "5"]) == [
            "/usr/bin/python3", "/opt/hermes-team/hermes_team.py", "onboard", "-c", "/t/team.yaml",
            "--members", "/t/members.yaml", "--hermes-bin", "/usr/bin/hermes", "--hermes-root",
            "/home/hermes/.hermes", "--user-id", "5", "--events"]


# ============================================================================= protokol subprocess
class TestKitProtocol:
    def test_events_are_streamed_and_result_parsed(self, env):
        seen = []

        async def main():
            async def on_event(event):
                seen.append(event["event"])
            return await onboarding.run_kit(env.rt().roster(), "onboard",
                                            ["--user-id", S.U_DODI, "--username", "dodi"], on_event)

        out = asyncio.run(main())
        assert out.ok and out.returncode == 0 and seen == ["channel", "result"]
        assert out.result["status"] == "created" and out.result["profile"] == "be-dodi"
        call = S.kit_calls(env.state_dir)[0]
        assert call["events"] is True and call["hermes_home"] == str(env.hermes_root)
        assert call["config"].endswith("team.yaml") and call["members"].endswith("members.yaml")

    @pytest.mark.parametrize("plan,code,fragment", [
        ({"fail": [S.U_DODI]}, 1, "tidak ditemukan di server Discord"),
        ({"crash": [S.U_DODI]}, 1, "disk penuh"),
        ({"partial": [S.U_DODI]}, 3, "config set model.default"),
    ])
    def test_failures_are_reported_not_raised(self, env, plan, code, fragment):
        S.set_kit_plan(env.state_dir, **plan)
        out = asyncio.run(onboarding.run_kit(env.rt().roster(), "onboard", ["--user-id", S.U_DODI]))
        assert not out.ok and out.returncode == code and fragment in out.describe()

    def test_missing_script_or_interpreter(self, env):
        env.roster["kit"]["python"] = str(env.tmp / "tidak-ada-python")
        env.save()
        out = asyncio.run(onboarding.run_kit(env.rt().roster(force=True), "onboard", ["--user-id", S.U_DODI]))
        assert not out.ok and out.returncode == 127 and "tidak bisa dijalankan" in out.describe()

    def test_timeout_kills_the_subprocess(self, env):
        S.set_kit_plan(env.state_dir, delay=5)
        out = asyncio.run(onboarding.run_kit(env.rt().roster(), "onboard", ["--user-id", S.U_DODI], timeout=0.5))
        assert not out.ok and out.returncode == 124 and "melewati" in out.describe()


# ============================================================================= join → role entry
class TestEntryRole:
    def test_new_member_gets_entry_role(self, env):
        async def body(bridge):
            newcomer = env.member("500000000000000050", "pendatang")
            await env.bot.dispatch("member_join", newcomer)
            return newcomer

        newcomer = scenario(env, body)
        assert newcomer.role_ids() == {S.ROLE_ENTRY}
        assert newcomer.role_log == [("add", S.ROLE_ENTRY, "Hermes: user baru masuk server")]
        assert onboard_calls(env) == []

    def test_entry_role_waits_for_membership_screening(self, env):
        async def body(bridge):
            newcomer = env.member("500000000000000051", "belum-setuju", pending=True)
            await env.bot.dispatch("member_join", newcomer)
            assert newcomer.role_ids() == set()
            before = newcomer.copy()
            newcomer.pending = False                                          # menyetujui aturan server
            await env.bot.dispatch("member_update", before, newcomer)
            return newcomer

        assert scenario(env, body).role_ids() == {S.ROLE_ENTRY}

    def test_bots_and_other_guilds_are_ignored(self, env):
        async def body(bridge):
            bot_user = env.member("500000000000000052", "musicbot", bot=True)
            foreign = S.FakeMember("500000000000000053", "tamu", guild=S.FakeGuild("111111111111111111"))
            await env.bot.dispatch("member_join", bot_user)
            await env.bot.dispatch("member_join", foreign)
            return bot_user, foreign

        bot_user, foreign = scenario(env, body)
        assert bot_user.role_ids() == set() and foreign.role_ids() == set()

    @pytest.mark.parametrize("change", ["give_entry_role_off", "no_entry_role", "onboarding_off"])
    def test_entry_role_can_be_disabled(self, env, change):
        if change == "give_entry_role_off":
            env.set("onboarding", give_entry_role=False)
        elif change == "no_entry_role":
            env.set("roles", entry="")
        else:
            env.set("onboarding", enabled=False)

        async def body(bridge):
            newcomer = env.member("500000000000000054", "pendatang")
            await env.bot.dispatch("member_join", newcomer)
            return newcomer

        assert scenario(env, body).role_ids() == set()

    def test_role_hierarchy_error_does_not_crash_listener(self, env):
        async def body(bridge):
            newcomer = env.member("500000000000000055", "pendatang")
            newcomer.forbid_roles = True                                      # role bot di bawah @entry
            await env.bot.dispatch("member_join", newcomer)
            other = env.member("500000000000000056", "kedua")
            await env.bot.dispatch("member_join", other)
            return newcomer, other

        newcomer, other = scenario(env, body)
        assert newcomer.role_ids() == set() and other.role_ids() == {S.ROLE_ENTRY}


# ============================================================================= @backend dari pemberi tepercaya
class TestTrustedGrant:
    def test_full_flow(self, env):
        async def body(bridge):
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        roster = env.reload()
        # 1. kit dipanggil sekali, dengan identitas Discord orangnya
        (call,) = onboard_calls(env)
        assert (call["user_id"], call["username"], call["display_name"]) == (S.U_DODI, "dodi.pratama", "Dodi Pratama")
        assert roster["users"][S.U_DODI]["channel_id"] == NEW_CH
        # 2. rute & channel baru berlaku di gateway yang sedang jalan, terverifikasi
        match = env.upstream.match_profile_route(env.runner.config.profile_routes, guild_id=S.GUILD, chat_id=NEW_CH)
        assert match.profile == "be-dodi-pratama"
        assert NEW_CH in env.adapter._get_allowed_channels() and NEW_CH in env.adapter._discord_free_response_channels()
        assert env.rt().state.live()["ok"] is True
        # 3. "sedang disiapkan" muncul lalu dihapus; sambutan tinggal satu-satunya pesan
        channel = env.bot.channels[int(NEW_CH)]
        assert [m.deleted for m in channel.messages] == [True, False]
        assert "sedang disiapkan" in channel.messages[0].content
        welcome = channel.messages[1]
        assert welcome.content == (f"Halo <@{S.U_DODI}>, selamat datang di tim Backend! Model: td-simple. "
                                   f"Tim: <#{S.CH_SHARED}>. Profile be-dodi-pratama.")
        # 4. role entry dicabut, admin dikabari
        member = env.bot.guild.get_member(int(S.U_DODI))
        assert member.role_ids() == {S.ROLE_MEMBER}
        assert ("remove", S.ROLE_ENTRY, "Hermes: onboarding selesai") in member.role_log
        (done,) = [t for t in env.admin_texts() if t.startswith("✅")]
        assert f"<@{S.U_DODI}> aktif: <#{NEW_CH}> · profile `be-dodi-pratama` · model td-simple" in done
        assert done.endswith("dtk")
        assert env.rt().state.was_welcomed(S.U_DODI) and env.rt().state.data["pending"] == {}

    def test_welcome_pings_only_the_target_user(self, env):
        discord = pytest.importorskip("discord")
        env.set("onboarding", welcome_message="Halo {mention}! cc @everyone <@&200000000000000002> <@300000000000000001>")

        async def body(bridge):
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        welcome = env.bot.channels[int(NEW_CH)].visible()[-1]
        allowed = welcome.allowed_mentions
        assert isinstance(allowed, discord.AllowedMentions)
        assert allowed.everyone is False and allowed.roles is False and allowed.replied_user is False
        assert [u.id for u in allowed.users] == [int(S.U_DODI)]               # hanya dodi yang ter-ping
        assert "@everyone" not in welcome.content and "<@&" not in welcome.content
        # kabar admin & pesan "sedang disiapkan" tidak mem-ping siapa pun
        others = [m for m in env.bot.sent if m is not welcome]
        assert others and all(m.allowed_mentions.users is False and m.allowed_mentions.everyone is False
                              for m in others)

    @pytest.mark.parametrize("actor", [S.OWNER, S.BOT, "777000000000000001"])
    def test_owner_bot_and_trusted_actor_ids_are_trusted(self, env, actor):
        env.set("onboarding", trusted_actor_ids=["777000000000000001"])

        async def body(bridge):
            await env.grant(S.U_DODI, actor)
            await S.settle(bridge)

        scenario(env, body)
        assert len(onboard_calls(env)) == 1 and env.rt().state.was_welcomed(S.U_DODI)
        assert not env.rt().state.is_held(S.U_DODI)

    def test_verify_actor_false_skips_audit_log(self, env):
        env.set("onboarding", verify_actor=False)

        async def body(bridge):
            env.bot.guild.audit_calls.clear()
            await env.grant(S.U_DODI, actor=None)                             # tidak ada entri audit sama sekali
            await S.settle(bridge)

        scenario(env, body)
        assert env.rt().state.was_welcomed(S.U_DODI) and env.bot.guild.audit_calls == []

    def test_audit_log_is_queried_for_role_updates_in_short_window(self, env):
        discord = pytest.importorskip("discord")

        async def body(bridge):
            env.bot.guild.audit_calls.clear()
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        (call,) = env.bot.guild.audit_calls
        assert call["action"] is discord.AuditLogAction.member_role_update
        age = onboarding.datetime.now(onboarding.timezone.utc) - call["after"]
        assert timedelta(minutes=9) < age < timedelta(minutes=11)

    def test_late_audit_entry_is_retried(self, env):
        async def body(bridge):
            member = env.bot.guild.get_member(int(S.U_DODI))
            before = member.copy()
            member.roles.append(S.FakeRole(S.ROLE_MEMBER))
            real_sleep = bridge.onboarder.sleep

            async def sleep_then_audit_arrives(seconds):
                env.bot.guild.grant(S.U_DODI, S.ADMIN)                        # Discord menulis audit log belakangan
                await real_sleep(seconds)

            bridge.onboarder.sleep = sleep_then_audit_arrives
            await env.bot.dispatch("member_update", before, member)
            await S.settle(bridge)

        scenario(env, body)
        assert env.rt().state.was_welcomed(S.U_DODI) and not env.rt().state.is_held(S.U_DODI)

    def test_duplicate_events_run_the_kit_once(self, env):
        async def body(bridge):
            member = env.bot.guild.get_member(int(S.U_DODI))
            before = member.copy()
            member.roles.append(S.FakeRole(S.ROLE_MEMBER))
            env.bot.guild.grant(S.U_DODI, S.ADMIN)
            for _ in range(3):
                await env.bot.dispatch("member_update", before, member)
            await S.settle(bridge)

        scenario(env, body)
        assert len(onboard_calls(env)) == 1
        assert len(env.bot.channels[int(NEW_CH)].visible()) == 1

    def test_unrelated_role_changes_do_nothing(self, env):
        async def body(bridge):
            await env.grant(S.U_DODI, S.ADMIN, role="200000000000000005")     # role designer
            await S.settle(bridge)

        scenario(env, body)
        assert S.kit_calls(env.state_dir) == [] and not env.rt().state.is_held(S.U_DODI)

    def test_onboarding_disabled(self, env):
        env.set("onboarding", enabled=False)

        async def body(bridge):
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        assert S.kit_calls(env.state_dir) == []

    def test_role_revoked_before_its_turn_is_skipped(self, env):
        S.set_kit_plan(env.state_dir, delay=0.2)

        async def body(bridge):
            await env.grant(S.U_EKA, S.ADMIN)                                 # sedang diproses (lambat)
            await env.grant(S.U_DODI, S.ADMIN)                                # menunggu giliran
            member = env.bot.guild.get_member(int(S.U_DODI))
            member.roles = [r for r in member.roles if str(r.id) != S.ROLE_MEMBER]   # dicabut diam-diam
            await S.settle(bridge)

        scenario(env, body)
        assert [c["user_id"] for c in onboard_calls(env)] == [S.U_EKA]


# ============================================================================= pemberi tidak tepercaya → ditahan
class TestHeld:
    def test_untrusted_grant_is_held_gated_and_reported(self, env):
        async def body(bridge):
            await env.grant(S.U_DODI, MOD)
            await S.settle(bridge)
            gate = dispatch.Dispatcher(env.rt())
            source = S.FakeSource(user_id=S.U_DODI, chat_id=S.CH_SHARED)
            return await gate.handle(event=S.FakeEvent(text="halo", source=source), gateway=env.runner)

        gated = scenario(env, body)
        state = env.rt().state
        assert state.is_held(S.U_DODI) and S.kit_calls(env.state_dir) == []
        assert gated["action"] == "skip"                                       # role saja tidak cukup, juga di shared
        (notice,) = [t for t in env.admin_texts() if t.startswith("⏸️")]
        assert f"<@{S.U_DODI}>" in notice and f"<@{MOD}>" in notice and "lanjutkan onboarding" in notice
        held = json.loads((env.state_dir / "onboarding.json").read_text())["held"][S.U_DODI]
        assert held["actor"] == MOD and "bukan admin tim" in held["reason"] and held["ts"]
        assert S.U_DODI not in env.reload()["users"]
        assert S.ROLE_ENTRY in env.bot.guild.get_member(int(S.U_DODI)).role_ids()   # belum disentuh

    def test_unknown_actor_is_held(self, env):
        async def body(bridge):
            await env.grant(S.U_DODI, actor=None)
            await S.settle(bridge)

        scenario(env, body)
        assert env.rt().state.is_held(S.U_DODI)
        assert "tidak ditemukan di audit log" in env.rt().state.data["held"][S.U_DODI]["reason"]

    def test_missing_audit_permission_holds_with_actionable_reason(self, env):
        async def body(bridge):
            env.bot.guild.audit_forbidden = True
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        assert env.rt().state.is_held(S.U_DODI) and S.kit_calls(env.state_dir) == []
        assert any("View Audit Log" in t for t in env.admin_texts())

    def test_admin_is_told_once_per_hold(self, env):
        async def body(bridge):
            await env.grant(S.U_DODI, MOD)
            await S.settle(bridge)
            for _ in range(3):
                bridge.onboarder.submit("onboard", S.U_DODI, source="reconcile")
                await S.settle(bridge)

        scenario(env, body)
        assert sum(t.startswith("⏸️") for t in env.admin_texts()) == 1

    def test_admin_continue_proceeds_to_welcome(self, env):
        """ "lanjutkan onboarding @dodi" = tool admin menjalankan onboard_member; roster.json berubah dan
        pengawas plugin menyelesaikan sisanya (rute live, sambutan, lepas tahanan)."""
        async def body(bridge):
            await env.grant(S.U_DODI, MOD)
            await S.settle(bridge)
            assert env.rt().state.is_held(S.U_DODI)
            data = env.reload()
            S.roster_add(data, S.U_DODI, "dodi", NEW_CH)                      # teamctl onboard_member
            S.write_roster(env.state_dir, data)
            await S.settle(bridge, rounds=6)
            gate = dispatch.Dispatcher(env.rt())
            source = S.FakeSource(user_id=S.U_DODI, chat_id=S.CH_SHARED)
            return await gate.handle(event=S.FakeEvent(text="halo", source=source), gateway=env.runner)

        gated = scenario(env, body)
        state = env.rt().state
        assert gated is None and not state.is_held(S.U_DODI) and state.was_welcomed(S.U_DODI)
        assert [m.content for m in env.bot.channels[int(NEW_CH)].visible()][-1].startswith(f"Halo <@{S.U_DODI}>")
        assert any(t.startswith("✅") for t in env.admin_texts())
        assert S.ROLE_ENTRY not in env.bot.guild.get_member(int(S.U_DODI)).role_ids()
        assert S.kit_calls(env.state_dir) == []                                # plugin tidak menjalankan kit lagi

    def test_role_removed_while_held_releases_hold_without_offboarding(self, env):
        async def body(bridge):
            await env.grant(S.U_DODI, MOD)
            await S.settle(bridge)
            member = env.bot.guild.get_member(int(S.U_DODI))
            member.roles = [r for r in member.roles if str(r.id) != S.ROLE_ENTRY]
            await env.revoke(S.U_DODI)
            await S.settle(bridge)

        scenario(env, body)
        assert not env.rt().state.is_held(S.U_DODI) and offboard_calls(env) == []
        assert env.bot.guild.get_member(int(S.U_DODI)).role_ids() == {S.ROLE_ENTRY}   # kembali ke entry

    def test_leaving_while_held_releases_hold(self, env):
        async def body(bridge):
            await env.grant(S.U_DODI, MOD)
            await S.settle(bridge)
            gone = env.bot.guild.drop(S.U_DODI)
            await env.bot.dispatch("member_remove", gone)
            await S.settle(bridge)

        scenario(env, body)
        assert not env.rt().state.is_held(S.U_DODI) and S.kit_calls(env.state_dir) == []

    def test_trusted_regrant_after_hold_proceeds(self, env):
        async def body(bridge):
            await env.grant(S.U_DODI, MOD)
            await S.settle(bridge)
            await env.revoke(S.U_DODI)
            await S.settle(bridge)
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        assert env.rt().state.was_welcomed(S.U_DODI) and len(onboard_calls(env)) == 1


# ============================================================================= @backend dicabut / keluar
class TestOffboarding:
    def test_role_removed_deactivates_member_live(self, env):
        async def body(bridge):
            await env.revoke(S.U_BUDI)
            await S.settle(bridge)

        scenario(env, body)
        (call,) = offboard_calls(env)
        assert call["user_id"] == S.U_BUDI and call["keep"] is False
        roster = env.reload()
        assert S.U_BUDI not in roster["users"] and S.U_BUDI in roster["inactive_users"]
        routes = env.runner.config.profile_routes
        assert env.upstream.match_profile_route(routes, guild_id=S.GUILD, chat_id=S.CH_BUDI) is None
        assert S.CH_BUDI not in env.adapter._get_allowed_channels()
        member = env.bot.guild.get_member(int(S.U_BUDI))
        assert member.role_ids() == {S.ROLE_ENTRY}                            # kembali mendapat @entry
        (notice,) = [t for t in env.admin_texts() if t.startswith("👋")]
        assert f"<@{S.U_BUDI}>" in notice and "role @backend dicabut" in notice and "disimpan" in notice
        assert not env.rt().state.was_welcomed(S.U_BUDI)

    def test_offboarded_member_is_refused_by_gate(self, env):
        async def body(bridge):
            await env.revoke(S.U_BUDI)
            await S.settle(bridge)
            gate = dispatch.Dispatcher(env.rt())
            source = S.FakeSource(user_id=S.U_BUDI, chat_id=S.CH_SHARED)
            return await gate.handle(event=S.FakeEvent(text="halo", source=source), gateway=env.runner)

        assert scenario(env, body)["action"] == "skip"

    def test_leaving_server_deactivates_without_touching_roles(self, env):
        async def body(bridge):
            gone = env.bot.guild.drop(S.U_BUDI)
            await env.bot.dispatch("member_remove", gone)
            await S.settle(bridge)
            return gone

        gone = scenario(env, body)
        assert len(offboard_calls(env)) == 1 and gone.role_log == []
        assert any("keluar dari server" in t for t in env.admin_texts())

    def test_keep_channel_access_when_hiding_disabled(self, env):
        env.set("onboarding", hide_channel_on_offboard=False, entry_role_on_offboard=False)

        async def body(bridge):
            await env.revoke(S.U_BUDI)
            await S.settle(bridge)

        scenario(env, body)
        assert offboard_calls(env)[0]["keep"] is True
        assert env.bot.guild.get_member(int(S.U_BUDI)).role_ids() == set()

    def test_offboarding_can_be_disabled(self, env):
        env.set("onboarding", offboard_on_role_remove=False)

        async def body(bridge):
            await env.revoke(S.U_BUDI)
            await S.settle(bridge)
            await env.grant(S.U_BUDI, S.ADMIN)
            await env.revoke(S.U_BUDI)
            await S.settle(bridge)

        scenario(env, body)
        assert offboard_calls(env) == [] and S.U_BUDI in env.reload()["users"]
        assert sum("penonaktifan otomatis dimatikan" in t for t in env.admin_texts()) == 1

    def test_regrant_reactivates_with_welcome_back_and_old_channel(self, env):
        async def body(bridge):
            await env.revoke(S.U_BUDI)
            await S.settle(bridge)
            await env.grant(S.U_BUDI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        roster = env.reload()
        assert roster["users"][S.U_BUDI]["channel_id"] == S.CH_BUDI           # channel & memory lama
        visible = [m.content for m in env.bot.channels[int(S.CH_BUDI)].visible()]
        assert visible == [f"Halo lagi <@{S.U_BUDI}>, ruang kerja Hermes-mu aktif kembali (td-simple)."]
        assert any(t.startswith("✅") and "aktif kembali" in t for t in env.admin_texts())
        assert env.bot.guild.get_member(int(S.U_BUDI)).role_ids() == {S.ROLE_MEMBER}
        assert S.CH_BUDI in env.adapter._get_allowed_channels()

    def test_offboard_failure_is_reported(self, env):
        S.set_kit_plan(env.state_dir, crash=[S.U_BUDI])

        async def body(bridge):
            await env.revoke(S.U_BUDI)
            await S.settle(bridge)

        scenario(env, body)
        assert any(t.startswith("❌ Penonaktifan") for t in env.admin_texts())
        assert S.U_BUDI in env.reload()["users"]

    def test_non_member_losing_role_only_gets_entry_back(self, env):
        async def body(bridge):
            stray = env.member("500000000000000060", "nyasar", (S.ROLE_MEMBER,))
            env.rt().state.hold(stray.id, "uji")
            await env.revoke(str(stray.id))
            await S.settle(bridge)
            return stray

        stray = scenario(env, body)
        assert offboard_calls(env) == [] and stray.role_ids() == {S.ROLE_ENTRY}


# ============================================================================= susulan (reconcile)
class TestReconcile:
    def test_startup_reconcile_onboards_missed_grant_with_45_day_window(self, env):
        env.bot.guild.get_member(int(S.U_DODI)).roles.append(S.FakeRole(S.ROLE_MEMBER))
        env.bot.guild.grant(S.U_DODI, S.ADMIN, age=timedelta(days=20))          # diberikan saat bot offline

        async def body(bridge):
            await S.settle(bridge, rounds=5)

        scenario(env, body)
        assert [c["user_id"] for c in onboard_calls(env)] == [S.U_DODI]
        assert env.rt().state.was_welcomed(S.U_DODI)
        age = onboarding.datetime.now(onboarding.timezone.utc) - env.bot.guild.audit_calls[-1]["after"]
        assert timedelta(days=44) < age < timedelta(days=46)

    def test_reconcile_still_verifies_the_actor(self, env):
        env.bot.guild.get_member(int(S.U_DODI)).roles.append(S.FakeRole(S.ROLE_MEMBER))
        env.bot.guild.grant(S.U_DODI, MOD, age=timedelta(days=2))
        env.bot.guild.get_member(int(S.U_EKA)).roles.append(S.FakeRole(S.ROLE_MEMBER))
        env.bot.guild.grant(S.U_EKA, S.ADMIN, age=timedelta(days=60))            # di luar jendela 45 hari

        async def body(bridge):
            await S.settle(bridge, rounds=5)

        scenario(env, body)
        state = env.rt().state
        assert state.is_held(S.U_DODI) and state.is_held(S.U_EKA) and onboard_calls(env) == []

    def test_reconcile_gives_entry_role_to_roleless_users(self, env):
        env.member("500000000000000070", "tanpa-role")
        env.member("500000000000000071", "belum-setuju", pending=True)
        env.member("500000000000000072", "robot", bot=True)

        async def body(bridge):
            await S.settle(bridge)

        scenario(env, body)
        get = lambda uid: env.bot.guild.get_member(int(uid)).role_ids()
        assert get("500000000000000070") == {S.ROLE_ENTRY}
        assert get("500000000000000071") == set() and get("500000000000000072") == set()
        assert get(MOD) == {"200000000000000004"}                             # punya role lain → bukan "tanpa role"

    def test_reconcile_never_offboards_only_notifies(self, env):
        member = env.bot.guild.get_member(int(S.U_BUDI))
        member.roles = []                                                     # kehilangan @backend saat bot offline
        env.bot.guild.drop(S.U_BE1)                                           # keluar server saat bot offline

        async def body(bridge):
            await S.settle(bridge)
            for _ in range(3):
                await bridge.onboarder.reconcile()
                await S.settle(bridge)

        scenario(env, body)
        assert offboard_calls(env) == []
        roster = env.reload()
        assert S.U_BUDI in roster["users"] and S.U_BE1 in roster["users"]     # tetap anggota aktif
        assert env.upstream.match_profile_route(env.runner.config.profile_routes, guild_id=S.GUILD,
                                                chat_id=S.CH_BUDI).profile == "be-budi"
        lost = [t for t in env.admin_texts() if "Susulan tidak menonaktifkan" in t]
        assert len(lost) == 2 and any(S.U_BUDI in t for t in lost) and any(S.U_BE1 in t for t in lost)
        assert member.role_ids() == set()                                     # juga tidak diberi @entry diam-diam

    def test_periodic_reconcile_runs_from_watcher(self, env):
        env.set("onboarding", reconcile_minutes=1)

        async def body(bridge):
            base, offset = bridge.onboarder.clock, [0.0]
            bridge.onboarder.clock = lambda: base() + offset[0]
            late = env.member("500000000000000073", "telat")
            await asyncio.sleep(0.05)
            assert late.role_ids() == set()
            offset[0] = 61.0                                                  # satu menit berlalu
            await asyncio.sleep(0.08)
            return late

        assert scenario(env, body).role_ids() == {S.ROLE_ENTRY}

    def test_admin_with_backend_role_is_onboarded_without_audit(self, env):
        env.bot.guild.get_member(int(S.ADMIN)).roles.append(S.FakeRole(S.ROLE_MEMBER))

        async def body(bridge):
            await S.settle(bridge, rounds=5)

        scenario(env, body)
        assert [c["user_id"] for c in onboard_calls(env)] == [S.ADMIN] and not env.rt().state.is_held(S.ADMIN)

    def test_reconcile_summary(self, env):
        async def body(bridge):
            env.member("500000000000000074", "baru")
            return await bridge.onboarder.reconcile()

        assert scenario(env, body) == {"onboard": 0, "entry": 1, "lost": 0}


# ============================================================================= serialisasi
class TestSerialisation:
    def test_simultaneous_grants_are_processed_one_at_a_time(self, env):
        S.set_kit_plan(env.state_dir, delay=0.15)

        async def body(bridge):
            await asyncio.gather(env.grant(S.U_DODI, S.ADMIN), env.grant(S.U_EKA, S.ADMIN),
                                 env.grant(S.U_CITRA, S.OWNER))
            await S.settle(bridge, rounds=4)
            return bridge.onboarder.max_parallel_seen

        assert scenario(env, body) == 1
        calls = onboard_calls(env)
        assert [c["user_id"] for c in calls] == [S.U_DODI, S.U_EKA, S.U_CITRA]   # urutan kedatangan
        for earlier, later in zip(calls, calls[1:]):
            assert later["start"] >= earlier["end"]                              # tidak pernah tumpang tindih
        roster = env.reload()
        assert {S.U_DODI, S.U_EKA, S.U_CITRA} <= set(roster["users"])
        state = env.rt().state
        assert all(state.was_welcomed(u) for u in (S.U_DODI, S.U_EKA, S.U_CITRA))
        assert sum(t.startswith("✅") for t in env.admin_texts()) == 3
        assert len(env.runner.config.profile_routes) == len(roster["gateway"]["profile_routes"]) == 7

    def test_onboard_and_offboard_do_not_interleave(self, env):
        S.set_kit_plan(env.state_dir, delay=0.12)

        async def body(bridge):
            await env.grant(S.U_DODI, S.ADMIN)
            await env.revoke(S.U_BUDI)
            await S.settle(bridge, rounds=4)

        scenario(env, body)
        calls = S.kit_calls(env.state_dir)
        assert [c["command"] for c in calls] == ["onboard", "offboard"]
        assert calls[1]["start"] >= calls[0]["end"]
        roster = env.reload()
        assert S.U_DODI in roster["users"] and S.U_BUDI not in roster["users"]

    def test_one_failing_job_does_not_stop_the_queue(self, env):
        S.set_kit_plan(env.state_dir, fail=[S.U_DODI])

        async def body(bridge):
            await env.grant(S.U_DODI, S.ADMIN)
            await env.grant(S.U_EKA, S.ADMIN)
            await S.settle(bridge, rounds=4)

        scenario(env, body)
        assert env.rt().state.was_welcomed(S.U_EKA) and not env.rt().state.was_welcomed(S.U_DODI)


# ============================================================================= kegagalan subprocess
class TestKitFailure:
    @pytest.mark.parametrize("plan,fragment", [
        ({"fail": [S.U_DODI]}, "tidak ditemukan di server Discord"),
        ({"crash": [S.U_DODI]}, "disk penuh"),
        ({"partial": [S.U_DODI]}, "config set model.default"),
    ])
    def test_failure_notifies_admin_and_sends_no_welcome(self, env, plan, fragment):
        S.set_kit_plan(env.state_dir, **plan)

        async def body(bridge):
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        (notice,) = [t for t in env.admin_texts() if t.startswith("❌")]
        assert f"<@{S.U_DODI}>" in notice and fragment in notice and "Tidak ada sambutan" in notice
        welcomes = [m for m in env.bot.sent if "selamat datang" in m.content and not m.deleted]
        assert welcomes == [] and not any(t.startswith("✅") for t in env.admin_texts())
        state = env.rt().state
        assert not state.was_welcomed(S.U_DODI) and fragment in state.data["failed"][S.U_DODI]["reason"]
        assert S.ROLE_ENTRY in env.bot.guild.get_member(int(S.U_DODI)).role_ids()    # entry tidak dicabut
        channel = env.bot.channels.get(int(NEW_CH))
        assert channel is None or channel.visible() == []                     # "sedang disiapkan" dibersihkan

    def test_partial_failure_is_retried_by_reconcile_once_fixed(self, env):
        S.set_kit_plan(env.state_dir, partial=[S.U_DODI])

        async def body(bridge):
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge, rounds=4)
            assert not env.rt().state.was_welcomed(S.U_DODI)                  # pengawas pun tidak menyambut
            S.set_kit_plan(env.state_dir)                                     # admin memperbaiki penyebabnya
            await bridge.onboarder.reconcile()
            await S.settle(bridge, rounds=4)

        scenario(env, body)
        state = env.rt().state
        assert state.was_welcomed(S.U_DODI) and state.data["failed"] == {}
        assert [c["code"] for c in onboard_calls(env)] == [3, 0]
        assert sum("selamat datang" in t for t in env.bot.texts(NEW_CH)) == 1

    def test_missing_kit_script_is_reported(self, env):
        env.roster["kit"]["python"] = str(env.tmp / "python-tidak-ada")
        env.save()

        async def body(bridge):
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        assert any(t.startswith("❌") and "tidak bisa dijalankan" in t for t in env.admin_texts())

    def test_retry_after_fix_succeeds(self, env):
        S.set_kit_plan(env.state_dir, fail=[S.U_DODI])

        async def body(bridge):
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)
            S.set_kit_plan(env.state_dir)
            await env.revoke(S.U_DODI)
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        state = env.rt().state
        assert state.was_welcomed(S.U_DODI) and state.data["failed"] == {}


# ============================================================================= live reload gagal & pengawas
class TestActivationAndWatcher:
    def test_no_welcome_until_route_is_really_live(self, env):
        env.roster["restart_fallback"] = "manual"
        env.save()

        async def body(bridge):
            env.upstream.break_match = True                                   # Hermes berubah: verifikasi gagal
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge, rounds=4)
            texts = list(env.admin_texts())
            channel = env.bot.channels[int(NEW_CH)]
            assert [m.content for m in channel.visible()] == [onboarding.PREPARING]   # belum ada sambutan
            assert not env.rt().state.was_welcomed(S.U_DODI)
            assert any(t.startswith("🕓") for t in texts) and any("/restart" in t for t in texts)
            env.upstream.break_match = False                                  # setelah /restart / perbaikan
            await bridge.onboarder.sync_once()
            await S.settle(bridge)

        scenario(env, body)
        channel = env.bot.channels[int(NEW_CH)]
        assert [m.content.split(",")[0] for m in channel.visible()] == [f"Halo <@{S.U_DODI}>"]
        assert env.rt().state.was_welcomed(S.U_DODI) and env.rt().state.data["pending"] == {}
        assert sum(t.startswith("🕓") for t in env.admin_texts()) == 1

    def test_pending_welcome_is_sent_after_gateway_restart(self, env):
        """Proses lama gagal live reload lalu restart; proses baru melihat anggota yang belum disambut."""
        S.roster_add(env.roster, S.U_DODI, "dodi", NEW_CH)
        env.save()
        state_path = env.state_dir / "onboarding.json"
        state_path.write_text(json.dumps({"welcomed": {S.U_BE1: {"active": True}, S.U_BUDI: {"active": True}},
                                          "pending": {S.U_DODI: {"channel_id": NEW_CH, "message_id": "1"}}}))
        env.bot.guild.get_member(int(S.U_DODI)).roles.append(S.FakeRole(S.ROLE_MEMBER))

        async def body(bridge):
            await S.settle(bridge, rounds=4)

        scenario(env, body)
        assert env.rt().state.was_welcomed(S.U_DODI) and S.kit_calls(env.state_dir) == []
        assert env.bot.texts(NEW_CH)[-1].startswith(f"Halo <@{S.U_DODI}>")
        assert env.bot.texts(S.CH_BE1) == [] and env.bot.texts(S.CH_BUDI) == []

    def test_first_install_does_not_greet_existing_members(self, env):
        async def body(bridge):
            await S.settle(bridge)

        scenario(env, body)
        assert env.bot.texts(S.CH_BE1) == [] and env.bot.texts(S.CH_BUDI) == []
        welcomed = json.loads((env.state_dir / "onboarding.json").read_text())["welcomed"]
        assert set(welcomed) == {S.U_BE1, S.U_BUDI} and all(v["baseline"] for v in welcomed.values())

    def test_member_added_by_cli_is_applied_and_greeted_by_watcher(self, env):
        async def body(bridge):
            env.bot.guild.get_member(int(S.U_EKA)).roles.append(S.FakeRole(S.ROLE_MEMBER))
            data = env.reload()
            S.roster_add(data, S.U_EKA, "eka", "610000000000000005", model="td-normal")   # hermes_team.py add-member
            S.write_roster(env.state_dir, data)
            await S.settle(bridge, rounds=6)

        scenario(env, body)
        assert "610000000000000005" in env.adapter._get_allowed_channels()
        assert env.bot.texts("610000000000000005") == [
            f"Halo <@{S.U_EKA}>, selamat datang di tim Backend! Model: td-normal. Tim: <#{S.CH_SHARED}>. "
            "Profile be-eka."]
        assert S.kit_calls(env.state_dir) == []

    def test_member_deactivated_by_admin_tool_is_unrouted_by_watcher(self, env):
        async def body(bridge):
            data = env.reload()
            S.roster_deactivate(data, S.U_BUDI)                               # teamctl "nonaktifkan budi"
            S.write_roster(env.state_dir, data)
            await S.settle(bridge, rounds=6)

        scenario(env, body)
        assert S.CH_BUDI not in env.adapter._get_allowed_channels()
        assert not env.rt().state.was_welcomed(S.U_BUDI) and env.rt().state.known_before(S.U_BUDI)
        assert offboard_calls(env) == []

    def test_welcome_send_failure_is_reported_but_member_stays_active(self, env):
        async def body(bridge):
            env.bot.channel(NEW_CH).forbid_send = True
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        assert any("sambutan gagal dikirim" in t for t in env.admin_texts())
        assert S.U_DODI in env.reload()["users"]

    def test_admin_notices_can_be_silenced(self, env):
        env.set("onboarding", admin_notices=False)

        async def body(bridge):
            await env.grant(S.U_DODI, S.ADMIN)
            await env.grant(S.U_EKA, MOD)
            await S.settle(bridge)

        scenario(env, body)
        assert env.admin_texts() == [] and env.rt().state.was_welcomed(S.U_DODI)
        assert env.rt().state.is_held(S.U_EKA)                                # tetap ditahan walau tanpa kabar

    def test_listener_exception_does_not_escape_into_discord_py(self, env):
        async def body(bridge):
            await env.bot.dispatch("member_update", object(), object())
            await env.bot.dispatch("member_join", None)
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge)

        scenario(env, body)
        assert env.rt().state.was_welcomed(S.U_DODI)


# ============================================================================= port discord.py
class TestDiscordPort:
    def test_allowed_mentions_and_objects_are_real_discord_types(self):
        discord = pytest.importorskip("discord")
        port = events.DiscordPort(S.FakeBot(), types.SimpleNamespace(roster=lambda: rostermod.Roster()))
        none = port.allowed_mentions()
        assert isinstance(none, discord.AllowedMentions) and none.users is False and none.everyone is False
        one = port.allowed_mentions(S.U_DODI)
        assert one.to_dict() == {"parse": [], "users": [int(S.U_DODI)]}        # tanpa everyone/roles/reply
        assert port.audit_action() is discord.AuditLogAction.member_role_update

    def test_channel_lookup_falls_back_to_fetch(self, env):
        async def main():
            port = events.DiscordPort(env.bot, env.rt())
            await port.send("123456789012345678", "x")
            await port.send("123456789012345678", "y")
            return env.bot.fetches

        assert asyncio.run(main()) == 1 and env.bot.texts("123456789012345678") == ["x", "y"]

    def test_get_member_handles_unknown_user_and_guild(self, env):
        async def main():
            port = events.DiscordPort(env.bot, env.rt())
            assert await port.get_member("555555555555555555") is None
            assert (await port.get_member(S.U_BUDI)).name == "budi"
            env.bot.guild = S.FakeGuild("222222222222222222")
            assert port.guild() is None and await port.get_member(S.U_BUDI) is None

        asyncio.run(main())

    def test_delete_by_id_after_restart(self, env):
        async def main():
            port = events.DiscordPort(env.bot, env.rt())
            message = await port.send(S.CH_BUDI, "⏳")
            await port.delete_by_id(S.CH_BUDI, message.id)
            await port.delete_by_id("", "")
            return message.deleted

        assert asyncio.run(main()) is True


# ============================================================================= integrasi (butuh Hermes)
class TestIntegrationWithRealHermes:
    """DILEWATI bila Hermes tidak terpasang; dijalankan di CI/selftest pada server ber-Hermes."""

    @pytest.fixture(autouse=True)
    def _needs_hermes(self):
        pytest.importorskip("discord")                                        # harus sebelum gateway.run
        pytest.importorskip("gateway", reason="Hermes tidak terpasang — uji integrasi dijalankan di CI")

    def test_onboarding_applies_routes_verified_by_real_resolver(self, env):
        livereload = S.sub(P, "livereload")
        real = livereload.Upstream()
        real.rescan = lambda home: None
        real.served_profiles = lambda config: None
        env.runner.config.profile_routes[:] = real.parse_profile_routes(env.roster["gateway"]["profile_routes"])

        async def main():
            env.register("default")
            factory = events.make_factory(env.rt(), env.ctxs["default"], upstream=real)
            factory(env.bot, env.adapter)
            bridge = env.rt().host
            bridge.onboarder.sleep = bridge.live.sleep = S.fast_sleep
            await S.settle(bridge)
            await env.grant(S.U_DODI, S.ADMIN)
            await S.settle(bridge, rounds=5)
            bridge.close()

        asyncio.run(main())
        from gateway.profile_routing import match_profile_route
        match = match_profile_route(env.runner.config.profile_routes, "discord", guild_id=S.GUILD, chat_id=NEW_CH,
                                    user_id=S.U_DODI)
        assert match.profile == "be-dodi-pratama" and env.rt().state.was_welcomed(S.U_DODI)

    def test_platform_handler_wiring_contract(self):
        from gateway.platforms.base import BasePlatformAdapter
        assert callable(getattr(BasePlatformAdapter, "_wire_plugin_handlers", None))
        assert callable(getattr(BasePlatformAdapter, "rewire_plugin_handlers", None))
        assert hasattr(BasePlatformAdapter, "gateway_runner")
