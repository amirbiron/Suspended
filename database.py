from datetime import datetime, timedelta, timezone
from typing import Optional

from pymongo import MongoClient

import config


class Database:
    def __init__(self):
        self.client = MongoClient(config.MONGODB_URI)
        self.db = self.client[config.DATABASE_NAME]
        self.services = self.db.service_activity
        self.user_interactions = self.db.user_interactions
        self.manual_actions = self.db.manual_actions  # Collection for manual actions
        self.status_changes = self.db.status_changes  # Collection for status change history
        self.deploy_events = self.db.deploy_events  # היסטוריית דיפלויים אחרונים שדווחו

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
                        "last_alert_sent": None,
                    },
                },
            },
            upsert=True,
        )

    def record_user_interaction(self, service_id, user_id):
        """רישום אינטראקציה של משתמש"""
        now = datetime.now(timezone.utc)

        # עדכון אינטראקציית המשתמש
        self.user_interactions.update_one(
            {"service_id": service_id, "user_id": user_id},
            {"$set": {"last_interaction": now}, "$inc": {"interaction_count": 1}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

        # עדכון פעילות השירות
        self.update_service_activity(service_id, last_activity=now)

    def get_inactive_services(self, days_inactive):
        """קבלת שירותים שלא היו פעילים"""
        cutoff_date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff_date = cutoff_date - timedelta(days=days_inactive)

        return list(
            self.services.find(
                {
                    "$or": [{"last_user_activity": {"$lt": cutoff_date}}, {"last_user_activity": {"$exists": False}}],
                    "status": {"$ne": "suspended"},
                }
            )
        )

    def get_suspended_services(self):
        """קבלת שירותים מושעים"""
        return list(self.services.find({"status": "suspended"}))

    def get_all_services(self):
        """קבלת כל השירותים"""
        return list(self.services.find())

    def update_alert_sent(self, service_id):
        """עדכון שהתראה נשלחה"""
        self.services.update_one(
            {"_id": service_id}, {"$set": {"notification_settings.last_alert_sent": datetime.now(timezone.utc)}}
        )

    def increment_suspend_count(self, service_id):
        """הגדלת מספר ההשעיות"""
        self.services.update_one({"_id": service_id}, {"$inc": {"suspend_count": 1}})

    # ===== New methods for status monitoring =====

    def enable_status_monitoring(
        self,
        service_id: str,
        user_id: int,
        service_name: Optional[str] = None,
        current_status: Optional[str] = None,
    ):
        """הפעלת ניטור סטטוס לשירות"""
        update_data = {
            "status_monitoring.enabled": True,
            "status_monitoring.enabled_by": user_id,
            "status_monitoring.enabled_at": datetime.now(timezone.utc),
        }

        if service_name:
            update_data["service_name"] = service_name

        if current_status:
            update_data["last_known_status"] = current_status

        return self.services.update_one(
            {"_id": service_id}, {"$set": update_data, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}}, upsert=True
        )

    def disable_status_monitoring(self, service_id: str, user_id: int):
        """כיבוי ניטור סטטוס לשירות"""
        return self.services.update_one(
            {"_id": service_id},
            {
                "$set": {
                    "status_monitoring.enabled": False,
                    "status_monitoring.disabled_by": user_id,
                    "status_monitoring.disabled_at": datetime.now(timezone.utc),
                }
            },
        )

    def get_status_monitored_services(self):
        """קבלת רשימת שירותים עם ניטור סטטוס פעיל"""
        return list(self.services.find({"status_monitoring.enabled": True}))

    def update_service_status(self, service_id: str, status: str):
        """עדכון הסטטוס הנוכחי של שירות"""
        return self.services.update_one(
            {"_id": service_id}, {"$set": {"last_known_status": status, "last_status_check": datetime.now(timezone.utc)}}
        )

    def record_manual_action(self, service_id: str, action_type: str = "manual"):
        """רישום פעולה ידנית על שירות"""
        return self.manual_actions.insert_one(
            {"service_id": service_id, "action_type": action_type, "timestamp": datetime.now(timezone.utc)}
        )

    def get_last_manual_action(self, service_id: str):
        """קבלת הפעולה הידנית האחרונה על שירות"""
        return self.manual_actions.find_one({"service_id": service_id}, sort=[("timestamp", -1)])

    def get_services_with_monitoring_enabled(self):
        """קבלת רשימת שירותים עם פרטי הניטור שלהם"""
        return list(
            self.services.find(
                {"status_monitoring.enabled": True},
                {"_id": 1, "service_name": 1, "last_known_status": 1, "status_monitoring": 1},
            )
        )

    def get_services_with_deploy_notifications_enabled(self):
        """החזרת שירותים שעבורם התראות דיפלוי מופעלות (גם אם ניטור סטטוס כבוי)"""
        return list(
            self.services.find(
                {"deploy_notifications_enabled": True},
                {
                    "_id": 1,
                    "service_name": 1,
                    "last_known_status": 1,
                    "status_monitoring": 1,
                    "deploy_notifications_enabled": 1,
                },
            )
        )

    def clear_test_data(self):
        """מחיקת נתוני בדיקות דמה מהמערכת"""
        # איפוס סטטוס של שירותים שנמצאים במצב בדיקה
        self.services.update_many(
            {"last_known_status": {"$in": ["test", "testing"]}},
            {"$unset": {"last_known_status": 1, "status_monitoring.last_check": 1}},
        )

        # איפוס last_activity עבור שירותים שהפעילות האחרונה שלהם הייתה בדיקה
        # זה ימנע מהם להופיע כפעילים בניהול סטטוס
        self.services.update_many(
            {
                "$or": [
                    {"last_activity": {"$regex": "test", "$options": "i"}},
                    {"service_name": {"$regex": "test", "$options": "i"}},
                ]
            },
            {"$unset": {"last_activity": 1, "last_activity_date": 1}},
        )

        # מחיקת פעולות ידניות של בדיקות
        result = self.manual_actions.delete_many(
            {"$or": [{"action": {"$regex": "test", "$options": "i"}}, {"service_id": {"$regex": "test", "$options": "i"}}]}
        )

        return result.deleted_count

    def toggle_deploy_notifications(self, service_id: str, enabled: bool):
        """הפעלה/כיבוי התראות דיפלוי לשירות ספציפי"""
        return self.services.update_one({"_id": service_id}, {"$set": {"deploy_notifications_enabled": enabled}})

    def get_deploy_notification_status(self, service_id: str):
        """קבלת סטטוס התראות דיפלוי לשירות"""
        service = self.services.find_one({"_id": service_id})
        if service:
            return service.get("deploy_notifications_enabled", False)
        return False

    def record_status_change(self, service_id: str, old_status: str, new_status: str, source: str = "test"):
        """רישום שינוי סטטוס בהיסטוריה"""
        return self.status_changes.insert_one(
            {
                "service_id": service_id,
                "old_status": old_status,
                "new_status": new_status,
                "source": source,
                "timestamp": datetime.now(timezone.utc),
            }
        )

    # ===== תמיכה בהתראות דיפלוי =====
    def get_last_reported_deploy_id(self, service_id: str) -> Optional[str]:
        """מחזיר את מזהה הדיפלוי האחרון שדווח עבור שירות, אם קיים"""
        doc = self.deploy_events.find_one({"service_id": service_id}, sort=[("reported_at", -1)])
        return doc.get("deploy_id") if doc else None

    def record_reported_deploy(self, service_id: str, deploy_id: str, status: str):
        """רישום דיפלוי שדווח כדי למנוע כפילויות"""
        return self.deploy_events.insert_one(
            {"service_id": service_id, "deploy_id": deploy_id, "status": status, "reported_at": datetime.now(timezone.utc)}
        )


# יצירת instance גלובלי
db = Database()
