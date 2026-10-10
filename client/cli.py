"""CLI client for the HR helpdesk backend (stdlib only).

  python -m client.cli                         interactive chat (streaming on)
  python -m client.cli -q "How many casual leaves do I have left?"
  python -m client.cli --no-stream -q "..."
  python -m client.cli --compare "What is the work-from-home policy?"   streamed vs non-streamed timing
  python -m client.cli --classify "My salary wasn't credited"           structured ticket JSON

Token: --token, or HR_CLIENT_TOKEN env var. Interactive commands: /ticket <text>, /stream on|off,
/history, /clear, /quit
"""
import argparse
import http.client
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "client.jsonl"
DIM, BOLD, CYAN, YELLOW, RED, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"


def load_client_env(path: Path = ROOT / ".env"):
    """The client only reads its own settings (HR_*) from .env — never the LLM key."""
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.startswith("HR_"):
                os.environ.setdefault(key, value.split(" #", 1)[0].strip().strip('"'))


def client_log(record: dict):
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **record}) + "\n")


class Client:
    def __init__(self, host: str, port: int, token: str, session_id: str):
        self.host, self.port, self.token, self.session_id = host, port, token, session_id

    def _request(self, method: str, path: str, body: dict = None):
        rid = uuid.uuid4().hex[:12]
        conn = http.client.HTTPConnection(self.host, self.port, timeout=120)
        headers = {"Content-Type": "application/json", "X-Request-ID": rid, "X-Client-Sent-At": f"{time.time():.3f}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        return rid, conn, conn.getresponse()

    def chat(self, message: str, stream: bool, quiet: bool = False) -> dict:
        t0 = time.perf_counter()
        rid, conn, resp = self._request("POST", "/chat", {"message": message, "session_id": self.session_id, "stream": stream})
        if resp.status != 200:
            body = resp.read().decode()
            print(f"{RED}HTTP {resp.status}: {body}{RESET}")
            client_log({"request_id": rid, "endpoint": "/chat", "http_status": resp.status, "status": "rejected",
                        "latency_ms": round((time.perf_counter() - t0) * 1000, 1)})
            return {"status": "http_error", "http_status": resp.status}

        ttft, out = None, {}
        if stream:
            if not quiet:
                print(f"{BOLD}HR Buddy:{RESET} ", end="", flush=True)
            for raw in resp:                         # http.client de-chunks; one JSON event per line
                line = raw.decode().strip()
                if not line:
                    continue
                ev = json.loads(line)
                if ev["type"] == "delta":
                    if ttft is None:
                        ttft = round((time.perf_counter() - t0) * 1000, 1)
                    if not quiet:
                        print(ev["text"], end="", flush=True)
                elif ev["type"] == "replace" and not quiet:   # output guard / error replaced streamed text
                    print(f"\n{YELLOW}[response withdrawn by safety check]{RESET}\n{BOLD}HR Buddy:{RESET} {ev['text']}", end="")
                elif ev["type"] == "ticket" and not quiet:
                    print(f"\n{CYAN}{json.dumps(ev['ticket'], indent=2)}{RESET}\n", end="")
                elif ev["type"] == "done":
                    out = ev
            if not quiet:
                print()
        else:
            out = json.loads(resp.read().decode())
            ttft = round((time.perf_counter() - t0) * 1000, 1)       # nothing visible until the whole body arrives
            if not quiet:
                if out.get("ticket"):
                    print(f"{CYAN}{json.dumps(out['ticket'], indent=2)}{RESET}")
                print(f"{BOLD}HR Buddy:{RESET} {out.get('reply')}")
        conn.close()
        total = round((time.perf_counter() - t0) * 1000, 1)
        usage = out.get("usage", {})
        if not quiet:
            print(f"{DIM}[{rid}] status={out.get('status')} intent={out.get('intent')} tools={usage.get('tool_names')} "
                  f"tokens={usage.get('input_tokens')}/{usage.get('output_tokens')} cost=${usage.get('cost_usd')} "
                  f"first_text={ttft}ms total={total}ms{RESET}")
        client_log({"request_id": rid, "endpoint": "/chat", "stream": stream, "status": out.get("status"),
                    "client_ttft_ms": ttft, "client_total_ms": total, "server_latency_ms": usage.get("latency_ms")})
        return {**out, "client_ttft_ms": ttft, "client_total_ms": total, "request_id": rid}

    def classify(self, text: str) -> dict:
        rid, conn, resp = self._request("POST", "/classify", {"text": text})
        body = json.loads(resp.read().decode())
        conn.close()
        print(json.dumps(body, indent=2))
        return body

    def history(self):
        _, conn, resp = self._request("GET", f"/history?session_id={self.session_id}")
        print(json.dumps(json.loads(resp.read().decode()), indent=2))
        conn.close()

    def clear(self):
        _, conn, resp = self._request("POST", "/history/clear", {"session_id": self.session_id})
        print(resp.read().decode())
        conn.close()


def compare(client: Client, query: str):
    print(f"{BOLD}Query:{RESET} {query}\n")
    print(f"{BOLD}--- NON-STREAMED ---{RESET}")
    a = client.chat(query, stream=False)
    print(f"\n{BOLD}--- STREAMED ---{RESET}")
    b = client.chat(query, stream=True)
    print(f"\n{BOLD}{'mode':<14}{'time to first text':>20}{'total time':>14}{RESET}")
    print(f"{'non-streamed':<14}{a.get('client_ttft_ms')!s:>18}ms{a.get('client_total_ms')!s:>12}ms")
    print(f"{'streamed':<14}{b.get('client_ttft_ms')!s:>18}ms{b.get('client_total_ms')!s:>12}ms")


def main():
    load_client_env()
    ap = argparse.ArgumentParser(description="HR helpdesk CLI")
    ap.add_argument("--host", default=os.getenv("HR_SERVER_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("HR_SERVER_PORT", "8080")))
    ap.add_argument("--token", default=os.getenv("HR_CLIENT_TOKEN", ""))
    ap.add_argument("--session", default="default")
    ap.add_argument("--no-stream", action="store_true")
    ap.add_argument("-q", "--query")
    ap.add_argument("--compare")
    ap.add_argument("--classify")
    args = ap.parse_args()

    c = Client(args.host, args.port, args.token, args.session)
    if args.compare:
        return compare(c, args.compare)
    if args.classify:
        return c.classify(args.classify)
    if args.query:
        r = c.chat(args.query, stream=not args.no_stream)
        sys.exit(0 if r.get("status") != "http_error" else 1)

    stream = not args.no_stream
    print(f"HR helpdesk — session '{args.session}'. Commands: /ticket <text>, /stream on|off, /history, /clear, /quit")
    while True:
        try:
            msg = input(f"\n{BOLD}You:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not msg:
            continue
        if msg in ("/quit", "/exit"):
            break
        if msg.startswith("/ticket "):
            c.classify(msg[8:])
        elif msg.startswith("/stream"):
            stream = msg.endswith("on")
            print(f"streaming {'on' if stream else 'off'}")
        elif msg == "/history":
            c.history()
        elif msg == "/clear":
            c.clear()
        else:
            c.chat(msg, stream=stream)


if __name__ == "__main__":
    main()
