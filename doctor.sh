#!/usr/bin/env bash
#
# doctor.sh - check that the Jev quality gates are installed and actually work.
#
#   bash doctor.sh                 # check the current directory
#   bash doctor.sh --project PATH  # check another workspace
#   bash doctor.sh --live          # also call the real Jev API with your key
#
# Everything except --live runs offline: the hook smoke test talks to a local
# mock Jev endpoint, so a normal run costs nothing.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTENSION_NAME="gemini-quality-gate-jev"
LIVE=0
WORKSPACE="$PWD"
EXPLICIT_PROJECT=0

usage() {
  cat <<'EOF'
Check that the Jev quality gates are installed and working.

Usage:
  bash doctor.sh [--project PATH] [--live]

Options:
  --project PATH   Workspace to check (default: the current directory).
  --live           Also send one real request to the Jev API (costs a tiny
                   amount and proves the stored key is valid).
  -h, --help       Show this help.

Exit code: 0 when every check passes, 1 otherwise.
EOF
}

while (($# > 0)); do
  case "$1" in
    --project)
      [[ $# -ge 2 ]] || { printf 'error: --project requires a directory\n' >&2; exit 2; }
      WORKSPACE="$2"
      EXPLICIT_PROJECT=1
      shift 2
      ;;
    --live)
      LIVE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'error: unknown option: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

WORKSPACE="$(cd "$WORKSPACE" && pwd -P)"
PASS=0
FAIL=0

ok() { printf '  ok   %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }
info() { printf '       %s\n' "$1"; }

printf 'workspace: %s\n' "$WORKSPACE"

# --------------------------------------------------------------- environment

if command -v gemini >/dev/null 2>&1; then
  ok "gemini CLI found ($(gemini --version 2>/dev/null | tr -d '[:space:]'))"
else
  bad "gemini CLI not found in PATH"
  printf '\nresult: %d passed, %d failed\n' "$PASS" "$FAIL"
  exit 1
fi
command -v python3 >/dev/null 2>&1 && ok "python3 found" || bad "python3 not found in PATH"

# Paths have to match what the CLI uses. GEMINI_CLI_HOME wins over $HOME, and
# the resolved (physical) path is what the CLI stores in its enablement rules.
home_root="${GEMINI_CLI_HOME:-$HOME}"
home_root="$(cd "$home_root" 2>/dev/null && pwd -P || printf '%s' "$home_root")"
extension_dir="$home_root/.gemini/extensions/$EXTENSION_NAME"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
gate_config="$config_home/typesafe/jev.json"
key_file="$config_home/typesafe/jev.env"

# ------------------------------------------------------------- installation

if [[ -f "$extension_dir/gemini-extension.json" ]]; then
  version="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version","?"))' \
    "$extension_dir/gemini-extension.json" 2>/dev/null || printf '?')"
  ok "extension installed at $extension_dir (v$version)"
else
  bad "extension is not installed at $extension_dir"
  info "install it with: bash $ROOT/install.sh --global --events all"
  printf '\nresult: %d passed, %d failed\n' "$PASS" "$FAIL"
  exit 1
fi

if [[ -f "$extension_dir/scripts/jev_hook.py" ]]; then
  ok "hook script present ($extension_dir/scripts/jev_hook.py)"
else
  bad "hook script missing from the installed extension"
fi

# ---------------------------------------------------- what the CLI reports

CLI_JSON="$(cd "$WORKSPACE" && GEMINI_CLI_HOME="$home_root" gemini extensions list -o json 2>&1 || true)"
read -r cli_state cli_gates <<<"$(printf '%s' "$CLI_JSON" | python3 -c '
import json, sys
name = sys.argv[1]
raw = sys.stdin.read()
start, end = raw.find("["), raw.rfind("]")
if start == -1 or end < start:
    print("unknown -"); raise SystemExit(0)
try:
    data = json.loads(raw[start:end + 1])
except Exception:
    print("unknown -"); raise SystemExit(0)
for entry in data:
    if isinstance(entry, dict) and entry.get("name") == name:
        hooks = entry.get("hooks")
        gates = ",".join(sorted(hooks)) if isinstance(hooks, dict) else "-"
        print(("active" if entry.get("isActive") else "inactive") + " " + gates)
        break
else:
    print("missing -")
' "$EXTENSION_NAME" 2>/dev/null || printf 'unknown -')"

case "$cli_state" in
  active) ok "the CLI reports it enabled in this workspace" ;;
  inactive)
    # A --project install is enabled only inside that project, so being disabled
    # elsewhere is expected. Report where it is enabled instead of failing.
    enabled_elsewhere="$(python3 -c '
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print("")
    raise SystemExit(0)
