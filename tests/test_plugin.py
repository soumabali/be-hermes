"""Uji plugin team-guard (tanpa Hermes): penyamaran, guard perintah, secret, skala, gerbang akses,
live reload, dan pendaftaran per jenis profile.

Jalankan:  python -m pytest tests/test_plugin.py -q
Kelompok `TestIntegration*` butuh Hermes terpasang (`pytest.importorskip("gateway")`) → dilewati di sini,
dijalankan di CI/selftest pada server yang punya Hermes.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import stat
import subprocess
import sys
import threading
import types
import urllib.parse
from pathlib import Path

import pytest

import plugin_stubs as S

P = S.load_plugin()
redact = S.sub(P, "redact")
guard = S.sub(P, "guard")
secrets = S.sub(P, "secrets")
ratelimit = S.sub(P, "ratelimit")
dispatch = S.sub(P, "dispatch")
livereload = S.sub(P, "livereload")
rostermod = S.sub(P, "roster")
statemod = S.sub(P, "state")
shared = S.sub(P, "shared")
commands = S.sub(P, "commands")
runtime = S.sub(P, "runtime")

SECRET = "s3cr3t-Value_42xyz"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_shared():
    shared.reset()
    yield
    shared.reset()


@pytest.fixture
def env(tmp_path):
    return S.Env(tmp_path, P)


# ============================================================================= pemuatan & manifest
class TestLoading:
    def test_loaded_like_upstream_directory_plugin(self):
        assert P.__name__ == "hermes_plugins.team_guard"
        assert callable(P.register)
        assert redact.__name__ == "hermes_plugins.team_guard.redact"      # impor relatif di namespace paket

    def test_version_matches_manifest(self):
        import yaml
        manifest = yaml.safe_load((S.PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
        assert manifest["version"] == "3.1.0" == P.__version__
        assert manifest["name"] == "team-guard"
        assert manifest["manifest_version"] == 2

    def test_manifest_uses_only_known_fields_and_valid_hooks(self):
        import yaml
        manifest = yaml.safe_load((S.PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
        known = {"name", "version", "description", "author", "requires_env", "provides_tools", "provides_hooks",
                 "kind", "hooks", "label", "optional_env", "platforms", "external_dependencies",
                 "pip_dependencies", "manifest_version", "api_version", "requires_plugins",
                 "python_dependencies", "config_schema", "license", "homepage", "tags", "capabilities",
                 "requires_hermes", "config_keys"}
        assert set(manifest) <= known
        assert set(manifest["provides_hooks"]) <= S.VALID_HOOKS
        assert {spec["type"] for spec in manifest["config_schema"].values()} <= {"str", "bool"}

    def test_pure_modules_import_without_hermes_or_discord(self, tmp_path):
        """Logika murni harus bisa diimpor walau Hermes dan discord.py tidak terpasang."""
        code = f"""
import sys, importlib.abc
BLOCK = ("discord", "gateway", "hermes_cli", "hermes_constants", "agent", "tools", "yaml")
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCK:
            raise ImportError("diblokir untuk uji: " + name)
sys.meta_path.insert(0, Block())
sys.path.insert(0, {str(S.REPO / "tests")!r})
sys.dont_write_bytecode = True
import plugin_stubs as S
P = S.load_plugin()
for name in ("redact", "guard", "secrets", "ratelimit", "dispatch", "livereload", "roster", "state",
             "onboarding", "hooks", "commands", "discord_events", "runtime", "shared"):
    S.sub(P, name)
