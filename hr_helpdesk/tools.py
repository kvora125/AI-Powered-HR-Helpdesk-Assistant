"""Deterministic layer: the ONLY way employee data reaches the model.

Security properties enforced here in code (not in the prompt):
  * Authorization/scoping: every tool receives the authenticated user_id from the server, and
    refuses any employee_id that is not that user (the model can't widen its own access).
  * Least privilege: tools are read-only and project away sensitive fields (salary, bank, phone).
    There is no tool that approves, edits, lists all employees, or returns salary.
  * Argument provenance: a claim_id must actually appear in the employee's own messages,
    so the model cannot invent one.
"""
import json
import re
from dataclasses import dataclass, field

from .config import DATA_DIR

CLAIM_ID_RE = re.compile(r"\bRMB-\d{4}\b", re.IGNORECASE)


def load_employees() -> dict:
    return json.loads((DATA_DIR / "employees.json").read_text())["employees"]


@dataclass
class ToolContext:
    user_id: str                     # authenticated employee (from the auth layer, never the model)
    user_messages: list = field(default_factory=list)   # this conversation's user turns (provenance check)


@dataclass
class ToolResult:
    status: str                      # "success" | "error" | "denied" | "needs_input"
    data: dict = None
    error: str = None

    def to_model_payload(self) -> str:
        if self.status == "success":
            return json.dumps({"status": "success", "data": self.data})
        return json.dumps({"status": self.status, "error": self.error})


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_leave_balance",
            "description": "Get the authenticated employee's own leave balance for the current leave year.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Authenticated employee_id from session context, e.g. E1001"},
                    "leave_type": {"type": "string", "enum": ["casual", "sick", "earned", "all"]},
                },
                "required": ["employee_id", "leave_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_reimbursement_status",
            "description": "Get the status of ONE of the authenticated employee's reimbursement claims. "
                           "Requires a claim_id the employee typed (format RMB-####). Ask the employee if missing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Authenticated employee_id from session context"},
                    "claim_id": {"type": "string", "description": "Claim ID given by the employee, e.g. RMB-2041"},
                },
                "required": ["employee_id", "claim_id"],
            },
        },
    },
]
TOOLS_BY_RECORD_TYPE = {
    "leave_balance": ["get_leave_balance"],
    "reimbursement_status": ["get_reimbursement_status"],
}


def specs_for(names) -> list:
    return [t for t in TOOL_SPECS if t["function"]["name"] in names]


# ------------------------------------------------------------------------------- tools
def get_leave_balance(employee_id: str, user_id: str, leave_type: str = "all") -> ToolResult:
    if employee_id != user_id:
        return ToolResult("denied", error="Authorization denied: you can only view your own leave balance.")
    emp = load_employees().get(user_id)
    if not emp:
        return ToolResult("error", error="Employee record not found.")
    bal = emp["leave_balance"]
    if leave_type in ("casual", "sick", "earned"):
        return ToolResult("success", {"employee_id": user_id, "leave_year": bal["leave_year"], leave_type: bal[leave_type]})
    if leave_type != "all":
        return ToolResult("error", error=f"Unknown leave_type '{leave_type}'.")
    return ToolResult("success", {"employee_id": user_id, **bal})


def get_reimbursement_status(employee_id: str, user_id: str, claim_id: str) -> ToolResult:
    if employee_id != user_id:
        return ToolResult("denied", error="Authorization denied: you can only view your own claims.")
    emp = load_employees().get(user_id)
    if not emp:
        return ToolResult("error", error="Employee record not found.")
    for claim in emp["reimbursements"]:
        if claim["claim_id"].upper() == claim_id.upper():
            return ToolResult("success", {"employee_id": user_id, **claim})
    # Same message whether the claim doesn't exist or belongs to someone else (no enumeration).
    return ToolResult("error", error=f"No claim {claim_id.upper()} found on your account.")


_IMPLS = {"get_leave_balance": get_leave_balance, "get_reimbursement_status": get_reimbursement_status}


def execute_tool(name: str, raw_args: str, ctx: ToolContext, allowed: list) -> ToolResult:
    """Validate a model-requested tool call, then run it scoped to ctx.user_id."""
    if name not in allowed or name not in _IMPLS:
        return ToolResult("denied", error=f"Tool '{name}' is not available for this request.")
    try:
        args = json.loads(raw_args or "{}")
        assert isinstance(args, dict)
    except (json.JSONDecodeError, AssertionError):
        return ToolResult("error", error="Tool arguments were not a valid JSON object.")

    employee_id = str(args.get("employee_id", "")).strip().upper()
    if not employee_id:
        return ToolResult("needs_input", error="employee_id missing; use the authenticated employee_id from session context.")

    if name == "get_leave_balance":
        return get_leave_balance(employee_id, ctx.user_id, str(args.get("leave_type", "all")).lower())

    claim_id = str(args.get("claim_id", "")).strip().upper()
    typed = {m.upper() for msg in ctx.user_messages for m in CLAIM_ID_RE.findall(msg)}
    if not claim_id or not CLAIM_ID_RE.fullmatch(claim_id):
        return ToolResult("needs_input", error="claim_id missing or malformed; ask the employee for their RMB-#### claim ID.")
    if claim_id not in typed:
        return ToolResult("needs_input", error=f"claim_id {claim_id} was not provided by the employee; ask them for it instead of guessing.")
    return get_reimbursement_status(employee_id, ctx.user_id, claim_id)
