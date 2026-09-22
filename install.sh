#!/usr/bin/env bash
#
# install.sh - install the gemini-quality-gate-jev extension for Gemini CLI,
# choose which Jev gates (hook events) are active, and store the API key.
#
# Scope:
#   --global            enabled for the current user (all projects).
#   --project PATH      installed once, enabled only inside PATH (Gemini CLI
#                       0.60.x loads extensions from ~/.gemini/extensions only,
#                       so a "project install" means workspace-scoped
#                       enablement).
#
# Gates (at least one, multi-select):
#   AfterAgent    after each answer: ask Jev whether one correction pass is needed
#   BeforeTool    before a tool runs: block or confirm destructive commands
#   BeforeAgent   before each request: refuse unsafe asks, nudge broad ones
#   SessionStart  at session start: inject a verification advisory
#
# Verified against Gemini CLI 0.60.x.

set -euo pipefail

readonly DEFAULT_REPO="https://github.com/ZongxingH/gemini-quality-gate-jev.git"
readonly EXTENSION_NAME="gemini-quality-gate-jev"
readonly KEY_VAR="TYPESAFE_API_KEY"
readonly MIN_GEMINI_VERSION="0.60.0"
readonly EVENT_NAMES=(AfterAgent BeforeTool BeforeAgent SessionStart)

repo="$DEFAULT_REPO"
ref=""
scope=""
project_dir=""
key_file=""
api_key=""
api_key_file=""
events=""
dry_run=0
uninstall=0
purge_key=0

usage() {
  cat <<'EOF'
Install (or remove) the gemini-quality-gate-jev extension for Gemini CLI.

Usage:
  ./install.sh --global  [--events LIST] [options]
  ./install.sh --project PATH [--events LIST] [options]
  ./install.sh --uninstall [--purge-key] [options]

Scope (default: --global):
  --global                 Enable the extension for the current user.
  --project PATH           Enable the extension only inside PATH.

Gates (hook events):
  --events LIST            Which gates to run. LIST is comma or space
                           separated, and may be repeated. At least one is
                           required. Without this option the script asks
                           interactively (default: AfterAgent).

                             AfterAgent    after each answer: ask Jev whether
                                           one correction pass is needed
                             BeforeTool    before a tool runs: block or
                                           confirm destructive commands and
                                           secret exposure
                             BeforeAgent   before each request: refuse unsafe
                                           asks, nudge broad ones to plan
                             SessionStart  at session start: inject a
                                           verification advisory for risky repos

                             all           select every gate

Source:
  --repo URL               Git repository to install from. A local directory
                           is accepted too (then --ref is not available).
                           Default: the official repository.
  --ref REF                Git branch, tag, or commit to install
                           (Git repositories only).

API key:
  --api-key KEY            Use KEY instead of prompting (visible in the
                           process list / shell history; prefer the options
                           below).
  --api-key-file PATH      Read the key from PATH (a bare key or a dotenv
                           file containing TYPESAFE_API_KEY=...).
  --key-file PATH          Where to store the key. Default:
                           ${XDG_CONFIG_HOME:-$HOME/.config}/typesafe/jev.env
                           (mode 600). The hooks read the same file.

Other:
  --uninstall              Remove the extension (keeps the key file and the
                           gate selection unless --purge-key is given).
  --purge-key              With --uninstall, also delete the stored key and
                           the gate configuration.
  --dry-run                Print the commands without running them.
  -h, --help               Show this help.

The key is also read from the TYPESAFE_API_KEY environment variable when set.
Without any of these the script prompts interactively with hidden input.

Notes:
  * The script agrees to the Gemini CLI extension consent prompt (--consent)
    on your behalf: it installs and enables a third-party extension, including
    its hooks, from the repository you selected.
  * The gate selection is stored in ${XDG_CONFIG_HOME:-$HOME/.config}/typesafe/jev.json
    and is also used to trim the installed hooks.json. Re-run the script any
    time to change which gates are active.
  * Gemini CLI does not expose a non-interactive way to configure extension
    settings, so the key is stored in a user-owned file rather than in the
    extension's OS-keychain setting.
EOF
}

