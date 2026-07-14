#!/usr/bin/env python3
"""Clawd hook sender — forwards Claude Code hook events to the pet via UDP.

Registered in ~/.claude/settings.json by the pet's tray menu ("Echtzeit-Hooks
aktivieren"). Claude Code pipes the event JSON to stdin; we fire it at the
running pet on localhost and exit immediately. Stdlib only, so the per-event
overhead stays at interpreter startup (~50 ms with pythonw).

Each datagram is prefixed with the shared token from ~/.clawd/hook_token
(written by the pet on startup) so the pet can tell our events apart from
anything else a local process might send to the port.
"""
import json
import socket
import sys
from pathlib import Path

PORT = 52741
TOKEN_FILE = Path.home() / ".clawd" / "hook_token"


def main() -> int:
    raw = sys.stdin.buffer.read()
    if not raw:
        return 0
    try:
        json.loads(raw)
    except ValueError:
        return 0
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""            # no token yet -> the pet will drop the event
    payload = token.encode("utf-8") + b"\n" + raw[:60000]
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(payload, ("127.0.0.1", PORT))
        sock.close()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
