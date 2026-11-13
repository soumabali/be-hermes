"""Bagian dari kit tim (dipecah dari hermes_team.py). Lihat docs/KONTRAK.md dan README."""
from __future__ import annotations
import argparse
import getpass
import json
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any, Optional
import kit_paths
from kit_base import KIT_VERSION, TeamError, rewrite_env_text
from kit.constants import PLUGIN_NAME
from kit.util import atomic_write
from kit.config import load_team, placeholder_ids, placeholder_message, placeholders_allowed, reload_team, team_lock
from kit.derive import _get_nested, active_members, fallback_warnings, host_memory_mb, host_settings, install_managed, managed_diff, managed_diff_clean, managed_diff_lines, managed_install_command, managed_yaml, model_alias, nested_yaml, sandbox_limits, sandbox_warnings, sandbox_worst_case_mb
from kit.hermes_cli import Hermes, resolve_hermes_bin
from kit.skills import list_skills, share_skill, unshare_skill
from kit.provision import cmd_apply
from kit.membership import add_member, load_batch_file, offboard_member, onboard_batch, onboard_member, remove_member, remove_member_plan, set_member_active, set_model, set_sandbox

CLI_DOC = """hermes_team.py — provisioning & operasi Hermes Agent untuk satu tim di Discord.

Satu instalasi Hermes, satu bot Discord, satu gateway (multiplex), satu profile per orang.

File (di $HERMES_TEAM_DIR, bawaan ~/.hermes-team/; atau folder team.yaml yang ditunjuk --config.
./team.yaml di direktori kerja TIDAK dipakai diam-diam):
  team.yaml     setelan tim, ditulis manusia: guild, role, admin, channel bersama/admin,
                katalog model, sandbox, batas skala, kebijakan secret, onboarding.
  members.yaml  daftar anggota + model per orang. Ditulis manusia ATAU otomatis: onboarding
                (role @backend diberikan), tool admin di chat (teamctl), add-member / set-model.
  state/        ditulis script: roster.json (dibaca plugin team-guard), shared-skills/, audit.log.

Perintah:
  check          validasi + peta channel → profile → model
  apply          buat/perbarui profile, plugin team-guard, MCP admin, setelan gateway, roster
                 (--only andi,budi untuk sebagian anggota; --host-only untuk gateway saja)
  managed        cetak /etc/hermes/config.yaml (kebijakan global yang tidak bisa diubah user);
                 --diff bandingkan dengan yang terpasang, --install pasang (sebagai root)
  host-yaml      cetak setelan gateway sebagai YAML (untuk dicek / ditempel manual)
  add-member     tambah anggota (opsional: buat channel Discord privat otomatis)
  onboard        anggota dari user Discord: channel privat + profile + rute (idempoten; dipanggil
                 plugin saat role @backend diberikan, juga oleh tool admin)
  offboard       nonaktifkan anggota dari user Discord (data tetap; dipanggil plugin saat role dicabut)
  set-model      ganti model AI anggota / shared / admin / lobby
  set-sandbox    batas RAM/CPU sandbox khusus satu anggota (--memory-mb, --cpu, --reset)
  set-active     aktif/nonaktifkan anggota berdasarkan nama
  remove-member  hapus PERMANEN anggota yang sudah nonaktif dari members.yaml
                 (--delete-profile, --delete-channel, --yes)
  list-skills    daftar skill milik seorang anggota
  share-skill    bagikan skill seorang anggota ke anggota lain atau semua anggota (ditolak bila pemindai
                 keamanan Hermes tidak memberi verdict "safe"; --allow-unscanned bila pemindai tidak ada)
  unshare-skill  tarik kembali skill yang pernah dibagikan
  llm-key        simpan API key LLM khusus satu profile (input tersembunyi; --stdin untuk pipe)
  status         ringkasan kondisi instalasi

Semua perintah yang mengubah sesuatu memegang kunci tim (<team_dir>/.team.lock), jadi onboarding
otomatis, tool admin, dan CLI tidak pernah saling menimpa.

Jalankan sebagai user Linux yang menjalankan Hermes (bukan root). Kesalahan tak terduga dicetak satu
baris; set HERMES_TEAM_DEBUG=1 untuk traceback lengkap.
"""



