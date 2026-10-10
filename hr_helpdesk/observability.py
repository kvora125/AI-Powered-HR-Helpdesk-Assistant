"""Structured JSON-lines logging for the full request lifecycle, plus per-request cost.

logs/server.jsonl gets one line per lifecycle event (request_received, auth, guard, llm_call,
tool_call, response_sent) and one `request_summary` line per request with every required
field: request_id, user_id, model, input_tokens, output_tokens, tool_names, latency_ms,
status, cost_usd. The CLI writes logs/client.jsonl with the same request_id so the two can be
joined to get true client-side end-to-end latency.
"""
import json
import threading
import time
from datetime import datetime, timezone

from . import config

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_jsonl(filename: str, record: dict) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": _now_iso(), **record}, ensure_ascii=False, default=str)
    with _lock, open(config.LOG_DIR / filename, "a") as f:
        f.write(line + "\n")


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = config.PRICING_PER_MTOK.get(model, config.PRICING_FALLBACK)
    return round(input_tokens / 1e6 * in_price + output_tokens / 1e6 * out_price, 8)


class RequestTrace:
    """Accumulates everything that happens during one request, then emits a summary line."""

    def __init__(self, request_id: str, user_id: str = None, endpoint: str = "", client_sent_at: float = None):
        self.request_id, self.user_id, self.endpoint = request_id, user_id, endpoint
        self.t0 = time.perf_counter()
        # Network + queueing time from the moment the client sent the request (client clock, same host in the demo).
        self.client_to_server_ms = round(time.time() * 1000 - client_sent_at * 1000, 1) if client_sent_at else None
        self.llm_calls, self.tool_calls, self.events = [], [], []
        self.extra = {}

    def event(self, name: str, **fields) -> None:
        rec = {"event": name, "request_id": self.request_id, "user_id": self.user_id,
               "t_ms": round((time.perf_counter() - self.t0) * 1000, 1), **fields}
        self.events.append(name)
        write_jsonl("server.jsonl", rec)

    def llm(self, result) -> None:
        call = {
            "purpose": result.purpose, "model": result.model, "status": result.status,
            "finish_reason": result.finish_reason, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "usage_estimated": result.usage_estimated,
            "latency_ms": result.latency_ms, "ttft_ms": result.ttft_ms,
            "cost_usd": cost_usd(result.model, result.input_tokens, result.output_tokens),
            "error": result.error,
        }
        self.llm_calls.append(call)
        self.event("llm_call", **call)

    def tool(self, name: str, args: str, status: str, latency_ms: float, error: str = None) -> None:
        call = {"tool_name": name, "arguments": args, "status": status, "latency_ms": latency_ms, "error": error}
        self.tool_calls.append(call)
        self.event("tool_call", **call)

    def finish(self, status: str, **fields) -> dict:
        in_tok = sum(c["input_tokens"] for c in self.llm_calls)
        out_tok = sum(c["output_tokens"] for c in self.llm_calls)
        summary = {
            "event": "request_summary",
            "request_id": self.request_id,
            "user_id": self.user_id,
            "endpoint": self.endpoint,
            "model": self.llm_calls[-1]["model"] if self.llm_calls else config.LLM_MODEL,
            "llm_calls": len(self.llm_calls),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "usage_estimated": any(c["usage_estimated"] for c in self.llm_calls),
            "tool_names": [c["tool_name"] for c in self.tool_calls],
            "latency_ms": round((time.perf_counter() - self.t0) * 1000, 1),
            "client_to_server_ms": self.client_to_server_ms,
            "status": status,
            "cost_usd": round(sum(c["cost_usd"] for c in self.llm_calls), 8),
            "price_per_mtok_usd": dict(zip(("input", "output"), config.PRICING_PER_MTOK.get(
                self.llm_calls[-1]["model"] if self.llm_calls else config.LLM_MODEL, config.PRICING_FALLBACK))),
            **self.extra,
            **fields,
        }
        write_jsonl("server.jsonl", summary)
        return summary
