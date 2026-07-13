"""
Small shared-state file so the controller (which knows what's happening)
and the dashboard (a separate process serving the web UI) can talk to each
other without a database. The controller writes state.json, everyone else
reads it. override.json works the same way but in the other direction:
the Telegram listener writes a manual override, the controller reads and
obeys it.

Kept deliberately simple: JSON files, atomic write-then-rename so no one
ever reads a half-written file.
"""

import json
import os
import tempfile
import time
from collections import deque

STATE_PATH = "/home/admin/solar_miner_controller/state.json"
OVERRIDE_PATH = "/home/admin/solar_miner_controller/override.json"
MAX_EVENTS = 1000

_events = deque(maxlen=MAX_EVENTS)


def _atomic_write(path: str, data: dict):
    dir_ = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def add_event(message: str, level: str = "info"):
    """Record an event for the dashboard's event log. level is one of
    'info', 'warn', 'error' -- used purely for coloring in the UI."""
    _events.appendleft({
        "time": time.time(),
        "level": level,
        "message": message,
    })


def get_events() -> list:
    return list(_events)


def write_state(power_kw, control_state, rigs: dict, connection_ok: bool, override: dict = None):
    """
    power_kw: float or None (None if we currently can't reach SolisCloud)
    control_state: 'idle' | 'max' | 'unknown'
    rigs: {ip: {"reachable": bool, "hashrate_ths": float|None,
                "temp_c": float|None, "raw": str|None}}
    connection_ok: whether the last SolisCloud poll succeeded
    override: the active manual override dict (from get_override()), or
        None if automatic control is in charge -- included here purely so
        the dashboard/Telegram status can show "(manual)" when relevant.
    """
    data = {
        "updated_at": time.time(),
        "power_kw": power_kw,
        "control_state": control_state,
        "connection_ok": connection_ok,
        "rigs": rigs,
        "events": get_events(),
        "override": override,
    }
    _atomic_write(STATE_PATH, data)


def read_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {
            "updated_at": None,
            "power_kw": None,
            "control_state": "unknown",
            "connection_ok": False,
            "rigs": {},
            "events": [],
            "override": None,
        }
    with open(STATE_PATH) as f:
        return json.load(f)


def set_override(mode: str, ttl_seconds: float = None):
    """
    mode: 'idle' or 'max'. ttl_seconds: how long the override holds before
    automatically expiring and handing control back to the automatic
    logic (None = never expires on its own -- not recommended, easy to
    forget about).
    """
    data = {
        "mode": mode,
        "set_at": time.time(),
        "expires_at": (time.time() + ttl_seconds) if ttl_seconds else None,
    }
    _atomic_write(OVERRIDE_PATH, data)


def get_override() -> dict:
    """Returns the active override dict, or None if there isn't one (or
    it's expired -- expiry is checked here, not enforced by a timer)."""
    if not os.path.exists(OVERRIDE_PATH):
        return None
    with open(OVERRIDE_PATH) as f:
        data = json.load(f)
    if data.get("expires_at") and time.time() > data["expires_at"]:
        return None
    return data


def clear_override():
    if os.path.exists(OVERRIDE_PATH):
        os.remove(OVERRIDE_PATH)


VERBOSE_PATH = "/home/admin/solar_miner_controller/verbose.json"


def set_verbose(enabled: bool):
    """When enabled, every info-level notifier.send() reaches Telegram
    immediately (still delivered on the next flush(), so up to ~one poll
    interval of delay -- effectively live). Persisted so the Telegram
    listener (which sets this) and the controller (which reads it) agree
    across process restarts."""
    _atomic_write(VERBOSE_PATH, {"enabled": enabled, "set_at": time.time()})


def get_verbose() -> bool:
    if not os.path.exists(VERBOSE_PATH):
        return False
    with open(VERBOSE_PATH) as f:
        data = json.load(f)
    return bool(data.get("enabled", False))
