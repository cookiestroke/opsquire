"""Verify check: establish, read-only, whether this task's intended end state is live on every target.

Report each target `ok` when the state is exactly what the task set out to make, `failed` when it is not, with a
message saying what was found. An all-ok verify run closes the task; a failed one goes to the judge."""

import opsquire_helpers as sq


def main() -> None:
    sq.step("Checking the intended state (read-only)")
    for name in sq.TARGETS:
        sq.warn(f"{name}: verify check not written yet; say what should be live and check it")
        sq.target(name, "skipped", "verify check not written")


if __name__ == "__main__":
    sq.run(main)