# --------------------------------------------------------------------------- tampilan
def describe(team: dict, hermes: Optional[Hermes] = None) -> str:
    lines = [f"Tim {team['team_name']} — guild {team['guild_id']}",
             f"Role dilayani: member {team['member_role']}, admin {team['admin_role']}",
             f"Admin: {', '.join(team['admins'])}",
             f"Rute pribadi: {'STRICT (hanya pemilik)' if team['strict'] else 'per channel'}; "
             f"rute admin: {'hanya user admin' if team['admin_strict'] else 'per channel'}", ""]
    llm = team["llm"]
    lines.append(f"{'channel':<21} {'profile':<18} {'model':<14} keterangan")
    for m in team["members"]:
        model = model_alias(team, "member", m) if llm else "-"
        state = "" if m["active"] else "  [NONAKTIF]"
        lines.append(f"{m['channel_id']:<21} {m['profile']:<18} {model:<14} pribadi {m['display_name']}{state}")
    for key, note in (("shared", "bersama, @mention → thread"), ("admin", "admin, tanpa @mention")):
        model = model_alias(team, key) if llm else "-"
        lines.append(f"{team[key]['channel_id']:<21} {team[key]['profile']:<18} {model:<14} {note}")
    lobby_model = model_alias(team, "lobby") if llm else "-"
    lines.append(f"{'(lainnya / DM)':<21} {'default':<18} {lobby_model:<14} lobby; DM ditolak")
    if llm:
        lines += ["", "Katalog model: " + ", ".join(f"{a} ({b['provider']}:{b['model']})" for a, b in llm["models"].items()),
                  f"Fallback: {', '.join(llm['fallback']) or '-'}; auxiliary: {llm['aux_model']}"]
    s = team["scale"]
    lines += ["", f"Skala: {s['max_concurrent_sessions']} sesi aktif, {s['user_rate_per_minute']} pesan/menit/orang "
              f"(burst {s['user_burst']}), antre maks {s['queue_max_wait_seconds']} dtk, busy={s['busy_input_mode']}",
              f"       {s['user_max_concurrent']} sesi/orang, LLM paralel {s['max_llm_parallel'] or 'tak dibatasi'}, "
              f"anggaran token/hari: orang {s['daily_token_budget'] or 'tanpa batas'}, "
              f"tim {s['team_daily_token_budget'] or 'tanpa batas'}; "
              f"cron anggota {'boleh' if s['allow_member_cron'] else 'mati'}, "
              f"/goal {'boleh' if s['allow_goals'] else 'mati'}, "
              f"retensi sesi {str(s['session_retention_days']) + ' hari' if s['session_retention_days'] else 'tidak diatur'}"]
    default_cpu, default_memory = sandbox_limits(team)
    total, host = sandbox_worst_case_mb(team), host_memory_mb()
    lines.append(f"Sandbox: {default_memory} MB, {default_cpu} CPU per profile; plafon RAM terburuk {total} MB "
                 f"({len(active_members(team))} anggota aktif + shared + admin)"
                 + (f" dari RAM server {host} MB" + (" — MELEBIHI" if total > host else "") if host else ""))
    for m in team["members"]:
        if m.get("sandbox"):
            cpu, memory = sandbox_limits(team, m)
            lines.append(f"         {m['name']}: {memory} MB, {cpu} CPU (pengecualian)"
                         + ("" if m["active"] else "  [NONAKTIF, tidak dihitung]"))
    ob = team["onboarding"]
    if ob["enabled"]:
        entry = f"role entry {team['entry_role']}" if team["entry_role"] else "tanpa role entry"
        lines.append(f"Onboarding otomatis: AKTIF ({entry}; @backend → channel + profile + sambutan; "
                     f"pemberi role {'harus admin' if ob['verify_actor'] else 'siapa pun yang punya izin Discord'}; "
                     f"role dicabut → {'nonaktif' if ob['offboard_on_role_remove'] else 'dibiarkan'})")
    else:
        lines.append("Onboarding otomatis: mati (pakai add-member / tool admin)")
    lines.append(f"Live reload rute: {'aktif' if team['live_reload'] else 'mati'}; bila gagal → "
                 f"{'restart halus otomatis saat sepi' if team['restart_fallback'] == 'idle' else 'admin mengetik /restart'}")
    if hermes is not None:
        lines.append("")
        for profile in [team["template"], *(m["profile"] for m in team["members"]),
                        team["shared"]["profile"], team["admin"]["profile"], "default"]:
            cfg = hermes.read_config(profile)
            ok = hermes.exists(profile)
            plugin = PLUGIN_NAME in (_get_nested(cfg, "plugins.enabled") or [])
            model = _get_nested(cfg, "model.default") or "-"
            lines.append(f"  {profile:<18} {'ada' if ok else 'BELUM ADA':<10} plugin={'ya' if plugin else 'TIDAK':<6} model={model}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- main
def _common_args() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=None,
                        help="team.yaml (default: $HERMES_TEAM_DIR/team.yaml, kalau tidak ~/.hermes-team/team.yaml; "
                             "./team.yaml TIDAK dipakai otomatis)")
    common.add_argument("--members", default=None, help="members.yaml (default: di samping team.yaml)")
    common.add_argument("--hermes-bin", default=None)
    common.add_argument("--hermes-root", default=os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
                        help="home profile default (default: ~/.hermes)")
    common.add_argument("--dry-run", action="store_true", help="tampilkan langkah tanpa menjalankannya")
    common.add_argument("--allow-placeholders", action="store_true",
                        help="check/apply: terima ID contoh dari *.example.yaml (hanya untuk uji; JANGAN di server)")
    common.add_argument("--allow-root", action="store_true",
                        help="izinkan jalan sebagai root (hanya kalau Hermes memang dipasang di user root)")
    return common


