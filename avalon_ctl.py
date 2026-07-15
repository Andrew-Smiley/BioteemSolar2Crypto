"""
Canaan Avalon Q control via the local cgminer-compatible API on TCP port 4028.

No cloud, no app -- this talks directly to the miner on your LAN. Commands
are plain text over a raw TCP socket, one command per connection.
"""

import socket
import json
import time

PORT = 4028
TIMEOUT = 5


def _send(ip: str, command: str) -> str:
    with socket.create_connection((ip, PORT), timeout=TIMEOUT) as s:
        s.sendall(command.encode())
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode(errors="replace").strip("\x00").strip()


def get_summary(ip: str) -> str:
    return _send(ip, "summary")


def go_idle(ip: str) -> str:
    """Put the miner into standby. Kept for backward compatibility --
    prefer set_level(ip, "idle")."""
    return set_level(ip, "idle")


def go_max(ip: str) -> str:
    """Wake and set to Super. Kept for backward compatibility -- prefer
    set_level(ip, "super")."""
    return set_level(ip, "super")


def set_workmode(ip: str, mode: int) -> str:
    """
    0 = Eco, 1 = Standard, 2 = Super (highest hashrate/power draw) on Avalon Q.
    Note the Q's syntax includes an extra "set" segment
    (ascset|0,workmode,set,<mode>), unlike older Avalon models (A10 etc)
    which use ascset|0,workmode,<mode> directly.
    """
    return _send(ip, f"ascset|0,workmode,set,{mode}")


# --- Level-based control -------------------------------------------------
# The Avalon Q has three running power modes plus standby. We model this as
# four ordered levels so the controller can step up/down through them:
#
#   0 idle     -> standby (softoff), ~0W draw
#   1 eco      -> workmode 0, ~850W
#   2 standard -> workmode 1, ~1400W
#   3 super    -> workmode 2, ~1674W
#
# Note the off-by-one that bites easily: the human ordering (eco < standard
# < super) matches Canaan's workmode numbering (0 < 1 < 2), but our LEVEL
# index is +1 relative to workmode because level 0 is "idle", which isn't a
# workmode at all. LEVELS/LEVEL_INDEX below are the single source of truth
# for that mapping so it doesn't get open-coded (and mis-coded) elsewhere.

LEVELS = ["idle", "eco", "standard", "super"]
LEVEL_INDEX = {name: i for i, name in enumerate(LEVELS)}

# Approx per-rig wall draw at each level (watts), from Canaan specs and
# independent reviews. Used only for the controller's load-aware step math
# and the energy estimate -- not precise metering.
LEVEL_WATTS = {"idle": 7, "eco": 850, "standard": 1400, "super": 1674}

# level name -> Canaan workmode number (idle has none; handled separately)
_LEVEL_TO_WORKMODE = {"eco": 0, "standard": 1, "super": 2}


def set_level(ip: str, level: str) -> str:
    """
    Put a rig into one of: idle, eco, standard, super.

    idle uses softoff (standby). The running modes use softon (to make sure
    it's awake, in case it was idled) followed by the matching workmode set.
    Both softoff/softon need the timestamp-trigger suffix the Q requires.
    """
    if level not in LEVEL_INDEX:
        raise ValueError(f"unknown level {level!r}, expected one of {LEVELS}")

    trigger = int(time.time()) + 5

    if level == "idle":
        return _send(ip, f"ascset|0,softoff,1:{trigger}")

    softon_result = _send(ip, f"ascset|0,softon,1:{trigger}")
    workmode_result = _send(ip, f"ascset|0,workmode,set,{_LEVEL_TO_WORKMODE[level]}")
    return f"{softon_result} | {workmode_result}"


import re

_FIELD_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\[([^\]]*)\]")


def parse_stats(raw: str) -> dict:
    """
    cgminer's STATS response packs data as Key[value] pairs inside one long
    string. This pulls out the handful the dashboard cares about.
    """
    fields = dict(_FIELD_RE.findall(raw))
    result = {}

    # Hashrate: prefer the INSTANTANEOUS reading (GHSspd) over the averaged
    # one (GHSavg/MGHS), which lags and doesn't drop to zero right away
    # after the rig goes idle.
    ghs = fields.get("GHSspd")
    if ghs is None:
        ghs = fields.get("GHSavg") or fields.get("MGHS")
    if ghs is not None:
        try:
            result["hashrate_ths"] = round(float(ghs) / 1000, 2)
        except ValueError:
            pass

    # Temp: TAvg reads 0 while idle; fall back to hashboard outlet temp.
    temp = fields.get("TAvg")
    if not temp or float(temp) == 0:
        temp = fields.get("HBOTemp") or fields.get("ITemp")
    if temp:
        try:
            result["temp_c"] = float(temp)
        except ValueError:
            pass

    fan = fields.get("FanR")
    if fan:
        result["fan_pct"] = fan.strip("%")

    # Actual current level, read back from the rig itself. SYSTEMSTATU
    # contains "In Idle" when in standby; otherwise WORKMODE (0/1/2) maps
    # to eco/standard/super.
    systemstatu = fields.get("SYSTEMSTATU", "")
    hashrate = result.get("hashrate_ths", 0) or 0
    if "idle" in systemstatu.lower() or hashrate == 0:
        result["level"] = "idle"
    else:
        workmode = fields.get("WORKMODE")
        wm_to_level = {"0": "eco", "1": "standard", "2": "super"}
        result["level"] = wm_to_level.get(workmode, "super")

    return result


def get_status(ip: str) -> dict:
    """
    Best-effort snapshot of a rig for the dashboard: reachable, hashrate,
    temp, level. Never raises -- returns reachable=False on any error.
    """
    try:
        raw = _send(ip, "estats")
        parsed = parse_stats(raw)
        parsed["reachable"] = True
        parsed["raw"] = raw[:500]
        return parsed
    except (OSError, TimeoutError, socket.error) as e:
        return {"reachable": False, "error": str(e)}
