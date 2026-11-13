"""Uji tools/probe_llm.py dan tools/sandbox_idle.py.

Tanpa jaringan luar dan tanpa Docker: endpoint LLM ditiru server HTTP pustaka standar di 127.0.0.1 yang
membalas 429 bila permintaan paralel melebihi N; perintah docker ditiru lewat fungsi `run` yang disuntikkan.
"""
import importlib.util
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

KIT = Path(__file__).resolve().parent.parent
SECRET = "sk-uji-JANGAN-BOCOR-0123456789"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"kit_tools_{name}", KIT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load("probe_llm")
idle = _load("sandbox_idle")


# ============================================================================ LLM tiruan
class FakeLLM:
    """Endpoint chat-completions tiruan: lebih dari `limit` permintaan bersamaan → 429."""

    def __init__(self, limit=4, delay=0.08, usage=True, status=None):
        self.limit, self.delay, self.usage, self.status = limit, delay, usage, status
        self.lock = threading.Lock()
        self.inflight = self.peak = self.requests = 0
        self.auth, self.paths, self.bodies = set(), set(), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with outer.lock:
                    outer.requests += 1
                    outer.inflight += 1
                    outer.peak = max(outer.peak, outer.inflight)
                    over = outer.inflight > outer.limit
                    outer.auth.add(self.headers.get("Authorization"))
                    outer.paths.add(self.path)
                    outer.bodies.append(json.loads(body))
                try:
                    if outer.status:
                        return self._send(outer.status, {"error": {"message": "ditolak"}})
                    if over:
                        return self._send(429, {"error": {"message": "rate limit"}})
                    time.sleep(outer.delay)
                    payload = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
                    if outer.usage:
                        payload["usage"] = {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14}
                    self._send(200, payload)
                finally:
                    with outer.lock:
                        outer.inflight -= 1

            def _send(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def llm():
    servers = []

    def make(**kw):
        servers.append(FakeLLM(**kw))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def run_probe(capsys, *argv, env=None):
    code = probe.main(list(argv), env={"UJI_KEY": SECRET} if env is None else env)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ============================================================================ probe_llm: logika murni
def level(parallel, ok=8, limited=0, errors=0, p95=1.0):
    total = ok + limited + errors
    return {"parallel": parallel, "requests": total, "ok": ok, "http_429": limited, "errors": errors,
            "bad_ratio": (limited + errors) / total, "p50_s": p95 / 2, "p95_s": p95 if ok else None,
            "tokens_per_s": None, "wall_s": 1.0, "details": {}}


class TestProbeLogic:
    def test_percentile_nearest_rank(self):
        values = [0.1 * i for i in range(1, 21)]
        assert probe.percentile(values, 50) == pytest.approx(1.0) and probe.percentile(values, 95) == pytest.approx(1.9)
        assert probe.percentile([3.0], 95) == 3.0 and probe.percentile([], 95) is None

    def test_recommend_picks_highest_clean_level_within_twice_baseline(self):
        levels = [level(1, p95=1.0), level(2, p95=1.2), level(4, p95=1.9), level(8, p95=2.4), level(16, limited=6)]
        advice = probe.recommend(levels, delegation_max_children=1)
        assert (advice["max_llm_parallel"], advice["max_concurrent_sessions"], advice["baseline_p95_s"]) == (4, 2, 1.0)
        assert probe.recommend(levels, 0)["max_concurrent_sessions"] == 4
        assert probe.recommend(levels, 2)["max_concurrent_sessions"] == 1
        assert probe.recommend(levels, 9)["max_concurrent_sessions"] == 1          # minimal 1

    def test_recommend_skips_levels_with_any_429_or_error(self):
        levels = [level(1), level(2), level(4, ok=15, limited=1), level(8), level(16, errors=8)]
        assert probe.recommend(levels, 1)["max_llm_parallel"] == 8                 # 4 tidak bersih; 8 bersih
        levels = [level(1), level(2, ok=7, errors=1), level(4, limited=8)]
        assert probe.recommend(levels, 1)["max_llm_parallel"] == 1

    def test_recommend_without_clean_baseline_gives_no_advice(self):
        for base in (level(1, ok=0, errors=4), level(1, ok=3, limited=1)):
            advice = probe.recommend([base, level(2)], 1)
            assert advice["max_llm_parallel"] is None and advice["max_concurrent_sessions"] is None
            assert "tingkat dasar" in advice["reason"]
        assert probe.recommend([], 1)["max_llm_parallel"] is None

    def test_recommend_notes_when_highest_tested_level_is_still_clean(self):
        assert "lebih besar" in probe.recommend([level(1), level(2)], 1)["reason"]

    def test_probe_stops_escalating_after_a_bad_level(self):
        seen = []

        def send():
            return {"status": "429" if threading.active_count() and seen and seen[-1] >= 4 else "ok",
                    "latency": 0.01, "tokens": 0, "detail": ""}

        def fake_level(_send, parallel, requests):
            seen.append(parallel)
            return level(parallel, limited=4 if parallel >= 4 else 0)

        original, probe.run_level = probe.run_level, fake_level
        try:
            results = probe.probe(send, [1, 2, 4, 8, 16])
        finally:
            probe.run_level = original
        assert seen == [1, 2, 4] and [r["parallel"] for r in results] == [1, 2, 4]

    def test_run_level_counts_and_default_request_count(self):
        outcomes = iter([{"status": "ok", "latency": 0.2, "tokens": 5, "detail": ""}] * 5
                        + [{"status": "429", "latency": 0.01, "tokens": 0, "detail": "HTTP 429"}] * 2
                        + [{"status": "error", "latency": 0.01, "tokens": 0, "detail": "URLError"}])
        lock = threading.Lock()

        def send():
            with lock:
                return next(outcomes)

        result = probe.run_level(send, 3, 8)
        assert (result["requests"], result["ok"], result["http_429"], result["errors"]) == (8, 5, 2, 1)
        assert result["bad_ratio"] == pytest.approx(3 / 8) and result["p95_s"] == 0.2
        assert result["details"] == {"HTTP 429": 2, "URLError": 1} and result["tokens_per_s"] > 0
        counts = []
        original, probe.run_level = probe.run_level, lambda s, p, r: counts.append(r) or level(p)
        try:
            probe.probe(send, [1, 2, 16])
            probe.probe(send, [1], requests_per_level=7)
        finally:
            probe.run_level = original
        assert counts == [4, 4, 32, 7]

    def test_parse_levels_and_url_validation(self):
        assert probe.parse_levels("4, 1,2,2") == [1, 2, 4]
        for bad in ("", "a,b", "0,1", "1,999"):
            with pytest.raises(probe.ProbeError):
                probe.parse_levels(bad)
        assert probe.chat_url("https://llm.example/v1/") == "https://llm.example/v1/chat/completions"
        for bad in ("llm.example/v1", "file:///etc/passwd", "ftp://x/v1"):
            with pytest.raises(probe.ProbeError):
                probe.chat_url(bad)


# ============================================================================ probe_llm: terhadap server tiruan
class TestProbeAgainstFakeEndpoint:
    def test_finds_parallel_limit_and_recommends_from_it(self, llm, capsys):
        server = llm(limit=4)
        code, out, err = run_probe(capsys, "--base-url", server.base, "--model", "uji-model", "--key-env", "UJI_KEY",
                                   "--levels", "1,2,4,8,16,32", "--max-tokens", "8", "--json")
        data = json.loads(out)
        assert code == 0 and err == ""
        levels = {lv["parallel"]: lv for lv in data["levels"]}
        assert list(levels) == [1, 2, 4, 8]                                      # berhenti setelah 8 (>10% 429)
        for parallel in (1, 2, 4):
            assert levels[parallel]["http_429"] == 0 and levels[parallel]["errors"] == 0
            assert levels[parallel]["ok"] == levels[parallel]["requests"]
            assert levels[parallel]["p95_s"] >= levels[parallel]["p50_s"] >= 0.05
            assert levels[parallel]["tokens_per_s"] > 0
        assert levels[8]["http_429"] > 0 and levels[8]["bad_ratio"] > 0.10
        assert levels[8]["details"] == {"HTTP 429": levels[8]["http_429"]}
        assert data["recommendation"]["max_llm_parallel"] == 4
        assert data["recommendation"]["max_concurrent_sessions"] == 2            # 4 ÷ (1 + 1)
        assert server.peak >= 5                                                  # paralel sungguhan, bukan berurutan
        # permintaan: jalur OpenAI, model & max_tokens sesuai argumen, key lewat header saja
        assert server.paths == {"/v1/chat/completions"} and server.auth == {f"Bearer {SECRET}"}
        assert server.bodies[0]["model"] == "uji-model" and server.bodies[0]["max_tokens"] == 8
        assert server.bodies[0]["stream"] is False and server.bodies[0]["messages"][0]["role"] == "user"

    def test_text_output_has_table_and_never_prints_the_key(self, llm, capsys):
        server = llm(limit=2)
        code, out, err = run_probe(capsys, "--base-url", server.base, "--model", "m", "--key-env", "UJI_KEY",
                                   "--levels", "1,2,4,8", "--delegation-max-children", "0")
        assert code == 0
        assert "paralel" in out and "p95 dtk" in out and "token/dtk" in out and "HTTP 429 ×" in out
        assert "Berhenti di paralel 4" in out
        assert "max_llm_parallel: 2" in out and "max_concurrent_sessions: 2" in out
        assert "env UJI_KEY" in err
        assert SECRET not in out and SECRET not in err

    def test_requests_per_level_and_missing_usage(self, llm, capsys):
        server = llm(limit=99, usage=False, delay=0.01)
        code, out, _ = run_probe(capsys, "--base-url", server.base, "--model", "m", "--levels", "1,2",
                                 "--requests-per-level", "5", "--json", env={})
        data = json.loads(out)
        assert code == 0 and [lv["requests"] for lv in data["levels"]] == [5, 5] and server.requests == 10
        assert all(lv["tokens_per_s"] is None for lv in data["levels"])
        assert server.auth == {None} and data["key_env"] == ""                   # tanpa key → tanpa header
        assert "lebih besar" in data["recommendation"]["reason"]

    def test_endpoint_that_always_fails_gives_exit_1_and_no_advice(self, llm, capsys):
        server = llm(status=401)
        code, out, err = run_probe(capsys, "--base-url", server.base, "--model", "m", "--key-env", "UJI_KEY")
        assert code == 1 and "Tidak ada saran" in out and "HTTP 401" in out
        assert server.requests == 4                                              # berhenti di tingkat pertama
        assert SECRET not in out + err

    def test_unreachable_endpoint_is_reported_as_errors(self, capsys):
        code, out, _ = run_probe(capsys, "--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--levels", "1",
                                 "--timeout", "2", "--json", env={})
        data = json.loads(out)
        assert code == 1 and data["levels"][0]["errors"] == data["levels"][0]["requests"]

    def test_argument_errors_exit_2_and_key_never_comes_from_argv(self, llm, capsys):
        server = llm()
        base = ["--base-url", server.base, "--model", "m"]
        cases = [
            (["--model", "m"], {}),                                              # tanpa URL
            (["--base-url", server.base], {}),                                   # tanpa model
            ([*base, "--key-env", "UJI_KEY"], {}),                               # env kosong
            ([*base, "--key-env", SECRET], {}),                                  # menaruh key di --key-env
            ([*base, "--levels", "0"], {}),
            ([*base, "--max-tokens", "0"], {}),
            ([*base, "--alias", "td-simple"], {}),                               # --alias tanpa --team
            (["--base-url", "bukan-url", "--model", "m"], {}),
        ]
        for argv, env in cases:
            code, out, err = run_probe(capsys, *argv, env=env)
            assert code == 2 and out == "" and err.startswith("probe_llm:"), argv
            assert SECRET not in err
        assert server.requests == 0
        assert not any("--key" == a.option_strings[0] or "--api-key" in a.option_strings
                       for a in probe.build_parser()._actions if a.option_strings)

    def test_reads_target_from_team_yaml(self, llm, capsys, tmp_path):
        server = llm(limit=2, delay=0.05)
        raw = yaml.safe_load((KIT / "team.example.yaml").read_text(encoding="utf-8"))
        raw["llm"]["providers"]["td"] = {"api": server.base, "key_env": "UJI_KEY"}
        raw["llm"]["models"]["td-normal"]["model"] = "model-besar"
        raw["scale"]["delegation_max_children"] = 0
        team_file = tmp_path / "team.yaml"
        team_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
        (tmp_path / "members.yaml").write_text("members: []\n", encoding="utf-8")
        code, out, _ = run_probe(capsys, "--team", str(team_file), "--alias", "td-normal", "--levels", "1,2", "--json")
        data = json.loads(out)
        assert code == 0 and data["model"] == "model-besar" and data["key_env"] == "UJI_KEY"
        assert server.auth == {f"Bearer {SECRET}"} and server.bodies[0]["model"] == "model-besar"
        assert data["recommendation"]["delegation_max_children"] == 0
        assert data["recommendation"]["max_concurrent_sessions"] == data["recommendation"]["max_llm_parallel"] == 2
        # alias bawaan = llm.default_model; alias tak dikenal dan team.yaml rusak → kode 2
        code, out, _ = run_probe(capsys, "--team", str(team_file), "--levels", "1", "--json")
        assert code == 0 and json.loads(out)["model"] == "td-simple"
        assert run_probe(capsys, "--team", str(team_file), "--alias", "tidak-ada")[0] == 2
        assert run_probe(capsys, "--team", str(tmp_path / "hilang.yaml"))[0] == 2

    def test_is_stdlib_only(self):
        source = (KIT / "tools" / "probe_llm.py").read_text(encoding="utf-8")
        top_level = [line.split()[1].split(".")[0] for line in source.splitlines()
                     if line.startswith(("import ", "from ")) and not line.startswith("from __future__")]
        assert set(top_level) <= set(sys.stdlib_module_names)


# ============================================================================ sandbox_idle
PS = "aaa\tbe-andi\thermes-be-andi\nbbb\tbe-budi\thermes-be-budi\nccc\tteam-admin\thermes-team-admin\n"
TOP_QUIET = "PID                 TIME\n101                 00:00:03\n102                 00:00:00\n"
TOP_BUSY = "PID                 TIME\n101                 00:00:03\n102                 00:00:09\n"
HOUR = 3600.0


CPU_STAT = "usage_usec {usec}\nuser_usec 1\nsystem_usec 2\nnr_periods 0\n"


class FakeDocker:
    """`cpu`: id → usage_usec kumulatif cgroup (bawaan 5 detik, tidak berubah); None = tidak terbaca.
    `v1=True` meniru cgroup v1 (cpu.stat tanpa usage_usec; cpuacct.usage dalam nanodetik)."""

    def __init__(self, ps=PS, tops=None, ps_code=0, stop_code=0, cpu=None, v1=False):
        self.ps, self.tops, self.ps_code, self.stop_code = ps, tops or {}, ps_code, stop_code
        self.cpu, self.v1 = cpu or {}, v1
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if cmd[:2] == ["docker", "ps"]:
            return self.ps_code, self.ps if self.ps_code == 0 else "Cannot connect to the Docker daemon"
        if cmd[:2] == ["docker", "top"]:
            out = self.tops.get(cmd[2], TOP_QUIET)
            return (1, "container tidak berjalan") if out is None else (0, out)
        if cmd[:2] == ["docker", "exec"]:
            usec = self.cpu.get(cmd[2], 5_000_000)
            if usec is None or cmd[3] != "cat":
                return 1, "OCI runtime exec failed"
            if cmd[4] == "/sys/fs/cgroup/cpu.stat":
                return (1, "cat: No such file") if self.v1 else (0, CPU_STAT.format(usec=usec))
            if cmd[4] == "/sys/fs/cgroup/cpuacct/cpuacct.usage" and self.v1:
                return 0, f"{usec * 1000}\n"
            return 1, "cat: No such file"
        if cmd[:2] == ["docker", "stop"]:
            return self.stop_code, "" if self.stop_code == 0 else "Error: permission denied"
        return 1, "perintah tak terduga"

    def stopped(self):
        return [c[2] for c in self.calls if c[:2] == ["docker", "stop"]]


def sweep(docker, state_file, now, **kw):
    lines = []
    kw.setdefault("idle_hours", 6)
    kw.setdefault("apply", True)
    kw.setdefault("read_file", lambda _path: None)                           # cgroup host tidak terbaca → docker exec
    code = idle.sweep(docker, state_file, now=now, out=lines.append, **kw)
    return code, "\n".join(lines)


class TestSandboxIdleSelection:
    def containers(self, cpu=7, source="host", **fingerprints):
        return [{"id": cid, "profile": f"be-{cid}", "name": cid, "fingerprint": fp, "cpu_usec": cpu,
                 "cpu_source": source} for cid, fp in fingerprints.items()]

    @staticmethod
    def seen(fingerprint="f1", since=0.0, cpu=7, profile="be-a"):
        return {"fingerprint": fingerprint, "cpu_usec": cpu, "since": since, "profile": profile}

    def test_first_sighting_is_never_idle(self):
        chosen, state = idle.select_idle(self.containers(a="f1"), {}, now=1000.0, idle_seconds=6 * HOUR)
        assert chosen == [] and state == {"a": self.seen(since=1000.0)}

    def test_unchanged_fingerprint_becomes_idle_after_threshold(self):
        state = {"a": self.seen()}
        chosen, new = idle.select_idle(self.containers(a="f1"), state, now=6 * HOUR - 1, idle_seconds=6 * HOUR)
        assert chosen == [] and new["a"]["since"] == 0.0                         # belum cukup lama; jam tidak diulang
        chosen, new = idle.select_idle(self.containers(a="f1"), state, now=6 * HOUR, idle_seconds=6 * HOUR)
        assert [c["id"] for c in chosen] == ["a"] and chosen[0]["idle_seconds"] == 6 * HOUR

    def test_any_cpu_change_restarts_the_clock(self):
        state = {"a": self.seen()}
        chosen, new = idle.select_idle(self.containers(a="f2"), state, now=100 * HOUR, idle_seconds=6 * HOUR)
        assert chosen == [] and new["a"] == self.seen("f2", 100 * HOUR)

    def test_cgroup_cpu_catches_use_that_docker_top_misses(self):
        """Regresi: (pid, TIME berpresisi 1 detik) yang dicuplik tiap 30 menit bisa SAMA walau container dipakai
        (perintah singkat lahir dan mati di antara dua cuplikan) → container yang dipakai dianggap menganggur."""
        state = {"a": self.seen(cpu=5_000_000)}
        busy = self.containers(cpu=5_000_001, a="f1")                             # sidik jari sama, CPU +1 µs
        chosen, new = idle.select_idle(busy, state, now=100 * HOUR, idle_seconds=6 * HOUR)
        assert chosen == [] and new["a"]["since"] == 100 * HOUR and new["a"]["cpu_usec"] == 5_000_001
        chosen, _ = idle.select_idle(self.containers(cpu=5_000_000, a="f1"), state, now=100 * HOUR,
                                     idle_seconds=6 * HOUR)
        assert [c["id"] for c in chosen] == ["a"]                                # benar-benar diam

    def test_exec_sampling_noise_is_tolerated_but_real_use_is_not(self):
        state = {"a": self.seen(cpu=5_000_000)}
        noisy = self.containers(cpu=5_000_000 + 200_000, source="exec", a="f1")  # 200 md: biaya `docker exec cat`
        chosen, new = idle.select_idle(noisy, state, now=7 * HOUR, idle_seconds=6 * HOUR)
        assert [c["id"] for c in chosen] == ["a"] and new["a"]["cpu_usec"] == 5_200_000
        used = self.containers(cpu=5_000_000 + 400_000, source="exec", a="f1")
        assert idle.select_idle(used, state, now=7 * HOUR, idle_seconds=6 * HOUR)[0] == []
        assert idle.select_idle(noisy, state, now=7 * HOUR, idle_seconds=6 * HOUR, exec_noise_usec=0)[0] == []
        on_host = self.containers(cpu=5_000_000 + 200_000, source="host", a="f1")  # dari host: tanpa toleransi
        assert idle.select_idle(on_host, state, now=7 * HOUR, idle_seconds=6 * HOUR)[0] == []

    def test_cpu_counter_going_backwards_means_restarted(self):
        state = {"a": self.seen(cpu=5_000_000)}
        chosen, new = idle.select_idle(self.containers(cpu=10, a="f1"), state, now=9 * HOUR, idle_seconds=6 * HOUR)
        assert chosen == [] and new["a"]["since"] == 9 * HOUR

    def test_state_from_before_cgroup_tracking_restarts_the_clock(self):
        old = {"a": {"fingerprint": "f1", "since": 0.0, "profile": "be-a"}}       # catatan versi lama
        chosen, new = idle.select_idle(self.containers(a="f1"), old, now=50 * HOUR, idle_seconds=6 * HOUR)
        assert chosen == [] and new["a"]["since"] == 50 * HOUR

    def test_recent_state_db_activity_keeps_container_alive(self):
        """Regresi: profile yang baru saja bercakap (state.db berubah) tidak boleh dihentikan walau CPU diam."""
        state = {"a": self.seen()}
        quiet = self.containers(a="f1")
        quiet[0]["activity"] = 10 * HOUR - 600                                   # 10 menit lalu
        chosen, new = idle.select_idle(quiet, state, now=10 * HOUR, idle_seconds=6 * HOUR)
        assert chosen == [] and new["a"]["since"] == 0.0                         # jam CPU tetap; hanya tidak dipilih
        quiet[0]["activity"] = 10 * HOUR - 7 * HOUR                              # terakhir 7 jam lalu
        chosen, _ = idle.select_idle(quiet, state, now=10 * HOUR, idle_seconds=6 * HOUR)
        assert [c["id"] for c in chosen] == ["a"] and chosen[0]["idle_seconds"] == 7 * HOUR
        quiet[0]["activity"] = None                                              # state.db belum ada → hanya CPU
        assert len(idle.select_idle(quiet, state, now=10 * HOUR, idle_seconds=6 * HOUR)[0]) == 1
        quiet[0]["activity_known"] = False                                       # tidak bisa diperiksa → aktif
        assert idle.select_idle(quiet, state, now=10 * HOUR, idle_seconds=6 * HOUR)[0] == []

    def test_unreadable_cpu_is_treated_as_active_and_forgotten(self):
        state = {"a": self.seen()}
        chosen, new = idle.select_idle(self.containers(a=None), state, now=100 * HOUR, idle_seconds=6 * HOUR)
        assert chosen == [] and new == {}
        chosen, new = idle.select_idle(self.containers(cpu=None, a="f1"), state, now=100 * HOUR,
                                       idle_seconds=6 * HOUR)                    # cgroup tidak terbaca
        assert chosen == [] and new == {}

    def test_excluded_profiles_and_vanished_containers(self):
        state = {"a": self.seen(), "gone": self.seen("x", profile="be-gone")}
        chosen, new = idle.select_idle(self.containers(a="f1", b="f9"), state, now=10 * HOUR, idle_seconds=6 * HOUR,
                                       exclude=frozenset({"be-a"}))
        assert chosen == [] and set(new) == {"a", "b"} and new["a"]["since"] == 0.0

    def test_corrupt_state_entries_are_ignored(self):
        for bad in ("x", {"fingerprint": "f1"}, {"fingerprint": "f1", "since": "kemarin", "cpu_usec": 7},
                    {"fingerprint": "f1", "since": 9e18, "cpu_usec": 7},
                    {"fingerprint": "f1", "since": 0.0, "cpu_usec": "7"}, {"fingerprint": "f1", "since": 0.0,
                                                                           "cpu_usec": True}):
            chosen, new = idle.select_idle(self.containers(a="f1"), {"a": bad}, now=50 * HOUR, idle_seconds=HOUR)
            assert chosen == [] and new["a"]["since"] == 50 * HOUR

    def test_parse_cpu_usage(self):
        assert idle.parse_cpu_usage(CPU_STAT.format(usec=123456)) == 123456
        assert idle.parse_cpu_usage("123456789000\n", 1000) == 123456789           # cgroup v1: nanodetik
        for bad in ("", None, "user_usec 5\n", "usage_usec banyak\n", "Error response from daemon"):
            assert idle.parse_cpu_usage(bad) is None
        assert idle.parse_cpu_usage("bukan angka", 1000) is None

    def test_cpu_usage_prefers_host_cgroup_then_docker_exec(self):
        cid = "a" * 64
        host = {f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat": CPU_STAT.format(usec=42)}
        docker = FakeDocker()
        assert idle.cpu_usage(docker, cid, host.get) == (42, "host") and docker.calls == []   # tanpa efek samping
        v1 = {f"/sys/fs/cgroup/cpu,cpuacct/docker/{cid}/cpuacct.usage": "9000000\n"}
        assert idle.cpu_usage(docker, cid, v1.get) == (9000, "host")
        docker = FakeDocker(cpu={cid: 77})
        assert idle.cpu_usage(docker, cid, lambda _p: None) == (77, "exec")
        assert docker.calls == [["docker", "exec", cid, "cat", "/sys/fs/cgroup/cpu.stat"]]     # hanya `cat`
        assert idle.cpu_usage(FakeDocker(cpu={cid: 77}, v1=True), cid, lambda _p: None) == (77, "exec")
        assert idle.cpu_usage(FakeDocker(cpu={cid: None}), cid, lambda _p: None) == (None, "")

    def test_profile_activity_reads_state_db_mtime(self, tmp_path):
        home = tmp_path / "profiles" / "be-andi"
        home.mkdir(parents=True)
        assert idle.profile_activity(tmp_path, "be-andi") == (None, True)        # belum ada state.db
        (home / "state.db").write_text("x")
        os.utime(home / "state.db", (1000, 1000))
        assert idle.profile_activity(tmp_path, "be-andi") == (1000.0, True)
        (home / "state.db-wal").write_text("x")
        os.utime(home / "state.db-wal", (2000, 2000))                            # WAL lebih baru → itu yang dipakai
        assert idle.profile_activity(tmp_path, "be-andi") == (2000.0, True)
        (tmp_path / "state.db").write_text("x")
        os.utime(tmp_path / "state.db", (3000, 3000))
        assert idle.profile_activity(tmp_path, "default") == (3000.0, True)      # profile default = root
        assert idle.profile_activity(None, "be-andi") == (None, True)            # pemeriksaan dimatikan
        for odd in ("../x", "a/b", "..", ""):
            assert idle.profile_activity(tmp_path, odd) == (None, False)         # label aneh → jangan menebak

        def denied(_path):
            raise PermissionError("Permission denied")

        assert idle.profile_activity(tmp_path, "be-andi", denied) == (None, False)

    def test_cpu_fingerprint_changes_with_time_or_process_set(self):
        quiet = idle.cpu_fingerprint(TOP_QUIET)
        assert quiet and quiet == idle.cpu_fingerprint(TOP_QUIET.replace("   ", " "))    # spasi tidak berpengaruh
        assert idle.cpu_fingerprint(TOP_BUSY) != quiet
        assert idle.cpu_fingerprint(TOP_QUIET + "103   00:00:00\n") != quiet
        assert idle.cpu_fingerprint("") is None and idle.cpu_fingerprint("PID TIME\n") is None
        assert idle.cpu_fingerprint("Error response from daemon") is None

    def test_parse_ps_skips_unlabelled_rows(self):
        assert idle.parse_ps(PS + "\nddd\t\tx\n")[0] == {"id": "aaa", "profile": "be-andi", "name": "hermes-be-andi"}
        assert len(idle.parse_ps(PS + "\nddd\t\tx\n")) == 3


class TestSandboxIdleSweep:
    def test_stops_only_containers_idle_long_enough(self, tmp_path):
        state_file = tmp_path / "state" / "sandbox_idle.json"
        docker = FakeDocker()
        code, text = sweep(docker, state_file, now=0.0)
        assert code == 0 and docker.stopped() == [] and "0 menganggur" in text    # pengamatan pertama
        assert set(json.loads(state_file.read_text())) == {"aaa", "bbb", "ccc"}
        assert (state_file.stat().st_mode & 0o777) == 0o600
        docker = FakeDocker(tops={"bbb": TOP_BUSY})                              # budi memakai CPU
        code, text = sweep(docker, state_file, now=7 * HOUR, exclude=frozenset({"team-admin"}))
        assert code == 0 and docker.stopped() == ["aaa"]
        assert "be-andi (hermes-be-andi): diam 7.0 jam → dihentikan" in text
        assert "team-admin (hermes-team-admin): dikecualikan" in text and "be-budi" in text
        assert "aaa" not in json.loads(state_file.read_text())
        # hanya `stop`: tidak pernah rm/kill/prune, dan `ps` hanya untuk yang berjalan & berlabel
        verbs = {c[1] for c in docker.calls}
        assert verbs == {"ps", "top", "exec", "stop"}
        assert all(c[3] == "cat" and c[4].startswith("/sys/fs/cgroup/") for c in docker.calls if c[1] == "exec")
        ps = docker.calls[0]
        assert "label=hermes-profile" in ps and "status=running" in ps

    def test_container_used_between_samples_is_not_stopped(self, tmp_path):
        """Regresi ujung-ke-ujung: `docker top` identik di dua jalan, tetapi cgroup mencatat CPU terpakai."""
        state_file = tmp_path / "sandbox_idle.json"
        sweep(FakeDocker(cpu={"aaa": 5_000_000}), state_file, now=0.0)
        docker = FakeDocker(cpu={"aaa": 5_900_000})                              # +0,9 dtk CPU; TIME (1 dtk) sama
        code, text = sweep(docker, state_file, now=7 * HOUR)
        assert code == 0 and "aaa" not in docker.stopped() and set(docker.stopped()) == {"bbb", "ccc"}
        assert "be-andi (hermes-be-andi): aktif atau belum cukup lama diam (0.0 jam)" in text
        assert json.loads(state_file.read_text())["aaa"] == {
            "fingerprint": idle.cpu_fingerprint(TOP_QUIET), "cpu_usec": 5_900_000, "since": 7 * HOUR,
            "profile": "be-andi"}

    def test_profile_with_recent_conversation_is_not_stopped(self, tmp_path):
        state_file = tmp_path / "sandbox_idle.json"
        root = tmp_path / "hermes"
        for profile, age in (("be-andi", 20 * 60), ("be-budi", 9 * HOUR)):
            home = root / "profiles" / profile
            home.mkdir(parents=True)
            (home / "state.db").write_text("x")
            os.utime(home / "state.db", (8 * HOUR - age, 8 * HOUR - age))
        sweep(FakeDocker(), state_file, now=0.0, hermes_root=root)
        docker = FakeDocker()
        code, text = sweep(docker, state_file, now=8 * HOUR, hermes_root=root)
        assert code == 0 and docker.stopped() == ["bbb", "ccc"]                  # andi bercakap 20 menit lalu
        assert "be-andi (hermes-be-andi): profile dipakai 0.3 jam lalu (state.db) → aktif" in text
        assert "be-budi (hermes-be-budi): diam 8.0 jam → dihentikan" in text     # min(CPU 8 jam, state.db 9 jam)

    def test_unreadable_cgroup_or_state_db_leaves_container_alone(self, tmp_path):
        state_file = tmp_path / "sandbox_idle.json"
        sweep(FakeDocker(), state_file, now=0.0)
        docker = FakeDocker(cpu={"aaa": None})
        code, text = sweep(docker, state_file, now=9 * HOUR)
        assert "aaa" not in docker.stopped() and "be-andi (hermes-be-andi): pemakaian CPU" in text

        def denied(path):
            if "be-budi" in str(path):
                raise PermissionError("Permission denied")
            return None

        sweep(FakeDocker(), state_file, now=0.0, hermes_root=tmp_path, mtime=denied)
        docker = FakeDocker()
        code, text = sweep(docker, state_file, now=9 * HOUR, hermes_root=tmp_path, mtime=denied)
        assert "bbb" not in docker.stopped() and "state.db profile tidak bisa diperiksa" in text

    def test_host_cgroup_is_used_without_touching_the_container(self, tmp_path):
        state_file = tmp_path / "sandbox_idle.json"
        usage = {"aaa": 100, "bbb": 100, "ccc": 100}

        def read(path):
            for cid, usec in usage.items():
                if path == f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat":
                    return CPU_STAT.format(usec=usec)
            return None

        sweep(FakeDocker(), state_file, now=0.0, read_file=read)
        usage["bbb"] = 101                                                       # 1 µs saja → aktif
        docker = FakeDocker()
        sweep(docker, state_file, now=7 * HOUR, read_file=read)
        assert docker.stopped() == ["aaa", "ccc"] and not any(c[1] == "exec" for c in docker.calls)

    def test_dry_run_reports_but_never_stops(self, tmp_path):
        state_file = tmp_path / "sandbox_idle.json"
        sweep(FakeDocker(), state_file, now=0.0, apply=False)
        docker = FakeDocker()
        code, text = sweep(docker, state_file, now=8 * HOUR, apply=False)
        assert code == 0 and docker.stopped() == []
        assert "AKAN dihentikan (dry-run)" in text and "pakai --apply" in text
        assert json.loads(state_file.read_text())["aaa"]["since"] == 0.0         # pengamatan tetap berlanjut

    def test_docker_unavailable_and_stop_failure(self, tmp_path):
        state_file = tmp_path / "sandbox_idle.json"
        code, text = sweep(FakeDocker(ps_code=1), state_file, now=0.0)
        assert code == 1 and "docker tidak bisa dijalankan" in text and not state_file.exists()
        sweep(FakeDocker(), state_file, now=0.0)
        docker = FakeDocker(stop_code=1)
        code, text = sweep(docker, state_file, now=7 * HOUR)
        assert code == 1 and "docker stop gagal: Error: permission denied" in text
        assert "aaa" in json.loads(state_file.read_text())                       # tetap diamati; dicoba lagi nanti

    def test_container_whose_top_fails_is_left_alone(self, tmp_path):
        state_file = tmp_path / "sandbox_idle.json"
        sweep(FakeDocker(), state_file, now=0.0)
        docker = FakeDocker(tops={"aaa": None})
        code, text = sweep(docker, state_file, now=9 * HOUR)
        assert "aaa" not in docker.stopped() and "tidak terbaca → dibiarkan" in text

    def test_main_defaults_to_dry_run_and_needs_apply(self, tmp_path, capsys):
        state_file = tmp_path / "sandbox_idle.json"
        state_file.write_text(json.dumps({cid: {"fingerprint": idle.cpu_fingerprint(TOP_QUIET), "since": 0.0,
                                                "cpu_usec": 5_000_000, "profile": "x"}
                                          for cid in ("aaa", "bbb", "ccc")}))
        root = tmp_path / "hermes"                                               # tanpa state.db → hanya CPU
        base = ["--state-file", str(state_file), "--hermes-root", str(root)]
        docker = FakeDocker()
        assert idle.main(base, run=docker) == 0
        assert docker.stopped() == [] and "dry-run" in capsys.readouterr().out
        docker = FakeDocker()
        assert idle.main(base + ["--dry-run", "--idle-hours", "1"], run=docker) == 0
        assert docker.stopped() == []
        home = root / "profiles" / "be-budi"
        home.mkdir(parents=True)
        (home / "state.db").write_text("x")                                      # budi baru saja bercakap
        docker = FakeDocker()
        assert idle.main(base + ["--apply", "--exclude", "team-admin"], run=docker) == 0
        assert docker.stopped() == ["aaa"]
        assert idle.main(base + ["--cpu-noise-ms", "-1"], run=docker) == 2
        assert idle.main(["--idle-hours", "0"], run=docker) == 2
        with pytest.raises(SystemExit):
            idle.main(["--apply", "--dry-run"], run=docker)

    def test_missing_docker_binary_is_reported_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(idle.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("docker")))
        code, out = idle.run_command(["docker", "ps"])
        assert code == 127 and "FileNotFoundError" in out
        lines = []
        assert idle.sweep(idle.run_command, tmp_path / "s.json", idle_hours=6, apply=True, out=lines.append) == 1


class TestSandboxIdlePackaging:
    def test_header_states_restart_behaviour_is_unverified(self):
        head = (KIT / "tools" / "sandbox_idle.py").read_text(encoding="utf-8").split('"""')[1]
        assert "BELUM DIVERIFIKASI" in head and "STAGING" in head.upper() and "--apply" in head
        assert "TIDAK PERNAH `docker rm`" in head

    def test_never_removes_containers(self):
        source = (KIT / "tools" / "sandbox_idle.py").read_text(encoding="utf-8")
        code = source.split('"""', 2)[2]
        for verb in ('"rm"', '"kill"', '"prune"', '"rmi"', "shell=True"):
            assert verb not in code

    def test_systemd_units(self):
        service = (KIT / "systemd" / "hermes-sandbox-idle.service").read_text(encoding="utf-8")
        timer = (KIT / "systemd" / "hermes-sandbox-idle.timer").read_text(encoding="utf-8")
        exec_line = next(line for line in service.splitlines() if line.startswith("ExecStart="))
        assert "tools/sandbox_idle.py" in exec_line and "--apply" not in exec_line     # bawaan: hanya melapor
        assert "BELUM" in service and "staging" in service
        assert "OnCalendar=" in timer and "WantedBy=timers.target" in timer

    def test_is_stdlib_only(self):
        source = (KIT / "tools" / "sandbox_idle.py").read_text(encoding="utf-8")
        top_level = [line.split()[1].split(".")[0] for line in source.splitlines()
                     if line.startswith(("import ", "from ")) and not line.startswith("from __future__")]
        assert set(top_level) <= set(sys.stdlib_module_names)
