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
sq_target() { sq_emit event target target "$1" status "$2" msg "${3:-}"; }

sq_confirm() {
  local answer=""
  if { printf '>>> %s [y/N] ' "$*" >/dev/tty; } 2>/dev/null; then
    read -r answer </dev/tty || answer=""
  fi
  if [[ "$answer" == "y" ]]; then
    sq_emit event confirm msg "$*" answer y
  else
    sq_emit event confirm msg "$*" answer n
    exit 3
  fi
}
