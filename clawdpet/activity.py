"""Real-time activity — Stufe 1: tail of the newest session log."""
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import ACTIVITY_IDLE_S

def tool_detail(name, inp) -> str:
    """Extract the concrete target from a tool_use input block (best effort)."""
    if not isinstance(inp, dict):
        return ""
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        return Path(fp).name if fp else ""
    if name in ("Bash", "PowerShell"):
        cmd = (inp.get("command") or "").strip().splitlines()
        return cmd[0][:48] if cmd else ""
    if name in ("Grep", "Glob"):
        return (inp.get("pattern") or "")[:40]
    if name in ("Task", "Agent"):
        return (inp.get("description") or "")[:40]
    if name == "WebFetch":
        return (inp.get("url") or "")[:48]
    if name == "WebSearch":
        return (inp.get("query") or "")[:40]
    return ""


def _user_prompt_text(content) -> str:
    """A genuine typed user prompt from a message's content, or '' for
    tool results / slash-command and system wrappers (which start with '<')."""
    if isinstance(content, str):
        s = content.strip()
    elif isinstance(content, list):
        s = " ".join(
            b.get("text", "").strip() for b in content
            if isinstance(b, dict) and b.get("type") == "text")
    else:
        return ""
    s = " ".join(s.split())
    return "" if not s or s.startswith("<") else s


@dataclass
class SessionContext:
    """What Claude is doing in the newest session, for the panel task view."""
    kind: Optional[str] = None    # "working" | "waiting" | None (idle)
    tool: Optional[str] = None    # current tool name
    detail: str = ""              # concrete target (file, command, pattern)
    task: str = ""                # latest genuine user prompt (truncated)
    project: str = ""             # working directory basename


def read_session_context(path: Path,
                         now: Optional[datetime] = None) -> Optional[SessionContext]:
    """Inspect the tail of a session log.

    Returns a SessionContext (current activity + the task Claude is working on
    + project), or None when the log has gone quiet. The .kind/.tool pair keeps
    the exact meaning of the old activity tuple so the mood logic is unchanged.
    """
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    if now_ts - mtime > ACTIVITY_IDLE_S:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 32768))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    ctx = SessionContext()
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue                      # first line may be cut by the seek
        if not isinstance(rec, dict):
            continue
        if not ctx.project and isinstance(rec.get("cwd"), str) and rec["cwd"]:
            ctx.project = Path(rec["cwd"]).name
        rtype = rec.get("type")
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else None

        if ctx.kind is None and msg is not None:
            if rtype == "assistant":
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "tool_use"):
                            ctx.kind = "working"
                            ctx.tool = block.get("name")
                            ctx.detail = tool_detail(ctx.tool, block.get("input"))
                            break
                if ctx.kind is None:
                    ctx.kind = "waiting"  # spoke without tools -> turn is over
            elif rtype == "user":
                ctx.kind = "working"      # tool result / prompt just arrived

        if not ctx.task and rtype == "user" and msg is not None:
            txt = _user_prompt_text(msg.get("content"))
            if txt:
                ctx.task = txt[:160]

        if ctx.kind is not None and ctx.task and ctx.project:
            break

    return ctx if ctx.kind is not None else None


def read_last_activity(path: Path, now: Optional[datetime] = None):
    """Backward-compatible activity tuple derived from the session context."""
    ctx = read_session_context(path, now)
    return (ctx.kind, ctx.tool) if ctx and ctx.kind else None
