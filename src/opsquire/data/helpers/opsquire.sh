#!/usr/bin/env bash
# Structured events for bash scripts run by `opsquire run`: source "$OPSQUIRE_HELPERS/opsquire.sh"

OPSQUIRE_MODE="${OPSQUIRE_MODE:-dry}"
export OPSQUIRE_MODE

# Redefine sq_cleanup after sourcing to undo scratch work; it runs on Ctrl-C, SIGTERM and SIGHUP.
sq_cleanup() { :; }
sq__interrupted() {
  trap - INT TERM HUP
  sq_cleanup
  sq_warn "interrupted by the operator"
  exit 130
}
trap sq__interrupted INT TERM HUP

sq_emit() {
  python3 -c 'import json, sys; a = sys.argv[1:]; print("@@OPSQUIRE " + json.dumps(dict(zip(a[0::2], a[1::2]))), flush=True)' "$@"
}

sq_step() { sq_emit event step msg "$*"; }
sq_result() { sq_emit event result msg "$*"; }
sq_warn() { sq_emit event warn msg "$*"; }
sq_error() { sq_emit event error msg "$*"; }
sq_progress() { sq_emit event progress msg "$1" "done" "$2" total "$3" unit "${4:-}"; }
sq_target() { sq_emit event target target "$1" status "$2" msg "${3:-}"; }

sq__wait_for_echo() {
  if [[ -n "${OPSQUIRE_SYNC_FD:-}" ]]; then
    sq_emit event sync
    read -r -t 10 -n 1 -u "$OPSQUIRE_SYNC_FD" _ 2>/dev/null || true
  fi
}

sq_confirm() {
  local answer=""
  sq__wait_for_echo
  if { printf '\n>>> %s [y/N] ' "$*" >/dev/tty; } 2>/dev/null; then
    read -r answer </dev/tty || answer=""
  fi
  if [[ "$answer" == "y" ]]; then
    sq_emit event confirm msg "$*" answer y
  else
    sq_emit event confirm msg "$*" answer n
    exit 3
  fi
}
