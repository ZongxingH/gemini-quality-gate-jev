#!/usr/bin/env python3
"""Gemini CLI hook entry point for the TypeSafe Jev gates.

One script serves every selected hook event. It reads the hook payload from
stdin, dispatches on ``hook_event_name``, and prints exactly one JSON object on
stdout (or nothing when the event is not selected).

Design rules:

* One event, one Jev question set, one bounded decision.
* Fail open: an unreachable or confused Jev must never make Gemini CLI
  unusable. ``BeforeTool`` can opt into fail-closed via ``before_tool.fail_mode``.
* No code or prose generation: hooks only allow, deny, ask the user, or inject
  a short advisory context.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_client import (  # noqa: E402  (path setup must run first)
    JevError,
    ask_jev,
    clip,
    configure,
    git_context,
    load_api_key,
    noul,
    repo_snapshot,
    score,
    selected_events,
)

SKIP_MESSAGE = (
    "JEV skipped: create ~/.config/typesafe/jev.env with TYPESAFE_API_KEY "
    "to enable the quality gates"
)

RETRY_REASON = (
    "JEV quality gate asks for one correction pass. Re-check the original "
    "requirements, run the relevant tests or verification commands, and then "
    "return the corrected result with concrete evidence."
)

PLAN_CONTEXT = (
    "JEV pre-flight note: this request looks broad enough to need an explicit "
    "plan. State the steps, make the smallest change that satisfies the "
    "request, run the relevant verification, and report the result with "
    "evidence. Do not claim success without running something."
)


def allow_payload(message: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"decision": "allow"}
    if message:
        payload["systemMessage"] = message
    return payload


def deny_payload(reason: str, message: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"decision": "deny", "reason": reason}
    if message:
        payload["systemMessage"] = message
    return payload


def ask_payload(message: str) -> dict[str, Any]:
    # BeforeTool: force the user confirmation dialog for medium-risk calls.
    return {"decision": "ask", "systemMessage": message}


def context_payload(context: str, message: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"hookSpecificOutput": {"additionalContext": context}}
    if message:
        payload["systemMessage"] = message
    return payload


# --------------------------------------------------------------------------
# AfterAgent: the quality gate that can ask for exactly one correction pass.
# --------------------------------------------------------------------------

def handle_after_agent(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("stop_hook_active"):
        return allow_payload("JEV accepted the retry result without another automatic retry")

    if not load_api_key():
        return allow_payload(SKIP_MESSAGE)

    thresholds = config["thresholds"]
    cwd = event.get("cwd") or os.getcwd()
    state = {
        "user_request": clip(event.get("prompt", "")),
        "agent_response": clip(event.get("prompt_response", "")),
        "workspace": git_context(cwd),
    }
    questions = {
        "needs_retry": {
            "type": "noul",
            "instructions": (
                "Does the agent's final response have a material omission that "
                "should be corrected before it is accepted? Consider missing "
                "verification, an unaddressed requirement, or an explicitly "
                "claimed but unsupported result."
            ),
            "criteria": {
                "true": "a requirement is unmet, unverified, or the answer overclaims",
                "false": "the request is satisfied and the verification is adequate",
            },
        },
        "risk": {
            "type": "score",
            "instructions": "How risky is the completed coding change?",
            "criteria": [
                "Low: narrow, local change with clear verification",
                "Medium: multiple files, or verification is incomplete",
                "High: behavior, data, or interface changes that are hard to undo",
                "Critical: security, credentials, destructive, or data-loss potential",
            ],
        },
    }

    answers = ask_jev(questions, state, config)
    retry = noul(answers, "needs_retry")
    risk = score(answers, "risk")

    hard_retry = retry >= float(thresholds["after_agent_retry"])
    # A high risk score lowers the bar: risk is now part of the decision
    # instead of being reported and ignored.
    risk_retry = (
        risk >= float(thresholds["after_agent_risk_hard"])
        and retry >= float(thresholds["after_agent_retry_soft"])
    )

    if hard_retry or risk_retry:
        trigger = "needs_retry" if hard_retry else "risk"
        return deny_payload(
            RETRY_REASON,
            f"JEV requested a correction pass ({trigger}: needs_retry={retry:.2f}, risk={risk:.1f})",
        )

    if config.get("report", {}).get("after_agent_pass", True):
        return allow_payload(f"JEV quality gate passed (needs_retry={retry:.2f}, risk={risk:.1f})")
    return None


# --------------------------------------------------------------------------
# BeforeTool: guard high-risk tool calls before they run.
# --------------------------------------------------------------------------

def _normalise_command(command: str) -> str:
    return " ".join(command.strip().split())


def _is_safe_command(command: str, prefixes: list[str]) -> bool:
    normalised = _normalise_command(command)
    if not normalised:
        return True
    for prefix in prefixes:
        if normalised == prefix or normalised.startswith(prefix + " "):
            # A read-only command that pipes into a shell or writes is not safe.
            if any(token in normalised for token in ("|", ">", "&&", ";", "$(", "`")):
                continue
            return True
    return False


def tool_candidate(tool_name: str, tool_input: dict[str, Any], options: dict[str, Any]) -> tuple[bool, str]:
    """Decide locally whether a tool call deserves a Jev check.

    This keeps latency (and cost) low: read-only tools and everyday shell
    commands never leave the machine.
    """
    read_only = {name.lower() for name in options.get("read_only_tools", [])}
    shell_tools = {name.lower() for name in options.get("shell_tools", [])}
    write_tools = {name.lower() for name in options.get("write_tools", [])}
    lowered = (tool_name or "").lower()

    if lowered in read_only:
        return False, "read-only tool"

    if lowered in shell_tools or "shell" in lowered:
        command = tool_input.get("command") or tool_input.get("cmd") or ""
        if not isinstance(command, str):
            command = json.dumps(command, ensure_ascii=False)
        if _is_safe_command(command, list(options.get("safe_command_prefixes", []))):
            return False, "read-only shell command"
        return True, "shell command"

    if lowered in write_tools or "write" in lowered or "edit" in lowered:
        # Only non-empty path-ish fields are joined; a trailing empty field
        # would otherwise defeat the end-anchored patterns below.
        parts = [
            str(tool_input[key]).strip()
            for key in ("file_path", "path", "absolute_path", "file", "target_file")
            if tool_input.get(key)
        ]
        target = " ".join(parts)
        import re

        for pattern in options.get("sensitive_path_patterns", []):
            try:
                if re.search(pattern, target):
                    return True, "sensitive path"
            except re.error:
                continue
        return False, "ordinary file write"

    if lowered.startswith("mcp__") or lowered.startswith("mcp_"):
        return True, "MCP tool"
    return False, "unclassified tool"


def handle_before_tool(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    options = config["before_tool"]
    tool_name = event.get("tool_name") or ""
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}

    candidate, reason = tool_candidate(tool_name, tool_input, options)
    if not candidate:
        return None

    if not load_api_key():
        return _tool_failure(options, "no TypeSafe API key configured")

    thresholds = config["thresholds"]
    state = {
        "tool": tool_name,
        "candidate_reason": reason,
        "tool_input": clip(tool_input, 4000),
        "workspace": git_context(event.get("cwd") or os.getcwd()),
        "original_request": clip(event.get("original_request_name", ""), 500),
    }
    questions = {
        "danger": {
            "type": "score",
            "instructions": (
                "How dangerous is running this tool call in this repository? "
                "Judge the action itself, not the agent's intent."
            ),
            "criteria": [
                "Safe: reads or inspects only, nothing can break",
                "Low: local and reversible, standard daily work",
                "Medium: changes shared state, credentials, or is hard to undo",
                "High: destructive, irreversible, or exfiltrates data",
            ],
        },
        "secret_exposure": {
            "type": "noul",
            "instructions": (
                "Would this tool call expose, print, upload, commit, or overwrite "
                "credentials, tokens, or private keys?"
            ),
            "criteria": {
                "true": "secrets may leak, be overwritten, or be committed",
                "false": "no secret is read, written, or transmitted",
            },
        },
    }

    answers = ask_jev(questions, state, config)
    danger = score(answers, "danger")
    leak = noul(answers, "secret_exposure")

    if danger >= float(thresholds["before_tool_deny"]) or leak >= float(thresholds["before_tool_leak_deny"]):
        detail = "secret exposure" if leak >= float(thresholds["before_tool_leak_deny"]) else "destructive action"
        return deny_payload(
            (
                f"JEV blocked this tool call ({detail}). Do not retry it as-is: "
                "explain what you were trying to achieve and propose a safe "
                "alternative, or ask the user how to proceed."
            ),
            f"JEV blocked {tool_name} (danger={danger:.1f}, secret_exposure={leak:.2f})",
        )

    if danger >= float(thresholds["before_tool_ask"]):
        return ask_payload(f"JEV flagged {tool_name} as medium risk (danger={danger:.1f}); confirm before running it.")

    return None


def _tool_failure(options: dict[str, Any], error: str) -> dict[str, Any] | None:
    if str(options.get("fail_mode", "open")).lower() == "closed":
        return deny_payload(
            f"JEV could not verify this tool call ({error}) and fail-closed mode is on.",
            "JEV guard unavailable; tool call blocked by fail-closed policy",
        )
    # Fail-open stays quiet: a warning on every tool call would be noise.
    print(f"JEV guard unavailable; allowing tool call: {error}", file=sys.stderr)
    return None


# --------------------------------------------------------------------------
# BeforeAgent: policy check plus an optional planning nudge.
# --------------------------------------------------------------------------

def handle_before_agent(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    prompt = (event.get("prompt") or "").strip()
    if not prompt:
        return None

    if not load_api_key():
        return allow_payload(SKIP_MESSAGE)

    thresholds = config["thresholds"]
    state = {
        "user_request": clip(prompt),
        "workspace": repo_snapshot(event.get("cwd") or os.getcwd()),
    }
    questions = {
        "policy_violation": {
            "type": "noul",
            "instructions": (
                "Does this request ask the agent to do something that should be "
                "refused: stealing secrets, disabling security controls, "
                "destroying data, or bypassing review?"
            ),
            "criteria": {
                "true": "the request is clearly against safe engineering practice",
                "false": "a normal development request, even if large or vague",
            },
        },
        "needs_plan": {
            "type": "noul",
            "instructions": (
                "Would this request benefit from an explicit plan and a "
                "verification step before the agent starts editing files?"
            ),
            "criteria": {
                "true": "broad, multi-file, migration, or refactor work",
                "false": "small, well-specified change",
            },
        },
    }

    answers = ask_jev(questions, state, config)
    violation = noul(answers, "policy_violation")
    needs_plan = noul(answers, "needs_plan")

    if violation >= float(thresholds["before_agent_policy_deny"]):
        return deny_payload(
            (
                "JEV policy gate refused this request. If the goal is legitimate, "
                "restate it without the unsafe part (no secret access, no "
                "destructive or review-bypassing steps)."
            ),
            f"JEV refused the request (policy_violation={violation:.2f})",
        )

    if needs_plan >= float(thresholds["before_agent_plan_notice"]):
        return context_payload(PLAN_CONTEXT, f"JEV pre-flight note (needs_plan={needs_plan:.2f})")

    return None


# --------------------------------------------------------------------------
# SessionStart: one short, computed advisory for the session.
# --------------------------------------------------------------------------

def handle_session_start(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    if not load_api_key():
        return allow_payload(SKIP_MESSAGE)

    thresholds = config["thresholds"]
    cwd = event.get("cwd") or os.getcwd()
    state = {
        "workspace": repo_snapshot(cwd),
        "session_source": event.get("source", "startup"),
    }
    questions = {
        "repo_risk": {
            "type": "score",
            "instructions": "How risky is working in this repository at this moment?",
            "criteria": [
                "Safe: clean tree, protected or disposable code",
                "Low: normal feature work with a small diff",
                "High: production, data, credentials, or an uncommitted risky diff",
                "Critical: secrets or destructive infrastructure are in play",
            ],
        },
        "verification_burden": {
            "type": "noul",
            "instructions": (
                "Does this repository need unusually careful verification (tests, "
                "migrations, security review) before a change can be trusted?"
            ),
            "criteria": {
                "true": "a change here needs tests, review, or staged rollout",
                "false": "routine change, quick check is enough",
            },
        },
    }

    answers = ask_jev(questions, state, config)
    risk = score(answers, "repo_risk")
    burden = noul(answers, "verification_burden")

    if risk < float(thresholds["session_notice"]) and burden < 0.8:
        return None

    context = (
        "JEV session pre-flight: this repository needs deliberate verification. "
        "Prefer small changes, run the project's tests or verification commands, "
        "and state explicitly what you verified. Do not touch credentials, "
        "production data, or deployment configuration unless asked."
    )
    return context_payload(context, f"JEV session notice (repo_risk={risk:.1f}, verification_burden={burden:.2f})")


HANDLERS = {
    "AfterAgent": handle_after_agent,
    "BeforeTool": handle_before_tool,
    "BeforeAgent": handle_before_agent,
    "SessionStart": handle_session_start,
}


def emit(payload: dict[str, Any] | None) -> None:
    if payload is None:
        return
    print(json.dumps(payload, ensure_ascii=False))


def run(event: dict[str, Any]) -> dict[str, Any] | None:
    name = event.get("hook_event_name")
    if name not in HANDLERS:
        return allow_payload()

    config = configure(name)
    if name not in selected_events(config):
        # Not selected at install time: stay silent so the event is a no-op
        # even if an extension update restored every hook definition.
        return None

    try:
        return HANDLERS[name](event, config)
    except JevError as error:
        print(f"JEV unavailable ({name}): {error}", file=sys.stderr)
        if name == "BeforeTool":
            return _tool_failure(config["before_tool"], str(error))
        return allow_payload("JEV unavailable; allowed without the quality gate")
    except Exception as error:  # noqa: BLE001 - a hook must never break the CLI
        print(f"JEV hook error ({name}): {error!r}", file=sys.stderr)
        return allow_payload("JEV error; allowed without the quality gate")


def main() -> None:
    if "--print-events" in sys.argv:
        print(",".join(selected_events()))
        return

    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        emit(allow_payload("JEV skipped: invalid hook input"))
        return
    if not isinstance(event, dict):
        emit(allow_payload("JEV skipped: invalid hook input"))
        return
    emit(run(event))


if __name__ == "__main__":
    main()
