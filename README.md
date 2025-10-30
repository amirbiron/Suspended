# 🤖 בוט חכם לחיסכון בעלויות ב-Render

בוט שמנטר את הבוטים שלך בטלגרם ומשעה אוטומטית שירותים ב-Render שלא היו בשימוש במשך מספר ימים מוגדר.

## ✨ תכונות

- 🔍 **ניטור אוטומטי** - בדיקה יומית של פעילות הבוטים
- ⏸️ **השעיה חכמה** - השעיית שירותים שלא היו בשימוש
- ▶️ **החזרה לפעילות** - החזרת שירותים מושעים בפקודה אחת
- 🔔 **התראות** - התראות על השעיות והחזרות
- 📊 **מעקב מלא** - שמירת היסטוריה במסד נתונים MongoDB
- 🎛️ **שליטה מלאה** - פקודות טלגרם לניהול השירותים
- 📋 **ניטור לוגים** 🆕 - זיהוי אוטומטי של שגיאות בזמן אמת
- 🔍 **צפייה בלוגים** 🆕 - הצגת לוגים ישירות מהבוט

## 📋 דרישות

- חשבון Render עם API Key
- בוט טלגרם (יצירה ב-@BotFather)
- מסד נתונים MongoDB (מקומי או Atlas)
- Python 3.8+

## 🚀 התקנה והפעלה

### 1. הורדה והתקנת תלויות

```bash
git clone <repository-url>
cd render-monitor-bot
pip install -r requirements.txt
```

### 2. הגדרת משתני סביבה

העתק את `.env.example` ל-`.env` ומלא את הפרטים:

```bash
cp .env.example .env
```

ערוך את `.env`:
```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
ADMIN_CHAT_ID=your_chat_id_here
RENDER_API_KEY=your_render_api_key_here
MONGODB_URI=your_mongodb_connection_string
STATUS_MONITORING_ENABLED=true
STATUS_CHECK_INTERVAL_SECONDS=300
DEPLOY_CHECK_INTERVAL_SECONDS=30
AUTO_SUSPEND_ENABLED=false
```

### 📨 התראות דיפלוי
- ניתן להפעיל/לכבות לכל שירות מתוך המסך "ניהול ניטור סטטוס" בבוט.
- המערכת שולחת התראה ב-2 מצבים:
  1. כאשר זוהה מעבר מ-`deploying` ל-`online` או `offline` בזמן הניטור.
  2. כגיבוי: כאשר דיפלוי הסתיים (success/failed) גם אם החמצנו את שלב ה-`deploying`. במקרה זה ההתראה תישלח פעם אחת לכל דיפלוי חדש.

