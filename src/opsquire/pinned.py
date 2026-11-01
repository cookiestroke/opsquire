"""Interview questions pinned to the bottom of the terminal: while one waits for an answer, agent progress keeps
scrolling in the region above it instead of landing inside the prompt. Falls back to a plain prompt when the
terminal is too small, unknown, or progress goes somewhere else."""

from __future__ import annotations

import os
import sys
import textwrap
import threading
from typing import IO

LOCK = threading.Lock()


class Pin:
    """The question block currently pinned, if any; `bottom` is the last row of the scrolling region."""

    out: IO[str] | None = None
    bottom = 0
    cols = 0


def plain(question: str, recommend: str) -> str | None:
    with open("/dev/tty", "w") as out, open("/dev/tty") as inp:
        out.write(f"\n? {question}\n  [Enter = {recommend}] > ")
        out.flush()
        return inp.readline() or None


def layout(question: str, recommend: str, cols: int) -> list[str]:
    width = max(cols - 1, 20)
    lines = textwrap.wrap(f"? {question}", width, subsequent_indent="  ")
    prompt = f"  [Enter = {recommend}] > "
    if len(prompt) + 12 > width:
        lines += textwrap.wrap(f"Recommended: {recommend}", width, initial_indent="  ", subsequent_indent="  ")
        prompt = "  [Enter = recommended] > "
    return ["─" * width, *lines, prompt]


def ask(question: str, recommend: str) -> str | None:
    """Ask on the terminal; the raw line typed (None at end of input)."""
    try:
        cols, rows = os.get_terminal_size(2) if os.isatty(2) else (0, 0)
    except OSError:
        cols, rows = 0, 0
    block = layout(question, recommend, cols)
    if os.environ.get("TERM", "dumb") == "dumb" or rows < 10 or cols < 30 or len(block) > rows // 2:
        return plain(question, recommend)
    bottom = rows - len(block)
    with open("/dev/tty", "w") as out, open("/dev/tty") as inp:
        with LOCK:
            sys.stderr.flush()
            out.write("\n" * len(block) + f"\033[1;{bottom}r")
            for i, text in enumerate(block):
                out.write(f"\033[{bottom + 1 + i};1H\033[2K{text}")
            out.flush()
            Pin.out, Pin.bottom, Pin.cols = out, bottom, cols
        try:
            return inp.readline() or None
        finally:
            with LOCK:
                Pin.out = None
                out.write("\033[r" + "".join(f"\033[{r};1H\033[2K" for r in range(bottom + 1, rows + 1)))
                out.write(f"\033[{bottom};1H\n")
                out.flush()


def line(msg: str) -> bool:
    """Print a progress line above a pinned question; False when nothing is pinned (print it normally)."""
    out = Pin.out
    if out is None:
        return False
    text = msg.replace("\n", " ")[: Pin.cols - 1]
    out.write(f"\0337\033[{Pin.bottom};1H\n{text}\0338")
    out.flush()
    return True
