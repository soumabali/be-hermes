"""Klien Discord REST ringan untuk kit tim (bit izin, DiscordAPI, daftar anggota). Hanya stdlib.

Dipisah dari hermes_team.py; semua nama tetap bisa diakses lewat `hermes_team` (re-export).
Token bot tidak pernah masuk pesan error maupun log."""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from kit_base import KIT_VERSION, TeamError, parse_env_text

# --------------------------------------------------------------------------- Discord REST
# Bit izin Discord (https://discord.com/developers/docs/topics/permissions), nama gaya discord.py.
PERMISSIONS = {
    "create_instant_invite": 0, "kick_members": 1, "ban_members": 2, "administrator": 3, "manage_channels": 4,
    "manage_guild": 5, "add_reactions": 6, "view_audit_log": 7, "priority_speaker": 8, "stream": 9,
    "view_channel": 10, "send_messages": 11, "send_tts_messages": 12, "manage_messages": 13, "embed_links": 14,
    "attach_files": 15, "read_message_history": 16, "mention_everyone": 17, "use_external_emojis": 18,
    "view_guild_insights": 19, "connect": 20, "speak": 21, "mute_members": 22, "deafen_members": 23,
    "move_members": 24, "use_voice_activation": 25, "change_nickname": 26, "manage_nicknames": 27,
    "manage_roles": 28, "manage_webhooks": 29, "manage_expressions": 30, "use_application_commands": 31,
    "request_to_speak": 32, "manage_events": 33, "manage_threads": 34, "create_public_threads": 35,
    "create_private_threads": 36, "use_external_stickers": 37, "send_messages_in_threads": 38,
    "use_embedded_activities": 39, "moderate_members": 40, "view_creator_monetization_analytics": 41,
    "use_soundboard": 42, "create_expressions": 43, "create_events": 44, "use_external_sounds": 45,
    "send_voice_messages": 46, "set_voice_channel_status": 48, "send_polls": 49, "use_external_apps": 50,
    "pin_messages": 51, "bypass_slowmode": 52,
}
PERMISSION_ALIASES = {
    "read_messages": "view_channel", "view": "view_channel", "read": "view_channel", "send": "send_messages",
    "manage_permissions": "manage_roles", "external_emojis": "use_external_emojis",
    "manage_emojis": "manage_expressions", "manage_emojis_and_stickers": "manage_expressions",
    "external_stickers": "use_external_stickers", "create_polls": "send_polls", "history": "read_message_history",
    "threads": "send_messages_in_threads", "manage_server": "manage_guild", "timeout_members": "moderate_members",
}
# Izin yang memberi kuasa atas server/orang lain. Tidak bisa diberikan lewat Hermes (kecuali
# server_admin.allow_elevated_permissions) supaya instruksi tersembunyi di chat tidak bisa menaikkan hak.
ELEVATED_PERMISSIONS = {
    "administrator", "manage_guild", "manage_roles", "manage_webhooks", "manage_channels", "ban_members",
    "kick_members", "moderate_members", "mention_everyone", "view_audit_log", "manage_nicknames",
    "manage_events", "manage_expressions", "manage_threads", "manage_messages", "view_guild_insights",
    "view_creator_monetization_analytics", "mute_members", "deafen_members", "move_members",
}
PERM = {name.upper(): 1 << bit for name, bit in PERMISSIONS.items()}
MEMBER_ALLOW = sum(PERM[p] for p in ("VIEW_CHANNEL", "SEND_MESSAGES", "READ_MESSAGE_HISTORY", "ATTACH_FILES",
                                     "EMBED_LINKS", "ADD_REACTIONS", "CREATE_PUBLIC_THREADS",
                                     "SEND_MESSAGES_IN_THREADS", "USE_APPLICATION_COMMANDS"))
BOT_ALLOW = MEMBER_ALLOW | PERM["MANAGE_MESSAGES"] | PERM["MANAGE_THREADS"]
ELEVATED_MASK = sum(1 << PERMISSIONS[p] for p in ELEVATED_PERMISSIONS)


