#!/usr/bin/env python3
"""Shared TypeSafe Jev client for the Gemini CLI hooks.

This module owns everything the individual hook handlers need but do not want
to care about: where the configuration lives, how the API key is found, how a
compact repository snapshot is built, and how a typed question is sent to the
Jev API.

It never decides anything by itself; decisions live in ``jev_hook.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

MAX_STATE_CHARS = 12000
MAX_TOOL_INPUT_CHARS = 4000
MAX_SNAPSHOT_ENTRIES = 40
GIT_TIMEOUT_SECONDS = 1.5

#: Events the extension knows how to serve. ``AfterAgent`` is the default
#: because it can only ever ask for one extra correction pass.
KNOWN_EVENTS = ("AfterAgent", "BeforeTool", "BeforeAgent", "SessionStart")
DEFAULT_EVENTS = ("AfterAgent",)

DEFAULT_CONFIG: dict[str, Any] = {
    "events": list(DEFAULT_EVENTS),
    "model": DEFAULT_MODEL,
    "api_url": DEFAULT_URL,
    "timeouts": {
        "AfterAgent": 5.0,
        "BeforeTool": 3.0,
        "BeforeAgent": 4.0,
        "SessionStart": 5.0,
    },
    "thresholds": {
        # AfterAgent: one correction pass.
        "after_agent_retry": 0.85,
        # A high risk score lowers the bar for asking for a correction.
        "after_agent_risk_hard": 2.5,
        "after_agent_retry_soft": 0.5,
        # BeforeTool: 0..3 danger score.
        "before_tool_ask": 1.5,
        "before_tool_deny": 2.5,
        "before_tool_leak_deny": 0.8,
        # BeforeAgent.
        "before_agent_policy_deny": 0.9,
        "before_agent_plan_notice": 0.6,
        # SessionStart: 0..3 repository risk score.
        "session_notice": 1.5,
        "session_burden_notice": 0.8,
    },
    "before_tool": {
        # "open" keeps the session usable when Jev is unreachable; "closed"
        # refuses the tool call instead.
        "fail_mode": "open",
        # Shell commands that never reach Jev (read-only everyday commands).
        "safe_command_prefixes": [
            "ls", "cat", "head", "tail", "wc", "pwd", "which", "whoami", "date",
            "echo", "grep", "rg", "find", "fd", "tree", "file", "stat", "du", "df",
            "git status", "git log", "git diff", "git show", "git branch",
            "git remote", "git config --get", "git ls-files", "git rev-parse",
            "python --version", "python3 --version", "node --version", "npm ls",
            "go version", "cargo --version", "java -version", "make -n",
        ],
        # Write-tool calls are only checked when the target looks sensitive.
        "sensitive_path_patterns": [
            r"(^|/)\.env($|\.)", r"(^|/)\.envrc$", r"(^|/)\.ssh/",
            r"(^|/)id_(rsa|dsa|ecdsa|ed25519)", r"(^|/)\.aws/", r"(^|/)\.npmrc$",
            r"(^|/)\.pypirc$", r"(^|/)\.netrc$", r"(^|/)\.git/config$",
            r"(^|/)credentials", r"(^|/)secrets?($|\.|/)",
            r"\.(pem|key|p12|pfx|jks|keystore)$",
        ],
        # Tools that cannot change state and are never sent to Jev.
        "read_only_tools": [
            "read_file", "read_many_files", "grep_search", "glob",
            "list_directory", "google_web_search", "web_fetch", "cli_help",
            "write_todos", "codebase_investigator",
        ],
        "shell_tools": ["run_shell_command", "ShellTool", "shell"],
        "write_tools": ["write_file", "replace", "edit_file"],
    },
    "report": {
        # Print a one-line summary when the AfterAgent gate passes.
        "after_agent_pass": True,
    },
    # Decision policy, following the TypeSafe "confidence-gated routing" pattern:
    # high confidence acts, medium escalates to the human, low confidence does
    # not act at all. See https://docs.typesafe.ai/patterns/confidence-routing
    "policy": {
        # Per-gate stance:
        #   auto      - thresholds decide; never ask the human
        #   escalate  - the uncertain band asks the human (official pattern)
        #   advisory  - never block; keep the signal visible instead
        "gates": {
            "AfterAgent": "auto",
            "BeforeTool": "escalate",
            "BeforeAgent": "advisory",
            "SessionStart": "advisory",
        },
        # Global confidence floor. Below it the model is telling us it cannot
        # answer reliably, so the decision escalates instead of acting.
        "confidence_floor": 0.6,
        # A high-stakes action only happens automatically above this confidence.
        "auto_act_confidence": 0.85,
        # Noul answers carry no confidence of their own, so the band between
        # this value and the action threshold is treated as "gather more
        # information" rather than a hard cut.
        "uncertain_low": 0.5,
        # auto | always | never - whether a human can be asked at all.
        "assume_human": "auto",
        # deny | allow - what to do when the uncertain band needs a human and
        # none is reachable (CI, gemini -p, sandboxed runs).
        "ask_fallback": "deny",
    },
    # Append every decision (with Jev's probabilities) to
    # ~/.config/typesafe/jev-decisions.jsonl so thresholds can be calibrated
    # from real traffic later.
    "log_decisions": False,
}


class JevError(RuntimeError):
    """Raised when Jev cannot be reached or answered unexpectedly."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def config_path() -> Path:
    override = os.environ.get("JEV_CONFIG_FILE")
    if override:
        return Path(override)
    return config_home() / "typesafe" / "jev.json"