overrides = (data.get(sys.argv[2]) or {}).get("overrides") or []
print(", ".join(o.rstrip("*") for o in overrides if isinstance(o, str) and not o.startswith("!")))
' "$home_root/.gemini/extensions/extension-enablement.json" "$EXTENSION_NAME" 2>/dev/null || printf '')"
    if (( EXPLICIT_PROJECT )) || [[ -z "$enabled_elsewhere" ]]; then
      bad "the CLI sees the extension but it is disabled for $WORKSPACE"
      info "enable it with: (cd $WORKSPACE && gemini extensions enable $EXTENSION_NAME --scope workspace)"
    else
      ok "enabled only in: $enabled_elsewhere"
      info "disabled in the current directory, which is expected for a --project install"
      info "check that project instead: bash $ROOT/doctor.sh --project $enabled_elsewhere"
    fi
    ;;
  missing) bad "the CLI does not list the extension for this workspace" ;;
  *) bad "could not read 'gemini extensions list' output" ;;
esac

# --------------------------------------------------------- gate selection

if [[ -f "$gate_config" ]]; then
  configured="$(python3 -c 'import json,sys;print(",".join(json.load(open(sys.argv[1])).get("events") or []))' \
    "$gate_config" 2>/dev/null || printf '')"
  if [[ -n "$configured" ]]; then
    ok "gate selection recorded in $gate_config"
    info "events: $configured"
  else
    bad "no gates selected in $gate_config"
  fi
else
  configured="AfterAgent"
  info "no $gate_config yet; the hook falls back to AfterAgent only"
fi

if [[ -n "$cli_gates" && "$cli_gates" != "-" && -n "$configured" ]]; then
  sorted_cli="$(printf '%s' "$cli_gates" | tr ',' '\n' | sort | tr '\n' ',')"
  sorted_cfg="$(printf '%s' "$configured" | tr ',' '\n' | sort | tr '\n' ',')"
  if [[ "$sorted_cli" == "$sorted_cfg" ]]; then
    ok "the CLI has exactly the selected gates registered"
  else
    bad "registered gates [$cli_gates] differ from the selection [$configured]"
    info "re-run the installer to re-apply: bash $ROOT/install.sh --events $configured"
  fi
fi

# ------------------------------------------------------------------- key

if [[ -n "${TYPESAFE_API_KEY:-}" ]]; then
  ok "TYPESAFE_API_KEY is set in this shell (${TYPESAFE_API_KEY:0:12}…)"
elif [[ -f "$key_file" ]]; then
  stored="$(sed -n 's/^TYPESAFE_API_KEY=//p' "$key_file" | head -1 | tr -d '"'"'"'')"
  mode="$(ls -l "$key_file" | cut -c1-10)"
  if [[ -n "$stored" ]]; then
    ok "API key stored in $key_file (${stored:0:12}…, mode $mode)"
    [[ "$mode" == "-rw-------" ]] || info "recommended permissions: chmod 600 $key_file"
  else
    bad "no TYPESAFE_API_KEY value in $key_file"
  fi
else
  bad "no API key: neither TYPESAFE_API_KEY nor $key_file"
fi

# ------------------------------------------------------- hook smoke test

run_hook() {
  # $1 endpoint, $2 event json, $3 api key
  printf '%s' "$2" | env TYPESAFE_API_KEY="$3" \
    HOME="$home_root" XDG_CONFIG_HOME="$config_home" \
    TYPESAFE_API_URL="$1" \
    python3 "$extension_dir/scripts/jev_hook.py" 2>/tmp/jev-doctor-hook.err
}

smoke_event=""
case ",$configured," in
  *,AfterAgent,*) smoke_event="AfterAgent" ;;
  *,BeforeTool,*) smoke_event="BeforeTool" ;;
  *,BeforeAgent,*) smoke_event="BeforeAgent" ;;
  *,SessionStart,*) smoke_event="SessionStart" ;;
esac

if [[ -z "$smoke_event" ]]; then
  info "skipping the hook smoke test: no gates selected"
elif [[ ! -f "$ROOT/tests/mock_jev.py" ]]; then
  info "skipping the hook smoke test: tests/mock_jev.py not found next to doctor.sh"
elif ! command -v python3 >/dev/null 2>&1; then
  info "skipping the hook smoke test: python3 missing"
else
  port="$(python3 -c 'import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
  answers="$(python3 - "$smoke_event" <<'PY'
