"""Provider-agnostic LLM client (OpenAI-compatible Chat Completions over plain HTTP).

Every call returns an LLMResult with an explicit `status` so callers MUST decide what to do
with success / incomplete / error before acting on or displaying the output.
"""
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config

STATUS_SUCCESS = "success"
STATUS_INCOMPLETE = "incomplete"   # truncated (finish_reason=length) or stream cut off
STATUS_ERROR = "error"             # HTTP/network error, content filter, malformed response


@dataclass
class LLMResult:
    status: str
    content: str = ""
    tool_calls: list = field(default_factory=list)   # [{"id", "name", "arguments"}]
    raw_tool_calls: list = field(default_factory=list)  # provider's original objects, echoed back verbatim
                                                        # (Gemini 3 requires its thought_signature to survive)
    finish_reason: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    usage_estimated: bool = False
    latency_ms: float = 0.0
    ttft_ms: Optional[float] = None
    model: str = ""
    error: Optional[str] = None
    purpose: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS


def _status_from_finish(finish_reason: Optional[str]) -> str:
    if finish_reason in ("stop", "tool_calls", "function_call", "end_turn"):
        return STATUS_SUCCESS
    if finish_reason in ("length", "max_tokens", None):
        return STATUS_INCOMPLETE
    return STATUS_ERROR   # e.g. content_filter, safety, recitation


def _output_tokens(usage: dict) -> int:
    """Billable output tokens. Thinking models report hidden reasoning only in total_tokens
    (total - prompt > completion), and it is billed at the output rate, so count it."""
    completion = usage.get("completion_tokens") or 0
    total, prompt = usage.get("total_tokens"), usage.get("prompt_tokens")
    return max(completion, total - prompt) if total and prompt else completion


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class LLMClient:
    def __init__(self, model: str = None, base_url: str = None, api_key: str = None, timeout: float = None):
        self.model = model or config.LLM_MODEL
        self.base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else config.llm_api_key()
        self.timeout = timeout or config.LLM_TIMEOUT_S

    # ---------------------------------------------------------------- transport
    def _post(self, payload: dict):
        if not self.api_key:
            raise RuntimeError(f"No API key: set {config.PROVIDERS[config.LLM_PROVIDER]['key_env']} in your environment/.env")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "hr-helpdesk-v1/1.0",
            },
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=self.timeout)

    def _post_with_retry(self, payload: dict, retries: int = 2):
        delay = 1.5
        for attempt in range(retries + 1):
            try:
                return self._post(payload)
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:500]
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    retry_after = e.headers.get("retry-after")
                    time.sleep(float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else delay)
                    delay *= 2
                    continue
                # Some providers reject stream_options; retry once without it.
                if e.code == 400 and "stream_options" in payload and "stream_options" in body:
                    payload = {k: v for k, v in payload.items() if k != "stream_options"}
                    continue
                raise RuntimeError(f"HTTP {e.code}: {body}") from None

    def _payload(self, messages, tools, tool_choice, json_mode, max_tokens, temperature, stream):
        p = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            p["tools"] = tools
            p["tool_choice"] = tool_choice or "auto"
        if config.LLM_REASONING_EFFORT:
            p["reasoning_effort"] = config.LLM_REASONING_EFFORT
        if json_mode:
            p["response_format"] = {"type": "json_object"}
        if stream:
            p["stream"] = True
            p["stream_options"] = {"include_usage": True}
        return p

    # ---------------------------------------------------------------- non-streaming
    def complete(self, messages, tools=None, tool_choice=None, json_mode=False,
                 max_tokens=700, temperature=0.1, purpose="") -> LLMResult:
        t0 = time.perf_counter()
        payload = self._payload(messages, tools, tool_choice, json_mode, max_tokens, temperature, stream=False)
        try:
            with self._post_with_retry(payload) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:  # network, HTTP, JSON decode
            return LLMResult(status=STATUS_ERROR, error=str(e), model=self.model, purpose=purpose,
                             latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                             input_tokens=_estimate_tokens(json.dumps(messages)), usage_estimated=True)
        latency = round((time.perf_counter() - t0) * 1000, 1)
        try:
            choice = data["choices"][0]
            msg = choice.get("message") or {}
        except (KeyError, IndexError, TypeError):
            return LLMResult(status=STATUS_ERROR, error=f"malformed response: {str(data)[:300]}",
                             model=self.model, latency_ms=latency, purpose=purpose)
        tool_calls = [
            {"id": tc.get("id") or f"call_{i}", "name": tc["function"]["name"],
             "arguments": tc["function"].get("arguments") or "{}"}
            for i, tc in enumerate(msg.get("tool_calls") or [])
        ]
        finish = choice.get("finish_reason")
        if tool_calls and finish not in ("length",):
            finish = "tool_calls"
        usage = data.get("usage") or {}
        content = msg.get("content") or ""
        return LLMResult(
            status=_status_from_finish(finish),
            content=content,
            tool_calls=tool_calls,
            raw_tool_calls=msg.get("tool_calls") or [],
            finish_reason=finish,
            input_tokens=usage.get("prompt_tokens") or _estimate_tokens(json.dumps(messages)),
            output_tokens=_output_tokens(usage) or _estimate_tokens(content),
            usage_estimated=not usage,
            latency_ms=latency,
            ttft_ms=latency,
            model=data.get("model") or self.model,
            error=None if finish in ("stop", "tool_calls") else f"finish_reason={finish}",
            purpose=purpose,
        )

    # ---------------------------------------------------------------- streaming
    def stream(self, messages, on_delta: Callable[[str], None], max_tokens=700, temperature=0.2,
               purpose="", tools=None, should_cancel: Callable[[], bool] = lambda: False) -> LLMResult:
        """Streams text deltas to `on_delta` as they arrive. If `tools` is passed (because the history
        contains tool results), they are sent with tool_choice="none": the model must answer in text."""
        t0 = time.perf_counter()
        ttft = None
        parts, finish, usage = [], None, None
        payload = self._payload(messages, tools, "none" if tools else None, False, max_tokens, temperature, stream=True)
        try:
            with self._post_with_retry(payload) as resp:
                for raw in resp:
                    if should_cancel():
                        finish = "cancelled"
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    chunk = json.loads(body)
                    usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            if ttft is None:
                                ttft = round((time.perf_counter() - t0) * 1000, 1)
                            parts.append(delta)
                            on_delta(delta)
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except Exception as e:
            text = "".join(parts)
            return LLMResult(status=STATUS_INCOMPLETE if parts else STATUS_ERROR, content=text,
                             error=str(e), model=self.model, purpose=purpose, ttft_ms=ttft,
                             latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                             input_tokens=_estimate_tokens(json.dumps(messages)),
                             output_tokens=_estimate_tokens(text), usage_estimated=True)
        text = "".join(parts)
        status = STATUS_INCOMPLETE if finish == "cancelled" else _status_from_finish(finish)
        return LLMResult(
            status=status,
            content=text,
            finish_reason=finish,
            input_tokens=(usage or {}).get("prompt_tokens") or _estimate_tokens(json.dumps(messages)),
            output_tokens=_output_tokens(usage or {}) or _estimate_tokens(text),
            usage_estimated=not usage,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            ttft_ms=ttft,
            model=self.model,
            error=None if status == STATUS_SUCCESS else f"finish_reason={finish}",
            purpose=purpose,
        )
