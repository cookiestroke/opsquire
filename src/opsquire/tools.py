"""What an ask agent may use: the home's tools.json, decided per tool as allow, confirm or off.

An agent's tool is a server tool (named by the agent's retinue `tools.name_re`, which yields the server and the tool),
a shell command (the retinue's `tools.shell`, its command in the input) or another of the agent's own tools. Rules
are glob patterns; the longest matching pattern wins, then the server's default, then the home's default. A server is
off until it is switched on here, so a newly installed one does nothing for ask agents until the operator enables it.
Servers added here are given to ask agents only, never written into the agent's own configuration.

tools.json:
  {"default": "confirm",
   "servers": {"<server id>": {"enabled": true, "default": "confirm", "tools": {"read_*": "allow"}}},
   "shell": {"gh pr view *": "allow"},
   "agent_tools": {"WebFetch": "allow"},
   "added": {"<name>": {"url": "https://..."} or {"command": "...", "args": [...], "env": {...}}}}
"""

from __future__ import annotations

import contextlib
import fnmatch
import re
import subprocess
from typing import Any

from .model import OpsquireError, home_dir, iso, now, read_json, var_dir, write_json

FILE = "tools.json"
SEEN = "tools-seen.json"
CHOICES = ("allow", "confirm", "off")
OWN_SERVER = "opsquire"


def load() -> dict[str, Any]:
    path = home_dir() / FILE
    if not path.is_file():
        return {}
    cfg = read_json(path)
    if not isinstance(cfg, dict):
        raise OpsquireError(f"{path} must hold a JSON object")
    return cfg


def save(cfg: dict[str, Any]) -> None:
    write_json(home_dir() / FILE, cfg)


def _choice(value: Any, fallback: str) -> str:
    return value if value in CHOICES else fallback


def _best(rules: dict[str, Any], subject: str) -> str | None:
    hits = [p for p in rules if fnmatch.fnmatchcase(subject, p)]
    return _choice(rules[max(hits, key=len)], "confirm") if hits else None


def server_id(name: str, spec: dict[str, Any]) -> str:
    sub = spec.get("server_id") or {}
    return re.sub(str(sub.get("sub", r"[^A-Za-z0-9_-]")), str(sub.get("with", "_")), name)


def split_name(tool: str, spec: dict[str, Any]) -> tuple[str, str] | None:
    """(server id, tool) for a server tool, None for any other tool."""
    m = re.match(str(spec.get("name_re") or r"^$a"), tool)
    return (m.group("server"), m.group("tool")) if m else None


def decide(tool: str, tool_input: dict[str, Any], spec: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str]:
    """(allow | confirm | off, a label the operator reads)."""
    default = _choice(cfg.get("default"), "confirm")
    shell = spec.get("shell") or {}
    if shell and tool == shell.get("tool"):
        command = str(tool_input.get(shell.get("input", "command"), ""))
        return _best(cfg.get("shell") or {}, command) or default, f"shell: {command}"
    parts = split_name(tool, spec)
    if parts:
        sid, name = parts
        server = (cfg.get("servers") or {}).get(sid) or {}
        if server.get("enabled") is not True:
            return "off", f"{sid}: {name}"
        found = _best(server.get("tools") or {}, name)
        return found or _choice(server.get("default"), default), f"{sid}: {name}"
    return _best(cfg.get("agent_tools") or {}, tool) or default, tool


def mcp_config(cfg: dict[str, Any], approve_cmd: list[str]) -> dict[str, Any]:
    """The servers an ask agent gets on top of its own: opsquire's approval server and the home's added servers."""
    servers: dict[str, Any] = {OWN_SERVER: {"type": "stdio", "command": approve_cmd[0], "args": approve_cmd[1:]}}
    for name, s in (cfg.get("added") or {}).items():
        if not isinstance(s, dict) or s.get("enabled") is False:
            continue
        if s.get("url"):
            servers[name] = {
                "type": s.get("type", "http"),
                "url": s["url"],
                **({"headers": s["headers"]} if s.get("headers") else {}),
            }
        elif s.get("command"):
            servers[name] = {
                "type": "stdio",
                "command": s["command"],
                "args": list(s.get("args") or []),
                "env": dict(s.get("env") or {}),
            }
    return {"mcpServers": servers}


def disabled_servers(cfg: dict[str, Any]) -> list[str]:
    return sorted(
        sid for sid, s in (cfg.get("servers") or {}).items() if isinstance(s, dict) and s.get("enabled") is False
    )


