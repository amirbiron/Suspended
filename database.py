from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
import config

class Database:
    def __init__(self):
        self.client = MongoClient(config.MONGODB_URI, tz_aware=True)
        self.db = self.client[config.DATABASE_NAME]
        self.services = self.db.service_activity
        self.user_interactions = self.db.user_interactions
        self.status_changes = self.db.status_changes  # New collection for status history
        self.manual_actions = self.db.manual_actions  # New collection for manual actions
        
    def get_service_activity(self, service_id):
        """קבלת נתוני פעילות של שירות"""
        return self.services.find_one({"_id": service_id})
    
    def update_service_activity(self, service_id, service_name=None, last_activity=None, status=None):
        """עדכון פעילות שירות"""
        update_data = {"updated_at": datetime.now(timezone.utc)}
        
        if last_activity:
            update_data["last_user_activity"] = last_activity
            update_data["inactive_days"] = 0
            
        if status:
            update_data["status"] = status
            if status == "suspended":
                update_data["suspended_at"] = datetime.now(timezone.utc)
            elif status == "active":
                update_data["resumed_at"] = datetime.now(timezone.utc)
                
        if service_name:
            update_data["service_name"] = service_name
            
        return self.services.update_one(
            {"_id": service_id},
            {
                "$set": update_data,
                "$setOnInsert": {
                    "created_at": datetime.now(timezone.utc),
                    "total_users": 0,
                    "suspend_count": 0,
                    "notification_settings": {
                        "alert_after_days": config.INACTIVE_DAYS_ALERT,
                        "auto_suspend_after_days": config.AUTO_SUSPEND_DAYS,
                        "last_alert_sent": None
                    }
                }
            },
            upsert=True
        )
    
    def record_user_interaction(self, service_id, user_id):
        """רישום אינטראקציה של משתמש"""
        now = datetime.now(timezone.utc)
        
        # עדכון אינטראקציית המשתמש
        self.user_interactions.update_one(
            {"service_id": service_id, "user_id": user_id},
            {
                "$set": {"last_interaction": now},
                "$inc": {"interaction_count": 1},
                "$setOnInsert": {"created_at": now}
            },
            upsert=True
        )
        
        # עדכון פעילות השירות
        self.update_service_activity(service_id, last_activity=now)
        
    def get_inactive_services(self, days_inactive):
        """קבלת שירותים שלא היו פעילים"""
        cutoff_date = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cutoff_date = cutoff_date - timedelta(days=days_inactive)
        
        return list(self.services.find({
            "$or": [
                {"last_user_activity": {"$lt": cutoff_date}},
                {"last_user_activity": {"$exists": False}}
            ],
            "status": {"$ne": "suspended"}
        }))
    
    def get_suspended_services(self):
        """קבלת שירותים מושעים"""
        return list(self.services.find({"status": "suspended"}))
    
    def get_all_services(self):
        """קבלת כל השירותים"""
        return list(self.services.find())
    
    def update_alert_sent(self, service_id):
        """עדכון שהתראה נשלחה"""
        self.services.update_one(
            {"_id": service_id},
            {"$set": {"notification_settings.last_alert_sent": datetime.now(timezone.utc)}}
        )
    
    def increment_suspend_count(self, service_id):
        """הגדלת מספר ההשעיות"""
        self.services.update_one(
            {"_id": service_id},
            {"$inc": {"suspend_count": 1}}
        )
    
    # ===== New methods for status monitoring =====
    
    def enable_status_monitoring(self, service_id: str, user_id: int, service_name: str = None, current_status: str = None):
        """הפעלת ניטור סטטוס לשירות"""
        update_data = {
            "status_monitoring.enabled": True,
            "status_monitoring.enabled_by": user_id,
            "status_monitoring.enabled_at": datetime.now(timezone.utc),
            "status_monitoring.notify_deploy": True  # ברירת מחדל - התראות על deploy מופעלות
        }
        
        if service_name:
            update_data["service_name"] = service_name
            
        if current_status:
            update_data["last_known_status"] = current_status
            
        result = self.services.update_one(
            {"_id": service_id},
            {
                "$set": update_data,
                "$setOnInsert": {
                    "created_at": datetime.now(timezone.utc),
                    "status": "active"
                }
            },
            upsert=True
        )
        
        return result.modified_count > 0 or result.upserted_id is not None
    
    def disable_status_monitoring(self, service_id: str, user_id: int):
        """כיבוי ניטור סטטוס לשירות"""
        return self.services.update_one(
            {"_id": service_id},
            {
                "$set": {
                    "status_monitoring.enabled": False,
                    "status_monitoring.disabled_by": user_id,
                    "status_monitoring.disabled_at": datetime.now(timezone.utc)
                }
            }
        )
    
    def toggle_deploy_notifications(self, service_id: str, enable: bool):
        """הפעלה/כיבוי של התראות deploy לשירות"""
        return self.services.update_one(
            {"_id": service_id},
            {"$set": {"status_monitoring.notify_deploy": enable}}
        )
    
    def get_status_monitored_services(self):
        """קבלת רשימת שירותים עם ניטור סטטוס פעיל"""
        return list(self.services.find({
            "status_monitoring.enabled": True
        }))
    
    def update_service_status(self, service_id: str, status: str):
        """עדכון הסטטוס הנוכחי של שירות"""
        return self.services.update_one(
            {"_id": service_id},
            {
                "$set": {
                    "last_known_status": status,
                    "last_status_check": datetime.now(timezone.utc)
                }
            }
        )
    
    def record_status_change(self, service_id: str, old_status: str, new_status: str):
        """רישום שינוי סטטוס בהיסטוריה"""
        return self.status_changes.insert_one({
            "service_id": service_id,
            "old_status": old_status,
            "new_status": new_status,
            "timestamp": datetime.now(timezone.utc),
            "detected_at": datetime.now(timezone.utc)
        })
    
    def record_manual_action(self, service_id: str, action_type: str = "manual"):
        """רישום פעולה ידנית על שירות"""
        return self.manual_actions.insert_one({
            "service_id": service_id,
            "action_type": action_type,
            "timestamp": datetime.now(timezone.utc)
        })
    
    def get_last_manual_action(self, service_id: str):
        """קבלת הפעולה הידנית האחרונה על שירות"""
        return self.manual_actions.find_one(
            {"service_id": service_id},
            sort=[("timestamp", -1)]
        )
    
    def get_status_history(self, service_id: str, limit: int = 10):
        """קבלת היסטוריית שינויי סטטוס של שירות"""
        return list(self.status_changes.find(
            {"service_id": service_id},
            sort=[("timestamp", -1)],
            limit=limit
        ))
    
    def get_services_with_monitoring_enabled(self):
        """קבלת רשימת שירותים עם פרטי הניטור שלהם"""
        return list(self.services.find(
            {"status_monitoring.enabled": True},
            {
                "_id": 1,
                "service_name": 1,
                "last_known_status": 1,
                "status_monitoring": 1
            }
        ))

# יצירת instance גלובלי
db = Database()
