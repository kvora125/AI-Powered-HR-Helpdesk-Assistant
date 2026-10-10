"""The request pipeline. HTTP-agnostic so it can be unit-tested with a fake LLM.

  auth (server) -> input validation -> catastrophic-action guard -> gibberish filter
  -> ROUTER model call (JSON, validated) -> deterministic branch:
        out_of_scope / restricted  -> canned reply, no tools, no further model calls
        ticket                     -> CLASSIFIER model call (JSON, validated, retry, fallback)
        personal_record            -> ASSISTANT + scoped tools -> tool loop -> final answer
        policy_question/general_hr -> ASSISTANT + supplied policy text -> answer
  -> response-status check -> output guard -> memory filter + store -> respond
"""
import json
import time
import uuid
from datetime import date

from . import config, guardrails, policies
from .llm import LLMClient, STATUS_SUCCESS, STATUS_INCOMPLETE
from .memory import ConversationStore
from .prompts import CLASSIFIER_PROMPT, PROMPT_VERSION, ROUTER_PROMPT, assistant_prompt
from .schemas import ROUTER_SCHEMA, TICKET_SCHEMA, safe_ticket_fallback, validate_json_output
from .tools import TOOLS_BY_RECORD_TYPE, ToolContext, execute_tool, load_employees, specs_for

OUT_OF_SCOPE_REPLY = ("I'm the Acme HR helpdesk assistant, so I can only help with HR topics — leave, work-from-home, "
                      "reimbursements, payroll questions, your own leave balance or claim status, or raising an HR ticket.")
RESTRICTED_REPLY = ("I can't help with that: I can't share other employees' data, change or approve records, or reveal "
                    "my internal instructions. I can help with your own records, HR policies, or raise a ticket.")
ERROR_REPLY = "Sorry — I couldn't complete that request right now. Please try again in a moment, or raise a ticket with HR."
MAX_TOOL_ROUNDS = 3


def _noop(_event):
    pass


