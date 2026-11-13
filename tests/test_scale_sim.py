"""Simulasi skala 20 orang (docs/RENCANA-SKALA.md #12, bagian simulasi) + uji LLM tiruan.

Menjalankan kelas plugin yang sebenarnya dengan jam tiruan (lihat tests/loadtest/sim.py); tidak ada Hermes,
Discord, maupun jaringan luar. `TestFakeLlm` membuka server HTTP di 127.0.0.1 (port acak).

Jalankan:  python -m pytest tests/test_scale_sim.py -q
Ringkasan angka (untuk menyetel skenario):  SIM_SUMMARY=1 python -m pytest tests/test_scale_sim.py -q -s
"""
from __future__ import annotations

import json
import os
import pprint
import statistics
import threading
import time
import urllib.error
import urllib.request

import pytest

import plugin_stubs as S
from loadtest import fake_llm, sim

P = S.load_plugin()
shared = S.sub(P, "shared")

METRIC_KEYS = {"schema", "ts", "active_sessions", "active_by_user", "llm_inflight", "llm_inflight_peak_5m", "queue_len",
               "queue_wait_p95_s", "refused", "http_429_last_minute", "effective_max_active",
               "configured_max_active", "throttled", "budget"}


def simulate(tmp_path, **overrides):
    shared.reset()
    try:
        return sim.run_simulation(S.Env(tmp_path, P), sim.Config(**overrides))
    finally:
        shared.reset()


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    out = simulate(tmp_path_factory.mktemp("sim"))
    if os.environ.get("SIM_SUMMARY"):
        pprint.pprint(sim.summary(out), width=140)
    return out


def waits(messages):
    return [m.wait for m in messages if m.admitted is not None and not m.followup]


