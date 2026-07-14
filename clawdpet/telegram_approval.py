"""Remote approval via a personal Telegram bot (pure logic, no Qt).

The most-requested capability in the desktop-pet space: answer Claude
Code's permission prompts from your phone while a long agent run works.
The user brings their OWN bot (token from @BotFather) and chat id; both
live in ~/.clawd/telegram.json (0600). When a permission query engages,
the pet ALSO sends a Telegram message with Allow/Deny buttons and polls
getUpdates while the request is open — first answer wins (local click or
remote tap), and the fail-open contract is untouched: no answer anywhere
means Claude Code falls back to its normal terminal prompt.

Runs on a worker thread only (urllib blocks); the module itself has no
Qt dependencies. TELEGRAM_API is module-level so tests can point it at a
local fake server.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

TELEGRAM_API = "https://api.telegram.org"
CONFIG_FILE = Path.home() / ".clawd" / "telegram.json"
REMOTE_WINDOW_S = 110.0        # decision window while Telegram is configured
POLL_TIMEOUT_S = 2             # getUpdates long-poll per request


def load_config(path: Path = None) -> Optional[dict]:
    path = CONFIG_FILE if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = data.get("bot_token") if isinstance(data, dict) else None
    chat = data.get("chat_id") if isinstance(data, dict) else None
    if isinstance(token, str) and token.strip() and chat not in (None, ""):
        return {"bot_token": token.strip(), "chat_id": str(chat).strip()}
    return None


def save_config(bot_token: str, chat_id: str, path: Path = None) -> bool:
    path = CONFIG_FILE if path is None else path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"bot_token": bot_token.strip(),
                                   "chat_id": str(chat_id).strip()}),
                       encoding="utf-8")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)      # the bot token is a credential
        except OSError:
            pass
        return True
    except OSError:
        return False


def remove_config(path: Path = None) -> bool:
    path = CONFIG_FILE if path is None else path
    try:
        path.unlink()
        return True
    except OSError:
        return False


def telegram_configured(path: Path = None) -> bool:
    return load_config(path) is not None


def _call(cfg: dict, method: str, payload: dict, timeout: float = 10.0):
    """One bot-API call; None on any failure (fail-open everywhere)."""
    url = f"{TELEGRAM_API}/bot{cfg['bot_token']}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "ClawdPet/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    return data.get("result")


def send_permission_request(cfg: dict, qid: str, tool: str,
                            detail: str) -> Optional[int]:
    """Post the Allow/Deny card; returns the message id (needed to edit)."""
    text = "🦀 *Claude fragt:* `{}` erlauben?".format(tool or "?")
    if detail:
        text += "\n`{}`".format(detail[:120].replace("`", "'"))
    result = _call(cfg, "sendMessage", {
        "chat_id": cfg["chat_id"],
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": [[
            {"text": "✓ Erlauben", "callback_data": f"allow:{qid}"},
            {"text": "✕ Ablehnen", "callback_data": f"deny:{qid}"},
        ]]},
    })
    if isinstance(result, dict) and isinstance(result.get("message_id"), int):
        return result["message_id"]
    return None


def poll_decision(cfg: dict, qid: str, offset: Optional[int]):
    """One getUpdates pass. Returns (decision|None, new_offset).

    Only callback taps matching THIS query id count; every processed update
    advances the offset so stale taps are never replayed onto a later query."""
    result = _call(cfg, "getUpdates",
                   {"timeout": POLL_TIMEOUT_S, "allowed_updates":
                    ["callback_query"],
                    **({"offset": offset} if offset is not None else {})},
                   timeout=POLL_TIMEOUT_S + 8)
    decision = None
    if isinstance(result, list):
        for upd in result:
            if not isinstance(upd, dict):
                continue
            uid = upd.get("update_id")
            if isinstance(uid, int):
                offset = max(offset or 0, uid + 1)
            cq = upd.get("callback_query")
            if not isinstance(cq, dict):
                continue
            data = str(cq.get("data") or "")
            action, _, got_qid = data.partition(":")
            if got_qid != qid or action not in ("allow", "deny"):
                continue
            decision = action
            _call(cfg, "answerCallbackQuery",
                  {"callback_query_id": cq.get("id"),
                   "text": "✓ erlaubt" if action == "allow" else "✕ abgelehnt"},
                  timeout=5.0)
    return decision, offset


def finish_message(cfg: dict, message_id: Optional[int], outcome: str) -> None:
    """Replace the buttons with the outcome (best effort)."""
    if message_id is None:
        return
    texts = {"allow": "✓ Erlaubt", "deny": "✕ Abgelehnt",
             "local": "✓ Am Rechner beantwortet", "timeout": "⏱ Abgelaufen"}
    _call(cfg, "editMessageText", {
        "chat_id": cfg["chat_id"], "message_id": message_id,
        "text": "🦀 {}".format(texts.get(outcome, outcome)),
    }, timeout=5.0)


def remote_watch(cfg: dict, qid: str, tool: str, detail: str,
                 stop_event, deadline_mono: float, on_decision) -> None:
    """Worker-thread body: send the card, poll until decided/stopped/expired.

    on_decision(decision) is called at most once from THIS thread — the
    caller must marshal it back to the GUI thread (queue + QTimer)."""
    message_id = send_permission_request(cfg, qid, tool, detail)
    offset = None
    while not stop_event.is_set() and time.monotonic() < deadline_mono:
        decision, offset = poll_decision(cfg, qid, offset)
        if decision is not None:
            finish_message(cfg, message_id, decision)
            on_decision(decision)
            return
    finish_message(cfg, message_id,
                   "local" if stop_event.is_set() else "timeout")
