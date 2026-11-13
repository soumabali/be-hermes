"""Bagian dari kit tim (dipecah dari hermes_team.py). Lihat docs/KONTRAK.md dan README."""
from __future__ import annotations
import base64
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Optional
import yaml
from kit_base import TeamError
from kit.util import atomic_write, now_iso
from kit.config import _lock_depth, reload_team, team_lock
from kit.derive import active_members
from kit.hermes_cli import Hermes


# --------------------------------------------------------------------------- skill
_SKIP_DIRS = {".hub", ".archive", ".git", "__pycache__", ".scan-cache", "node_modules"}


SHARED_CATEGORY = "shared"


def _frontmatter(skill_md: Path) -> dict:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    with contextlib.suppress(yaml.YAMLError):
        data = yaml.safe_load(text[3:end])
        return data if isinstance(data, dict) else {}
    return {}


def list_skills(hermes: Hermes, profile: str) -> list[dict]:
    root = hermes.home(profile) / "skills"
    out = []
    if not root.is_dir():
        return out
    for skill_md in sorted(root.rglob("SKILL.md")):
        rel = skill_md.parent.relative_to(root)
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        meta = _frontmatter(skill_md)
        out.append({"name": str(meta.get("name") or skill_md.parent.name), "dir": skill_md.parent.name,
                    "path": str(rel), "description": str(meta.get("description") or "")[:200],
                    "shared": rel.parts[0] == SHARED_CATEGORY if rel.parts else False})
    return out


def _find_skill(hermes: Hermes, profile: str, skill: str) -> Path:
    matches = [s for s in list_skills(hermes, profile) if skill in (s["name"], s["dir"], s["path"])]
    if not matches:
        raise TeamError(f"skill '{skill}' tidak ditemukan di profile {profile}")
    if len(matches) > 1:
        raise TeamError(f"skill '{skill}' ambigu: {', '.join(s['path'] for s in matches)} — sebut path lengkapnya")
    return hermes.home(profile) / "skills" / matches[0]["path"]


def shared_skill_registry(team: dict) -> dict:
    path = Path(team["paths"]["state_dir"]) / "shared-skills" / "registry.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"skills": {}}
    return data if isinstance(data, dict) and isinstance(data.get("skills"), dict) else {"skills": {}}


def _save_registry(team: dict, data: dict) -> None:
    path = Path(team["paths"]["state_dir"]) / "shared-skills" / "registry.json"
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=0o600)


def _profile_registry_path(hermes: Hermes, profile: str) -> Path:
    return hermes.home(profile) / "skills" / SHARED_CATEGORY / ".team-shared.json"


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(file.relative_to(path)).encode() + b"\0")
        digest.update(file.read_bytes())
    return "sha256:" + digest.hexdigest()[:16]


SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|CREDENTIALS?|AUTH|PAT|PRIVATE_KEY|DSN)(?:_|$)")


_LEAK_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-(?:proj-|ant-[a-z0-9]+-)?[A-Za-z0-9_-]{20,}\b"), re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]


def _profile_secret_values(hermes: Hermes, profile: str, min_length: int = 6) -> dict[str, str]:
    """SEMUA nilai di `.env` pemilik yang cukup panjang untuk dicocokkan (≥ secrets.min_length), apa pun
    namanya — /env-set menerima nama bebas (mis. `STRIPE`, `DB_URL`), jadi menyaring berdasarkan nama
    meloloskan secret. Nama yang terdaftar di indeks team-guard ikut walau lebih pendek dari min_length."""
    env = hermes.read_env(profile)
    index_path = hermes.home(profile) / "team-guard" / "secrets.json"
    registered: set = set()
    try:   # indeks rusak / bukan mapping → diabaikan, pemindaian tetap jalan dari .env
        data = json.loads(index_path.read_text(encoding="utf-8", errors="replace"))
        keys = data.get("keys") if isinstance(data, dict) else None
        if isinstance(keys, (dict, list)):
            registered = {str(k) for k in keys}
    except (OSError, ValueError):
        pass
    try:
        floor = max(1, int(min_length))
    except (TypeError, ValueError):
        floor = 6
    return {k: v for k, v in env.items()
            if v and (len(v) >= floor or (k in registered and len(v) >= min(floor, 6)))}


