#!/usr/bin/env python3
"""
Main control loop: poll SolisCloud for current output power and drive both
Avalon Q rigs together through power LEVELS (idle -> eco -> standard ->
super) to soak up solar output near the 100kW export cap without exceeding
it. Publishes shared state for the dashboard.

--- Control model ---
Both rigs share a single level (they move together). Levels, in order:
    idle (standby, ~0W) -> eco (~850W) -> standard (~1400W) -> super (~1674W)

Ramp UP: while export reading is >= RAMP_UP_KW (default 99), step up one
level, then wait STEP_SETTLE_SEC (default 60s) before considering the next
step. The settle wait matters because each rig stepping up a level pulls
its extra draw straight off the export reading -- without the wait we'd see
that self-induced dip and immediately reverse (flap).

Ramp DOWN: based on the current reading, at most one decision per settle
interval:
    97-99 kW  -> hold (target band, near the cap)
    95-97 kW  -> step down 1 level
    90-95 kW  -> step down 2 levels
    < 90 kW   -> straight to idle, IMMEDIATELY (no settle wait) -- the
                 fast-drop / cloud-cover case where reacting now matters
                 more than avoiding a flap, and idle is the floor anyway.

Recovery: if output climbs back to >= RAMP_UP_KW mid-descent, it resumes
ramping up by the normal rule.

Manual override (set via Telegram) pauses all of this and holds both rigs
at idle or super until it expires or is cleared.

Run under systemd so it restarts on crash/reboot.
"""

import logging
import time
import sys
from datetime import date

import config
import solis_api
import avalon_ctl
import notifier
import state_store
import daily_stats
import f2pool_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("controller")

LEVELS = avalon_ctl.LEVELS  # ["idle", "eco", "standard", "super"]

RAMP_UP_KW = getattr(config, "RAMP_UP_KW", 99.0)
STEP_SETTLE_SEC = getattr(config, "STEP_SETTLE_SEC", 60)
HOLD_FLOOR_KW = getattr(config, "HOLD_FLOOR_KW", 97.0)
DOWN1_FLOOR_KW = getattr(config, "DOWN1_FLOOR_KW", 95.0)
DOWN2_FLOOR_KW = getattr(config, "DOWN2_FLOOR_KW", 90.0)


def set_all_rigs_level(level: str, unreachable_rigs: set):
    """Command every reachable rig to `level`. Records transitions for the
    daily energy estimate, and alerts once per rig on going unreachable /
    coming back."""
    for ip in config.RIG_IPS:
        try:
            result = avalon_ctl.set_level(ip, level)
            log.info(f"rig {ip} -> {level}: {result[:120]}")
            daily_stats.record_rig_transition(ip, level, config.RIG_IPS)
            if ip in unreachable_rigs:
                unreachable_rigs.discard(ip)
                notifier.send(f"Rig {ip} back online, now set to {level}.", level="info")
        except (OSError, TimeoutError) as e:
            log.warning(f"rig {ip} unreachable while setting {level}: {e}")
            if ip not in unreachable_rigs:
                unreachable_rigs.add(ip)
                notifier.send(f"Rig {ip} unreachable while trying to set {level}: {e}", level="warn")


def collect_rig_statuses(commanded_level: str) -> dict:
    """Live telemetry per rig for the dashboard, plus the level we believe
    all rigs should be at. Never raises."""
    statuses = {}
    for ip in config.RIG_IPS:
        status = avalon_ctl.get_status(ip)
        status["commanded_level"] = commanded_level
        statuses[ip] = status
    return statuses


def probe_actual_level() -> str:
    """
    On startup, read the rigs' REAL current level rather than assuming idle.
    Returns the HIGHEST level any reachable rig reports, so we err toward
    "there's load running we need to manage" rather than losing track of a
    rig left mining across a restart.
    """
    highest = "idle"
    for ip in config.RIG_IPS:
        status = avalon_ctl.get_status(ip)
        if not status.get("reachable"):
            continue
        lvl = status.get("level", "idle")
        if avalon_ctl.LEVEL_INDEX.get(lvl, 0) > avalon_ctl.LEVEL_INDEX.get(highest, 0):
            highest = lvl
    return highest


def build_daily_summary(power_kw, current_level: str) -> str:
    runtime = daily_stats.get_runtime_seconds(config.RIG_IPS)
    power_stats = daily_stats.get_power_stats(config.RIG_IPS)

    today_str = date.today().isoformat()
    today_epoch_start = time.mktime(time.strptime(today_str, "%Y-%m-%d"))
    events = [e for e in state_store.get_events() if e["time"] >= today_epoch_start]
    info_count = sum(1 for e in events if e["level"] == "info")
    warn_count = sum(1 for e in events if e["level"] == "warn")
    error_count = sum(1 for e in events if e["level"] == "error")

    lines = [f"Daily summary -- {today_str}"]
    if power_kw is not None:
        lines.append(f"Current: {power_kw:.1f}kW, rigs at {current_level.upper()}")
    else:
        lines.append("Current: no data")
    lines.append(f"Events today: {len(events)} ({info_count} info, {warn_count} warnings, {error_count} errors)")

    if power_stats["samples"]:
        lines.append(
            f"Output today: avg {power_stats['avg']:.1f}kW, "
            f"peak {power_stats['max']:.1f}kW, low {power_stats['min']:.1f}kW"
        )

    total_kwh = 0.0
    for ip in config.RIG_IPS:
        per_level = runtime.get(ip, {})
        rig_kwh = 0.0
        parts = []
        for lvl in ("eco", "standard", "super"):
            secs = per_level.get(lvl, 0.0) if isinstance(per_level, dict) else 0.0
            if secs > 0:
                hrs = secs / 3600
                kwh = hrs * (avalon_ctl.LEVEL_WATTS[lvl] / 1000)
                rig_kwh += kwh
                parts.append(f"{lvl} {hrs:.1f}h")
        total_kwh += rig_kwh
        detail = ", ".join(parts) if parts else "idle all day"
        lines.append(f"  {ip}: {detail} (~{rig_kwh:.1f} kWh)")
    lines.append(f"Total est. energy burned: ~{total_kwh:.1f} kWh")

    if getattr(config, "F2POOL_ENABLED", False):
        lines.append(f2pool_api.get_daily_summary_line(config.F2POOL_CURRENCY, config.F2POOL_USERNAME, config.F2POOL_API_TOKEN))

    notable = [e["message"] for e in events if e["level"] in ("warn", "error")]
    if notable:
        shown = notable[:10]
        lines.append("Notable issues:")
        lines.extend(f"  - {m}" for m in shown)
        if len(notable) > len(shown):
            lines.append(f"  (+{len(notable) - len(shown)} more, see dashboard)")

    return "\n".join(lines)


