#!/usr/bin/env bash
# Verify check: establish, read-only, whether this task's intended end state is live on every target.
# Report each target ok when the state is exactly what the task set out to make, failed when it is not.
# An all-ok verify run closes the task; a failed one goes to the judge.
set -euo pipefail
# shellcheck source=opsquire.sh
source "$OPSQUIRE_HELPERS/opsquire.sh"

sq_step "Checking the intended state (read-only)"
for name in $OPSQUIRE_TARGETS; do
  sq_warn "$name: verify check not written yet; say what should be live and check it"
  sq_target "$name" skipped "verify check not written"
done
