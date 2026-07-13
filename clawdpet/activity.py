"""Real-time activity — Stufe 1: tail of the newest session log."""
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import ACTIVITY_IDLE_S, CODEX_ACTIVE_S, CODEX_SESSIONS_DIR

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


# ----------------------------------------------------------------------
#  Codex CLI fallback (F6) — mtime-based, tolerant of an unknown format
# ----------------------------------------------------------------------

def newest_codex_log(base: Path = CODEX_SESSIONS_DIR,
                     now: Optional[datetime] = None) -> Optional[Path]:
    """The newest still-active Codex CLI session log under base, or None.

    Codex CLI (OpenAI) drops *.jsonl rollout logs into nested date folders;
    only the mtime matters here — a log untouched for ACTIVITY_IDLE_S has
    gone quiet, exactly like a Claude session log. base/now are parameters
    for testability.
    """
    try:
        if not base.is_dir():
            return None
    except OSError:
        return None
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    best = None
    best_mtime = 0.0
    try:
        for f in base.rglob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue                  # racing deletion / permission — skip
            if now_ts - mtime > ACTIVITY_IDLE_S:
                continue
            if best is None or mtime > best_mtime:
                best, best_mtime = f, mtime
    except OSError:
        pass                              # directory vanished mid-walk
    return best


def _codex_text_field(value) -> str:
    """Plain prompt text from a string or list-of-blocks value, or ''.

    Strings starting with '<' are system/context wrappers, not typed prompts.
    """
    if isinstance(value, str):
        parts = [value]
    elif isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    else:
        return ""
    s = " ".join(" ".join(parts).split())
    return "" if not s or s.startswith("<") else s


def _codex_user_text(rec: dict) -> str:
    """A user-prompt-looking text from one Codex log record, or ''.

    Deliberately conservative: Codex wraps records in a few observed shapes
    ({"payload": {...}} or the record itself), and only candidates whose
    "type" mentions "user" or whose "role" is "user" are considered. Text is
    pulled from the common "content"/"text"/"message" fields. Anything
    unrecognized yields '' — guessing wrong would show garbage in the panel.
    """
    for cand in (rec.get("payload"), rec):
        if not isinstance(cand, dict):
            continue
        rtype = cand.get("type")
        typed_user = isinstance(rtype, str) and "user" in rtype.lower()
        if not typed_user and cand.get("role") != "user":
            continue
        for key in ("content", "text", "message"):
            txt = _codex_text_field(cand.get(key))
            if txt:
                return txt
    return ""


def read_codex_context(path: Path,
                       now: Optional[datetime] = None) -> Optional[SessionContext]:
    """Best-effort SessionContext for a Codex CLI session log.

    The Codex rollout format is undocumented and may change, so activity is
    judged purely by mtime: a file still being appended to (younger than
    CODEX_ACTIVE_S) means "working", a recently quiet one "waiting", an old
    one None. The task line is fished out of the tail defensively; when
    nothing safely user-prompt-like is found, task stays "" — that is fine,
    never an error. No exceptions escape.
    """
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    age = now_ts - mtime
    if age > ACTIVITY_IDLE_S:
        return None
    kind = "working" if age < CODEX_ACTIVE_S else "waiting"

    task = ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 32768))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        tail = ""
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
        txt = _codex_user_text(rec)
        if txt:
            task = txt[:160]
            break
    return SessionContext(kind=kind, tool=None, task=task, project="Codex CLI")