### 📋 ניטור לוגים 🆕
- **זיהוי אוטומטי של שגיאות** - הבוט סורק את הלוגים כל דקה ומזהה שגיאות
- **התראות בזמן אמת** - קבל התראה מיידית כשמתגלות שגיאות
- **שגיאות קריטיות** - זיהוי מיוחד לשגיאות חמורות (fatal, out of memory, וכו')
- **סף שגיאות מותאם** - הגדר כמה שגיאות נדרשות להתראה (ברירת מחדל: 5)
- **צפייה ישירה** - הצג לוגים ישירות מהבוט ללא צורך בגישה ל-Render

**תבניות שגיאות שמזוהות:**
- שגיאות רגילות: `error`, `exception`, `failed`, `crash`, קודי HTTP 4xx/5xx
- שגיאות קריטיות: `fatal`, `out of memory`, `disk full`, `connection refused`

**איך להשתמש:**
```bash
# 1. צפה בלוגים
/logs srv-123456

# 2. הפעל ניטור אוטומטי
/logs_monitor srv-123456 5

# 3. קבל התראות כשיש שגיאות! 🔔
```

📖 **למדריך מפורט:** ראה [LOG_MONITORING_GUIDE.md](LOG_MONITORING_GUIDE.md)

### 3. הגדרת שירותים לניטור

ערוך את `config.py` והוסף את מזהי השירותים שלך:

```python
SERVICES_TO_MONITOR = [
    "srv-your-service-id-1",
    "srv-your-service-id-2"
]
```

### 4. הפעלה

```bash
python main.py
```

## 🎮 פקודות הבוט

### פקודות בסיסיות
- `/start` - התחלת השיחה עם הבוט
- `/status` - הצגת מצב כל השירותים
- `/suspend` - השעיית כל השירותים
- `/resume` - החזרת כל השירותים המושעים
- `/list_suspended` - רשימת שירותים מושעים
- `/help` - עזרה ורשימת פקודות

### פקודות ניטור לוגים 🆕
- `/logs [service_id] [lines]` - צפייה בלוגים של שירות
- `/logs_monitor [service_id] [threshold]` - הפעלת ניטור אוטומטי של שגיאות
- `/logs_unmonitor [service_id]` - כיבוי ניטור לוגים
- `/logs_manage` - ניהול ניטור לוגים עם ממשק כפתורים

**דוגמאות:**
```bash
/logs srv-123456              # 100 שורות אחרונות
/logs srv-123456 50           # 50 שורות אחרונות
/logs srv-123456 100 5        # 100 שורות מה-5 דקות האחרונות
/logs_monitor srv-123456 5    # התראה אחרי 5 שגיאות
```

**הסבר על פרמטר הזמן:**
- **ללא זמן** (`/logs srv-123 100`): מציג את 100 השורות **האחרונות** מכל הזמן
- **עם זמן** (`/logs srv-123 100 5`): מציג את 100 השורות האחרונות **מה-5 דקות האחרונות**
- הלוגים מוצגים **כרונולוגית** (מהישן לחדש)
- כל הודעה מכילה **טווח זמן** (🕐) כדי שתדע בדיוק מה אתה רואה

## ⚙️ הגדרות מתקדמות

ב-`config.py` ניתן לשנות:

```python
INACTIVE_DAYS_ALERT = 3  # התראה אחרי כמה ימים
AUTO_SUSPEND_DAYS = 7    # השעיה אוטומטית אחרי כמה ימים
CHECK_INTERVAL_HOURS = 24  # תדירות בדיקה
AUTO_SUSPEND_ENABLED = False  # כדי לבטל השעיה אוטומטית, השאר False
```

## 📊 מבנה מסד הנתונים

### Collection: service_activity
```json
{
  "_id": "srv-abc123",
  "service_name": "my-bot",
  "last_user_activity": "2024-08-01T10:30:00Z",
  "status": "active",
  "suspended_at": null,
  "suspend_count": 0,
  "notification_settings": {
    "alert_after_days": 3,
    "auto_suspend_after_days": 7
  }
}
```

### Collection: user_interactions
```json
{
  "service_id": "srv-abc123",
  "user_id": 123456789,
  "last_interaction": "2024-08-01T10:30:00Z",
  "interaction_count": 25
}
```

## 🔧 שימוש בבוטים שלך

כדי שהמערכת תזהה פעילות בבוטים שלך:

### 1. העתק את `activity_reporter.py` לכל בוט

### 2. הוסף בכל בוט 4 שורות בלבד:

```python
# בראש הקובץ
from activity_reporter import create_reporter

# הגדרה חד-פעמית
reporter = create_reporter(
    mongodb_uri="your_mongodb_connection_string",
    service_id="srv-your-service-id-from-render",
    service_name="שם הבוט שלך"  # אופציונלי
)

# בכל handler - שורה אחת!
def handle_message(update, context):
    reporter.report_activity(update.effective_user.id)  # זה הכל!

    # השאר הלוגיקה שלך כרגיל...
```

**זהו!** הבוט המרכזי יזהה את הפעילות אוטומטically.

## 🌐 הפעלה ב-Render

1. צור שירות חדש ב-Render
2. חבר את הרפוזיטורי
3. הגדר את משתני הסביבה בדשבורד של Render
4. השירות יעלה אוטומטית

## 🆘 פתרון בעיות

**הבוט לא מגיב:**
- ודא שה-TELEGRAM_BOT_TOKEN תקין
- בדק שהבוט קיבל הרשאות מתאימות

**כשלון בחיבור ל-MongoDB:**
- ודא שמחרוזת החיבור נכונה
- בדק שמסד הנתונים זמין

**כשלון ב-Render API:**
- ודא שה-API_KEY תקין ופעיל
- בדק שמזהי השירותים נכונים

## 📝 רישיון

MIT License - ראה קובץ LICENSE לפרטים

## 🤝 תרומה

Pull Requests מתקבלים בברכה! אנא פתח Issue קודם לדיון על שינויים גדולים.
