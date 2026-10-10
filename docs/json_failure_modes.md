# JSON failure modes: detection and handling (Phase 2, step 6)

Run: `python -m scripts.json_failure_demo` (full output: [`json_failure_demo_output.txt`](json_failure_demo_output.txt)).

The model's JSON is never trusted. `schemas.validate_json_output()` checks every router and ticket response against an explicit
schema (required keys, no extra keys, types, enums, max lengths) and labels the failure. Handling is the same everywhere:

1. **Detect** and label the failure (`truncated`, `non_json_preamble`, `invalid_json`, `missing_field`, `unknown_field`, `invalid_enum`, `wrong_type`).
2. **Re-ask once** with the exact validator errors ("Your previous output was rejected: invalid_enum: 'category'='salary_issue'…").
3. **Fall back safely.** Router → general answer with no tools. Ticket → `category=other, requires_human_review=true` with the reason recorded.
   The model's unvalidated output is never acted on.
4. **Log** each attempt as `event=schema_validation` with its issues.

## Part A: each failure mode through the production validator

| Failure mode | Example | Detected as | Outcome |
|---|---|---|---|
| Non-JSON preamble | `Sure! Here's the classification…{…}` | `non_json_preamble` | Object is complete and valid → salvaged, logged as `repaired` |
| Missing field | no `urgency` | `missing_field` | rejected → retry → fallback |
| Renamed field | `priority` instead of `urgency` | `unknown_field` + `missing_field` | rejected → retry → fallback |
| Invented enum | `category: "salary_issue"` | `invalid_enum` | rejected → retry → fallback |
| Wrong type | `requires_human_review: "yes"` | `wrong_type` | rejected → retry → fallback |
| Truncated | `{"category": "payroll", "urgency": "high", "requires_human_review": tr` + `finish_reason=length` | `truncated` | rejected (never salvaged) |
| Prose only | `This looks like a payroll problem…` | `invalid_json` | rejected → retry → fallback |

Preamble is the only failure we repair automatically. The JSON object itself is intact and fully validated, and it is
only *extracted* from the surrounding text, never guessed. Truncated output is never repaired, because a missing closing
brace can hide missing fields.

## Part B: failures provoked from the live model (gemini-3.8-flash)

| Scenario | What the model actually returned | Detected | Final |
|---|---|---|---|
| B1 `max_tokens=12` | `{"category":"payroll","urgency":"` (`finish_reason=length`, both attempts) | `truncated` ×2 | human-review fallback |
| B2 JSON mode off + sloppy prompt ("explain your reasoning first, then JSON"; asks for "priority") | attempt 1: `**Reasoning:** …` prose cut off at the limit; attempt 2: `{"category": "Payroll", "priority": "High", "human_review_required": true}` | attempt 1: `truncated`, `invalid_json`; attempt 2: **renamed fields** (`priority`, `human_review_required`), **invented enum** (`"Payroll"` with a capital P), **6 missing fields** | human-review fallback |
| B3 production (six-part prompt + JSON mode) | valid object, first attempt | none | `payroll` / `high` / human review |

B2 shows why "hope it works" fails: a reasonable-looking JSON object came back with three different contract violations.
Only the strict validator caught them.

**Real-world catch.** Before `reasoning_effort=none` was set, Gemini's hidden thinking tokens used up the output budget, and short
JSON calls came back as `finish_reason=length` with almost no content. The `truncated` check flagged it instead of passing on a
half-object.
