"""Output contracts between the model and the backend, plus a strict validator.

The validator detects every JSON failure mode discussed in class and labels it:
  truncated          - finish_reason=length, or braces never close
  non_json_preamble  - prose before/after the JSON object ("Sure! Here is the JSON: {...}")
  invalid_json       - not parseable at all
  missing_field      - a required key is absent
  unknown_field      - an extra key (usually a *renamed* field, e.g. "priority" instead of "urgency")
  invalid_enum       - a value outside the allowed set (e.g. category "salary_issue")
  wrong_type         - e.g. requires_human_review: "yes" instead of true
"""
import json
from dataclasses import dataclass, field

TICKET_CATEGORIES = ["leave", "payroll", "benefits", "reimbursement", "it_access",
                     "policy_query", "workplace_concern", "onboarding_offboarding", "other"]
URGENCY_LEVELS = ["low", "medium", "high", "critical"]

# --- Contract 1: HR ticket classification ----------------------------------------------
TICKET_SCHEMA = {
    "category": {"type": str, "enum": TICKET_CATEGORIES},
    "urgency": {"type": str, "enum": URGENCY_LEVELS},
    "requires_human_review": {"type": bool},
    "needs_clarification": {"type": bool},
    "clarification_question": {"type": str, "nullable": True},
    "summary": {"type": str, "max_len": 200},
    "urgency_reason": {"type": str, "max_len": 300},
}

# --- Contract 2: per-turn router (decides what the backend does next) -------------------
INTENTS = ["general_hr", "policy_question", "personal_record", "ticket", "restricted", "out_of_scope"]
POLICY_TOPICS = ["leave", "wfh", "reimbursement"]
RECORD_TYPES = ["leave_balance", "reimbursement_status", "none"]

ROUTER_SCHEMA = {
    "intent": {"type": str, "enum": INTENTS},
    "policy_topics": {"type": list, "item_enum": POLICY_TOPICS},
    "record_type": {"type": str, "enum": RECORD_TYPES},
    "refers_to_other_employee": {"type": bool},
    "reason": {"type": str, "max_len": 300},
}


@dataclass
class ValidationReport:
    ok: bool
    data: dict = None
    issues: list = field(default_factory=list)   # [(code, detail)]
    repaired: bool = False                       # true if we salvaged JSON out of surrounding prose/fences

    def summary(self) -> str:
        return "; ".join(f"{c}: {d}" for c, d in self.issues) or "valid"


def _extract_object(text: str):
    """Return (json_substring, had_extra_text, balanced). Scans for the first balanced {...}."""
    start = text.find("{")
    if start == -1:
        return None, bool(text.strip()), False
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                extra = text[:start].strip() + text[i + 1:].strip()
                return text[start:i + 1], bool(extra), True
    return text[start:], bool(text[:start].strip()), False


def validate_json_output(text: str, schema: dict, finish_reason: str = "stop") -> ValidationReport:
    issues = []
    raw = (text or "").strip()
    if finish_reason == "length":
        issues.append(("truncated", "model hit max_tokens (finish_reason=length)"))

    # Code fences are a cosmetic wrapper: strip them, but anything else around the JSON is flagged.
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:] if raw.lower().startswith("json") else raw
        raw = raw.strip()

    candidate, had_extra, balanced = _extract_object(raw)
    if candidate is None:
        issues.append(("invalid_json", "no JSON object found"))
        return ValidationReport(False, None, issues)
    if not balanced:
        if not any(c == "truncated" for c, _ in issues):
            issues.append(("truncated", "JSON object never closes"))
        return ValidationReport(False, None, issues)
    if had_extra:
        issues.append(("non_json_preamble", "text found outside the JSON object"))

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        issues.append(("invalid_json", str(e)))
        return ValidationReport(False, None, issues)
    if not isinstance(data, dict):
        issues.append(("invalid_json", "top-level value is not an object"))
        return ValidationReport(False, None, issues)

    for key in data:
        if key not in schema:
            issues.append(("unknown_field", f"unexpected field '{key}' (renamed field?)"))
    for key, rule in schema.items():
        if key not in data:
            issues.append(("missing_field", f"'{key}' is required"))
            continue
        value = data[key]
        if value is None:
            if not rule.get("nullable"):
                issues.append(("wrong_type", f"'{key}' must not be null"))
            continue
        expected = rule["type"]
        if expected is bool and not isinstance(value, bool):
            issues.append(("wrong_type", f"'{key}' must be true/false, got {value!r}"))
            continue
        if expected is not bool and not isinstance(value, expected):
            issues.append(("wrong_type", f"'{key}' must be {expected.__name__}, got {type(value).__name__}"))
            continue
        if "enum" in rule and value not in rule["enum"]:
            issues.append(("invalid_enum", f"'{key}'={value!r} not in {rule['enum']}"))
        if "item_enum" in rule:
            bad = [v for v in value if v not in rule["item_enum"]]
            if bad:
                issues.append(("invalid_enum", f"'{key}' has values {bad} not in {rule['item_enum']}"))
        if "max_len" in rule and isinstance(value, str) and len(value) > rule["max_len"]:
            issues.append(("wrong_type", f"'{key}' longer than {rule['max_len']} chars"))

    # Preamble alone is recoverable: the object itself was complete and valid, so we accept
    # the salvaged object but record that a repair happened (it is logged).
    blocking = [c for c, _ in issues if c != "non_json_preamble"]
    return ValidationReport(ok=not blocking, data=data if not blocking else None,
                            issues=issues, repaired=had_extra and not blocking)


def safe_ticket_fallback(text: str, issues: str) -> dict:
    """Used when the model still fails validation after a retry: never guess, route to a human."""
    return {
        "category": "other",
        "urgency": "medium",
        "requires_human_review": True,
        "needs_clarification": False,
        "clarification_question": None,
        "summary": (text or "")[:180],
        "urgency_reason": f"Automatic classification failed validation ({issues[:200]}); defaulted for human triage.",
    }
