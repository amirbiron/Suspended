```yaml
name: "Suspended - בוט ניטור והשעיה חכמה ל-Render"
repo: "https://github.com/amirbiron/Suspended"
status: "פעיל (בייצור)"

one_liner: "בוט טלגרם שמנטר שירותי Render, משעה אוטומטית שירותים לא פעילים לחיסכון בעלויות, ומספק ניטור לוגים בזמן אמת."

stack:
  - Python 3.8+
  - python-telegram-bot 20.7
  - MongoDB (pymongo)
  - Render API
  - schedule
  - python-dotenv
  - pytz

key_features:
  - "ניטור אוטומטי יומי של פעילות בוטים על Render"
  - "השעיה חכמה של שירותים לא פעילים לחיסכון בעלויות"
  - "החזרה לפעילות בפקודה אחת"
  - "התראות דיפלוי בזמן אמת (מעבר בין deploying ל-online/offline)"
  - "ניטור לוגים אוטומטי - זיהוי שגיאות רגילות וקריטיות"
  - "צפייה בלוגים ישירות מהבוט עם סינון (errors/stdout/stderr)"
  - "ניטור סטטוס שירותים עם מרווחי בדיקה מותאמים"
  - "שמירת היסטוריית פעילות במסד נתונים MongoDB"

architecture:
  summary: |
    ארכיטקטורת מודולרית מבוססת Python. הבוט רץ כשירות מתמשך על Render,
    מתקשר עם Render API לניטור שירותים, שומר נתונים ב-MongoDB,
    ושולח התראות דרך Telegram Bot API. כולל מערכת ניטור לוגים עם זיהוי תבניות שגיאה.
  entry_points:
    - "main.py - נקודת כניסה ראשית, הפעלת הבוט"
    - "config.py - הגדרות, משתני סביבה, רשימת שירותים"
    - "render_api.py - תקשורת עם Render API"
    - "database.py - שכבת מסד נתונים MongoDB"
    - "notifications.py - שליחת התראות טלגרם"
    - "status_monitor.py - ניטור סטטוס שירותים ודיפלויים"
    - "log_monitor.py - ניטור לוגים וזיהוי שגיאות"
    - "activity_tracker.py - מעקב פעילות משתמשים"

demo:
  live_url: "" # TODO: בדוק ידנית
  video_url: "" # TODO: בדוק ידנית

setup:
  quickstart: |
    1. git clone <repository-url> && cd render-monitor-bot
    2. pip install -r requirements.txt
    3. cp .env.example .env  # מלא TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, RENDER_API_KEY, MONGODB_URI
    4. ערוך config.py עם מזהי השירותים שלך
    5. python main.py

your_role: "פיתוח מלא - ארכיטקטורה, Backend, אינטגרציה עם Render API ו-Telegram, ניטור לוגים"

tradeoffs:
  - "שימוש ב-polling במקום webhook - פשטות על חשבון תגובתיות"
  - "MongoDB במקום SQL - גמישות סכמה לנתוני ניטור מגוונים"
  - "schedule library במקום APScheduler - פשטות לתזמון בסיסי"

metrics:
  # TODO: בדוק ידנית
  services_monitored: "7 שירותים מנוטרים"

faq:
  - q: "איך הבוט יודע שיש פעילות בבוט אחר?"
    a: "באמצעות activity_reporter.py שמוטמע בכל בוט ומדווח פעילות ל-MongoDB"
  - q: "מה קורה אם Render API לא זמין?"
    a: "הבוט ממשיך לרוץ ומנסה שוב בבדיקה הבאה"
  - q: "איך מוסיפים שירות חדש לניטור?"
    a: "מוסיפים את ה-service ID לרשימה ב-config.py"
```