ctx = S.StubContext({{"state_dir": {str(tmp_path)!r}}})
P.register(ctx)
assert "pre_gateway_dispatch" in ctx.hooks and "discord" in ctx.platform_handlers
assert S.sub(P, "guard").check_command("printenv")
print("OK")
"""
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "HERMES_HOME": str(tmp_path / "h")})
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().endswith("OK")

    def test_plugin_never_imports_hermes_team(self):
        for path in S.PLUGIN_DIR.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "import hermes_team" not in text and "from hermes_team" not in text, path.name


# ============================================================================= penyamaran
VARIANTS = {
    "asli": SECRET,
    "base64": base64.b64encode(SECRET.encode()).decode(),
    "base64-tanpa-padding": base64.b64encode(SECRET.encode()).decode().rstrip("="),
    "base64-urlsafe": base64.urlsafe_b64encode(b"k\xff\xfe-rahasia-??>>").decode().rstrip("="),
    "urlencode": urllib.parse.quote("p@ss w/rd&x=1", safe=""),
    "quote_plus": urllib.parse.quote_plus("p@ss w/rd&x=1"),
    "hex": SECRET.encode().hex(),
    "HEX": SECRET.encode().hex().upper(),
    "dibalik": SECRET[::-1],
    "huruf-besar": SECRET.upper(),
    "huruf-kecil": SECRET.lower(),
}


class TestRedaction:
    @pytest.mark.parametrize("label", ["asli", "base64", "base64-tanpa-padding", "hex", "HEX", "dibalik",
                                       "huruf-besar", "huruf-kecil"])
    def test_variant_is_replaced_by_marker(self, label):
        r = redact.Redactor({"GH_PAT": SECRET})
        out = r.redact(f"awal {VARIANTS[label]} akhir")
        assert out == "awal «secret:GH_PAT» akhir"

    @pytest.mark.parametrize("label", ["urlencode", "quote_plus"])
    def test_urlencoded_variant(self, label):
        r = redact.Redactor({"DB_PASSWORD": "p@ss w/rd&x=1"})
        assert r.redact(f"postgres://u:{VARIANTS[label]}@db/x") == "postgres://u:«secret:DB_PASSWORD»@db/x"

    def test_urlsafe_base64_variant(self):
        value = b"k\xff\xfe-rahasia-??>>".decode("latin-1")
        r = redact.Redactor({"BIN_KEY": value})
        encoded = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
        assert "«secret:BIN_KEY»" in r.redact(f"x={encoded}")

    def test_variants_match_kit_definition(self):
        """Sama dengan secret_variants() di hermes_team.py (pemindai skill bersama)."""
        value = "Tok3n/+rahasia=="
        raw = value.encode()
        b64 = base64.b64encode(raw).decode()
        kit = {value, b64, b64.rstrip("="), base64.urlsafe_b64encode(raw).decode().rstrip("="),
               urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value), raw.hex(), raw.hex().upper(),
               value[::-1], value.upper(), value.lower()}
        assert {v for v in kit if len(v) >= 6} <= redact.secret_variants(value)

    def test_multiple_secrets_and_names_never_leak_values(self):
        r = redact.Redactor({"A_TOKEN": "alpha-token-111", "B_TOKEN": "beta-token-2222"})
        out = r.redact("x alpha-token-111 y beta-token-2222 z")
        assert out == "x «secret:A_TOKEN» y «secret:B_TOKEN» z"
        assert r.names == ["A_TOKEN", "B_TOKEN"]

    def test_longest_variant_wins_over_prefix(self):
        r = redact.Redactor({"SHORT": "abcdef", "LONG": "abcdefghij"})
        assert r.redact("abcdefghij") == "«secret:LONG»"

    def test_short_values_are_ignored(self):
        r = redact.Redactor({"PIN": "1234", "OK_KEY": "123456"}, min_length=6)
        assert r.redact("pin 1234 key 123456") == "pin 1234 key «secret:OK_KEY»"

    def test_unchanged_text_and_empty_redactor(self):
        assert redact.Redactor({}).redact("apa saja") == "apa saja"
        assert not redact.Redactor({})
        r = redact.Redactor({"K": SECRET})
        assert r.redact("bersih") == "bersih" and not r.contains("bersih") and r.contains(SECRET)

    def test_redact_obj_nested_copy_without_mutating_original(self):
        r = redact.Redactor({"GH_PAT": SECRET})
        payload = {"messages": [{"role": "tool", "content": f"token={SECRET}"},
                                {"role": "user", "content": [{"type": "text", "text": "halo"}]}],
                   "meta": ("a", SECRET), "n": 3}
        new, changed = r.redact_obj(payload)
        assert changed and new["messages"][0]["content"] == "token=«secret:GH_PAT»"
        assert new["meta"] == ("a", "«secret:GH_PAT»") and new["n"] == 3
        assert payload["messages"][0]["content"] == f"token={SECRET}"       # asli tidak diubah
        same, changed = r.redact_obj({"a": ["bersih"]})
        assert not changed and same == {"a": ["bersih"]}

    @pytest.mark.parametrize("label,token", [
        ("github-token", "ghp_" + "A1b2C3d4" * 5),
        ("github-token", "github_pat_" + "11ABCDEFG0" * 3),
        ("gitlab-token", "glpat-" + "xY9_" * 6),
        ("api-key", "sk-" + "a1B2c3D4e5" * 3),
        ("api-key", "sk-proj-" + "Zz9" * 12),
        ("api-key", "sk-ant-api03-" + "Qw3" * 12),
        ("aws-key", "AKIA" + "ABCDEFGHIJKLMNOP"),
        ("slack-token", "xoxb-1234567890-abcdefghij"),
        ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
        ("google-key", "AIza" + "B" * 35),
    ])
    def test_vendor_token_scrubbed_from_chat(self, label, token):
        text, kinds = redact.scrub_tokens(f"pakai token {token} ya")
        assert token not in text and kinds == [label]
        assert text == f"pakai token «disamarkan:{label}» ya"

    def test_private_key_block_scrubbed(self):
        pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
        text, kinds = redact.scrub_tokens(f"ini key-ku:\n{pem}\nmakasih")
        assert "b3BlbnNzaC1rZXk" not in text and kinds == ["private-key"] and text.endswith("makasih")

    @pytest.mark.parametrize("text", [
        "tolong perbaiki skill deploy", "sk-pendek", "commit ghp_abc belum lengkap", "AKIA terlalu pendek",
        "lihat task-123 dan risk-assessment-2026-final-v2", "eyJ bukan jwt",
    ])
    def test_ordinary_text_is_not_scrubbed(self, text):
        assert redact.scrub_tokens(text) == (text, [])

    def test_scrub_user_messages_only_touches_user_role(self):
        token = "ghp_" + "A1b2C3d4" * 5
        msgs = [{"role": "system", "content": f"contoh {token}"},
                {"role": "user", "content": f"ini {token}"},
                {"role": "user", "content": [{"type": "text", "text": f"dan {token}"}, {"type": "image"}]},
                {"role": "assistant", "content": "ok"}]
        out, changed = redact.scrub_user_messages(msgs)
        assert changed and token in out[0]["content"] and token not in out[1]["content"]
        assert token not in out[2]["content"][0]["text"] and out[2]["content"][1] == {"type": "image"}
        assert token in msgs[1]["content"]                                   # asli tidak diubah

    def test_upstream_patterns_fit_register_redaction_patterns_contract(self):
        ctx = S.StubContext()
        assert ctx.register_redaction_patterns(redact.UPSTREAM_PATTERNS) == len(redact.UPSTREAM_PATTERNS)


# ============================================================================= guard perintah
NAMES = {"GH_PAT", "DB_URL"}

BLOCKED = [
    "env", "printenv", "set", "export -p", "export", "declare -x", "declare -p", "typeset -x",
    "/usr/bin/env", "\\env", "command env", "env | grep GH", "env > /tmp/x", "sudo env", "  env  ", "env\n",
    "cat /proc/self/environ", "strings /proc/1/environ", "xxd /proc/$$/environ",
    "echo $GH_PAT", "echo ${GH_PAT}", 'echo "$GH_PAT"', "printf '%s' \"$GH_PAT\"", "echo ${GH_PAT:0:4}",
    "echo $MY_API_TOKEN", "cat <<< $GH_PAT", "printenv GH_PAT",
    # usaha mengakali
    "echo $GH_PAT | base64", "echo $GH_PAT | rev", "bash -c 'env'", 'sh -c "printenv | base64"', "bash -lc env",
    "ls; env", "ls && printenv", "true || set", "echo $(env)", "echo `printenv`", "x=$(env | base64)",
    "e''nv", "e\\nv", "FOO=1 env", "env -0", "env -u FOO", "timeout 5 env", "nohup printenv &", "eval env",
    "eval 'printenv'", "xargs env", "(env)", "{ env; }", 'echo "token: $(printenv GH_PAT)"',
    'echo "a $(echo $GH_PAT) b"', "ps eww", "ps auxe",
    # bahasa lain
    "python -c 'import os; print(os.environ)'", 'python3 -c "import os;print(dict(os.environ))"',
    "node -e 'console.log(process.env)'", 'node -e "console.log(JSON.stringify(process.env))"',
    "ruby -e 'puts ENV.to_h'", "perl -e 'print %ENV'", "php -r 'print_r($_ENV);'",
    "python -c 'import os\nfor k,v in os.environ.items(): print(k,v)'",
    "awk 'BEGIN{for(k in ENVIRON)print k,ENVIRON[k]}'", "python -c 'import os; print(os.environ)' | base64",
]

ALLOWED = [
    "ls -la", "env FOO=1 make test", "env -i PATH=/bin ls", "printenv PATH", "set -e", "set -euo pipefail",
    "export FOO=bar", "export PATH=$PATH:/x", "declare -x FOO=1", "declare -a arr", "export -f myfunc",
    'curl -H "Authorization: Bearer $GH_PAT" https://api.github.com/user',
    "git clone https://x:$GH_PAT@github.com/a/b", "GH_TOKEN=$GH_PAT gh pr list",
    "echo $GH_PAT | docker login -u x --password-stdin", "echo $GH_PAT | gh auth login --with-token",
    "echo $GH_PAT > ~/.token", "echo hello", "echo $HOME", "echo $PWD", "echo '$GH_PAT'", 'echo "set"',
    "python -c 'import os; print(os.environ.get(\"HOME\"))'", "python -c 'import os; print(os.environ[\"PATH\"])'",
    "node -e 'console.log(process.env.HOME)'", "cat environment.md", "grep -r environ src/", "ps -ef", "ps aux",
    "python manage.py runserver", "git commit -m 'update env handling'", "printf '%s\\n' done",
    "docker run --env-file .env img", "npm run env:check", "cat /proc/cpuinfo", "envsubst < a > b",
    "python -c \"import subprocess,os; subprocess.run(['ls'], env=dict(os.environ, A='1'))\"",
    "settings=1; echo $settings", "make test 2>&1 | tee build.log", "psql \"$DB_URL\" -c 'select 1'",
]

CODE_BLOCKED = [
    "import os\nprint(os.environ)", "import os\nfor k in os.environ: print(k)",
    "import os, json\nprint(json.dumps(dict(os.environ)))", "import subprocess\nsubprocess.run('env', shell=True)",
    "import os\nos.system('printenv')", "open('/proc/self/environ').read()",
    "from os import environ\nprint(environ)", "terminal('env | base64')", "import os\nprint(os.environ.items())",
    "import os\nprint({k:v for k,v in os.environ.items()})", "import os\nprint(str(os.environ))",
    "import pprint,os\npprint.pprint(dict(os.environ))",
]

CODE_ALLOWED = [
    "import os\nprint(os.environ.get('HOME'))",
    "import os\ntoken=os.environ['GH_PAT']\nrequests.get(u, headers={'Authorization': token})",
    "print('hello env')", "import subprocess\nsubprocess.run(['ls','-la'])", "import os\nos.environ['X']='1'",
    "import os\nenv = dict(os.environ, FOO='1')\nsubprocess.run(cmd, env=env)",
]


class TestCommandGuard:
    @pytest.mark.parametrize("command", BLOCKED)
    def test_env_dump_is_blocked(self, command):
        assert guard.check_command(command, NAMES), command

    @pytest.mark.parametrize("command", ALLOWED)
    def test_ordinary_command_is_allowed(self, command):
        assert guard.check_command(command, NAMES) is None, command

    @pytest.mark.parametrize("code", CODE_BLOCKED)
    def test_code_dump_is_blocked(self, code):
        assert guard.check_code(code, NAMES), code

    @pytest.mark.parametrize("code", CODE_ALLOWED)
    def test_ordinary_code_is_allowed(self, code):
        assert guard.check_code(code, NAMES) is None, code

    def test_message_explains_and_points_to_env_list(self):
        msg = guard.check_command("printenv")
        assert "team-guard" in msg and "printenv" in msg and "/env-list" in msg

    def test_secret_reference_needs_known_or_secretish_name(self):
        assert guard.check_command("echo $APP_NAME", NAMES) is None
        assert guard.check_command("echo $DB_URL", NAMES)                  # nama tersimpan
        assert guard.check_command("echo $DB_URL", set()) is None           # tak dikenal & bukan bentuk secret

    @pytest.mark.parametrize("tool,args,blocked", [
        ("terminal", {"command": "printenv"}, True),
        ("terminal", {"command": "ls -la", "timeout": 5}, False),
        ("execute_code", {"code": "import os\nprint(os.environ)"}, True),
        ("execute_code", {"code": "print(1+1)"}, False),
        ("read_file", {"path": "/proc/self/environ"}, True),
        ("read_file", {"path": "/workspace/README.md"}, False),
        ("terminal", {"command": "echo $GH_PAT", "background": True}, True),
        ("web_search", {"query": "how to print env in bash"}, False),
        ("terminal", None, False),
    ])
    def test_check_tool_call(self, tool, args, blocked):
        assert bool(guard.check_tool_call(tool, args, NAMES)) is blocked

    def test_split_pipelines_structure(self):
        assert guard.split_pipelines("a | b; c && d") == [["a", "b"], ["c"], ["d"]]
        assert guard.split_pipelines("echo 'x; y | z'") == [["echo 'x; y | z'"]]
        assert guard.split_pipelines("make 2>&1 | tee log") == [["make 2>&1", "tee log"]]

    def test_huge_or_non_string_input_is_safe(self):
        assert guard.check_command(None) is None and guard.check_command(123) is None
        assert guard.check_command("x" * 500_000) is None
        assert guard.check_command("echo 'tak tertutup") is None


# ============================================================================= nama & nilai secret
class TestEnvNamePolicy:
    @pytest.mark.parametrize("name", ["GH_PAT", "DB_PASSWORD", "MY_OPENAI_KEY", "DEPLOY_KEY_B64", "STRIPE_SECRET",
                                      "AB", "X9_TOKEN"])
    def test_ordinary_names_allowed(self, name):
        assert secrets.check_env_name(name, reserved=["TD_API_KEY"]) == (True, "")

    @pytest.mark.parametrize("name,fragment", [
        ("GITHUB_TOKEN", "GH_PAT"), ("GH_TOKEN", "GH_PAT"), ("OPENAI_API_KEY", "MY_OPENAI_KEY"),
        ("DISCORD_BOT_TOKEN", "Hermes"), ("DISCORD_ALLOWED_ROLES", "Hermes"), ("HERMES_HOME", "Hermes"),
        ("TERMINAL_ENV", "Hermes"), ("GATEWAY_ALLOW_ALL_USERS", "Hermes"), ("AUXILIARY_VISION_API_KEY", "Hermes"),
        ("PATH", "sistem"), ("LD_PRELOAD", "sistem"), ("PYTHONPATH", "sistem"), ("HOME", "sistem"),
        ("BASH_ENV", "sistem"), ("TD_API_KEY", "API key LLM tim"),
    ])
    def test_hermes_and_system_names_rejected_with_suggestion(self, name, fragment):
        ok, why = secrets.check_env_name(name, reserved=["TD_API_KEY"])
        assert not ok and fragment in why and "nama lain" in why

    @pytest.mark.parametrize("name", ["gh_pat", "1TOKEN", "A", "X" * 65, "GH-PAT", "GH PAT", "", None, "_PRIVATE"])
    def test_malformed_names_rejected(self, name):
        ok, why = secrets.check_env_name(name)
        assert not ok and "HURUF BESAR" in why

    def test_allow_names_opens_hermes_name_but_never_reserved(self):
        assert secrets.check_env_name("GITHUB_TOKEN", allow=["GITHUB_TOKEN"])[0]
        assert not secrets.check_env_name("TD_API_KEY", reserved=["TD_API_KEY"], allow=["TD_API_KEY"])[0]

    def test_deny_names_wins_over_allow(self):
        ok, why = secrets.check_env_name("PROD_DB_PASSWORD", deny=["PROD_DB_PASSWORD"], allow=["PROD_DB_PASSWORD"])
        assert not ok and "deny_names" in why

    def test_upstream_blocklist_is_consulted_and_fail_safe(self):
        assert not secrets.check_env_name("VENDOR_X_KEY", upstream=lambda n: n == "VENDOR_X_KEY")[0]
        assert secrets.check_env_name("VENDOR_Y_KEY", upstream=lambda n: 1 / 0)[0]

    @pytest.mark.parametrize("value,ok", [
        ("123456", True), ("12345", False), ("a\nb-multi-line", False), ("x" * 8193, False), (None, False),
        ("nilai dengan spasi & simbol \"'$#\\", True),
    ])
    def test_value_checks(self, value, ok):
        got, why = secrets.check_env_value(value, 6)
        assert got is ok and (ok or why)


# ============================================================================= .env & SecretStore
class TestEnvFile:
    def test_set_creates_private_env_and_index(self, tmp_path):
        store = secrets.SecretStore(tmp_path / "p", extra_env=())
        assert store.set("GH_PAT", SECRET, by="500") is True
        assert stat.S_IMODE(store.env_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.index_path.stat().st_mode) == 0o600
        assert store.read_env() == {"GH_PAT": SECRET} and store.names() == ["GH_PAT"]
        index = json.loads(store.index_path.read_text())
        assert index["keys"]["GH_PAT"]["by"] == "500" and SECRET not in store.index_path.read_text()

    def test_update_replaces_without_duplicating_and_keeps_other_lines(self, tmp_path):
        home = tmp_path / "p"
        home.mkdir()
        (home / ".env").write_text("# komentar\nFOO=bar\nexport GH_PAT=lama\nBAZ='q u x'\n")
        store = secrets.SecretStore(home, extra_env=())
        assert store.set("GH_PAT", "nilai-baru-123") is False
        text = store.env_path.read_text()
        assert text.count("GH_PAT=") == 1 and "# komentar" in text and "FOO=bar" in text
        assert store.read_env() == {"FOO": "bar", "GH_PAT": "nilai-baru-123", "BAZ": "q u x"}

    @pytest.mark.parametrize("value", ['p@ss "quoted" \\ back', "with # hash and spaces", "$HOME/${X} `cmd`",
                                       "'single' quotes", "tab\there", "=equals=at=start"])
    def test_value_roundtrip_through_env_tokenizer(self, tmp_path, value):
        store = secrets.SecretStore(tmp_path, extra_env=())
        store.set("DB_PASSWORD", value)
        assert store.read_env()["DB_PASSWORD"] == value

    def test_unset_removes_value_and_index(self, tmp_path):
        store = secrets.SecretStore(tmp_path, extra_env=())
        store.set("GH_PAT", SECRET)
        store.set("OTHER_KEY", "other-value-1")
        assert store.unset("GH_PAT") is True and store.unset("GH_PAT") is False
        assert store.read_env() == {"OTHER_KEY": "other-value-1"} and store.names() == ["OTHER_KEY"]

    def test_unset_refuses_names_not_set_by_user(self, tmp_path):
        (tmp_path / ".env").write_text("DISCORD_BOT_TOKEN=abc.def.ghi-jkl\n")
        store = secrets.SecretStore(tmp_path, extra_env=())
        assert store.unset("DISCORD_BOT_TOKEN") is False
        assert "DISCORD_BOT_TOKEN" in store.read_env() and store.names() == []

    def test_values_cover_user_secrets_secretish_names_and_managed_key(self, tmp_path):
        managed = tmp_path / "etc.env"
        managed.write_text("TD_API_KEY=llm-key-rahasia-1\nNOT_SECRETISH=zzzzzzzz\n")
        (tmp_path / ".env").write_text("DISCORD_BOT_TOKEN=bot-token-xyz-9\nLOG_LEVEL=verbose-mode\nPIN_CODE=12\n")
        store = secrets.SecretStore(tmp_path, extra_env=(managed,))
        store.set("PLAIN_NAME", "user-value-777")
        values = store.values(reserved=["TD_API_KEY"], min_length=6)
        assert values == {"DISCORD_BOT_TOKEN": "bot-token-xyz-9", "PLAIN_NAME": "user-value-777",
                          "TD_API_KEY": "llm-key-rahasia-1"}

    def test_redactor_cache_follows_file_changes(self, tmp_path):
        store = secrets.SecretStore(tmp_path, extra_env=())
        seen = []
        store.on_change = lambda values: seen.append(sorted(values))
        first = store.redactor()
        assert store.redactor() is first and not first
        store.set("GH_PAT", SECRET)
        assert store.redactor().redact(SECRET) == "«secret:GH_PAT»"
        store.unset("GH_PAT")
        assert store.redactor().redact(SECRET) == SECRET
        assert seen == [[], ["GH_PAT"], []]

    def test_concurrent_writers_do_not_lose_keys(self, tmp_path):
        stores = [secrets.SecretStore(tmp_path, extra_env=()) for _ in range(6)]
        threads = [threading.Thread(target=lambda i=i, s=s: [s.set(f"KEY_{i}_{n}", f"value-{i}-{n}-x")
                                                               for n in range(5)])
                   for i, s in enumerate(stores)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(stores[0].read_env()) == 30 and len(stores[0].names()) == 30

    def test_missing_files_are_empty_not_errors(self, tmp_path):
        store = secrets.SecretStore(tmp_path / "belum-ada", extra_env=(tmp_path / "tidak-ada",))
        assert store.read_env() == {} and store.names() == [] and store.values() == {}

    def test_passthrough_plan(self):
        assert secrets.passthrough_plan(["A"], ["A"]) is None
        assert secrets.passthrough_plan(["OPS_VAR"], ["GH_PAT", "A_KEY"]) == ["OPS_VAR", "A_KEY", "GH_PAT"]
        assert secrets.passthrough_plan(["OPS_VAR", "GH_PAT"], [], removed=["GH_PAT"]) == ["OPS_VAR"]
        assert secrets.passthrough_plan(None, ["GH_PAT"]) == ["GH_PAT"]

    def test_passthrough_argv_matches_kit_cli_usage(self):
        assert secrets.passthrough_argv("/usr/bin/hermes", "be-budi", ["GH_PAT"]) == [
            "/usr/bin/hermes", "-p", "be-budi", "config", "set", "terminal.env_passthrough", '["GH_PAT"]']

    def test_sync_passthrough_runs_hermes_config_set_for_profile(self, env):
        roster = env.rt().roster()
        home = env.home("be-budi")
        (home / "config.yaml").write_text("terminal:\n  env_passthrough: [OPS_VAR]\n")
        store = secrets.SecretStore(home, extra_env=())
        store.set("GH_PAT", SECRET)
        calls = []

        async def runner(argv, environ):
            calls.append((argv, environ["HERMES_HOME"]))
            return 0, ""

        assert run(secrets.sync_passthrough(roster, "be-budi", store, runner=runner)) == (True, "diperbarui")
        assert calls == [(["hermes", "-p", "be-budi", "config", "set", "terminal.env_passthrough",
                           '["OPS_VAR", "GH_PAT"]'], str(env.hermes_root))]


# ============================================================================= /env-set, /env-list, /env-unset
class TestEnvCommands:
    def _roster(self, env):
        return env.rt().roster()

    @pytest.mark.parametrize("uid,channels,profile,error_fragment", [
        (S.U_BUDI, [S.CH_BUDI], "be-budi", ""),
        (S.U_BUDI, ["777", None, S.CH_BUDI], "be-budi", ""),                 # thread di channel pribadinya
        (S.U_BUDI, [S.CH_BE1], None, "anggota lain"),
        (S.U_BUDI, [S.CH_SHARED], None, "hanya admin"),
        (S.U_BUDI, [S.CH_ADMIN], None, "hanya admin"),
        (S.U_BUDI, ["123123123"], None, "channel pribadimu"),
        (S.ADMIN, [S.CH_SHARED], "team-shared", ""),
        (S.ADMIN, [S.CH_ADMIN], "team-admin", ""),
        (S.ADMIN, [S.CH_BE1], None, "anggota lain"),                         # admin pun tidak ke channel orang
        (S.U_CITRA, [S.CH_BUDI], None, "anggota lain"),
    ])
    def test_resolve_env_target(self, env, uid, channels, profile, error_fragment):
        got, error = commands.resolve_env_target(self._roster(env), uid, channels)
        assert got == profile and error_fragment in error

    def test_resolve_without_roster(self):
        assert commands.resolve_env_target(rostermod.Roster(), S.U_BUDI, [S.CH_BUDI])[0] is None

    def test_env_set_writes_profile_env_and_never_echoes_value(self, env):
        calls = []

        async def runner(argv, environ):
            calls.append(argv)
            return 0, ""

        ok, text = run(commands.do_env_set(self._roster(env), "be-budi", "gh_pat ", f" {SECRET} ", S.U_BUDI,
                                           runner=runner))
        assert ok and "`GH_PAT`" in text and SECRET not in text and "$GH_PAT" in text
        store = secrets.SecretStore(env.home("be-budi"), extra_env=())
        assert store.read_env()["GH_PAT"] == SECRET
        assert not (env.home("be-be1") / ".env").exists() and not (env.hermes_root / ".env").exists()
        assert calls and calls[0][-1] == '["GH_PAT"]' and calls[0][1:3] == ["-p", "be-budi"]

    @pytest.mark.parametrize("name,value,fragment", [
        ("GITHUB_TOKEN", SECRET, "GH_PAT"), ("TD_API_KEY", SECRET, "API key LLM"), ("GH_PAT", "abc", "terlalu pendek"),
        ("bad name", SECRET, "HURUF BESAR"), ("MULTI_LINE", "a\nbbbbbbb", "_B64"),
    ])
    def test_env_set_rejections_store_nothing(self, env, name, value, fragment):
        ok, text = run(commands.do_env_set(self._roster(env), "be-budi", name, value, S.U_BUDI))
        assert not ok and fragment in text and SECRET not in text
        assert not (env.home("be-budi") / ".env").exists()

    def test_env_set_respects_roster_policy(self, env):
        env.set("settings", max_keys=2, deny_names=["LEGACY_KEY"], allow_names=["GITHUB_TOKEN"], min_length=10)

        async def runner(argv, environ):
            return 0, ""

        roster = env.rt().roster(force=True)
        do = lambda n, v: run(commands.do_env_set(roster, "be-budi", n, v, S.U_BUDI, runner=runner))
        assert not do("LEGACY_KEY", SECRET)[0]
        assert not do("SHORT_ONE", "123456789")[0]                           # min_length 10
        assert do("GITHUB_TOKEN", SECRET)[0] and do("SECOND_KEY", SECRET + "2")[0]
        ok, text = do("THIRD_KEY", SECRET + "3")
        assert not ok and "Batas 2" in text
        assert do("SECOND_KEY", SECRET + "x")[0]                             # mengganti yang ada tetap boleh

    def test_env_set_reports_passthrough_failure_but_keeps_secret(self, env):
        async def runner(argv, environ):
            return 1, "terminal.env_passthrough is managed"

        ok, text = run(commands.do_env_set(self._roster(env), "be-budi", "GH_PAT", SECRET, runner=runner))
        assert ok and "env_passthrough" in text and "hermes_team.py apply" in text
        assert secrets.SecretStore(env.home("be-budi"), extra_env=()).names() == ["GH_PAT"]

    def test_env_list_shows_names_only(self, env):
        store = secrets.SecretStore(env.home("be-budi"), extra_env=())
        store.set("GH_PAT", SECRET)
        store.set("DB_PASSWORD", "hunter2-hunter2")
        text = run(commands.do_env_list(self._roster(env), "be-budi"))
        assert "`GH_PAT`" in text and "`DB_PASSWORD`" in text and "2/40" in text
        assert SECRET not in text and "hunter2" not in text
        assert "Belum ada secret" in run(commands.do_env_list(self._roster(env), "be-be1"))

    def test_env_unset(self, env):
        store = secrets.SecretStore(env.home("be-budi"), extra_env=())
        store.set("GH_PAT", SECRET)
        calls = []

        async def runner(argv, environ):
            calls.append(argv[-1])
            return 0, ""

        (env.home("be-budi") / "config.yaml").write_text("terminal:\n  env_passthrough: [GH_PAT]\n")
        ok, text = run(commands.do_env_unset(self._roster(env), "be-budi", "gh_pat", runner=runner))
        assert ok and "dihapus" in text and store.names() == [] and calls == ["[]"]
        ok, text = run(commands.do_env_unset(self._roster(env), "be-budi", "GH_PAT", runner=runner))
        assert not ok and "tidak ada" in text


class FakeResponse:
    def __init__(self):
        self.messages, self.modals = [], []

    async def send_message(self, content=None, *, ephemeral=False, allowed_mentions=None, **_kw):
        self.messages.append({"content": content, "ephemeral": ephemeral})

    async def send_modal(self, modal):
        self.modals.append(modal)


def interaction(uid, channel_id, parent_id=None):
    return types.SimpleNamespace(user=types.SimpleNamespace(id=int(uid)), channel_id=int(channel_id),
                                 channel=types.SimpleNamespace(id=int(channel_id), parent_id=parent_id),
                                 response=FakeResponse())


class TestDiscordSlashCommands:
    """Memakai discord.py asli (tanpa koneksi) untuk memastikan objek command sah."""

    @pytest.fixture(autouse=True)
    def _needs_discord(self):
        pytest.importorskip("discord")

    def test_three_guild_commands_installed_on_real_tree(self, env):
        import discord
        from discord.ext import commands as dcommands

        async def main():
            bot = dcommands.Bot(command_prefix="!", intents=discord.Intents.none())
            guild = commands.install(bot, env.rt())
            assert guild.id == int(S.GUILD)
            assert sorted(c.name for c in bot.tree.get_commands(guild=guild)) == ["env-list", "env-set", "env-unset"]
            assert bot.tree.get_commands() == []                # tidak menyentuh command global milik adapter
            unset = bot.tree.get_command("env-unset", guild=guild)
            assert [p.name for p in unset.parameters] == ["key"]
            commands.install(bot, env.rt())                      # idempoten (override)
            assert len(bot.tree.get_commands(guild=guild)) == 3

        run(main())

    def test_env_set_opens_modal_only_in_own_channel(self, env):
        async def main():
            built = commands.build_commands(env.rt())
            env_set = next(c for c in built["commands"] if c.name == "env-set")
            ok = interaction(S.U_BUDI, S.CH_BUDI)
            await env_set.callback(ok)
            assert len(ok.response.modals) == 1 and ok.response.modals[0].profile == "be-budi"
            wrong = interaction(S.U_BUDI, S.CH_SHARED)
            await env_set.callback(wrong)
            assert not wrong.response.modals and wrong.response.messages[0]["ephemeral"] is True
            assert "hanya admin" in wrong.response.messages[0]["content"]

        run(main())

    def test_modal_submit_saves_secret_with_ephemeral_reply(self, env):
        async def runner(argv, environ):
            return 0, ""

        async def main():
            built = commands.build_commands(env.rt(), runner=runner)
            modal = built["modal"]("be-budi")
            modal.key._value, modal.value._value = "GH_PAT", SECRET
            inter = interaction(S.U_BUDI, 999, parent_id=int(S.CH_BUDI))      # dari thread di channel pribadi
            await modal.on_submit(inter)
            reply = inter.response.messages[0]
            assert reply["ephemeral"] is True and "`GH_PAT`" in reply["content"] and SECRET not in reply["content"]

        run(main())
        assert secrets.SecretStore(env.home("be-budi"), extra_env=()).read_env()["GH_PAT"] == SECRET

    def test_modal_submitted_from_other_channel_is_refused(self, env):
        async def main():
            modal = commands.build_commands(env.rt())["modal"]("be-budi")
            modal.key._value, modal.value._value = "GH_PAT", SECRET
            inter = interaction(S.U_BE1, S.CH_BE1)                             # profile lain
            await modal.on_submit(inter)
            assert inter.response.messages[0]["ephemeral"] is True

        run(main())
        assert not (env.home("be-budi") / ".env").exists() and not (env.home("be-be1") / ".env").exists()

    def test_held_user_cannot_use_env_commands(self, env):
        async def main():
            env.rt().state.hold(S.U_CITRA, "uji")
            built = commands.build_commands(env.rt())
            env_list = next(c for c in built["commands"] if c.name == "env-list")
            inter = interaction(S.U_CITRA, S.CH_SHARED)
            await env_list.callback(inter)
            assert "menunggu persetujuan" in inter.response.messages[0]["content"]

        run(main())

    def test_list_and_unset_callbacks(self, env):
        secrets.SecretStore(env.home("be-budi"), extra_env=()).set("GH_PAT", SECRET)

        async def runner(argv, environ):
            return 0, ""

        async def main():
            built = {c.name: c for c in commands.build_commands(env.rt(), runner=runner)["commands"]}
            listing = interaction(S.U_BUDI, S.CH_BUDI)
            await built["env-list"].callback(listing)
            assert "`GH_PAT`" in listing.response.messages[0]["content"]
            assert SECRET not in listing.response.messages[0]["content"]
            unset = interaction(S.U_BUDI, S.CH_BUDI)
            await built["env-unset"].callback(unset, "GH_PAT")
            assert "dihapus" in unset.response.messages[0]["content"] and unset.response.messages[0]["ephemeral"]

        run(main())
        assert secrets.SecretStore(env.home("be-budi"), extra_env=()).names() == []


# ============================================================================= hook per profile
class TestProfileHooks:
    def _member(self, env, profile="be-budi", **settings):
        ctx = env.register(profile, **settings)
        secrets.SecretStore(env.home(profile), extra_env=()).set("GH_PAT", SECRET)
        return ctx

    def test_terminal_output_redacted(self, env):
        ctx = self._member(env)
        out = ctx.fire("transform_terminal_output", command="curl x", output=f"token: {SECRET}\n", returncode=0,
                       task_id="t", env_type="docker")
        assert out == ["token: «secret:GH_PAT»\n"]

    def test_clean_output_returns_none_so_other_plugins_can_transform(self, env):
        ctx = self._member(env)
        assert ctx.fire("transform_terminal_output", command="ls", output="README.md\n", returncode=0,
                        task_id="t", env_type="docker") == []
        assert ctx.fire("transform_terminal_output", command="ls", output="", returncode=0) == []

    def test_tool_result_and_llm_output_redacted(self, env):
        ctx = self._member(env)
        encoded = base64.b64encode(SECRET.encode()).decode()
        assert ctx.fire("transform_tool_result", tool_name="read_file", args={}, result=f'{{"content": "{encoded}"}}',
                        task_id="t") == ['{"content": "«secret:GH_PAT»"}']
        assert ctx.fire("transform_llm_output", response_text=f"Token-mu {SECRET[::-1]}", session_id="s",
                        model="m", platform="discord") == ["Token-mu «secret:GH_PAT»"]

    def test_profiles_are_isolated(self, env):
        budi = self._member(env, "be-budi")
        be1 = env.register("be-be1")
        text = f"nilai {SECRET}"
        assert budi.fire("transform_tool_result", tool_name="x", args={}, result=text) == ["nilai «secret:GH_PAT»"]
        assert be1.fire("transform_tool_result", tool_name="x", args={}, result=text) == []   # bukan secret be1

    def test_llm_request_middleware_is_last_filter(self, env):
        ctx = self._member(env)
        token = "ghp_" + "A1b2C3d4" * 5
        request = {"model": "td-simple", "messages": [
            {"role": "system", "content": "kamu Hermes"},
            {"role": "user", "content": f"pakai {token} dan {SECRET}"},
            {"role": "tool", "content": f"hasil: {SECRET.upper()}"}],
            "tools": [{"name": "terminal"}]}
        results = ctx.fire_middleware("llm_request", request=request, original_request=request, session_id="s")
        assert len(results) == 1 and results[0]["source"] == "team-guard"
        new = results[0]["request"]
        dumped = json.dumps(new)
        assert SECRET not in dumped and SECRET.upper() not in dumped and token not in dumped
        assert new["messages"][1]["content"] == "pakai «disamarkan:github-token» dan «secret:GH_PAT»"
        assert new["tools"] == [{"name": "terminal"}] and SECRET in request["messages"][1]["content"]

    def test_llm_request_untouched_returns_none(self, env):
        ctx = self._member(env)
        request = {"messages": [{"role": "user", "content": "halo"}]}
        assert ctx.fire_middleware("llm_request", request=request, original_request=request) == []
        assert ctx.fire_middleware("llm_request", request=None) == []

    def test_responses_api_input_is_filtered_too(self, env):
        ctx = self._member(env)
        token = "sk-" + "a1B2c3D4e5" * 3
        request = {"input": [{"role": "user", "content": [{"type": "input_text", "text": f"key {token}"}]}]}
        new = ctx.fire_middleware("llm_request", request=request)[0]["request"]
        assert token not in json.dumps(new)

    def test_pre_tool_call_blocks_env_dump_with_block_directive(self, env):
        ctx = self._member(env)
        got = ctx.fire("pre_tool_call", tool_name="terminal", args={"command": "printenv | sort"}, task_id="t",
                       session_id="s", tool_call_id="c")
        assert got and got[0]["action"] == "block" and got[0]["message"]
        assert ctx.fire("pre_tool_call", tool_name="terminal", args={"command": "echo $GH_PAT"}, task_id="t")
        assert ctx.fire("pre_tool_call", tool_name="terminal", args={"command": "ls"}, task_id="t") == []

    def test_block_env_dump_can_be_disabled(self, env):
        env.set("settings", block_env_dump=False)
        ctx = self._member(env)
        assert ctx.fire("pre_tool_call", tool_name="terminal", args={"command": "printenv"}, task_id="t") == []

    def test_prompt_hint_lists_names_only(self, env):
        ctx = self._member(env)
        text = ctx.render_prompt_section("team-guard.secrets", session_id="s", profile_name="be-budi")
        assert "$GH_PAT" in text and SECRET not in text and len(text) <= 2000
        empty = env.register("be-be1").render_prompt_section("team-guard.secrets")
        assert "/env-set" in empty and "$" not in empty

    def test_prompt_hint_disabled(self, env):
        env.set("settings", hint=False)
        ctx = self._member(env)
        text = ctx.render_prompt_section("team-guard.secrets")
        assert "GH_PAT" not in text and text

    def test_hold_stream_is_opt_in(self, env):
        assert env.register("be-budi").hook_options["transform_llm_output"] == {"hold_stream": False}
        assert env.register("be-be1", hold_stream=True).hook_options["transform_llm_output"] == {"hold_stream": True}

    def test_turn_hooks_feed_the_shared_tracker(self, env):
        ctx = self._member(env)
        turns = env.rt().turns
        ctx.fire("pre_llm_call", session_id="s1", sender_id=S.U_BUDI, platform="discord", user_message="hi")
        ctx.fire("pre_llm_call", session_id="s2", sender_id=S.ADMIN, platform="discord", user_message="hi")
        assert turns.active(0.0 + __import__("time").monotonic()) == 1       # giliran admin tidak dihitung
        ctx.fire("on_session_end", session_id="s1", completed=True)
        assert turns.active(__import__("time").monotonic()) == 0 and turns.trusted()

    def test_reserved_llm_key_is_redacted_everywhere(self, env, monkeypatch):
        monkeypatch.setenv("TD_API_KEY", "llm-team-key-0001")
        ctx = env.register("be-be1")
        assert ctx.fire("transform_terminal_output", command="x", output="k=llm-team-key-0001") == [
            "k=«secret:TD_API_KEY»"]


# ============================================================================= rate limit & antrean
class TestRateLimit:
    def test_burst_then_spaced(self):
        rl = ratelimit.RateLimiter()
        waits = [rl.reserve("u", 6, 4, 100.0)[0] for _ in range(6)]
        assert waits[:4] == [0, 0, 0, 0] and waits[4] == pytest.approx(10.0) and waits[5] == pytest.approx(20.0)

    def test_refill_over_time(self):
        rl = ratelimit.RateLimiter()
        for _ in range(4):
            rl.reserve("u", 6, 4, 0.0)
        assert rl.reserve("u", 6, 4, 10.0) == (0.0, True)
        assert rl.reserve("u", 6, 4, 10.0)[0] == pytest.approx(10.0)
        assert rl.reserve("u", 6, 4, 1000.0) == (0.0, True)                 # tidak pernah melebihi burst

    def test_refusal_over_max_wait_does_not_consume(self):
        rl = ratelimit.RateLimiter()
        results = [rl.reserve("u", 6, 4, 0.0, 45.0) for _ in range(12)]
        accepted = [r for r in results if r[1]]
        assert len(accepted) == 8 and all(w <= 45.0 for w, _ok in accepted)   # 4 langsung + 4 ditunda ≤ 40 dtk
        assert all(not ok and w > 45.0 for w, ok in results[8:])
        assert rl.reserve("u", 6, 4, 50.0)[1] is True                        # penolakan tidak menambah utang

    def test_users_are_independent_and_zero_rate_is_unlimited(self):
        rl = ratelimit.RateLimiter()
        for _ in range(4):
            rl.reserve("a", 6, 4, 0.0)
        assert rl.reserve("b", 6, 4, 0.0) == (0.0, True)
        assert all(rl.reserve("c", 0, 4, 0.0) == (0.0, True) for _ in range(100))

    def test_forget_idle(self):
        rl = ratelimit.RateLimiter()
        rl.reserve("a", 6, 4, 0.0)
        rl.forget_idle(4000.0)
        assert rl._buckets == {}


class TestTurnTrackerAndProbe:
    def test_reservation_expires_and_is_consumed_by_start(self):
        t = ratelimit.TurnTracker(reserve_ttl=30)
        t.reserve("u1", 0.0)
        t.reserve("u2", 0.0)
        assert t.reserved(10.0) == 2
        t.start("s1", "u1", 11.0)
        assert t.reserved(12.0) == 1 and t.active(12.0) == 1
        assert t.reserved(31.0) == 0
        t.end("s1")
        assert t.active(32.0) == 0 and t.trusted()

    def test_stale_turns_expire(self):
        t = ratelimit.TurnTracker(turn_ttl=100)
        t.start("s1", "u1", 0.0)
        assert t.active(50.0) == 1 and t.active(101.0) == 0

    def test_probe_prefers_min_of_hooks_and_runner(self):
        t = ratelimit.TurnTracker()
        probe = ratelimit.LoadProbe(t)
        runner = S.FakeRunner()
        runner._running_agents = {"a": 1, "b": 1, "c": 1}
        assert probe(runner, 0.0) == 3                                       # hook belum terbukti → internal
        t.start("s1", "u1", 0.0)
        t.start("s0", "u0", 0.0)
        t.end("s0")
        assert probe(runner, 1.0) == 1                                       # hanya giliran anggota
        runner._running_agents = {}
        assert probe(runner, 1.0) == 0                                       # hook bocor → internal mengoreksi

    def test_probe_with_hooks_only(self):
        t = ratelimit.TurnTracker()
        t.start("s0", "u", 0.0)
        t.end("s0")
        t.start("s1", "u", 0.0)
        assert ratelimit.LoadProbe(t)(object(), 1.0) == 1

    def test_queue_disables_itself_with_one_log_line(self, caplog):
        probe = ratelimit.LoadProbe(ratelimit.TurnTracker())
        with caplog.at_level(logging.WARNING, logger="team-guard"):
            assert probe(object(), 0.0) is None and probe(object(), 1.0) is None and probe(None, 2.0) is None
        lines = [r for r in caplog.records if "antrean sesi nonaktif" in r.getMessage()]
        assert len(lines) == 1

    def test_broken_internal_counts_as_missing(self):
        runner = types.SimpleNamespace(_running_agent_count=lambda: 1 / 0)
        assert ratelimit.LoadProbe(ratelimit.TurnTracker())(runner, 0.0) is None


class TestAdmissionQueue:
    def test_fifo_positions(self):
        q = ratelimit.AdmissionQueue()
        a, b, c = q.enter("a", 0), q.enter("b", 1), q.enter("c", 2)
        assert [q.position(t) for t in (a, b, c)] == [1, 2, 3] and q.is_head(a) and not q.is_head(b)
        q.leave(a)
        assert q.is_head(b) and q.position(a) == 0 and len(q) == 2

    def test_wait_for_slot_admits_when_free(self):
        clock = S.Clock()
        q = ratelimit.AdmissionQueue()
        free_at = clock.now + 5
        told = []

        async def main():
            ticket = q.enter("u", clock())
            return await ratelimit.wait_for_slot(q, ticket, free=lambda: clock() >= free_at, max_wait=180,
                                                 clock=clock, sleep=clock.sleep, on_wait=told.append)

        assert run(main()) is True and told == [1] and 5 <= clock.now - 1000 <= 6

    def test_wait_for_slot_times_out(self):
        clock = S.Clock()
        q = ratelimit.AdmissionQueue()

        async def main():
            ticket = q.enter("u", clock())
            return await ratelimit.wait_for_slot(q, ticket, free=lambda: False, max_wait=180, clock=clock,
                                                 sleep=clock.sleep)

        assert run(main()) is False and 180 <= clock.now - 1000 <= 181

    def test_waiters_admitted_in_arrival_order(self):
        clock = S.Clock()
        q = ratelimit.AdmissionQueue()
        slots = {"free": 0}
        order = []

        async def waiter(name):
            ticket = q.enter(name, clock())
            ok = await ratelimit.wait_for_slot(q, ticket, free=lambda: slots["free"] > 0, max_wait=500,
                                               clock=clock, sleep=clock.sleep)
            if ok:
                slots["free"] -= 1
                order.append(name)
            q.leave(ticket)

        async def main():
            tasks = [asyncio.ensure_future(waiter(n)) for n in ("pertama", "kedua", "ketiga")]
            await asyncio.sleep(0)
            for _ in range(3):
                for _ in range(20):
                    await asyncio.sleep(0)
                slots["free"] += 1
            await asyncio.gather(*tasks)

        run(main())
        assert order == ["pertama", "kedua", "ketiga"]

    def test_unmeasurable_load_lets_everyone_through(self):
        clock = S.Clock()
        q = ratelimit.AdmissionQueue()

        async def main():
            return await ratelimit.wait_for_slot(q, q.enter("u", clock()), free=lambda: None, max_wait=10,
                                                 clock=clock, sleep=clock.sleep)

        assert run(main()) is True and clock.slept == []


# ============================================================================= pre_gateway_dispatch
class Raw:
    def __init__(self, fail=False):
        self.deleted, self.fail = False, fail

    async def delete(self):
        if self.fail:
            raise PermissionError("Missing Permissions")
        self.deleted = True


def event(uid, chat, text="halo", **kw):
    source_kw = {k: kw.pop(k) for k in ("platform", "chat_type", "guild_id", "thread_id", "parent_chat_id",
                                        "is_bot") if k in kw}
    return S.FakeEvent(text=text, source=S.FakeSource(user_id=uid, chat_id=chat, **source_kw), **kw)


class TestDispatchGate:
    @pytest.fixture
    def d(self, env):
        self.env = env
        self.clock = S.Clock()
        self.rt = env.rt()
        return dispatch.Dispatcher(self.rt, clock=self.clock, sleep=self.clock.sleep)

    def go(self, d, ev, gateway=None):
        return run(d.handle(event=ev, gateway=self.env.runner if gateway is None else gateway, session_store=None))

    def said(self):
        return [m["content"] for m in self.env.adapter.sent]

    # --- lingkup
    @pytest.mark.parametrize("ev", [
        event(S.U_BUDI, S.CH_BUDI, platform=S.TELEGRAM),
        event(S.U_BUDI, "123456789"),                                        # channel bukan milik tim
        event(S.U_BUDI, S.U_BUDI, chat_type="dm"),                           # DM: ditolak Hermes sendiri
        event(S.U_BUDI, S.CH_BUDI, internal=True),
        event(S.U_BUDI, S.CH_BUDI, guild_id="111"),                          # server lain
        event(None, S.CH_BUDI),
        event(S.BOT, S.CH_BUDI, is_bot=True),
        S.FakeEvent(text="x", source=None),
    ])
    def test_out_of_scope_events_pass_untouched(self, d, ev):
        self.rt.state.hold(S.U_BUDI, "uji")
        assert self.go(d, ev) is None and self.said() == []

    def test_normal_member_message_passes(self, d):
        assert self.go(d, event(S.U_BUDI, S.CH_BUDI)) is None

    def test_personal_channel_serves_other_viewers_under_non_strict_routes(self, d):
        """Rute non-strict: siapa pun yang bisa melihat channel dilayani profile pemiliknya (perilaku upstream)."""
        assert self.go(d, event(S.U_BE1, S.CH_BUDI)) is None

    # --- E. gerbang akses
    @pytest.mark.parametrize("chat", [S.CH_SHARED, S.CH_BUDI, S.CH_ADMIN])
    def test_held_user_refused_everywhere(self, d, chat):
        self.rt.state.hold(S.U_CITRA, "role diberikan oleh orang lain")
        got = self.go(d, event(S.U_CITRA, chat))
        assert got["action"] == "skip" and "ditahan" in got["reason"]
        assert len(self.said()) == 1 and "menunggu persetujuan admin" in self.said()[0]

    def test_held_notice_is_throttled_per_user(self, d):
        self.rt.state.hold(S.U_CITRA, "x")
        for _ in range(5):
            assert self.go(d, event(S.U_CITRA, S.CH_SHARED))["action"] == "skip"
        assert len(self.said()) == 1
        self.clock.advance(61)
        self.go(d, event(S.U_CITRA, S.CH_SHARED))
        assert len(self.said()) == 2

    def test_unhold_restores_service(self, d):
        self.rt.state.hold(S.U_CITRA, "x")
        assert self.go(d, event(S.U_CITRA, S.CH_SHARED))["action"] == "skip"
        self.rt.state.unhold(S.U_CITRA)
        assert self.go(d, event(S.U_CITRA, S.CH_SHARED)) is None

    def test_member_already_in_roster_is_never_treated_as_held(self, d):
        """Admin sudah menyetujui (anggota aktif di roster) → tahanan lama tidak boleh memblokirnya."""
        self.rt.state.hold(S.U_BUDI, "sisa tahanan lama")
        assert self.go(d, event(S.U_BUDI, S.CH_BUDI)) is None

    def test_inactive_member_refused(self, d):
        S.roster_deactivate(self.env.roster, S.U_BUDI)
        self.env.save()
        got = self.go(d, event(S.U_BUDI, S.CH_SHARED))
        assert got["action"] == "skip" and "nonaktif" in got["reason"] and "nonaktif" in self.said()[0]

    def test_admin_is_never_gated(self, d):
        self.rt.state.hold(S.ADMIN, "tidak mungkin, tapi tetap")
        assert self.go(d, event(S.ADMIN, S.CH_ADMIN)) is None

    def test_thread_under_team_channel_is_recognised(self, d):
        self.rt.state.hold(S.U_CITRA, "x")
        got = self.go(d, event(S.U_CITRA, "888", parent_chat_id=S.CH_SHARED, thread_id="888"))
        assert got["action"] == "skip"
        assert self.env.adapter.sent[0]["metadata"] == {"thread_id": "888"}

    # --- C. token di chat
    def test_token_in_chat_rewritten_deleted_and_user_reminded(self, d):
        token = "ghp_" + "A1b2C3d4" * 5
        raw = Raw()
        got = self.go(d, event(S.U_BUDI, S.CH_BUDI, f"clone pakai {token}", raw_message=raw))
        assert got == {"action": "rewrite", "text": "clone pakai «disamarkan:github-token»"}
        assert raw.deleted and "/env-set" in self.said()[0] and "dihapus" in self.said()[0]
        assert token not in self.said()[0]

    def test_delete_can_be_disabled(self, d):
        self.env.set("settings", delete_secret_messages=False)
        raw = Raw()
        got = self.go(d, event(S.U_BUDI, S.CH_BUDI, "sk-" + "a1B2c3D4e5" * 3, raw_message=raw))
        assert got["action"] == "rewrite" and not raw.deleted and "dihapus" not in self.said()[0]

    def test_delete_failure_is_tolerated(self, d):
        got = self.go(d, event(S.U_BUDI, S.CH_BUDI, "AKIAABCDEFGHIJKLMNOP", raw_message=Raw(fail=True)))
        assert got["action"] == "rewrite" and "AKIA" not in got["text"]

    def test_scrub_chat_can_be_disabled(self, d):
        self.env.set("settings", scrub_chat=False)
        raw = Raw()
        assert self.go(d, event(S.U_BUDI, S.CH_BUDI, "ghp_" + "A1b2C3d4" * 5, raw_message=raw)) is None
        assert not raw.deleted

    def test_admin_tokens_are_scrubbed_too(self, d):
        got = self.go(d, event(S.ADMIN, S.CH_ADMIN, "xoxb-1234567890-abcdefghij", raw_message=Raw()))
        assert got["action"] == "rewrite"

    # --- D. rate limit
    def test_burst_passes_then_excess_is_delayed(self, d):
        for _ in range(4):
            assert self.go(d, event(S.U_BUDI, S.CH_BUDI)) is None
        assert self.clock.slept == []
        assert self.go(d, event(S.U_BUDI, S.CH_BUDI)) is None
        assert self.clock.slept == [pytest.approx(10.0)]

    def test_flood_is_politely_refused_when_wait_exceeds_45s(self, d):
        delays = []

        async def instant(seconds):                                          # 12 pesan tiba pada detik yang sama
            delays.append(seconds)
            await asyncio.sleep(0)

        d.sleep = instant

        async def flood():
            return await asyncio.gather(*[d.handle(event=event(S.U_BUDI, S.CH_BUDI), gateway=self.env.runner)
                                          for _ in range(12)])

        results = run(flood())
        assert results.count(None) == 8                                      # 4 langsung + 4 ditunda
        assert delays == [pytest.approx(x) for x in (10.0, 20.0, 30.0, 40.0)]
        skipped = [r for r in results if r]
        assert len(skipped) == 4 and all(r["reason"].endswith("rate limit") for r in skipped)
        assert sum("terlalu cepat" in s for s in self.said()) == 1 and "6 pesan/menit" in self.said()[0]

    def test_rate_limit_is_per_user(self, d):
        for _ in range(4):
            self.go(d, event(S.U_BUDI, S.CH_BUDI))
        assert self.go(d, event(S.U_BE1, S.CH_BE1)) is None and self.clock.slept == []

    def test_admin_and_slash_commands_bypass_rate_limit(self, d):
        for _ in range(30):
            assert self.go(d, event(S.ADMIN, S.CH_ADMIN)) is None
            assert self.go(d, event(S.U_BUDI, S.CH_BUDI, "/stop")) is None
        assert self.clock.slept == [] and self.said() == []

    def test_rate_zero_disables_limit(self, d):
        self.env.set("settings", rate_per_minute=0)
        assert all(self.go(d, event(S.U_BUDI, S.CH_BUDI)) is None for _ in range(50))
        assert self.clock.slept == []

    # --- D. antrean
    def test_full_gateway_queues_then_admits_when_slot_frees(self, d):
        self.env.set("settings", max_active=2, rate_per_minute=0)
        runner = self.env.runner
        runner._running_agents = {"a": 1, "b": 1}

        async def main():
            task = asyncio.ensure_future(d.handle(event=event(S.U_BUDI, S.CH_BUDI), gateway=runner))
            for _ in range(10):
                await asyncio.sleep(0)
            assert not task.done() and len(self.rt.queue) == 1
            runner._running_agents.pop("a")
            return await task

        assert run(main()) is None
        assert "antrean nomor 1" in self.said()[0] and len(self.rt.queue) == 0
        assert self.rt.turns.reserved(self.clock()) == 1                      # slot dipesan untuk giliran ini

    def test_queue_timeout_is_politely_refused(self, d):
        self.env.set("settings", max_active=1, queue_max_wait=180, rate_per_minute=0)
        self.env.runner._running_agents = {"a": 1}
        got = self.go(d, event(S.U_BUDI, S.CH_BUDI))
        assert got["action"] == "skip" and "antrean" in got["reason"]
        assert 180 <= self.clock.now - 1000 <= 182 and any("masih penuh" in s for s in self.said())

    def test_reservation_counts_against_capacity(self, d):
        self.env.set("settings", max_active=1, queue_max_wait=5, rate_per_minute=0)
        assert self.go(d, event(S.U_BUDI, S.CH_BUDI)) is None                 # ambil satu-satunya slot
        got = self.go(d, event(S.U_BE1, S.CH_BE1))                            # giliran budi belum mulai
        assert got["action"] == "skip"

    def test_admin_bypasses_queue(self, d):
        self.env.set("settings", max_active=1)
        self.env.runner._running_agents = {"a": 1, "b": 1}
        assert self.go(d, event(S.ADMIN, S.CH_ADMIN)) is None and self.clock.slept == []

    def test_followup_to_running_session_is_not_queued(self, d):
        self.env.set("settings", max_active=1, rate_per_minute=0)
        self.env.runner._running_agents = {f"{S.CH_BUDI}:{S.U_BUDI}": 1}
        assert self.go(d, event(S.U_BUDI, S.CH_BUDI)) is None and self.clock.slept == []

    def test_queue_off_when_gateway_internals_are_gone(self, d, caplog):
        self.env.set("settings", max_active=1, rate_per_minute=0)
        bare = types.SimpleNamespace(adapters=self.env.runner.adapters)       # tanpa _running_agent_count
        with caplog.at_level(logging.WARNING, logger="team-guard"):
            assert all(self.go(d, event(S.U_BUDI, S.CH_BUDI), gateway=bare) is None for _ in range(5))
        assert sum("antrean sesi nonaktif" in r.getMessage() for r in caplog.records) == 1

    def test_max_active_zero_disables_queue(self, d):
        self.env.set("settings", max_active=0, rate_per_minute=0)
        self.env.runner._running_agents = {"a": 1}
        assert self.go(d, event(S.U_BUDI, S.CH_BUDI)) is None

    def test_hook_failure_is_fail_open(self, d):
        class Boom:
            @property
            def adapters(self):
                raise RuntimeError("upstream berubah")

        self.rt.state.hold(S.U_CITRA, "x")
        assert self.go(d, event(S.U_CITRA, S.CH_SHARED), gateway=Boom())["action"] == "skip"   # kabar gagal ≠ lolos
        broken = S.FakeEvent(text="x", source=types.SimpleNamespace(platform=S.DISCORD, user_id=S.U_BUDI))
        assert self.go(d, broken) is None

    def test_no_roster_means_no_policy(self, tmp_path):
        rt = runtime.runtime_for(tmp_path / "kosong")
        d = dispatch.Dispatcher(rt)
        assert run(d.handle(event=event(S.U_BUDI, S.CH_BUDI), gateway=None)) is None


# ============================================================================= live reload
class TestLiveReload:
    @pytest.fixture
    def live(self, env):
        self.env = env
        self.clock = S.Clock()
        self.notices = []
        self.spawned = []
        rt = env.rt()
        rt.started = self.clock() - 10_000                                    # sudah lama berjalan

        async def notify(text):
            self.notices.append(text)

        def spawn(coro):
            task = asyncio.get_running_loop().create_task(coro)
            self.spawned.append(task)
            return task

        return livereload.LiveReloader(rt, get_runner=lambda: env.runner, get_adapter=lambda: env.adapter,
                                       notify=notify, upstream=env.upstream, clock=self.clock,
                                       sleep=self.clock.sleep, spawn=spawn)

    def add_member(self, uid=S.U_DODI, name="dodi", channel="610000000000000004"):
        S.roster_add(self.env.roster, uid, name, channel)
        (self.env.hermes_root / "profiles" / f"be-{name}").mkdir(parents=True, exist_ok=True)
        self.env.save()
        return channel

    def apply(self, live, **kw):
        async def main():
            result = await live.apply("uji", **kw)
            if self.spawned:
                await asyncio.gather(*self.spawned)
            return result

        return run(main())

    def test_expected_routes(self, env):
        got = livereload.expected_routes(env.rt().roster())
        assert (S.CH_BUDI, "be-budi", S.U_BUDI) in got and (S.CH_ADMIN, "team-admin", S.ADMIN) in got
        assert (S.CH_SHARED, "team-shared", S.U_BE1) in got and len(got) == 4

    def test_apply_updates_routes_in_place_and_both_gate_stores(self, live):
        routes_obj = self.env.runner.config.profile_routes
        channel = self.add_member()
        ok, detail = self.apply(live)
        assert ok and detail == "5 rute, 5 channel"
        assert self.env.runner.config.profile_routes is routes_obj           # objek yang dibaca per pesan
        match = self.env.upstream.match_profile_route(routes_obj, guild_id=S.GUILD, chat_id=channel)
        assert match.profile == "be-dodi"
        assert channel in self.env.adapter._gate_env_snapshot["DISCORD_ALLOWED_CHANNELS"].split(",")
        assert channel in self.env.adapter.config.extra["free_response_channels"].split(",")
        assert channel in self.env.adapter._get_allowed_channels()
        assert channel in self.env.adapter._discord_free_response_channels()
        assert self.env.rt().state.live()["ok"] is True and self.notices == []

    def test_thread_in_new_channel_routes_via_parent(self, live):
        channel = self.add_member()
        self.apply(live)
        match = self.env.upstream.match_profile_route(self.env.runner.config.profile_routes, guild_id=S.GUILD,
                                                      chat_id="999", thread_id="999", parent_chat_id=channel)
        assert match.profile == "be-dodi"

    def test_gates_living_only_in_config_extra(self, live):
        self.env.adapter._gate_env_snapshot = None
        channel = self.add_member()
        assert self.apply(live)[0] and channel in self.env.adapter._get_allowed_channels()

    def test_gates_living_only_in_env_snapshot(self, live):
        self.env.adapter.config.extra = {}
        channel = self.add_member()
        assert self.apply(live)[0] and channel in self.env.adapter._get_allowed_channels()
        assert channel in self.env.adapter._discord_free_response_channels()

    def test_process_env_updated_only_when_already_present(self, live, monkeypatch):
        monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "lama")
        monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)
        channel = self.add_member()
        self.apply(live)
        assert channel in os.environ["DISCORD_ALLOWED_CHANNELS"]
        assert "DISCORD_FREE_RESPONSE_CHANNELS" not in os.environ

    def test_offboarded_member_route_and_channel_removed(self, live):
        S.roster_deactivate(self.env.roster, S.U_BUDI)
        self.env.save()
        assert self.apply(live)[0]
        routes = self.env.runner.config.profile_routes
        assert self.env.upstream.match_profile_route(routes, guild_id=S.GUILD, chat_id=S.CH_BUDI) is None
        assert S.CH_BUDI not in self.env.adapter._get_allowed_channels()

    def test_verify_detects_wrong_route(self, live):
        roster = self.env.rt().roster()
        self.env.runner.config.profile_routes[:] = self.env.upstream.parse_profile_routes(
            [dict(r, profile="be-be1") if r["chat_id"] == S.CH_BUDI else r for r in roster.profile_routes])
        ok, detail = live.verify(self.env.runner, self.env.adapter, roster)
        assert not ok and S.CH_BUDI in detail and "be-budi" in detail

    def test_verify_detects_stale_gate_and_lingering_inactive_route(self, live):
        roster = self.env.rt().roster()
        self.env.adapter._gate_env_snapshot["DISCORD_ALLOWED_CHANNELS"] = S.CH_SHARED
        ok, detail = live.verify(self.env.runner, self.env.adapter, roster)
        assert not ok and "allowed_channels" in detail
        live.apply_gates(self.env.adapter, roster)
        S.roster_deactivate(self.env.roster, S.U_BUDI)
        self.env.save()
        ok, detail = live.verify(self.env.runner, self.env.adapter, self.env.rt().roster(force=True))
        assert not ok and "nonaktif" in detail

    def test_strict_user_routes_verify_with_owner(self, live):
        for route in self.env.roster["gateway"]["profile_routes"]:
            if route["name"] == "personal-budi":
                route["user_id"] = S.U_BUDI
        self.env.save()
        assert self.apply(live)[0]

    # --- fallback
    def test_idle_fallback_restarts_gracefully_when_no_sessions(self, live):
        self.env.upstream.break_match = True
        ok, detail = self.apply(live)
        assert not ok and "match_profile_route" in detail
        assert self.env.runner.restart_requests == [{"detached": False, "via_service": True}]
        assert len(self.notices) == 1 and "restart halus" in self.notices[0]
        state = self.env.rt().state
        assert state.live()["ok"] is False and state.seconds_since_auto_restart() is not None

    def test_idle_fallback_waits_for_running_sessions(self, live):
        self.env.upstream.break_match = True
        self.env.runner._running_agents = {"a": 1}
        original_sleep = self.clock.sleep

        async def sleep(seconds):
            await original_sleep(seconds)
            if self.clock.now - 1000 > 100:
                self.env.runner._running_agents.clear()

        live.sleep = sleep
        self.apply(live)
        assert len(self.env.runner.restart_requests) == 1 and 100 < self.clock.now - 1000 < 200

    def test_idle_fallback_restarts_anyway_after_ten_minutes(self, live):
        self.env.upstream.break_match = True
        self.env.runner._running_agents = {"a": 1}
        self.apply(live)
        assert len(self.env.runner.restart_requests) == 1 and 600 <= self.clock.now - 1000 <= 620

    def test_manual_fallback_only_tells_admins(self, live):
        self.env.roster["restart_fallback"] = "manual"
        self.env.save()
        self.env.upstream.break_match = True
        assert not self.apply(live)[0]
        assert self.env.runner.restart_requests == [] and "/restart" in self.notices[0]

    def test_no_restart_loop_when_failing_right_after_start(self, live):
        self.env.rt().started = self.clock()                                 # gateway baru saja start
        self.env.upstream.break_match = True
        assert not self.apply(live, startup=True)[0]
        assert self.env.runner.restart_requests == [] and live.manual_only
        assert "TIDAK me-restart" in self.notices[0] and "/restart" in self.notices[0]
        self.clock.advance(5000)                                             # tetap manual sampai proses diganti
        self.apply(live)
        assert self.env.runner.restart_requests == []

    def test_no_second_auto_restart_within_cooldown(self, live):
        self.env.rt().state.mark_auto_restart()                              # proses sebelumnya baru me-restart
        self.env.upstream.break_match = True
        self.apply(live)
        assert self.env.runner.restart_requests == [] and "TIDAK me-restart" in self.notices[0]

    def test_same_failure_notifies_once(self, live):
        self.env.roster["restart_fallback"] = "manual"
        self.env.save()
        self.env.upstream.break_match = True
        for _ in range(4):
            self.apply(live)
        assert len(self.notices) == 1

    def test_recovery_after_failure_clears_status(self, live):
        self.env.roster["restart_fallback"] = "manual"
        self.env.save()
        self.env.upstream.break_match = True
        self.apply(live)
        self.env.upstream.break_match = False
        assert self.apply(live)[0] and self.env.rt().state.live()["ok"] is True

    @pytest.mark.parametrize("breakage,fragment", [
        ("parse", "parse_profile_routes"), ("no_routes_attr", "profile_routes"),
        ("no_allowed_getter", "_get_allowed_channels"), ("no_free_getter", "_discord_free_response_channels"),
        ("no_gate_stores", "_gate_env_snapshot"), ("no_runner", "belum tersambung"),
    ])
    def test_upstream_change_degrades_to_fallback_not_crash(self, live, breakage, fragment):
        self.env.roster["restart_fallback"] = "manual"
        self.env.save()
        if breakage == "parse":
            self.env.upstream.break_parse = True
        elif breakage == "no_routes_attr":
            self.env.runner.config = types.SimpleNamespace()
        elif breakage == "no_allowed_getter":
            self.env.adapter._get_allowed_channels = None
        elif breakage == "no_free_getter":
            self.env.adapter._discord_free_response_channels = None
        elif breakage == "no_gate_stores":
            self.env.adapter._gate_env_snapshot = None
            self.env.adapter.config = None
        elif breakage == "no_runner":
            live.get_runner = lambda: None
        ok, detail = self.apply(live)
        assert not ok and fragment in detail
        assert len(self.notices) == 1 and self.env.rt().state.live()["ok"] is False

    def test_restart_api_missing_falls_back_to_manual_notice(self, live):
        self.env.upstream.break_match = True
        self.env.runner.request_restart = None
        self.apply(live)
        assert any("/restart" in n for n in self.notices)

    def test_live_reload_disabled_changes_nothing(self, live):
        self.env.roster["live_reload"] = False
        channel = self.add_member()
        ok, detail = self.apply(live)
        assert not ok and "dimatikan" in detail and self.notices == []
        assert channel not in self.env.adapter._get_allowed_channels()
        assert self.env.runner.restart_requests == []

    def test_new_profile_waits_until_served_using_rescan(self, live):
        self.env.upstream.served = {"default", "be-be1", "be-budi", "team-shared", "team-admin"}
        self.add_member()
        original_sleep = self.clock.sleep

        async def sleep(seconds):
            await original_sleep(seconds)
            self.env.upstream.served.add("be-dodi")                          # rescan Hermes menemukannya

        live.sleep = sleep
        assert self.apply(live)[0] and self.env.upstream.rescans >= 2

    def test_profile_never_served_fails_verification(self, live):
        self.env.roster["restart_fallback"] = "manual"
        self.env.upstream.served = {"default", "be-be1", "be-budi", "team-shared", "team-admin"}
        self.add_member()
        ok, detail = self.apply(live)
        assert not ok and "be-dodi" in detail and "belum dilayani" in detail

    def test_control_socket_unavailable_relies_on_periodic_rescan(self, live):
        self.env.upstream.rescan = lambda home: (_ for _ in ()).throw(ImportError("gateway.control_socket"))
        self.add_member()
        assert self.apply(live)[0]


# ============================================================================= roster & state
class TestRosterAndState:
    def test_roster_defaults_for_missing_fields(self):
        r = rostermod.Roster({"guild_id": 1, "settings": {"burst": 9}})
        assert r.guild_id == "1" and r.settings["burst"] == 9 and r.settings["max_active"] == 8
        assert r.onboarding["verify_actor"] is True and r.restart_fallback == "idle" and r.live_reload is True
        assert not rostermod.Roster() and not rostermod.Roster("rusak")

    def test_profile_helpers(self, env):
        r = env.rt().roster()
        assert r.profile_of_home(env.hermes_root) == "default" and r.profile_kind("default") == "host"
        assert r.profile_of_home(env.home("be-budi")) == "be-budi" and r.profile_kind("be-budi") == "member"
        assert r.profile_kind("team-shared") == "shared" and r.profile_kind("team-admin") == "admin"
        assert r.profile_kind("be-tidak-ada") == "other" and r.profile_home("be-budi") == env.home("be-budi")
        assert r.user_status(S.ADMIN) == "admin" and r.user_status(S.U_BUDI) == "active"
        assert r.user_status("1") == "unknown"

    def test_cache_reloads_on_change_and_survives_torn_write(self, env):
        cache = env.rt().roster_cache
        first = cache.get()
        assert cache.get() is first and not cache.changed()
        (env.state_dir / "roster.json").write_text("{ setengah tertulis")
        assert cache.changed() and cache.get() is first and "tidak terbaca" in cache.error
        S.roster_add(env.roster, S.U_DODI, "dodi", "610000000000000004")
        env.save()
        assert S.U_DODI in cache.get().users and cache.error == ""

    def test_state_file_is_private_atomic_and_teamctl_readable(self, tmp_path):
        store = statemod.StateStore(tmp_path / "state" / "onboarding.json")
        assert store.hold("5", "pemberi role tidak dikenal", actor="7") is True
        assert store.hold("5", "pemberi role tidak dikenal") is False        # tidak mengabari ulang
        store.set_live(False, "rute salah")
        path = tmp_path / "state" / "onboarding.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        data = json.loads(path.read_text())
        assert data["held"]["5"]["reason"] == "pemberi role tidak dikenal" and data["held"]["5"]["ts"]
        assert data["live"]["ok"] is False and data["live"]["detail"] == "rute salah"
        assert not list(path.parent.glob(".onboarding.json.*"))              # tidak ada sisa file sementara

    def test_state_survives_restart(self, tmp_path):
        path = tmp_path / "onboarding.json"
        a = statemod.StateStore(path)
        a.hold("5", "x")
        a.mark_welcomed("6", "60")
        a.set_pending("7", "70", "700")
        b = statemod.StateStore(path)
        assert b.is_held("5") and b.was_welcomed("6") and b.pop_pending("7") == {"channel_id": "70",
                                                                                   "message_id": "700"}
        assert b.unhold("5") is True and b.unhold("5") is False

    def test_state_tolerates_corrupt_file_and_unwritable_dir(self, tmp_path):
        path = tmp_path / "onboarding.json"
        path.write_text("bukan json")
        store = statemod.StateStore(path)
        assert store.data["held"] == {} and store.baseline_needed
        blocked = statemod.StateStore(tmp_path / "file-bukan-folder" / "onboarding.json")
        (tmp_path / "file-bukan-folder").write_text("x")
        blocked.hold("5", "tetap jalan")                                      # gagal tulis tidak melempar
        assert blocked.is_held("5")

    def test_welcome_bookkeeping(self, tmp_path):
        store = statemod.StateStore(tmp_path / "s.json")
        assert store.baseline_needed
        store.baseline({"1": {"channel_id": "10"}})
        assert store.was_welcomed("1") and not store.baseline_needed
        assert store.mark_inactive("1") and not store.was_welcomed("1") and store.known_before("1")
        assert store.notify_once("k") and not store.notify_once("k")
        store.clear_notified("k")
        assert store.notify_once("k")

    def test_auto_restart_timestamp(self, tmp_path):
        now = [1000.0]
        store = statemod.StateStore(tmp_path / "s.json", clock=lambda: now[0])
        assert store.seconds_since_auto_restart() is None
        store.mark_auto_restart()
        now[0] += 120
        store.set_live(True, "ok")                                           # tidak menghapus cap restart
        assert store.seconds_since_auto_restart() == pytest.approx(120)


# ============================================================================= register() per jenis profile
EVERYWHERE = {"pre_gateway_dispatch", "pre_tool_call", "transform_terminal_output", "transform_tool_result",
              "transform_llm_output", "pre_llm_call", "on_session_start", "on_session_end"}


class TestRegisterWiring:
    def test_host_profile_registers_everything_including_discord_handler(self, env):
        ctx = env.register("default")
        assert set(ctx.hooks) == EVERYWHERE and set(ctx.middleware) == {"llm_request"}
        assert list(ctx.platform_handlers) == ["discord"] and len(ctx.platform_handlers["discord"]) == 1
        assert "team-guard.secrets" in ctx.prompt_sections and len(ctx.unload_callbacks) == 1
        assert ctx.unknown_hooks == [] and env.rt().profiles == {"default": "host"}

    @pytest.mark.parametrize("profile,kind", [("be-budi", "member"), ("team-shared", "shared"),
                                              ("team-admin", "admin")])
    def test_non_host_profiles_skip_host_only_parts(self, env, profile, kind):
        ctx = env.register(profile)
        assert set(ctx.hooks) == EVERYWHERE and set(ctx.middleware) == {"llm_request"}
        assert ctx.platform_handlers == {} and ctx.unload_callbacks == []
        assert env.rt().profiles[profile] == kind

    def test_each_profile_gets_own_module_copy_but_one_shared_runtime(self, env):
        """Hermes memuat salinan modul per profile (`…__home_<digest>`); state proses harus tetap satu."""
        other = S.load_plugin("__home_0123456789ab")
        assert other is not P and S.sub(other, "runtime") is not runtime
        host = env.register("default")
        member = env.register("be-budi", plugin=other)
        rt_host = runtime.runtime_for(env.state_dir)
        rt_member = S.sub(other, "runtime").runtime_for(env.state_dir)
        assert rt_host is rt_member and set(rt_host.profiles) == {"default", "be-budi"}
        assert host.hooks["pre_gateway_dispatch"][0] == member.hooks["pre_gateway_dispatch"][0]
        rt_host.state.hold(S.U_CITRA, "x")                                    # ditahan di host → terlihat di salinan lain
        got = run(member.afire("pre_gateway_dispatch", event=event(S.U_CITRA, S.CH_SHARED), gateway=env.runner,
                               session_store=None))
        assert got and got[0]["action"] == "skip"

    def test_dispatch_hook_is_async_and_allows_normal_messages(self, env):
        ctx = env.register("default")
        assert asyncio.iscoroutinefunction(ctx.hooks["pre_gateway_dispatch"][0])
        assert run(ctx.afire("pre_gateway_dispatch", event=event(S.U_BUDI, S.CH_BUDI), gateway=env.runner,
                             session_store=None)) == []

    def test_state_dir_falls_back_to_default_location(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_TEAM_DIR", str(tmp_path / "team"))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        ctx = S.StubContext({})
        P.register(ctx)
        assert runtime.existing_runtime(tmp_path / "team" / "state") is not None

    def test_without_roster_secret_protection_still_registers(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        ctx = S.StubContext({"state_dir": str(tmp_path / "kosong")})
        with caplog.at_level(logging.WARNING, logger="team-guard"):
            P.register(ctx)
            P.register(S.StubContext({"state_dir": str(tmp_path / "kosong")}))
        assert set(ctx.hooks) == EVERYWHERE
        assert sum("roster.json belum ada" in r.getMessage() for r in caplog.records) == 1
        assert ctx.fire("pre_tool_call", tool_name="terminal", args={"command": "printenv"}, task_id="t")

    def test_older_hermes_without_some_apis_does_not_break_loading(self, env, monkeypatch):
        class OldContext(S.StubContext):
            register_middleware = None
            register_system_prompt_section = None
            register_redaction_patterns = None

            def register_hook(self, hook_name, callback):                    # tanpa kw hold_stream
                return S.StubContext.register_hook(self, hook_name, callback)

        ctx = OldContext({"state_dir": str(env.state_dir), "hold_stream": True})
        monkeypatch.setenv("HERMES_HOME", str(env.hermes_root))
        P.register(ctx)
        assert set(ctx.hooks) == EVERYWHERE and "discord" in ctx.platform_handlers

    def test_factory_wires_exactly_once_per_native_client(self, env):
        async def main():
            ctx = env.register("default")
            factory = ctx.platform_handlers["discord"][0]
            env.rt()                                                         # runtime yang sama
            factory(env.bot, env.adapter)
            first = env.rt().host
            first.live.upstream = env.upstream
            factory(env.bot, env.adapter)                                    # rewire / profile lain memanggil lagi
            assert env.rt().host is first
            assert {k: len(v) for k, v in env.bot.listeners.items()} == {
                "on_member_join": 1, "on_member_update": 1, "on_member_remove": 1}
            new_bot = S.FakeBot()                                            # client dibangun ulang (reconnect)
            factory(new_bot, env.adapter)
            second = env.rt().host
            assert second is not first and second.native is new_bot
            assert all(len(v) == 0 for v in env.bot.listeners.values())      # jembatan lama dilepas
            assert all(t.done() or t.cancelling() for t in first.tasks) or first.tasks == []
            second.close()

        run(main())

    def test_factory_survives_missing_native(self, env):
        ctx = env.register("default")
        ctx.platform_handlers["discord"][0](None, env.adapter)
        assert env.rt().host is None

    def test_startup_task_is_supervised_by_ctx_spawn_task(self, env):
        async def main():
            ctx = env.register("default")
            bridge = env.attach()
            assert ctx.spawned and ctx.spawned[0].get_name() == "team-guard:startup"
            await asyncio.sleep(0.05)
            assert env.rt().state.live()["ok"] is True                       # live reload awal terverifikasi
            bridge.close()

        run(main())

    def test_unload_then_reload_reattaches_listeners(self, env):
        async def main():
            ctx = env.register("default")
            factory = ctx.platform_handlers["discord"][0]
            factory(env.bot, env.adapter)
            env.rt().host.live.upstream = env.upstream
            ctx.unload_callbacks[0]()
            await asyncio.sleep(0.01)
            assert env.rt().host is None and all(len(v) == 0 for v in env.bot.listeners.values())
            env.register("default")                                          # reload-plugins
            await asyncio.sleep(0.01)
            assert env.rt().host is not None and len(env.bot.listeners["on_member_update"]) == 1
            env.rt().host.close()

        run(main())

    def test_members_intent_off_is_reported_once(self, env, caplog):
        async def main():
            env.register("default")
            env.bot.intents = S.FakeIntents(members=False)
            with caplog.at_level(logging.WARNING, logger="team-guard"):
                env.attach().close()

        run(main())
        assert sum("Server Members Intent" in r.getMessage() for r in caplog.records) == 1


# ============================================================================= integrasi (butuh Hermes)
class TestIntegrationWithRealHermes:
    """DILEWATI bila Hermes tidak terpasang. Memeriksa asumsi plugin terhadap upstream yang sebenarnya:
    resolver rute, nama hook/middleware, skema manifest, dan pemuat plugin."""

    @pytest.fixture(autouse=True)
    def _needs_hermes(self):
        pytest.importorskip("discord")                                       # harus sebelum gateway.run
        pytest.importorskip("gateway", reason="Hermes tidak terpasang — uji integrasi dijalankan di CI")

    def test_real_resolver_routes_every_roster_channel(self, env):
        real = livereload.Upstream()
        roster = env.rt().roster()
        routes = real.parse_profile_routes(roster.profile_routes)
        assert len(routes) == len(roster.profile_routes)
        assert [r.specificity for r in routes] == sorted((r.specificity for r in routes), reverse=True)
        for channel, profile, user in livereload.expected_routes(roster):
            match = real.match_profile_route(routes, guild_id=roster.guild_id, chat_id=channel, user_id=user)
            assert match is not None and match.profile == profile
        thread = real.match_profile_route(routes, guild_id=roster.guild_id, chat_id="9", thread_id="9",
                                          parent_chat_id=S.CH_BUDI, user_id=S.U_BUDI)
        assert thread.profile == "be-budi"
        assert real.match_profile_route(routes, guild_id=roster.guild_id, chat_id="12345", user_id=S.U_BUDI) is None

    def test_real_resolver_drives_live_reload_verification(self, env):
        from gateway.config import GatewayConfig
        assert "profile_routes" in {f for f in GatewayConfig.__dataclass_fields__}
        notices = []

        async def notify(text):
            notices.append(text)

        env.rt().started -= 10_000
        live = livereload.LiveReloader(env.rt(), get_runner=lambda: env.runner, get_adapter=lambda: env.adapter,
                                       notify=notify, sleep=S.fast_sleep)
        live.upstream.rescan = lambda home: None
        live.upstream.served_profiles = lambda config: None
        S.roster_add(env.roster, S.U_DODI, "dodi", "610000000000000004")
        env.save()
        ok, detail = run(live.apply("integrasi"))
        assert ok, detail

    def test_registered_names_exist_upstream(self):
        from hermes_cli.middleware import VALID_MIDDLEWARE
        from hermes_cli.plugins import VALID_HOOKS
        assert EVERYWHERE <= VALID_HOOKS and "llm_request" in VALID_MIDDLEWARE

    def test_manifest_fits_upstream_schema(self):
        import yaml
        from hermes_cli.plugins_manifest import _CONFIG_SCHEMA_TYPES, _KNOWN_MANIFEST_FIELDS, SUPPORTED_MANIFEST_VERSION
        manifest = yaml.safe_load((S.PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
        assert set(manifest) <= _KNOWN_MANIFEST_FIELDS
        assert manifest["manifest_version"] <= SUPPORTED_MANIFEST_VERSION
        assert all(spec["type"] in _CONFIG_SCHEMA_TYPES for spec in manifest["config_schema"].values())

    def test_real_loader_imports_and_registers_plugin(self, env, monkeypatch):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest
        monkeypatch.setenv("HERMES_HOME", str(env.hermes_root))
        manager = PluginManager(scope_key="team-guard-test")
        manifest = PluginManifest(name="team-guard", version="3.1.0", source="user", path=str(S.PLUGIN_DIR),
                                  key="team-guard")
        module = manager._load_directory_module(manifest)
        assert callable(module.register) and module.__name__.startswith("hermes_plugins.team_guard")
        module.register(PluginContext(manifest, manager))
        assert manager.has_hook("pre_gateway_dispatch") and manager.has_hook("transform_tool_result")
        assert manager.get_platform_handler_factories("discord")

    def test_discord_adapter_still_has_the_gate_accessors_we_patch(self):
        import importlib
        adapter = importlib.import_module("plugins.platforms.discord.adapter")
        cls = next(v for k, v in vars(adapter).items() if k == "DiscordAdapter")
        for name in ("_gate_raw", "_get_allowed_channels", "_discord_free_response_channels", "_snapshot_gate_env",
                     "_wire_plugin_handlers"):
            assert callable(getattr(cls, name, None)), name

    def test_runner_still_has_the_internals_we_guard(self):
        from gateway.control_socket import rescan_gateway_profiles
        from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
        from gateway.run import GatewayRunner, _multiplex_profile_homes
        assert GATEWAY_SERVICE_RESTART_EXIT_CODE == 75 and callable(rescan_gateway_profiles)
        assert callable(_multiplex_profile_homes)
        for name in ("_running_agent_count", "request_restart", "_hm_pre_gateway_dispatch_hook"):
            assert callable(getattr(GatewayRunner, name, None)), name
