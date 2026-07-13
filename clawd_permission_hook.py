#!/usr/bin/env python3
"""Clawd permission hook — lets the pet answer Claude Code permission prompts.

Registered under the PermissionRequest hook event (fires ONLY when a
permission dialog actually appears, never on allowlisted tool calls). The
flow is fail-open by design: any timeout or absent pet simply ends this
script with no output, and Claude Code shows its normal terminal prompt.

  1. send the query to the pet (token-authenticated UDP on localhost)
  2. wait up to ACK_TIMEOUT for the pet to confirm it is showing the bubble
     (no pet running -> we are done after half a second)
  3. wait up to DECISION_TIMEOUT for the user's click; "allow"/"deny" is
     printed as the hook decision, anything else falls back to the terminal

Stdlib only, like clawd_hook.py. The env overrides (CLAWD_PET_PORT,
CLAWD_TOKEN_FILE) exist for the selftest.
"""
import hmac
import json
import os
import secrets
import socket
import sys
from pathlib import Path

PORT = int(os.environ.get("CLAWD_PET_PORT", "52741"))
TOKEN_FILE = Path(os.environ.get("CLAWD_TOKEN_FILE",
                                 str(Path.home() / ".clawd" / "hook_token")))
ACK_TIMEOUT_S = 0.5        # pet must confirm this fast, else terminal prompt
DECISION_TIMEOUT_S = 15.0  # how long the user may take to click


def _detail(tool: str, inp) -> str:
    """Best-effort one-liner naming what the tool wants to touch."""
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "notebook_path", "pattern",
                "url", "query", "description"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().splitlines()[0][:60]
    return ""


def _recv(sock: socket.socket, token: str, qid: str):
    """One token-authenticated reply for our query id, or None on timeout."""
    try:
        data, _addr = sock.recvfrom(4096)
    except (socket.timeout, OSError):
        return None
    nl = data.find(b"\n")
    if nl <= 0:
        return None
    sent = data[:nl].decode("utf-8", errors="replace").strip()
    if not hmac.compare_digest(sent, token):
        return None
    try:
        reply = json.loads(data[nl + 1:].decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(reply, dict) or reply.get("id") != qid:
        return None
    return reply


def main() -> int:
    raw = sys.stdin.buffer.read()
    if not raw:
        return 0
    try:
        event = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return 0                       # no shared secret -> terminal prompt
    if not token:
        return 0

    qid = secrets.token_hex(8)
    query = {"clawd_permission": {
        "id": qid,
        "tool_name": str(event.get("tool_name") or ""),
        "detail": _detail(event.get("tool_name"), event.get("tool_input")),
    }}
    payload = token.encode("utf-8") + b"\n" + json.dumps(query).encode("utf-8")

    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(ACK_TIMEOUT_S)
        sock.sendto(payload, ("127.0.0.1", PORT))
        ack = _recv(sock, token, qid)
        if not ack or ack.get("type") != "ack":
            return 0                   # pet absent/busy -> terminal prompt
        sock.settimeout(DECISION_TIMEOUT_S)
        while True:
            reply = _recv(sock, token, qid)
            if reply is None:
                return 0               # user did not click in time
            if reply.get("type") != "decision":
                continue
            decision = reply.get("decision")
            if decision in ("allow", "deny"):
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": decision},
                }}))
            return 0                   # "pass" or anything else -> terminal
    except OSError:
        return 0
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
