from datetime import datetime, timezone, timedelta
import config
from database import db
from render_api import render_api
from notifications import send_notification

class ActivityTracker:
    def __init__(self):
        self.inactive_days_alert = config.INACTIVE_DAYS_ALERT
        self.auto_suspend_days = config.AUTO_SUSPEND_DAYS
        
    def record_bot_usage(self, service_id: str, user_id: int, service_name: str = None):
        """רישום שימוש בבוט"""
        db.record_user_interaction(service_id, user_id)
        if service_name:
            db.update_service_activity(service_id, service_name=service_name)
        print(f"רושם פעילות עבור שירות {service_id}, משתמש {user_id}")
    
    def check_inactive_services(self):
        """בדיקת שירותים לא פעילים והתראות"""
        print("בודק שירותים לא פעילים...")
        
        # בדיקת שירותים להתראה
        alert_services = db.get_inactive_services(self.inactive_days_alert)
        for service in alert_services:
            self._send_inactivity_alert(service)
        
        # בדיקת שירותים להשעיה אוטומטית
        suspend_services = db.get_inactive_services(self.auto_suspend_days)
        for service in suspend_services:
            self._auto_suspend_service(service)
    
    def _send_inactivity_alert(self, service: dict):
        """שליחת התראה על חוסר פעילות"""
        service_id = service["_id"]
        service_name = service.get("service_name", service_id)
        
        # בדיקה אם כבר נשלחה התראה היום
        last_alert = service.get("notification_settings", {}).get("last_alert_sent")
        if last_alert:
            if last_alert.tzinfo is None:
                last_alert = last_alert.replace(tzinfo=timezone.utc)
            time_since_alert = datetime.now(timezone.utc) - last_alert
            if time_since_alert.days < 1:
                return  # כבר נשלחה התראה היום
        
        # חישוב ימי חוסר פעילות
        last_activity = service.get("last_user_activity")
        if last_activity:
            if last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)
            inactive_days = (datetime.now(timezone.utc) - last_activity).days
        else:
            inactive_days = "לא ידוע"
        
        message = f"🔴 התראת חוסר פעילות\n"
        message += f"שירות: {service_name}\n"
        message += f"ID: {service_id}\n"
        message += f"ימים ללא פעילות: {inactive_days}\n"
        message += f"השעיה אוטומטית בעוד: {self.auto_suspend_days - self.inactive_days_alert} ימים"
        
        send_notification(message)
        db.update_alert_sent(service_id)
        print(f"נשלחה התראת חוסר פעילות עבור {service_name}")
    
    def _auto_suspend_service(self, service: dict):
        """השעיה אוטומטית של שירות"""
        service_id = service["_id"]
        service_name = service.get("service_name", service_id)
        
        print(f"מנסה להשעות אוטומטית את השירות {service_name}")
        
        # השעיה ב-Render
        result = render_api.suspend_service(service_id)
        
        if result["success"]:
            # עדכון במסד הנתונים
            db.update_service_activity(service_id, status="suspended")
            db.increment_suspend_count(service_id)
            db.record_our_action(service_id, action_type="auto_suspend")
            
            # שליחת התראה על השעיה מוצלחת
            message = f"✅ השעיה אוטומטית מוצלחת\n"
            message += f"שירות: {service_name}\n"
            message += f"ID: {service_id}\n"
            message += f"סיבה: חוסר פעילות של {self.auto_suspend_days} ימים"
            
            send_notification(message)
            print(f"שירות {service_name} הושעה בהצלחה")
        else:
            # שליחת התראה על כשלון
            message = f"❌ כשלון בהשעיה אוטומטית\n"
            message += f"שירות: {service_name}\n"
            message += f"ID: {service_id}\n"
            message += f"שגיאה: {result['message']}"
            
            send_notification(message)
            print(f"כשלון בהשעיית השירות {service_name}: {result['message']}")
    
    def manual_suspend_service(self, service_id: str) -> dict:
        """השעיה ידנית של שירות"""
        # קבלת מידע על השירות
        service = db.get_service_activity(service_id)
        service_name = service.get("service_name", service_id) if service else service_id
        
        print(f"מנסה להשעות ידנית את השירות {service_name}")
        
        # השעיה ב-Render
        result = render_api.suspend_service(service_id)
        
        if result["success"]:
            # עדכון במסד הנתונים
            db.update_service_activity(service_id, status="suspended")
            db.increment_suspend_count(service_id)
            db.record_our_action(service_id, action_type="manual_suspend")
            print(f"שירות {service_name} הושעה ידנית בהצלחה")
        else:
            print(f"כשלון בהשעיה ידנית של השירות {service_name}: {result['message']}")
        
        return result
    
    def manual_resume_service(self, service_id: str) -> dict:
        """החזרה ידנית של שירות לפעילות"""
        # קבלת מידע על השירות
        service = db.get_service_activity(service_id)
        service_name = service.get("service_name", service_id) if service else service_id
        
        print(f"מנסה להחזיר לפעילות את השירות {service_name}")
        
        # החזרה לפעילות ב-Render
        result = render_api.resume_service(service_id)
        
        if result["success"]:
            # עדכון במסד הנתונים
            db.update_service_activity(service_id, status="active")
            db.record_our_action(service_id, action_type="manual_resume")
            
            # שליחת התראה על החזרה מוצלחת
            message = f"✅ החזרה לפעילות מוצלחת\n"
            message += f"שירות: {service_name}\n"
            message += f"ID: {service_id}"
            
            send_notification(message)
            print(f"שירות {service_name} הוחזר לפעילות בהצלחה")
        else:
            # שליחת התראה על כשלון
            message = f"❌ כשלון בהחזרה לפעילות\n"
            message += f"שירות: {service_name}\n"
            message += f"ID: {service_id}\n"
            message += f"שגיאה: {result['message']}"
            
            send_notification(message)
            print(f"כשלון בהחזרת השירות {service_name}: {result['message']}")
        
        return result

# יצירת instance גלובלי
activity_tracker = ActivityTracker()
