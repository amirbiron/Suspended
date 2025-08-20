from datetime import datetime
from typing import Optional

import requests

import config


def _send_to_chat(chat_id: str, message: str, title: Optional[str] = None) -> bool:
    """שליחת הודעה לצ'אט נתון דרך טלגרם"""
    if not chat_id or not config.TELEGRAM_BOT_TOKEN:
        print("⚠️ חסר chat_id או TELEGRAM_BOT_TOKEN - לא ניתן לשלוח התראה")
        print(f"הודעה: {message}")
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"

    timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
    # כותרת מותאמת (לשיפור תצוגת תצוגה מקדימה), אם לא ניתנה נשתמש בכותרת ברירת מחדל
    if title:
        formatted_message = f"{title}\n"
    else:
        formatted_message = "🤖 *Render Monitor Bot*\n"
    formatted_message += f"⏰ {timestamp}\n\n"
    formatted_message += message

    payload = {"chat_id": str(chat_id), "text": formatted_message, "parse_mode": "Markdown"}

    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"❌ כשלון בשליחת התראה: {response.status_code} - {response.text}")
            return False
        try:
            data = response.json()
        except Exception:
            print("❌ תגובת טלגרם אינה JSON תקין")
            return False
        if bool(data.get("ok")):
            print("✅ התראה נשלחה בהצלחה")
            return True
        description = data.get("description") or data
        print(f"❌ טלגרם דחה את ההודעה: {description}")
        return False
    except requests.RequestException as e:
        print(f"❌ שגיאה בשליחת התראה: {str(e)}")
        return False


def send_notification(message: str):
    """שליחת התראה לאדמין דרך טלגרם"""
    if not config.ADMIN_CHAT_ID or not config.TELEGRAM_BOT_TOKEN:
        print("⚠️ לא מוגדר ADMIN_CHAT_ID או TELEGRAM_BOT_TOKEN - לא ניתן לשלוח התראה")
        print(f"הודעה: {message}")
        return False
    return _send_to_chat(config.ADMIN_CHAT_ID, message)


def send_status_change_notification(
    service_id: str, service_name: str, old_status: str, new_status: str, emoji: str = "🔔", action: str = "שינה סטטוס"
):
    """שליחת התראה על שינוי סטטוס של שירות"""
    message = f"{emoji} *התראת שינוי סטטוס*\n\n"
    # Escape כדי למנוע כשלי Markdown בעת שליחת הודעה לטלגרם
    safe_service_name = str(service_name).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
    safe_action = str(action).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
    safe_old_status = str(old_status).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
    safe_new_status = str(new_status).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")

    message += f"🤖 השירות: *{safe_service_name}*\n"
    # בטלגרם backticks יכולים לשבור Markdown אם יש תווים מיוחדים ב-ID, נחליף backtick חזרה
    safe_service_id = str(service_id).replace("`", "\\`")
    message += f"🆔 ID: `{safe_service_id}`\n"
    message += f"📊 הפעולה: {safe_action}\n"
    message += f"⬅️ סטטוס קודם: {safe_old_status}\n"
    message += f"➡️ סטטוס חדש: {safe_new_status}\n\n"

    # הוספת הסבר למשמעות
    if new_status == "online":
        message += "✅ השירות חזר לפעילות תקינה"
    elif new_status == "offline":
        message += "⚠️ השירות ירד ואינו זמין"
    elif new_status == "deploying":
        message += "🔄 השירות בתהליך פריסה"

    # שליחה לאדמין עם כותרת קצרה בראש ההודעה
    short_title = f"{emoji} *{safe_service_name}* – {safe_action}"
    sent_admin = _send_to_chat(config.ADMIN_CHAT_ID, message, title=short_title)

    # בנוסף: אם יש מפעיל ניטור לשירות – שלח גם אליו
    try:
        from database import db

        service = db.get_service_activity(service_id) or {}
        monitoring_info = service.get("status_monitoring", {})
        enabled_by = monitoring_info.get("enabled_by")
        if enabled_by and str(enabled_by) != str(config.ADMIN_CHAT_ID):
            _send_to_chat(str(enabled_by), message, title=short_title)
    except Exception:
        pass

    return sent_admin


def send_startup_notification():
    """התראה על הפעלת הבוט"""
    message = "🚀 בוט ניטור Render הופעל בהצלחה"
    send_notification(message)


def send_deploy_event_notification(
    service_name: str,
    service_id: str,
    status: str,
    commit_message: Optional[str] = None,
) -> bool:
    """התראה ממוקדת על דיפלוי שהסתיים (סיום/כשלון)"""
    emoji = "🚀" if str(status).lower() in ["succeeded", "success", "completed", "deployed", "live"] else "⚠️"
    title = "סיום פריסה מוצלח" if emoji == "🚀" else "כשלון בפריסה"
    safe_service_id = str(service_id).replace("`", "\\`")
    safe_service_name = str(service_name).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
    message = f"{emoji} *{title}*\n\n"
    message += f"🤖 השירות: *{safe_service_name}*\n"
    message += f"🆔 ID: `{safe_service_id}`\n"
    # הימנעות משבירת Markdown ע"י תווים מיוחדים בסטטוס
    safe_status = str(status).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
    message += f"סטטוס דיפלוי: {safe_status}\n"
    if commit_message:
        # חיתוך כדי לא לשבור הודעות ארוכות במיוחד
        trimmed = commit_message.strip().replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
        if len(trimmed) > 200:
            trimmed = trimmed[:197] + "..."
        message += f"📝 Commit: {trimmed}\n"
    # כותרת קצרה שמדגישה את שם השירות לשורה הראשונה
    short_title = f"{emoji} *{safe_service_name}* – {title}"
    sent_admin = bool(_send_to_chat(config.ADMIN_CHAT_ID, message, title=short_title))

    # בנוסף: ניסיון לשלוח גם למי שהפעיל ניטור על השירות (אם קיים)
    try:
        from database import db

        service = db.get_service_activity(service_id) or {}
        monitoring_info = service.get("status_monitoring", {})
        enabled_by = monitoring_info.get("enabled_by")
        if enabled_by and str(enabled_by) != str(config.ADMIN_CHAT_ID):
            _send_to_chat(str(enabled_by), message, title=short_title)
    except Exception:
        pass

    return sent_admin


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
    message += f'📈 סה"כ שירותים: {len(all_services)}\n\n'

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
