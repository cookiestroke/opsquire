#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=opsquire.sh
source "$OPSQUIRE_HELPERS/opsquire.sh"

sq_step "Pre-checks (read-only)"

if [[ "$OPSQUIRE_MODE" == "apply" ]]; then
  sq_confirm "Apply the change?"
  sq_step "Apply"
fi

for name in $OPSQUIRE_TARGETS; do
  sq_target "$name" ok
done
sq_result "done"
