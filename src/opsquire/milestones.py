"""`opsquire milestones`: what happened that someone outside the record would want to know, read straight from the
records with nothing to configure. Three kinds: `done`, a changing task closed (its outcome note); `failed`, an apply
or verify run of a changing task that did not end ok (the host may have been touched); `result`, a thread's result
written. Dry, read and ok verify runs, lint, handovers, asks and read-only tasks are never milestones. Steps run by a
plan are reported through the plan, not on their own. The daily handover and any poster, human or agent, build on
this; posting itself stays outside opsquire."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .model import OpsquireError, home_dir, iso, now
from .state import state

KINDS = ("done", "failed", "result")
NOT_A_CHANGE = {"ok", "blocked", "aborted"}
DURATION_RE = re.compile(r"^(\d+)([mhd])$")
CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})Z?$")


def since_arg(text: str | None, at: dt.datetime) -> str:
    """`--since` as an ISO prefix: a date or ISO time as given, `HH:MM` today (UTC), `2h`/`30m`/`1d` ago,
    or midnight UTC today when absent."""
    if not text:
        return f"{at:%Y-%m-%d}T00:00"
    m = DURATION_RE.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = dt.timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})
        return iso(at - delta)
    m = CLOCK_RE.match(text)
    if m:
        return f"{at:%Y-%m-%d}T{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text
    raise OpsquireError(f"--since {text!r}: use a date, an ISO time, HH:MM (UTC today) or 2h/30m/1d")


RECORD_PREFIX_RE = re.compile(r"^(?:apply|verify) run \S+ finished ok: [^.]*\.\s*(?:Results: )?", re.I)


def plain(note: str) -> str:
    """An auto-close note without its record-speak prefix: what happened, not which run said so."""
    return " ".join(RECORD_PREFIX_RE.sub("", note.strip()).split())


def last_paragraph(text: str) -> str:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip() and not p.strip().startswith("#")]
    return " ".join(parts[-1].split()) if parts else ""


def milestones(
    since: str,
    until: str | None = None,
    thread: str | None = None,
    kinds: tuple[str, ...] = KINDS,
    at: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    at = at or now()
    st = state(at, with_offboarded=True)
    tasks: dict[str, dict[str, Any]] = st["tasks"]
    runs: dict[str, dict[str, Any]] = st["runs"]
    threads = {str(th["id"]): th for th in st["threads"]}
    step_runs = {str(s.get("run_id")) for r in runs.values() for s in r.get("steps") or [] if s.get("run_id")}
    out: list[dict[str, Any]] = []

    def entry(kind: str, stamp: str, t: dict[str, Any] | None, tid: str | None, **more: Any) -> None:
        th = threads.get(str((t or {}).get("thread") or tid))
        if thread and (not th or th["id"] != thread) and (t or {}).get("thread") != thread:
            return
        if not (since <= stamp and (until is None or stamp <= until)):
            return
        out.append(
            {
                "at": stamp,
                "kind": kind,
                "thread": th["id"] if th else None,
                "thread_title": th["title"] if th else None,
                "task_id": (t or {}).get("id"),
                "area": (t or {}).get("area"),
                "targets": list((t or {}).get("targets") or []),
                "links": list((t or {}).get("links") or []),
                **more,
            }
        )

    for tid, t in tasks.items():
        if t["kind"] == "general" or not t["changes_prod"]:
            continue
        o = t.get("outcome")
        if "done" in kinds and o and o.get("closed") and str(o.get("run") or "") not in step_runs:
            entry(
                "done",
                str(o["closed"]),
                t,
                tid,
                state=o.get("state"),
                text=plain(str(o.get("note") or "")),
                run_id=o.get("run"),
                by=o.get("closed_by"),
            )
        if "failed" not in kinds:
            continue
        for rid in t["runs"]:
            r = runs.get(rid)
            if not r or r["mode"] not in ("apply", "verify") or r["status"] in NOT_A_CHANGE or rid in step_runs:
                continue
            failed = [k for k, v in (r.get("targets") or {}).items() if v not in ("ok", "skipped")]
            text = str(
                r.get("last_error")
                or "; ".join(str(x) for x in r.get("results") or [])
                or f"{r['mode']} ended {r['status']}"
            )
            entry(
                "failed",
                str(r.get("ended") or r.get("started")),
                t,
                tid,
                state=r["status"],
                mode=r["mode"],
                text=text,
                run_id=rid,
                failed_targets=failed,
            )
    if "result" in kinds:
        for th in st["threads"]:
            if not th.get("result"):
                continue
            path = home_dir() / "threads" / f"{th['id']}.md"
            try:
                stamp = iso(dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc))
            except OSError:
                continue
            entry(
                "result",
                stamp,
                {"thread": th["id"], "targets": th.get("targets"), "links": th.get("links")},
                None,
                text=last_paragraph(str(th["result"])),
            )
    return sorted(out, key=lambda m: (str(m["at"]), str(m["kind"])))


def render(items: list[dict[str, Any]], at: dt.datetime) -> str:
    """Grouped by thread, threads in the order of their latest milestone, items in time order within a thread."""
    if not items:
        return "no milestones in that window"
    today = f"{at:%Y-%m-%d}"
    groups: dict[str, list[dict[str, Any]]] = {}
    for m in items:
        groups.setdefault(str(m["thread_title"] or m["thread"] or m["task_id"] or "(no thread)"), []).append(m)
    lines: list[str] = []
    for head, members in sorted(groups.items(), key=lambda kv: str(kv[1][-1]["at"])):
        lines.append(("" if not lines else "\n") + head)
        for m in members:
            stamp = str(m["at"])
            when = stamp[11:16] + "Z" if stamp.startswith(today) else stamp[:16].replace("T", " ") + "Z"
            label = m["kind"] if m["kind"] != "failed" else f"{m.get('mode')} {m.get('state')}"
            subject = str(m["task_id"]).split("_", 2)[-1].replace("-", " ") if m["task_id"] else "thread result"
            where = ",".join(m.get("failed_targets") or m.get("targets") or [])
            text = " ".join(str(m.get("text") or "").split())
            tail = (f" [{where}]" if where else "") + (f": {text[:220]}" if text else "")
            lines.append(f"  {when}  {label:<14} {subject}{tail}")
    return "\n".join(lines)
