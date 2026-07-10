#!/usr/bin/env python3
"""Clawd hook sender — forwards Claude Code hook events to the pet via UDP.

Registered in ~/.claude/settings.json by the pet's tray menu ("Echtzeit-Hooks
aktivieren"). Claude Code pipes the event JSON to stdin; we fire it at the
running pet on localhost and exit immediately. Stdlib only, so the per-event
overhead stays at interpreter startup (~50 ms with pythonw).
"""
import json
import socket
import sys

PORT = 52741


def main() -> int:
    raw = sys.stdin.buffer.read()
    if not raw:
        return 0
    try:
        json.loads(raw)
    except ValueError:
        return 0
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(raw[:60000], ("127.0.0.1", PORT))
        sock.close()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
