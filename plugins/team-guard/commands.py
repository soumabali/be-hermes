"""/env-set (modal), /env-list, /env-unset — guild command Discord, balasan ephemeral.

Kenapa bukan `ctx.register_command`? Perintah plugin Hermes menerima argumennya sebagai TEKS chat
(`fn(raw_args)`): nilai secret akan lewat transcript, hook, dan log. Modal Discord + balasan ephemeral
menjaga nilai tetap di luar chat, dan interaksi asli memberi kita ID pemanggil untuk pemeriksaan pemilik
channel. Karena itu ketiganya didaftarkan langsung di `commands.Bot.tree` sebagai GUILD command — terpisah
dari sinkronisasi command global milik adapter (plugins/platforms/discord/adapter.py::
_run_post_connect_initialization hanya merekonsiliasi command global).

Catatan: modul ini sengaja TANPA `from __future__ import annotations` — discord.py membaca anotasi
parameter callback, dan `discord` baru diimpor di dalam fungsi.
"""
import asyncio
from typing import Any, Callable, Optional

from .secrets import SecretStore, check_env_name, check_env_value, sync_passthrough
from .shared import log, log_once

MSG_WHERE = ("Perintah ini hanya berfungsi di channel pribadimu (#hermes-<nama>) atau thread di dalamnya. "
             "Secret tersimpan per profile dan tidak terlihat anggota lain.")
MSG_NOT_OWNER = "Ini channel pribadi anggota lain. Pakai perintah ini di channel pribadimu sendiri."
MSG_ADMIN_ONLY = "Di channel ini hanya admin tim yang boleh mengatur secret. Pakai channel pribadimu."
MSG_NO_ROSTER = "Kit belum siap (roster.json belum ada). Hubungi admin tim."


def resolve_env_target(roster: Any, user_id: Any, channel_ids: Any) -> tuple:
    """(profile, pesan_galat). Channel pribadi → hanya pemiliknya; shared/admin → hanya admin tim."""
    if not roster:
        return None, MSG_NO_ROSTER
    uid = str(user_id)
    found = roster.channel_entry(*[c for c in channel_ids if c])
    if found is None:
        return None, MSG_WHERE
    _cid, entry = found
    profile = str(entry.get("profile") or "")
    if not profile:
        return None, MSG_WHERE
    if entry.get("kind") == "personal":
        if str(entry.get("owner")) != uid:
            return None, MSG_NOT_OWNER
        return profile, ""
    if not roster.is_admin(uid):
        return None, MSG_ADMIN_ONLY
    return profile, ""


def _upstream_blocklist() -> Optional[Callable[[str], bool]]:
    """Daftar nama yang dipakai Hermes versi ini (bila bisa diimpor) — melengkapi daftar statis kit."""
    try:
        from tools.env_passthrough import _is_hermes_provider_credential  # type: ignore[import-not-found]
        return _is_hermes_provider_credential
    except Exception:
        return None


def _live_allow(names: list) -> None:
    try:
        from tools.env_passthrough import register_env_passthrough  # type: ignore[import-not-found]
        register_env_passthrough(names)
    except Exception:
        pass


async def do_env_set(roster: Any, profile: str, name: Any, value: Any, by: Any = "", *,
                     store: Optional[SecretStore] = None, runner: Any = None) -> tuple:
    """Simpan secret ke `.env` profile. (berhasil?, pesan untuk pemanggil). Nilai tidak pernah ada di pesan."""
    settings = roster.settings
    name = str(name or "").strip().upper()
    ok, why = check_env_name(name, reserved=roster.reserved_env, allow=settings.get("allow_names") or [],
                             deny=settings.get("deny_names") or [], upstream=_upstream_blocklist())
    if not ok:
        return False, "❌ " + why
    value = value.strip() if isinstance(value, str) else value
    ok, why = check_env_value(value, int(settings.get("min_length") or 6))
    if not ok:
        return False, "❌ " + why
    store = store or SecretStore(roster.profile_home(profile))
    names = store.names()
    if name not in names and len(names) >= int(settings.get("max_keys") or 40):
        return False, (f"❌ Batas {int(settings.get('max_keys') or 40)} secret per profile tercapai. Hapus yang "
                       "tidak dipakai dengan `/env-unset`.")
    try:
        fresh = await asyncio.to_thread(store.set, name, value, str(by))
    except OSError as exc:
        log.warning("team-guard: .env profile %s tidak bisa ditulis (%s)", profile, exc)
        return False, "❌ Secret gagal disimpan (berkas .env profile tidak bisa ditulis). Hubungi admin tim."
    _live_allow([name])
    passed, detail = await sync_passthrough(roster, profile, store, runner=runner)
    verb = "disimpan" if fresh else "diperbarui"
    text = (f"✅ `{name}` {verb} untuk profile `{profile}`. Pakai sebagai `${name}` di perintah sandbox; "
            "nilainya tidak pernah terlihat oleh AI maupun anggota lain.")
    if not passed:
        log_once(f"passthrough-{profile}", "terminal.env_passthrough profile %s gagal diperbarui (%s)",
                 profile, detail)
        text += ("\n⚠️ Daftar terusan sandbox (`terminal.env_passthrough`) belum bisa diperbarui otomatis — "
                 "minta admin menjalankan `hermes_team.py apply` bila variabelnya belum muncul di sandbox.")
    else:
        text += " Berlaku mulai perintah berikutnya (sesi lama: `/new` bila belum terbaca)."
    return True, text


