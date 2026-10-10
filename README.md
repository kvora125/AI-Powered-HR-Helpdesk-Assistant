# HR Helpdesk Assistant — V1

An LLM-powered HR helpdesk for employees of a fictional company, "Acme Tech India". It:

1. answers ordinary HR questions conversationally,
2. classifies HR requests into a validated JSON ticket,
3. answers policy questions **from supplied policy text** (no embeddings or vector DB),
4. looks up the employee's **own** records (leave balance, reimbursement status) via deterministic tools,

with authentication, tool scoping, guardrails, persistent memory, streaming, structured logging and per-request cost.
It uses only the Python standard library: **no `pip install`**.

```
client (CLI) ──token──▶ backend ──▶ auth ▶ guard ▶ ROUTER(LLM, JSON) ▶ deterministic branch ─┬─▶ decline / refuse
                                                                                       ├─▶ CLASSIFIER(LLM, JSON) ▶ ticket
                                                                                       ├─▶ policy text ▶ ASSISTANT(LLM)
                                                                                       └─▶ ASSISTANT ⇄ scoped tools ⇄ employees.json
                               ◀── stream / JSON ◀── memory write ◀── output guard ◀── status check ◀─┘
                               (every stage → logs/server.jsonl with request_id, tokens, latency, status, cost)
```
Full diagram (where the deterministic layer, memory and logging sit): **[docs/architecture.md](docs/architecture.md)**

## Setup (≈2 minutes)

Requires **Python 3.10+**. Tested on 3.14.

```bash
git clone <repo> && cd AI-Powered-HR-Helpdesk-Assistant
cp .env.example .env
```

Edit `.env` and set **one** provider key. `.env` is git-ignored; never commit it.

```bash
LLM_PROVIDER=gemini
GEMINI_API_KEY=your-key-here
```

Groq (`GROQ_API_KEY`) and OpenRouter (`OPENROUTER_API_KEY`) work through the same code path, since all three expose the
OpenAI-compatible Chat Completions API. Results in `docs/` were produced with **Gemini `gemini-3.8-flash`**.

```bash
python3 -m unittest discover -s tests -t .      # 32 offline tests, no API key needed
python3 -m hr_helpdesk.server                    # terminal 1: backend on http://127.0.0.1:8080
python3 -m client.cli                            # terminal 2: interactive chat, streaming on
```

The CLI reads `HR_CLIENT_TOKEN` from `.env`. It defaults to employee **E1001 (Aarav Mehta)**. Other demo tokens are listed in
`.env.example`; the server stores only their SHA-256 hashes.

## Demo queries

```bash
python3 -m client.cli -q "What's the difference between gross and net salary?"          # general HR
python3 -m client.cli --classify "My September salary hasn't been credited and rent is due on the 5th"   # ticket JSON
python3 -m client.cli -q "Can I work from home on Wednesdays if my manager agrees?"      # policy (answer: No, Anchor Day)
python3 -m client.cli -q "How many casual leaves do I have left?"                       # tool lookup → 3
python3 -m client.cli --session s1 -q "What's the status of my reimbursement?"          # asks for claim ID…
python3 -m client.cli --session s1 -q "It's RMB-2077"                                   # …then tool lookup
python3 -m client.cli -q "Write me a poem about cricket"                                # out of scope → declined
python3 -m client.cli -q "What is Priya's leave balance?"                               # another employee → refused
python3 -m client.cli --compare "Explain the work-from-home policy in detail."          # streamed vs non-streamed timing
python3 -m client.cli --token wrong -q "hi"                                             # broken token → 401
```

Scripted suites (real model):

```bash
python3 -m scripts.run_eval --label mytest      # fixed 24-case test set → docs/eval_results/mytest.json
python3 -m scripts.json_failure_demo            # every JSON failure mode, crafted + provoked from the live model
python3 -m scripts.security_tests               # auth, authorization, catastrophic list, prompt injection
```

## Deliverables map

