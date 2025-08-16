# 🤖 בוט חכם לחיסכון בעלויות ב-Render

בוט שמנטר את הבוטים שלך בטלגרם ומשעה אוטומטית שירותים ב-Render שלא היו בשימוש במשך מספר ימים מוגדר.

## ✨ תכונות

- 🔍 **ניטור אוטומטי** - בדיקה יומית של פעילות הבוטים
- ⏸️ **השעיה חכמה** - השעיית שירותים שלא היו בשימוש
- ▶️ **החזרה לפעילות** - החזרת שירותים מושעים בפקודה אחת
- 🔔 **התראות** - התראות על השעיות והחזרות
- 📊 **מעקב מלא** - שמירת היסטוריה במסד נתונים MongoDB
- 🎛️ **שליטה מלאה** - פקודות טלגרם לניהול השירותים

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
```

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

- `/start` - התחלת השיחה עם הבוט
- `/status` - הצגת מצב כל השירותים
- `/suspend` - השעיית כל השירותים
- `/resume` - החזרת כל השירותים המושעים
- `/list_suspended` - רשימת שירותים מושעים
- `/help` - עזרה ורשימת פקודות

## ⚙️ הגדרות מתקדמות

ב-`config.py` ניתן לשנות:

```python
INACTIVE_DAYS_ALERT = 3  # התראה אחרי כמה ימים
AUTO_SUSPEND_DAYS = 7    # השעיה אוטומטית אחרי כמה ימים
CHECK_INTERVAL_HOURS = 24  # תדירות בדיקה
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
