#!/usr/bin/env python3
"""Codex CLI notify hook -> Clawd (stdlib only, fire and forget).

Registered in ~/.codex/config.toml as
    notify = ["<python>", "<this file>"]
Codex invokes it with one JSON argument, e.g. an "agent-turn-complete"
notification. The event is forwarded to the running pet over the same
authenticated UDP protocol as the Claude hooks (token + newline + JSON).
No reply is awaited; on any problem the script exits 0 silently, so Codex
never sees an error from us. Env overrides CLAWD_PET_PORT and
CLAWD_TOKEN_FILE exist for the selftest.
"""
import json
import os
import socket
import sys
from pathlib import Path

PORT = int(os.environ.get("CLAWD_PET_PORT", "52741"))
TOKEN_FILE = Path(os.environ.get("CLAWD_TOKEN_FILE",
                                 str(Path.home() / ".clawd" / "hook_token")))


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    try:
        event = json.loads(sys.argv[-1])
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    kind = str(event.get("type") or "")
    if kind != "agent-turn-complete":
        return 0                       # only the documented notification
    msg = event.get("last-assistant-message") or event.get("last_assistant_message")
    payload = {"codex_turn": {
        "turn_id": event.get("turn-id") or event.get("turn_id"),
        "message": str(msg)[:200] if isinstance(msg, str) else None,
    }}
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if not token:
            return 0
        data = token.encode("utf-8") + b"\n" + json.dumps(payload).encode("utf-8")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(data, ("127.0.0.1", PORT))
        finally:
            sock.close()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