def _debug_enabled() -> bool:
    return os.environ.get("HERMES_TEAM_DEBUG", "").strip().lower() not in ("", "0", "false", "no", "off")


def main(argv: Optional[list[str]] = None) -> None:
    """Titik masuk CLI. Exception tak terduga dirapikan di sini: satu baris error + exit code ≠ 0
    (dan event `error` bila --events); traceback hanya dengan HERMES_TEAM_DEBUG=1."""
    args = _parse_args(argv)
    events = bool(getattr(args, "events", False))
    try:
        _run(args, events)
    except TeamError as exc:
        _fail(f"gagal: {exc}", events, code=1)
    except KeyboardInterrupt:
        _fail("dibatalkan", events, code=130)
    except Exception as exc:  # noqa: BLE001 — batas proses: jangan pernah menumpahkan traceback ke plugin/admin
        if _debug_enabled():
            traceback.print_exc()
        _fail(f"gagal (kesalahan tak terduga): {type(exc).__name__}: {exc}. "
              "Set HERMES_TEAM_DEBUG=1 untuk traceback.", events, code=2)


def _fail(message: str, events: bool, code: int) -> None:
    if events:
        print(json.dumps({"event": "error", "message": message}, ensure_ascii=False), flush=True)
        sys.exit(code)
    sys.exit(message)   # pesan ke stderr, exit 1


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    common = _common_args()
    parser = argparse.ArgumentParser(description=f"{CLI_DOC}\nVersi kit: {KIT_VERSION}",
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"hermes-team-kit {KIT_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", parents=[common], help="validasi konfigurasi")
    p = sub.add_parser("apply", parents=[common], help="terapkan konfigurasi ke Hermes")
    p.add_argument("--only", default="", help="nama anggota dipisah koma")
    p.add_argument("--host-only", action="store_true")
    p.add_argument("--force-soul", action="store_true", help="timpa SOUL.md yang sudah ada (backup .bak)")
    p = sub.add_parser("managed", parents=[common], help="cetak / bandingkan / pasang /etc/hermes/config.yaml")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--diff", action="store_true",
                      help="bandingkan dengan berkas yang terpasang; exit 1 bila ada beda")
    mode.add_argument("--install", action="store_true",
                      help="tulis berkasnya secara atomik (0644); harus root — tanpa root mencetak perintah sudo persisnya")
    sub.add_parser("host-yaml", parents=[common], help="cetak setelan gateway sebagai YAML")
    sub.add_parser("status", parents=[common], help="ringkasan kondisi")
    p = sub.add_parser("add-member", parents=[common], help="tambah anggota")
    p.add_argument("name")
    p.add_argument("discord_user_id")
    p.add_argument("--display-name", default="")
    p.add_argument("--model", default="")
    p.add_argument("--channel-id", default="", help="pakai channel yang sudah ada (tanpa membuat baru)")
    p = sub.add_parser("onboard", parents=[common], help="jadikan user Discord anggota (dipanggil plugin)")
    p.add_argument("--user-id", required=True)
    p.add_argument("--username", default="", help="username Discord (dasar nama anggota)")
    p.add_argument("--display-name", default="")
    p.add_argument("--name", default="", help="paksa nama anggota (default: dari username)")
    p.add_argument("--model", default="")
    p.add_argument("--events", action="store_true", help="keluaran JSON per baris (untuk plugin)")
    p = sub.add_parser("onboard-batch", parents=[common],
                       help="onboarding massal: semua channel dulu, lalu SATU apply")
    p.add_argument("--file", required=True,
                   help="berkas YAML/JSON: daftar {user_id, username?, display_name?, name?, model?}")
    p.add_argument("--events", action="store_true", help="keluaran JSON per baris (untuk plugin)")
    p = sub.add_parser("offboard", parents=[common], help="nonaktifkan anggota dari user Discord (data tetap)")
    p.add_argument("--user-id", required=True)
    p.add_argument("--keep-channel-access", action="store_true", help="jangan cabut izin melihat channel")
    p.add_argument("--events", action="store_true")
    p = sub.add_parser("set-model", parents=[common], help="ganti model")
    p.add_argument("target", help="nama anggota | all | shared | admin | lobby")
    p.add_argument("model", help="alias di llm.models")
    p = sub.add_parser("set-sandbox", parents=[common], help="batas RAM/CPU sandbox khusus satu anggota")
    p.add_argument("member", help="nama anggota")
    p.add_argument("--memory-mb", default=None, help="RAM sandbox dalam MB (256..65536)")
    p.add_argument("--cpu", default=None, help="jumlah CPU (0.25..64)")
    p.add_argument("--reset", action="store_true", help="hapus pengecualian (kembali ke team.yaml → sandbox)")
    p = sub.add_parser("set-active", parents=[common], help="aktif/nonaktifkan anggota")
    p.add_argument("name")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("remove-member", parents=[common],
                       help="hapus PERMANEN anggota yang sudah nonaktif (lihat juga --dry-run)")
    p.add_argument("name")
    p.add_argument("--delete-profile", action="store_true",
                   help="hapus juga profile Hermes-nya (memory, secret, sesi) lewat `hermes profile delete`")
    p.add_argument("--delete-channel", action="store_true", help="hapus juga channel Discord pribadinya")
    p.add_argument("--yes", action="store_true", help="jangan minta konfirmasi")
    p = sub.add_parser("list-skills", parents=[common], help="daftar skill anggota")
    p.add_argument("member")
    p = sub.add_parser("share-skill", parents=[common], help="bagikan skill")
    p.add_argument("skill")
    p.add_argument("--from", dest="from_member", required=True)
    p.add_argument("--to", required=True, help="nama dipisah koma, atau all, atau shared")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--allow-unscanned", action="store_true",
                   help="tetap bagikan walau pemindai keamanan Hermes tidak bisa dijalankan (keputusan admin)")
    p = sub.add_parser("unshare-skill", parents=[common], help="tarik skill yang dibagikan")
    p.add_argument("skill")
    p.add_argument("--from-members", default="all", help="nama dipisah koma, atau all")
    p = sub.add_parser("llm-key", parents=[common], help="simpan API key LLM khusus satu profile")
    p.add_argument("profile", help="nama anggota, shared, admin, atau default")
    p.add_argument("--stdin", action="store_true",
                   help="baca key dari stdin (satu baris), untuk dipakai dari skrip/pipe")
    return parser.parse_args(argv)


