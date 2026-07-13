"""
Notifier: logs every event to the shared event log immediately (so the
dashboard is always fully granular), but is selective about what
actually reaches Telegram.

Default behavior: warn/error always go to Telegram right away. info-level
messages are dashboard-only unless a caller opts in with telegram=True, OR
verbose mode is on (state_store.get_verbose(), toggled via "verbose"/
"quiet" in Telegram) -- verbose mode makes every info-level message go out
immediately too, overriding a telegram=False from the caller.

Telegram delivery is still batched within a poll cycle (call flush() once
per cycle) and broadcasts to every chat ID in the target list -- your
personal chat plus, optionally, a read-only channel.
"""

import logging
import requests

import config
import state_store

log = logging.getLogger("notifier")

_pending = []  # [(level, message), ...] queued since the last flush()

_LEVEL_PREFIX = {"info": "\u2139\ufe0f", "warn": "\u26a0\ufe0f", "error": "\U0001F6D1"}


def _broadcast_targets() -> list:
    targets = [config.TELEGRAM_CHAT_ID]
    channel_id = getattr(config, "TELEGRAM_CHANNEL_CHAT_ID", None)
    if channel_id:
        targets.append(channel_id)
    return targets


def send(message: str, level: str = "info", telegram: bool = None):
    """
    Log immediately (dashboard sees it right away). Reaches Telegram if:
      - level is 'warn'/'error' (always immediate, regardless of telegram=), or
      - verbose mode is on (state_store.get_verbose()) -- every info-level
        message goes out too, overriding a telegram=False from the caller, or
      - the caller explicitly passed telegram=True
    Otherwise info-level messages are dashboard-only.
    """
    state_store.add_event(message, level)

    if level in ("warn", "error"):
        should_send = True
    elif state_store.get_verbose():
        should_send = True
    elif telegram is not None:
        should_send = telegram
    else:
        should_send = False

    if should_send:
        _pending.append((level, message))


def flush():
    """
    Send everything queued since the last flush as a single Telegram
    message to every broadcast target, then clear the queue. No-op if
    nothing was queued, or if Telegram is disabled. Call this once per
    poll cycle, after all of that cycle's send() calls.
    """
    if not _pending:
        return
    if not getattr(config, "TELEGRAM_ENABLED", False):
        _pending.clear()
        return

    lines = [f"{_LEVEL_PREFIX.get(level, '')} {msg}".strip() for level, msg in _pending]
    text = "\n".join(lines)
    _pending.clear()

    for chat_id in _broadcast_targets():
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": text},
                timeout=10,
            )
            if not resp.ok:
                log.warning(f"telegram send to {chat_id} failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            log.warning(f"telegram send to {chat_id} raised: {e}")
