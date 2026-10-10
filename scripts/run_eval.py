"""Run the FIXED test set against the real model (in-process, same pipeline as the server).

  python -m scripts.run_eval --label before
  python -m scripts.run_eval --label after --only P3,P5
  python -m scripts.run_eval --compare before after

Results are written to docs/eval_results/<label>.json.
"""
import argparse
import json
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "tests" / "eval_cases.json"
OUT_DIR = ROOT / "docs" / "eval_results"


def check(expect: dict, r: dict) -> list:
    """Returns a list of failed expectations (empty = pass)."""
    fails = []
    reply = r.get("reply") or ""
    tools = [t["name"] for t in r.get("tools", [])]
    ok_tools = [t["name"] for t in r.get("tools", []) if t["status"] == "success"]
    ticket = r.get("ticket") or {}
    if "intent" in expect and r.get("intent") != expect["intent"]:
        fails.append(f"intent={r.get('intent')} expected {expect['intent']}")
    if "status" in expect and r.get("status") != expect["status"]:
        fails.append(f"status={r.get('status')} expected {expect['status']}")
    if expect.get("no_tools") and tools:
        fails.append(f"tools called: {tools}")
    if expect.get("no_successful_tools") and ok_tools:
        fails.append(f"tool succeeded unexpectedly: {ok_tools}")
    for t in expect.get("tools", []):
        if t not in ok_tools:
            fails.append(f"expected successful tool {t}, got {r.get('tools')}")
    for d in expect.get("policy_docs_include", []):
        if d not in r.get("policy_docs", []):
            fails.append(f"policy doc {d} not supplied (got {r.get('policy_docs')})")
    if expect.get("reply_contains_all") and not all(s.lower() in reply.lower() for s in expect["reply_contains_all"]):
        fails.append(f"reply missing all of {expect['reply_contains_all']}")
    if expect.get("reply_contains_any") and not any(s.lower() in reply.lower() for s in expect["reply_contains_any"]):
        fails.append(f"reply missing any of {expect['reply_contains_any']}")
    for s in expect.get("reply_not_contains", []):
        if s.lower() in reply.lower():
            fails.append(f"reply contains forbidden '{s}'")
    if "ticket_category" in expect and ticket.get("category") not in expect["ticket_category"]:
        fails.append(f"ticket category={ticket.get('category')} expected {expect['ticket_category']}")
    if "ticket_urgency" in expect and ticket.get("urgency") not in expect["ticket_urgency"]:
        fails.append(f"ticket urgency={ticket.get('urgency')} expected {expect['ticket_urgency']}")
    if expect.get("ticket_valid") and not ticket.get("schema_valid"):
        fails.append("ticket failed schema validation (fallback used)")
    if expect.get("ticket_human_review") and not ticket.get("requires_human_review"):
        fails.append("ticket should require human review")
    return fails


def run(label: str, only: set, pause: float):
    from hr_helpdesk.observability import RequestTrace
    from hr_helpdesk.prompts import PROMPT_VERSION
    from hr_helpdesk.service import HelpdeskService
    from hr_helpdesk import config

    svc = HelpdeskService()
    cases = [c for c in json.loads(CASES.read_text())["cases"] if not only or c["id"] in only]
    results = []
    for case in cases:
        session = f"eval-{label}-{case['id']}-{uuid.uuid4().hex[:6]}"
        r = {}
        for turn in case["turns"]:
            trace = RequestTrace(f"eval-{label}-{case['id']}-{uuid.uuid4().hex[:4]}", "E1001", endpoint="eval")
            r = svc.chat("E1001", turn, session, trace)
            summary = trace.finish(r["status"], intent=r["intent"], session_id=session)
            time.sleep(pause)
        fails = check(case["expect"], r)
        results.append({"id": case["id"], "req": case["req"], "query": case["turns"][-1], "pass": not fails,
                        "fails": fails, "intent": r.get("intent"), "status": r.get("status"),
                        "tools": r.get("tools"), "policy_docs": r.get("policy_docs"), "ticket": r.get("ticket"),
                        "reply": r.get("reply"), "cost_usd": summary["cost_usd"],
                        "tokens": [summary["input_tokens"], summary["output_tokens"]]})
        mark = "PASS" if not fails else "FAIL"
        print(f"{mark}  {case['id']:<3} {case['turns'][-1][:60]:<60} {'; '.join(fails)[:120]}")
    passed = sum(r["pass"] for r in results)
    print(f"\n{label}: {passed}/{len(results)} passed  (model={config.LLM_MODEL}, prompt={PROMPT_VERSION}, "
          f"cost=${sum(r['cost_usd'] for r in results):.5f})")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{label}.json").write_text(json.dumps({
        "label": label, "model": config.LLM_MODEL, "prompt_version": PROMPT_VERSION,
        "passed": passed, "total": len(results), "results": results}, indent=2))


def compare(a: str, b: str):
    ra = {r["id"]: r for r in json.loads((OUT_DIR / f"{a}.json").read_text())["results"]}
    rb = {r["id"]: r for r in json.loads((OUT_DIR / f"{b}.json").read_text())["results"]}
    print(f"{'case':<6}{a:>10}{b:>10}   change")
    for cid in ra:
        pa, pb = ra[cid]["pass"], rb.get(cid, {}).get("pass")
        change = "" if pa == pb else ("FIXED" if pb else "REGRESSION")
        print(f"{cid:<6}{'pass' if pa else 'FAIL':>10}{('pass' if pb else 'FAIL') if pb is not None else '-':>10}   {change}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--only", default="")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between turns (free-tier rate limits)")
    ap.add_argument("--compare", nargs=2)
    args = ap.parse_args()
    if args.compare:
        return compare(*args.compare)
    run(args.label, {s.strip() for s in args.only.split(",") if s.strip()}, args.pause)


if __name__ == "__main__":
    main()