log() { printf '%s\n' "$*" >&2; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

run() {
  if (( dry_run )); then
    printf '[dry-run] %s\n' "$*" >&2
    return 0
  fi
  "$@"
}

# Run a Gemini CLI command from DIR with a pinned, physical GEMINI_CLI_HOME.
# An optional --trust argument also bypasses the folder-trust prompts.
run_gemini_in() {
  local dir="$1"
  shift
  local trust=""
  if [[ "${1:-}" == "--trust" ]]; then
    trust="GEMINI_CLI_TRUST_WORKSPACE=true"
    shift
  fi
  if (( dry_run )); then
    printf '[dry-run] (%s GEMINI_CLI_HOME=%s cd %s && gemini %s)\n' \
      "$trust" "$gemini_home_root" "$dir" "$*" >&2
    return 0
  fi
  # shellcheck disable=SC2086
  ( cd "$dir" && env GEMINI_CLI_HOME="$gemini_home_root" $trust gemini "$@" )
}

# Read one line from the terminal, falling back to /dev/tty so that piping the
# script into bash still allows interactive answers. Returns non-zero when no
# terminal is available.
prompt_read() {
  local __var="$1"
  if [[ -t 0 ]]; then
    IFS= read -r "$__var" || return 1
    return 0
  fi
  if { IFS= read -r "$__var" < /dev/tty; } 2>/dev/null; then
    return 0
  fi
  return 1
}

# ------------------------------------------------------------------- events

event_desc() {
  case "$1" in
    AfterAgent) printf 'after each answer: ask Jev whether one correction pass is needed' ;;
    BeforeTool) printf 'before a tool runs: block or confirm destructive commands and secret exposure' ;;
    BeforeAgent) printf 'before each request: refuse unsafe asks, nudge broad ones to plan first' ;;
    SessionStart) printf 'at session start: inject a verification advisory for risky repos' ;;
    *) printf '' ;;
  esac
}

canonical_event() {
  local lowered candidate
  lowered="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  for candidate in "${EVENT_NAMES[@]}"; do
    if [[ "$(printf '%s' "$candidate" | tr '[:upper:]' '[:lower:]')" == "$lowered" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

# Appends the comma/space separated LIST to the global $events variable.
add_events_from_list() {
  local list="$1" token name
  list="${list//,/ }"
  for token in $list; do
    case "$token" in
      all|ALL|All)
        for name in "${EVENT_NAMES[@]}"; do
          case ",$events," in *",$name,"*) ;; *) events="${events:+$events,}$name" ;; esac
        done
        continue
        ;;
      [1-9])
        if (( token < 1 || token > ${#EVENT_NAMES[@]} )); then
          die "invalid gate number: $token (use 1-${#EVENT_NAMES[@]})"
        fi
        name="${EVENT_NAMES[$((token - 1))]}"
        ;;
      *)
        name="$(canonical_event "$token")" || die "unknown gate: $token"
        ;;
    esac
    case ",$events," in *",$name,"*) ;; *) events="${events:+$events,}$name" ;; esac
  done
}

