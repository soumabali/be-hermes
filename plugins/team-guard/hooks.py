"""Hook per profile: penyamaran output, saringan request model, guard perintah, petunjuk nama secret.

Hook-hook ini berjalan di scope profile yang dirutekan (tiap profile memuat salinan plugin sendiri),
jadi secret yang dipakai selalu milik profile itu saja.
"""
from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from .guard import check_tool_call
from .redact import scrub_user_messages
from .secrets import SecretStore
from .shared import log_once


class ProfileHooks:
    def __init__(self, rt: Any, store: SecretStore, profile: str, kind: str):
        self.rt, self.store, self.profile, self.kind = rt, store, profile, kind
        store.on_change = self._publish_upstream

    # ------------------------------------------------------------------ bantu
    def _settings(self) -> dict:
        return self.rt.roster().settings

    def _redactor(self):
        roster = self.rt.roster()
        return self.store.redactor(roster.reserved_env, int(roster.settings.get("min_length") or 6))

    def _publish_upstream(self, values: Mapping[str, str]) -> None:
        """Daftarkan nilai ke mekanisme bawaan Hermes bila ada (dipanggil di scope profile ini).

        - agent.redact.register_vault_redaction_value: nilai PERSIS, ter-scope per HERMES_HOME, hanya di
          memori, dan disaring di `redact_sensitive_text` (jalur menuju model, termasuk input kompresi).
          Kontraknya cocok, tetapi ini fungsi internal (bukan API ctx) dan registrinya dibatasi 64 nilai per
          profile yang DIBAGI dengan vault browser Hermes — jadi kita hanya menambah (tidak pernah
          mengosongkan), paling banyak 32 nilai, sebagai lapisan tambahan; lapisan utama tetap hook plugin.
        - ctx.register_redaction_patterns TIDAK dipakai untuk nilai tersimpan: registrinya global satu
          proses (secret profile A akan ikut disamarkan — dan terkonfirmasi ada — di profile B), tidak bisa
          dicabut saat /env-unset, dan hasilnya topeng parsial, bukan «secret:NAMA». Hanya pola vendor
          statis yang didaftarkan ke sana (lihat __init__.register).
        - ctx.register_secret_source TIDAK dipakai: itu untuk menarik kredensial dari vault eksternal ke
          environment saat proses start; secret anggota sudah ada di `.env` profile yang dibaca Hermes.
        - ctx.register_session_env TIDAK dipakai: hanya mencapai backend terminal lokal
          (tools/environments/local.py), sedangkan kit memakai sandbox Docker (terminal.env_passthrough).
        """
        try:
            from agent.redact import register_vault_redaction_value  # type: ignore[import-not-found]
        except Exception:
            return
        try:
            for value in list(values.values())[:32]:
                register_vault_redaction_value(value)
        except Exception as exc:
            log_once("vault-redaction", "pendaftaran nilai ke redaktor Hermes gagal (%s)", exc)

    def _allow_passthrough(self) -> None:
        """Izinkan nama secret diteruskan ke sandbox tanpa menunggu cache config (API publik Hermes;
        nilainya tetap diselesaikan per profile oleh Hermes)."""
        names = self.store.names()
        if not names:
            return
        try:
            from tools.env_passthrough import register_env_passthrough  # type: ignore[import-not-found]
            register_env_passthrough(names)
        except Exception:
            pass

    # ------------------------------------------------------------------ transform
    def on_terminal_output(self, output: Any = None, **_kw: Any) -> Optional[str]:
        return self._redact_text(output)

    def on_tool_result(self, result: Any = None, **_kw: Any) -> Optional[str]:
        return self._redact_text(result)

    def on_llm_output(self, response_text: Any = None, **_kw: Any) -> Optional[str]:
        return self._redact_text(response_text)

    def _redact_text(self, text: Any) -> Optional[str]:
        if not isinstance(text, str) or not text:
            return None
        try:
            new = self._redactor().redact(text)
        except Exception as exc:
            log_once(f"redact-{self.profile}", "penyamaran gagal di profile %s (%s)", self.profile, exc)
            return None
        return new if new != text else None

    # ------------------------------------------------------------------ middleware llm_request
    def on_llm_request(self, request: Any = None, **_kw: Any) -> Optional[dict]:
        """Saringan terakhir sebelum provider: nilai tersimpan di seluruh payload + token vendor di
        pesan user. (`pre_api_request` hanya observer — tidak bisa mengubah request — jadi dipakai
        middleware `llm_request`, yang memang kontrak Hermes untuk menulis ulang request.)"""
        if not isinstance(request, dict):
            return None
        try:
            new, changed = self._redactor().redact_obj(request)
            for key in ("messages", "input"):
                if isinstance(new.get(key), list):
                    scrubbed, did = scrub_user_messages(new[key])
                    if did:
                        new = {**new, key: scrubbed}
                        changed = True
        except Exception as exc:
            log_once(f"llm-request-{self.profile}", "saringan request gagal di profile %s (%s)", self.profile, exc)
            return None
        if not changed:
            return None
        return {"request": new, "source": "team-guard", "reason": "secret disamarkan sebelum ke provider"}

    # ------------------------------------------------------------------ guard
    def on_pre_tool_call(self, tool_name: Any = None, args: Any = None, **_kw: Any) -> Optional[dict]:
        roster = self.rt.roster()
        if not roster.settings.get("block_env_dump", True):
            return None
        names = set(self.store.names()) | set(roster.reserved_env)
        found = check_tool_call(tool_name, args, names)
        return {"action": "block", "message": found} if found else None

    # ------------------------------------------------------------------ petunjuk
    def prompt_section(self, _session_info: Any = None) -> str:
        if not self._settings().get("hint", True):
            return "Secret pribadi dikelola lewat /env-set; jangan pernah meminta secret ditempel di chat."
        names = self.store.names()
        if not names:
            return ("Belum ada secret tersimpan untuk profile ini. Kalau butuh token/password, minta user "
                    "menyimpannya lewat /env-set (jangan ditempel di chat).")
        return ("Secret yang tersedia sebagai environment variable di sandbox (hanya NAMA; nilainya tidak "
                "pernah terlihat olehmu): " + ", ".join(f"${n}" for n in names) + ". Pakai langsung di "
                "perintah (mis. `curl -H \"Authorization: Bearer $NAMA\" …`). Jangan mencetak, menyalin, "
                "atau mengirim nilainya; output yang memuatnya otomatis diganti «secret:NAMA».")

    # ------------------------------------------------------------------ sesi
    def on_session_start(self, **_kw: Any) -> None:
        self._allow_passthrough()
        self._redactor()

    def on_pre_llm_call(self, session_id: Any = None, sender_id: Any = None, platform: Any = None,
                        **_kw: Any) -> None:
        """Mulai giliran (untuk antrean). Giliran admin tidak dihitung: admin punya slot cadangan."""
        if str(platform or "").lower() not in ("discord", ""):
            return None
        uid = str(sender_id or "")
        counted = not (uid and self.rt.roster().is_admin(uid))
        self.rt.turns.start(str(session_id or ""), uid, time.monotonic(), counted=counted)
        return None

    def on_session_end(self, session_id: Any = None, **_kw: Any) -> None:
        self.rt.turns.end(str(session_id or ""))
