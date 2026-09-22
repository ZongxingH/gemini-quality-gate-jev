#!/usr/bin/env bash
#
# End-to-end check for install.sh against a real Gemini CLI.
#
# It installs the extension from this checkout into a throwaway HOME (no
# network, no real API key), verifies that the selected gates are recorded both
# in jev.json and in the installed hooks.json, checks that the installed hooks
# respect the selection against a mock Jev endpoint, then uninstalls.
#
#   ./tests/e2e_install.sh
#
# Requires: gemini, python3, git (only for the self-check of this script).
# Exits 0 when every check passes, 1 otherwise, and skips (exit 0) when the
# Gemini CLI is not installed.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v gemini >/dev/null 2>&1; then
  printf 'SKIP: gemini CLI is not installed\n'
  exit 0
fi

# Pick a free port so a stale mock from an earlier run can never be reused.
PORT="$(python3 -c 'import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()')"
MOCK_URL="http://127.0.0.1:${PORT}/v1/systemone"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/jev-e2e.XXXXXX")"
HOME_DIR="$WORK/home"
SRC="$WORK/src"
MOCK_PID=""
FAILURES=0

cleanup() {
  if [[ -n "$MOCK_PID" ]]; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

check() {
  local label="$1" ok="$2" detail="${3:-}"
  if [[ "$ok" == "1" ]]; then
    printf '  ok   %s\n' "$label"
  else
    printf '  FAIL %s %s\n' "$label" "$detail"
    FAILURES=$((FAILURES + 1))
  fi
}

contains() { [[ "$1" == *"$2"* ]] && printf '1' || printf '0'; }

mkdir -p "$HOME_DIR" "$SRC"
tar --exclude=.git --exclude=.dsh --exclude=.DS_Store --exclude=tests \
  --exclude=__pycache__ -cf - -C "$ROOT" . | tar -xf - -C "$SRC"

python3 "$ROOT/tests/mock_jev.py" --port "$PORT" --log "$WORK/jev-requests.jsonl" >"$WORK/mock.log" 2>&1 &
MOCK_PID=$!
MOCK_READY=0
for _ in $(seq 1 50); do
  if ! kill -0 "$MOCK_PID" 2>/dev/null; then
    break
  fi
  if grep -q "$MOCK_URL" "$WORK/mock.log" 2>/dev/null; then
    MOCK_READY=1
    break
  fi
  sleep 0.1
done
if [[ "$MOCK_READY" != "1" ]]; then
  printf 'e2e: mock Jev failed to start on port %s\n' "$PORT"
  cat "$WORK/mock.log"
  exit 1
fi

count_requests() {
  if [[ -f "$WORK/jev-requests.jsonl" ]]; then
    wc -l <"$WORK/jev-requests.jsonl" | tr -d ' '
  else
    printf '0'
  fi
}

printf 'install --events AfterAgent,BeforeTool\n'
INSTALL_LOG="$WORK/install.log"
HOME="$HOME_DIR" TYPESAFE_API_KEY=ts_e2e \
  bash "$SRC/install.sh" --global --repo "$SRC" --events AfterAgent,BeforeTool \
  >"$INSTALL_LOG" 2>&1 || true
cat "$INSTALL_LOG" | sed 's/^/    | /'

EXT_DIR="$(sed -n 's/^Extension: //p' "$INSTALL_LOG" | tail -1)"
GATE_CONFIG="$(sed -n 's/^Gate conf: //p' "$INSTALL_LOG" | tail -1)"

check "install reported an extension directory" "$([[ -n "$EXT_DIR" ]] && printf 1 || printf 0)"
check "extension directory exists" "$([[ -f "$EXT_DIR/gemini-extension.json" ]] && printf 1 || printf 0)" "$EXT_DIR"
check "gate config written" "$([[ -f "$GATE_CONFIG" ]] && printf 1 || printf 0)" "$GATE_CONFIG"
if grep -q "missing settings" "$INSTALL_LOG"; then
  # A declared-but-unset extension setting makes the CLI print this warning,
  # which reads like a failed install. The manifest must not declare one.
  check "install output has no 'missing settings' warning" 0 "$(grep -m1 'missing settings' "$INSTALL_LOG")"
else
  check "install output has no 'missing settings' warning" 1
fi

python3 - "$GATE_CONFIG" "$EXT_DIR/hooks/hooks.json" <<'PY' >"$WORK/state.json"
import json, sys
print(json.dumps({
    "configured": json.load(open(sys.argv[1]))["events"],
    "installed_hooks": sorted(json.load(open(sys.argv[2]))["hooks"]),
}))
PY
check "config records the selected gates" \
  "$(contains "$(cat "$WORK/state.json")" '"configured": ["AfterAgent", "BeforeTool"]')" "$(cat "$WORK/state.json")"
check "installed hooks.json trimmed to the selection" \
  "$(contains "$(cat "$WORK/state.json")" '"installed_hooks": ["AfterAgent", "BeforeTool"]')" "$(cat "$WORK/state.json")"

CLI_JSON="$(cd "$HOME_DIR" && GEMINI_CLI_HOME="$HOME_DIR" gemini extensions list -o json 2>&1)"
check "CLI sees the extension as active" \
  "$(contains "$CLI_JSON" '"isActive": true')"
check "CLI sees only the selected gates" \
  "$(python3 -c '
import json, sys
raw = sys.stdin.read()
data = json.loads(raw[raw.find("["):raw.rfind("]") + 1])
entry = [e for e in data if e.get("name") == "gemini-quality-gate-jev"][0]
print("1" if sorted(entry.get("hooks", {})) == ["AfterAgent", "BeforeTool"] else "0")
' <<<"$CLI_JSON")"

run_hook() {
  printf '%s' "$1" | env -u TYPESAFE_API_KEY \
    HOME="$HOME_DIR" XDG_CONFIG_HOME="$HOME_DIR/.config" \
    TYPESAFE_API_URL="$MOCK_URL" \
    python3 "$EXT_DIR/scripts/jev_hook.py" 2>"$WORK/hook.err"
}

printf 'hook behaviour through the installed extension\n'
OUT="$(run_hook "{\"hook_event_name\":\"AfterAgent\",\"cwd\":\"$HOME_DIR\",\"prompt\":\"p\",\"prompt_response\":\"r\",\"stop_hook_active\":false}")"
check "AfterAgent denies when Jev asks for a correction" "$(contains "$OUT" '"decision": "deny"')" "$OUT"

OUT="$(run_hook "{\"hook_event_name\":\"BeforeTool\",\"cwd\":\"$HOME_DIR\",\"tool_name\":\"run_shell_command\",\"tool_input\":{\"command\":\"rm -rf build\"}}")"
check "BeforeTool denies a destructive command" "$(contains "$OUT" '"decision": "deny"')" "$OUT"

OUT="$(run_hook "{\"hook_event_name\":\"BeforeAgent\",\"cwd\":\"$HOME_DIR\",\"prompt\":\"refactor everything\"}")"
check "unselected BeforeAgent is a silent no-op" "$([[ -z "$OUT" ]] && printf 1 || printf 0)" "$OUT"

OUT="$(run_hook "{\"hook_event_name\":\"SessionStart\",\"cwd\":\"$HOME_DIR\",\"source\":\"startup\"}")"
check "unselected SessionStart is a silent no-op" "$([[ -z "$OUT" ]] && printf 1 || printf 0)" "$OUT"
check "only the selected gates called Jev" "$([[ "$(count_requests)" == "2" ]] && printf 1 || printf 0)" "requests=$(count_requests)"

printf 're-install with all gates\n'
BEFORE_REQUESTS="$(count_requests)"
HOME="$HOME_DIR" TYPESAFE_API_KEY=ts_e2e \
  bash "$SRC/install.sh" --global --repo "$SRC" --events all >"$WORK/install2.log" 2>&1 || true
python3 - "$GATE_CONFIG" "$EXT_DIR/hooks/hooks.json" <<'PY' >"$WORK/state2.json"
import json, sys
print(json.dumps({
    "configured": sorted(json.load(open(sys.argv[1]))["events"]),
    "installed_hooks": sorted(json.load(open(sys.argv[2]))["hooks"]),
}))
PY
check "re-install records every gate" \
  "$(contains "$(cat "$WORK/state2.json")" '["AfterAgent", "BeforeAgent", "BeforeTool", "SessionStart"]')" "$(cat "$WORK/state2.json")"
if grep -q 'the CLI reports gates' "$WORK/install2.log"; then
  check "no spurious gate mismatch warning after re-install" 0 "$(grep 'the CLI reports gates' "$WORK/install2.log")"
else
  check "no spurious gate mismatch warning after re-install" 1
fi

run_hook "{\"hook_event_name\":\"BeforeAgent\",\"cwd\":\"$HOME_DIR\",\"prompt\":\"refactor everything\"}" >/dev/null
check "BeforeAgent now reaches Jev" "$([[ "$(count_requests)" -gt "$BEFORE_REQUESTS" ]] && printf 1 || printf 0)" "requests=$(count_requests)"

printf 'uninstall --purge-key\n'
HOME="$HOME_DIR" bash "$SRC/install.sh" --uninstall --purge-key >"$WORK/uninstall.log" 2>&1 || true
check "extension removed" "$([[ -d "$EXT_DIR" ]] && printf 0 || printf 1)"
check "key file removed" "$([[ -f "$HOME_DIR/.config/typesafe/jev.env" ]] && printf 0 || printf 1)"
check "gate config removed" "$([[ -f "$GATE_CONFIG" ]] && printf 0 || printf 1)"

printf '\n'
if (( FAILURES == 0 )); then
  printf 'e2e: all checks passed\n'
else
  printf 'e2e: %d check(s) failed (work dir kept: %s)\n' "$FAILURES" "$WORK"
  trap - EXIT
  exit 1
fi
