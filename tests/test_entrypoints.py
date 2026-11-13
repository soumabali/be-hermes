"""Titik masuk kit harus jalan dari salinan di folder lain (wizard menyalin repo ke /opt/hermes-team), dari folder
kerja mana pun, tanpa PYTHONPATH, juga dengan `python -I` / PYTHONSAFEPATH=1 (launcher tertentu memakainya).
Kegagalan di sini = `ModuleNotFoundError: kit_paths` di server nyata padahal semua uji lain hijau."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent
IGNORE = shutil.ignore_patterns(".git", "__pycache__", "arsip-versi-lama", ".pytest_cache", ".github", "tests")
SCRIPTS = ["hermes_team.py", "kit_doctor.py", "usage_ledger.py", "usage_report.py", "kit_setup.py",
           "tools/usage_post.py", "tools/probe_llm.py", "tools/metrics_export.py", "tools/sandbox_idle.py"]


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    dest = tmp_path_factory.mktemp("opt") / "hermes-team"
    shutil.copytree(KIT, dest, ignore=IGNORE)
    return dest


def run(installed, cwd, *args, stdin=None, isolated=True):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), "LC_ALL": "C.UTF-8"}
    if isolated:
        env["PYTHONSAFEPATH"] = "1"
    cmd = [sys.executable] + (["-I"] if isolated else []) + list(args)
    return subprocess.run(cmd, env=env, cwd=cwd, input=stdin, text=True, capture_output=True, timeout=60)


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("isolated", [True, False], ids=["isolated", "normal"])
def test_script_help_runs_from_a_copy(installed, tmp_path, script, isolated):
    proc = run(installed, tmp_path, str(installed / script), "--help", isolated=isolated)
    assert proc.returncode == 0, proc.stderr[-600:]
    assert "Traceback" not in proc.stderr and "ModuleNotFoundError" not in proc.stderr


def test_version_matches_version_file(installed, tmp_path):
    proc = run(installed, tmp_path, str(installed / "hermes_team.py"), "--version")
    assert proc.returncode == 0 and (installed / "VERSION").read_text().strip() in proc.stdout


def test_mcp_server_initializes_from_a_copy(installed, tmp_path):
    msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
    proc = run(installed, tmp_path, str(installed / "teamctl_mcp.py"), "--team", str(installed / "team.example.yaml"),
               "--members", str(installed / "members.example.yaml"), stdin=msg + "\n")
    reply = json.loads(proc.stdout.splitlines()[0])
    assert reply["result"]["serverInfo"]["name"] == "teamctl"
    assert reply["result"]["serverInfo"]["version"] == (installed / "VERSION").read_text().strip()


def test_kit_works_when_copy_is_read_only_and_symlinked(installed, tmp_path):
    link = tmp_path / "hermes_team_link.py"
    link.symlink_to(installed / "hermes_team.py")
    os.chmod(installed, 0o555)
    try:
        proc = run(installed, tmp_path, str(link), "--version")
        assert proc.returncode == 0, proc.stderr[-400:]
    finally:
        os.chmod(installed, 0o755)


def test_placeholder_example_is_refused_from_a_copy(installed, tmp_path):
    proc = run(installed, tmp_path, str(installed / "hermes_team.py"), "check", "--config",
               str(installed / "team.example.yaml"))
    assert proc.returncode != 0 and "ID asli" in (proc.stdout + proc.stderr)