async def do_env_list(roster: Any, profile: str, *, store: Optional[SecretStore] = None) -> str:
    store = store or SecretStore(roster.profile_home(profile))
    names = await asyncio.to_thread(store.names)
    if not names:
        return f"Belum ada secret untuk profile `{profile}`. Tambahkan dengan `/env-set`."
    limit = int(roster.settings.get("max_keys") or 40)
    return (f"Secret profile `{profile}` ({len(names)}/{limit}) — hanya nama, nilainya tidak ditampilkan:\n"
            + "\n".join(f"• `{n}`" for n in names))


async def do_env_unset(roster: Any, profile: str, name: Any, *, store: Optional[SecretStore] = None,
                       runner: Any = None) -> tuple:
    name = str(name or "").strip().upper()
    store = store or SecretStore(roster.profile_home(profile))
    try:
        removed = await asyncio.to_thread(store.unset, name)
    except OSError:
        return False, "❌ Secret gagal dihapus (berkas .env profile tidak bisa ditulis)."
    if not removed:
        return False, f"`{name}` tidak ada di daftar secret profile `{profile}` (lihat `/env-list`)."
    await sync_passthrough(roster, profile, store, removed=[name], runner=runner)
    return True, f"🗑️ `{name}` dihapus dari profile `{profile}`."


# ----------------------------------------------------------------------------- Discord
def interaction_channels(interaction: Any) -> list:
    channel = getattr(interaction, "channel", None)
    return [getattr(interaction, "channel_id", None), getattr(channel, "id", None),
            getattr(channel, "parent_id", None)]


def build_commands(rt: Any, *, runner: Any = None) -> dict:
    """Bangun objek app command discord.py. Mengembalikan {"commands": [...], "modal": kelas modal}."""
    import discord
    from discord import app_commands

    def target(interaction: Any) -> tuple:
        roster = rt.roster()
        uid = getattr(getattr(interaction, "user", None), "id", "")
        if roster and roster.user_status(uid) not in ("admin", "active") and rt.state.is_held(uid):
            return roster, None, "Onboarding-mu masih menunggu persetujuan admin tim."
        profile, error = resolve_env_target(roster, uid, interaction_channels(interaction))
        return roster, profile, error

    async def reply(interaction: Any, text: str) -> None:
        try:
            await interaction.response.send_message(text[:1900], ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            log_once("env-reply", "balasan ephemeral gagal (%s: %s)", type(exc).__name__, exc)

    class EnvSetModal(discord.ui.Modal):
        """Form pribadi: nilai tidak pernah masuk chat, transcript, memory, atau log Hermes."""

        def __init__(self, profile: str):
            super().__init__(title="Simpan secret (hanya kamu yang melihat)", timeout=600)
            self.profile = profile
            self.key = discord.ui.TextInput(label="Nama (HURUF_BESAR, mis. GH_PAT)", min_length=2, max_length=64,
                                            placeholder="GH_PAT", required=True)
            self.value = discord.ui.TextInput(label="Nilai", style=discord.TextStyle.paragraph, min_length=1,
                                              max_length=4000, required=True,
                                              placeholder="Token / password. Tidak ditampilkan lagi setelah disimpan.")
            self.add_item(self.key)
            self.add_item(self.value)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            roster, profile, error = target(interaction)
            if error or profile != self.profile:
                await reply(interaction, error or MSG_WHERE)
                return
            _ok, text = await do_env_set(roster, profile, self.key.value, self.value.value,
                                         getattr(interaction.user, "id", ""), runner=runner)
            await reply(interaction, text)

        async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
            log.warning("team-guard: /env-set gagal (%s)", type(error).__name__)   # tanpa isi form
            await reply(interaction, "❌ Secret gagal disimpan. Coba lagi atau hubungi admin tim.")

    async def env_set(interaction: discord.Interaction) -> None:
        _roster, profile, error = target(interaction)
        if error:
            await reply(interaction, error)
            return
        await interaction.response.send_modal(EnvSetModal(profile))

    async def env_list(interaction: discord.Interaction) -> None:
        roster, profile, error = target(interaction)
        await reply(interaction, error or await do_env_list(roster, profile))

    async def env_unset(interaction: discord.Interaction, key: str) -> None:
        roster, profile, error = target(interaction)
        if error:
            await reply(interaction, error)
            return
        _ok, text = await do_env_unset(roster, profile, key, runner=runner)
        await reply(interaction, text)

    unset = app_commands.Command(name="env-unset", description="Hapus satu secret pribadimu",
                                 callback=app_commands.describe(key="Nama secret, mis. GH_PAT")(env_unset))
    return {"modal": EnvSetModal, "commands": [
        app_commands.Command(name="env-set", description="Simpan secret pribadi (form tertutup, tidak masuk chat)",
                             callback=env_set),
        app_commands.Command(name="env-list", description="Daftar NAMA secret pribadimu (tanpa nilai)",
                             callback=env_list),
        unset,
    ]}


def install(native: Any, rt: Any, *, runner: Any = None) -> Any:
    """Pasang ketiga command ke tree bot sebagai guild command. Mengembalikan objek guild untuk sync."""
    import discord

    roster = rt.roster()
    tree = getattr(native, "tree", None)
    if tree is None or not roster.guild_id:
        log_once("env-tree", "commands.Bot.tree / guild_id tidak tersedia; /env-set tidak dipasang")
        return None
    guild = discord.Object(id=int(roster.guild_id))
    built = build_commands(rt, runner=runner)
    for command in built["commands"]:
        try:
            tree.add_command(command, guild=guild, override=True)
        except Exception as exc:
            log_once(f"env-add-{command.name}", "command /%s gagal dipasang (%s)", command.name, exc)
    return guild
