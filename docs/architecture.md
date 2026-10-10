# Architecture — HR Helpdesk Assistant V1

## Request–response flow

```mermaid
flowchart TD
    C["CLI client<br/>client/cli.py<br/>(Bearer token, X-Request-ID, X-Client-Sent-At)"]
    C -->|"POST /chat {message, session_id, stream}"| AUTH

    subgraph Backend["Backend server — hr_helpdesk/server.py + service.py"]
        AUTH{"1. Authentication<br/>token hash → employee_id"}
        AUTH -->|"missing / bad token"| R401["401 — model & tools never reached"]
        AUTH -->|ok| GUARD{"2. Input guard (code)<br/>catastrophic-action list<br/>+ gibberish filter"}
        GUARD -->|blocked| CANNED["Canned refusal / decline"]
        GUARD -->|pass| MEMR[("Memory read<br/>SQLite, keyed by<br/>employee_id + session")]
        MEMR --> ROUTER["3. LLM call #1 — ROUTER<br/>JSON contract → schema validated<br/>(retry once → safe fallback)"]
        ROUTER --> BRANCH{"4. Deterministic branch on intent"}
        BRANCH -->|"out_of_scope / restricted"| CANNED
        BRANCH -->|ticket| CLS["LLM call — CLASSIFIER<br/>ticket JSON → validated<br/>(retry → human-review fallback)"]
        CLS --> TSTORE[("tickets.jsonl")]
        BRANCH -->|"policy_question / general_hr"| POL["Policy loader<br/>topic + keyword selection<br/>injection-line quarantine<br/>&lt;policy_document&gt; delimiters"]
        BRANCH -->|personal_record| TOOLS
        POL --> ANS
        subgraph DET["Deterministic layer — tools.py"]
            TOOLS["LLM call — ASSISTANT with tool specs"] -->|tool_call| EXEC["execute_tool()<br/>• scoped to authenticated user_id<br/>• arg provenance (claim ID typed by user)<br/>• read-only, sensitive fields removed"]
            EXEC --> DB[("employees.json<br/>mock HR DB")]
            DB --> EXEC
            EXEC -->|"tool result (JSON)"| TOOLS
        end
        TOOLS --> ANS["5. LLM call — ASSISTANT final answer<br/>streamed token-by-token (or not)"]
        ANS --> STATUS{"6. Response status check<br/>success / incomplete / error"}
        STATUS --> OGUARD{"7. Output guard<br/>prompt canary, other employees'<br/>names / IDs / salaries"}
        OGUARD --> MEMW[("8. Memory write<br/>after filtering")]
        CANNED --> MEMW
        CLS --> MEMW
    end

    MEMW -->|"JSON, or NDJSON stream of delta events"| C

    LOG[["Observability — logs/server.jsonl<br/>every stage emits an event; one request_summary per request:<br/>request_id, user_id, model, tokens, tool_names, latency, status, cost_usd"]]
    Backend -.-> LOG
    C -.->|"logs/client.jsonl (same request_id: client TTFT + total)"| LOG
```

## Where things sit

| Concern | Location | Notes |
|---|---|---|
| Client | `client/cli.py` | Sends token + request ID + send timestamp; renders stream; logs client-side latency |
| Authentication | `hr_helpdesk/auth.py` | SHA-256 token hashes, constant-time compare; 401 before any model/tool work |
| Catastrophic-action guard | `hr_helpdesk/guardrails.py` | Input check (before model) + output check (after model) |
| Router contract | `hr_helpdesk/schemas.py::ROUTER_SCHEMA` | `intent`, `policy_topics`, `record_type`, `refers_to_other_employee`, `reason` |
| Ticket contract | `hr_helpdesk/schemas.py::TICKET_SCHEMA` | `category`, `urgency`, `requires_human_review`, `needs_clarification`, `clarification_question`, `summary`, `urgency_reason` |
| Deterministic layer | `hr_helpdesk/tools.py` + `data/employees.json` | `get_leave_balance`, `get_reimbursement_status`; authorization in code |
| Policy context | `hr_helpdesk/policies.py` + `data/policies/*.md` | Full text of selected policies placed in context; no embeddings |
| Memory | `hr_helpdesk/memory.py` → `data/runtime/memory.db` | SQLite; filter before write |
| Streaming | `server.py` (chunked NDJSON) ← `llm.py::stream` (provider SSE) | `delta`, `ticket`, `replace`, `done` events |
| Observability & cost | `hr_helpdesk/observability.py` → `logs/server.jsonl` | Lifecycle events + `request_summary` with `cost_usd` |

## Structured response contract (model → backend)

The model never "decides" an action in free text. Every decision point returns JSON that is
validated before the backend acts:

```json
// Router (every turn)
{"intent": "personal_record", "policy_topics": [], "record_type": "leave_balance",
 "refers_to_other_employee": false, "reason": "Own leave balance lookup."}

// Ticket classifier
{"category": "payroll", "urgency": "high", "requires_human_review": true,
 "needs_clarification": false, "clarification_question": null,
 "summary": "September salary not credited.", "urgency_reason": "Salary not received; rent due on the 5th."}

// Tool calls use the provider's native function-calling format, and the arguments are
// re-validated in tools.py (JSON object, required keys, ownership, provenance).
```

Only the final conversational answer is free text, and it still passes the status check and
the output guard before the client sees it.
