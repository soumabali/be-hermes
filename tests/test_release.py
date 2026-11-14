"""Satu sumber versi: VERSION, plugin.yaml, CHANGELOG, pyproject, dan keluaran `--version` harus sama."""
import re
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
VERSION = (KIT / "VERSION").read_text(encoding="utf-8").strip()


def test_version_file_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION), VERSION


def test_kit_base_reads_the_version_file():
    sys.path.insert(0, str(KIT))
    import kit_base
    assert kit_base.KIT_VERSION == VERSION and kit_base._FALLBACK_VERSION == VERSION


def test_plugin_manifest_follows_the_kit_version():
    manifest = (KIT / "plugins" / "team-guard" / "plugin.yaml").read_text(encoding="utf-8")
    assert re.search(rf'^version:\s*"?{re.escape(VERSION)}"?\s*$', manifest, re.M), "plugin.yaml tertinggal dari VERSION"


def test_changelog_has_an_entry_for_the_current_version():
    text = (KIT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(VERSION)}\]", text, re.M), f"CHANGELOG belum punya bagian [{VERSION}]"


def test_pyproject_takes_version_from_the_file():
    text = (KIT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text and 'file = ["VERSION"]' in text and "version =" not in text.split("[tool.setuptools.dynamic]")[0]


def test_cli_prints_the_version():
    out = subprocess.run([sys.executable, str(KIT / "hermes_team.py"), "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and VERSION in out.stdout


def test_readme_and_docs_do_not_reference_missing_files():
    docs = [KIT / "README.md", *sorted((KIT / "docs").glob("*.md"))]
    missing = []
    for doc in docs:
        text = re.sub(r"```.*?```", "", doc.read_text(encoding="utf-8"), flags=re.S)
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)\s]*)?\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (doc.parent / target).resolve().exists():
                missing.append(f"{doc.name} → {target}")
    assert not missing, missing


def test_configuration_reference_is_generated_from_the_schema():
    out = subprocess.run([sys.executable, str(KIT / "hermes_team.py"), "config-doc"], capture_output=True, text=True)
    assert out.returncode == 0
    doc = (KIT / "docs" / "KONFIGURASI.md").read_text(encoding="utf-8")
    regenerate = "python3 hermes_team.py config-doc --write docs/KONFIGURASI.md"
    assert out.stdout.strip() in doc, f"docs/KONFIGURASI.md usang. Perbarui dengan satu perintah:\n  {regenerate}"


def test_config_doc_write_replaces_only_the_table(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# Judul\n\nPengantar.\n\n| key | tipe | default | plugin | deskripsi |\n|---|---|---|---|---|\n"
                   "| `lama` | int | `1` |  | usang |\n\nCatatan akhir.\n", encoding="utf-8")
    out = subprocess.run([sys.executable, str(KIT / "hermes_team.py"), "config-doc", "--write", str(doc)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    text = doc.read_text(encoding="utf-8")
    table = subprocess.run([sys.executable, str(KIT / "hermes_team.py"), "config-doc"],
                           capture_output=True, text=True).stdout.strip()
    assert text.startswith("# Judul\n\nPengantar.\n\n") and table in text and "`lama`" not in text
    assert text.endswith("\n\nCatatan akhir.\n")
    again = subprocess.run([sys.executable, str(KIT / "hermes_team.py"), "config-doc", "--write", str(doc)],
                           capture_output=True, text=True)
    assert again.returncode == 0 and doc.read_text(encoding="utf-8") == text         # idempoten
    doc.write_text("tanpa tabel\n", encoding="utf-8")
    bad = subprocess.run([sys.executable, str(KIT / "hermes_team.py"), "config-doc", "--write", str(doc)],
                         capture_output=True, text=True)
    assert bad.returncode != 0 and "tabel konfigurasi tidak ditemukan" in bad.stderr
