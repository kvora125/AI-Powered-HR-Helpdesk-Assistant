"""Persistent conversation memory (SQLite file — survives server restarts).

A filtering step runs BEFORE anything is stored, so we don't dump the raw conversation:
  * only the user message and the final assistant reply are stored — never system prompts,
    policy text, or raw tool payloads (those are re-derived deterministically each turn);
  * exact duplicate user messages (same text re-sent) are dropped;
  * pure acknowledgements ("ok", "thanks") and their short replies are dropped;
  * messages blocked by a guardrail are stored as a placeholder, so an injection attempt is
    not replayed into future prompts;
  * long messages are truncated.
Memory is keyed by (employee_id, session_id): one employee can never load another's history.
"""
import hashlib
import json
import re
import sqlite3
import threading
import time

from . import config

ACK_RE = re.compile(r"^(ok(ay)?|thanks?( you)?|thx|ty|cool|great|got it|sure|fine|noted|👍)[.! ]*$", re.IGNORECASE)
MAX_STORED_CHARS = 800


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


class ConversationStore:
    def __init__(self, path=None):
        self.path = path or (config.RUNTIME_DIR / "memory.db")
        if str(self.path) != ":memory:":
            config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT NOT NULL, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
            meta TEXT, created_at REAL NOT NULL)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_turns ON turns(employee_id, session_id, id)")
        self._db.commit()

    def _last(self, employee_id, session_id, role):
        return self._db.execute(
            "SELECT content_hash FROM turns WHERE employee_id=? AND session_id=? AND role=? ORDER BY id DESC LIMIT 1",
            (employee_id, session_id, role)).fetchone()

    def store_exchange(self, employee_id: str, session_id: str, user_text: str, assistant_text: str,
                       meta: dict = None, blocked_rule: str = None) -> dict:
        """Apply the filter, then persist. Returns a dict describing what was kept/dropped (logged)."""
        user_text, assistant_text = _norm(user_text), _norm(assistant_text)
        if not user_text or not assistant_text:
            return {"stored": False, "reason": "empty"}
        if ACK_RE.match(user_text) and len(assistant_text) < 200:
            return {"stored": False, "reason": "acknowledgement"}
        if blocked_rule:
            user_text = f"[message withheld: blocked by guardrail {blocked_rule}]"
        user_hash = hashlib.sha256(user_text.lower().encode()).hexdigest()
        with self._lock:
            last = self._last(employee_id, session_id, "user")
            if last and last[0] == user_hash:
                return {"stored": False, "reason": "duplicate_of_previous_user_turn"}
            now = time.time()
            for role, text in (("user", user_text), ("assistant", assistant_text)):
                truncated = text if len(text) <= MAX_STORED_CHARS else text[:MAX_STORED_CHARS] + " …[truncated]"
                self._db.execute(
                    "INSERT INTO turns(employee_id, session_id, role, content, content_hash, meta, created_at) VALUES (?,?,?,?,?,?,?)",
                    (employee_id, session_id, role, truncated,
                     hashlib.sha256(truncated.lower().encode()).hexdigest() if role == "assistant" else user_hash,
                     json.dumps(meta or {}), now))
            self._db.commit()
        return {"stored": True, "reason": "ok" if not blocked_rule else "stored_placeholder_for_blocked_input"}

    def history(self, employee_id: str, session_id: str, turns: int = config.HISTORY_TURNS) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT role, content FROM turns WHERE employee_id=? AND session_id=? ORDER BY id DESC LIMIT ?",
                (employee_id, session_id, turns * 2)).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    def clear(self, employee_id: str, session_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM turns WHERE employee_id=? AND session_id=?", (employee_id, session_id))
            self._db.commit()
