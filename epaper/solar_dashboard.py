"""Read-only four-button Waveshare 2.7-inch V2 dashboard for Bioteem Solar.

Install alongside waveshare_epd in /home/admin/waveshare-2in7-v2.
Run with GPIOZERO_PIN_FACTORY=lgpio /home/admin/display_env/bin/python.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path('/home/admin/solar_miner_controller')
CACHE = Path(__file__).with_name('solar_display_cache.json')
ZONE = ZoneInfo('America/Halifax')
# Observed Solis stationDay timestamps encode local wall clock in UTC+8.
GRAPH_STAMP_ZONE = ZoneInfo('Asia/Shanghai')
BUTTONS = {5: 'now', 6: 'solar', 13: 'miners', 19: 'events'}
REFRESH_SECONDS = 300
RETURN_SECONDS = 120
sys.path.insert(0, str(PROJECT))


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def solar_data():
    today = datetime.now(ZONE).date().isoformat()
    cache = read_json(CACHE, {})
    age = time.time() - cache.get('fetched_at', 0)
    if cache.get('date') == today and 0 <= age < REFRESH_SECONDS:
        return cache, 'live'
    try:
        import config
        from solis_api import SolisClient

        client = SolisClient(config.SOLIS_KEY_ID, config.SOLIS_KEY_SECRET,
                             config.SOLIS_PLANT_ID)
        record = client.get_station_record()
        raw = client._post('/v1/api/stationDay', {
            'id': record['id'],
            'money': record.get('money') or 'CAD',
            'time': today,
            'timeZone': record['timeZone'],
        })
        points = []
        for item in raw:
            stamp = datetime.fromtimestamp(float(item['time']) / 1000,
                                           GRAPH_STAMP_ZONE)
            if stamp.date().isoformat() == today:
                points.append([stamp.hour + stamp.minute / 60,
                               max(0.0, float(item['power']) / 1000)])
        cache = {
            'date': today,
            'fetched_at': time.time(),
            'power_kw': float(record['power']),
            'day_kwh': float(record['dayEnergy']),
            'points': sorted(points),
        }
        fd, tmp = tempfile.mkstemp(dir=CACHE.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(cache, output)
            os.replace(tmp, CACHE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return cache, 'live'
    except Exception as exc:
        print(f'Solis request: {type(exc).__name__}: {exc}', flush=True)
        if cache.get('date') == today and 0 <= age < 1800:
            return cache, 'cached'
        return {}, 'unavailable'


def font(size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', size)
    except OSError:
        return ImageFont.load_default()


def text(draw, xy, value, size=13):
    draw.text(xy, str(value), font=font(size), fill=0)


def header(draw, name, right=''):
    text(draw, (7, 2), 'BIOTEEM  |  ' + name.upper(), 15)
    if right:
        text(draw, (8, 155), right, 10)


def number(value, unit, digits=1):
    return f'{value:.{digits}f} {unit}' if isinstance(value, (int, float)) else '--'


def state_data():
    data = read_json(PROJECT / 'state.json', {})
    age = time.time() - (data.get('updated_at') or 0)
    return data, 0 <= age < 300


def rigs_in_order(state):
    rigs = state.get('rigs') or {}
    # Use configured miner order; never show an IP address on the screen.
    try:
        import config
        ordered = [rigs.get(ip, {}) for ip in config.RIG_IPS]
    except (ImportError, AttributeError):
        ordered = list(rigs.values())
    return (ordered + [{}, {}])[:2]


def miner_status(rig, fresh):
    if not fresh:
        return 'STATUS STALE'
    if not rig.get('reachable'):
        return 'OFFLINE'
    rate = rig.get('hashrate_ths')
    if not isinstance(rate, (int, float)):
        return 'ONLINE  rate unknown'
    return f'{"HASHING" if rate > 0 else "IDLE"}  {rate:.1f} TH/s'


def draw_now(draw, state, fresh, solar, health):
    updated = (datetime.fromtimestamp(solar['fetched_at'], ZONE).strftime('%H:%M')
               if solar.get('fetched_at') else '--:--')
    header(draw, updated)
    power = solar.get('power_kw') if health != 'unavailable' else None
    day = solar.get('day_kwh') if health != 'unavailable' else None
    if power is None or day is None:
        text(draw, (7, 24), 'Solis data unavailable', 13)
    else:
        text(draw, (7, 24), f'{power:.1f} kW   Today {day:.1f} kWh', 13)

    left, right, top, bottom = 13, 255, 50, 110
    now = datetime.now(ZONE)
    start = 6.0
    end = max(12.0, now.hour + now.minute / 60 + 0.5)
    draw.line((left, top, left, bottom, right, bottom), fill=0)
    points = solar.get('points') or []
    if points:
        scale = max(110, max(power for _, power in points) * 1.1)
        coordinates = [(int(left + (hour - start) / (end - start) * (right - left)),
                        int(bottom - power / scale * (bottom - top)))
                       for hour, power in points if start <= hour <= end]
        if len(coordinates) > 1:
            draw.line(coordinates, fill=0, width=2)
        for x, y in coordinates[::max(1, len(coordinates) // 35)]:
            draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=0)
    else:
        text(draw, (44, 75), 'No graph data', 14)
    for hour in (6, 9, 12, 15, 18, 21):
        if hour >= end - 1:
            break
        x = int(left + (hour - start) / (end - start) * (right - left))
        text(draw, (x - 6, 113), hour, 10)
    text(draw, (right - 22, 113), 'Now', 10)

    draw.line((7, 129, 256, 129), fill=0)
    for index, rig in enumerate(rigs_in_order(state), 1):
        text(draw, (8, 133 + 18 * (index - 1)),
             f'M{index}  {miner_status(rig, fresh)}', 13)
    if health == 'cached' or not fresh:
        draw.rectangle((176, 1, 263, 20), fill=255)
        text(draw, (177, 2), 'DATA OLD', 10)


def draw_solar(draw, solar, health):
    header(draw, 'SOLAR')
    if health == 'unavailable':
        text(draw, (9, 37), 'SolisCloud unavailable', 15)
        text(draw, (9, 68), 'Last reading unavailable', 12)
        return
    text(draw, (9, 30), 'Current output', 12)
    text(draw, (9, 46), number(solar.get('power_kw'), 'kW'), 23)
    draw.line((8, 79, 256, 79), fill=0)
    text(draw, (9, 86), 'Generated today', 12)
    text(draw, (9, 103), number(solar.get('day_kwh'), 'kWh'), 21)
    points = solar.get('points') or []
    if points:
        text(draw, (9, 135), f'Highest sample: {max(p for _, p in points):.1f} kW', 11)
    stamp = datetime.fromtimestamp(solar['fetched_at'], ZONE).strftime('%H:%M')
    text(draw, (9, 155), f'{"Cached" if health == "cached" else "Updated"} {stamp}', 10)


def draw_miners(draw, state, fresh):
    header(draw, 'MINERS')
    for index, rig in enumerate(rigs_in_order(state), 1):
        top = 27 + 59 * (index - 1)
        text(draw, (8, top), f'MINER {index}', 14)
        text(draw, (8, top + 19), miner_status(rig, fresh), 14)
        if fresh and rig.get('reachable'):
            temp = rig.get('temp_c')
            text(draw, (8, top + 39),
                 f'Temperature {number(temp, "C", 0)}', 11)
        if index == 1:
            draw.line((8, 82, 256, 82), fill=0)
    draw.line((8, 145, 256, 145), fill=0)
    level = state.get('control_state', 'unknown') if fresh else 'state stale'
    text(draw, (8, 151), f'Controller: {level}', 11)


def draw_events(draw, state, fresh):
    header(draw, 'EVENTS')
    if not fresh:
        text(draw, (8, 30), 'Controller state is stale', 12)
    override = state.get('override') if fresh else None
    text(draw, (8, 44),
         f'Manual: {override.get("mode", "active")}' if isinstance(override, dict)
         else 'Automatic control', 11)
    draw.line((8, 64, 256, 64), fill=0)
    events = state.get('events') or []
    if not events:
        text(draw, (8, 76), 'No recent events', 12)
    for index, event in enumerate(events[:3]):
        y = 70 + 33 * index
        try:
            stamp = datetime.fromtimestamp(event['time'], ZONE).strftime('%H:%M')
        except (KeyError, TypeError, ValueError, OverflowError):
            stamp = '--:--'
        message = str(event.get('message', '')).replace('\n', ' ')
        text(draw, (8, y), f'{stamp} {event.get("level", "info").upper()}', 10)
        text(draw, (8, y + 13), message[:38], 10)


def render(page):
    from PIL import Image, ImageDraw

    state, fresh = state_data()
    solar, health = solar_data() if page in ('now', 'solar') else ({}, 'unavailable')
    image = Image.new('1', (264, 176), 255)
    draw = ImageDraw.Draw(image)
    if page == 'now':
        draw_now(draw, state, fresh, solar, health)
    elif page == 'solar':
        draw_solar(draw, solar, health)
    elif page == 'miners':
        draw_miners(draw, state, fresh)
    elif page == 'events':
        draw_events(draw, state, fresh)
    else:
        raise ValueError(f'Unknown page: {page}')

    from waveshare_epd import epd2in7_V2
    epd = epd2in7_V2.EPD()
    epd.init()
    try:
        epd.display(epd.getbuffer(image))
    finally:
        epd.sleep()
    print(f'Displayed {page} ({health}); state fresh: {fresh}', flush=True)


def watch():
    from gpiozero import Button

    buttons = {pin: Button(pin, pull_up=True, bounce_time=0.05)
               for pin in BUTTONS}
    previous = {pin: button.is_pressed for pin, button in buttons.items()}
    page = 'now'
    last_button = time.monotonic()
    last_draw = 0
    needs_draw = True
    try:
        while True:
            now = time.monotonic()
            for pin, button in buttons.items():
                pressed = button.is_pressed
                if pressed and not previous[pin]:
                    requested = BUTTONS[pin]
                    last_button = now
                    if requested != page:
                        page = requested
                        needs_draw = True
                    print(f'KEY {pin}: {page}', flush=True)
                previous[pin] = pressed
            if page != 'now' and now - last_button >= RETURN_SECONDS:
                page = 'now'
                needs_draw = True
            if needs_draw or now - last_draw >= REFRESH_SECONDS:
                print(f'Refreshing {page}', flush=True)
                try:
                    subprocess.run([sys.executable, __file__, '--render', page],
                                   check=True, timeout=75)
                except (subprocess.SubprocessError, OSError) as exc:
                    print(f'Refresh failed: {exc}', flush=True)
                last_draw = time.monotonic()
                needs_draw = False
            time.sleep(0.04)
    finally:
        for button in buttons.values():
            button.close()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--render':
        render(sys.argv[2])
    elif len(sys.argv) == 1:
        watch()
    else:
        raise SystemExit('Usage: solar_dashboard.py [--render now|solar|miners|events]')
