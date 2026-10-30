import opsquire_helpers as sq


def main() -> None:
    sq.step("Pre-checks (read-only)")

    if sq.APPLY:
        sq.confirm("Apply the change?")
        sq.step("Apply")

    for name in sq.TARGETS:
        sq.target(name, "ok")
    sq.result("done")


if __name__ == "__main__":
    sq.run(main)
