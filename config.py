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
    "srv-c1234567890abcdef",
    "srv-d1234567890fedcba"
]

# הגדרות התראות
INACTIVE_DAYS_ALERT = 3  # התראה אחרי כמה ימים של חוסר פעילות
AUTO_SUSPEND_DAYS = 7    # השעיה אוטומטית אחרי כמה ימים
CHECK_INTERVAL_HOURS = 24  # בדיקה כל כמה שעות

# הגדרות כלליות
TIMEZONE = "Asia/Jerusalem"
