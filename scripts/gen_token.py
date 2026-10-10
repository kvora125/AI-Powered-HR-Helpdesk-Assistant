"""Issue a new client token for an employee: python -m scripts.gen_token E1001
Prints the plaintext token once; only its SHA-256 hash is stored in data/auth_tokens.json."""
import hashlib
import json
import secrets
import sys

from hr_helpdesk.config import DATA_DIR

if __name__ == "__main__":
    employee_id = sys.argv[1]
    token = f"tok-{employee_id}-{secrets.token_urlsafe(24)}"
    path = DATA_DIR / "auth_tokens.json"
    data = json.loads(path.read_text())
    data["tokens"][hashlib.sha256(token.encode()).hexdigest()] = employee_id
    path.write_text(json.dumps(data, indent=2))
    print(token)
