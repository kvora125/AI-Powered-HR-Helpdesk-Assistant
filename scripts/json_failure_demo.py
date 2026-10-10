"""Phase 2 step 6: deliberately trigger each JSON failure mode and show how the code handles it.

  python -m scripts.json_failure_demo            # part A (offline) + part B (live model)
  python -m scripts.json_failure_demo --offline  # part A only

Part A feeds hand-crafted bad outputs through the SAME validator the server uses.
Part B provokes real failures from the live model (tiny max_tokens, no JSON mode + a sloppy
prompt) and shows detection -> one corrective retry -> safe human-review fallback.
"""
import argparse
import json

from hr_helpdesk.schemas import TICKET_SCHEMA, validate_json_output

GOOD = {"category": "payroll", "urgency": "high", "requires_human_review": True, "needs_clarification": False,
        "clarification_question": None, "summary": "September salary not credited.",
        "urgency_reason": "Salary not received; rent due on the 5th."}

CRAFTED = [
    ("valid output", json.dumps(GOOD), "stop"),
    ("non-JSON preamble", "Sure! Here's the classification you asked for:\n" + json.dumps(GOOD) + "\nLet me know if you need more!", "stop"),
    ("missing field (no urgency)", json.dumps({k: v for k, v in GOOD.items() if k != "urgency"}), "stop"),
    ("renamed field (urgency -> priority)", json.dumps({("priority" if k == "urgency" else k): v for k, v in GOOD.items()}), "stop"),
    ("invented enum (category=salary_issue)", json.dumps({**GOOD, "category": "salary_issue"}), "stop"),
    ("wrong type (requires_human_review='yes')", json.dumps({**GOOD, "requires_human_review": "yes"}), "stop"),
    ("truncated output (max_tokens hit)", json.dumps(GOOD)[:70], "length"),
    ("plain prose, no JSON", "This looks like a payroll problem with high urgency.", "stop"),
]

SLOPPY_PROMPT = ("You triage HR tickets. Tell me the category, priority and whether a human needs to look at it. "
                 "Explain your reasoning first, then give the answer in JSON.")


def part_a():
    print("=" * 90 + "\nPART A — crafted failure modes through the production validator\n" + "=" * 90)
    for name, text, finish in CRAFTED:
        r = validate_json_output(text, TICKET_SCHEMA, finish)
        verdict = "ACCEPTED" + (" (salvaged, logged as repaired)" if r.repaired else "") if r.ok else "REJECTED -> retry, then fallback"
        print(f"\n• {name}\n  input : {text[:110]!r}{'…' if len(text) > 110 else ''}\n  issues: {r.summary()}\n  result: {verdict}")


def part_b():
    from hr_helpdesk.observability import RequestTrace
    from hr_helpdesk.service import HelpdeskService
    from hr_helpdesk.memory import ConversationStore

    svc = HelpdeskService(store=ConversationStore(":memory:"))
    text = "My September salary hasn't been credited and my rent is due on the 5th"
    print("\n" + "=" * 90 + "\nPART B — real model failures, provoked on purpose\n" + "=" * 90)
    scenarios = [
        ("B1 truncation: max_tokens=12", dict(max_tokens=12)),
        ("B2 no JSON mode + sloppy prompt (preamble / renamed fields / invented enums likely)",
         dict(json_mode=False, system_prompt=SLOPPY_PROMPT)),
        ("B3 production settings (6-part prompt + JSON mode)", dict()),
    ]
    for name, kwargs in scenarios:
        trace = RequestTrace(f"jsondemo-{name[:2]}", "E1001", endpoint="json_failure_demo")
        ticket, info = svc.classify_ticket(text, trace, **kwargs)
        trace.finish("success" if info["valid"] else "fallback")
        print(f"\n### {name}")
        for a in info["attempts"]:
            print(f"  attempt {a['attempt']}: status={a['status']} finish={a.get('finish_reason')} "
                  f"issues={[c for c, _ in a['issues']] or 'none'}\n    raw: {a.get('raw', '')[:160]!r}")
        print(f"  => {'VALID' if info['valid'] else 'FALLBACK (human review)'}: {json.dumps(ticket)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    part_a()
    if not args.offline:
        part_b()
