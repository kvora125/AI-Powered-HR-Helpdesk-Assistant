"""Regenerate docs/prompts.md from the live prompt code so the docs never drift: python -m scripts.export_prompts"""
from pathlib import Path

from hr_helpdesk.prompts import ASSISTANT_SECTIONS, CLASSIFIER_SECTIONS, PROMPT_VERSION, ROUTER_SECTIONS

OUT = Path(__file__).resolve().parent.parent / "docs" / "prompts.md"

PROMPTS = [
    ("A. Router prompt", "Runs on every chat turn. Returns JSON (validated against `ROUTER_SCHEMA`) that the backend uses to pick a deterministic path.", ROUTER_SECTIONS),
    ("B. Ticket-classifier prompt", "Returns the ticket JSON (validated against `TICKET_SCHEMA`; one corrective retry; then a human-review fallback).", CLASSIFIER_SECTIONS),
    ("C. Assistant prompt", "Writes the conversational, streamed answer. The backend appends a trusted *Session context* block (employee ID, name, date, canary marker) at runtime.", ASSISTANT_SECTIONS),
]

if __name__ == "__main__":
    parts = [f"# System prompts ({PROMPT_VERSION})\n",
             "Generated from `hr_helpdesk/prompts.py` by `python -m scripts.export_prompts`. "
             "Each prompt is labelled with the six components from class: **1. Role & task, 2. Allowed evidence, "
             "3. Output contract, 4. Behavior rules, 5. Failure behavior, 6. Examples.**\n"]
    for title, note, sections in PROMPTS:
        parts.append(f"\n---\n\n## {title}\n\n_{note}_\n")
        for name, body in sections.items():
            parts.append(f"\n### {name}\n\n```text\n{body.strip()}\n```\n")
    OUT.write_text("".join(parts))
    print(f"wrote {OUT}")
