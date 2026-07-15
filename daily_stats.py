"""
Tracks running totals for the once-daily Telegram summary: per-rig time
spent in each running level (for the energy estimate), and today's power
min/max/avg. Resets automatically the first time it's accessed after local
midnight.

Kept separate from state_store.py's live state/override -- this data only
matters in aggregate at summary time, not on every poll.
"""

import json
import os
import tempfile
import time
from datetime import date, datetime

DAILY_STATS_PATH = "/home/admin/solar_miner_controller/daily_stats.json"


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


def _today_str() -> str:
    return date.today().isoformat()


def _default(rig_ips: list) -> dict:
    now = time.time()
    return {
        "date": _today_str(),
        # per-rig: {level_name: seconds} accumulated in each running level
        "rig_level_seconds": {ip: {} for ip in rig_ips},
        "rig_state": {ip: "idle" for ip in rig_ips},
        "rig_since": {ip: now for ip in rig_ips},
        "power_sum": 0.0,
        "power_count": 0,
        "power_max": None,
        "power_min": None,
        "notified_activation_today": False,
        "last_summary_sent_date": None,
    }


def _rollover(data: dict, rig_ips: list) -> dict:
    """New day: zero the accumulators, keep each rig's current level as-is
    (an ongoing session just starts counting fresh from midnight rather
    than exactly on the boundary -- fine for a rough daily estimate)."""
    now = time.time()
    data["date"] = _today_str()
    data["rig_level_seconds"] = {ip: {} for ip in rig_ips}
    data["rig_since"] = {ip: now for ip in rig_ips}
    data["power_sum"] = 0.0
    data["power_count"] = 0
    data["power_max"] = None
    data["power_min"] = None
    data["notified_activation_today"] = False
    return data


def _load(rig_ips: list) -> dict:
    if not os.path.exists(DAILY_STATS_PATH):
        data = _default(rig_ips)
    else:
        with open(DAILY_STATS_PATH) as f:
            data = json.load(f)
        for ip in rig_ips:
            data.setdefault("rig_level_seconds", {}).setdefault(ip, {})
            data.setdefault("rig_state", {}).setdefault(ip, "idle")
            data.setdefault("rig_since", {}).setdefault(ip, time.time())
        if data.get("date") != _today_str():
            data = _rollover(data, rig_ips)
    return data


def record_power_sample(power_kw, rig_ips: list):
    if power_kw is None:
        return
    data = _load(rig_ips)
    data["power_sum"] += power_kw
    data["power_count"] += 1
    data["power_max"] = power_kw if data["power_max"] is None else max(data["power_max"], power_kw)
    data["power_min"] = power_kw if data["power_min"] is None else min(data["power_min"], power_kw)
    _atomic_write(DAILY_STATS_PATH, data)


def record_rig_transition(ip: str, new_level: str, rig_ips: list):
    """Call whenever a rig's level actually changes. Credits the time just
    spent in the OLD level to that level's accumulator (idle time isn't
    tracked -- it draws ~nothing and isn't needed for the energy sum)."""
    data = _load(rig_ips)
    now = time.time()
    old_level = data["rig_state"].get(ip, "idle")
    since = data["rig_since"].get(ip, now)
    if old_level and old_level != "idle":
        per = data["rig_level_seconds"].setdefault(ip, {})
        per[old_level] = per.get(old_level, 0.0) + max(0.0, now - since)
    data["rig_state"][ip] = new_level
    data["rig_since"][ip] = now
    _atomic_write(DAILY_STATS_PATH, data)


def get_runtime_seconds(rig_ips: list) -> dict:
    """Per-rig {level: seconds} spent in each running level today,
    including the current ongoing session if applicable."""
    data = _load(rig_ips)
    now = time.time()
    out = {}
    for ip in rig_ips:
        per = dict(data["rig_level_seconds"].get(ip, {}))
        cur = data["rig_state"].get(ip, "idle")
        if cur and cur != "idle":
            per[cur] = per.get(cur, 0.0) + max(0.0, now - data["rig_since"].get(ip, now))
        out[ip] = per
    return out


def get_power_stats(rig_ips: list) -> dict:
    data = _load(rig_ips)
    avg = (data["power_sum"] / data["power_count"]) if data["power_count"] else None
    return {"avg": avg, "max": data["power_max"], "min": data["power_min"], "samples": data["power_count"]}


def should_notify_first_activation(rig_ips: list) -> bool:
    data = _load(rig_ips)
    return not data["notified_activation_today"]


def mark_activation_notified(rig_ips: list):
    data = _load(rig_ips)
    data["notified_activation_today"] = True
    _atomic_write(DAILY_STATS_PATH, data)


def should_send_daily_summary(rig_ips: list, hour_threshold: int) -> bool:
    """True once per day, the first time this is checked at/after
    hour_threshold (24hr, local time -- e.g. 18 for 6pm)."""
    data = _load(rig_ips)
    if data.get("last_summary_sent_date") == _today_str():
        return False
    return datetime.now().hour >= hour_threshold


def mark_summary_sent(rig_ips: list):
    data = _load(rig_ips)
    data["last_summary_sent_date"] = _today_str()
    _atomic_write(DAILY_STATS_PATH, data)