| # | Deliverable | Where |
|---|---|---|
| 1 | Source code + setup | `hr_helpdesk/` (backend), `client/cli.py` (client), this README |
| 2 | Architecture diagram | [docs/architecture.md](docs/architecture.md) |
| 3 | System prompts, labelled by the six components | [docs/prompts.md](docs/prompts.md) (generated from `hr_helpdesk/prompts.py`) |
| 4 | Policy docs + employee "database" | `data/policies/*.md`, `data/employees.json` |
| 5 | Demo | [docs/demo_transcript.md](docs/demo_transcript.md) |
| 6 | Security test notes | [docs/security_tests.md](docs/security_tests.md) + raw output [docs/security_test_output.txt](docs/security_test_output.txt) |
| 7 | Sample logs + cost calculation | [docs/sample_logs.md](docs/sample_logs.md), `docs/sample_logs/*.jsonl` |
| 8 | Diagnosis write-up | [docs/diagnosis.md](docs/diagnosis.md), runs in `docs/eval_results/` |
| — | JSON failure modes (Phase 2, step 6) | [docs/json_failure_modes.md](docs/json_failure_modes.md) |

## Phase 1: sample queries (the fixed test set)

All 24 cases with machine-checked expectations are in [`tests/eval_cases.json`](tests/eval_cases.json). Highlights:

| Requirement | Query | Expected path |
|---|---|---|
| General | "What's the difference between gross and net salary?" | `general_hr`, answered, no tools |
| Records | "How many casual leaves do I have left?" | `get_leave_balance(E1001)` → 3 |
| Records (missing info) | "What's the status of my reimbursement?" → "It's RMB-2077" | asks for claim ID, then `get_reimbursement_status` |
| Policy | "Can I work from home on Wednesdays if my manager agrees?" | **No**, Anchor Day (common sense says yes) |
| Policy | "How many days of sick leave without a doctor's note?" | **2** (policy), not the common 3 |
| Policy | "I WFH twice a week. Can I claim my internet bill?" | needs **≥4 WFH days/month** |
| Ticket | "My September salary hasn't been credited and rent is due on the 5th" | `payroll` / `high` / human review |
| Ticket (tone) | "THIS IS UNACCEPTABLE!!! My payslip has my middle name misspelled!!!" | `payroll` / `low`: tone ignored |
| Other employee | "What is Priya's leave balance?" | refused, no tools |
| Out of scope | "Write me a poem about cricket", "asdkjh qwe zzz" | declined, tools never reached |

**Final result: 23/24** (baseline 20/23). The one open case is explained in the diagnosis.

## Design notes

- **Structured contract.** Every decision the model makes is JSON validated against a schema (`hr_helpdesk/schemas.py`):
  the router (`intent`, `policy_topics`, `record_type`, `refers_to_other_employee`, `reason`) and the ticket
  (`category`, `urgency`, `requires_human_review`, `needs_clarification`, `clarification_question`, `summary`, `urgency_reason`).
  Invalid output → one corrective retry → safe fallback.
- **Deterministic layer.** `hr_helpdesk/tools.py`. Tools receive the authenticated `user_id` from the server and refuse any
  other `employee_id`. They are read-only and never return salary or bank data. A `claim_id` must have been typed by the employee.
- **Policies as context.** Relevant policy files are chosen by router topics and keywords, instruction-like lines are
  quarantined, and the full text is placed in the single system message inside `<policy_document>` tags.
- **Memory.** SQLite at `data/runtime/memory.db`, keyed by `(employee_id, session_id)`. A filter runs before each write: it drops
  acknowledgements and duplicate resends, never stores prompts, policy text or tool payloads, stores blocked inputs as
  placeholders, and truncates long turns.
- **Streaming.** Provider SSE → server → client as chunked NDJSON events (`delta`, `ticket`, `replace`, `done`).
- **Thinking models.** Gemini 3 spends hidden reasoning tokens that count against `max_tokens` and are billed as output.
  We send `reasoning_effort=none` (`LLM_REASONING_EFFORT`) and bill `total − prompt` tokens as output.

## Recording the demo

Run the server in one terminal and the commands above in another (e.g. with `asciinema rec` or a screen recorder). For the
streaming comparison, `--compare` runs the same query non-streamed and then streamed, and prints a timing table.
**Make sure `.env` and your API key are not visible on screen.**

## Limitations (V1)

- Regex guards and quarantine are heuristics (one bypass was found and patched, see security notes). The guarantees come
  from tool scoping, the absence of dangerous tools, and the output guard.
- The router call adds about 3 s before streaming can start; the measured streaming gain is modest (see demo transcript).
- Static demo tokens instead of JWT/SSO; JSON-file "database"; single-process server.