class TestSimulation:
    """Skenario: 20 anggota selama 45 menit virtual; pembanjir membuka 6 thread tiap 4 menit; cron 3 panggilan
    LLM per menit; provider menjawab 429 di atas 11 paralel, dan hanya sanggup 2 paralel pada menit 15–17."""

    def test_scenario_is_busy_enough_to_mean_something(self, result):
        assert len(result.messages) > 500 and result.peak_sessions == result.cfg.max_active
        assert sum(1 for w in waits(result.of("normal")) if w > 0) > 50      # antrean sungguh terpakai
        assert len(result.of("flooder")) >= 60 and len(result.http_429) >= 3

    def test_no_invariant_was_violated(self, result):
        """Dicek di tiap penerimaan dan tiap 5 detik: sesi per orang, sesi anggota ≤ batas efektif, gerbang
        panggilan LLM, dan pelacak plugin (sesi, panggilan LLM) sama dengan keadaan sebenarnya."""
        assert result.violations == []

    def test_no_user_exceeds_user_max_concurrent(self, result):
        limit = result.cfg.user_max_concurrent
        assert max(result.peak_user.values()) <= limit
        assert result.peak_user[result.uids["flooder"]] == limit             # batasnya memang tercapai
        assert result.final["refused"]["rate"] == 0

    def test_active_sessions_never_exceed_the_cap(self, result):
        assert result.peak_sessions <= result.cfg.max_active
        assert all(1 <= cap <= result.cfg.max_active for _t, cap in result.cap_samples)

    def test_flooder_does_not_starve_normal_users(self, result):
        start, end, _parallel = result.cfg.degraded
        calm = [m for m in result.of("normal") if not m.followup and (m.sent < start - 60 or m.sent > end + 600)]
        assert len(calm) > 200
        assert all(m.admitted is not None for m in calm)                     # tak satu pun ditolak
        assert max(m.wait for m in calm) <= 90                               # tunggu terbatas (terukur: 49 dtk)
        calm_flood = [m for m in result.of("flooder") if m.sent < start - 60 or m.sent > end + 600]
        assert statistics.mean(waits(calm_flood)) > 2 * statistics.mean(waits(calm))   # pembanjir yang menunggu
        assert waits(result.of("admin")) and max(waits(result.of("admin"))) == 0       # admin tak pernah antre

    def test_throttle_engages_and_recovers(self, result):
        start, end, _parallel = result.cfg.degraded
        configured = result.cfg.max_active
        assert all(cap == configured for t, cap in result.cap_samples if t < start)
        during = [cap for t, cap in result.cap_samples if start + 30 <= t <= end + 120]
        assert during and max(during) <= configured // 2 and min(during) >= 1
        assert result.final["effective"] == configured and result.final["throttled"] is False
        kinds = ["turun" if "diturunkan" in text else "pulih" for _t, text in result.admin_notices
                 if "diturunkan" in text or "kembali ke" in text]
        assert kinds == ["turun", "pulih"]                                   # sekali per kejadian
        down, up = (t for t, text in result.admin_notices if "diturunkan" in text or "kembali ke" in text)
        assert start <= down <= start + 60 and up >= end + 120
        caps_after = [cap for t, cap in result.cap_samples if t >= up]
        assert caps_after and set(caps_after) == {configured}

    def test_429_only_while_provider_is_degraded_and_no_turn_fails(self, result):
        start, end, _parallel = result.cfg.degraded
        assert all(start <= t <= end + 30 for t in result.http_429)
        assert result.peak_llm <= result.cfg.provider_parallel and result.failed_calls == 0

    def test_budget_stops_exactly_the_over_budget_user(self, result):
        hog = result.uids["hog"]
        refused = [m for m in result.messages if "anggaran token" in m.reason]
        assert refused and {m.uid for m in refused} == {hog}
        assert result.final["refused"]["budget"] == len(refused)
        crossed = result.hog_crossed
        assert crossed is not None
        # Paling lambat 120 dtk (kolektor) + 60 dtk (cache) setelah jatah terlampaui, pesan baru ditolak.
        late = [m for m in result.of("hog") if m.sent > crossed + 185 and not m.followup]
        assert late and all("anggaran token" in m.reason for m in late)
        others = [m for m in result.messages if m.uid != hog and m.role != "admin" and m.sent > crossed + 185]
        assert sum(1 for m in others if m.admitted is not None) > 100         # yang lain tetap dilayani
        sent = result_adapter(result)
        assert sum("Jatah token harian" in m["content"] and m["chat_id"] == S.CH_ADMIN for m in sent) == 1
        assert any("jatah token harianmu sudah habis" in m["content"] for m in sent)

    def test_collector_runs_at_most_every_two_minutes(self, result):
        gaps = [b - a for a, b in zip(result.collects, result.collects[1:])]
        assert len(result.collects) >= 10 and min(gaps) >= 120

    def test_everything_drains_and_metrics_are_published(self, result):
        final = result.final
        assert (final["pending_tasks"], final["queue_len"], final["active"], final["llm_inflight"],
                final["provider_inflight"], final["running_agents"]) == (0, 0, 0, 0, 0, 0)
        m = result.metrics
        assert set(m) == METRIC_KEYS and m["configured_max_active"] == m["effective_max_active"] == 8
        assert m["refused"] == final["refused"] and m["queue_len"] == 0 and m["throttled"] is False
        assert m["budget"]["day"] == sim.DAY and m["budget"]["used_by_user"][result.uids["hog"]] >= 1_500_000
        assert m["budget"]["team_used"] == sum(m["budget"]["used_by_user"].values())

    def test_queue_alerts_reach_admin_once_per_episode(self, result):
        texts = [text for _t, text in result.admin_notices]
        assert sum("ditolak karena antrean penuh" in t for t in texts) >= 1
        assert sum("Antrean Hermes berisi" in t for t in texts) >= 1
        assert len(texts) <= 8                                               # tidak membanjiri #hermes-admin

    def test_simulation_is_deterministic(self, result, tmp_path):
        again = simulate(tmp_path)
        assert sim.summary(again) == sim.summary(result)
        assert [(m.uid, m.sent, m.admitted, m.reason) for m in again.messages] == \
               [(m.uid, m.sent, m.admitted, m.reason) for m in result.messages]


def result_adapter(result):
    return result.adapter_sent


class TestSimulationControls:
    """Pembanding: tanpa fitur terkait, invarian yang sama memang dilanggar (uji di atas tidak kosong)."""

    def test_without_per_user_cap_the_flooder_takes_most_slots(self, tmp_path):
        out = simulate(tmp_path, user_max_concurrent=0, duration=900.0, degraded=(1e9, 1e9, 2))
        assert out.peak_user[out.uids["flooder"]] >= 5

    def test_without_adaptive_throttle_the_cap_never_moves(self, tmp_path):
        out = simulate(tmp_path, adaptive_throttle=False, duration=1500.0)
        assert {cap for _t, cap in out.cap_samples} == {8}
        assert not any("diturunkan" in text for _t, text in out.admin_notices)
        assert len(out.http_429) > 0

    def test_without_budget_nobody_is_refused_for_tokens(self, tmp_path):
        out = simulate(tmp_path, daily_token_budget=0, duration=900.0, degraded=(1e9, 1e9, 2))
        assert out.final["refused"]["budget"] == 0 and out.collects == []
        assert all(m.admitted is not None for m in out.of("hog"))

    def test_other_seeds_keep_the_invariants(self, tmp_path):
        for seed in (1, 2, 3):
            out = simulate(tmp_path / str(seed), seed=seed, duration=1800.0)
            assert out.violations == [] and max(out.peak_user.values()) <= 2 and out.peak_sessions <= 8
            assert out.final["effective"] == 8 or out.final["throttled"]
            assert {m.uid for m in out.messages if "anggaran token" in m.reason} <= {out.uids["hog"]}


