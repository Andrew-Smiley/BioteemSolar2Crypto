#!/usr/bin/env python3
"""
Listens for incoming Telegram messages: replies with live status, accepts
manual control commands, and toggles verbose mode. Also responds to posts
in the configured broadcast channel.

Manual commands set an override (see state_store.set_override) that the
main controller checks every poll cycle, so the controller's automatic
logic won't flip the rigs back. The override expires after OVERRIDE_TTL_SEC
or when "auto" is sent.

Uses long polling (getUpdates) -- no public HTTPS endpoint needed.

Security note: only responds to messages from TELEGRAM_CHAT_ID and posts
in the configured channel. Anything else is silently ignored.
"""

import logging
import time

import requests

import config
import state_store
import avalon_ctl
import f2pool_api

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("telegram_listener")

API_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

STATUS_TRIGGERS = {"status", "show me", "power", "/status"}
IDLE_COMMANDS = {"idle", "/idle"}
MAX_COMMANDS = {"max", "super", "/max", "/super"}
AUTO_COMMANDS = {"auto", "resume", "/auto", "/resume"}
VERBOSE_COMMANDS = {"verbose", "live", "/verbose", "/live"}
QUIET_COMMANDS = {"quiet", "normal", "/quiet", "/normal"}
HELP_COMMANDS = {"help", "/help", "commands", "/commands", "?"}

HELP_TEXT = (
    "Commands:\n"
    "  status -- current power/rig state\n"
    "  idle -- force rigs to idle (manual override)\n"
    "  max / super -- force rigs to super/max (manual override)\n"
    "  auto -- cancel manual override, resume automatic control\n"
    "  verbose / live -- get every event immediately instead of "
    "waiting for the 6pm daily summary\n"
    "  quiet / normal -- back to normal: only first activation of the "
    "day + warnings/errors ping immediately, everything else in the "
    "6pm summary\n"
    "  help -- this list"
)

OVERRIDE_TTL_SEC = getattr(config, "OVERRIDE_TTL_SEC", 2 * 60 * 60)


def format_status() -> str:
    state = state_store.read_state()
    power = state.get("power_kw")
    control_state = state.get("control_state", "unknown")
    connection_ok = state.get("connection_ok")
    rigs = state.get("rigs", {})
    updated_at = state.get("updated_at")
    override = state.get("override")

    lines = []
    if power is None:
        lines.append("Power: no data (can't reach SolisCloud right now)")
    else:
        lines.append(f"Power: {power:.1f} kW of 100kW cap")

    state_line = f"Rigs: {control_state.upper()}"
    if override:
        remaining = override.get("expires_at")
        if remaining:
            mins_left = max(0, int((remaining - time.time()) / 60))
            state_line += f" (MANUAL, {mins_left}m left -- send \"auto\" to cancel)"
        else:
            state_line += " (MANUAL -- send \"auto\" to cancel)"
    lines.append(state_line)

    if state_store.get_verbose():
        lines.append("Notifications: VERBOSE (live) -- send \"quiet\" for normal mode")

    if not connection_ok:
        lines.append("Inverter connection: DOWN")

    for ip, rig in rigs.items():
        commanded = rig.get("commanded_level", rig.get("commanded_state", "?")).upper()
        if rig.get("reachable"):
            hr = rig.get("hashrate_ths")
            temp = rig.get("temp_c")
            hr_str = f"{hr:.1f} TH/s" if hr is not None else "hashrate n/a"
            temp_str = f"{temp:.0f}\u00b0C" if temp is not None else "temp n/a"
            lines.append(f"  {ip}: {commanded}, {hr_str}, {temp_str}")
        else:
            lines.append(f"  {ip}: {commanded} (unreachable)")

    if updated_at:
        age = time.time() - updated_at
        lines.append(f"(as of {age:.0f}s ago)")

    if getattr(config, "F2POOL_ENABLED", False):
        lines.append(f2pool_api.get_daily_summary_line(config.F2POOL_CURRENCY, config.F2POOL_USERNAME, config.F2POOL_API_TOKEN))

    return "\n".join(lines)


