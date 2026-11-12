"""Uji kecocokan kit dengan Hermes. Bagian "integrasi" memakai kode Hermes asli dan DILEWATI bila Hermes
tidak bisa di-import (CI memasangnya, lihat .github/workflows/ci.yml; di server pakai tests/selftest.sh)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hermes_team as ht  # noqa: E402


# ----------------------------------------------------------------------------- tanpa Hermes
def test_lobby_denylist_has_no_duplicates_and_covers_core_toolsets():
    assert len(ht.LOBBY_DISABLED_TOOLSETS) == len(set(ht.LOBBY_DISABLED_TOOLSETS))
    for name in ("terminal", "file", "code_execution", "discord", "discord_admin", "delegation", "memory"):
        assert name in ht.LOBBY_DISABLED_TOOLSETS


def test_aux_tasks_unique_and_without_retired_skills_hub():
    assert len(ht.AUX_TASKS) == len(set(ht.AUX_TASKS))
    assert "skills_hub" not in ht.AUX_TASKS


# ----------------------------------------------------------------------------- integrasi (Hermes asli)
def _hermes_toolsets():
    toolsets = pytest.importorskip("toolsets", reason="Hermes tidak terpasang")
    return toolsets.TOOLSETS


def test_every_upstream_individual_toolset_is_denied_in_lobby():
    """Hermes menambah toolset baru dan mengaktifkannya otomatis; lobby harus tetap hanya-teks."""
    names = [n for n in _hermes_toolsets() if not n.startswith("hermes-")]
    missing = sorted(set(names) - set(ht.LOBBY_DISABLED_TOOLSETS))
    assert not missing, ("toolset baru di Hermes belum ada di LOBBY_DISABLED_TOOLSETS: " + ", ".join(missing)
                         + " — periksa fungsinya lalu tambahkan (lobby tidak boleh punya tool).")


def test_denied_toolsets_still_exist_upstream_or_are_known_retired():
    upstream = set(_hermes_toolsets())
    ghosts = sorted(t for t in ht.LOBBY_DISABLED_TOOLSETS + ht.WORKER_DISABLED_TOOLSETS + ht.HOST_SIDE_TOOLSETS
                    if t not in upstream)
    assert not ghosts, f"toolset tidak dikenal lagi oleh Hermes (hapus dari daftar kit): {ghosts}"


def test_aux_tasks_exist_in_upstream_defaults():
    config = pytest.importorskip("hermes_cli.config_defaults", reason="Hermes tidak terpasang")
    auxiliary = config.DEFAULT_CONFIG["auxiliary"]
    missing = [t for t in ht.AUX_TASKS if t not in auxiliary]
    assert not missing, f"tugas auxiliary tidak ada di Hermes: {missing}"


def test_managed_and_host_config_keys_are_known_to_hermes():
    """Setiap key yang ditulis kit harus dikenal Hermes (tidak diam-diam diabaikan)."""
    config = pytest.importorskip("hermes_cli.config_defaults", reason="Hermes tidak terpasang")
    default = config.DEFAULT_CONFIG

    def has(path):
        node = default
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    team = _sample_team()
    unknown = []
    for section, value in ht.managed_config(team).items():
        if section == "providers":
            continue
        if isinstance(value, dict):
            unknown += [f"{section}.{k}" for k in value if not has(f"{section}.{k}")]
        elif not has(section):
            unknown.append(section)
    assert not unknown, f"key managed yang tidak ada di DEFAULT_CONFIG Hermes: {unknown}"


def _sample_team():
    import yaml
    import tempfile
    kit = Path(__file__).resolve().parent.parent
    tmp = Path(tempfile.mkdtemp())
    (tmp / "members.yaml").write_text("members: []\n")
    return ht.load_team(kit / "team.example.yaml", tmp / "members.yaml")


def test_user_commands_are_known_slash_commands():
    commands = pytest.importorskip("hermes_cli.commands", reason="Hermes tidak terpasang")
    native_discord = {"thread"}   # didaftarkan adapter Discord sendiri (plugins/platforms/discord), bukan COMMAND_REGISTRY
    unknown = sorted(c for c in ht.DEFAULT_USER_COMMANDS
                     if c not in native_discord and commands.resolve_command(c) is None)
    assert not unknown, f"perintah user yang tidak dikenal Hermes (hapus atau ganti namanya): {unknown}"