def _run(args: argparse.Namespace, events: bool) -> None:
    team_file = kit_paths.team_file(args.config)
    notice = None if args.config else kit_paths.cwd_notice(team_file)
    if notice:
        print(notice, file=sys.stderr)      # stderr: stdout dipakai `managed`, `host-yaml`, dan --events
    try:
        team = load_team(team_file, Path(args.members).expanduser() if args.members else None)
    except TeamError as exc:
        _fail(f"konfigurasi: {exc}", events, code=1)
    hermes_bin = resolve_hermes_bin(args.hermes_bin)
    hermes = Hermes(hermes_bin, Path(args.hermes_root), dry_run=args.dry_run, quiet=events)
    placeholders = [] if placeholders_allowed(args.allow_placeholders) else placeholder_ids(team)

    if args.command == "check":
        print(describe(team))
        _print_warnings(team)
        if placeholders:
            sys.exit("\nKonfigurasi BELUM siap: " + placeholder_message(placeholders))
        print("\nKonfigurasi valid.")
        return
    if args.command == "managed":
        _cmd_managed(team, args)
        return
    if args.command == "host-yaml":
        print(nested_yaml(host_settings(team)), end="")
        return
    if args.command == "status":
        print(describe(team, hermes))
        _print_warnings(team)
        return
    if args.command == "apply" and placeholders:
        sys.exit("apply ditolak: " + placeholder_message(placeholders))
    if args.command == "list-skills":
        member = next((m for m in team["members"] if m["name"] == args.member), None)
        if member is None:
            sys.exit(f"anggota '{args.member}' tidak ada")
        for s in list_skills(hermes, member["profile"]):
            print(f"{s['path']:<40} {s['description']}")
        return

    if os.geteuid() == 0 and not args.dry_run and not args.allow_root:
        sys.exit("Jangan jalankan sebagai root; pakai user Linux yang menjalankan Hermes.")
    if not args.dry_run and not Path(hermes_bin).exists() and not shutil.which(hermes_bin):
        sys.exit(f"CLI hermes tidak ditemukan ({hermes_bin}). Pasang Hermes dulu atau pakai --hermes-bin.")
    if args.command in ("onboard", "offboard"):
        _cmd_membership(team, hermes, args, events)
        return
    if args.command == "onboard-batch":
        _cmd_onboard_batch(team, hermes, args, events)
        return
    try:
        if args.command == "apply":
            only = [n.strip() for n in args.only.split(",") if n.strip()] or None
            result = cmd_apply(team, hermes, only=only, host_only=args.host_only, force_soul=args.force_soul)
            _print_apply_tail(team, hermes, result)
        elif args.command == "add-member":
            result = add_member(team, hermes, name=args.name, user_id=args.discord_user_id,
                                display_name=args.display_name, model=args.model, channel_id=args.channel_id,
                                create_channel=not args.channel_id)
            for note in result["notes"]:
                print(f"  • {note}")
            _print_apply_tail(team, hermes, result)
        elif args.command == "set-model":
            result = set_model(team, hermes, args.target, args.model)
            print(f"Model {result['target']} → {result['model']} ({', '.join(result['profiles'])}). "
                  "Berlaku untuk sesi baru (/new).")
            _print_warnings(team, hermes)
        elif args.command == "set-sandbox":
            result = set_sandbox(team, hermes, args.member, memory_mb=args.memory_mb, cpu=args.cpu,
                                 reset=args.reset)
            kind = "pengecualian" if result["override"] else "bawaan tim"
            print(f"Sandbox {result['name']} ({result['profile']}): {result['memory_mb']} MB, {result['cpu']} CPU "
                  f"({kind}). Plafon RAM terburuk tim: {result['worst_case_mb']} MB.")
            if not result["applied"]:
                print("Anggota ini nonaktif: tersimpan di members.yaml, diterapkan saat ia diaktifkan lagi.")
            else:
                print("Berlaku saat container sandbox profile itu dibuat lagi; container yang sudah ada tetap "
                      "memakai batas lama sampai dihapus (docker rm) — /workspace tersimpan di volume.")
            _print_warnings(team, hermes)
        elif args.command == "set-active":
            result = set_member_active(team, hermes, args.name, args.state == "on")
            _print_apply_tail(team, hermes, result)
        elif args.command == "remove-member":
            _cmd_remove_member(team, hermes, args)
        elif args.command == "share-skill":
            result = share_skill(team, hermes, args.skill, args.from_member, args.to.split(","), args.overwrite,
                                 allow_unscanned=args.allow_unscanned)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "unshare-skill":
            print(json.dumps(unshare_skill(team, hermes, args.skill, args.from_members.split(",")), indent=2,
                             ensure_ascii=False))
        elif args.command == "llm-key":
            _cmd_llm_key(team, hermes, args.profile, use_stdin=args.stdin)
    except TeamError as exc:
        sys.exit(f"gagal: {exc}")
    if hermes.failures:
        sys.exit(1)