def secret_variants(value: str) -> set[str]:
    """Bentuk yang ikut diperiksa: asli, base64 (std/urlsafe, dengan/tanpa padding), url-encode, hex,
    dibalik, huruf besar/kecil — sama dengan penyamar di plugin team-guard."""
    raw = value.encode("utf-8")
    b64 = base64.b64encode(raw).decode()
    found = {value, b64, b64.rstrip("="), base64.urlsafe_b64encode(raw).decode().rstrip("="),
             urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value), raw.hex(), raw.hex().upper(),
             value[::-1], value.upper(), value.lower()}
    return {v for v in found if len(v) >= 6}


def scan_skill(hermes: Hermes, source_profile: str, skill_dir: Path, max_bytes: int = 2_000_000,
               skipped_symlinks: Iterable[str] = (), *, allow_unscanned: bool = False,
               min_length: int = 6) -> dict:
    """Periksa skill sebelum dibagikan: ukuran, symlink, nilai secret pemilik (dan bentuk encode-nya),
    pola kredensial, plus pemindai keamanan Hermes (tools.skills_guard).

    Fail-closed: hanya verdict "safe" yang lolos. "caution"/"dangerous" → ditolak. Pemindai tidak ada /
    timeout / error ("unknown") → ditolak, kecuali admin sengaja memberi `allow_unscanned=True`."""
    files, total, problems = [], 0, [f"symlink tidak diizinkan: {p}" for p in skipped_symlinks]
    for path in sorted(skill_dir.rglob("*")):
        rel = str(path.relative_to(skill_dir))
        if path.is_symlink():
            problems.append(f"symlink tidak diizinkan: {rel}")
            continue
        if path.is_file():
            size = path.stat().st_size
            total += size
            files.append({"path": rel, "bytes": size})
    if total > max_bytes:
        problems.append(f"ukuran total {total} byte melebihi batas {max_bytes}")
    if len(files) > 300:
        problems.append(f"terlalu banyak file ({len(files)})")
    needles = {name: {v.encode("utf-8") for v in secret_variants(value)}
               for name, value in _profile_secret_values(hermes, source_profile, min_length).items()}
    leaked_names, pattern_hits = set(), set()
    for item in files:
        try:
            blob = (skill_dir / item["path"]).read_bytes()
        except OSError:
            continue
        for name, variants in needles.items():
            if any(v in blob for v in variants):
                leaked_names.add(name)
        text = blob.decode("utf-8", "replace")
        if any(regex.search(text) for regex in _LEAK_PATTERNS):
            pattern_hits.add(item["path"])
    if leaked_names:
        problems.append("berisi nilai secret pemilik: " + ", ".join(sorted(leaked_names)))
    if pattern_hits:
        problems.append("berisi pola kredensial di: " + ", ".join(sorted(pattern_hits)))
    guard = _hermes_skill_guard(hermes, skill_dir)
    verdict = guard["verdict"]
    detail = "; ".join(guard["findings"][:5]) or "tanpa rincian"
    if verdict in ("dangerous", "caution"):
        problems.append(f"pemindai Hermes: {verdict} — {detail}")
    elif verdict != "safe":
        if allow_unscanned:
            guard["unscanned_allowed"] = True
        else:
            problems.append(f"pemindai keamanan Hermes tidak memberi hasil ({detail}); skill tidak dibagikan tanpa "
                            "dipindai. Perbaiki instalasi Hermes (`hermes doctor`), atau admin boleh melewatinya "
                            "secara sadar dengan --allow-unscanned")
    return {"files": files, "bytes": total, "problems": problems, "guard": guard, "hash": _tree_hash(skill_dir)}


def stage_skill(team: dict, hermes: Hermes, owner: dict, skill: str) -> tuple[Path, Path, list[str]]:
    """Salin skill pemilik ke folder staging (symlink tidak ikut). Yang dipindai, dikonfirmasi admin,
    dan dipasang ke penerima adalah salinan ini — perubahan pemilik setelahnya tidak ikut."""
    skill_dir = _find_skill(hermes, owner["profile"], skill)
    if skill_dir.relative_to(hermes.home(owner["profile"]) / "skills").parts[0] == SHARED_CATEGORY:
        raise TeamError(f"'{skill}' adalah kiriman dari anggota lain; bagikan dari pemilik aslinya")
    staging_root = Path(team["paths"]["state_dir"]) / "shared-skills" / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"{skill_dir.name}.", dir=str(staging_root)))
    skipped: list[str] = []

    def _no_symlinks(directory: str, names: list[str]) -> list[str]:
        bad = [n for n in names if os.path.islink(os.path.join(directory, n))]
        skipped.extend(str(Path(directory, n).relative_to(skill_dir)) for n in bad)
        return bad

    staged = workdir / skill_dir.name
    shutil.copytree(skill_dir, staged, symlinks=True, ignore=_no_symlinks)
    return workdir, staged, skipped


