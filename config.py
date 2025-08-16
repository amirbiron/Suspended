import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "your_telegram_bot_token_here")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "your_admin_chat_id_here")

# Render API
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "your_render_api_key_here")
RENDER_API_URL = "https://api.render.com/v1"

# MongoDB
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DATABASE_NAME = "render_bot_monitor"

# שירותים לניטור - רשימת service IDs של Render
SERVICES_TO_MONITOR = [
    # דוגמאות - החלף במזהי השירותים שלך
    "srv-d26cf32dbo4c73f27de0",
    "srv-d26079be5dus73ctnegg",
    "srv-d202d2i4d50c73b7u3pg",
    "srv-d1vm4m7diees73bq7eh0",
    "srv-d220g0je5dus7395phvg",
    "srv-d1lk1mfdiees73fos2h0",
    "srv-d1t3lijuibrs738s0af0",
]

# הגדרות התראות
INACTIVE_DAYS_ALERT = 3  # התראה אחרי כמה ימים של חוסר פעילות
AUTO_SUSPEND_DAYS = 7    # השעיה אוטומטית אחרי כמה ימים
CHECK_INTERVAL_HOURS = 24  # בדיקה כל כמה שעות

# הגדרות ניטור סטטוס
STATUS_CHECK_INTERVAL_SECONDS = int(os.getenv("STATUS_CHECK_INTERVAL_SECONDS", "300"))  # 5 minutes default
STATUS_MONITORING_ENABLED = os.getenv("STATUS_MONITORING_ENABLED", "true").lower() == "true"

# Feature toggles
ENABLE_STATUS_HISTORY = os.getenv("ENABLE_STATUS_HISTORY", "true").lower() == "true"
ENABLE_DEPLOYMENT_NOTIFICATIONS = os.getenv("ENABLE_DEPLOYMENT_NOTIFICATIONS", "false").lower() == "true"

# הגדרות כלליות
TIMEZONE = "Asia/Jerusalem"