select_events_interactively() {
  local i=1 name answer=""
  {
    printf '\nWhich Jev gates should be active? Choose one or more.\n\n'
    for name in "${EVENT_NAMES[@]}"; do
      printf '  %d) %-13s %s\n' "$i" "$name" "$(event_desc "$name")"
      i=$((i + 1))
    done
    printf '\nNumbers separated by spaces or commas, or "all" [default: 1 = AfterAgent]: '
  } >&2

  if ! prompt_read answer; then
    events="AfterAgent"
    warn "no terminal for the gate prompt; defaulting to --events AfterAgent"
    return 0
  fi

  answer="${answer%"${answer##*[![:space:]]}"}"
  if [[ -z "$answer" ]]; then
    events="AfterAgent"
    return 0
  fi
  add_events_from_list "$answer"
  [[ -n "$events" ]] || die "select at least one gate"
}

# ------------------------------------------------------------------- main
#
# Everything below runs only when the script is executed, so the file can also
# be sourced by tests to exercise the gate-selection logic directly.

main() {
# ---------------------------------------------------------------- arguments

while (($# > 0)); do
  case "$1" in
    --global)
      [[ -z "$scope" ]] || die "choose only one of --global or --project"
      scope="global"
      shift
      ;;
    --project)
      [[ $# -ge 2 ]] || die "--project requires a directory"
      [[ -z "$scope" ]] || die "choose only one of --global or --project"
      scope="project"
      project_dir="$2"
      shift 2
      ;;
    --events)
      [[ $# -ge 2 ]] || die "--events requires a list (for example: AfterAgent,BeforeTool)"
      add_events_from_list "$2"
      shift 2
      ;;
    --repo)
      [[ $# -ge 2 ]] || die "--repo requires a URL"
      repo="$2"
      shift 2
      ;;
    --ref)
      [[ $# -ge 2 ]] || die "--ref requires a branch, tag, or commit"
      ref="$2"
      shift 2
      ;;
    --api-key)
      [[ $# -ge 2 ]] || die "--api-key requires a value"
      api_key="$2"
      shift 2
      ;;
    --api-key-file)
      [[ $# -ge 2 ]] || die "--api-key-file requires a path"
      api_key_file="$2"
      shift 2
      ;;
    --key-file)
      [[ $# -ge 2 ]] || die "--key-file requires a path"
      key_file="$2"
      shift 2
      ;;
    --uninstall)
      uninstall=1
      shift
      ;;
    --purge-key)
      purge_key=1
      shift
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1 (try --help)"
      ;;
  esac
done

[[ -n "$repo" ]] || die "--repo must not be empty"
scope="${scope:-global}"

# ------------------------------------------------------------- environment

require_command gemini
require_command python3

# Gemini CLI treats URL-shaped sources as git and everything else as a local
# directory.
is_git_source() {
  case "$1" in
    http://*|https://*|git@*|sso://*|github:*|gitlab:*|ssh://*) return 0 ;;
    *) return 1 ;;
  esac
}

# Source validation matters only when installing; --uninstall never needs it.
if (( ! uninstall )); then
  if is_git_source "$repo"; then
    require_command git
    # Fail fast with a readable message instead of letting the CLI hang on a
    # credential prompt or report an opaque clone error.
    if ! GIT_TERMINAL_PROMPT=0 git ls-remote "$repo" HEAD >/dev/null 2>&1; then
      die "cannot reach the Git repository: $repo
       Check the URL, your network, and your Git credentials (for example by
       running: git ls-remote $repo HEAD)."
    fi
    if [[ -n "$ref" ]]; then
      # A commit id cannot be matched by ls-remote; let the clone resolve it.
      if [[ ! "$ref" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
        GIT_TERMINAL_PROMPT=0 git ls-remote --exit-code "$repo" "$ref" >/dev/null 2>&1 \
          || die "the ref '$ref' was not found in $repo"
      fi
    fi
  else
    [[ -e "$repo" ]] || die "install source not found: $repo (expected a Git URL or an existing path)"
    [[ -z "$ref" ]] || die "--ref is not applicable to a local path (Gemini CLI limitation); use a Git URL or commit the ref first"
    repo="$(cd "$repo" && pwd -P)"
    [[ -f "$repo/gemini-extension.json" ]] || die "not a Gemini CLI extension: $repo/gemini-extension.json is missing"
  fi
fi

gemini_version="$(gemini --version 2>/dev/null | tr -d '[:space:]' || true)"
# True when the first dotted version is lower than the second (portable: BSD
# sort has no -V).
version_lt() {
  python3 -c '
import sys
def parts(v):
    out = []
    for chunk in v.split(".")[:3]:
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits or 0))
    return out + [0] * (3 - len(out))
sys.exit(0 if parts(sys.argv[1]) < parts(sys.argv[2]) else 1)
' "$1" "$2"
}
if [[ -n "$gemini_version" ]]; then
  if version_lt "$gemini_version" "$MIN_GEMINI_VERSION"; then
    warn "Gemini CLI $gemini_version is older than $MIN_GEMINI_VERSION; extension hooks may not run."
  fi
else
  warn "could not determine the Gemini CLI version."
fi

config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
[[ -n "$key_file" ]] || key_file="$config_home/typesafe/jev.env"
gate_config="$config_home/typesafe/jev.json"

# Gemini CLI resolves its home as GEMINI_CLI_HOME, else $HOME. Pass the physical
# path so that workspace paths (always resolved) and the user-scope path used by
# `extensions enable/disable` (taken verbatim from the home setting) agree even
# when $HOME contains a symlink.
gemini_home_root="${GEMINI_CLI_HOME:-$HOME}"
gemini_home_root="$(cd "$gemini_home_root" 2>/dev/null && pwd -P || printf '%s' "$gemini_home_root")"
gemini_home="$gemini_home_root/.gemini"
extension_dir="$gemini_home/extensions/$EXTENSION_NAME"

if [[ "$scope" == "project" ]]; then
  [[ -d "$project_dir" ]] || die "project directory does not exist: $project_dir"
  project_dir="$(cd "$project_dir" && pwd -P)"
  case "$project_dir" in
    "$gemini_home_root"|"$gemini_home_root"/*) ;;
    *)
      warn "the project is outside the Gemini CLI home ($gemini_home_root);"
      warn "Gemini CLI can only scope the user-level disable to its home directory,"
      warn "so the extension stays enabled by default in other locations."
      ;;
  esac
fi

# ------------------------------------------------------------------- gates

if (( ! uninstall )) && [[ -z "$events" ]]; then
  select_events_interactively
fi
if (( ! uninstall )); then
  [[ -n "$events" ]] || die "select at least one gate with --events"
fi

# ------------------------------------------------------------------- key

read_key_from_file() {
  local path="$1" line value=""
  [[ -f "$path" ]] || die "API key file not found: $path"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    case "$line" in
      ''|'#'*) continue ;;
    esac
    case "$line" in
      "$KEY_VAR"=*)
        value="${line#*=}"
        break
        ;;
      *=*)
        [[ -n "$value" ]] || value="${line#*=}"
        ;;
      *)
        [[ -n "$value" ]] || value="$line"
        ;;
    esac
  done < "$path"
  printf '%s' "$value"
}

prompt_for_key() {
  local value=""
  if [[ -t 0 ]]; then
    printf 'TypeSafe API key (hidden input): ' >&2
    IFS= read -r -s value || true
    printf '\n' >&2
  elif { IFS= read -r -s value < /dev/tty; } 2>/dev/null; then
    printf '\n' >&2
  else
    die "no terminal available for the API key prompt; use --api-key-file or set $KEY_VAR"
  fi
  printf '%s' "$value"
}

resolve_api_key() {
  if [[ -n "$api_key" ]]; then
    return 0
  fi
  if [[ -n "$api_key_file" ]]; then
    api_key="$(read_key_from_file "$api_key_file")"
  elif [[ -n "${TYPESAFE_API_KEY:-}" ]]; then
    api_key="$TYPESAFE_API_KEY"
  elif (( dry_run )); then
    api_key="ts_dry_run_placeholder"
  else
    api_key="$(prompt_for_key)"
  fi
  # Trim surrounding whitespace and reject anything that cannot be a token.
  api_key="$(printf '%s' "$api_key" | tr -d '\r\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
}

write_key_file() {
  local dir tmp
  dir="$(dirname "$key_file")"
  if (( dry_run )); then
    printf '[dry-run] install -d -m 700 %s\n' "$dir" >&2
    printf '[dry-run] write %s=%s to %s (mode 600)\n' "$KEY_VAR" '***' "$key_file" >&2
    return 0
  fi
  mkdir -p "$dir"
  chmod 700 "$dir" 2>/dev/null || warn "could not restrict permissions on $dir"
  tmp="$(umask 077; mktemp "$dir/.jev.env.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -f '$tmp'" EXIT
  {
    printf '# TypeSafe Jev API key. Keep this file private (mode 600).\n'
    printf '%s=%s\n' "$KEY_VAR" "$api_key"
  } > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$key_file"
  trap - EXIT
}

# Merge the gate selection into jev.json, preserving any other keys the user
# has set there (thresholds, fail modes, ...).
write_gate_config() {
  local dir
  dir="$(dirname "$gate_config")"
  if (( dry_run )); then
    printf '[dry-run] record gates [%s] in %s\n' "$events" "$gate_config" >&2
    return 0
  fi
  mkdir -p "$dir"
  python3 - "$gate_config" "$events" <<'PY'
import json, os, sys, tempfile

path, events = sys.argv[1], [e for e in sys.argv[2].split(",") if e]
data = {}
try:
    with open(path, encoding="utf-8") as handle:
        loaded = json.load(handle)
    if isinstance(loaded, dict):
        data = loaded
except FileNotFoundError:
    pass
except (OSError, json.JSONDecodeError):
    pass

data["events"] = events
directory = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=directory, prefix=".jev.json.")
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
os.replace(tmp, path)
PY
  chmod 644 "$gate_config" 2>/dev/null || true
}

# Trim the installed hooks.json to the selected gates. This is an optimisation
# (no process is spawned for unselected events); the recorded selection in
# jev.json stays authoritative if an extension update restores every hook.
prune_installed_hooks() {
  local hooks_path="$extension_dir/hooks/hooks.json"
  if (( dry_run )); then
    printf '[dry-run] keep gates [%s] in %s\n' "$events" "$hooks_path" >&2
    return 0
  fi
  [[ -f "$hooks_path" ]] || return 0
  python3 - "$hooks_path" "$events" <<'PY' || warn "could not trim $hooks_path; the recorded gate selection still applies"
import json, sys

path, keep = sys.argv[1], {e for e in sys.argv[2].split(",") if e}
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
hooks = data.get("hooks")
if isinstance(hooks, dict):
    data["hooks"] = {name: value for name, value in hooks.items() if name in keep}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
PY
}

# ------------------------------------------------------------- gemini calls

# Gemini CLI refuses to install from an untrusted folder (and would otherwise
# record the current directory in ~/.gemini/trustedFolders.json). The script
# already asks for the user's consent by being run explicitly, so trust the
# invocation for this one command instead of editing the user's trust store.
install_extension_from() {
  local work_dir="$1"
  local -a args
  args=(extensions install "$repo" --consent --skip-settings)
  if [[ -n "$ref" ]]; then
    args+=(--ref "$ref")
  fi
  run_gemini_in "$work_dir" --trust "${args[@]}"
}

extension_installed() {
  [[ -f "$extension_dir/gemini-extension.json" ]]
}

remove_extension() {
  local work_dir="$1"
  if extension_installed; then
    run_gemini_in "$work_dir" extensions uninstall "$EXTENSION_NAME"
  else
    warn "$EXTENSION_NAME is not installed in $gemini_home/extensions; nothing to uninstall."
  fi
}

# Prints "<active|inactive|missing|unknown> <gates>", where gates is the
# comma separated list of hook events the CLI sees for the extension.
extension_state_in() {
  local work_dir="$1" state
  (( dry_run )) && { printf 'dry-run -' ; return 0; }
  # Gemini CLI writes the JSON report to stderr for command-mode output, and it
  # may prepend warning lines, so slice out the JSON array before parsing.
  state="$( cd "$work_dir" && GEMINI_CLI_HOME="$gemini_home_root" gemini extensions list -o json 2>&1 \
    | python3 -c '
import json, sys
name = sys.argv[1]
raw = sys.stdin.read()
start, end = raw.find("["), raw.rfind("]")
if start == -1 or end < start:
    print("unknown -")
    raise SystemExit(0)
try:
    data = json.loads(raw[start:end + 1])
except Exception:
    print("unknown -")
    raise SystemExit(0)
if not isinstance(data, list):
    print("unknown -")
    raise SystemExit(0)
for entry in data:
    if isinstance(entry, dict) and entry.get("name") == name:
        hooks = entry.get("hooks")
        gates = ",".join(sorted(hooks)) if isinstance(hooks, dict) else ""
        print(("active" if entry.get("isActive") else "inactive") + " " + (gates or "-"))
        break
else:
    print("missing -")
' "$EXTENSION_NAME" 2>/dev/null || printf 'unknown -' )"
  printf '%s' "$state"
}

# ---------------------------------------------------------------- uninstall

if (( uninstall )); then
  work_dir="${project_dir:-$PWD}"
  remove_extension "$work_dir"
  if (( purge_key )); then
    [[ -f "$key_file" ]] && { run rm -f "$key_file"; log "Removed key file $key_file"; }
    [[ -f "$gate_config" ]] && { run rm -f "$gate_config"; log "Removed gate configuration $gate_config"; }
  else
    [[ -f "$key_file" ]] && log "Kept key file $key_file (use --purge-key to delete it)."
    [[ -f "$gate_config" ]] && log "Kept gate configuration $gate_config."
  fi
  log "Done. Restart Gemini CLI."
  exit 0
fi

# ------------------------------------------------------------------ install

resolve_api_key
[[ -n "$api_key" ]] || die "$KEY_VAR is required (use --api-key-file, --api-key, the environment, or the hidden prompt)"

if extension_installed; then
  log "$EXTENSION_NAME is already installed; replacing it with $repo${ref:+ @ $ref}."
  remove_extension "$PWD"
fi

write_key_file

work_dir="${project_dir:-$PWD}"
install_extension_from "$work_dir"
if (( ! dry_run )); then
  log "note: a warning about the missing 'TypeSafe API key' extension setting is expected;"
  log "      the hooks read the key from $key_file instead."
fi

write_gate_config
prune_installed_hooks

if [[ "$scope" == "global" ]]; then
  run_gemini_in "$work_dir" extensions enable "$EXTENSION_NAME" --scope user
else
  # Installed extensions are enabled for the user by default. Disable the
  # user-scope activation, then enable it for the requested workspace only.
  run_gemini_in "$work_dir" extensions disable "$EXTENSION_NAME" --scope user
  run_gemini_in "$work_dir" extensions enable "$EXTENSION_NAME" --scope workspace
fi

# ----------------------------------------------------------------- verify

if (( dry_run )); then
  log "Dry run complete; no changes were made."
  exit 0
fi

state="$(extension_state_in "$work_dir")"
status="${state%% *}"
seen="${state#* }"

case "$status" in
  active)
    if [[ "$scope" == "global" ]]; then
      log "Installed and enabled $EXTENSION_NAME for the current user."
    else
      log "Installed $EXTENSION_NAME and enabled it for project $project_dir."
    fi
    ;;
  inactive)
    printf 'error: %s is installed but not active in %s.\n' "$EXTENSION_NAME" "$work_dir" >&2
    printf '       Enable it manually: (cd %s && gemini extensions enable %s --scope %s)\n' \
      "$work_dir" "$EXTENSION_NAME" "$([[ "$scope" == "global" ]] && printf 'user' || printf 'workspace')" >&2
    exit 1
    ;;
  missing)
    die "$EXTENSION_NAME is not visible to the Gemini CLI after installation; check the install output above."
    ;;
  *)
    warn "could not verify the activation state; run 'gemini extensions list' inside $work_dir."
    ;;
esac

if [[ -n "$seen" && "$seen" != "-" ]]; then
  # Compare as sets: the CLI may report the gates in a different order.
  seen_sorted="$(printf '%s' "$seen" | tr ',' '\n' | sort | tr '\n' ',')"
  events_sorted="$(printf '%s' "$events" | tr ',' '\n' | sort | tr '\n' ',')"
  if [[ "$seen_sorted" != "$events_sorted" ]]; then
    warn "the CLI reports gates [$seen] but [$events] was selected;"
    warn "run 'gemini extensions list' to inspect the extension."
  fi
fi

# ------------------------------------------------------------------ advice

log ""
log "Gates:     $events"
log "Gate conf: $gate_config"
log "API key:   $key_file (mode 600)"
log "Extension: $extension_dir"
case "$scope" in
  global)
    log "These gates now run in every project for this user."
    ;;
  project)
    log "These gates now run only inside $project_dir."
    log "Other projects stay untouched until you run the script for them."
    ;;
esac
log "Change gates later by re-running this script with --events."
log "Restart Gemini CLI before using the extension."
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
  main "$@"
fi