def _hermes_python(hermes_bin: str) -> Optional[str]:
    """Interpreter venv Hermes, dibaca dari shebang launcher `hermes`."""
    try:
        with open(hermes_bin, "rb") as fh:
            first = fh.readline().decode("utf-8", "replace").strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    parts = shlex.split(first[2:])
    if not parts:
        return None
    if Path(parts[0]).name == "env" and len(parts) > 1:
        return shutil.which(parts[-1])
    return parts[0] if "python" in Path(parts[0]).name else None


GUARD_VERDICTS = ("safe", "caution", "dangerous")


def _hermes_skill_guard(hermes: Hermes, skill_dir: Path) -> dict:
    """Jalankan tools.skills_guard milik Hermes (pemindai yang sama dengan Skills Hub).
    Selalu mengembalikan {"verdict": safe|caution|dangerous|unknown, "findings": [str, ...]}."""
    def unknown(why: str) -> dict:
        return {"verdict": "unknown", "findings": [why]}

    python = _hermes_python(hermes.binary)
    if not python:
        return unknown("pemindai Hermes tidak ditemukan")
    code = ("import json,sys\nfrom pathlib import Path\nfrom tools.skills_guard import scan_skill\n"
            "r=scan_skill(Path(sys.argv[1]),source='agent-created')\n"
            "print(json.dumps({'verdict':r.verdict,'findings':[f'{f.severity}:{f.file}:{f.line} {f.description}' "
            "for f in r.findings][:20]}))")
    # Direktori kerja kosong + PYTHONSAFEPATH: dengan `python -c`, direktori saat ini masuk sys.path, jadi
    # `tools/` atau `json.py` di folder tempat perintah dijalankan bisa menggantikan pemindai aslinya.
    env = hermes.env()
    env["PYTHONSAFEPATH"] = "1"
    try:
        workdir = tempfile.mkdtemp(prefix="hermes-team-scan.")
    except OSError as exc:
        return unknown(f"{type(exc).__name__}: {exc}")
    try:
        proc = subprocess.run([python, "-c", code, str(Path(skill_dir).resolve())], text=True, capture_output=True,
                              timeout=120, env=env, stdin=subprocess.DEVNULL, cwd=workdir)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return unknown(type(exc).__name__)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    lines = proc.stdout.strip().splitlines()
    if proc.returncode == 0 and lines:
        data: Any = None
        with contextlib.suppress(ValueError):
            data = json.loads(lines[-1])
        if isinstance(data, dict):
            verdict = str(data.get("verdict") or "").strip().lower()
            findings = data.get("findings")
            findings = [str(f) for f in findings] if isinstance(findings, list) else []
            if verdict in GUARD_VERDICTS:
                return {"verdict": verdict, "findings": findings}
            return unknown(f"verdict tidak dikenal: {verdict or '-'}")
    err = (proc.stderr or "").strip().splitlines()
    return unknown(err[-1][:300] if err else f"exit {proc.returncode}")


def _copy_shared_skill(team: dict, hermes: Hermes, skill: str, entry: dict, profile: str,
                       overwrite: bool = False) -> str:
    """Salin dari arsip state/shared-skills ke profile tujuan. Kembalikan status singkat."""
    source = Path(team["paths"]["state_dir"]) / "shared-skills" / skill
    if not (source / "SKILL.md").exists():
        raise TeamError(f"arsip skill '{skill}' tidak ada")
    return _install_skill_copy(hermes, source, skill, entry, profile, overwrite)


