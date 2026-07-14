#!/usr/bin/env python3
"""Clawd statusline — context-window fill for Claude Code's status line.

Registered in ~/.claude/settings.json as {"statusLine": {"type": "command",
"command": ...}} by the pet's tray menu ("Kontext-Anzeige aktivieren").
Claude Code pipes a JSON payload to stdin on every update and displays the
first stdout line as the status line. We print a compact fill indicator and
fire the percentage at the running pet via authenticated UDP (token +
newline + JSON, the same protocol as clawd_hook.py) — fire and forget, no
reply is awaited.

Stdlib only, and it must never crash: whatever stdin holds, something is
printed and the exit code is 0, so Claude Code always has a status line.
The env overrides (CLAWD_PET_PORT, CLAWD_TOKEN_FILE) exist for the selftest.
"""
import json
import os
import socket
import sys
from pathlib import Path

PORT = int(os.environ.get("CLAWD_PET_PORT", "52741"))
TOKEN_FILE = Path(os.environ.get("CLAWD_TOKEN_FILE",
                                 str(Path.home() / ".clawd" / "hook_token")))


def _print_line(text: str) -> None:
    """Print the status line; survive exotic console encodings (Windows)."""
    try:
        print(text)
    except Exception:
        try:
            sys.stdout.buffer.write(text.encode("utf-8", "replace") + b"\n")
        except Exception:
            pass


def _parse_payload(raw: bytes):
    """(pct, model, session_id) from the statusline JSON, all best-effort."""
    pct = model = session_id = None
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return None, None, None
    if not isinstance(data, dict):
        return None, None, None
    cw = data.get("context_window")
    if isinstance(cw, dict):
        val = cw.get("used_percentage")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            pct = max(0.0, min(100.0, float(val)))
    mi = data.get("model")
    if isinstance(mi, dict):
        name = mi.get("display_name") or mi.get("id")
        if isinstance(name, str) and name.strip():
            model = name.strip()
    sid = data.get("session_id")
    if isinstance(sid, str) and sid:
        session_id = sid
    return pct, model, session_id


def main() -> int:
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        raw = b""
    pct, model, session_id = _parse_payload(raw)

    if pct is not None:                       # forward to the pet (best effort)
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""        # no token yet -> the pet will drop the datagram
        payload = {"clawd_statusline": {"context_pct": pct, "model": model,
                                        "session_id": session_id}}
        datagram = (token.encode("utf-8") + b"\n"
                    + json.dumps(payload).encode("utf-8"))
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(datagram, ("127.0.0.1", PORT))
            sock.close()
        except OSError:
            pass

    line = f"🦀 {pct:.0f}% Kontext" if pct is not None else "🦀 Kontext ?"
    if model:
        line += f" · {model}"
    _print_line(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)               # never break Claude Code's status line