def apply_manual_command(mode: str) -> str:
    """mode is 'idle' or 'max'. Sets the rigs immediately (so you get fast
    feedback) and sets the override so the controller doesn't undo it on
    its next poll. 'max' maps to the top running level (super)."""
    level = "super" if mode == "max" else "idle"
    results = []
    for ip in config.RIG_IPS:
        try:
            avalon_ctl.set_level(ip, level)
            results.append(f"{ip}: ok")
        except (OSError, TimeoutError) as e:
            results.append(f"{ip}: unreachable ({e})")

    state_store.set_override(mode, ttl_seconds=OVERRIDE_TTL_SEC)
    ttl_hours = OVERRIDE_TTL_SEC / 3600
    return (
        f"Manual override set: {level.upper()} for up to {ttl_hours:.1f}h "
        f"(or until you send \"auto\").\n" + "\n".join(results)
    )


def clear_manual_override() -> str:
    had_override = state_store.get_override() is not None
    state_store.clear_override()
    if had_override:
        return "Manual override cleared -- automatic control resumes on the next poll."
    return "No manual override was active."


def send_message(text: str, chat_id: str = None):
    if chat_id is None:
        chat_id = config.TELEGRAM_CHAT_ID
    try:
        resp = requests.post(
            f"{API_BASE}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if not resp.ok:
            log.warning(f"sendMessage to {chat_id} failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.warning(f"sendMessage to {chat_id} raised: {e}")


def set_verbose_mode(enabled: bool) -> str:
    state_store.set_verbose(enabled)
    if enabled:
        return "Verbose mode ON -- every event will ping immediately (up to ~1 poll interval delay) instead of waiting for the 6pm summary. Send \"quiet\" to go back to normal."
    return "Back to normal: only first activation of the day + warnings/errors ping immediately. Everything else in the 6pm summary."


def handle_text(text: str) -> str:
    normalized = text.strip().lower()

    if normalized in IDLE_COMMANDS:
        return apply_manual_command("idle")
    if normalized in MAX_COMMANDS:
        return apply_manual_command("max")
    if normalized in AUTO_COMMANDS:
        return clear_manual_override()
    if normalized in VERBOSE_COMMANDS:
        return set_verbose_mode(True)
    if normalized in QUIET_COMMANDS:
        return set_verbose_mode(False)
    if normalized in HELP_COMMANDS:
        return HELP_TEXT
    if any(trigger in normalized for trigger in STATUS_TRIGGERS):
        return format_status()

    return f"Not sure what you mean.\n\n{HELP_TEXT}"


def _channel_matches(chat: dict) -> bool:
    configured = str(getattr(config, "TELEGRAM_CHANNEL_CHAT_ID", "") or "")
    if not configured:
        return False
    if configured.startswith("@"):
        username = chat.get("username")
        return username is not None and f"@{username}".lower() == configured.lower()
    return str(chat.get("id", "")) == configured


def main():
    offset = 0
    log.info("Telegram listener started, polling for messages...")
    while True:
        try:
            resp = requests.get(
                f"{API_BASE}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            resp.raise_for_status()
            data = resp.json()

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message")
                channel_post = update.get("channel_post")

                if message:
                    chat = message.get("chat", {})
                    chat_id = str(chat.get("id", ""))
                    text = message.get("text") or ""

                    if chat_id != str(config.TELEGRAM_CHAT_ID):
                        log.info(f"Ignoring message from unrecognized chat_id {chat_id}")
                        continue

                    if text.strip():
                        send_message(handle_text(text))

                elif channel_post:
                    chat = channel_post.get("chat", {})
                    text = channel_post.get("text") or ""

                    if not _channel_matches(chat):
                        continue

                    if text.strip():
                        send_message(handle_text(text), chat_id=chat.get("id"))

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log.error(f"poll error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