def permission_bits(names: Iterable[str]) -> int:
    """['view_channel', 'SEND_MESSAGES', 'pin messages'] → bitmask. Nama tak dikenal → TeamError."""
    bits = 0
    for raw in names or []:
        key = re.sub(r"[\s\-]+", "_", str(raw).strip().lower())
        key = PERMISSION_ALIASES.get(key, key)
        if key not in PERMISSIONS:
            raise TeamError(f"izin '{raw}' tidak dikenal (contoh: view_channel, send_messages, "
                            "read_message_history, attach_files, add_reactions, pin_messages)")
        bits |= 1 << PERMISSIONS[key]
    return bits


def permission_names(bits: int) -> list[str]:
    return [name for name, bit in sorted(PERMISSIONS.items(), key=lambda kv: kv[1]) if bits & (1 << bit)]


class DiscordHTTPError(TeamError):
    def __init__(self, method: str, path: str, status: int, code: Any, message: str):
        super().__init__(f"Discord API {method} {path} → {status} {message}")
        self.status, self.code = status, code


def discord_token(hermes_root: Path) -> str:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        token = parse_env_text((Path(hermes_root) / ".env").read_text(encoding="utf-8")).get("DISCORD_BOT_TOKEN", "")
    except OSError:
        token = ""
    if not token:
        raise TeamError("DISCORD_BOT_TOKEN tidak ditemukan (env atau ~/.hermes/.env)")
    return token


def private_overwrites(guild_id: str, owner_id: str, bot_id: str) -> list[dict]:
    """Set izin lengkap channel pribadi: @everyone tidak melihat, pemilik + bot boleh."""
    return [
        {"id": str(guild_id), "type": 0, "allow": "0", "deny": str(PERM["VIEW_CHANNEL"])},
        {"id": str(owner_id), "type": 1, "allow": str(MEMBER_ALLOW), "deny": "0"},
        {"id": str(bot_id), "type": 1, "allow": str(BOT_ALLOW), "deny": "0"},
    ]


class MemberList(list):
    """Hasil `DiscordAPI.list_members`. `truncated` True = server punya lebih banyak anggota daripada
    `limit` yang diminta (daftar ini TIDAK lengkap — jangan dipakai untuk menyimpulkan "tidak ada")."""
    truncated: bool = False