def note_seen(tool: str) -> None:
    """Remember a tool an agent asked for, so Settings can offer it; never in the way of a decision."""
    path = var_dir() / SEEN
    with contextlib.suppress(OSError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_json(path) if path.is_file() else {}
        write_json(path, {**(data if isinstance(data, dict) else {}), tool: iso(now())})


def seen() -> dict[str, str]:
    path = var_dir() / SEEN
    try:
        data = read_json(path) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def listed_servers(spec: dict[str, Any], timeout: float = 30) -> list[dict[str, str]]:
    """The servers the agent itself knows (its `tools.list` command), as [{name, id, target, status}]."""
    lister = spec.get("list") or {}
    if not lister.get("argv"):
        return []
    try:
        r = subprocess.run(list(lister["argv"]), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    line_re = re.compile(str(lister.get("line_re", r"^(?P<name>[^:]+): (?P<target>.+?) - (?P<status>.+)$")))
    out = []
    for line in r.stdout.splitlines():
        m = line_re.match(line.strip())
        if m:
            name = m.group("name").strip()
            out.append(
                {"name": name, "id": server_id(name, spec), "target": m.group("target"), "status": m.group("status")}
            )
    return out


def view(spec: dict[str, Any], listed: list[dict[str, str]]) -> dict[str, Any]:
    """Everything the console's Settings shows: each server with its rules and the tools seen from it."""
    cfg = load()
    rules = cfg.get("servers") or {}
    servers: dict[str, dict[str, Any]] = {}
    for found in listed:
        if found["id"] != OWN_SERVER:
            servers[found["id"]] = {**found, "source": "agent", "seen": []}
    for name in cfg.get("added") or {}:
        sid = server_id(name, spec)
        servers.setdefault(sid, {"id": sid, "name": name, "status": "", "seen": []})["source"] = "added"
    other_seen: list[str] = []
    shell_tool = (spec.get("shell") or {}).get("tool")
    for tool in seen():
        parts = split_name(tool, spec)
        if parts and parts[0] != OWN_SERVER:
            servers.setdefault(
                parts[0], {"id": parts[0], "name": parts[0], "status": "", "source": "seen", "seen": []}
            )["seen"].append(parts[1])
        elif not parts and tool != shell_tool:
            other_seen.append(tool)
    for sid in rules:
        servers.setdefault(sid, {"id": sid, "name": sid, "status": "", "source": "rules", "seen": []})
    for sid, server in servers.items():
        r = rules.get(sid) or {}
        server.update(
            enabled=r.get("enabled") is True,
            default=r.get("default", ""),
            tools=r.get("tools") or {},
            seen=sorted(set(server["seen"])),
        )
    return {
        "default": _choice(cfg.get("default"), "confirm"),
        "servers": sorted(servers.values(), key=lambda s: s["name"].lower()),
        "shell": cfg.get("shell") or {},
        "agent_tools": cfg.get("agent_tools") or {},
        "agent_tools_seen": sorted(set(other_seen)),
        "added": cfg.get("added") or {},
        "can_list": bool((spec.get("list") or {}).get("argv")),
        "supported": bool(spec.get("approval_argv")),
    }


def update(change: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Apply one change from the console's Settings. Kinds: default, server (enabled/default), rule (server or
    shell or agent_tools pattern -> choice, or null to drop it), add, remove."""
    cfg = load()
    kind = change.get("kind")
    if kind == "default":
        cfg["default"] = _choice(change.get("value"), "confirm")
    elif kind == "server":
        s = cfg.setdefault("servers", {}).setdefault(str(change["id"]), {})
        if "enabled" in change:
            s["enabled"] = bool(change["enabled"])
        if "default" in change:
            if change["default"] in CHOICES:
                s["default"] = change["default"]
            else:
                s.pop("default", None)
    elif kind == "rule":
        scope, pattern, value = str(change.get("scope")), str(change.get("pattern", "")).strip(), change.get("value")
        if not pattern:
            raise OpsquireError("a rule needs a pattern")
        if scope in ("shell", "agent_tools"):
            rules = cfg.setdefault(scope, {})
        else:
            rules = cfg.setdefault("servers", {}).setdefault(scope, {}).setdefault("tools", {})
        if value is None:
            rules.pop(pattern, None)
        elif value in CHOICES:
            rules[pattern] = value
        else:
            raise OpsquireError(f"choice must be one of {', '.join(CHOICES)}")
    elif kind == "add":
        name = str(change.get("name", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,40}", name) or name == OWN_SERVER:
            raise OpsquireError("a server name is letters, digits, space, dot, dash or underscore")
        url, command = str(change.get("url") or "").strip(), str(change.get("command") or "").strip()
        if bool(url) == bool(command):
            raise OpsquireError("give a URL or a command, not both")
        if url and not re.match(r"https?://", url):
            raise OpsquireError("the URL must start with http:// or https://")
        entry: dict[str, Any] = {"url": url} if url else {"command": command, "args": list(change.get("args") or [])}
        cfg.setdefault("added", {})[name] = entry
        cfg.setdefault("servers", {}).setdefault(server_id(name, spec), {})["enabled"] = True
    elif kind == "remove":
        name = str(change.get("name"))
        (cfg.get("added") or {}).pop(name, None)
        (cfg.get("servers") or {}).pop(server_id(name, spec), None)
    else:
        raise OpsquireError(f"unknown settings change {kind!r}")
    save(cfg)
    return cfg
