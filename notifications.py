import requests
import config
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def send_notification(message: str):
    """שליחת התראה לאדמין דרך טלגרם"""
    # בדיקת תקינות הגדרות
    if not config.ADMIN_CHAT_ID or config.ADMIN_CHAT_ID == "your_admin_chat_id_here":
        logger.error("ADMIN_CHAT_ID is not configured properly")
        print("⚠️ לא מוגדר ADMIN_CHAT_ID - לא ניתן לשלוח התראה")
        print(f"הודעה שלא נשלחה: {message}")
        return False
    
    if not config.TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN == "your_telegram_bot_token_here":
        logger.error("TELEGRAM_BOT_TOKEN is not configured properly")
        print("⚠️ לא מוגדר TELEGRAM_BOT_TOKEN - לא ניתן לשלוח התראה")
        print(f"הודעה שלא נשלחה: {message}")
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
    
    logger.info(f"Attempting to send notification to chat_id: {config.ADMIN_CHAT_ID}")
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response_data = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
        
        if response.status_code == 200:
            if response_data.get('ok'):
                logger.info(f"Notification sent successfully to chat_id: {config.ADMIN_CHAT_ID}")
                print(f"✅ התראה נשלחה בהצלחה")
                return True
            else:
                error_desc = response_data.get('description', 'Unknown error')
                logger.error(f"Telegram API returned ok=false: {error_desc}")
                print(f"❌ כשלון בשליחת התראה: {error_desc}")
                return False
        else:
            error_desc = response_data.get('description', response.text)
            logger.error(f"Failed to send notification: HTTP {response.status_code} - {error_desc}")
            print(f"❌ כשלון בשליחת התראה: {response.status_code} - {error_desc}")
            
            # טיפול בשגיאות נפוצות
            if response.status_code == 400:
                if "chat not found" in error_desc.lower():
                    logger.error("Chat ID not found - make sure the bot has access to this chat")
                    print("💡 טיפ: ודא שהבוט יכול לשלוח הודעות לצ'אט המבוקש")
                elif "bot was blocked" in error_desc.lower():
                    logger.error("Bot was blocked by the user")
                    print("💡 טיפ: בטל את חסימת הבוט בטלגרם")
            elif response.status_code == 401:
                logger.error("Invalid bot token")
                print("💡 טיפ: בדוק שה-TELEGRAM_BOT_TOKEN נכון")
            
            return False
    except requests.Timeout:
        logger.error("Request timeout while sending notification")
        print(f"❌ פג זמן המתנה בשליחת התראה")
        return False
    except requests.RequestException as e:
        logger.error(f"Network error while sending notification: {str(e)}")
        print(f"❌ שגיאת רשת בשליחת התראה: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error while sending notification: {str(e)}")
        print(f"❌ שגיאה לא צפויה בשליחת התראה: {str(e)}")
        return False

def send_status_change_notification(service_id: str, service_name: str, 
                                   old_status: str, new_status: str, 
                                   emoji: str = "🔔", action: str = "שינה סטטוס"):
    """שליחת התראה על שינוי סטטוס של שירות"""
    message = f"{emoji} *התראת שינוי סטטוס*\n\n"
    message += f"🤖 השירות: *{service_name}*\n"
    message += f"🆔 ID: `{service_id}`\n"
    message += f"📊 הפעולה: {action}\n"
    message += f"⬅️ סטטוס קודם: {old_status}\n"
    message += f"➡️ סטטוס חדש: {new_status}\n\n"
    
    # הוספת הסבר למשמעות
    if new_status == "online":
        message += "✅ השירות חזר לפעילות תקינה"
    elif new_status == "offline":
        message += "⚠️ השירות ירד ואינו זמין"
    elif new_status == "deploying":
        message += "🔄 השירות בתהליך פריסה"
        
    return send_notification(message)

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
    
    # קבלת שירותים עם ניטור סטטוס
    monitored_services = db.get_services_with_monitoring_enabled()
    
    message = "📊 *דוח יומי - מצב השירותים*\n\n"
    message += f"🟢 שירותים פעילים: {len(active_services)}\n"
    message += f"🔴 שירותים מושעים: {len(suspended_services)}\n"
    message += f"👁️ שירותים בניטור סטטוס: {len(monitored_services)}\n"
    message += f"📈 סה\"כ שירותים: {len(all_services)}\n\n"
    
    if suspended_services:
        message += "*שירותים מושעים:*\n"
        for service in suspended_services:
            name = service.get("service_name", service["_id"])
            suspended_at = service.get("suspended_at")
            if suspended_at:
                try:
                    from datetime import timezone
                    if suspended_at.tzinfo is None:
                        suspended_at = suspended_at.replace(tzinfo=timezone.utc)
                    days_suspended = (datetime.now(timezone.utc) - suspended_at).days
                except Exception:
                    # Fallback: treat as naive
                    days_suspended = (datetime.now() - suspended_at.replace(tzinfo=None)).days
                message += f"• {name} (מושעה {days_suspended} ימים)\n"
            else:
                message += f"• {name}\n"
    
    if monitored_services:
        message += "\n*שירותים בניטור סטטוס:*\n"
        for service in monitored_services:
            name = service.get("service_name", service["_id"])
            status = service.get("last_known_status", "unknown")
            status_emoji = "🟢" if status == "online" else "🔴" if status == "offline" else "🟡"
            message += f"{status_emoji} {name} ({status})\n"
    
    send_notification(message)
