"""One self-updating progress line on a terminal (plain lines every 10% elsewhere). Scripts report counts with
`sq.progress(label, done, total, unit)`; speed and time left are computed here from arrival times."""

from __future__ import annotations

import time
from typing import IO

WIDTH = 24
LOG_EVERY_S = 30.0


def human(value: float, unit: str) -> str:
    if unit != "B":
        return f"{value:g}"
    for size, name in ((1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
        if value >= size:
            return f"{value / size:.1f} {name}" if value < 10 * size else f"{value / size:.0f} {name}"
    return f"{value:.0f} B"


def amount(done: float, total: float, unit: str) -> str:
    """'113/181 MB' when both share a unit, '900 KB/2.0 MB' otherwise; plain counts get the unit appended."""
    if unit != "B":
        return f"{human(done, unit)}/{human(total, unit)}" + (f" {unit}" if unit else "")
    d, t = human(done, "B"), human(total, "B")
    if d.rsplit(" ", 1)[-1] == t.rsplit(" ", 1)[-1]:
        return f"{d.rsplit(' ', 1)[0]}/{t}"
    return f"{d}/{t}"


def duration(seconds: float) -> str:
    if seconds < 90:
        return f"~{seconds:.0f} s"
    if seconds < 5400:
        return f"~{seconds / 60:.0f} min"
    return f"~{seconds / 3600:.1f} h"


class ProgressBar:
    def __init__(self, out: IO[str]) -> None:
        self.out = out
        self.tty = out.isatty()
        self.active = False
        self.text = ""
        self.label = ""
        self.started = 0.0
        self.start_done = 0.0
        self.bucket = -1
        self.last_logged = 0.0

    def render(self, label: str, done: float, total: float, unit: str, now: float) -> str:
        frac = min(done / total, 1.0) if total > 0 else 0.0
        filled = int(frac * WIDTH)
        bar = "█" * filled + "░" * (WIDTH - filled)
        elapsed = now - self.started
        rate = (done - self.start_done) / elapsed if elapsed > 0.5 else 0.0
        size = amount(done, total, unit)
        parts = [f"{label} [{bar}] {size} {frac * 100:.0f}%"]
        if rate > 0:
            parts.append(f"{human(rate, unit)}/s" if unit == "B" else f"{rate:.1f}/s")
            if done < total:
                parts.append(duration((total - done) / rate))
        return " ".join(parts)

    def update(self, label: str, done: float, total: float, unit: str) -> str | None:
        """Draw the bar; return its text when it should also be logged (each 10%, every 30 s, at completion)."""
        now = time.monotonic()
        if label != self.label or done < self.start_done:
            self.label, self.started, self.start_done, self.bucket, self.last_logged = label, now, done, -1, now
        text = self.render(label, done, total, unit, now)
        finished = total > 0 and done >= total
        bucket = int(min(done / total, 1.0) * 10) if total > 0 else 0
        log = finished or bucket > self.bucket or now - self.last_logged >= LOG_EVERY_S
        if log:
            self.bucket, self.last_logged = bucket, now
        if self.tty:
            self.out.write("\r\033[K" + text + ("\n" if finished else ""))
            self.active, self.text = not finished, text
            self.out.flush()
        elif log:
            self.out.write(text + "\n")
            self.out.flush()
        if finished:
            self.label = ""
        return text if log else None

    def suspend(self) -> None:
        """Clear the bar so another line can be printed in its place."""
        if self.tty and self.active:
            self.out.write("\r\033[K")
            self.out.flush()

    def resume(self) -> None:
        if self.tty and self.active:
            self.out.write(self.text)
            self.out.flush()

    def close(self) -> None:
        if self.tty and self.active:
            self.out.write("\n")
            self.out.flush()
            self.active = False
