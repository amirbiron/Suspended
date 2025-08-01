import requests
import config
from datetime import datetime

def send_notification(message: str):
    """שליחת התראה לאדמין דרך טלגרם"""
    if not config.ADMIN_CHAT_ID or not config.TELEGRAM_BOT_TOKEN:
        print("⚠️ לא מוגדר ADMIN_CHAT_ID או TELEGRAM_BOT_TOKEN - לא ניתן לשלוח התראה")
        print(f"הודעה: {message}")
        return False
    
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # הוספת חותמת זמן
    timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
    formatted_message = f"🤖 *Render Monitor Bot*\n"
    formatted_message += f"⏰ {timestamp}\n\n"
    formatted_message += message
    
    payload = {
        "chat_id": config.ADMIN_CHAT_ID,
        "text": formatted_message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            print(f"✅ התראה נשלחה בהצלחה")
            return True
        else:
            print(f"❌ כשלון בשליחת התראה: {response.status_code} - {response.text}")
            return False
    except requests.RequestException as e:
        print(f"❌ שגיאה בשליחת התראה: {str(e)}")
        return False

def send_startup_notification():
    """התראה על הפעלת הבוט"""
    message = "🚀 בוט ניטור Render הופעל בהצלחה"
    send_notification(message)

def send_daily_report():
    """דוח יומי על מצב השירותים"""
    from database import db
    
    # קבלת נתונים
    all_services = db.get_all_services()
    suspended_services = [s for s in all_services if s.get("status") == "suspended"]
    active_services = [s for s in all_services if s.get("status") != "suspended"]
    
    message = "📊 *דוח יומי - מצב השירותים*\n\n"
    message += f"🟢 שירותים פעילים: {len(active_services)}\n"
    message += f"🔴 שירותים מושעים: {len(suspended_services)}\n"
    message += f"📈 סה\"כ שירותים: {len(all_services)}\n\n"
    
    if suspended_services:
        message += "*שירותים מושעים:*\n"
        for service in suspended_services:
            name = service.get("service_name", service["_id"])
            suspended_at = service.get("suspended_at")
            if suspended_at:
                days_suspended = (datetime.now() - suspended_at).days
                message += f"• {name} (מושעה {days_suspended} ימים)\n"
            else:
                message += f"• {name}\n"
    
    send_notification(message)
