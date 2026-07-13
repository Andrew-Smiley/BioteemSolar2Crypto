#!/usr/bin/env python3
"""
Main control loop: poll SolisCloud for current output power, bring Avalon
Q rigs online/offline one at a time as we approach the 100kW export cap.
Publishes shared state for the dashboard.

Telegram notification policy (see notifier.py for the mechanism):
  - Warnings/errors (rig unreachable, lost SolisCloud contact) -> immediate
  - First rig activation of the day -> immediate
  - Every other routine step (later activations, all deactivations) ->
    dashboard-only, no phone ping
  - A single daily summary at config.DAILY_SUMMARY_HOUR (default 6pm) with
    event counts, rig runtime, estimated energy burned, and today's power
    min/max/avg

Rigs are staged on/off individually (not all switched at once) with a
cooldown between steps -- see config.STEP_SETTLE_SEC. This matters because
each rig's power draw is larger than the hysteresis band in some setups:
switching both rigs on simultaneously can push the reading well below
RAMP_DOWN_KW instantly, triggering an immediate reversal. Bringing rigs on
one at a time and waiting to see where the reading settles avoids that.

Also checks for a manual override (set via Telegram -- see
telegram_listener.py) each cycle: if one's active, automatic staged
control is paused and ALL rigs are held at whatever the override says,
until it expires or is cleared with "auto".

Run this under systemd (see solar-miner.service) so it restarts itself on
crash or reboot.
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("controller")


def set_single_rig(ip: str, state: str, unreachable_rigs: set) -> bool:
    """Sets one rig to 'idle' or 'max'. Returns True on success. Tracks
    unreachable_rigs so we only alert once per outage, not on every poll.
    Also records the transition for the daily runtime/energy estimate."""
    try:
        if state == "idle":
            result = avalon_ctl.go_idle(ip)
        else:
            result = avalon_ctl.go_max(ip)
        log.info(f"rig {ip} -> {state}: {result[:120]}")
        daily_stats.record_rig_transition(ip, state, config.RIG_IPS)
        if ip in unreachable_rigs:
            unreachable_rigs.discard(ip)
            notifier.send(f"Rig {ip} back online, now set to {state}.", level="info")
        return True
    except (OSError, TimeoutError) as e:
        log.warning(f"rig {ip} unreachable while setting {state}: {e}")
        if ip not in unreachable_rigs:
            unreachable_rigs.add(ip)
            notifier.send(f"Rig {ip} unreachable while trying to set {state}: {e}", level="warn")
        return False


def set_rigs(state: str, unreachable_rigs: set):
    """Sets ALL configured rigs to the same state at once -- used for
    manual override and the safety fallback (lost SolisCloud contact),
    NOT for normal automatic ramping, which brings rigs on/off one at a
    time (see main())."""
    for ip in config.RIG_IPS:
        set_single_rig(ip, state, unreachable_rigs)


def collect_rig_statuses(active_rig_ips: list) -> dict:
    """Pulls live telemetry from each rig for the dashboard, plus what we
    last commanded it to (separate from what it's actually doing, in case
    a command failed). Never raises."""
    statuses = {}
    for ip in config.RIG_IPS:
        status = avalon_ctl.get_status(ip)
        status["commanded_state"] = "max" if ip in active_rig_ips else "idle"
        statuses[ip] = status
    return statuses


def build_daily_summary(power_kw, active_rig_ips: list, total_rigs: int) -> str:
    runtime = daily_stats.get_runtime_seconds(config.RIG_IPS)
    power_stats = daily_stats.get_power_stats(config.RIG_IPS)
    rig_power_est = getattr(config, "RIG_POWER_KW_ESTIMATE", 3.6)

    today_str = date.today().isoformat()
    today_epoch_start = time.mktime(time.strptime(today_str, "%Y-%m-%d"))
    events = [e for e in state_store.get_events() if e["time"] >= today_epoch_start]
    info_count = sum(1 for e in events if e["level"] == "info")
    warn_count = sum(1 for e in events if e["level"] == "warn")
    error_count = sum(1 for e in events if e["level"] == "error")

    lines = [f"Daily summary -- {today_str}"]
    if power_kw is not None:
        lines.append(f"Current: {power_kw:.1f}kW, {len(active_rig_ips)}/{total_rigs} rigs active")
    else:
        lines.append("Current: no data")
    lines.append(f"Events today: {len(events)} ({info_count} info, {warn_count} warnings, {error_count} errors)")

    if power_stats["samples"]:
        lines.append(
            f"Output today: avg {power_stats['avg']:.1f}kW, "
            f"peak {power_stats['max']:.1f}kW, low {power_stats['min']:.1f}kW"
        )

    total_hours = 0.0
    total_kwh = 0.0
    for ip in config.RIG_IPS:
        hours = runtime.get(ip, 0.0) / 3600
        kwh = hours * rig_power_est
        total_hours += hours
        total_kwh += kwh
        lines.append(f"  {ip}: {hours:.1f}h active (~{kwh:.1f} kWh est.)")
    lines.append(f"Total rig runtime: {total_hours:.1f}h, ~{total_kwh:.1f} kWh burned (est. at {rig_power_est}kW/rig)")

    notable = [e["message"] for e in events if e["level"] in ("warn", "error")]
    if notable:
        shown = notable[:10]
        lines.append("Notable issues:")
        lines.extend(f"  - {m}" for m in shown)
        if len(notable) > len(shown):
            lines.append(f"  (+{len(notable) - len(shown)} more, see dashboard)")

    return "\n".join(lines)


def main():
    client = solis_api.SolisClient(
        config.SOLIS_KEY_ID, config.SOLIS_KEY_SECRET, config.SOLIS_PLANT_ID
    )

    total_rigs = len(config.RIG_IPS)
    active_rig_ips = []  # rigs currently commanded to MAX, in activation order
    consecutive_errors = 0
    unreachable_rigs = set()
    last_step_at = 0.0
    was_overridden = False

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
            if override["mode"] == "max" and len(active_rig_ips) != total_rigs:
                set_rigs("max", unreachable_rigs)
                active_rig_ips = list(config.RIG_IPS)
                last_step_at = now
            elif override["mode"] == "idle" and len(active_rig_ips) != 0:
                set_rigs("idle", unreachable_rigs)
                active_rig_ips = []
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
                log.info(f"current export power: {power_kw:.1f} kW ({len(active_rig_ips)}/{total_rigs} rigs active)")

                settled = (now - last_step_at) >= config.STEP_SETTLE_SEC

                if power_kw >= config.RAMP_UP_KW and len(active_rig_ips) < total_rigs and settled:
                    was_empty = len(active_rig_ips) == 0
                    next_ip = next(ip for ip in config.RIG_IPS if ip not in active_rig_ips)
                    if set_single_rig(next_ip, "max", unreachable_rigs):
                        active_rig_ips.append(next_ip)
                    last_step_at = now
                    msg = (
                        f"Output at {power_kw:.1f}kW -> bringing {next_ip} to MAX "
                        f"({len(active_rig_ips)}/{total_rigs} active). "
                        f"Settling {config.STEP_SETTLE_SEC // 60}m before next step."
                    )
                    first_of_day = was_empty and daily_stats.should_notify_first_activation(config.RIG_IPS)
                    notifier.send(msg, level="info", telegram=first_of_day)
                    if first_of_day:
                        daily_stats.mark_activation_notified(config.RIG_IPS)

                elif power_kw <= config.RAMP_DOWN_KW and len(active_rig_ips) > 0 and settled:
                    ip_to_idle = active_rig_ips.pop()
                    if not set_single_rig(ip_to_idle, "idle", unreachable_rigs):
                        active_rig_ips.append(ip_to_idle)  # put it back, command failed
                    last_step_at = now
                    notifier.send(
                        f"Output at {power_kw:.1f}kW -> idling {ip_to_idle} "
                        f"({len(active_rig_ips)}/{total_rigs} active). "
                        f"Settling {config.STEP_SETTLE_SEC // 60}m before next step.",
                        level="info", telegram=False,
                    )
                # else: in the hysteresis band, already correct, or still
                # settling from the last step -- do nothing this cycle.

            except Exception as e:
                consecutive_errors += 1
                log.error(f"poll failed ({consecutive_errors} in a row): {e}")
                if consecutive_errors == 3:
                    notifier.send(f"Can't reach SolisCloud (3+ failures in a row): {e}", level="warn")
                if consecutive_errors >= 5 and len(active_rig_ips) > 0:
                    log.warning("too many consecutive failures -- forcing rigs to IDLE as a safe default")
                    notifier.send("Lost contact with SolisCloud for a while -- forcing rigs to IDLE as a safety default.", level="error")
                    set_rigs("idle", unreachable_rigs)
                    active_rig_ips = []
                    last_step_at = now

        daily_stats.record_power_sample(power_kw, config.RIG_IPS)

        control_state = "idle" if not active_rig_ips else ("max" if len(active_rig_ips) == total_rigs else "partial")
        rig_statuses = collect_rig_statuses(active_rig_ips)
        state_store.write_state(power_kw, control_state, rig_statuses, connection_ok, override=override)

        summary_hour = getattr(config, "DAILY_SUMMARY_HOUR", 18)
        if daily_stats.should_send_daily_summary(config.RIG_IPS, summary_hour):
            notifier.send(build_daily_summary(power_kw, active_rig_ips, total_rigs), level="info", telegram=True)
            daily_stats.mark_summary_sent(config.RIG_IPS)

        notifier.flush()
        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
