"""Tool tim (grup "team"): status, model, anggota, onboarding, skill bersama.

Satu tool = satu fungsi + satu dekorator `@tool(...)` (lihat registry.py). Tool pengubah mengembalikan
fungsi `plan()`; konfirmasi admin dijalankan kerangka, bukan di sini."""
from __future__ import annotations

import shutil
from typing import Union

import hermes_team as ht

from .confirm import Plan, PlanFn, Planner, _clean, _q
from .discord_guard import TYPE_NAMES, VIEW, Server
from .errors import ToolError
from .registry import tool


@tool(
    "team_status",
    description="Ringkasan tim: anggota, channel, profile, model per orang, status plugin, batas skala, "
                "BEBAN SEKARANG (sesi aktif, panggilan LLM berjalan, antrean, penolakan, 429, batas efektif) "
                "dan anggaran token hari ini per orang. Hanya-baca. Pakai untuk \"bagaimana beban sekarang?\" "
                "atau \"kenapa bot lambat?\".",
    schema={"type": "object", "properties": {}, "additionalProperties": False},
    order=10,
)
def team_status(ctl, args: dict) -> str:
    team = ctl.team()
    text = ht.describe(team, ctl.hermes())
    registry = ht.shared_skill_registry(team)["skills"]
    if registry:
        text += "\n\nSkill yang dibagikan: " + ", ".join(
            f"{k} (dari {v.get('origin_member')}, {v.get('scope')})" for k, v in sorted(registry.items()))
    metrics, note = ctl._read_metrics(team)
    fresh = metrics is not None and not note.startswith("DATA BASI")
    text += "\n\n" + "\n".join(ctl._load_section(team, metrics, note))
    text += "\n\n" + "\n".join(ctl._budget_section(team, metrics, fresh))
    warnings = ht.fallback_warnings(team)
    if warnings:
        text += "\n\nPeringatan konfigurasi:\n" + "\n".join(f"- {w}" for w in warnings)
    return text


@tool(
    "list_models",
    description="Katalog model AI yang boleh dipakai (alias → provider:model) dan model per peran.",
    schema={"type": "object", "properties": {}, "additionalProperties": False},
    order=30,
)
def list_models(ctl, args: dict) -> str:
    team = ctl.team()
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


