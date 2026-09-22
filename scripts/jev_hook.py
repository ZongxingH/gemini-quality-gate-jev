#!/usr/bin/env python3
"""Gemini CLI hook entry point for the TypeSafe Jev gates.

One script serves every selected hook event. It reads the hook payload from
stdin, dispatches on ``hook_event_name``, and prints exactly one JSON object on
stdout (or nothing when the event is not selected).

The decision layer follows TypeSafe's recommended pattern
(https://docs.typesafe.ai/patterns/confidence-routing):

* **high confidence** - act automatically (allow or deny);
* **medium confidence / high stakes** - escalate: ask the human to confirm, or
  gather more information before acting;
* **low confidence** - do not act on the model's read (a score below the
  confidence floor cannot trigger an automatic deny).

Noul answers carry no confidence of their own, so a configurable band below the
action threshold is treated as "gather more information" instead of a hard cut
(https://docs.typesafe.ai/confidence).

Every gate declares its stance in ``policy.gates``:

* ``auto``     - thresholds decide; the human is never asked;
* ``escalate`` - the uncertain band asks the human (BeforeTool by default);
* ``advisory`` - never block; keep the signal visible instead.

Fail-open stays the default: an unreachable Jev never makes Gemini CLI
unusable. ``BeforeTool`` can opt into fail-closed via ``before_tool.fail_mode``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_client import (  # noqa: E402  (path setup must run first)
    JevError,
    ask_jev,
    clip,
    confidence,
    configure,
    git_context,
    load_api_key,
    log_decision,
    noul,
    probabilities,
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

VERIFY_REASON = (
    "JEV is not confident this answer is complete. Before finishing, state what "
    "you verified, run the single most relevant check, and correct anything it "
    "exposes. Do not claim success without evidence."
)

PLAN_CONTEXT = (
    "JEV pre-flight note: this request looks broad enough to need an explicit "
    "plan. State the steps, make the smallest change that satisfies the "
    "request, run the relevant verification, and report the result with "
    "evidence. Do not claim success without running something."
)

CAUTION_CONTEXT = (
    "JEV pre-flight note: this request may ask for something unsafe (secret "
    "access, destructive action, or bypassing review). If it does, refuse that "
    "part and explain why instead of complying."
)

SESSION_CONTEXT = (
    "JEV session pre-flight: this repository needs deliberate verification. "
    "Prefer small changes, run the project's tests or verification commands, "
    "and state explicitly what you verified. Do not touch credentials, "
    "production data, or deployment configuration unless asked."
)

UNVERIFIED_REASON = (
    "JEV could not verify this step with enough confidence and no human "
    "confirmation is available, so it was blocked. Propose a safer alternative, "
    "or ask the user how to proceed."
)


# --------------------------------------------------------------------------
# Decision plumbing
# --------------------------------------------------------------------------

def allow_decision(message: str | None = None, details: dict | None = None) -> dict:
    return {"kind": "allow", "message": message, "details": details or {}}


def deny_decision(reason: str, message: str | None = None, details: dict | None = None) -> dict:
    return {"kind": "deny", "reason": reason, "message": message, "details": details or {}}


def ask_decision(message: str, details: dict | None = None) -> dict:
    return {"kind": "ask", "message": message, "details": details or {}}


def context_decision(context: str, message: str | None = None, details: dict | None = None) -> dict:
    return {"kind": "context", "context": context, "message": message, "details": details or {}}


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
    # BeforeTool: force the user confirmation dialog.
    return {"decision": "ask", "systemMessage": message}


def context_payload(context: str, message: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"hookSpecificOutput": {"additionalContext": context}}
    if message:
        payload["systemMessage"] = message
    return payload


def gate_mode(config: dict[str, Any], gate: str) -> str:
    gates = (config.get("policy") or {}).get("gates") or {}
    mode = str(gates.get(gate, "auto")).lower()
    return mode if mode in ("auto", "escalate", "advisory") else "auto"


def human_available(policy: dict[str, Any]) -> bool:
    """Whether the uncertain band may escalate to a human.

    ``auto`` probes the controlling terminal: the hook only escalates when it
    can actually reach a terminal. Headless and CI runs therefore fall back to
    ``ask_fallback`` deterministically; sandboxed runs can force either answer
    with ``assume_human`` or the ``JEV_ASSUME_HUMAN`` environment variable.
    """
    override = os.environ.get("JEV_ASSUME_HUMAN")
    if override is not None and override.strip():
        return override.strip().lower() in ("1", "true", "yes", "on")

    mode = str(policy.get("assume_human", "auto")).lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return False
    os.close(descriptor)
    return True


def finalize(
    decision: dict,
    config: dict[str, Any],
    gate: str,
    advisory_context: str | None = None,
) -> dict[str, Any] | None:
    """Map an internal decision through the gate's stance (the official bands)."""
    kind = decision["kind"]
    message = decision.get("message")
    details = decision.get("details") or {}
    mode = gate_mode(config, gate)
    policy = config.get("policy") or {}

    if kind == "context":
        log_decision(gate, "context", details, config)
        return context_payload(decision.get("context") or "", message)

    if kind == "allow":
        log_decision(gate, "allow", details, config)
        return allow_payload(message) if message else None

    if kind == "ask":
        if mode == "advisory":
            log_decision(gate, "advisory", {**details, "downgraded_from": "ask"}, config)
            return allow_payload(f"{message or 'JEV flagged this step'} (advisory only: not blocking)")
        if mode == "auto" or not human_available(policy):
            if str(policy.get("ask_fallback", "deny")).lower() == "allow":
                log_decision(gate, "fallback_allow", details, config)
                return allow_payload(f"{message or 'JEV was unsure'} (no confirmation available: allowed)")
            log_decision(gate, "fallback_deny", details, config)
            return deny_payload(UNVERIFIED_REASON, f"{message or 'JEV was unsure'} (no confirmation available: blocked)")
        log_decision(gate, "ask", details, config)
        return ask_payload(message or "JEV flagged this step as uncertain; confirm before continuing.")

    # kind == "deny"
    if mode == "advisory":
        log_decision(gate, "advisory", {**details, "downgraded_from": "deny"}, config)
        payload = allow_payload(f"{message or 'JEV flagged this step'} (advisory only: not blocking)")
        if advisory_context:
            payload["hookSpecificOutput"] = {"additionalContext": advisory_context}
        return payload
    log_decision(gate, "deny", details, config)
    return deny_payload(decision.get("reason") or "JEV blocked this step.", message)


