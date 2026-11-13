#!/usr/bin/env python3
"""pollinations_bridge.py — 把 pollinations 的 /openai 端点翻译成标准 OpenAI /v1/chat/completions

为什么需要：coai 的 openai adapter 固定拼 {endpoint}/v1/chat/completions，
而 pollinations 只在 /openai 上提供兼容端点，且限速 1 次/45 秒/IP。
本桥做三件事：
  1. 路径翻译  /v1/chat/completions -> https://text.pollinations.ai/openai
  2. 限速排队  402 FREE_LIMIT 时按 retry-after 自动等待重试（默认最多等 120 秒）
  3. 补齐 usage  上游不返回 usage 时按字符数估算，供 coai 计费

用法：
  python pollinations_bridge.py --port 8899
  python pollinations_bridge.py --port 8899 --upstream https://text.pollinations.ai
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("pbridge")

UPSTREAM = "https://text.pollinations.ai"
LOCK = threading.Lock()
NEXT_OK = 0.0          # 全局最早可发请求的时间戳（秒）
MAX_WAIT = 8.0         # 单次请求最多排队等待秒数（超时立刻 429，交给 coai 走兜底渠道，不要挂死客户端）
TIMEOUT = 180


def _estimate_tokens(text: str) -> int:
    """粗估 token：中文 1 字≈1 token，英文 4 字符≈1 token。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", text))
    rest = len(text) - cjk
    return max(1, cjk + rest // 4)


def _messages_text(messages) -> str:
    out = []
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    out.append(part.get("text", ""))
    return "\n".join(out)


def _wait_slot() -> float:
    """按全局节流窗口排队，返回实际等待秒数。"""
    global NEXT_OK
    with LOCK:
        now = time.time()
        wait = max(0.0, NEXT_OK - now)
        if wait > MAX_WAIT:
            raise TimeoutError(f"rate limit queue too long: {wait:.0f}s")
        NEXT_OK = max(now, NEXT_OK) + 46.0   # 45 秒限速 + 1 秒余量
    if wait > 0:
        LOG.info("throttle: sleep %.1fs", wait)
        time.sleep(wait)
    return wait


def _post_upstream(payload: dict, retries: int = 3):
    """向 pollinations /openai 发请求，遇 402 FREE_LIMIT 按 retry-after 重试。"""
    body = json.dumps(payload, ensure_ascii=False).encode()
    for attempt in range(retries + 1):
        _wait_slot()
        req = urllib.request.Request(UPSTREAM + "/openai", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer pollinations")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            if e.code == 402 and attempt < retries:
                txt = raw.decode("utf-8", "replace")
                m = re.search(r"[Rr]etry after (\d+)", txt)
                wait = float(m.group(1)) if m else 46.0
                wait = min(wait + 2.0, MAX_WAIT)
                LOG.warning("402 FREE_LIMIT, retry in %.0fs (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue
            return e.code, raw
        except Exception as e:
            if attempt < retries:
                LOG.warning("upstream error %s, retry", e)
                time.sleep(5)
                continue
            raise
    return 599, b'{"error":"upstream exhausted"}'


def _normalize(data: dict, model_in: str, prompt_text: str) -> dict:
    """补齐 id/created/usage，保证 coai 能正常解析并计费。"""
    data.setdefault("id", "pllns-" + str(int(time.time() * 1000)))
    data.setdefault("object", "chat.completion")
    data.setdefault("created", int(time.time()))
    choices = data.get("choices") or []
    content = ""
    if choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
    if not data.get("usage"):
        data["usage"] = {
            "prompt_tokens": _estimate_tokens(prompt_text),
            "completion_tokens": _estimate_tokens(content),
            "total_tokens": _estimate_tokens(prompt_text) + _estimate_tokens(content),
        }
    else:
        u = data["usage"]
        u.setdefault("prompt_tokens", _estimate_tokens(prompt_text))
        u.setdefault("completion_tokens", _estimate_tokens(content))
        u.setdefault("total_tokens", u["prompt_tokens"] + u["completion_tokens"])
    if not data.get("model"):
        data["model"] = model_in
    return data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            try:
                with urllib.request.urlopen(UPSTREAM + "/openai/models", timeout=30) as r:
                    body = r.read()
            except Exception:
                body = json.dumps({
                    "object": "list",
                    "data": [{"id": "gpt-oss-20b", "object": "model", "owned_by": "pollinations"}],
                }).encode()
            self._send(200, body)
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, b'{"error":"not found"}')
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            self._send(400, json.dumps({"error": {"message": f"bad json: {e}"}}).encode())
            return

        model_in = req.get("model") or "gpt-oss-20b"
        # pollinations 免费层只认 openai-fast；"openai" 现已要求付费 key（会 402/404）
        payload = dict(req)
        payload["model"] = "openai-fast"
        payload.pop("stream", None)   # 统一走非流式，桥内部再合成 SSE
        want_stream = bool(req.get("stream"))
        prompt_text = _messages_text(req.get("messages"))

        try:
            code, raw = _post_upstream(payload)
        except TimeoutError as e:
            self._send(429, json.dumps({"error": {"message": str(e), "type": "rate_limit_error"}}).encode())
            return
        except Exception as e:
            self._send(502, json.dumps({"error": {"message": f"bridge upstream failed: {e}"}}).encode())
            return

        if code != 200:
            self._send(code, raw)
            return

        try:
            data = _normalize(json.loads(raw.decode("utf-8")), model_in, prompt_text)
        except Exception as e:
            self._send(502, json.dumps({"error": {"message": f"bridge parse failed: {e}"}}).encode())
            return

        if not want_stream:
            self._send(200, json.dumps(data, ensure_ascii=False).encode())
            return

        # 合成 OpenAI SSE：一次性把内容分块吐出，再给 [DONE]
        cid = data["id"]
        created = data["created"]
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")

        def chunk(delta, finish=None):
            return "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model_in,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }, ensure_ascii=False) + "\n\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(chunk({"role": "assistant", "content": ""}).encode())
            step = 24
            for i in range(0, len(content), step):
                self.wfile.write(chunk({"content": content[i:i + step]}).encode())
                self.wfile.flush()
            self.wfile.write(chunk({}, "stop").encode())
            tail = {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model_in, "choices": [], "usage": data["usage"]}
            self.wfile.write(("data: " + json.dumps(tail, ensure_ascii=False) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception as e:
            LOG.warning("stream write failed: %s", e)


def main() -> int:
    global UPSTREAM
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--upstream", default=UPSTREAM)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    UPSTREAM = a.upstream.rstrip("/")
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    LOG.info("pollinations bridge on http://%s:%d -> %s/openai", a.host, a.port, UPSTREAM)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        LOG.info("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