@tool(
    "add_member",
    description="Tambah anggota dengan nama/model/channel tertentu: buat channel Discord privat #hermes-<name> "
                "(hanya dia + bot), profile Hermes sendiri dari template, model AI, rute gateway, lalu role "
                "@backend. Rute aktif otomatis (live reload) dan bot mengirim sambutan. Butuh konfirmasi. "
                "Untuk kasus biasa pakai onboard_member (nama diambil dari username).",
    schema={
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
    mutating=True, event="add-member", destructive=False, order=40,
)
def add_member(ctl, args: dict) -> PlanFn:
    name = str(args.get("name") or "").strip().lower()
    user_id = str(args.get("discord_user_id") or "").strip()
    # Nama tampilan masuk ke SOUL.md/status/topik channel → satu baris, tanpa karakter kontrol, maks 64.
    display = _clean(args.get("display_name"), 64) or name.capitalize()
    model = str(args.get("model") or "").strip()
    channel_id = str(args.get("channel_id") or "").strip()

    def plan() -> Plan:
        team = ctl.team()
        if not ht.NAME_RE.match(name):
            raise ToolError("name harus huruf kecil/angka/-/_ (mis. citra, be1)")
        if not ht.ID_RE.match(user_id):
            raise ToolError("discord_user_id harus angka 15–21 digit (klik kanan user → Copy User ID)")
        if team["llm"] and model and model not in team["llm"]["models"]:
            raise ToolError(f"model {_q(model, 60)} tidak ada; pilihan: {', '.join(team['llm']['models'])}")
        if any(m["name"] == name or m["user_id"] == user_id for m in team["members"]):
            raise ToolError("nama atau user ID itu sudah terdaftar")
        if channel_id and not ht.ID_RE.match(channel_id):
            raise ToolError("channel_id harus ID channel (angka 15–21 digit)")
        srv = Server(team, ctl.api())
        member = srv.member(user_id)
        if (member.get("user") or {}).get("bot"):
            raise ToolError("bot tidak bisa dijadikan anggota")
        who = srv.member_label(member)
        effective = model or (team["llm"]["default_model"] if team["llm"] else "-")
        if channel_id:
            ch = srv.channel(channel_id)   # harus ada DI SERVER INI
            kind = srv.channel_kind(ch)
            if kind:
                raise ToolError(f"{srv.channel_label(ch)} adalah {kind} — tidak bisa dijadikan channel pribadi "
                                "anggota lain")
            if ch.get("type") != 0:
                raise ToolError(f"{srv.channel_label(ch)} bukan channel teks biasa "
                                f"({TYPE_NAMES.get(ch.get('type'), ch.get('type'))})")
            sees = bool(srv.channel_perms(ch, member.get("roles") or [], user_id) & VIEW)
            channel_note = (
                f"pakai channel yang SUDAH ADA {srv.channel_label(ch)} sebagai channel pribadinya. PERINGATAN: "
                f"izin channel itu TIDAK diubah; saat ini bisa dilihat oleh: {'; '.join(srv.viewers(ch))}. "
                "Semua yang bisa melihatnya bisa membaca percakapan pribadinya dengan Hermes"
                + ("" if sees else f"; {who} sendiri BELUM bisa melihat channel itu") + ".")
        else:
            channel_note = f"buat channel privat #{team['channel_prefix']}{name} (hanya dia + bot);"
        preview = (f"Tambah anggota {_q(display, 64)} ({name}), Discord user {who}: {channel_note} "
                   f"buat profile {team['prefix']}{name} (memory, .env, sandbox sendiri); model {effective}"
                   + ("." if channel_id else "; beri role anggota."))

        def run() -> str:
            hermes = ctl.hermes()
            result = ht.add_member(team, hermes, name=name, user_id=user_id, display_name=display, model=model,
                                   channel_id=channel_id, create_channel=not channel_id)
            added = result["member"]
            lines = [f"Anggota {added['display_name']} ditambahkan: profile {added['profile']}, "
                     f"channel <#{added['channel_id']}>, model {effective}."]
            lines += [f"- {note}" for note in result["notes"]]
            lines.append(ctl._activation_note(team, result) + f" {added['display_name']} bisa langsung chat di "
                         "channelnya dan menyimpan secret dengan /env-set.")
            return "\n".join(lines) + ctl._tail(hermes)

        return Plan(team, preview, run, member=name, user=user_id, model=effective, channel=channel_id or "baru")

    return plan


@tool(
    "set_model",
    description="Ganti model AI untuk satu anggota (nama), semua anggota (all), channel bersama (shared), "
                "admin (admin), atau lobby. Berlaku untuk sesi baru. Butuh konfirmasi admin.",
    schema={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "nama anggota | all | shared | admin | lobby"},
            "model": {"type": "string", "description": "alias dari list_models"},
        },
        "required": ["target", "model"],
        "additionalProperties": False,
    },
    mutating=True, event="set-model", order=50,
)
def set_model(ctl, args: dict) -> PlanFn:
    target = str(args.get("target") or "").strip().lower()
    alias = str(args.get("model") or "").strip()

    def plan() -> Plan:
        team = ctl.team()
        if not team["llm"]:
            raise ToolError("team.yaml belum punya bagian llm (katalog model)")
        if alias not in team["llm"]["models"]:
            raise ToolError(f"model {_q(alias, 60)} tidak ada; pilihan: {', '.join(team['llm']['models'])}")
        names = {m["name"] for m in team["members"]}
        if target not in names | {"all", "semua", "all-members", "shared", "admin", "lobby"}:
            raise ToolError(f"target {_q(target, 60)} tidak dikenal; pakai nama anggota, all, shared, admin, "
                            "atau lobby")
        spec = team["llm"]["models"][alias]
        scope = (f"SEMUA anggota ({len(names)} orang)" if target in ("all", "semua", "all-members") else target)
        preview = (f"Ganti model {scope} → {alias} ({_clean(spec['provider'], 60)} / {_clean(spec['model'], 80)}). "
                   "Berlaku untuk sesi baru.")

        def run() -> str:
            hermes = ctl.hermes()
            result = ht.set_model(team, hermes, target, alias)
            return (f"Model {target} sekarang {alias} (profile: {', '.join(result['profiles'])}). "
                    "Sesi yang sedang berjalan tetap memakai model lama sampai /new." + ctl._tail(hermes))

        return Plan(team, preview, run, target=target, model=alias)

    return plan


