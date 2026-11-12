#!/usr/bin/env python3
"""teamctl_mcp.py — server MCP (stdio) berisi tool admin untuk tim Hermes.

Dipasang hermes_team.py HANYA di profile team-admin (mcp_servers.teamctl), dan rute
#hermes-admin hanya melayani user admin. Tool di sini dipanggil oleh Hermes ketika admin
meminta lewat chat, mis. "tambahkan Citra sebagai anggota baru, user ID 1234…".

Keamanan:
- Setiap aksi yang mengubah sesuatu meminta konfirmasi lewat MCP elicitation. Hermes
  menampilkannya sebagai tombol Approve/Deny di #hermes-admin; tanpa klik admin, tidak ada
  yang berubah. Klien yang tidak mendukung elicitation → aksi ditolak (fail closed).
- Tidak pernah membaca atau mengembalikan isi .env siapa pun. Token bot Discord hanya dipakai
  untuk membuat channel.
- Semua aksi dicatat ke <state>/audit.log.

Protokol: JSON-RPC 2.0 per baris (MCP stdio), tanpa dependensi selain PyYAML (lewat hermes_team).
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import threading
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermes_team as ht  # noqa: E402

SERVER_VERSION = "2.0.0"
HANDSHAKE_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
ELICITATION_SINCE = "2025-06-18"

INSTRUCTIONS = (
    "Tool admin tim Hermes. Aksi yang mengubah sesuatu (add_member, set_model, set_member_active, "
    "share_skill, unshare_skill) meminta konfirmasi tombol dari admin; jelaskan dulu rencananya ke admin. "
    "Kalau hasil menyebut restart, minta admin mengetik /restart. Jangan pernah meminta atau menampilkan secret."
)

TOOLS: list[dict] = [
    {
        "name": "team_status",
        "description": "Ringkasan tim: anggota, channel, profile, model per orang, status plugin, batas skala.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_models",
        "description": "Katalog model AI yang boleh dipakai (alias → provider:model) dan model per peran.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "add_member",
        "description": ("Tambah anggota tim baru: buat channel Discord privat #hermes-<name> (hanya dia + bot), "
                        "profile Hermes sendiri dari template, model AI, dan rute gateway. Butuh konfirmasi "
                        "admin; setelah selesai admin perlu mengetik /restart."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "huruf kecil/angka/-/_, mis. citra atau be1"},
                "discord_user_id": {"type": "string", "description": "User ID Discord (angka 15–21 digit)"},
                "display_name": {"type": "string"},
                "model": {"type": "string", "description": "alias dari list_models; kosong = model default anggota"},
                "channel_id": {"type": "string", "description": "isi hanya kalau channel sudah dibuat manual"},
            },
            "required": ["name", "discord_user_id"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": False},
    },
    {
        "name": "set_model",
        "description": ("Ganti model AI untuk satu anggota (nama), semua anggota (all), channel bersama (shared), "
                        "admin (admin), atau lobby. Berlaku untuk sesi baru. Butuh konfirmasi admin."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "nama anggota | all | shared | admin | lobby"},
                "model": {"type": "string", "description": "alias dari list_models"},
            },
            "required": ["target", "model"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_member_active",
        "description": ("Nonaktifkan (active=false) atau aktifkan lagi seorang anggota. Data profile, memory, "
                        "dan channel tidak dihapus. Butuh konfirmasi admin dan /restart."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "active": {"type": "boolean"}},
            "required": ["name", "active"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_member_skills",
        "description": "Daftar skill milik seorang anggota (nama, path, deskripsi). Skill di shared/ adalah kiriman.",
        "inputSchema": {
            "type": "object",
            "properties": {"member": {"type": "string"}},
            "required": ["member"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "share_skill",
        "description": ("Bagikan skill buatan seorang anggota ke anggota lain atau ke semua. Skill dipindai dulu "
                        "(nilai secret pemilik, pola kredensial, pemindai keamanan Hermes); kalau ada masalah, "
                        "dibatalkan. Salinan masuk ke kategori shared/ milik penerima. Butuh konfirmasi admin."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "nama skill atau path dari list_member_skills"},
                "from_member": {"type": "string"},
                "to": {"type": "array", "items": {"type": "string"},
                       "description": "daftar nama anggota, atau [\"all\"] untuk semua anggota + channel bersama, "
                                      "atau [\"shared\"] untuk channel bersama saja"},
                "overwrite": {"type": "boolean", "description": "timpa kiriman lain dengan nama sama"},
            },
            "required": ["skill", "from_member", "to"],
            "additionalProperties": False,
        },
    },
    {
        "name": "unshare_skill",
        "description": "Tarik kembali skill yang pernah dibagikan (dari semua penerima atau sebagian). Butuh konfirmasi.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string"},
                "from_members": {"type": "array", "items": {"type": "string"},
                                 "description": "[\"all\"] (default) atau daftar nama"},
            },
            "required": ["skill"],
            "additionalProperties": False,
        },
    },
]


class ToolError(Exception):
    pass


class TeamCtl:
    def __init__(self, team_file: Path, members_file: Optional[Path], hermes_bin: str, hermes_root: Path,
                 require_consent: bool = True, consent_timeout: float = 330.0,
                 out=None, inp=None):
        self.team_file, self.members_file = team_file, members_file
        self.hermes_bin, self.hermes_root = hermes_bin, hermes_root
        self.require_consent, self.consent_timeout = require_consent, consent_timeout
        self.out = out or sys.stdout
        self.inp = inp or sys.stdin
        self.out_lock = threading.Lock()
        self.work_lock = threading.Lock()   # aksi yang mengubah sesuatu dijalankan satu per satu
        self.pending: dict[Any, Future] = {}
        self.ids = itertools.count(1)
        self.client_caps: dict = {}
        self.protocol = "2025-06-18"
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="teamctl")

    # ------------------------------------------------------------------ transport
    def send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False)
        with self.out_lock:
            self.out.write(line + "\n")
            self.out.flush()

    def reply(self, mid: Any, result: Any = None, error: Optional[dict] = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self.send(msg)

    def ask_client(self, method: str, params: dict, timeout: float) -> dict:
        rid = f"teamctl-{next(self.ids)}"
        future: Future = Future()
        self.pending[rid] = future
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return future.result(timeout=timeout)
        finally:
            self.pending.pop(rid, None)

    def serve(self) -> None:
        for raw in self.inp:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.reply(None, error={"code": -32700, "message": "Parse error"})
                continue
            for item in (message if isinstance(message, list) else [message]):
                if isinstance(item, dict):
                    self.dispatch(item)
        self.pool.shutdown(wait=True)

    def dispatch(self, msg: dict) -> None:
        method, mid = msg.get("method"), msg.get("id")
        if method is None:  # jawaban atas permintaan kita (elicitation)
            future = self.pending.get(mid)
            if future is not None and not future.done():
                future.set_result(msg)
            return
        if mid is None:  # notifikasi: initialized, cancelled, dll.
            return
        if method == "initialize":
            params = msg.get("params") or {}
            self.client_caps = params.get("capabilities") or {}
            wanted = str(params.get("protocolVersion") or "")
            self.protocol = wanted if wanted in HANDSHAKE_VERSIONS else "2025-06-18"
            self.reply(mid, {"protocolVersion": self.protocol, "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "teamctl", "version": SERVER_VERSION},
                             "instructions": INSTRUCTIONS})
        elif method == "ping":
            self.reply(mid, {})
        elif method == "tools/list":
            self.reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            self.pool.submit(self._call_safely, mid, msg.get("params") or {})
        elif method in ("resources/list", "prompts/list"):
            self.reply(mid, {"resources": []} if method == "resources/list" else {"prompts": []})
        else:
            self.reply(mid, error={"code": -32601, "message": f"Method not found: {method}"})

    def _call_safely(self, mid: Any, params: dict) -> None:
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        try:
            handler: Optional[Callable[[dict], str]] = getattr(self, f"tool_{name}", None)
            if handler is None or not any(t["name"] == name for t in TOOLS):
                raise ToolError(f"tool tidak dikenal: {name}")
            if not isinstance(args, dict):
                raise ToolError("arguments harus object")
            text = handler(args)
            self.reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        except (ToolError, ht.TeamError) as exc:
            self.reply(mid, {"content": [{"type": "text", "text": f"Gagal: {exc}"}], "isError": True})
        except Exception as exc:  # pragma: no cover - jaga-jaga
            traceback.print_exc(file=sys.stderr)
            self.reply(mid, {"content": [{"type": "text", "text": f"Gagal (internal): {type(exc).__name__}: {exc}"}],
                             "isError": True})

    # ------------------------------------------------------------------ helper
    def team(self) -> dict:
        return ht.load_team(self.team_file, self.members_file)

    def hermes(self) -> ht.Hermes:
        return ht.Hermes(self.hermes_bin, self.hermes_root, quiet=True)

    def supports_elicitation(self) -> bool:
        return "elicitation" in self.client_caps and self.protocol >= ELICITATION_SINCE

    def confirm(self, message: str) -> tuple[bool, str]:
        if not self.require_consent:
            return True, "tanpa konfirmasi (TEAMCTL_REQUIRE_CONSENT=0)"
        if not self.supports_elicitation():
            return False, "klien MCP tidak mendukung konfirmasi (elicitation), jadi aksi ditolak demi keamanan"
        params: dict = {"message": message, "requestedSchema": {"type": "object", "properties": {}}}
        if self.protocol >= "2025-11-25":
            params["mode"] = "form"
        try:
            response = self.ask_client("elicitation/create", params, timeout=self.consent_timeout)
        except Exception:
            return False, "tidak ada jawaban konfirmasi (timeout)"
        if "error" in response:
            return False, f"konfirmasi gagal: {(response.get('error') or {}).get('message')}"
        action = (response.get("result") or {}).get("action")
        if action == "accept":
            return True, "disetujui"
        return False, {"decline": "ditolak admin", "cancel": "dibatalkan / tidak dijawab"}.get(action, f"jawaban {action}")

    def audit(self, team: dict, event: str, **fields: Any) -> None:
        path = Path(team["paths"]["state_dir"]) / "audit.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": ht.now_iso(), "event": event, "via": "teamctl", **fields},
                                    ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    def _tail(hermes: ht.Hermes, n: int = 6) -> str:
        if not hermes.failures:
            return ""
        return "\nKegagalan:\n" + "\n".join(f"- {f}" for f in hermes.failures[-n:])

    def _guarded(self, team: dict, event: str, preview: str, action: Callable[[], str], **audit_fields: Any) -> str:
        ok, why = self.confirm(preview)
        if not ok:
            self.audit(team, event + "-refused", reason=why, **audit_fields)
            return f"Tidak dijalankan: {why}."
        with self.work_lock:
            result = action()
        self.audit(team, event, **audit_fields)
        return result

    # ------------------------------------------------------------------ tool baca
    def tool_team_status(self, args: dict) -> str:
        team = self.team()
        text = ht.describe(team, self.hermes())
        registry = ht.shared_skill_registry(team)["skills"]
        if registry:
            text += "\n\nSkill yang dibagikan: " + ", ".join(
                f"{k} (dari {v.get('origin_member')}, {v.get('scope')})" for k, v in sorted(registry.items()))
        return text

    def tool_list_models(self, args: dict) -> str:
        team = self.team()
        llm = team["llm"]
        if not llm:
            return "team.yaml belum punya bagian llm; model diatur manual per profile."
        lines = ["Katalog model:"]
        lines += [f"- {alias}: {m['provider']} / {m['model']}" for alias, m in llm["models"].items()]
        lines.append(f"Default anggota: {llm['default_model']}; shared: {ht.model_alias(team, 'shared')}; "
                     f"admin: {ht.model_alias(team, 'admin')}; lobby: {ht.model_alias(team, 'lobby')}; "
                     f"auxiliary: {llm['aux_model']}; fallback: {', '.join(llm['fallback']) or '-'}")
        custom = [f"{m['name']}={m['model']}" for m in team["members"] if m.get("model")]
        if custom:
            lines.append("Model khusus per anggota: " + ", ".join(custom))
        return "\n".join(lines)

    def tool_list_member_skills(self, args: dict) -> str:
        team = self.team()
        member = next((m for m in team["members"] if m["name"] == str(args.get("member", "")).strip().lower()), None)
        if member is None:
            raise ToolError(f"anggota '{args.get('member')}' tidak ada")
        skills = ht.list_skills(self.hermes(), member["profile"])
        if not skills:
            return f"{member['name']} belum punya skill."
        return "\n".join(f"- {s['path']}{' (kiriman)' if s['shared'] else ''}: {s['description']}" for s in skills)

    # ------------------------------------------------------------------ tool ubah
    def tool_add_member(self, args: dict) -> str:
        team = self.team()
        name = str(args.get("name") or "").strip().lower()
        user_id = str(args.get("discord_user_id") or "").strip()
        display = str(args.get("display_name") or "").strip() or name.capitalize()
        model = str(args.get("model") or "").strip()
        channel_id = str(args.get("channel_id") or "").strip()
        if not ht.NAME_RE.match(name):
            raise ToolError("name harus huruf kecil/angka/-/_ (mis. citra, be1)")
        if not ht.ID_RE.match(user_id):
            raise ToolError("discord_user_id harus angka 15–21 digit (klik kanan user → Copy User ID)")
        if team["llm"] and model and model not in team["llm"]["models"]:
            raise ToolError(f"model '{model}' tidak ada; pilihan: {', '.join(team['llm']['models'])}")
        if any(m["name"] == name or m["user_id"] == user_id for m in team["members"]):
            raise ToolError("nama atau user ID itu sudah terdaftar")
        effective = model or (team["llm"]["default_model"] if team["llm"] else "-")
        channel_note = (f"pakai channel {channel_id}" if channel_id
                        else f"buat channel privat #{team['channel_prefix']}{name} (hanya {display} + bot)")
        preview = (f"Tambah anggota {display} ({name}), Discord user {user_id}: {channel_note}; "
                   f"buat profile {team['prefix']}{name}; model {effective}. Setelah itu perlu /restart.")

        def run() -> str:
            hermes = self.hermes()
            result = ht.add_member(team, hermes, name=name, user_id=user_id, display_name=display, model=model,
                                   channel_id=channel_id, create_channel=not channel_id)
            member = result["member"]
            lines = [f"Anggota {member['display_name']} ditambahkan: profile {member['profile']}, "
                     f"channel <#{member['channel_id']}>, model {effective}."]
            lines += [f"- {note}" for note in result["notes"]]
            lines.append("Ketik /restart supaya rute channel baru aktif. Setelah itu "
                         f"{member['display_name']} bisa langsung chat di channelnya dan menyimpan secret dengan /env-set.")
            return "\n".join(lines) + self._tail(hermes)

        return self._guarded(team, "add-member", preview, run, member=name, user=user_id, model=effective)

    def tool_set_model(self, args: dict) -> str:
        team = self.team()
        target = str(args.get("target") or "").strip().lower()
        alias = str(args.get("model") or "").strip()
        if not team["llm"]:
            raise ToolError("team.yaml belum punya bagian llm (katalog model)")
        if alias not in team["llm"]["models"]:
            raise ToolError(f"model '{alias}' tidak ada; pilihan: {', '.join(team['llm']['models'])}")
        names = {m["name"] for m in team["members"]}
        if target not in names | {"all", "semua", "all-members", "shared", "admin", "lobby"}:
            raise ToolError(f"target '{target}' tidak dikenal; pakai nama anggota, all, shared, admin, atau lobby")
        spec = team["llm"]["models"][alias]
        preview = f"Ganti model {target} → {alias} ({spec['provider']} / {spec['model']}). Berlaku untuk sesi baru."

        def run() -> str:
            hermes = self.hermes()
            result = ht.set_model(team, hermes, target, alias)
            return (f"Model {target} sekarang {alias} (profile: {', '.join(result['profiles'])}). "
                    "Sesi yang sedang berjalan tetap memakai model lama sampai /new." + self._tail(hermes))

        return self._guarded(team, "set-model", preview, run, target=target, model=alias)

    def tool_set_member_active(self, args: dict) -> str:
        team = self.team()
        name = str(args.get("name") or "").strip().lower()
        active = bool(args.get("active"))
        member = next((m for m in team["members"] if m["name"] == name), None)
        if member is None:
            raise ToolError(f"anggota '{name}' tidak ada")
        preview = (f"{'Aktifkan' if active else 'Nonaktifkan'} anggota {member['display_name']} ({name}). "
                   "Data profile dan channel tidak dihapus. Perlu /restart.")

        def run() -> str:
            hermes = self.hermes()
            ht.set_member_active(team, hermes, name, active)
            return (f"{member['display_name']} sekarang {'aktif' if active else 'nonaktif'}. Ketik /restart supaya "
                    "rute gateway diperbarui." + self._tail(hermes))

        return self._guarded(team, "set-active", preview, run, member=name, active=active)

    def tool_share_skill(self, args: dict) -> str:
        team = self.team()
        hermes = self.hermes()
        skill = str(args.get("skill") or "").strip()
        owner_name = str(args.get("from_member") or "").strip().lower()
        targets = [str(t) for t in (args.get("to") or [])]
        overwrite = bool(args.get("overwrite", False))
        owner = next((m for m in team["members"] if m["name"] == owner_name), None)
        if owner is None:
            raise ToolError(f"anggota '{owner_name}' tidak ada")
        if not targets:
            raise ToolError("isi `to` (nama anggota, atau [\"all\"])")
        skill_dir = ht._find_skill(hermes, owner["profile"], skill)
        report = ht.scan_skill(hermes, owner["profile"], skill_dir)
        if report["problems"]:
            self.audit(team, "share-skill-blocked", skill=skill, from_member=owner_name, problems=report["problems"])
            raise ToolError("skill tidak aman untuk dibagikan: " + " | ".join(report["problems"]))
        kb = max(1, report["bytes"] // 1024)
        verdict = report["guard"].get("verdict", "unknown")
        who = "semua anggota + channel bersama" if any(t.lower() in ("all", "semua") for t in targets) \
            else ", ".join(targets)
        preview = (f"Bagikan skill '{skill_dir.name}' milik {owner['display_name']} ke {who}. "
                   f"{len(report['files'])} file, {kb} KB, pemindai Hermes: {verdict}."
                   + (" Kiriman lama bernama sama akan ditimpa." if overwrite else ""))

        def run() -> str:
            result = ht.share_skill(team, hermes, skill, owner_name, targets, overwrite, scan=report)
            lines = [f"Skill '{result['skill']}' dari {result['from']} ({result['scope']}):"]
            lines += [f"- {name}: {status}" for name, status in sorted(result["results"].items())]
            lines.append("Penerima melihatnya di sesi baru (atau setelah /reload-skills), di kategori shared/.")
            return "\n".join(lines) + self._tail(hermes)

        return self._guarded(team, "share-skill", preview, run, skill=skill_dir.name, from_member=owner_name,
                             to=targets, guard=verdict)

    def tool_unshare_skill(self, args: dict) -> str:
        team = self.team()
        skill = str(args.get("skill") or "").strip()
        targets = [str(t) for t in (args.get("from_members") or ["all"])]
        if skill not in ht.shared_skill_registry(team)["skills"]:
            raise ToolError(f"skill '{skill}' tidak ada di daftar kiriman (lihat team_status)")
        preview = f"Tarik skill kiriman '{skill}' dari {', '.join(targets)}."

        def run() -> str:
            hermes = self.hermes()
            result = ht.unshare_skill(team, hermes, skill, targets)
            done = ", ".join(sorted(result["results"])) or "tidak ada penerima"
            return f"Skill '{skill}' ditarik dari: {done}." + self._tail(hermes)

        return self._guarded(team, "unshare-skill", preview, run, skill=skill, targets=targets)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Server MCP admin tim Hermes (stdio)")
    parser.add_argument("--team", required=True, help="path team.yaml")
    parser.add_argument("--members", default=None, help="path members.yaml")
    parser.add_argument("--hermes-bin", default=os.environ.get("HERMES_BIN") or None)
    parser.add_argument("--hermes-root", default=os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    args = parser.parse_args(argv)
    require = os.environ.get("TEAMCTL_REQUIRE_CONSENT", "1").strip().lower() not in ("0", "false", "no", "off")
    server = TeamCtl(Path(args.team).expanduser(), Path(args.members).expanduser() if args.members else None,
                     ht.resolve_hermes_bin(args.hermes_bin), Path(args.hermes_root).expanduser(),
                     require_consent=require)
    server.serve()


if __name__ == "__main__":
    main()
