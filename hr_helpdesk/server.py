"""Backend HTTP server (stdlib only).

  GET  /health                      no auth
  POST /chat     {message, session_id, stream}   -> JSON, or NDJSON stream when stream=true
  POST /classify {text}                          -> validated ticket JSON
  GET  /history?session_id=...                   -> the caller's own stored history
  POST /history/clear {session_id}

Run:  python -m hr_helpdesk.server
"""
import json
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config
from .auth import authenticate
from .observability import RequestTrace
from .service import HelpdeskService

SERVICE = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HRHelpdesk/1.0"

    def log_message(self, fmt, *args):  # quiet default access log; we log structured JSON instead
        pass

    # ------------------------------------------------------------------ helpers
    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 20_000:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        return body

    def _trace(self, endpoint: str) -> RequestTrace:
        rid = self.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        sent = self.headers.get("X-Client-Sent-At")
        trace = RequestTrace(rid[:64], endpoint=endpoint, client_sent_at=float(sent) if sent else None)
        trace.event("request_received", endpoint=endpoint, client_to_server_ms=trace.client_to_server_ms)
        return trace

    def _authenticate(self, trace: RequestTrace):
        user_id = authenticate(self.headers.get("Authorization", ""))
        if not user_id:
            trace.event("auth_failed", reason="missing or invalid client token")
            trace.finish("unauthorized")
            self._json(401, {"error": "unauthorized", "request_id": trace.request_id})
            return None
        trace.user_id = user_id
        trace.event("auth_ok")
        return user_id

    # ------------------------------------------------------------------ routes
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self._json(200, {"status": "ok", "model": config.LLM_MODEL, "provider": config.LLM_PROVIDER})
        if url.path == "/history":
            trace = self._trace("/history")
            user_id = self._authenticate(trace)
            if not user_id:
                return
            sid = (parse_qs(url.query).get("session_id") or ["default"])[0]
            trace.finish("success")
            return self._json(200, {"employee_id": user_id, "session_id": sid, "turns": SERVICE.store.history(user_id, sid, 50)})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/chat", "/classify", "/history/clear"):
            return self._json(404, {"error": "not found"})
        trace = self._trace(path)
        user_id = self._authenticate(trace)          # 401 here: model and tools are never reached
        if not user_id:
            return
        try:
            body = self._body()
        except Exception as e:
            trace.finish("bad_request")
            return self._json(400, {"error": f"bad request: {e}", "request_id": trace.request_id})

        try:
            if path == "/history/clear":
                SERVICE.store.clear(user_id, str(body.get("session_id", "default")))
                trace.finish("success")
                return self._json(200, {"cleared": True})
            if path == "/classify":
                return self._classify(user_id, body, trace)
            return self._chat(user_id, body, trace)
        except BrokenPipeError:
            trace.finish("cancelled", error="client disconnected")
        except Exception as e:
            trace.event("unhandled_error", error=str(e), tb=traceback.format_exc()[-800:])
            trace.finish("error", error=str(e))
            try:
                self._json(500, {"error": "internal error", "request_id": trace.request_id})
            except Exception:
                pass

    def _classify(self, user_id, body, trace):
        text = str(body.get("text", "")).strip()
        if not text:
            trace.finish("bad_request")
            return self._json(400, {"error": "text is required"})
        ticket, validation = SERVICE.classify_ticket(text, trace)
        summary = trace.finish("success" if validation["valid"] else "fallback", schema_valid=validation["valid"])
        self._json(200, {"request_id": trace.request_id, "ticket": ticket, "validation": validation,
                         "usage": {k: summary[k] for k in ("input_tokens", "output_tokens", "cost_usd", "latency_ms")}})

    def _chat(self, user_id, body, trace):
        message = str(body.get("message", ""))
        session_id = str(body.get("session_id", "default"))[:64]
        stream = bool(body.get("stream", False))
        trace.extra["stream"] = stream

        if not stream:
            result = SERVICE.chat(user_id, message, session_id, trace, stream=False)
            summary = trace.finish(result["status"], intent=result["intent"], session_id=session_id)
            trace.event("response_sent")
            return self._json(200, {**result, "usage": {k: summary[k] for k in
                                    ("input_tokens", "output_tokens", "cost_usd", "latency_ms", "tool_names", "model")}})

        # Streaming: newline-delimited JSON events over HTTP/1.1 chunked transfer encoding.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        first_byte = {}

        def emit(event: dict):
            if "t" not in first_byte and event.get("type") == "delta":
                first_byte["t"] = round((time.perf_counter() - trace.t0) * 1000, 1)
            chunk = (json.dumps(event) + "\n").encode()
            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()

        emit({"type": "start", "request_id": trace.request_id})
        try:
            result = SERVICE.chat(user_id, message, session_id, trace, stream=True, emit=emit)
        except BrokenPipeError:
            raise
        except Exception as e:   # headers already sent: report the error inside the stream, then close it cleanly
            trace.event("unhandled_error", error=str(e), tb=traceback.format_exc()[-800:])
            trace.finish("error", error=str(e))
            emit({"type": "error", "error": "internal error", "request_id": trace.request_id})
            self.wfile.write(b"0\r\n\r\n")
            return
        summary = trace.finish(result["status"], intent=result["intent"], session_id=session_id,
                               server_ttft_ms=first_byte.get("t"))
        emit({"type": "done", "status": result["status"], "intent": result["intent"], "tools": result["tools"],
              "policy_docs": result["policy_docs"], "reply": result["reply"],
              "usage": {k: summary[k] for k in ("input_tokens", "output_tokens", "cost_usd", "latency_ms", "tool_names", "model")}})
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        trace.event("response_sent")


def main():
    global SERVICE
    SERVICE = HelpdeskService()
    if not config.llm_api_key():
        print(f"WARNING: {config.PROVIDERS[config.LLM_PROVIDER]['key_env']} is not set — model calls will fail.")
    srv = ThreadingHTTPServer((config.SERVER_HOST, config.SERVER_PORT), Handler)
    print(f"HR helpdesk backend on http://{config.SERVER_HOST}:{config.SERVER_PORT}  "
          f"provider={config.LLM_PROVIDER} model={config.LLM_MODEL}  logs -> {config.LOG_DIR}/server.jsonl")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