class DiscordAPI:
    RETRY_STATUSES = (502, 503, 504)
    IDEMPOTENT = ("GET", "PUT", "DELETE")
    MAX_WAIT = 60.0          # batas tunggu per percobaan (detik), berapa pun Retry-After dari Discord

    def __init__(self, token: str, base: Optional[str] = None, timeout: float = 20.0,
                 sleep: Optional[Callable[[float], None]] = None, max_attempts: int = 4):
        self.token = token
        self.base = (base or os.environ.get("TEAMCTL_DISCORD_API") or "https://discord.com/api/v10").rstrip("/")
        self.timeout = timeout
        self.sleep = sleep or time.sleep     # bisa diganti di uji
        self.max_attempts = max(1, int(max_attempts))
        self._bot_id: Optional[str] = None

    def _safe(self, text: Any) -> str:
        """Teks error tidak boleh memuat token bot (mis. bila ikut tercetak di pesan exception)."""
        text = str(text)
        return text.replace(self.token, "«token»") if self.token else text

    @staticmethod
    def _retry_after(headers: Any, payload: dict) -> float:
        waits = []
        for raw in ((headers.get("Retry-After") if headers is not None else None), payload.get("retry_after")):
            try:
                waits.append(float(raw))
            except (TypeError, ValueError):
                continue
        waits = [w for w in waits if w == w and w >= 0]   # buang NaN / negatif
        return max(waits) if waits else 1.0

    def request(self, method: str, path: str, body: Any = None, reason: Optional[str] = None,
                query: Optional[dict] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        url = self.base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        last = self.max_attempts - 1
        for attempt in range(self.max_attempts):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bot {self.token}")
            req.add_header("User-Agent", f"DiscordBot (https://github.com/NousResearch/hermes-agent, hermes-team-kit {KIT_VERSION})")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            if reason:
                req.add_header("X-Audit-Log-Reason", urllib.parse.quote(reason[:500]))
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = getattr(resp, "status", 200)
                    raw = resp.read()
                if not raw or not raw.strip():
                    return None
                try:
                    return json.loads(raw)
                except ValueError:
                    raise DiscordHTTPError(method, path, status, None, "balasan bukan JSON yang valid")
            except urllib.error.HTTPError as exc:
                try:
                    raw = exc.read()
                except (OSError, http.client.HTTPException):
                    raw = b""
                payload: Any = {}
                with contextlib.suppress(ValueError):
                    payload = json.loads(raw or b"{}")
                if not isinstance(payload, dict):
                    payload = {}
                if exc.code == 429 and attempt < last:
                    self.sleep(min(self._retry_after(exc.headers, payload) + 0.2, self.MAX_WAIT))
                    continue
                # 5xx: server mungkin SUDAH menjalankan permintaan; hanya method idempoten yang diulang
                # (POST/PATCH yang diulang bisa membuat channel/role/pesan dobel).
                if exc.code in self.RETRY_STATUSES and method in self.IDEMPOTENT and attempt < last:
                    self.sleep(min(0.5 * (2 ** attempt), 8.0))
                    continue
                raise DiscordHTTPError(method, path, exc.code, payload.get("code"),
                                       self._safe(payload.get("message") or exc.reason))
            except DiscordHTTPError:
                raise
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                # URLError, timeout baca (TimeoutError), koneksi putus (ConnectionError), dll. Hanya method
                # idempoten yang diulang: POST/PATCH yang diulang bisa membuat channel/pesan dobel.
                if method in self.IDEMPOTENT and attempt < last:
                    self.sleep(min(0.5 * (2 ** attempt), 8.0))
                    continue
                detail = getattr(exc, "reason", None) or exc
                raise TeamError(self._safe(f"Discord API tidak terjangkau ({method} {path}): "
                                           f"{type(exc).__name__}: {detail}"))
        raise TeamError("Discord API: rate limit terus-menerus")   # pragma: no cover (loop selalu return/raise)

    def bot_user_id(self) -> str:
        if self._bot_id is None:
            self._bot_id = str(self.request("GET", "/users/@me")["id"])
        return self._bot_id

    def guild_member(self, guild_id: str, user_id: str) -> dict:
        return self.request("GET", f"/guilds/{guild_id}/members/{user_id}")

    def create_private_channel(self, guild_id: str, name: str, owner_id: str, bot_id: str,
                               category_id: str = "", topic: str = "") -> str:
        body: dict = {"name": name, "type": 0, "topic": topic[:1024],
                      "permission_overwrites": private_overwrites(guild_id, owner_id, bot_id)}
        if category_id:
            body["parent_id"] = category_id
        channel = self.request("POST", f"/guilds/{guild_id}/channels", body, reason=f"Hermes: channel pribadi {name}")
        return str(channel["id"])

    def add_role(self, guild_id: str, user_id: str, role_id: str, reason: str = "Hermes: anggota tim") -> None:
        self.request("PUT", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}", reason=reason)

    def remove_role(self, guild_id: str, user_id: str, role_id: str, reason: str = "Hermes") -> None:
        self.request("DELETE", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}", reason=reason)

    def get_channel(self, channel_id: str) -> Optional[dict]:
        """Channel, atau None kalau sudah dihapus (404 / Unknown Channel)."""
        try:
            return self.request("GET", f"/channels/{channel_id}")
        except DiscordHTTPError as exc:
            if exc.status == 404 or exc.code == 10003:
                return None
            raise

    def set_overwrite(self, channel_id: str, target_id: str, kind: int, allow: int, deny: int = 0,
                      reason: str = "Hermes") -> None:
        body = {"allow": str(allow), "deny": str(deny), "type": kind}
        self.request("PUT", f"/channels/{channel_id}/permissions/{target_id}", body, reason=reason)

    def delete_overwrite(self, channel_id: str, target_id: str, reason: str = "Hermes") -> None:
        try:
            self.request("DELETE", f"/channels/{channel_id}/permissions/{target_id}", reason=reason)
        except DiscordHTTPError as exc:
            if exc.status != 404:
                raise

    # ---- dipakai tool kelola server (teamctl) ----
    def guild(self, guild_id: str) -> dict:
        return self.request("GET", f"/guilds/{guild_id}", query={"with_counts": "true"})

    def guild_channels(self, guild_id: str) -> list:
        return self.request("GET", f"/guilds/{guild_id}/channels") or []

    def guild_roles(self, guild_id: str) -> list:
        return self.request("GET", f"/guilds/{guild_id}/roles") or []

    def list_members(self, guild_id: str, limit: Optional[int] = None) -> "MemberList":
        """Semua anggota server (dipaginasi sampai habis). Dengan `limit`, berhenti di situ dan
        `hasil.truncated` True bila server masih punya anggota lain — tidak pernah terpotong diam-diam."""
        out = MemberList()
        after = "0"
        while limit is None or len(out) < limit:
            want = 1000 if limit is None else min(1000, limit - len(out))
            page = self.request("GET", f"/guilds/{guild_id}/members",
                                query={"limit": str(want), "after": after}) or []
            out.extend(page)
            if len(page) < want:
                return out
            after = str(page[-1]["user"]["id"])
        # Tepat `limit` anggota terambil: cek satu lagi supaya pemotongan terlihat oleh pemanggil.
        probe = self.request("GET", f"/guilds/{guild_id}/members", query={"limit": "1", "after": after}) or []
        out.truncated = bool(probe)
        return out

    def search_members(self, guild_id: str, query: str, limit: int = 10) -> list:
        return self.request("GET", f"/guilds/{guild_id}/members/search",
                            query={"query": query, "limit": str(max(1, min(limit, 100)))}) or []

    def create_channel(self, guild_id: str, body: dict, reason: str) -> dict:
        return self.request("POST", f"/guilds/{guild_id}/channels", body, reason=reason)

    def edit_channel(self, channel_id: str, body: dict, reason: str) -> dict:
        return self.request("PATCH", f"/channels/{channel_id}", body, reason=reason)

    def delete_channel(self, channel_id: str, reason: str) -> None:
        self.request("DELETE", f"/channels/{channel_id}", reason=reason)

    def create_role(self, guild_id: str, body: dict, reason: str) -> dict:
        return self.request("POST", f"/guilds/{guild_id}/roles", body, reason=reason)

    def edit_role(self, guild_id: str, role_id: str, body: dict, reason: str) -> dict:
        return self.request("PATCH", f"/guilds/{guild_id}/roles/{role_id}", body, reason=reason)

    def delete_role(self, guild_id: str, role_id: str, reason: str) -> None:
        self.request("DELETE", f"/guilds/{guild_id}/roles/{role_id}", reason=reason)

    def edit_member(self, guild_id: str, user_id: str, body: dict, reason: str) -> dict:
        return self.request("PATCH", f"/guilds/{guild_id}/members/{user_id}", body, reason=reason)

    def kick_member(self, guild_id: str, user_id: str, reason: str) -> None:
        self.request("DELETE", f"/guilds/{guild_id}/members/{user_id}", reason=reason)

    def send_message(self, channel_id: str, content: str, mention_users: Iterable[str] = ()) -> dict:
        users = [str(u) for u in mention_users]
        body = {"content": content[:2000], "allowed_mentions": {"parse": [], "users": users[:100]}}
        return self.request("POST", f"/channels/{channel_id}/messages", body)

    def channel_messages(self, channel_id: str, limit: int = 20) -> list:
        return self.request("GET", f"/channels/{channel_id}/messages",
                            query={"limit": str(max(1, min(limit, 100)))}) or []
