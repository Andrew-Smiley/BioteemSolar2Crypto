"""
Copy this to config.py and fill in your real values. config.py itself is
gitignored -- never commit it, it holds live credentials.
"""

SOLIS_KEY_ID = "your-key-id"
SOLIS_KEY_SECRET = "your-key-secret"
SOLIS_PLANT_ID = "your-plant-id"

RIG_IPS = [
    "192.168.1.50",
    "192.168.1.51",
]

RAMP_UP_KW = 99.0
RAMP_DOWN_KW = 97.0
STEP_SETTLE_SEC = 120

POLL_INTERVAL_SEC = 30
LOG_FILE = "/home/admin/solar_miner_controller/controller.log"

TELEGRAM_ENABLED = True
TELEGRAM_BOT_TOKEN = "123456789:AAExampleTokenReplaceMe"
TELEGRAM_CHAT_ID = "your-personal-chat-id"
TELEGRAM_CHANNEL_CHAT_ID = None

OVERRIDE_TTL_SEC = 2 * 60 * 60
DAILY_SUMMARY_HOUR = 18
RIG_POWER_KW_ESTIMATE = 3.6

DASHBOARD_PORT = 8080
DASHBOARD_ALLOWED_USERS = None

F2POOL_ENABLED = False
F2POOL_CURRENCY = "bitcoin"
F2POOL_USERNAME = "your-f2pool-username"
F2POOL_API_TOKEN = "your-f2pool-api-token"