def key_file_path() -> Path:
    override = os.environ.get("JEV_KEY_FILE")
    if override:
        return Path(override)
    return config_home() / "typesafe" / "jev.env"


def load_config() -> dict[str, Any]:
    """Defaults, overridden by ``jev.json`` when it exists.

    A broken or unreadable configuration file is reported on stderr and the
    defaults are used, so a bad edit can never break Gemini CLI.
    """
    config = _deep_merge(DEFAULT_CONFIG, {})
    path = config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return config
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"JEV config {path} is not valid JSON ({error}); using defaults", file=os.sys.stderr)
        return config
    if not isinstance(parsed, dict):
        print(f"JEV config {path} must be a JSON object; using defaults", file=os.sys.stderr)
        return config
    return _deep_merge(config, parsed)


def configure(event_name: str) -> dict[str, Any]:
    """Configuration plus the resolved timeout for one event."""
    config = load_config()
    timeouts = config.get("timeouts") or {}
    timeout = timeouts.get(event_name)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 5.0
    config["_timeout"] = max(0.5, timeout)
    return config


def selected_events(config: dict[str, Any] | None = None) -> list[str]:
    config = config or load_config()
    events = config.get("events")
    if isinstance(events, str):
        events = [part.strip() for part in events.split(",")]
    if not isinstance(events, list):
        return list(DEFAULT_EVENTS)
    known = {name.lower(): name for name in KNOWN_EVENTS}
    result: list[str] = []
    for item in events:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name.lower() == "all":
            return list(KNOWN_EVENTS)
        canonical = known.get(name.lower())
        if canonical and canonical not in result:
            result.append(canonical)
    return result or list(DEFAULT_EVENTS)


def load_api_key() -> str | None:
    # Direct environment lookup helps manual tests. Gemini CLI redacts
    # environment variables matching /KEY/i before running a command hook, so
    # the key file is the reliable source.
    direct = os.environ.get("TYPESAFE_API_KEY")
    if direct:
        return direct.strip()

    candidates = [key_file_path(), Path.home() / ".config" / "typesafe" / "jev.env",
                  Path.home() / ".config" / "jev" / ".env"]
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