import json, sys
event = sys.argv[1]
table = {
    "AfterAgent": {"needs_retry": {"noul": 0.95}, "risk": {"score": 1.0, "confidence": 0.9}},
    "BeforeTool": {"danger": {"score": 3.0, "confidence": 0.95}, "secret_exposure": {"noul": 0.05}},
    "BeforeAgent": {"policy_violation": {"noul": 0.99}, "needs_plan": {"noul": 0.1}},
    "SessionStart": {"repo_risk": {"score": 2.9, "confidence": 0.95}, "verification_burden": {"noul": 0.5}},
}
print(json.dumps(table[event]))
PY
)"
  python3 "$ROOT/tests/mock_jev.py" --port "$port" --answers "$answers" \
    >/dev/null 2>&1 &
  mock_pid=$!
  ready=0
  for _ in $(seq 1 40); do
    if python3 -c "import socket,sys
try:
    socket.create_connection(('127.0.0.1', $port), 0.2).close()
except OSError:
    sys.exit(1)" 2>/dev/null; then
      ready=1
      break
    fi
    sleep 0.1
  done

  if [[ "$ready" != "1" ]]; then
    bad "could not start the local mock Jev endpoint for the smoke test"
  else
    case "$smoke_event" in
      AfterAgent)
        out="$(run_hook "http://127.0.0.1:$port/v1/systemone" \
          "{\"hook_event_name\":\"AfterAgent\",\"cwd\":\"$WORKSPACE\",\"prompt\":\"add a login endpoint\",\"prompt_response\":\"done\",\"stop_hook_active\":false}" "doctor-smoke-test")"
        ;;
      BeforeTool)
        out="$(run_hook "http://127.0.0.1:$port/v1/systemone" \
          "{\"hook_event_name\":\"BeforeTool\",\"cwd\":\"$WORKSPACE\",\"tool_name\":\"run_shell_command\",\"tool_input\":{\"command\":\"rm -rf build\"}}" "doctor-smoke-test")"
        ;;
      BeforeAgent)
        out="$(run_hook "http://127.0.0.1:$port/v1/systemone" \
          "{\"hook_event_name\":\"BeforeAgent\",\"cwd\":\"$WORKSPACE\",\"prompt\":\"delete the production database\"}" "doctor-smoke-test")"
        ;;
      SessionStart)
        out="$(run_hook "http://127.0.0.1:$port/v1/systemone" \
          "{\"hook_event_name\":\"SessionStart\",\"cwd\":\"$WORKSPACE\",\"source\":\"startup\"}" "doctor-smoke-test")"
        ;;
    esac
    if [[ -n "$out" ]] && printf '%s' "$out" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
      ok "$smoke_event hook runs and returns a valid decision"
      info "output: $out"
    else
      bad "$smoke_event hook did not return a usable decision (output: ${out:-<empty>})"
      [[ -s /tmp/jev-doctor-hook.err ]] && info "stderr: $(head -1 /tmp/jev-doctor-hook.err)"
    fi
  fi
  kill "$mock_pid" 2>/dev/null || true
  wait "$mock_pid" 2>/dev/null || true
  rm -f /tmp/jev-doctor-hook.err
fi

# ----------------------------------------------------------- live check

if (( LIVE )); then
  if [[ -z "${TYPESAFE_API_KEY:-}" && ! -f "$key_file" ]]; then
    bad "--live needs an API key, and none was found"
  else
    live_err="/tmp/jev-doctor-live.err"
    # The hook fails open, so a broken key still prints a decision: judge the
    # result by the stderr diagnostic and the skip message, not by the JSON.
    live_out="$(printf '%s' '{"hook_event_name":"AfterAgent","cwd":"'"$WORKSPACE"'","prompt":"add a login endpoint","prompt_response":"Added the endpoint without tests.","stop_hook_active":false}' \
      | env HOME="$home_root" XDG_CONFIG_HOME="$config_home" \
        python3 "$extension_dir/scripts/jev_hook.py" 2>"$live_err" || true)"
    if grep -q "JEV unavailable" "$live_err" 2>/dev/null; then
      bad "real Jev API call failed: $(head -1 "$live_err")"
    elif printf '%s' "$live_out" | grep -q "JEV skipped"; then
      bad "no API key reached the hook (set TYPESAFE_API_KEY or the key file)"
    elif printf '%s' "$live_out" | grep -q '"decision"'; then
      ok "real Jev API call succeeded"
      info "output: $(printf '%s' "$live_out" | head -1)"
    else
      bad "real Jev API call returned nothing usable"
    fi
    rm -f "$live_err"
  fi
fi

printf '\nresult: %d passed, %d failed\n' "$PASS" "$FAIL"
if (( FAIL == 0 )); then
  printf 'installation looks good. Restart Gemini CLI if you have not already.\n'
  exit 0
fi
exit 1