def _cmd_managed(team: dict, args: argparse.Namespace) -> None:
    if args.diff:
        diff = managed_diff(team)
        if managed_diff_clean(diff):
            print(f"✓ {diff['path']} sama dengan kebijakan dari team.yaml.")
            return
        print(f"{diff['path']} BERBEDA dari kebijakan team.yaml:")
        for line in managed_diff_lines(diff):
            print(f"  • {line}")
        print(f"Pasang ulang:\n  {managed_install_command(team)}")
        if diff["moved"]:
            print("Urutan untuk key yang dipindah ke config per profile: apply → managed --install → apply.")
        sys.exit(1)
    if args.install:
        try:
            path = install_managed(team, dry_run=args.dry_run)
        except TeamError as exc:
            sys.exit(str(exc))
        if args.dry_run:
            print(f"(dry-run) akan menulis {path} (0644)")
            return
        print(f"✓ {path} ditulis (0644). Sebagai user Hermes: `hermes_team.py apply` lalu `hermes gateway restart` "
              "(atau /restart di Discord).")
        return
    print(managed_yaml(team), end="")


def _cmd_remove_member(team: dict, hermes: Hermes, args: argparse.Namespace) -> None:
    opts = {"delete_profile": args.delete_profile, "delete_channel": args.delete_channel}
    plan = remove_member_plan(team, hermes, args.name, **opts)
    member = plan["member"]
    print(f"Hapus permanen anggota {member['name']} ({member['display_name']}, user {member['user_id']}):")
    print("  AKAN DIHAPUS:")
    for line in plan["will_delete"]:
        print(f"    - {line}")
    print("  TIDAK dihapus:")
    for line in plan["kept"]:
        print(f"    - {line}")
    if hermes.dry_run:
        print("\n(dry-run) tidak ada yang diubah.")
        return
    if not args.yes:
        if not sys.stdin or not sys.stdin.isatty():
            raise TeamError("butuh konfirmasi: jalankan interaktif, atau tambah --yes")
        if input(f"Ketik nama anggota ({member['name']}) untuk melanjutkan: ").strip() != member["name"]:
            raise TeamError("dibatalkan (nama tidak cocok)")
    result = remove_member(team, hermes, args.name, **opts)
    print("\nSudah dilakukan:")
    for line in result["done"]:
        print(f"  ✓ {line}")
    for line in result["notes"]:
        print(f"  ! {line}")
    print("Masih ada (tidak dihapus):")
    for line in result["kept"]:
        print(f"  - {line}")