class HelpdeskService:
    def __init__(self, llm: LLMClient = None, store: ConversationStore = None):
        self.llm = llm or LLMClient()
        self.store = store or ConversationStore()

    # ============================================================ ticket classification
    def classify_ticket(self, text: str, trace, max_tokens: int = 300, json_mode: bool = True,
                        system_prompt: str = CLASSIFIER_PROMPT) -> tuple:
        """Returns (ticket_dict, validation_info). Never raises, never returns unvalidated data."""
        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Employee request:\n<request>\n{text}\n</request>"}]
        attempts = []
        for attempt in (1, 2):
            res = self.llm.complete(messages, json_mode=json_mode, max_tokens=max_tokens, temperature=0,
                                    purpose=f"classify_attempt_{attempt}")
            trace.llm(res)
            if res.status not in (STATUS_SUCCESS, STATUS_INCOMPLETE):     # hard error: don't parse garbage
                attempts.append({"attempt": attempt, "status": res.status, "issues": [["llm_error", res.error]]})
                continue
            report = validate_json_output(res.content, TICKET_SCHEMA, res.finish_reason)
            attempts.append({"attempt": attempt, "status": res.status, "finish_reason": res.finish_reason,
                             "issues": report.issues, "repaired": report.repaired, "raw": res.content[:400]})
            trace.event("schema_validation", schema="ticket", attempt=attempt, ok=report.ok,
                        issues=report.issues, repaired=report.repaired)
            if report.ok:
                return report.data, {"valid": True, "attempts": attempts, "fallback_used": False}
            # Feed the concrete validation errors back once (re-ask), instead of hoping.
            messages = messages + [
                {"role": "assistant", "content": res.content or ""},
                {"role": "user", "content": f"Your previous output was rejected by the schema validator: {report.summary()}. "
                                            "Return ONLY the corrected JSON object with exactly the required keys and allowed values."}]
        issues = "; ".join(f"{c}" for a in attempts for c, _ in a["issues"]) or "unknown"
        return safe_ticket_fallback(text, issues), {"valid": False, "attempts": attempts, "fallback_used": True}

    def save_ticket(self, user_id: str, text: str, ticket: dict, validation: dict) -> str:
        ticket_id = f"HR-{uuid.uuid4().hex[:6].upper()}"
        config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.RUNTIME_DIR / "tickets.jsonl", "a") as f:
            f.write(json.dumps({"ticket_id": ticket_id, "employee_id": user_id, "created_at": time.time(),
                                "request_text": text, "classification": ticket,
                                "validation_valid": validation["valid"]}) + "\n")
        return ticket_id

    # ============================================================ routing
    def route(self, message: str, history: list, trace) -> dict:
        transcript = "\n".join(f"{t['role']}: {t['content'][:300]}" for t in history[-4:]) or "(none)"
        messages = [{"role": "system", "content": ROUTER_PROMPT},
                    {"role": "user", "content": f"Conversation so far:\n{transcript}\n\nLatest employee message:\n<message>\n{message}\n</message>"}]
        for attempt in (1, 2):
            res = self.llm.complete(messages, json_mode=True, max_tokens=200, temperature=0, purpose=f"route_attempt_{attempt}")
            trace.llm(res)
            if res.status != STATUS_SUCCESS:
                continue
            report = validate_json_output(res.content, ROUTER_SCHEMA, res.finish_reason)
            trace.event("schema_validation", schema="router", attempt=attempt, ok=report.ok, issues=report.issues)
            if report.ok:
                return {**report.data, "router_valid": True}
            messages = messages + [{"role": "assistant", "content": res.content},
                                   {"role": "user", "content": f"Invalid output: {report.summary()}. Return only the corrected JSON."}]
        # Safe fallback: no tools, answer generically with keyword-selected policy text.
        return {"intent": "general_hr", "policy_topics": [], "record_type": "none",
                "refers_to_other_employee": False, "reason": "router failed validation; safe fallback", "router_valid": False}

    # ============================================================ chat
    def chat(self, user_id: str, message: str, session_id: str, trace, stream: bool = False, emit=_noop) -> dict:
        message = (message or "").strip()
        emp = load_employees().get(user_id, {})
        history = self.store.history(user_id, session_id)
        trace.extra["prompt_version"] = PROMPT_VERSION
        result = {"request_id": trace.request_id, "intent": None, "tools": [], "ticket": None,
                  "policy_docs": [], "prompt_version": PROMPT_VERSION}

        def finish(reply: str, status: str, blocked_rule: str = None, streamed: bool = False):
            if not streamed:
                emit({"type": "delta", "text": reply})
            mem = self.store.store_exchange(user_id, session_id, message, reply,
                                            meta={"intent": result["intent"], "request_id": trace.request_id},
                                            blocked_rule=blocked_rule)
            trace.event("memory_write", **mem)
            result.update(reply=reply, status=status, guardrail=blocked_rule)
            return result

        # 1. Input validation (cheap, deterministic)
        if not message:
            return finish("Please type a question.", "rejected_input")
        if len(message) > config.MAX_USER_MESSAGE_CHARS:
            return finish(f"Your message is too long (max {config.MAX_USER_MESSAGE_CHARS} characters).", "rejected_input")

        # 2. Catastrophic-action guard: blocked requests never reach the model or the tools.
        guard = guardrails.check_input(message, user_id)
        if guard.blocked:
            result["intent"] = "restricted"
            trace.event("guardrail_block", stage="input", rule=guard.rule_id)
            return finish(guard.message, "refused", blocked_rule=guard.rule_id)

        # 3. Gibberish never costs a model call.
        if guardrails.looks_like_gibberish(message):
            result["intent"] = "out_of_scope"
            trace.event("declined", reason="gibberish_prefilter")
            return finish(OUT_OF_SCOPE_REPLY, "declined_out_of_scope")

        # 4. Router (structured output) decides the deterministic path.
        route = self.route(message, history, trace)
        result["intent"] = route["intent"]
        trace.extra["route"] = route
        trace.event("routed", **route)

        if route["intent"] == "out_of_scope":
            return finish(OUT_OF_SCOPE_REPLY, "declined_out_of_scope")
        if route["intent"] == "restricted" or route["refers_to_other_employee"]:
            trace.event("guardrail_block", stage="router", rule="router_restricted")
            return finish(RESTRICTED_REPLY, "refused", blocked_rule="router_restricted")

        if route["intent"] == "ticket":
            ticket, validation = self.classify_ticket(message, trace)
            ticket_id = self.save_ticket(user_id, message, ticket, validation)
            result["ticket"] = {"ticket_id": ticket_id, **ticket, "schema_valid": validation["valid"]}
            emit({"type": "ticket", "ticket": result["ticket"]})
            if ticket["needs_clarification"]:
                reply = f"I've opened ticket {ticket_id}, but HR will need more detail: {ticket['clarification_question']}"
            else:
                reply = (f"I've logged ticket {ticket_id} for the HR team — category: {ticket['category']}, "
                         f"urgency: {ticket['urgency']}. {'An HR agent will review it personally. ' if ticket['requires_human_review'] else ''}"
                         f"Summary: {ticket['summary']}")
            return finish(reply, "success")

        # 5. Build the assistant context: policy text (supplied, not guessed) + memory + message.
        topics = list(dict.fromkeys(route["policy_topics"] + policies.keyword_topics(message)))
        if route["intent"] == "policy_question" and not topics:
            topics = ["leave", "wfh", "reimbursement"]
        policy_ctx, docs, quarantined = policies.build_policy_context(topics)
        result["policy_docs"] = docs
        if quarantined:
            trace.event("policy_injection_quarantined", lines=quarantined)
        trace.event("context_built", policy_docs=docs, history_turns=len(history))

        # ONE system message. Some OpenAI-compatible endpoints (Gemini's) keep only the LAST system
        # message, so a separate policy message silently dropped the whole assistant prompt,
        # including the session's employee_id (root cause in docs/diagnosis.md).
        system = assistant_prompt(user_id, emp.get("name", "Employee"), date.today().isoformat())
        if policy_ctx:
            system += "\n\n## Supplied policy documents\n" + policy_ctx
        messages = [{"role": "system", "content": system}]
        messages += history + [{"role": "user", "content": message}]

        # 6. Tool loop (only for personal_record; tools scoped to this user in code).
        tool_specs = None
        if route["intent"] == "personal_record":
            allowed = TOOLS_BY_RECORD_TYPE.get(route["record_type"]) or [n for v in TOOLS_BY_RECORD_TYPE.values() for n in v]
            tool_specs = specs_for(allowed)
            ctx = ToolContext(user_id=user_id, user_messages=[t["content"] for t in history if t["role"] == "user"] + [message])
            for rnd in range(1, MAX_TOOL_ROUNDS + 1):
                res = self.llm.complete(messages, tools=tool_specs, max_tokens=400, purpose=f"tool_round_{rnd}")
                trace.llm(res)
                if res.status != STATUS_SUCCESS:       # check status before acting on a tool call
                    return finish(ERROR_REPLY, "error")
                if not res.tool_calls:
                    # The model answered without a tool (e.g. it asked for a missing claim ID).
                    return self._final_text(res.content, res.status, user_id, trace, finish, result)
                # Echo the provider's tool-call objects back unchanged (keeps provider metadata such as
                # Gemini's thought_signature); fall back to a rebuilt OpenAI-format list.
                messages.append({"role": "assistant", "content": res.content or None, "tool_calls": res.raw_tool_calls or [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in res.tool_calls]})
                for tc in res.tool_calls:
                    t0 = time.perf_counter()
                    out = execute_tool(tc["name"], tc["arguments"], ctx, allowed)
                    trace.tool(tc["name"], tc["arguments"], out.status, round((time.perf_counter() - t0) * 1000, 2), out.error)
                    if out.status == "denied":
                        trace.event("authorization_denied", tool=tc["name"], arguments=tc["arguments"])
                    result["tools"].append({"name": tc["name"], "status": out.status})
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out.to_model_payload()})
                if stream:
                    break     # one tool round, then stream the final answer token-by-token below
            # fall through: tool results are in `messages`; the model must now answer in text.

        # 7. Final answer: streamed token-by-token, or in one piece.
        if stream:
            res = self.llm.stream(messages, on_delta=lambda d: emit({"type": "delta", "text": d}),
                                  tools=tool_specs, purpose="answer_stream")
            trace.llm(res)
            return self._final_text(res.content, res.status, user_id, trace, finish, result, streamed=True, emit=emit)
        res = self.llm.complete(messages, tools=tool_specs, tool_choice="none" if tool_specs else None,
                                max_tokens=700, temperature=0.2, purpose="answer")
        trace.llm(res)
        return self._final_text(res.content, res.status, user_id, trace, finish, result)

    def _final_text(self, text, status, user_id, trace, finish, result, streamed=False, emit=_noop):
        # Response-status check: only a complete, successful response is shown as-is.
        if status == STATUS_INCOMPLETE and text:
            text += "\n\n(My reply was cut off — please ask me to continue or rephrase.)"
        elif status != STATUS_SUCCESS or not text.strip():
            if streamed:
                emit({"type": "replace", "text": ERROR_REPLY})
            return finish(ERROR_REPLY, "error", streamed=streamed)
        # Output guard: last line of defence for leaks (prompt canary, other employees' data).
        out_guard = guardrails.check_output(text, user_id)
        if out_guard.blocked:
            trace.event("guardrail_block", stage="output", rule=out_guard.rule_id)
            if streamed:
                emit({"type": "replace", "text": out_guard.message})
            return finish(out_guard.message, "refused", streamed=streamed)
        return finish(text, "success" if status == STATUS_SUCCESS else "incomplete", streamed=streamed)
