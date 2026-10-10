"""Central configuration. Secrets come ONLY from environment variables (or a git-ignored .env file)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RUNTIME_DIR = DATA_DIR / "runtime"          # git-ignored: memory DB, tickets
LOG_DIR = ROOT / "logs"


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader (no dependency). Never overrides variables already set in the shell."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0]            # allow trailing comments
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

# Every provider below speaks the OpenAI-compatible Chat Completions protocol, so the
# code path is identical; only base_url / key / model change.
PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "default_model": "gemini-3.8-flash",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "default_model": "meta-llama/llama-3.3-70b-instruct",
    },
}

# USD per 1M tokens (input, output), from each provider's public pricing page (checked 2026-10-10;
# Gemini output price includes thinking tokens). Unknown models fall back to PRICING_FALLBACK.
PRICING_PER_MTOK = {
    "gemini-3.8-flash": (0.75, 3.75),        # promo price through 2026-12-31 ($1.50 / $7.50 from 2027-01-01)
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "meta-llama/llama-3.3-70b-instruct": (0.13, 0.40),
}
PRICING_FALLBACK = (1.00, 3.00)

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()
if LLM_PROVIDER not in PROVIDERS:
    raise SystemExit(f"LLM_PROVIDER must be one of {sorted(PROVIDERS)}")
LLM_MODEL = os.getenv("LLM_MODEL", PROVIDERS[LLM_PROVIDER]["default_model"])
LLM_BASE_URL = os.getenv("LLM_BASE_URL", PROVIDERS[LLM_PROVIDER]["base_url"])
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "60"))
# Thinking models (Gemini 3.x) spend hidden reasoning tokens that count against max_tokens and are
# billed as output. "none" keeps short structured calls from being truncated. Empty = provider default.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "none" if LLM_PROVIDER == "gemini" else "")


def llm_api_key() -> str:
    return os.getenv(PROVIDERS[LLM_PROVIDER]["key_env"], "")


SERVER_HOST = os.getenv("HR_SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("HR_SERVER_PORT", "8080"))

# Optional extra policy folder (used by the prompt-injection test to load a poisoned document).
EXTRA_POLICY_DIR = os.getenv("HR_EXTRA_POLICY_DIR", "")
# Experiment switch for the injection test ONLY: turn off line quarantine to see what
# delimiters alone achieve. Never set in normal operation.
DISABLE_POLICY_SANITIZER = os.getenv("HR_DISABLE_POLICY_SANITIZER", "") == "1"

MAX_USER_MESSAGE_CHARS = 2000
HISTORY_TURNS = 6            # user+assistant pairs loaded from memory into context