def _cmd_membership(team: dict, hermes: Hermes, args: argparse.Namespace, events: bool) -> None:
    """onboard / offboard. Dengan --events: satu objek JSON per baris di stdout (dibaca plugin)."""
    def emit(event: str, **fields: Any) -> None:
        if events:
            print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)
        elif event == "channel":
            print(f"  • channel {fields.get('channel_id')}" + (" (baru)" if fields.get("created") else ""))

    try:
        if args.command == "onboard":
            result = onboard_member(team, hermes, user_id=args.user_id, username=args.username,
                                    display_name=args.display_name, name=args.name, model=args.model, emit=emit)
        else:
            result = offboard_member(team, hermes, user_id=args.user_id, hide_channel=not args.keep_channel_access)
    except TeamError as exc:
        if events:
            emit("error", message=str(exc), log=hermes.log[-15:])
            sys.exit(1)
        sys.exit(f"gagal: {exc}")
    summary = _membership_summary(team, result, args.user_id)
    if events:
        emit("result", **summary)
        sys.exit(3 if summary["failures"] else 0)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if summary["failures"]:
        sys.exit(1)


def _membership_summary(team: dict, result: dict, user_id: str) -> dict:
    """Isi event `result` (dan keluaran JSON tanpa --events) untuk satu anggota."""
    member = result.get("member") or {}
    return {
        "status": result["status"], "user_id": user_id, "name": member.get("name"),
        "display_name": member.get("display_name"), "profile": member.get("profile"),
        "channel_id": member.get("channel_id"),
        "model": model_alias(team, "member", member) if (team["llm"] and member) else "",
        "notes": result.get("notes", []), "restart_needed": bool(result.get("restart_needed")),
        "live_changed": result.get("live_changed", []), "failures": result.get("failures", []),
    }