@tool(
    "set_member_active",
    description="Nonaktifkan (active=false) atau aktifkan lagi seorang anggota (berdasarkan nama). Data "
                "profile, memory, dan channel tidak dihapus; rute diperbarui otomatis. Butuh konfirmasi. "
                "Catatan: kalau orangnya masih punya role @backend, sebaiknya cabut role itu (discord_remove_role).",
    schema={
        "type": "object",
        "properties": {"name": {"type": "string"}, "active": {"type": "boolean"}},
        "required": ["name", "active"],
        "additionalProperties": False,
    },
    mutating=True, event="set-active", order=60,
)
def set_member_active(ctl, args: dict) -> PlanFn:
    name = str(args.get("name") or "").strip().lower()
    active = bool(args.get("active"))

    def plan() -> Union[Plan, str]:
        team = ctl.team()
        member = next((m for m in team["members"] if m["name"] == name), None)
        if member is None:
            raise ToolError(f"anggota {_q(name, 60)} tidak ada")
        who = f"{_q(member['display_name'], 64)} ({name}, Discord user {member['user_id']})"
        channel = ctl._channel_note(team, member["channel_id"])
        if active:
            preview = (f"Aktifkan anggota {who}: rute Hermes dipasang lagi dan user {member['user_id']} diberi akses "
                       f"(lihat/kirim) ke channel pribadinya {channel}. Profile, memory, dan secret lamanya "
                       "dipakai lagi.")
        else:
            hide = team["onboarding"]["hide_channel_on_offboard"]
            preview = (f"Nonaktifkan anggota {who}: rute Hermes dicabut"
                       + (f" dan aksesnya ke channel pribadinya {channel} dicabut" if hide
                          else f"; izin channel pribadinya {channel} tidak diubah")
                       + ". Data profile dan channel tidak dihapus.")

        def run() -> str:
            hermes = ctl.hermes()
            result = ht.set_member_active(team, hermes, name, active)
            notes = "".join(f"\n- {n}" for n in result.get("notes", []))
            return (f"{member['display_name']} sekarang {'aktif' if active else 'nonaktif'}. "
                    + ctl._activation_note(team, result) + notes + ctl._tail(hermes))

        return Plan(team, preview, run, member=name, active=active, channel=member["channel_id"])

    return plan


@tool(
    "list_member_skills",
    description="Daftar skill milik seorang anggota (nama, path, deskripsi). Skill di shared/ adalah kiriman.",
    schema={
        "type": "object",
        "properties": {"member": {"type": "string"}},
        "required": ["member"],
        "additionalProperties": False,
    },
    order=70,
)
def list_member_skills(ctl, args: dict) -> str:
    team = ctl.team()
    member = next((m for m in team["members"] if m["name"] == str(args.get("member", "")).strip().lower()), None)
    if member is None:
        raise ToolError(f"anggota {_q(args.get('member'), 60)} tidak ada")
    skills = ht.list_skills(ctl.hermes(), member["profile"])
    if not skills:
        return f"{member['name']} belum punya skill."
    return "\n".join(f"- {s['path']}{' (kiriman)' if s['shared'] else ''}: {s['description']}" for s in skills)


