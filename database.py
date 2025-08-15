from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
import config

class Database:
    def __init__(self):
        self.client = MongoClient(config.MONGODB_URI, tz_aware=True)
        self.db = self.client[config.DATABASE_NAME]
        self.services = self.db.service_activity
        self.user_interactions = self.db.user_interactions
        
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
    
    # --- ניטור סטטוס Render ---
    def update_render_status(self, service_id, render_status, service_name=None):
        """עדכון סטטוס אחרון שהגיע מ-Render"""
        update = {
            "render_status": render_status,
            "render_status_updated_at": datetime.now(timezone.utc)
        }
        if service_name:
            update["service_name"] = service_name
        self.services.update_one(
            {"_id": service_id},
            {
                "$set": update,
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
    
    def record_our_action(self, service_id, action_type):
        """רישום פעולה שבוצעה על-ידי הבוט (לצורך חלון השתקה)"""
        self.services.update_one(
            {"_id": service_id},
            {
                "$set": {
                    "last_our_action_type": action_type,
                    "last_our_action_at": datetime.now(timezone.utc)
                }
            },
            upsert=True
        )
    
    def record_transient_status_seen(self, service_id):
        """רישום זמן בו זוהה סטטוס ביניים (דיפלוי/בניה) לצורך השתקת התראות"""
        self.services.update_one(
            {"_id": service_id},
            {"$set": {"last_transient_status_at": datetime.now(timezone.utc)}},
            upsert=True
        )

# יצירת instance גלובלי
db = Database()
