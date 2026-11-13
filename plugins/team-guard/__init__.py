"""team-guard — plugin Hermes untuk kit tim (v3.1).

Dipasang ke SEMUA profile (host/default, be-<nama>, team-shared, team-admin). Hermes memuat satu salinan
modul per profile; state yang harus tunggal per proses ada di `shared.py`.

  semua profile : penyamaran secret (terminal / hasil tool / jawaban AI / request model), guard dump
                  environment, penjaga jadwal cron, petunjuk nama secret, pencatat giliran dan panggilan LLM
                  (antrean adil, batas sesi per orang, pembatas adaptif, anggaran token), pre_gateway_dispatch
  host saja     : listener Discord (onboarding), /env-set, live reload rute, pengawas roster.json,
                  metrics.json + peringatan beban ke #hermes-admin

Konfigurasi: hanya `<state_dir>/roster.json` (ditulis hermes_team.py) dan
`plugins.entries.team-guard.settings.state_dir`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import shared
from .dispatch import Dispatcher
from .discord_events import make_factory
from .hooks import ProfileHooks
from .redact import UPSTREAM_PATTERNS
from .roster import default_state_dir, profile_name_of_home
from .runtime import runtime_for
from .secrets import SecretStore
from .upstream import HermesGateway, note_ctx

__version__ = shared.VERSION

PROMPT_SECTION_ID = "team-guard.secrets"


def _hermes_home() -> Path:
    """HERMES_HOME profile yang sedang memuat plugin (loader mengikatnya selama register())."""
    return HermesGateway().hermes_home()


def _state_dir(ctx: Any) -> Path:
    value = None
    try:
        value = ctx.get_config("state_dir")
    except Exception:
        value = None
    return Path(str(value)).expanduser() if value else default_state_dir()


def _setting(ctx: Any, key: str, default: Any) -> Any:
    try:
        value = ctx.get_config(key, default)
    except Exception:
        return default
    return default if value is None else value


def _try(what: str, fn: Callable[[], Any]) -> bool:
    """Satu pendaftaran; API yang tidak ada di versi Hermes ini → satu baris log, plugin tetap jalan."""
    try:
        fn()
        note_ctx(what, True)
        return True
    except Exception as exc:
        shared.log_once(f"register-{what}", "%s tidak tersedia di versi Hermes ini (%s: %s)", what,
                        type(exc).__name__, exc)
        note_ctx(what, False, f"{type(exc).__name__}: {exc}")
        return False


def register(ctx: Any) -> None:
    home = _hermes_home()
    rt = runtime_for(_state_dir(ctx))
    roster = rt.roster()
    if not roster:
        shared.log_once("no-roster", "roster.json belum ada di %s — jalankan `hermes_team.py apply`; hanya "
                        "perlindungan secret dasar yang aktif", rt.state_dir)
    profile = roster.profile_of_home(home) if roster else profile_name_of_home(home)
    kind = roster.profile_kind(profile) if roster else ("host" if profile == "default" else "other")
    rt.profiles[profile] = kind

    hooks = ProfileHooks(rt, SecretStore(home), profile, kind)
    if rt.dispatcher is None:
        rt.dispatcher = Dispatcher(rt)

    # Hook kebijakan gateway. Didaftarkan di semua profile: Hermes memanggilnya lewat manager profile yang
    # aktif saat pesan masuk (sebelum routing = host), dan state-nya dibagi satu proses, jadi tepat satu
    # salinan yang memproses tiap pesan, apa pun manager yang dipakai Hermes.
    _try("hook pre_gateway_dispatch", lambda: ctx.register_hook("pre_gateway_dispatch", rt.dispatcher.handle))

    _try("hook pre_tool_call", lambda: ctx.register_hook("pre_tool_call", hooks.on_pre_tool_call))
    _try("hook transform_terminal_output",
         lambda: ctx.register_hook("transform_terminal_output", hooks.on_terminal_output))
    _try("hook transform_tool_result", lambda: ctx.register_hook("transform_tool_result", hooks.on_tool_result))

    def _llm_output() -> None:
        # hold_stream=True (opsional): jawaban ditahan sampai selesai disamarkan — streaming mati untuk
        # profile ini. Bawaan mati: AI tidak pernah menerima nilai secret (output tool sudah disamarkan).
        if _setting(ctx, "hold_stream", False):
            try:
                ctx.register_hook("transform_llm_output", hooks.on_llm_output, hold_stream=True)
                return
            except TypeError:
                pass
        ctx.register_hook("transform_llm_output", hooks.on_llm_output)

    _try("hook transform_llm_output", _llm_output)
    _try("middleware llm_request", lambda: ctx.register_middleware("llm_request", hooks.on_llm_request))
    _try("hook pre_llm_call", lambda: ctx.register_hook("pre_llm_call", hooks.on_pre_llm_call))
    _try("hook on_session_start", lambda: ctx.register_hook("on_session_start", hooks.on_session_start))
    _try("hook on_session_end", lambda: ctx.register_hook("on_session_end", hooks.on_session_end))
    # Pengamat panggilan LLM: beban nyata (termasuk cron, /goal, subagent) + 429 untuk pembatas adaptif.
    _try("hook pre_api_request", lambda: ctx.register_hook("pre_api_request", hooks.on_pre_api_request))
    _try("hook post_api_request", lambda: ctx.register_hook("post_api_request", hooks.on_post_api_request))
    _try("hook api_request_error", lambda: ctx.register_hook("api_request_error", hooks.on_api_request_error))
    _try("system prompt section", lambda: ctx.register_system_prompt_section(
        PROMPT_SECTION_ID, hooks.prompt_section, position="after_memory", max_chars=2000))
    # Pola vendor statis ke redaktor bawaan Hermes (log + output). Nilai tersimpan TIDAK lewat sini —
    # alasannya di hooks.ProfileHooks._publish_upstream.
    _try("redaction patterns", lambda: ctx.register_redaction_patterns(list(UPSTREAM_PATTERNS)))

    if kind == "host":
        factory = make_factory(rt, ctx)
        _try("platform handler discord", lambda: ctx.register_platform_handler("discord", factory))

        def _unload() -> None:
            # Bisa dipanggil dari thread lain (reload-plugins): tutup jembatan di loop gateway, dan simpan
            # client-nya supaya salinan plugin berikutnya memasang ulang listener tanpa menunggu reconnect.
            # Pesan yang masih menunggu di antrean tidak akan diproses: penunggunya berhenti dan mengabari
            # pemiliknya supaya mengirim ulang (sebisanya; lihat Dispatcher._admit).
            try:
                rt.queue.cancel_all()
            except Exception:
                pass
            bridge = rt.host
            if bridge is None:
                return
            rt.host, rt.detached = None, (bridge.native, bridge.adapter, bridge.loop)
            loop = bridge.loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(bridge.close)
            else:
                bridge.close()

        _try("on_unload", lambda: ctx.on_unload(_unload))
        detached, rt.detached = rt.detached, None
        if detached is not None and detached[2] is not None and detached[2].is_running():
            detached[2].call_soon_threadsafe(factory, detached[0], detached[1])
    # Fitur mana yang aktif di Hermes ini: <state_dir>/capabilities.json (dibaca kit_doctor / admin).
    rt.upstream.write_capabilities(rt.state_dir, rt.wall)
    shared.log.info("team-guard %s dimuat untuk profile %s (%s)", __version__, profile, kind)
