"""Satu-satunya tempat plugin menyentuh INTERNAL Hermes (bukan API publik PluginContext).

Modul lain tidak boleh mengimpor `gateway`, `tools.`, `agent.`, `hermes_cli`, atau `hermes_constants`, dan
tidak boleh membaca atribut privat runner/adapter (`_session_key_for_source`, `_running_agent_count`,
`_gate_env_snapshot`, …): semuanya lewat `HermesGateway`. Uji `TestUpstreamAdapterLayer` menggagalkan build bila
ada yang melanggar. Akibatnya, kalau Hermes mengubah internalnya, yang perlu diperiksa hanya berkas ini, dan
`capabilities()` / `<state_dir>/capabilities.json` memberi tahu admin fitur mana yang aktif di versi Hermes ini.

Aturan degradasi (sama seperti sebelum ada lapisan ini): setiap panggilan yang gagal → satu baris `log_once`
atau diam (di tempat yang memang diam sebelumnya) dan fitur terkait mati sendiri; plugin tidak pernah jatuh.

Internal yang disentuh (semuanya dibungkus getattr/try):
  gateway/profile_routing.py   parse_profile_routes, match_profile_route
  gateway/control_socket.py    rescan_gateway_profiles
  gateway/run.py               _multiplex_profile_homes; GatewayRunner._running_agent_count / _is_session_running
                               / _session_key_for_source / .config.profile_routes
  gateway/run_shutdown.py      GatewayRunner.request_restart (kode keluar 75 → supervisor)
  gateway/restart.py           is_gateway_supervisor_process, is_container_restart_context
  gateway/platforms/event.py   MessageEvent.get_command
  plugins/platforms/discord/adapter.py  DiscordAdapter._gate_env_snapshot, .config.extra, ._get_allowed_channels(),
                               ._discord_free_response_channels()
  tools/env_passthrough.py     _is_hermes_provider_credential, register_env_passthrough
  agent/redact.py              register_vault_redaction_value
  hermes_constants.py          get_hermes_home
Modul sintetis `hermes_plugins.team_guard[__home_<digest>]` (skema penamaan loader plugin) hanya dirujuk di
`shared.py` sebagai penjelasan; tidak ada kode yang bergantung pada nama itu.
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import shared
from .shared import log_once
from .state import atomic_write, now_iso

CAPABILITIES_SCHEMA = 1
CAPABILITIES_FILE = "capabilities.json"
CAPABILITIES_REFRESH = 6 * 3600.0       # detik; isi yang sama tidak ditulis ulang selama ini
_SOURCE_LIMIT = 4_000_000

ENV_ALLOWED = "DISCORD_ALLOWED_CHANNELS"
ENV_FREE = "DISCORD_FREE_RESPONSE_CHANNELS"


class UpstreamMissing(Exception):
    """API internal Hermes yang dibutuhkan tidak ada / tidak bisa dipakai; pesannya siap ditampilkan ke admin."""


# ---------------------------------------------------------------------------------------------------------
# Tabel kapabilitas: SATU entri per API internal yang dipakai. (kunci, modul-modul tempat mencarinya,
# kelas pemilik atau "", nama, untuk apa). Daftar modul > 1 = nama bisa berada di mixin.
_PROBES: tuple[tuple[str, tuple[str, ...], str, str, str], ...] = (
    ("gateway.profile_routing.parse_profile_routes", ("gateway.profile_routing",), "", "parse_profile_routes",
     "live reload: urai rute profile"),
    ("gateway.profile_routing.match_profile_route", ("gateway.profile_routing",), "", "match_profile_route",
     "live reload: verifikasi rute dengan resolver Hermes"),
    ("gateway.control_socket.rescan_gateway_profiles", ("gateway.control_socket",), "", "rescan_gateway_profiles",
     "live reload: profile baru dilayani tanpa restart"),
    ("gateway.run._multiplex_profile_homes", ("gateway.run",), "", "_multiplex_profile_homes",
     "live reload: daftar profile yang dilayani"),
    ("gateway.restart.is_gateway_supervisor_process", ("gateway.restart",), "", "is_gateway_supervisor_process",
     "fallback restart halus"),
    ("gateway.restart.is_container_restart_context", ("gateway.restart",), "", "is_container_restart_context",
     "fallback restart halus"),
    ("GatewayRunner.request_restart", ("gateway.run", "gateway.run_shutdown"), "GatewayRunner", "request_restart",
     "fallback restart halus"),
    ("GatewayRunner._running_agent_count", ("gateway.run",), "GatewayRunner", "_running_agent_count",
     "antrean sesi & pembatas: jumlah sesi berjalan"),
    ("GatewayRunner._is_session_running", ("gateway.run",), "GatewayRunner", "_is_session_running",
     "antrean sesi & pembatas per orang: sesi terlihat berjalan"),
    ("GatewayRunner._session_key_for_source", ("gateway.run",), "GatewayRunner", "_session_key_for_source",
     "antrean sesi & pembatas per orang: kunci sesi pesan"),
    ("MessageEvent.get_command", ("gateway.platforms.event",), "MessageEvent", "get_command",
     "daftar izin perintah kendali (/stop, /approve, …)"),
    ("DiscordAdapter._gate_env_snapshot", ("plugins.platforms.discord.adapter",), "DiscordAdapter",
     "_gate_env_snapshot", "live reload: gerbang channel allowed/free-response"),
    ("DiscordAdapter._get_allowed_channels", ("plugins.platforms.discord.adapter",), "DiscordAdapter",
     "_get_allowed_channels", "live reload: verifikasi allowed_channels"),
    ("DiscordAdapter._discord_free_response_channels", ("plugins.platforms.discord.adapter",), "DiscordAdapter",
     "_discord_free_response_channels", "live reload: verifikasi free_response_channels"),
    ("tools.env_passthrough._is_hermes_provider_credential", ("tools.env_passthrough",), "",
     "_is_hermes_provider_credential", "/env-set: tolak nama kredensial provider Hermes versi ini"),
    ("tools.env_passthrough.register_env_passthrough", ("tools.env_passthrough",), "", "register_env_passthrough",
     "/env-set & sesi: nama secret langsung diteruskan ke sandbox"),
    ("agent.redact.register_vault_redaction_value", ("agent.redact",), "", "register_vault_redaction_value",
     "penyamaran: nilai tersimpan ikut redaktor Hermes"),
    ("hermes_constants.get_hermes_home", ("hermes_constants",), "", "get_hermes_home",
     "lokasi HERMES_HOME profile yang memuat plugin"),
)


def _source_text(module: str) -> Optional[str]:
    """Teks sumber `module` dicari di sys.path TANPA mengimpornya (tidak ada efek samping dari kode hulu)."""
    parts = module.split(".")
    for base in sys.path:
        root = Path(base or ".")
        for cand in (root.joinpath(*parts).with_suffix(".py"), root.joinpath(*parts, "__init__.py")):
            try:
                if cand.is_file():
                    return cand.read_text(encoding="utf-8", errors="replace")[:_SOURCE_LIMIT]
            except OSError:
                continue
    return None


def _probe(modules: tuple[str, ...], owner: str, attr: str) -> dict:
    """{available, reason}. Modul sudah dimuat → periksa objeknya langsung; belum → cari namanya di sumber."""
    seen_missing = []
    for module in modules:
        mod = sys.modules.get(module)
        if mod is not None:
            obj: Any = mod
            for part in ([owner] if owner else []) + [attr]:
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            if obj is not None:
                return {"available": True, "reason": f"ada ({module} sudah dimuat)"}
            # Atribut INSTANS (`self.x = ...` di konstruktor) tidak ada di kelasnya, hanya di objek yang sudah
            # dibuat; cari penugasannya di sumber modul supaya tidak salah melapor "hilang".
            if owner:
                text = _source_text(module)
                if text and re.search(rf"\bself\.{re.escape(attr)}\b\s*(?::[^=\n]+)?=", text):
                    return {"available": True, "reason": f"atribut instans di sumber {module} (modul sudah dimuat)"}
            seen_missing.append(module)
            continue
        text = _source_text(module)
        if text is None:
            seen_missing.append(module)
            continue
        if re.search(rf"\b{re.escape(attr)}\b", text):
            return {"available": True, "reason": f"nama ada di sumber {module} (modul belum dimuat)"}
        seen_missing.append(module)
    return {"available": False, "reason": "tidak ditemukan di " + ", ".join(modules)
            + (" (modul tidak terpasang / tidak ditemukan)" if not any(sys.modules.get(m) for m in modules)
               else "")}


def hermes_version() -> str:
    """Versi Hermes bila bisa diketahui tanpa menjalankan kode hulu yang berat; "" bila tidak."""
    try:
        mod = sys.modules.get("hermes_cli")
        if mod is not None:
            value = getattr(mod, "__version__", None)
            if isinstance(value, str) and value:
                return value
    except Exception:
        pass
    try:
        from importlib import metadata
        for dist in ("hermes-agent", "hermes_agent"):
            try:
                return str(metadata.version(dist))
            except metadata.PackageNotFoundError:
                continue
    except Exception:
        pass
    return ""


def note_ctx(what: str, ok: bool, reason: str = "") -> None:
    """Catat hasil satu pendaftaran lewat PluginContext (API publik), supaya tampil di capabilities.json."""
    table = shared.slot("upstream:ctx", dict)
    table[what] = {"available": bool(ok), "reason": reason or ("terdaftar" if ok else "ditolak/tidak ada")}


class HermesGateway:
    """Fasad internal Hermes. Tanpa state; aman dibuat di mana saja. Uji memakai subkelas yang menimpa
    `parse_profile_routes` / `match_profile_route` / `rescan` / `served_profiles` / `restart`."""

    # ------------------------------------------------------------------ rute & multiplexer
    def parse_profile_routes(self, raw: list) -> list:
        from gateway.profile_routing import parse_profile_routes  # type: ignore[import-not-found]
        return parse_profile_routes(raw)

    def match_profile_route(self, routes: list, **kwargs: Any) -> Any:
        from gateway.profile_routing import match_profile_route  # type: ignore[import-not-found]
        return match_profile_route(routes, "discord", **kwargs)

    def rescan(self, home: Any) -> Optional[dict]:
        """Blocking (soket kontrol) — panggil dari thread, bukan dari event loop."""
        from gateway.control_socket import rescan_gateway_profiles  # type: ignore[import-not-found]
        return rescan_gateway_profiles(Path(str(home)))

    def served_profiles(self, config: Any) -> Optional[set]:
        from gateway.run import _multiplex_profile_homes  # type: ignore[import-not-found]
        return {name for name, _home in _multiplex_profile_homes(config)}

    def restart(self, runner: Any) -> None:
        from gateway.restart import (  # type: ignore[import-not-found]
            is_container_restart_context, is_gateway_supervisor_process)
        via_service = bool(is_gateway_supervisor_process() or is_container_restart_context())
        runner.request_restart(detached=not via_service, via_service=via_service)

    # ------------------------------------------------------------------ GatewayRunner.config.profile_routes
    def has_profile_routes(self, runner: Any) -> bool:
        config = getattr(runner, "config", None)
        return config is not None and hasattr(config, "profile_routes")

    def read_profile_routes(self, runner: Any) -> Any:
        try:
            return runner.config.profile_routes
        except Exception:
            raise UpstreamMissing("GatewayRunner.config.profile_routes tidak ditemukan") from None

    def write_profile_routes(self, runner: Any, parsed: list) -> None:
        """Tulis rute di tempat (pemegang referensi lama ikut melihatnya). Melempar UpstreamMissing."""
        config = getattr(runner, "config", None)
        if config is None or not hasattr(config, "profile_routes"):
            raise UpstreamMissing("GatewayRunner.config.profile_routes tidak ditemukan")
        try:
            current = config.profile_routes
            if isinstance(current, list):
                current[:] = parsed
            else:
                config.profile_routes = parsed
        except Exception as exc:
            raise UpstreamMissing(f"profile_routes tidak bisa ditulis ({type(exc).__name__}: {exc})") from None

    # ------------------------------------------------------------------ DiscordAdapter (gerbang channel)
    def write_channel_gates(self, adapter: Any, allowed: str, free: str) -> list[str]:
        """Gerbang channel hidup di dua tempat: snapshot env per-adapter (menang untuk allowed_channels) dan
        PlatformConfig.extra (menang untuk free_response_channels). Keduanya ditulis; os.environ hanya
        diperbarui bila key-nya memang sudah ada di sana. Mengembalikan bagian yang tersentuh."""
        touched = []
        snapshot = getattr(adapter, "_gate_env_snapshot", None)
        if isinstance(snapshot, dict):
            snapshot[ENV_ALLOWED], snapshot[ENV_FREE] = allowed, free
            touched.append("snapshot")
        extra = getattr(getattr(adapter, "config", None), "extra", None)
        if isinstance(extra, dict):
            extra["allowed_channels"], extra["free_response_channels"] = allowed, free
            touched.append("extra")
        for key, value in ((ENV_ALLOWED, allowed), (ENV_FREE, free)):
            if key in os.environ:
                os.environ[key] = value
        return touched

    def read_channel_gate(self, adapter: Any, which: str) -> set:
        """Himpunan channel yang kini dipakai adapter: which = "allowed" | "free". Melempar UpstreamMissing."""
        getter = {"allowed": "_get_allowed_channels", "free": "_discord_free_response_channels"}[which]
        fn = getattr(adapter, getter, None)
        if not callable(fn):
            raise UpstreamMissing(f"DiscordAdapter.{getter} tidak ditemukan")
        try:
            return {str(x) for x in fn()}
        except Exception as exc:
            raise UpstreamMissing(f"DiscordAdapter.{getter} gagal ({type(exc).__name__}: {exc})") from None

    # ------------------------------------------------------------------ GatewayRunner (sesi)
    def running_agent_count_fn(self, gateway: Any) -> Optional[Callable[[], Any]]:
        fn = getattr(gateway, "_running_agent_count", None)
        return fn if callable(fn) else None

    def running_agent_count(self, gateway: Any) -> Optional[int]:
        """Jumlah sesi berjalan menurut gateway; None bila tidak ada / gagal."""
        fn = self.running_agent_count_fn(gateway)
        if fn is None:
            return None
        try:
            return int(fn())
        except Exception:
            return None

    def session_probe(self, gateway: Any) -> Optional[Callable[[str], bool]]:
        """`GatewayRunner._is_session_running` (kunci sesi → bool) bila ada."""
        probe = getattr(gateway, "_is_session_running", None)
        return probe if callable(probe) else None

    def session_key(self, gateway: Any, source: Any) -> str:
        """Kunci sesi gateway untuk `source`; "" bila tidak bisa diketahui."""
        try:
            return str(gateway._session_key_for_source(source) or "")
        except Exception:
            return ""

    def session_running(self, gateway: Any, source: Any) -> bool:
        """Pesan susulan ke sesi yang sedang jalan masuk antrean sesi Hermes sendiri → tidak ikut antre."""
        try:
            key = gateway._session_key_for_source(source)
            return bool(gateway._is_session_running(key))
        except Exception:
            return False

    def restart_capable(self, runner: Any) -> bool:
        return self.running_agent_count_fn(runner) is not None and callable(getattr(runner, "request_restart", None))

    # ------------------------------------------------------------------ MessageEvent
    def event_get_command(self, event: Any) -> tuple[bool, Optional[str]]:
        """(ada?, perintah). `event.get_command()` milik Hermes bila ada; gagal → (True, None) = pesan biasa."""
        getter = getattr(event, "get_command", None)
        if not callable(getter):
            return False, None
        try:
            got = getter()
            return True, got if got is None or isinstance(got, str) else None
        except Exception:
            return True, None

    # ------------------------------------------------------------------ env passthrough & penyamaran
    def provider_credential_checker(self) -> Optional[Callable[[str], bool]]:
        """Daftar nama kredensial yang dikelola Hermes versi ini (bila bisa diimpor)."""
        try:
            from tools.env_passthrough import _is_hermes_provider_credential  # type: ignore[import-not-found]
            return _is_hermes_provider_credential
        except Exception:
            return None

    def register_env_passthrough(self, names: Iterable[str]) -> bool:
        """Izinkan nama diteruskan ke sandbox tanpa menunggu cache config (nilai tetap diselesaikan per
        profile oleh Hermes). Diam bila API tidak ada."""
        try:
            from tools.env_passthrough import register_env_passthrough  # type: ignore[import-not-found]
            register_env_passthrough(list(names))
            return True
        except Exception:
            return False

    def register_vault_values(self, values: Iterable[str], limit: int = 32) -> None:
        """Nilai PERSIS ke redaktor bawaan Hermes (lapisan tambahan; alasannya di hooks.ProfileHooks)."""
        try:
            from agent.redact import register_vault_redaction_value  # type: ignore[import-not-found]
        except Exception:
            return
        try:
            for value in list(values)[:limit]:
                register_vault_redaction_value(value)
        except Exception as exc:
            log_once("vault-redaction", "pendaftaran nilai ke redaktor Hermes gagal (%s)", exc)

    # ------------------------------------------------------------------ HERMES_HOME
    def hermes_home(self) -> Path:
        """HERMES_HOME profile yang sedang memuat plugin (loader mengikatnya selama register())."""
        try:
            from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
            return Path(get_hermes_home())
        except Exception:
            return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()

    # ------------------------------------------------------------------ kapabilitas
    def capabilities(self) -> dict[str, dict]:
        """{nama API: {available, reason}} — satu entri per internal Hermes yang dipakai, ditambah entri
        `ctx.<pendaftaran>` untuk API PluginContext yang gagal/berhasil didaftarkan. Tidak mengimpor kode hulu."""
        probed = shared.slot("upstream:probes", lambda: {
            key: _probe(modules, owner, attr) for key, modules, owner, attr, _why in _PROBES})
        out = {key: dict(value) for key, value in probed.items()}
        for what, entry in sorted(shared.slot("upstream:ctx", dict).items()):
            out[f"ctx.{what}"] = dict(entry)
        return out

    def capabilities_document(self, wall: Callable[[], float] = time.time) -> dict:
        return {"schema": CAPABILITIES_SCHEMA, "generated_at": now_iso(wall()), "plugin_version": shared.VERSION,
                "hermes_version": hermes_version(), "capabilities": self.capabilities()}

    def write_capabilities(self, state_dir: Any, wall: Callable[[], float] = time.time) -> Optional[Path]:
        """Tulis `<state_dir>/capabilities.json` (atomik, 0600). Isi sama dan masih segar (< 6 jam) → tidak
        ditulis ulang (tiap profile + tiap proses `hermes config set` memuat plugin ini). Gagal → satu log."""
        import json
        path = Path(str(state_dir)).expanduser() / CAPABILITIES_FILE
        try:
            doc = self.capabilities_document(wall)
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                same = all(old.get(k) == doc[k] for k in ("schema", "plugin_version", "hermes_version", "capabilities"))
                fresh = (wall() - datetime.strptime(str(old.get("generated_at")), "%Y-%m-%dT%H:%M:%SZ")
                         .replace(tzinfo=timezone.utc).timestamp()) < CAPABILITIES_REFRESH
                if same and fresh:
                    return path
            except (OSError, ValueError, AttributeError, TypeError):
                pass
            atomic_write(path, json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n", 0o600)
        except Exception as exc:
            log_once("capabilities-write", "capabilities.json tidak bisa ditulis (%s: %s)", type(exc).__name__, exc)
            return None
        return path
