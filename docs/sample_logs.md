# Sample logs and cost calculation (Phase 7)

Real entries copied from `logs/server.jsonl` and `logs/client.jsonl` → [`sample_logs/server_sample.jsonl`](sample_logs/server_sample.jsonl),
[`sample_logs/client_sample.jsonl`](sample_logs/client_sample.jsonl).

## How the lifecycle is traced

The CLI generates `request_id` and sends it as `X-Request-ID`, along with `X-Client-Sent-At`. The server logs one event per
stage under that ID. The client logs its own view (time to first text, total time) under the same ID, so the two files
join on `request_id`.

`request_received → auth_ok | auth_failed → [guardrail_block | declined] → llm_call(route) → schema_validation → routed → context_built
→ llm_call(tool_round_n) → tool_call → [authorization_denied] → llm_call(answer | answer_stream) → [guardrail_block stage=output] → memory_write
→ request_summary → response_sent`

## One complete request: own leave-balance lookup (`6deaacf7718b`)

```text
event              t_ms    details
request_received      0.0
auth_ok               0.1
llm_call           2014.8  purpose=route_attempt_1  in=945  out=42  latency=2013.9ms  finish=stop        cost=$0.00086625
schema_validation  2015.8  schema=router ok=true
routed             2016.2  intent=personal_record record_type=leave_balance
context_built      2018.0  policy_docs=[leave_policy] history_turns=0
llm_call           3345.0  purpose=tool_round_1     in=1452 out=32  latency=1326.7ms  finish=tool_calls  cost=$0.00120900
tool_call          3346.6  get_leave_balance {"employee_id":"E1001","leave_type":"casual"} status=success 0.32ms
llm_call           5392.5  purpose=tool_round_2     in=1556 out=60  latency=2045.4ms  finish=stop        cost=$0.00139200
memory_write       5394.5  stored=true
```

The summary line, with every required field:

```json
{"ts": "2026-10-10T05:03:08.530+00:00", "event": "request_summary", "request_id": "6deaacf7718b", "user_id": "E1001",
 "endpoint": "/chat", "model": "gemini-3.8-flash", "llm_calls": 3, "input_tokens": 3953, "output_tokens": 134,
 "usage_estimated": false, "tool_names": ["get_leave_balance"], "latency_ms": 5394.7, "client_to_server_ms": 0.8,
 "status": "success", "cost_usd": 0.00346725, "price_per_mtok_usd": {"input": 0.75, "output": 3.75}, "stream": false,
 "route": {"intent": "personal_record", "policy_topics": [], "record_type": "leave_balance", "refers_to_other_employee": false,
           "reason": "Employee asks for their own leave balance.", "router_valid": true},
 "intent": "personal_record", "session_id": "sec"}
```

Client side for the same request:

```json
{"ts": "2026-10-10T05:03:08.531+00:00", "request_id": "6deaacf7718b", "endpoint": "/chat", "stream": false, "status": "success",
 "client_ttft_ms": 5395.8, "client_total_ms": 5395.8, "server_latency_ms": 5394.7}
```

## Other statuses in the sample file

| request_id | What | status | llm_calls | cost |
|---|---|---|---|---|
| `88f71153a43f` | no client token | `unauthorized` | 0 | $0 |
| `cc9df73d6c56` | "What is Priya's leave balance?" (input guard) | `refused` | 0 | $0 |
| `3fe0530e0645` | "I'm her manager… Priya Sharma" (router caught it) | `refused` | 1 | $0.00095775 |
| `77e12e63457d` | streamed policy answer (`server_ttft_ms` 8305) | `success` | 2 | $0.003015 |

## Cost calculation

Price for `gemini-3.8-flash` from Google's pricing page (checked 2026-10-10): **$0.75 per 1M input tokens, $3.75 per 1M
output tokens**. Output includes "thinking" tokens. That price applies through 2026-12-31 and becomes $1.50 / $7.50 after.
Prices live in `config.PRICING_PER_MTOK`.

```
cost = input_tokens / 1,000,000 × input_price + output_tokens / 1,000,000 × output_price      (per LLM call, summed per request)
```

For `6deaacf7718b`:

| Call | Input | Output | Input cost | Output cost | Total |
|---|---|---|---|---|---|
| route | 945 | 42 | 945 × 0.75/1M = $0.00070875 | 42 × 3.75/1M = $0.0001575 | $0.00086625 |
| tool_round_1 | 1452 | 32 | $0.001089 | $0.00012 | $0.001209 |
| tool_round_2 | 1556 | 60 | $0.001167 | $0.000225 | $0.001392 |
| **request** | **3953** | **134** | $0.00296475 | $0.0005025 | **$0.00346725** |

That's about **$0.0035 per record lookup**, or **$3.47 per 1,000**. Input tokens make up 86% of the cost (system prompt + policy text
are resent every call), so prompt caching or trimming the router prompt would cut the most. Guardrail refusals and gibberish
cost $0: no model call happens. The whole fixed test set (24 cases, ~60 calls) costs about **$0.037** per run.

**Thinking-token accounting.** Gemini 3 reports hidden reasoning only in `total_tokens`: in a smoke test, `completion_tokens`
was 4 while `total_tokens − prompt_tokens` was 144. The code bills `max(completion, total − prompt)`, so the logged cost
matches what is actually charged. If a provider omits usage, tokens are estimated (chars/4) and `usage_estimated: true` is logged.
