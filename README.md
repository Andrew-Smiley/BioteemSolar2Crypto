# Solar-aware Avalon Q controller

Polls your Solis inverter's real output power and switches your Avalon Q
rigs between idle and max so you don't waste solar generation above your
100kW export cap.

## Setup

1. **Get SolisCloud API access** (this replaces the dashboard link entirely
   -- don't use that link/token for automation, it's a session token for
   the web UI, not an API credential, and it's not something you should
   leave lying around since it grants dashboard access to whoever has it):
   - Log into soliscloud.com on a computer
   - Service (bottom of sidebar) -> API Management -> Activate now
   - If it's not already enabled on your account, email
     usservice@solisinverters.com asking them to open API access, then
     retry -- can take ~24h
   - Once activated, generate a Key ID + Key Secret (needs an email-based
     verification code)
   - Put these plus your plant ID into `config.py`

2. **Find your Avalon Q rigs' IPs** on your LAN (check your router's DHCP
   client list, or the Avalon Family app -> device details). Put them in
   `config.py`.

3. **Set each rig's workmode to Super/max once** via the Avalon Family app.
   The controller only toggles standby (softoff/softon) after that --
   waking a rig resumes whatever workmode it was last set to.

4. **Set up Telegram notifications** (optional but recommended):
   - In Telegram, message `@BotFather` -> `/newbot` -> follow the prompts.
     You'll get a token like `123456789:AAExample...`
   - Send your new bot any message (it can't message you until you've
     messaged it first)
   - Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a
     browser and find `"chat":{"id": ...}` in the response -- that number
     is your chat ID
   - Put both into `config.py` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
   - Set `TELEGRAM_ENABLED = False` if you'd rather skip this for now

   You'll get a message on: startup, every idle<->max switch, a rig going
   unreachable/recovering, SolisCloud connection trouble, and a heartbeat
   every 6 hours (tunable) with a small summary -- so if the messages stop
   entirely, that itself tells you the Pi or its network is down, rather
   than leaving you wondering if it's fine or dead.

4. **Test manually first:**
   ```
   pip install requests
   python3 -c "from solis_api import SolisClient; import config; \
       print(SolisClient(config.SOLIS_KEY_ID, config.SOLIS_KEY_SECRET, config.SOLIS_PLANT_ID).get_station_detail())"
   ```
   Check the `pac` field against what the dashboard shows, so you know
   you're reading the right units (kW vs W) before trusting the automation.

   Then test rig control directly:
   ```
   python3 -c "import avalon_ctl; print(avalon_ctl.get_summary('192.168.1.50'))"
   ```

5. **Run it:**
   ```
   sudo cp solar-miner.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now solar-miner
   journalctl -u solar-miner -f
   ```

## Dashboard

A dark-themed web dashboard, served straight from the Pi: a live gauge
showing current output against the 100kW export cap (color shifts blue →
cyan → amber → red-and-pulsing as you approach it), a card per rig with
hashrate/temp/online status, and the same event log the Telegram bot
sends, scrollable in the page.

It runs as a separate small process (`dashboard.py`) from the controller,
talking to it only through `state.json` on disk -- the controller writes
its current state every poll, the dashboard just reads it. If the
dashboard crashes, the controller (and your actual power/mining control)
keeps running unaffected, and vice versa.

**Login** uses HTTP Basic Auth checked against your Pi's real Linux user
accounts via PAM -- the same username/password you SSH in with, nothing
new to set up. Optionally restrict which accounts are allowed in via
`DASHBOARD_ALLOWED_USERS` in `config.py` (e.g. `["pi"]`), otherwise any
valid account on the box can log in.

Setup:
```
pip install -r requirements.txt
sudo cp solar-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now solar-dashboard
```
Then visit `http://<pi-ip>:8080` from any device on your LAN and log in
with your Pi account.

**One thing worth knowing:** Basic Auth sends your password base64-encoded,
not encrypted -- fine on your own home LAN, but don't port-forward this to
the open internet as-is. If you ever want to reach it remotely, put it
behind a VPN (e.g. Tailscale/WireGuard to your home network) rather than
opening the port, or add HTTPS via a reverse proxy in front of it.



- `RAMP_UP_KW` / `RAMP_DOWN_KW` in `config.py` control the hysteresis band
  (default 90kW up, 80kW down). Widen the gap if you see the rigs
  flapping; narrow it if you're leaving too much power uncaptured near the
  cap.
- If your two rigs alone can't soak up the ~60kW+ of headroom between
  80kW and 160kW array capacity, this only gets you part of the way --
  worth knowing going in.

## Notification options, if Telegram isn't your thing

- **Telegram** (what's wired in above): free, easy bot API, works well
  from a headless Pi, has a proper message history you can scroll back
  through. Downside: you're on Telegram's infrastructure and need an
  account.
- **ntfy.sh**: arguably even simpler than Telegram -- no bot registration
  at all, just `curl -d "message" ntfy.sh/your-topic-name` (self-hostable
  too if you want it fully private). Install the ntfy app and subscribe to
  your topic. I'd suggest this if you want the absolute least setup.
- **Signal**: possible via `signal-cli`, but it needs a JVM and linking as
  a secondary device to your account -- more moving parts on a Pi for not
  much upside over Telegram/ntfy unless you specifically need
  Signal's privacy properties.
- **Pushover**: paid one-time app fee, very reliable delivery, nice for
  phone push notifications specifically with priority levels (e.g. make
  the "lost contact with inverter" alert bypass silent mode).
- **Home Assistant** (if you go that route per below): built-in
  notification integrations to basically everything, plus a dashboard
  so you're not relying on a message history to know current state.

If you want, I can swap the notifier over to ntfy.sh instead -- it's a
one-function change and removes the bot-setup step entirely.



This script works, but polling a cloud API has two structural weaknesses
worth knowing about:

1. **Latency/reliability**: cloud round-trips + Solis's own reporting
   interval means you're reacting to data that's already a bit stale, and
   if SolisCloud has an outage your loop goes blind (handled here by
   defaulting to idle after repeated failures, but it's a real gap).
2. **Rate limits**: hammering the cloud API for faster reaction time will
   get you throttled.

If you want tighter, more reliable control, the better path is **local
Modbus TCP/RTU** straight to the inverter or its datalogger, bypassing the
cloud entirely for the control loop (you can still use SolisCloud for
historical dashboards). Many Solis string inverters/loggers expose Modbus
registers for real-time AC output power on the local network, readable
with `pymodbus` in a few lines, at whatever poll rate you want, with no
API quota. Whether yours supports this depends on your specific logger
model (S2, S3, etc.) -- check your inverter's Modbus register map (Solis
publishes these per model) or a wired RS485 connection if the built-in
datalogger doesn't expose it over LAN.

The other piece worth considering: **Home Assistant**. Both Solis and
Avalon Q already have community HA integrations (a Solis SolisCloud
integration, and `c7ph3r10/ha_avalonq` for the miners). Wiring this
through HA gets you free logging, alerting, history graphs, and a UI to
override manually -- likely less code to maintain than a bespoke Pi
script, at the cost of running HA. Worth it if you already have HA or
plan to add more automation later; probably overkill if this is the only
thing you're automating.