def _install_skill_copy(hermes: Hermes, source: Path, skill: str, entry: dict, profile: str,
                        overwrite: bool) -> str:
    dest_root = hermes.home(profile) / "skills" / SHARED_CATEGORY
    dest = dest_root / skill
    reg_path = _profile_registry_path(hermes, profile)
    registry: dict = {}
    with contextlib.suppress(OSError, ValueError):
        registry = json.loads(reg_path.read_text(encoding="utf-8"))
    names = {skill, str(_frontmatter(source / "SKILL.md").get("name") or skill)}
    conflict = [s for s in list_skills(hermes, profile)
                if (s["name"] in names or s["dir"] in names) and not s["path"].startswith(f"{SHARED_CATEGORY}/")]
    if conflict:
        return f"dilewati: sudah punya skill bernama '{skill}' sendiri ({conflict[0]['path']})"
    if dest.exists() and not overwrite and registry.get(skill, {}).get("origin_profile") != entry.get("origin_profile"):
        return "dilewati: sudah ada kiriman lain dengan nama sama (pakai overwrite)"
    if hermes.dry_run:
        return "dry-run"
    dest_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{skill}.", dir=str(dest_root)))
    try:
        shutil.copytree(source, tmp / skill, symlinks=True)
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(tmp / skill, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    registry[skill] = {k: entry[k] for k in ("origin_member", "origin_profile", "shared_at", "hash") if k in entry}
    atomic_write(reg_path, json.dumps(registry, indent=2, ensure_ascii=False) + "\n", mode=0o600)
    return "terpasang"


def share_skill(team: dict, hermes: Hermes, skill: str, from_member: str, to: Iterable[str],
                overwrite: bool = False, staged: Optional[tuple[Path, Path, list[str]]] = None,
                scan: Optional[dict] = None, allow_unscanned: bool = False) -> dict:
    with team_lock(team):
        if _lock_depth() == 1:
            reload_team(team)   # daftar anggota/tujuan dibaca ulang setelah kunci dipegang
        return _share_skill(team, hermes, skill, from_member, to, overwrite, staged, scan, allow_unscanned)


# Nama di state/shared-skills/ yang bukan skill: arsip skill memakai nama foldernya sebagai kunci.
RESERVED_SKILL_NAMES = {"registry.json", ".staging", ".team-shared.json"}


def _share_targets(team: dict, owner: dict, to: Iterable[str]) -> tuple[list[tuple[str, str]], bool]:
    """Tujuan berbagi skill → ([(nama, profile)], scope_all). "all"/"semua" = semua anggota aktif + shared."""
    targets = [str(t).strip().lower() for t in to if str(t).strip()]
    scope_all = any(t in ("all", "semua") for t in targets)
    profiles: list[tuple[str, str]] = []
    if scope_all:
        profiles = [(m["name"], m["profile"]) for m in active_members(team) if m["name"] != owner["name"]]
        profiles.append(("shared", team["shared"]["profile"]))
    else:
        for t in targets:
            if t == "shared":
                profiles.append(("shared", team["shared"]["profile"]))
                continue
            member = next((m for m in active_members(team) if m["name"] == t), None)
            if member is None:
                raise TeamError(f"anggota tujuan '{t}' tidak ada atau nonaktif")
            if member["name"] != owner["name"]:
                profiles.append((member["name"], member["profile"]))
    if not profiles:
        raise TeamError("tidak ada tujuan")
    return profiles, scope_all


def _archive_shared_skill(team: dict, skill_dir: Path, archive: Path, skill_name: str, entry: dict,
                          profiles: list[tuple[str, str]], scope_all: bool) -> None:
    """Salin skill ke arsip bersama (atomik) dan catat di registri."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{skill_name}.", dir=str(archive.parent)))
    try:
        shutil.copytree(skill_dir, tmp / skill_name, symlinks=True)
        if archive.exists():
            shutil.rmtree(archive)
        os.replace(tmp / skill_name, archive)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    registry = shared_skill_registry(team)
    previous = registry["skills"].get(skill_name, {})
    if previous.get("scope") == "all" and not scope_all:
        entry["scope"] = "all"
    registry["skills"][skill_name] = {**entry, "targets": sorted(
        set(previous.get("targets", [])) | {name for name, _ in profiles})}
    _save_registry(team, registry)


def _share_skill(team: dict, hermes: Hermes, skill: str, from_member: str, to: Iterable[str],
                 overwrite: bool = False, staged: Optional[tuple[Path, Path, list[str]]] = None,
                 scan: Optional[dict] = None, allow_unscanned: bool = False) -> dict:
    """Bagikan skill. `staged`/`scan` dari pemanggil (teamctl) memastikan yang dipasang = yang dipindai
    dan dikonfirmasi admin; tanpa itu, staging & pemindaian dilakukan di sini."""
    owner = next((m for m in team["members"] if m["name"] == from_member), None)
    if owner is None:
        raise TeamError(f"anggota '{from_member}' tidak ada")
    own_stage = staged is None
    workdir, skill_dir, skipped = staged or stage_skill(team, hermes, owner, skill)
    try:
        skill_name = skill_dir.name
        if skill_name.lower() in RESERVED_SKILL_NAMES or skill_name.startswith("."):
            raise TeamError(f"skill bernama '{skill_name}' tidak bisa dibagikan (nama itu dipakai arsip skill "
                            "bersama); ganti nama folder skill-nya dulu")
        archive = Path(team["paths"]["state_dir"]) / "shared-skills" / skill_name
        previous = shared_skill_registry(team)["skills"].get(skill_name) or {}
        other_origin = bool(previous) and previous.get("origin_profile") != owner["profile"]
        if (other_origin or (not previous and archive.exists())) and not overwrite:
            origin = previous.get("origin_member") or previous.get("origin_profile") or "asal tidak tercatat"
            raise TeamError(f"sudah ada skill bersama bernama '{skill_name}' dari {origin}; arsip tidak ditimpa "
                            "oleh skill milik orang lain. Ganti nama skill-nya, atau pakai overwrite untuk "
                            "menggantikan kiriman lama itu")
        report = scan or scan_skill(hermes, owner["profile"], skill_dir, skipped_symlinks=skipped,
                                    allow_unscanned=allow_unscanned,
                                    min_length=team["secrets"].get("min_length", 6))
        if report["problems"]:
            raise TeamError("skill tidak dibagikan: " + " | ".join(report["problems"]))
        if report.get("hash") and report["hash"] != _tree_hash(skill_dir):
            raise TeamError("salinan skill berubah setelah dipindai; ulangi")
        profiles, scope_all = _share_targets(team, owner, to)
        entry = {"origin_member": owner["name"], "origin_profile": owner["profile"], "shared_at": now_iso(),
                 "hash": _tree_hash(skill_dir), "scope": "all" if scope_all else "some"}
        if other_origin:
            # overwrite: tarik dulu kiriman lama (salinan di penerima + arsip) supaya tidak ada salinan yatim
            # dari pemilik lama yang tertinggal dengan nama yang sama.
            _unshare_skill(team, hermes, skill_name, ("all",))
        if not hermes.dry_run:
            _archive_shared_skill(team, skill_dir, archive, skill_name, entry, profiles, scope_all)
        results = {}
        for name, profile in profiles:
            try:
                results[name] = _install_skill_copy(hermes, archive if not hermes.dry_run else skill_dir,
                                                    skill_name, entry, profile, overwrite)
            except (OSError, TeamError) as exc:
                results[name] = f"gagal: {exc}"
        return {"skill": skill_name, "from": owner["name"], "scope": entry["scope"], "results": results,
                "files": len(report["files"]), "guard": report["guard"].get("verdict", "unknown")}
    finally:
        if own_stage:
            shutil.rmtree(workdir, ignore_errors=True)


def unshare_skill(team: dict, hermes: Hermes, skill: str, targets: Iterable[str] = ("all",)) -> dict:
    with team_lock(team):
        if _lock_depth() == 1:
            reload_team(team)
        return _unshare_skill(team, hermes, skill, targets)


def _unshare_skill(team: dict, hermes: Hermes, skill: str, targets: Iterable[str] = ("all",)) -> dict:
    registry = shared_skill_registry(team)
    entry = registry["skills"].get(skill)
    if entry is None:
        raise TeamError(f"skill '{skill}' tidak ada di daftar skill yang dibagikan")
    wanted = [str(t).strip().lower() for t in targets]
    everyone = any(t in ("all", "semua") for t in wanted)
    candidates = [(m["name"], m["profile"]) for m in team["members"]] + [("shared", team["shared"]["profile"])]
    results = {}
    for name, profile in candidates:
        if not everyone and name not in wanted:
            continue
        reg_path = _profile_registry_path(hermes, profile)
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if reg.get(skill, {}).get("origin_profile") != entry.get("origin_profile"):
            continue
        dest = hermes.home(profile) / "skills" / SHARED_CATEGORY / skill
        if not hermes.dry_run:
            shutil.rmtree(dest, ignore_errors=True)
            reg.pop(skill, None)
            atomic_write(reg_path, json.dumps(reg, indent=2, ensure_ascii=False) + "\n", mode=0o600)
        results[name] = "dihapus"
    if everyone and not hermes.dry_run:
        registry["skills"].pop(skill, None)
        shutil.rmtree(Path(team["paths"]["state_dir"]) / "shared-skills" / skill, ignore_errors=True)
        _save_registry(team, registry)
    return {"skill": skill, "results": results}