def decide_target_level(current_idx: int, power_kw: float) -> tuple:
    """
    Pure decision function: given the current level index and the export
    reading, return (target_idx, immediate) where `immediate` means skip
    the settle wait (only used for the fast-drop-to-idle case). Returns
    current_idx unchanged when the right move is to hold.
    """
    if power_kw >= RAMP_UP_KW:
        return min(current_idx + 1, len(LEVELS) - 1), False
    if power_kw >= HOLD_FLOOR_KW:
        return current_idx, False
    if power_kw >= DOWN1_FLOOR_KW:
        return max(current_idx - 1, 0), False
    if power_kw >= DOWN2_FLOOR_KW:
        return max(current_idx - 2, 0), False
    return 0, True


def main():
    client = solis_api.SolisClient(
        config.SOLIS_KEY_ID, config.SOLIS_KEY_SECRET, config.SOLIS_PLANT_ID
    )

    current_level = probe_actual_level()
    consecutive_errors = 0
    unreachable_rigs = set()
    last_step_at = 0.0
    was_overridden = False

    if current_level != "idle":
        notifier.send(
            f"Solar miner controller started -- rigs already running at "
            f"{current_level.upper()} on startup. Managing from there.",
            level="warn",
        )
    else:
        notifier.send("Solar miner controller started.", level="info", telegram=True)

    while True:
        power_kw = None
        connection_ok = False
        now = time.time()

        override = state_store.get_override()

        if override:
            if not was_overridden:
                notifier.send(f"Manual override active: rigs held at {override['mode'].upper()}.", level="info", telegram=False)
                was_overridden = True
            target_level = "super" if override["mode"] == "max" else "idle"
            if current_level != target_level:
                set_all_rigs_level(target_level, unreachable_rigs)
                current_level = target_level
                last_step_at = now
            try:
                power_kw = client.get_current_power_kw()
                connection_ok = True
            except Exception as e:
                log.warning(f"poll failed during override: {e}")

        else:
            if was_overridden:
                notifier.send("Manual override expired/cleared -- resuming automatic control.", level="info", telegram=False)
                was_overridden = False

            try:
                power_kw = client.get_current_power_kw()
                connection_ok = True
                consecutive_errors = 0
                log.info(f"current export power: {power_kw:.1f} kW (rigs at {current_level})")

                current_idx = avalon_ctl.LEVEL_INDEX[current_level]
                target_idx, immediate = decide_target_level(current_idx, power_kw)
                settled = (now - last_step_at) >= STEP_SETTLE_SEC

                if target_idx != current_idx and (immediate or settled):
                    was_idle = current_level == "idle"
                    if target_idx > current_idx:
                        new_idx = current_idx + 1   # up: one level at a time
                    else:
                        new_idx = target_idx         # down: may skip levels
                    new_level = LEVELS[new_idx]

                    set_all_rigs_level(new_level, unreachable_rigs)
                    direction = "up" if new_idx > current_idx else "down"
                    current_level = new_level
                    last_step_at = now

                    msg = f"Output at {power_kw:.1f}kW -> stepping {direction} to {new_level.upper()}."
                    first_of_day = was_idle and daily_stats.should_notify_first_activation(config.RIG_IPS)
                    notifier.send(msg, level="info", telegram=first_of_day)
                    if first_of_day:
                        daily_stats.mark_activation_notified(config.RIG_IPS)

            except Exception as e:
                consecutive_errors += 1
                log.error(f"poll failed ({consecutive_errors} in a row): {e}")
                if consecutive_errors == 3:
                    notifier.send(f"Can't reach SolisCloud (3+ failures in a row): {e}", level="warn")
                if consecutive_errors >= 5 and current_level != "idle":
                    log.warning("too many consecutive failures -- forcing rigs to IDLE as a safe default")
                    notifier.send("Lost contact with SolisCloud for a while -- forcing rigs to IDLE as a safety default.", level="error")
                    set_all_rigs_level("idle", unreachable_rigs)
                    current_level = "idle"
                    last_step_at = now

        daily_stats.record_power_sample(power_kw, config.RIG_IPS)

        rig_statuses = collect_rig_statuses(current_level)
        state_store.write_state(power_kw, current_level, rig_statuses, connection_ok, override=override)

        summary_hour = getattr(config, "DAILY_SUMMARY_HOUR", 18)
        if daily_stats.should_send_daily_summary(config.RIG_IPS, summary_hour):
            notifier.send(build_daily_summary(power_kw, current_level), level="info", telegram=True)
            daily_stats.mark_summary_sent(config.RIG_IPS)

        notifier.flush()
        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