def _cmd_onboard_batch(team: dict, hermes: Hermes, args: argparse.Namespace, events: bool) -> None:
    """onboard-batch. Dengan --events: baris `channel` dan `result` per anggota (bentuknya sama dengan
    `onboard`, ditambah `user_id` di baris `channel`), lalu satu baris `summary`. Exit 3 bila ada yang gagal."""
    def emit(event: str, **fields: Any) -> None:
        if events:
            print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)
        elif event == "channel":
            print(f"  • {fields.get('user_id')}: channel {fields.get('channel_id')}"
                  + (" (baru)" if fields.get("created") else ""))

    try:
        result = onboard_batch(team, hermes, load_batch_file(Path(args.file)), emit=emit)
    except TeamError as exc:
        if events:
            emit("error", message=str(exc), log=hermes.log[-15:])
            sys.exit(1)
        sys.exit(f"gagal: {exc}")
    rows = [_membership_summary(team, {**m, "restart_needed": result["restart_needed"],
                                       "live_changed": result["live_changed"]}, m["user_id"])
            for m in result["members"]]
    counts: dict = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    summary = {"total": len(rows), "counts": counts,
               "failed": [{"user_id": r["user_id"], "name": r["name"], "error": (r["failures"] or [""])[0]}
                          for r in rows if r["status"] == "failed"],
               "restart_needed": result["restart_needed"], "live_changed": result["live_changed"],
               "failures": result["failures"]}
    if events:
        for row in rows:
            emit("result", **row)
        emit("summary", **summary)
    else:
        for row in rows:
            mark = "✗" if row["status"] == "failed" else "✓"
            print(f"  {mark} {row['name']} ({row['user_id']}): {row['status']}"
                  + (f" — {row['failures'][0]}" if row["failures"] else ""))
            for note in row["notes"]:
                print(f"      • {note}")
        print(f"\n{len(rows)} entri: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
        _print_apply_tail(team, hermes, result)
    if summary["failures"]:
        sys.exit(3)


def _llm_key_target(team: dict, who: str) -> tuple[str, str]:
    """(profile, nama env key) untuk `llm-key <who>`."""
    if not team["llm"]:
        raise TeamError("team.yaml belum punya bagian llm")
    if who in ("shared", "admin"):
        profile, alias = team[who]["profile"], model_alias(team, who)
    elif who in ("default", "lobby"):
        profile, alias = "default", model_alias(team, "lobby")
    else:
        member = next((m for m in team["members"] if m["name"] == who), None)
        if member is None:
            raise TeamError(f"'{who}' tidak dikenal")
        profile, alias = member["profile"], model_alias(team, "member", member)
    provider = team["llm"]["models"][alias]["provider"]
    key_env = (team["llm"]["providers"].get(provider.removeprefix("custom:")) or {}).get("key_env")
    if not key_env:
        raise TeamError(f"provider {provider} tidak punya key_env di team.yaml")
    return profile, str(key_env)


def _cmd_llm_key(team: dict, hermes: Hermes, who: str, *, use_stdin: bool = False) -> None:
    profile, key_env = _llm_key_target(team, who)
    if use_stdin:
        value = sys.stdin.readline().strip()
    elif not sys.stdin or not sys.stdin.isatty():
        # Tanpa TTY getpass jatuh ke input biasa (bisa menggemakan key / menggantung di cron).
        raise TeamError("stdin bukan terminal: jalankan interaktif, atau kirim key lewat pipe dengan --stdin")
    else:
        value = getpass.getpass(f"{key_env} untuk profile {profile} (tidak ditampilkan): ").strip()
    if not value:
        raise TeamError("kosong, dibatalkan")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise TeamError("key tidak boleh berisi baris baru")
    # Baca-ubah-tulis .env di bawah kunci tim; tim dibaca ulang supaya profile tujuannya masih benar.
    with team_lock(team):
        reload_team(team)
        profile, key_env = _llm_key_target(team, who)
        path = hermes.home(profile) / ".env"
        if hermes.dry_run:
            print(f"(dry-run) {key_env} akan disimpan di {path}")
            return
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        atomic_write(path, rewrite_env_text(text, key_env, value), mode=0o600)
    print(f"✓ {key_env} disimpan di {path}. Ingat: nilai di /etc/hermes/.env (kalau ada) selalu menang.")


def managed_blocked_warning(hermes: Hermes) -> list[str]:
    if not hermes.managed_blocked:
        return []
    return [f"/etc/hermes/config.yaml masih mem-pin {', '.join(sorted(hermes.managed_blocked))} (file dari kit "
            "versi lama), jadi nilai per profile BELUM berlaku. Pasang ulang managed config "
            "(`sudo … hermes_team.py managed --install`; perintah persisnya: `hermes_team.py managed --diff`), "
            "lalu jalankan `apply` sekali lagi."]


def _print_warnings(team: dict, hermes: Optional[Hermes] = None) -> None:
    """Peringatan konfigurasi (tidak menggagalkan `check`/`apply`)."""
    warnings = list(team.get("warnings") or []) + fallback_warnings(team) + sandbox_warnings(team)
    if hermes is not None:
        warnings += managed_blocked_warning(hermes)
    if warnings:
        print("\n⚠️  Peringatan:")
        for line in warnings:
            print(f"  • {line}")


def _print_apply_tail(team: dict, hermes: Hermes, result: dict) -> None:
    print("\nSelesai." if not hermes.failures else f"\nSelesai dengan {len(hermes.failures)} kegagalan:")
    for failure in hermes.failures:
        print(f"  ✗ {failure}")
    _print_warnings(team, hermes)
    known = {m["profile"] for m in team["members"]} | {team["template"]}
    profiles_dir = hermes.root / "profiles"
    stale = sorted(d.name for d in profiles_dir.iterdir()
                   if d.is_dir() and team["prefix"] and d.name.startswith(team["prefix"])
                   and d.name not in known) if profiles_dir.is_dir() else []
    if stale:
        print(f"Profile berawalan sama yang tidak ada di members.yaml: {', '.join(stale)}")
    if result.get("restart_needed"):
        print("\n⚠️  Setelan gateway berubah → restart gateway: ketik /restart di Discord (admin) atau "
              "`hermes gateway restart`.")
    elif result.get("live_changed") or result.get("created"):
        print("\nRute/channel baru diterapkan plugin team-guard ke gateway yang sedang jalan dalam beberapa detik "
              "(live reload; kabar muncul di channel admin). Gateway belum jalan? Cukup start seperti biasa.")
    print("\nKalau kebijakan global berubah (model, sandbox, skala):\n"
          "  python3 hermes_team.py managed --diff       # beda dengan /etc/hermes/config.yaml yang terpasang\n"
          f"  {managed_install_command(team)}")

