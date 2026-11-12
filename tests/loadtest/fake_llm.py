#!/usr/bin/env python3
"""LLM tiruan OpenAI-compatible untuk uji beban manual terhadap gateway Hermes sungguhan.

Hanya pustaka standar. Endpoint:
  GET  /v1/models              daftar model
  POST /v1/chat/completions    jawaban tetap + `usage`; mendukung `stream: true` (SSE)
  GET  /stats                  penghitung: requests, ok, rejected_429, inflight, peak_parallel, tokens
  POST /stats/reset            nolkan penghitung
  GET  /health

Perilaku yang bisa diatur: latensi (+ jitter), jumlah request paralel sebelum dijawab 429
(`--max-parallel`, 0 = tanpa batas), dan jumlah token yang dilaporkan di `usage`.

Contoh:  python tests/loadtest/fake_llm.py --port 9100 --latency 3 --jitter 2 --max-parallel 8
Lihat tests/loadtest/README.md untuk mengarahkan Hermes uji ke server ini.

Server ini tidak pernah meminta tool call: tiap giliran agent = satu panggilan LLM (ditambah panggilan
bantu seperti judul sesi bila aktif). Ia TIDAK memeriksa API key — jangan dibuka ke jaringan luar.
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

REPLY = "Baik, sudah saya kerjakan. (jawaban LLM tiruan untuk uji beban)"


class FakeLLM:
    def __init__(self, host: str = "127.0.0.1", port: int = 9100, *, latency: float = 1.0, jitter: float = 0.0,
                 max_parallel: int = 8, prompt_tokens: int = 0, completion_tokens: int = 200,
                 models: tuple = ("td-normal", "td-simple"), retry_after: int = 1, seed: Optional[int] = None,
                 verbose: bool = False):
        self.host, self.port = host, port
        self.latency, self.jitter, self.max_parallel = float(latency), float(jitter), int(max_parallel)
        self.prompt_tokens, self.completion_tokens = int(prompt_tokens), int(completion_tokens)
        self.models, self.retry_after, self.verbose = tuple(models), int(retry_after), verbose
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._zero()

    # ------------------------------------------------------------------ penghitung
    def _zero(self) -> None:
        self._stats = {"requests": 0, "ok": 0, "rejected_429": 0, "inflight": 0, "peak_parallel": 0,
                       "tokens": 0, "started_at": time.time()}

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset(self) -> None:
        with self._lock:
            inflight = self._stats["inflight"]
            self._zero()
            self._stats["inflight"] = inflight

    def acquire(self) -> bool:
        """True = request dilayani; False = melebihi paralel → 429."""
        with self._lock:
            self._stats["requests"] += 1
            if self.max_parallel > 0 and self._stats["inflight"] >= self.max_parallel:
                self._stats["rejected_429"] += 1
                return False
            self._stats["inflight"] += 1
            self._stats["peak_parallel"] = max(self._stats["peak_parallel"], self._stats["inflight"])
            return True

    def release(self, tokens: int) -> None:
        with self._lock:
            self._stats["inflight"] -= 1
            self._stats["ok"] += 1
            self._stats["tokens"] += tokens

    def delay(self) -> float:
        with self._lock:
            return max(0.0, self.latency + (self._rng.uniform(0, self.jitter) if self.jitter > 0 else 0.0))

    def usage_for(self, body: dict) -> dict:
        prompt = self.prompt_tokens
        if prompt <= 0:                                   # perkiraan kasar: 4 karakter per token
            prompt = max(1, len(json.dumps(body.get("messages") or [], ensure_ascii=False)) // 4)
        return {"prompt_tokens": prompt, "completion_tokens": self.completion_tokens,
                "total_tokens": prompt + self.completion_tokens}

    # ------------------------------------------------------------------ hidup-mati
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "FakeLLM":
        self._server = ThreadingHTTPServer((self.host, self.port), _handler_for(self))
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-llm", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    def serve_forever(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), _handler_for(self))
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        try:
            self._server.serve_forever()
        finally:
            self._server.server_close()


def _handler_for(llm: FakeLLM) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FakeLLM/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            if llm.verbose:
                super().log_message(fmt, *args)

        def _json(self, status: int, payload: dict, headers: Optional[dict] = None) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, str(value))
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, status: int, kind: str, message: str, headers: Optional[dict] = None) -> None:
            self._json(status, {"error": {"message": message, "type": kind, "code": kind}}, headers)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/v1/models":
                now = int(time.time())
                self._json(200, {"object": "list", "data": [
                    {"id": m, "object": "model", "created": now, "owned_by": "fake-llm"} for m in llm.models]})
            elif path == "/stats":
                self._json(200, llm.stats())
            elif path in ("/health", ""):
                self._json(200, {"status": "ok"})
            else:
                self._error(404, "not_found", f"tidak ada {path}")

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body bukan objek")
            except (ValueError, OSError) as exc:
                self._error(400, "invalid_request_error", f"JSON tidak sah: {exc}")
                return
            if path == "/stats/reset":
                llm.reset()
                self._json(200, llm.stats())
                return
            if path != "/v1/chat/completions":
                self._error(404, "not_found", f"tidak ada {path}")
                return
            if not isinstance(body.get("messages"), list):
                self._error(400, "invalid_request_error", "`messages` wajib berupa daftar")
                return
            if not llm.acquire():
                self._error(429, "rate_limit_error", "Too Many Requests: batas request paralel terlampaui",
                            {"Retry-After": llm.retry_after})
                return
            usage = llm.usage_for(body)
            try:
                time.sleep(llm.delay())
                if body.get("stream"):
                    self._stream(body, usage)
                else:
                    self._json(200, {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion",
                        "created": int(time.time()), "model": str(body.get("model") or llm.models[0]),
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": REPLY}}],
                        "usage": usage})
            except (BrokenPipeError, ConnectionResetError):
                pass                                      # klien menutup sambungan (giliran dihentikan)
            finally:
                llm.release(usage["total_tokens"])

        def _stream(self, body: dict, usage: dict) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            base = {"id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": str(body.get("model") or llm.models[0])}

            def send(payload: Any) -> None:
                data = payload if isinstance(payload, str) else json.dumps({**base, **payload})
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()

            send({"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
            for word in REPLY.split(" "):
                send({"choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]})
            send({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            if (body.get("stream_options") or {}).get("include_usage"):
                send({"choices": [], "usage": usage})
            send("[DONE]")

    return Handler


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="LLM tiruan OpenAI-compatible untuk uji beban Hermes.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--latency", type=float, default=1.0, help="detik per request")
    ap.add_argument("--jitter", type=float, default=0.0, help="tambahan acak 0..N detik")
    ap.add_argument("--max-parallel", type=int, default=8, help="request paralel sebelum 429 (0 = tanpa batas)")
    ap.add_argument("--prompt-tokens", type=int, default=0, help="0 = perkirakan dari ukuran request")
    ap.add_argument("--completion-tokens", type=int, default=200)
    ap.add_argument("--models", default="td-normal,td-simple", help="daftar ID model, dipisah koma")
    ap.add_argument("--retry-after", type=int, default=1, help="nilai header Retry-After pada 429")
    ap.add_argument("--verbose", action="store_true", help="catat tiap request ke stderr")
    return ap.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    llm = FakeLLM(args.host, args.port, latency=args.latency, jitter=args.jitter, max_parallel=args.max_parallel,
                  prompt_tokens=args.prompt_tokens, completion_tokens=args.completion_tokens,
                  models=tuple(m.strip() for m in args.models.split(",") if m.strip()),
                  retry_after=args.retry_after, verbose=args.verbose)
    print(f"LLM tiruan di http://{args.host}:{args.port}/v1  (latensi {args.latency}+{args.jitter} dtk, "
          f"429 di atas {args.max_parallel or '∞'} paralel) — Ctrl+C untuk berhenti", flush=True)
    try:
        llm.serve_forever()
    except KeyboardInterrupt:
        print(json.dumps(llm.stats()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