@tool(
    "share_skill",
    description="Bagikan skill buatan seorang anggota ke anggota lain atau ke semua. Skill dipindai dulu "
                "(nilai secret pemilik, pola kredensial, pemindai keamanan Hermes); kalau ada masalah, "
                "dibatalkan. Salinan masuk ke kategori shared/ milik penerima. Butuh konfirmasi admin.",
    schema={
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "nama skill atau path dari list_member_skills"},
            "from_member": {"type": "string"},
            "to": {"type": "array", "items": {"type": "string"},
                   "description": "daftar nama anggota, atau [\"all\"] untuk semua anggota + channel bersama, "
                                  "atau [\"shared\"] untuk channel bersama saja"},
            "overwrite": {"type": "boolean", "description": "timpa kiriman lain dengan nama sama"},
            "allow_unscanned": {"type": "boolean", "description": "izinkan skill yang TIDAK bisa dipindai pemindai Hermes (pemindai hilang/error). Skill yang dinilai berbahaya tetap ditolak."},
        },
        "required": ["skill", "from_member", "to"],
        "additionalProperties": False,
    },
    mutating=True, event="share-skill", order=80,
)
def share_skill(ctl, args: dict) -> PlanFn:
    skill = str(args.get("skill") or "").strip()
    owner_name = str(args.get("from_member") or "").strip().lower()
    targets = [str(t) for t in (args.get("to") or [])]
    overwrite = bool(args.get("overwrite", False))
    allow_unscanned = bool(args.get("allow_unscanned", False))
    box: dict = {}   # salinan staging dibuat SEKALI: yang dipindai = yang dikonfirmasi = yang dipasang

    def plan() -> Plan:
        team = ctl.team()
        hermes = ctl.hermes()
        owner = next((m for m in team["members"] if m["name"] == owner_name), None)
        if owner is None:
            raise ToolError(f"anggota {_q(owner_name, 60)} tidak ada")
        if not targets:
            raise ToolError("isi `to` (nama anggota, atau [\"all\"])")
        if "staged" not in box:
            box["staged"] = ht.stage_skill(team, hermes, owner, skill)
            box["report"] = ht.scan_skill(hermes, owner["profile"], box["staged"][1],
                                          skipped_symlinks=box["staged"][2], allow_unscanned=allow_unscanned)
        staged, report = box["staged"], box["report"]
        skill_dir = staged[1]
        if report["problems"]:
            raise ToolError("skill tidak aman untuk dibagikan: " + " | ".join(report["problems"]))
        kb = max(1, report["bytes"] // 1024)
        verdict = report["guard"].get("verdict", "unknown")
        who = "semua anggota + channel bersama" if any(t.lower() in ("all", "semua") for t in targets) \
            else ", ".join(_q(t, 41) for t in targets)
        preview = (f"Bagikan skill {_q(skill_dir.name, 80)} milik {_q(owner['display_name'], 64)} ({owner_name}) ke "
                   f"{who}. {len(report['files'])} file, {kb} KB, pemindai Hermes: {_clean(verdict, 40)}."
                   + (" Kiriman lama bernama sama akan ditimpa." if overwrite else "")
                   + (" PERHATIAN: dibagikan walau pemindai Hermes tidak memberi hasil." if allow_unscanned and verdict in ("unknown", "") else ""))

        def run() -> str:
            result = ht.share_skill(team, hermes, skill, owner_name, targets, overwrite,
                                    staged=staged, scan=report, allow_unscanned=allow_unscanned)
            lines = [f"Skill '{result['skill']}' dari {result['from']} ({result['scope']}):"]
            lines += [f"- {name}: {status}" for name, status in sorted(result["results"].items())]
            lines.append("Penerima melihatnya di sesi baru (atau setelah /reload-skills), di kategori shared/.")
            return "\n".join(lines) + ctl._tail(hermes)

        return Plan(team, preview, run, skill=skill_dir.name, from_member=owner_name, to=targets, guard=verdict,
                    hash=report.get("hash"))

    def cleanup() -> None:
        if "staged" in box:
            shutil.rmtree(box["staged"][0], ignore_errors=True)

    return Planner(plan, cleanup)


@tool(
    "unshare_skill",
    description="Tarik kembali skill yang pernah dibagikan (dari semua penerima atau sebagian). Butuh konfirmasi.",
    schema={
        "type": "object",
        "properties": {
            "skill": {"type": "string"},
            "from_members": {"type": "array", "items": {"type": "string"},
                             "description": "[\"all\"] (default) atau daftar nama"},
        },
        "required": ["skill"],
        "additionalProperties": False,
    },
    mutating=True, event="unshare-skill", order=90,
)
def unshare_skill(ctl, args: dict) -> PlanFn:
    skill = str(args.get("skill") or "").strip()
    targets = [str(t) for t in (args.get("from_members") or ["all"])]

    def plan() -> Plan:
        team = ctl.team()
        if skill not in ht.shared_skill_registry(team)["skills"]:
            raise ToolError(f"skill {_q(skill, 80)} tidak ada di daftar kiriman (lihat team_status)")
        preview = f"Tarik skill kiriman {_q(skill, 80)} dari {', '.join(_q(t, 41) for t in targets)}."

        def run() -> str:
            hermes = ctl.hermes()
            result = ht.unshare_skill(team, hermes, skill, targets)
            done = ", ".join(sorted(result["results"])) or "tidak ada penerima"
            return f"Skill '{skill}' ditarik dari: {done}." + ctl._tail(hermes)

        return Plan(team, preview, run, skill=skill, targets=targets)

    return plan


@tool(
    "onboarding_status",
    description="Kondisi onboarding otomatis: onboarding yang DITAHAN (pemberi role bukan admin) atau gagal, "
                "user ber-@backend yang belum jadi anggota, jumlah user ber-role entry, status live reload rute.",
    schema={"type": "object", "properties": {}, "additionalProperties": False},
    order=120,
)
def onboarding_status(ctl, args: dict) -> str:
    team = ctl.team()
    ob = team["onboarding"]
    state = ctl._plugin_state(team)
    lines = [("Onboarding otomatis: AKTIF" if ob["enabled"] else "Onboarding otomatis: mati")
             + (" · pemberi @backend harus admin" if ob["verify_actor"] else " · pemberi @backend tidak dicek")
             + (f" · role entry {team['entry_role']}" if team["entry_role"] else " · tanpa role entry")]
    live = state.get("live") or {}
    if live:
        lines.append(f"Live reload rute: {'OK' if live.get('ok') else 'BERMASALAH'} — {_clean(live.get('detail'), 200)} "
                     f"({_clean(live.get('ts'), 40)})")
    else:
        lines.append("Live reload rute: belum ada laporan dari gateway (gateway belum jalan dengan plugin v3?)")
    held = state.get("held") or {}
    if held:
        lines.append("Onboarding DITAHAN (setujui dengan onboard_member):")
        lines += [f"- <@{_clean(uid, 24)}>: {_clean((info or {}).get('reason'), 200)} ({_clean((info or {}).get('ts'), 40)})"
                  for uid, info in sorted(held.items())]
    else:
        lines.append("Tidak ada onboarding yang ditahan.")
    try:
        members = ctl.api().list_members(team["guild_id"], limit=1000)
        active = {m["user_id"] for m in team["members"] if m["active"]}
        waiting = [Server.member_label(x) for x in members
                   if team["member_role"] in (x.get("roles") or []) and str(x["user"]["id"]) not in active
                   and not x["user"].get("bot")]
        lines.append("Punya @backend tapi belum anggota aktif: " + (", ".join(waiting) if waiting else "-")
                     + (" (PERINGATAN: daftar anggota server terpotong, hasil mungkin tidak lengkap)"
                        if getattr(members, "truncated", False) else ""))
        if team["entry_role"]:
            entry = sum(1 for x in members if team["entry_role"] in (x.get("roles") or []))
            lines.append(f"User ber-role entry (belum onboarding): {entry}")
    except ht.TeamError as exc:
        lines.append(f"(server Discord tidak bisa dicek: {exc})")
    return "\n".join(lines)


@tool(
    "onboard_member",
    description="Jadikan user Discord anggota tim sekarang: channel privat #hermes-<nama> (hanya dia + bot), "
                "profile + memory + .env + sandbox sendiri, rute gateway, role @backend bila belum, lalu bot "
                "mengirim pesan sambutan yang me-mention dia. Juga untuk MELANJUTKAN onboarding yang ditahan/gagal. "
                "Butuh konfirmasi.",
    schema={
        "type": "object",
        "properties": {
            "user": {"type": "string", "description": "user ID, <@mention>, atau username PERSIS"},
            "name": {"type": "string", "description": "opsional; default dari username (huruf kecil/angka/-/_)"},
            "model": {"type": "string", "description": "opsional; alias dari list_models"},
        },
        "required": ["user"],
        "additionalProperties": False,
    },
    mutating=True, event="onboard-member", order=130,
)
def onboard_member(ctl, args: dict) -> PlanFn:
    name = str(args.get("name") or "").strip().lower()
    model = str(args.get("model") or "").strip()

    def plan() -> Union[Plan, str]:
        team = ctl.team()
        srv = Server(team, ctl.api())
        member = srv.member(args.get("user"))
        user = member.get("user") or {}
        uid = str(user.get("id"))
        if user.get("bot"):
            raise ToolError("bot tidak bisa dijadikan anggota")
        if model and team["llm"] and model not in team["llm"]["models"]:
            raise ToolError(f"model {_q(model, 60)} tidak ada; pilihan: {', '.join(team['llm']['models'])}")
        if name:
            ht._check_new_name(team, name)
        label = srv.member_label(member)
        existing = next((m for m in team["members"] if m["user_id"] == uid), None)
        has_role = team["member_role"] in (member.get("roles") or [])
        if existing and existing["active"] and has_role:
            return f"{label} sudah anggota aktif: <#{existing['channel_id']}>, profile {existing['profile']}."
        if existing and existing["active"]:
            what = f"{label} sudah anggota aktif ({existing['profile']})."
        elif existing:
            what = (f"Aktifkan lagi {label} ({existing['name']}): ia diberi akses lagi ke channel pribadinya "
                    f"{ctl._channel_note(team, existing['channel_id'])} (dibuat ulang bila sudah tidak ada); "
                    f"profile {existing['profile']} dengan memory & secret lamanya.")
        else:
            preview_name = name or ht.derive_member_name(str(user.get("username") or ""), uid,
                                                         ht._taken_names(team, ctl.hermes()))
            effective = model or (team["llm"]["default_model"] if team["llm"] else "-")
            what = (f"Jadikan {label} anggota: channel privat #{team['channel_prefix']}{preview_name} (hanya dia + "
                    f"bot), profile {team['prefix']}{preview_name} (memory, .env, sandbox sendiri), model {effective}.")
        member_role = next((r for r in srv.roles() if str(r["id"]) == team["member_role"]), None)
        role_text = srv.role_label(member_role) if member_role else f"role anggota ({team['member_role']})"
        preview = (what + ("" if has_role else f" Beri {role_text}.")
                   + " Bot mengirim pesan sambutan yang me-mention dia.")

        def run() -> str:
            hermes = ctl.hermes()
            display = _clean(member.get("nick") or user.get("global_name") or user.get("username") or "", 64)
            result = ht.onboard_member(team, hermes, user_id=uid, username=str(user.get("username") or ""),
                                       display_name=display, name=name, model=model, api=srv.api)
            notes = list(result.get("notes", []))
            if not has_role:
                try:
                    srv.api.add_role(team["guild_id"], uid, team["member_role"],
                                     reason=srv.reason("onboarding oleh admin"))
                    notes.append("role @backend diberikan")
                except ht.TeamError as exc:
                    notes.append(f"role @backend belum diberikan ({exc}); berikan manual")
            m = result["member"]
            status = {"created": "dibuat", "reactivated": "diaktifkan lagi", "exists": "sudah ada"}.get(
                result["status"], result["status"])
            lines = [f"Anggota {m['display_name']} {status}: profile {m['profile']}, channel <#{m['channel_id']}>."]
            lines += [f"- {note}" for note in notes]
            lines.append(ctl._activation_note(team, result))
            return "\n".join(lines) + ctl._tail(hermes)

        return Plan(team, preview, run, user=uid)

    return plan
