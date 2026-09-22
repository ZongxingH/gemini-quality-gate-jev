#!/usr/bin/env python3
"""Gemini CLI AfterAgent hook backed by TypeSafe Jev.

The hook is deliberately fail-open: an unavailable Jev service must not make
Gemini CLI unusable. It only asks Jev for bounded decisions; it never asks Jev
to generate code or prose.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


JEV_URL = os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
JEV_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")
TIMEOUT_SECONDS = float(os.environ.get("JEV_HOOK_TIMEOUT_SECONDS", "8"))
MAX_TEXT = 12000


def emit(payload: dict[str, Any]) -> None:
    # Gemini CLI requires stdout to contain only one JSON object.
    print(json.dumps(payload, ensure_ascii=False))


def allow(message: str | None = None) -> None:
    payload: dict[str, Any] = {"decision": "allow"}
    if message:
        payload["systemMessage"] = message
    emit(payload)


def clip(value: Any, limit: int = MAX_TEXT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def git_context(cwd: str) -> dict[str, str]:
    context: dict[str, str] = {}
    for name, args in (
        ("status", ["git", "status", "--short"]),
        ("diff_stat", ["git", "diff", "--stat"]),
    ):
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.strip():
            context[name] = clip(result.stdout, 4000)
    return context


def load_api_key() -> str | None:
    # Direct environment lookup is useful for manual tests. Gemini CLI command
    # hooks run with a sanitized environment, so production use falls back to
    # a user-owned file outside the repository.
    direct = os.environ.get("TYPESAFE_API_KEY")
    if direct:
        return direct

    candidates = [
        Path.home() / ".config" / "typesafe" / "jev.env",
        Path.home() / ".config" / "jev" / ".env",
    ]
    for path in candidates:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() == "TYPESAFE_API_KEY":
                    value = value.strip().strip("\"'")
                    if value:
                        return value
        except OSError:
            continue
    return None


def ask_jev(state: dict[str, Any], api_key: str) -> dict[str, Any]:
    request_body = {
        "model": JEV_MODEL,
        "state": json.dumps(state, ensure_ascii=False),
        "questions": {
            "needs_retry": {
                "type": "noul",
                "instructions": (
                    "Does the agent's final response have a material omission that "
                    "should be corrected before it is accepted? Consider missing "
                    "verification, an unaddressed requirement, or an explicitly "
                    "claimed but unsupported result."
                ),
            },
            "risk": {
                "type": "score",
                "instructions": "How risky is the completed coding change?",
                "criteria": [
                    "Low: narrow change with clear verification",
                    "Medium: multiple files or incomplete verification",
                    "High: security, data, API, or destructive behavior is involved",
                ],
            },
        },
    }
    data = json.dumps(request_body).encode("utf-8")
    request = urllib.request.Request(
        JEV_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        allow("JEV skipped: invalid hook input")
        return

    if event.get("hook_event_name") not in (None, "AfterAgent"):
        allow()
        return

    # Do not create an endless automatic retry loop.
    if event.get("stop_hook_active"):
        allow("JEV accepted the retry result without another automatic retry")
        return

    api_key = load_api_key()
    if not api_key:
        allow(
            "JEV skipped: create ~/.config/typesafe/jev.env with "
            "TYPESAFE_API_KEY to enable the AfterAgent quality gate"
        )
        return

    cwd = event.get("cwd") or os.getcwd()
    state = {
        "user_request": clip(event.get("prompt", "")),
        "agent_response": clip(event.get("prompt_response", "")),
        "workspace": git_context(cwd),
    }

    try:
        response = ask_jev(state, api_key)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        print(f"JEV unavailable; allowing Gemini response: {error}", file=sys.stderr)
        allow("JEV unavailable; response was allowed without the quality gate")
        return

    answers = response.get("answers", {})
    retry_answer = answers.get("needs_retry", {})
    retry_probability = float(retry_answer.get("noul", 0.0) or 0.0)
    risk_answer = answers.get("risk", {})
    risk_score = risk_answer.get("score")

    if retry_probability >= 0.85:
        reason = (
            "JEV quality gate asks for one correction pass. Re-check the original "
            "requirements, run the relevant tests or verification commands, and "
            "then return the corrected result with concrete evidence."
        )
        emit({
            "decision": "deny",
            "reason": reason,
            "systemMessage": (
                f"JEV requested a correction pass (needs_retry={retry_probability:.2f}, "
                f"risk={risk_score})."
            ),
        })
        return

    allow(
        f"JEV quality gate passed (needs_retry={retry_probability:.2f}, risk={risk_score})"
    )


if __name__ == "__main__":
    main()
