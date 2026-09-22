#!/usr/bin/env python3
"""Tiny stand-in for the TypeSafe Jev API, used by the test suite.

Import it for in-process tests (``MockJev``) or run it for shell-level tests:

    python3 tests/mock_jev.py --port 18899
    python3 tests/mock_jev.py --port 18899 --answers '{"needs_retry": {"noul": 0.95}}'
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: Answers that make every gate react, handy for end-to-end checks.
LOUD_ANSWERS: dict = {
    "needs_retry": {"noul": 0.95},
    "risk": {"score": 1.0, "confidence": 0.9},
    "danger": {
        "score": 3.0,
        "confidence": 0.92,
        "probabilities": {"0": 0.0, "1": 0.02, "2": 0.08, "3": 0.9},
    },
    "secret_exposure": {"noul": 0.1},
    "policy_violation": {"noul": 0.0},
    "needs_plan": {"noul": 0.1},
    "repo_risk": {"score": 0.5, "confidence": 0.9},
    "verification_burden": {"noul": 0.1},
}


class MockJev:
    """Canned Jev answers plus a request log."""

    def __init__(self, answers: dict | None = None, port: int = 0, log_path: str | None = None) -> None:
        self.requests: list[dict] = []
        self.answers: dict = answers if answers is not None else dict(LOUD_ANSWERS)
        self.log_path = log_path
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = {"raw": raw.decode("utf-8", "replace")}
                record = {
                    "body": body,
                    "authorization": self.headers.get("Authorization", ""),
                    "path": self.path,
                }
                outer.requests.append(record)
                if outer.log_path:
                    with open(outer.log_path, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                payload = json.dumps({"model": "jev-test", "answers": outer.answers}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:  # keep the test log clean
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reset(self, answers: dict | None = None) -> None:
        self.requests.clear()
        self.answers = answers if answers is not None else dict(LOUD_ANSWERS)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock TypeSafe Jev endpoint")
    parser.add_argument("--port", type=int, default=18899)
    parser.add_argument("--answers", default="", help="JSON answers object to return")
    parser.add_argument("--log", default="", help="append every request to this JSONL file")
    args = parser.parse_args()
    answers = json.loads(args.answers) if args.answers else dict(LOUD_ANSWERS)
    server = MockJev(answers=answers, port=args.port, log_path=args.log or None)
    print(server.url, flush=True)
    try:
        while True:
            server.thread.join(1)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