def _confidence_note(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _below_floor(value: float | None, floor: float) -> bool:
    return value is not None and value < floor


# --------------------------------------------------------------------------
# AfterAgent: the quality gate that can ask for one correction pass.
# --------------------------------------------------------------------------

def handle_after_agent(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    gate = "AfterAgent"
    if event.get("stop_hook_active"):
        return finalize(allow_decision("JEV accepted the retry result without another automatic retry"), config, gate)

    if not load_api_key():
        return finalize(allow_decision(SKIP_MESSAGE), config, gate)

    thresholds = config["thresholds"]
    policy = config["policy"]
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
    risk_confidence = confidence(answers, "risk")

    details = {
        "needs_retry": round(retry, 4),
        "risk": round(risk, 3),
        "risk_confidence": risk_confidence,
        "risk_probabilities": probabilities(answers, "risk"),
    }

    retry_at = float(thresholds["after_agent_retry"])
    risk_hard = float(thresholds["after_agent_risk_hard"])
    uncertain_low = float(policy["uncertain_low"])
    floor = float(policy["confidence_floor"])

    # A high risk score only lowers the bar when the risk read itself is
    # trustworthy; a low-confidence read must not trigger work by itself.
    risk_actionable = risk >= risk_hard and not _below_floor(risk_confidence, floor)

    if retry >= retry_at:
        decision = deny_decision(
            RETRY_REASON,
            f"JEV requested a correction pass (needs_retry={retry:.2f}, risk={risk:.1f})",
            details,
        )
    elif retry >= uncertain_low and risk_actionable:
        # Medium confidence on a high-stakes change: gather information instead
        # of either accepting it or demanding a full rewrite.
        decision = deny_decision(
            VERIFY_REASON,
            (
                f"JEV wants verification evidence (needs_retry={retry:.2f}, "
                f"risk={risk:.1f}, risk_confidence={_confidence_note(risk_confidence)})"
            ),
            details,
        )
    elif retry >= uncertain_low:
        decision = allow_decision(
            (
                f"JEV: unverified but probably fine (needs_retry={retry:.2f}, "
                f"risk={risk:.1f}); treat the result as provisional"
            ),
            details,
        )
    elif config.get("report", {}).get("after_agent_pass", True):
        decision = allow_decision(
            f"JEV quality gate passed (needs_retry={retry:.2f}, risk={risk:.1f})", details
        )
    else:
        decision = allow_decision(None, details)

    return finalize(decision, config, gate)


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
    gate = "BeforeTool"
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
    policy = config["policy"]
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
    danger_confidence = confidence(answers, "danger")
    leak = noul(answers, "secret_exposure")

    details = {
        "tool": tool_name,
        "candidate_reason": reason,
        "danger": round(danger, 3),
        "danger_confidence": danger_confidence,
        "danger_probabilities": probabilities(answers, "danger"),
        "secret_exposure": round(leak, 4),
    }

    deny_at = float(thresholds["before_tool_deny"])
    ask_at = float(thresholds["before_tool_ask"])
    leak_deny = float(thresholds["before_tool_leak_deny"])
    uncertain_low = float(policy["uncertain_low"])
    floor = float(policy["confidence_floor"])
    auto_act = float(policy["auto_act_confidence"])

    # A destructive action is auto-blocked only when the read is confident;
    # secret exposure acts as a veto (a Noul has no confidence to weigh).
    strong_deny = danger >= deny_at and not _below_floor(danger_confidence, auto_act)

    if strong_deny or leak >= leak_deny:
        detail = "secret exposure" if leak >= leak_deny else "destructive action"
        decision = deny_decision(
            (
                f"JEV blocked this tool call ({detail}). Do not retry it as-is: "
                "explain what you were trying to achieve and propose a safe "
                "alternative, or ask the user how to proceed."
            ),
            (
                f"JEV blocked {tool_name} (danger={danger:.1f}, "
                f"danger_confidence={_confidence_note(danger_confidence)}, "
                f"secret_exposure={leak:.2f})"
            ),
            details,
        )
    elif _below_floor(danger_confidence, floor) or danger >= ask_at or leak >= uncertain_low:
        why = []
        if _below_floor(danger_confidence, floor):
            why.append(f"low confidence ({_confidence_note(danger_confidence)})")
        if danger >= ask_at:
            why.append(f"danger={danger:.1f}")
        if leak >= uncertain_low:
            why.append(f"possible secret exposure={leak:.2f}")
        decision = ask_decision(
            f"JEV flagged {tool_name} ({', '.join(why)}); confirm before running it.",
            details,
        )
    else:
        decision = allow_decision(None, details)

    return finalize(decision, config, gate)


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
    gate = "BeforeAgent"
    prompt = (event.get("prompt") or "").strip()
    if not prompt:
        return None

    if not load_api_key():
        return finalize(allow_decision(SKIP_MESSAGE), config, gate)

    thresholds = config["thresholds"]
    policy = config["policy"]
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

    details = {
        "policy_violation": round(violation, 4),
        "needs_plan": round(needs_plan, 4),
    }

    deny_at = float(thresholds["before_agent_policy_deny"])
    plan_at = float(thresholds["before_agent_plan_notice"])
    uncertain_low = float(policy["uncertain_low"])

    caution = violation >= uncertain_low
    if violation >= deny_at:
        decision = deny_decision(
            (
                "JEV policy gate refused this request. If the goal is legitimate, "
                "restate it without the unsafe part (no secret access, no "
                "destructive or review-bypassing steps)."
            ),
            f"JEV refused the request (policy_violation={violation:.2f})",
            details,
        )
    elif caution:
        # Medium confidence on a possibly unsafe request: do not refuse the
        # user's own request, but tell the agent to push back on the unsafe part.
        decision = context_decision(
            CAUTION_CONTEXT,
            f"JEV: request may be unsafe (policy_violation={violation:.2f}); proceed carefully",
            details,
        )
    elif needs_plan >= plan_at:
        decision = context_decision(
            PLAN_CONTEXT, f"JEV pre-flight note (needs_plan={needs_plan:.2f})", details
        )
    else:
        decision = allow_decision(None, details)

    return finalize(
        decision,
        config,
        gate,
        advisory_context=CAUTION_CONTEXT if caution else None,
    )


# --------------------------------------------------------------------------
# SessionStart: one short, computed advisory for the session.
# --------------------------------------------------------------------------

def handle_session_start(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    gate = "SessionStart"
    if not load_api_key():
        return finalize(allow_decision(SKIP_MESSAGE), config, gate)

    thresholds = config["thresholds"]
    policy = config["policy"]
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
    risk_confidence = confidence(answers, "repo_risk")
    burden = noul(answers, "verification_burden")

    details = {
        "repo_risk": round(risk, 3),
        "risk_confidence": risk_confidence,
        "risk_probabilities": probabilities(answers, "repo_risk"),
        "verification_burden": round(burden, 4),
    }

    notice = float(thresholds["session_notice"])
    burden_notice = float(thresholds["session_burden_notice"])
    floor = float(policy["confidence_floor"])

    risk_actionable = risk >= notice and not _below_floor(risk_confidence, floor)
    if risk >= notice and not risk_actionable:
        print(
            "JEV session pre-flight: repository risk read was uncertain "
            f"(risk={risk:.1f}, confidence={_confidence_note(risk_confidence)}); no advisory injected",
            file=sys.stderr,
        )

    if risk_actionable or burden >= burden_notice:
        decision = context_decision(
            SESSION_CONTEXT,
            (
                f"JEV session notice (repo_risk={risk:.1f}, "
                f"risk_confidence={_confidence_note(risk_confidence)}, "
                f"verification_burden={burden:.2f})"
            ),
            details,
        )
    else:
        decision = allow_decision(None, details)

    return finalize(decision, config, gate)


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
