# Diagnosis write-up — diagnose before you change

**Fixed test set:** [`tests/eval_cases.json`](../tests/eval_cases.json). This is the Phase 1 query list plus extras, 23 cases covering all four
business requirements, authorization, out-of-scope and security. It was written and run **before** any change.
Runner: `python -m scripts.run_eval --label <name>`. Every run is saved in [`docs/eval_results/`](eval_results/).
Model: `gemini-3.8-flash` (thinking off). The process was one change per run, and the full set was re-run after each change.

| Run | Change | Prompt | Result |
|---|---|---|---|
| `before` | — (baseline) | v1.0 | **20/23** — R1, R3, T2 fail |
| `after_fix1_code` | echo Gemini tool-call objects verbatim (code) | v1.0 | 20/23 |
| `attempt_prompt_edit_reverted` | stricter employee_id rule + tool few-shot (prompt) | v1.0+edit | 20/23 → **reverted** |
| `after` | send ONE merged system message (context) | v1.0 | **22/23** — R1, R3 fixed, 0 regressions |
| `before_f2_run1/2` | G2 amended, G3 added (see finding 2) | v1.0 | G2 fails 2/2 |
| `after_f2` / `final` | allow general HR concepts (prompt) | v1.1 | **23/24** — G2 fixed, 0 regressions |

---

## Finding 1 (main): leave-balance and claim lookups asked the employee for their ID

**Symptom (R1, R3).** "How many casual leaves do I have left?" sent `get_leave_balance` an empty `employee_id`
(the tool layer returned `needs_input`). In the claim follow-up the assistant replied *"Could you also share your Employee
ID?"*, even though the employee had already authenticated.

**The logs also showed a second, unrelated error.** The follow-up model call failed with
`HTTP 400: Function call is missing a thought_signature`. Gemini 3 attaches a signature to each tool call, and my code
rebuilt the tool-call message from scratch and dropped it. This is a code integration bug, so I fixed it first, on its own:
the code now echoes the provider's tool-call objects back verbatim. The 400 disappeared, but R1/R3 still failed (20/23), so
it wasn't the root cause.

**First diagnosis (wrong): "instruction problem".** The prompt *does* contain `Authenticated employee_id: E1001`, so it looked like
the model received the right context and ignored a nuanced instruction. I made the rule explicit and added a tool-call
few-shot example. **Result: no change (20/23).** A fix that does nothing is evidence that the diagnosis is wrong, so I
reverted it rather than piling on more prompt text.

**Isolating experiments.** These held everything constant except one variable, with 3 trials each:

| Messages sent to the model | Result |
|---|---|
| assistant prompt only | tool called with `E1001`, 3/3 |
| assistant prompt **+ policy text as a 2nd system message** (what the app actually sent) | asks for employee ID, 3/3 |
| assistant prompt + policy text **merged into one** system message | tool called with `E1001`, 3/3 |
| Probe: "what is your authenticated employee_id?" with 2 system messages | **"NONE"** |

Swapping models/settings (`gemini-3.8-flash` with thinking none/low, `gemini-3.5-flash-lite`) all worked when given one
system message, so this was **not a model-capability limit**.

**Root cause: missing context.** Gemini's OpenAI-compatible endpoint keeps only the **last** system message. For
"casual leave" or "RMB-2077", keyword matching also attached a policy document as a *second* system message. That
silently replaced the whole assistant prompt: role, rules, and the session block holding the employee ID. The model
never saw the ID, so asking for it was the correct, safe behaviour given what it received. (The same bug also meant policy
answers were being generated without the assistant's behaviour rules. They happened to pass because the policy text
itself was enough.)

**Fix (supply the missing context).** `service.py` now builds **one** system message: assistant prompt + delimited policy
documents. I added a unit-test regression check that asserts exactly one system message containing both the session
context and the policy text.

**Before/after on the fixed set:** 20/23 → **22/23**. R1 and R3 FAIL → pass; every previously passing case still passes.
A repeat run of R1, R3, P2, P5 passed 4/4.

## Finding 2: general HR questions were refused (found in the demo, not by the test set)

**Symptom.** During the demo recording, "What's the difference between gross and net salary?" got *"The policy documents
provided to me don't cover payroll definitions… would you like me to raise a ticket?"*. Case G2 had **passed**, because it
only checked `intent` and `status`. **Test-set gap:** I amended G2 with answer-quality checks and added G3, both marked
`_amended` in the JSON, then re-ran them on the *unchanged* v1.0 prompt: G2 failed 2/2 runs.

**Diagnosis: behaviour/instruction problem.** No context is missing, because a generic concept needs no company data.
The model followed the "Allowed evidence" section *exactly*: "You may use ONLY (a) conversation (b) policy documents
(c) tool results". The instruction was wrong for `general_hr`, not ignored.

**Fix (prompt v1.1).** Allowed evidence (d): textbook HR concepts that are the same at any company may be explained
directly. Company-specific facts must still come from policies/tools. I also added one worked example (CTC vs take-home).

**Before/after:** G2 FAIL → pass. Full set 23/24 with zero regressions; policy answers still cite the supplied text and do
not drift into "typical company" rules (P1–P5 pass).

**A test-oracle error, recorded honestly.** G3's first check forbade the phrase "raise a ticket". On v1.1 it "failed",
but the reply was correct: it explained notice periods, then offered a ticket for Acme's *specific* terms. The saved v1.0
replies did the same. So G3 was never a real failure. I corrected its check to require the substance ("resign"), and it
does not count as a fix.

## Still failing (left open on purpose): T2 urgency

"I can't log in to the HRMS portal… so I can't apply for leave" is classified `it_access` / **low**; expected medium/high.
**Diagnosis: instruction (rubric gap).** The urgency rubric covers "completely blocked from working" (high) and stated
deadlines, but has no rule for a blocked HR self-service process. The model applied the rubric literally, and its
`urgency_reason` says so: "no stated deadline or complete work stoppage". The proposed fix is a rubric line, e.g. *"medium:
cannot access an HR system needed to complete a request (leave, claims, payslips)"*. I'm leaving it as the next change, so
that this round measured one change at a time.

## Lessons

1. The first plausible category ("it ignored the instruction") was wrong. The cheap probe "what is your employee_id?" would
   have found the real cause in one call. **Verify the model actually received the context before editing the prompt.**
2. A fix that changes nothing should be reverted, not stacked.
3. Pass/fail checks on intent and status alone hid a bad answer. Assertions need to check answer *quality*, and an over-strict
   oracle can create false failures too.
