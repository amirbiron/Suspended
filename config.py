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

# הגדרות כלליות
TIMEZONE = "Asia/Jerusalem"

# --- ניטור סטטוס שירותים (UP/DOWN) ---
ENABLE_STATE_MONITOR = os.getenv("ENABLE_STATE_MONITOR", "false").lower() in ("1", "true", "yes")
STATUS_POLL_INTERVAL_MINUTES = int(os.getenv("STATUS_POLL_INTERVAL_MINUTES", "5"))

# חלונות השתקה כדי לא לשלוח התראות שנגרמות מפעולה שלנו / דיפלוי
ALERT_SUPPRESSION_MINUTES_AFTER_OUR_ACTION = int(os.getenv("ALERT_SUPPRESSION_MINUTES_AFTER_OUR_ACTION", "10"))
DEPLOY_SUPPRESSION_MINUTES = int(os.getenv("DEPLOY_SUPPRESSION_MINUTES", "10"))
STATE_ALERT_COOLDOWN_MINUTES = int(os.getenv("STATE_ALERT_COOLDOWN_MINUTES", "5"))

# מצבי ביניים של Render (במהלך בניה/דיפלוי) שבהם לא נשלחות התראות
RENDER_TRANSIENT_STATUSES = [
    "deploy_in_progress",
    "build_in_progress",
    "update_in_progress",
    "starting",
    "stopping",
    "restarting",
]

# מצבים שנחשבים DOWN
RENDER_DOWN_STATUSES = [
    "suspended",
    "failed",
    "crashed",
]

# --- Heuristics כדי שלא תצטרך פקודות ---
REQUIRE_CONSECUTIVE_POLLS_FOR_ALERT = int(os.getenv("REQUIRE_CONSECUTIVE_POLLS_FOR_ALERT", "2"))
MIN_DOWN_DURATION_MINUTES = int(os.getenv("MIN_DOWN_DURATION_MINUTES", "3"))
ONLY_ALERT_UP_IF_PREVIOUS_DOWN_ALERT = os.getenv("ONLY_ALERT_UP_IF_PREVIOUS_DOWN_ALERT", "true").lower() in ("1", "true", "yes")
PREVIOUS_DOWN_LOOKBACK_HOURS = int(os.getenv("PREVIOUS_DOWN_LOOKBACK_HOURS", "24"))

AUTO_DEPLOY_WINDOW_AUTODETECT = os.getenv("AUTO_DEPLOY_WINDOW_AUTODETECT", "true").lower() in ("1", "true", "yes")
AUTO_DEPLOY_WINDOW_THRESHOLD_SERVICES = int(os.getenv("AUTO_DEPLOY_WINDOW_THRESHOLD_SERVICES", "2"))
AUTO_DEPLOY_WINDOW_MINUTES = int(os.getenv("AUTO_DEPLOY_WINDOW_MINUTES", "15"))

# --- Webhooks ל-CI לציון חלון דיפלוי ---
ENABLE_CI_HTTP_HOOKS = os.getenv("ENABLE_CI_HTTP_HOOKS", "false").lower() in ("1", "true", "yes")
CI_HTTP_PORT = int(os.getenv("CI_HTTP_PORT", "8088"))
CI_SHARED_SECRET = os.getenv("CI_SHARED_SECRET", "")
