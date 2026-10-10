"""Authentication: "are you who you say you are?"

Every request must carry `Authorization: Bearer <client token>`. The server stores only SHA-256
hashes of tokens (data/auth_tokens.json) mapped to an employee_id, and compares in constant time.
A missing/unknown token is rejected with 401 BEFORE the model or any tool is touched.

The resolved employee_id then drives *authorization* (what you may access): every tool is scoped
to it in tools.py. In production this raw token would be replaced by a signed, expiring JWT
issued by the company SSO (claims: sub=employee_id, exp, aud), verified on every request.
"""
import hashlib
import hmac
import json

from .config import DATA_DIR


def _load() -> dict:
    return json.loads((DATA_DIR / "auth_tokens.json").read_text())["tokens"]


def authenticate(auth_header: str):
    """Returns employee_id or None."""
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    presented = hashlib.sha256(auth_header[7:].strip().encode()).hexdigest()
    match = None
    for stored_hash, employee_id in _load().items():
        if hmac.compare_digest(stored_hash, presented):   # constant-time comparison
            match = employee_id
    return match