def clip(value: Any, limit: int = MAX_STATE_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def _run_git(args: list[str], cwd: str) -> str | None:
    try:
        result = subprocess.run(
            args, cwd=cwd, check=False, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def git_context(cwd: str) -> dict[str, str]:
    """Small diff-oriented snapshot used by the AfterAgent gate."""
    context: dict[str, str] = {}
    for name, args in (
        ("status", ["git", "status", "--short"]),
        ("diff_stat", ["git", "diff", "--stat"]),
    ):
        text = _run_git(args, cwd)
        if text:
            context[name] = clip(text, 4000)
    return context


def repo_snapshot(cwd: str) -> dict[str, Any]:
    """Compact repository fingerprint used by BeforeAgent and SessionStart.

    It is deliberately structured (not a transcript) so Jev can judge the
    project without shipping file contents.
    """
    snapshot: dict[str, Any] = {"path": cwd, "name": Path(cwd).name}

    branch = _run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if branch:
        snapshot["git_branch"] = branch
        status = _run_git(["git", "status", "--short"], cwd)
        if status:
            lines = status.splitlines()
            snapshot["git_changes"] = len(lines)
            snapshot["git_changed_paths"] = clip(lines[:MAX_SNAPSHOT_ENTRIES], 2000)
        else:
            snapshot["git_changes"] = 0
        snapshot["git_head"] = _run_git(["git", "log", "-1", "--pretty=%h %s"], cwd) or ""

    try:
        entries = sorted(p.name for p in Path(cwd).iterdir() if not p.name.startswith("."))
    except OSError:
        entries = []
    snapshot["top_level"] = entries[:MAX_SNAPSHOT_ENTRIES]

    markers = {
        "package.json": "node", "pyproject.toml": "python", "requirements.txt": "python",
        "go.mod": "go", "Cargo.toml": "rust", "pom.xml": "java", "build.gradle": "java",
        "Gemfile": "ruby", "composer.json": "php", "Dockerfile": "docker",
        "terraform": "terraform", "AGENTS.md": "agent-rules", "GEMINI.md": "agent-rules",
        "CLAUDE.md": "agent-rules", ".github": "ci",
    }
    found = []
    for marker, label in markers.items():
        if (Path(cwd) / marker).exists() and label not in found:
            found.append(label)
    snapshot["detected"] = found
    return snapshot


def ask_jev(
    questions: dict[str, Any],
    state: Any,
    config: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Send typed questions to Jev and return the ``answers`` mapping."""
    config = config or load_config()
    key = api_key or load_api_key()
    if not key:
        raise JevError("no TypeSafe API key configured")

    body = {
        "model": os.environ.get("TYPESAFE_MODEL") or config.get("model") or DEFAULT_MODEL,
        "state": state if isinstance(state, str) else json.dumps(state, ensure_ascii=False),
        "questions": questions,
    }
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        os.environ.get("TYPESAFE_API_URL") or config.get("api_url") or DEFAULT_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    timeout_env = os.environ.get("JEV_HOOK_TIMEOUT_SECONDS")
    try:
        timeout = float(timeout_env) if timeout_env else float(config.get("_timeout") or 5.0)
    except ValueError:
        timeout = float(config.get("_timeout") or 5.0)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise JevError(str(error)) from error

    if not isinstance(payload, dict):
        raise JevError("unexpected response payload")
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise JevError("response is missing the answers object")
    return answers


def noul(answers: dict[str, Any], name: str) -> float:
    """Read a 0..1 noul answer, tolerating missing or malformed values."""
    entry = answers.get(name)
    if not isinstance(entry, dict):
        raise JevError(f"answer '{name}' is missing")
    value = entry.get("noul")
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError) as error:
        raise JevError(f"answer '{name}' is not a probability") from error


def score(answers: dict[str, Any], name: str) -> float:
    """Read a numeric score answer, tolerating missing or malformed values."""
    entry = answers.get(name)
    if not isinstance(entry, dict):
        raise JevError(f"answer '{name}' is missing")
    value = entry.get("score")
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise JevError(f"answer '{name}' is not a score") from error


def confidence(answers: dict[str, Any], name: str) -> float | None:
    """Read the answer's confidence (Score/Choice only).

    ``None`` means the answer did not carry one - Noul answers never do, and an
    older deployment may not either. Callers fall back to a value-only decision
    in that case.
    """
    entry = answers.get(name)
    if not isinstance(entry, dict):
        return None
    value = entry.get("confidence")
    if value is None:
        return None
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return None


def probabilities(answers: dict[str, Any], name: str) -> dict[str, float]:
    """Read the full probability distribution of a Score/Choice answer.

    TypeSafe returns this so a caller can replace the built-in ``confidence``
    with a measure of their own; the decision log records it for calibration.
    """
    entry = answers.get(name)
    if not isinstance(entry, dict):
        return {}
    raw = entry.get("probabilities")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, float] = {}
    for key, value in raw.items():
        try:
            result[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return result


def decision_log_path() -> Path:
    return config_home() / "typesafe" / "jev-decisions.jsonl"


def log_decision(gate: str, verdict: str, details: dict[str, Any], config: dict[str, Any]) -> None:
    """Append one JSONL record per decision (best effort, never raises)."""
    if not config.get("log_decisions"):
        return
    try:
        path = decision_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "gate": gate,
            "verdict": verdict,
            "mode": (config.get("policy") or {}).get("gates", {}).get(gate),
            **details,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return
