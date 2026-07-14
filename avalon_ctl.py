"""
Canaan Avalon Q control via the local cgminer-compatible API on TCP port 4028.

No cloud, no app -- this talks directly to the miner on your LAN. Commands
are plain text over a raw TCP socket, one command per connection.

We use softoff/softon (standby toggle) rather than changing workmode
directly:
  - it's fast (no reboot involved)
  - the miner resumes its last workmode automatically on softon
So: set workmode to Super/max once via the Avalon Family app, then just
flip softoff/softon from this script.
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
    # cgminer responses are sometimes null-terminated JSON-ish text
    return b"".join(chunks).decode(errors="replace").strip("\x00").strip()


def get_summary(ip: str) -> str:
    return _send(ip, "summary")


def go_idle(ip: str) -> str:
    """
    Put the miner into standby (near-zero power draw). The Q requires a
    trigger timestamp suffix on softoff/softon -- a bare "1" is rejected
    with "parameter invalid". A few seconds in the future is standard.
    """
    trigger = int(time.time()) + 5
    return _send(ip, f"ascset|0,softoff,1:{trigger}")


def go_max(ip: str) -> str:
    """
    Wake the miner and force it to Super mode. We explicitly set the mode
    here every time rather than relying on "softon resumes the last mode"
    -- that only works if Super was set correctly via the app and never
    changed since, which is an easy thing to have drift.
    """
    trigger = int(time.time()) + 5
    softon_result = _send(ip, f"ascset|0,softon,1:{trigger}")
    workmode_result = _send(ip, "ascset|0,workmode,set,2")
    return f"{softon_result} | {workmode_result}"


def set_workmode(ip: str, mode: int) -> str:
    """
    0 = Eco, 1 = Standard, 2 = Super (highest hashrate/power draw) on Avalon Q.
    Only needs to be called once to set the "max" baseline -- softon then
    resumes whichever mode was last set here. Note the Q's syntax includes
    an extra "set" segment (ascset|0,workmode,set,<mode>), unlike older
    Avalon models (A10 etc) which use ascset|0,workmode,<mode> directly.
    """
    return _send(ip, f"ascset|0,workmode,set,{mode}")


import re

_FIELD_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\[([^\]]*)\]")


def parse_stats(raw: str) -> dict:
    """
    cgminer's STATS response packs data as Key[value] pairs inside one long
    string (see Canaan's docs). This pulls out the handful the dashboard
    cares about. Field names are best-effort based on Canaan's published
    format -- if your firmware reports something slightly different, run
    get_summary()/_send(ip, "estats") once by hand and adjust the keys
    below to match what you actually see.
    """
    fields = dict(_FIELD_RE.findall(raw))
    result = {}

    # Hashrate: prefer the INSTANTANEOUS reading (GHSspd) over the
    # averaged one (GHSavg/GHSmm/MGHS). The averages are computed over a
    # trailing window and don't drop to zero right away after the rig
    # goes idle -- using them as "current hashrate" made a freshly-idled
    # rig look like it was still mining at full tilt for a while after.
    # GHSspd genuinely reads 0.00 the moment it's idle, which is what a
    # "right now" dashboard figure should show.
    ghs = fields.get("GHSspd")
    if ghs is None:
        ghs = fields.get("GHSavg") or fields.get("MGHS")
    if ghs is not None:
        try:
            result["hashrate_ths"] = round(float(ghs) / 1000, 2)
        except ValueError:
            pass

    # Temp: TAvg reads 0 while idle (no active hashing to average across),
    # which would make an idled rig look stone cold when it may still be
    # warm. Fall back to the hashboard outlet temp, which reflects actual
    # current board temperature regardless of mining state.
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

    return result


def get_status(ip: str) -> dict:
    """
    Best-effort snapshot of a rig for the dashboard: reachable, hashrate,
    temp. Never raises -- returns reachable=False on any error so one dead
    rig doesn't take down the whole dashboard refresh.
    """
    try:
        raw = _send(ip, "estats")
        parsed = parse_stats(raw)
        parsed["reachable"] = True
        parsed["raw"] = raw[:500]  # trimmed, mostly for debugging
        return parsed
    except (OSError, TimeoutError, socket.error) as e:
        return {"reachable": False, "error": str(e)}
