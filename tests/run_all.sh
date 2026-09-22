#!/usr/bin/env bash
#
# Run every test suite:
#
#   ./tests/run_all.sh
#
# 1. hook behaviour tests (mock Jev, no network, no API key)
# 2. installer gate-selection tests (unit + --dry-run integration)
# 3. end-to-end install/uninstall against the real Gemini CLI (skipped if the
#    CLI is not installed)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
failures=0

run() {
  local label="$1"
  shift
  printf '\n=== %s ===\n' "$label"
  if ! "$@"; then
    failures=$((failures + 1))
  fi
}

run "hook behaviour" python3 "$ROOT/tests/test_jev_hook.py"
run "installer selection" python3 "$ROOT/tests/test_install_sh.py"
run "end-to-end install" bash "$ROOT/tests/e2e_install.sh"

printf '\n'
if (( failures == 0 )); then
  printf 'all suites passed\n'
else
  printf '%d suite(s) failed\n' "$failures"
  exit 1
fi