# ============================================================================= LLM tiruan (HTTP sungguhan)
def post(url, payload, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer uji"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


class TestFakeLlm:
    @pytest.fixture
    def server(self):
        srv = fake_llm.FakeLLM(port=0, latency=0.0, max_parallel=2, completion_tokens=7).start()
        yield srv
        srv.stop()

    def test_models_endpoint(self, server):
        status, body = get(server.url + "/v1/models")
        assert status == 200 and body["object"] == "list"
        assert {m["id"] for m in body["data"]} == {"td-normal", "td-simple"}

    def test_chat_completion_reports_usage(self, server):
        status, _headers, raw = post(server.url + "/v1/chat/completions", {
            "model": "td-simple", "messages": [{"role": "user", "content": "halo apa kabar hari ini"}]})
        body = json.loads(raw)
        assert status == 200 and body["object"] == "chat.completion" and body["model"] == "td-simple"
        assert body["choices"][0]["message"]["role"] == "assistant" and body["choices"][0]["finish_reason"] == "stop"
        usage = body["usage"]
        assert usage["completion_tokens"] == 7 and usage["prompt_tokens"] >= 1
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
        # Server mencatat token SETELAH jawaban terkirim, jadi klien bisa lebih dulu sampai di sini.
        deadline = time.time() + 5
        while server.stats()["tokens"] != usage["total_tokens"] and time.time() < deadline:
            time.sleep(0.01)
        assert server.stats()["requests"] == 1 and server.stats()["tokens"] == usage["total_tokens"]

    def test_streaming_ends_with_usage_and_done(self, server):
        status, headers, raw = post(server.url + "/v1/chat/completions", {
            "model": "td-normal", "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "halo"}]})
        assert status == 200 and headers["Content-Type"].startswith("text/event-stream")
        lines = [ln[6:] for ln in raw.decode().splitlines() if ln.startswith("data: ")]
        assert lines[-1] == "[DONE]"
        chunks = [json.loads(ln) for ln in lines[:-1]]
        assert all(c["object"] == "chat.completion.chunk" for c in chunks)
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
        assert text and chunks[-1]["usage"]["completion_tokens"] == 7

    def test_429_above_max_parallel(self):
        srv = fake_llm.FakeLLM(port=0, latency=0.6, max_parallel=2).start()
        try:
            results = []

            def one():
                results.append(post(srv.url + "/v1/chat/completions",
                                    {"model": "td-simple", "messages": [{"role": "user", "content": "x"}]}))

            threads = [threading.Thread(target=one) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(20)
            codes = sorted(r[0] for r in results)
            assert codes == [200, 200, 429, 429, 429]
            limited = next(r for r in results if r[0] == 429)
            assert limited[1]["Retry-After"] == "1"
            assert json.loads(limited[2])["error"]["type"] == "rate_limit_error"
            # Server memperbarui penghitung SETELAH jawaban terkirim; tunggu sampai tenang sebelum memeriksa.
            deadline = time.time() + 5
            while (srv.stats()["inflight"] != 0 or srv.stats()["ok"] != 2) and time.time() < deadline:
                time.sleep(0.01)
            stats = srv.stats()
            assert stats == {**stats, "requests": 5, "ok": 2, "rejected_429": 3, "peak_parallel": 2, "inflight": 0}
        finally:
            srv.stop()

    def test_bad_requests_and_unknown_paths(self, server):
        assert post(server.url + "/v1/chat/completions", {"model": "x"})[0] == 400
        assert post(server.url + "/v1/embeddings", {})[0] == 404
        req = urllib.request.Request(server.url + "/v1/chat/completions", data=b"{rusak", method="POST")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=10)
        assert err.value.code == 400

    def test_stats_reset_and_latency_is_applied(self):
        srv = fake_llm.FakeLLM(port=0, latency=0.15, max_parallel=0).start()      # 0 = tanpa batas paralel
        try:
            t0 = time.monotonic()
            assert post(srv.url + "/v1/chat/completions",
                        {"model": "m", "messages": [{"role": "user", "content": "x"}]})[0] == 200
            assert time.monotonic() - t0 >= 0.15
            assert get(srv.url + "/stats")[1]["requests"] == 1
            assert post(srv.url + "/stats/reset", {})[0] == 200 and srv.stats()["requests"] == 0
        finally:
            srv.stop()

    def test_cli_arguments(self):
        args = fake_llm.parse_args(["--port", "9100", "--latency", "1.5", "--max-parallel", "4", "--models", "a,b"])
        assert (args.port, args.latency, args.max_parallel, args.models) == (9100, 1.5, 4, "a,b")
