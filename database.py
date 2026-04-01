import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

import config

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.client = MongoClient(
            config.MONGODB_URI,
            serverSelectionTimeoutMS=45000,
            connectTimeoutMS=30000,
            socketTimeoutMS=45000,
            retryWrites=True,
            retryReads=True,
            maxPoolSize=10,
            minPoolSize=1,
            maxIdleTimeMS=60000,
            waitQueueTimeoutMS=30000,
        )
        self.db = self.client[config.DATABASE_NAME]
        self.services = self.db.service_activity
        self.user_interactions = self.db.user_interactions
        self.manual_actions = self.db.manual_actions  # Collection for manual actions
        self.status_changes = self.db.status_changes  # Collection for status change history
        self.deploy_events = self.db.deploy_events  # היסטוריית דיפלויים אחרונים שדווחו
        self.reminders = self.db.reminders  # תזכורות משתמשים

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
                    "removed": {"$ne": True},
                }
            )
        )

    def get_suspended_services(self):
        """קבלת שירותים מושעים"""
        return list(self.services.find({"status": "suspended", "removed": {"$ne": True}}))

    def get_all_services(self):
        """קבלת כל השירותים"""
        return list(self.services.find({"removed": {"$ne": True}}))

    def ensure_service_exists(
        self,
        service_id: str,
        *,
        owner_id: Optional[str] = None,
        service_name: Optional[str] = None,
    ):
        """ודא שקיים מסמך שירות בסיסי במסד (לצורך תאימות לאחור עם SERVICES_TO_MONITOR).

        חשוב: משתמשים ב-$setOnInsert בלבד כדי לא "לגעת" בשירותים קיימים.
        בנוסף, נגדיר last_user_activity בעת יצירה כדי למנוע התראות "לא ידוע" מיד לאחר רישום.
        """
        now = datetime.now(timezone.utc)
        base_doc = {
            "created_at": now,
            "updated_at": now,
            "service_name": service_name or service_id,
            "status": "active",
            "removed": False,
            "last_user_activity": now,
            "inactive_days": 0,
            "total_users": 0,
            "suspend_count": 0,
            "notification_settings": {
                "alert_after_days": config.INACTIVE_DAYS_ALERT,
                "auto_suspend_after_days": config.AUTO_SUSPEND_DAYS,
                "last_alert_sent": None,
            },
            "registered_at": now,
        }
        if owner_id:
            base_doc["owner_id"] = str(owner_id)
            base_doc["registered_by"] = str(owner_id)

        return self.services.update_one({"_id": service_id}, {"$setOnInsert": base_doc}, upsert=True)

    def register_service(
        self,
        service_id: str,
        owner_id: str,
        service_name: str,
        *,
        force_owner_update: bool = False,
        claim_owner_if_unowned: bool = False,
    ):
        """רישום שירות חדש (או עדכון שם) עם owner.

        שומר ב-`service_activity` עם `_id`=service_id ומוסיף owner_id.
        """
        now = datetime.now(timezone.utc)
        owner_str = str(owner_id)

        if force_owner_update and claim_owner_if_unowned:
            raise ValueError("force_owner_update and claim_owner_if_unowned are mutually exclusive")

        update_set: dict = {
            "updated_at": now,
            "service_name": service_name,
            # אם השירות הוסר בעבר — רישום מחדש מחזיר אותו לרשימה
            "removed": False,
            "removed_at": None,
            "removed_by": None,
        }

        # כברירת מחדל: לא דורסים בעלות קיימת.
        # אם רוצים להעביר בעלות (אדמין/תהליך ייעודי) אפשר להפעיל force_owner_update.
        if force_owner_update or claim_owner_if_unowned:
            update_set["owner_id"] = owner_str

        set_on_insert: dict = {
            "created_at": now,
            "status": "active",
            "last_user_activity": now,
            "inactive_days": 0,
            "total_users": 0,
            "suspend_count": 0,
            "owner_id": owner_str,
            "registered_at": now,
            "registered_by": owner_str,
            "notification_settings": {
                "alert_after_days": config.INACTIVE_DAYS_ALERT,
                "auto_suspend_after_days": config.AUTO_SUSPEND_DAYS,
                "last_alert_sent": None,
            },
        }

        # MongoDB לא מאפשר את אותו שדה גם ב-$set וגם ב-$setOnInsert בזמן upsert שגורם ל-insert.
        # אם force_owner_update פעיל, owner_id יגיע דרך $set (שמיושם גם ב-insert), ולכן נסיר אותו מ-$setOnInsert.
        if force_owner_update or claim_owner_if_unowned:
            set_on_insert.pop("owner_id", None)

        # Atomic guard for "claim unowned": only set owner if still unowned (prevents last-writer-wins race)
        filter_doc: dict = {"_id": service_id}
        if claim_owner_if_unowned:
            filter_doc = {
                "_id": service_id,
                "$or": [
                    {"owner_id": {"$exists": False}},
                    {"owner_id": None},
                    {"owner_id": ""},
                ],
            }

        # חשוב: כשמבצעים claim_owner_if_unowned אנחנו לא רוצים upsert.
        # אם הפילטר לא מתאים בגלל שמישהו כבר תפס בעלות, upsert היה מנסה לבצע insert עם אותו _id וגורם ל-DuplicateKeyError.
        upsert_flag = False if claim_owner_if_unowned else True

        return self.services.update_one(filter_doc, {"$set": update_set, "$setOnInsert": set_on_insert}, upsert=upsert_flag)

    def remove_service_from_management(self, service_id: str, user_id: str) -> bool:
        """מסמן שירות כהוסר מהמערכת (ללא מחיקה מ-Render).

        בפועל: מסתיר אותו מהרשימות (removed=true) ומכבה ניטורים/התראות הקשורים אליו.
        אינו מוחק היסטוריה מקולקציות אחרות.
        """
        now = datetime.now(timezone.utc)
        result = self.services.update_one(
            {"_id": service_id},
            {
                "$set": {
                    "removed": True,
                    "removed_at": now,
                    "removed_by": str(user_id),
                    "updated_at": now,
                    # כיבוי ניטורים/התראות
                    "status_monitoring.enabled": False,
                    "log_monitoring.enabled": False,
                    "deploy_notifications_enabled": False,
                }
            },
            upsert=False,
        )
        return bool(result.matched_count)

    def ensure_services_exist(self, service_ids: Iterable[str], *, owner_id: Optional[str] = None):
        """ודא שמספר שירותים קיימים במסד (ללא עדכון מסמכים קיימים)."""
        count = 0
        for sid in service_ids:
            if not sid:
                continue
            try:
                self.ensure_service_exists(str(sid), owner_id=owner_id, service_name=str(sid))
                count += 1
            except Exception:
                # נשתיק כדי לא להפיל את האפליקציה בזמן אתחול
                continue
        return count

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
        return list(self.services.find({"status_monitoring.enabled": True, "removed": {"$ne": True}}))

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
                {"status_monitoring.enabled": True, "removed": {"$ne": True}},
                {"_id": 1, "service_name": 1, "last_known_status": 1, "status_monitoring": 1},
            )
        )

    def get_services_with_deploy_notifications_enabled(self):
        """החזרת שירותים שעבורם התראות דיפלוי מופעלות (גם אם ניטור סטטוס כבוי)"""
        return list(
            self.services.find(
                {"deploy_notifications_enabled": True, "removed": {"$ne": True}},
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

    def delete_service(self, service_id: str) -> dict:
        """מחיקת שירות וכל הרשומות הקשורות אליו מהמערכת.

        מוחק את המסמך הראשי מ-`service_activity` ואת ההיסטוריות/אינטראקציות הקשורות:
        `user_interactions`, `manual_actions`, `status_changes`, `deploy_events`.

        מחזיר ספירת מחיקות לכל קולקציה.
        """
        services_del = self.services.delete_one({"_id": service_id}).deleted_count
        interactions_del = self.user_interactions.delete_many({"service_id": service_id}).deleted_count
        manual_del = self.manual_actions.delete_many({"service_id": service_id}).deleted_count
        status_changes_del = self.status_changes.delete_many({"service_id": service_id}).deleted_count
        deploy_events_del = self.deploy_events.delete_many({"service_id": service_id}).deleted_count

        return {
            "services": services_del,
            "user_interactions": interactions_del,
            "manual_actions": manual_del,
            "status_changes": status_changes_del,
            "deploy_events": deploy_events_del,
        }

    def toggle_deploy_notifications(self, service_id: str, enabled: bool):
        """הפעלה/כיבוי התראות דיפלוי לשירות ספציפי"""
        # ודא שהמסמך קיים כדי ששירותים שלא נרשמו מראש לא יידלגו בסריקה
        return self.services.update_one(
            {"_id": service_id},
            {"$set": {"deploy_notifications_enabled": enabled}, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

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

    # ===== ניטור לוגים =====
    
    def enable_log_monitoring(
        self,
        service_id: str,
        user_id: int,
        service_name: Optional[str] = None,
        error_threshold: int = 5,
    ):
        """הפעלת ניטור לוגים לשירות"""
        update_data = {
            "log_monitoring.enabled": True,
            "log_monitoring.enabled_by": user_id,
            "log_monitoring.enabled_at": datetime.now(timezone.utc),
            "log_monitoring.error_threshold": error_threshold,
        }

        if service_name:
            update_data["service_name"] = service_name

        return self.services.update_one(
            {"_id": service_id}, 
            {"$set": update_data, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}}, 
            upsert=True
        )

    def disable_log_monitoring(self, service_id: str, user_id: int):
        """כיבוי ניטור לוגים לשירות"""
        return self.services.update_one(
            {"_id": service_id},
            {
                "$set": {
                    "log_monitoring.enabled": False,
                    "log_monitoring.disabled_by": user_id,
                    "log_monitoring.disabled_at": datetime.now(timezone.utc),
                }
            },
        )

    def get_log_monitored_services(self):
        """קבלת רשימת שירותים עם ניטור לוגים פעיל"""
        return list(self.services.find({"log_monitoring.enabled": True, "removed": {"$ne": True}}))

    def get_log_monitoring_settings(self, service_id: str) -> dict:
        """קבלת הגדרות ניטור לוגים של שירות"""
        service = self.services.find_one({"_id": service_id})
        if not service:
            return {"error_threshold": 5}
        
        log_monitoring = service.get("log_monitoring", {})
        return {
            "enabled": log_monitoring.get("enabled", False),
            "error_threshold": log_monitoring.get("error_threshold", 5),
            "enabled_by": log_monitoring.get("enabled_by"),
            "enabled_at": log_monitoring.get("enabled_at"),
        }

    def record_log_error(self, service_id: str, error_count: int, is_critical: bool):
        """רישום שגיאות לוג שזוהו"""
        return self.services.update_one(
            {"_id": service_id},
            {
                "$set": {
                    "log_monitoring.last_error_count": error_count,
                    "log_monitoring.last_error_time": datetime.now(timezone.utc),
                    "log_monitoring.last_was_critical": is_critical,
                    "log_monitoring.last_checked": datetime.now(timezone.utc),
                },
                "$inc": {
                    "log_monitoring.total_errors": error_count,
                    "log_monitoring.total_critical_errors": 1 if is_critical else 0,
                }
            },
        )

    def update_log_threshold(self, service_id: str, error_threshold: int):
        """עדכון סף שגיאות לניטור לוגים"""
        return self.services.update_one(
            {"_id": service_id},
            {"$set": {"log_monitoring.error_threshold": error_threshold}}
        )

    # ===== תזכורות =====

    def create_reminder(self, user_id: int, text: str, remind_at: datetime, chat_id: int) -> str:
        """יצירת תזכורת חדשה. מחזיר את ה-ID של התזכורת."""
        now = datetime.now(timezone.utc)
        result = self.reminders.insert_one({
            "user_id": user_id,
            "chat_id": chat_id,
            "text": text,
            "remind_at": remind_at,
            "created_at": now,
            "sent": False,
        })
        return str(result.inserted_id)

    def get_pending_reminders(self) -> list:
        """קבלת תזכורות שהגיע זמנן ועדיין לא נשלחו"""
        now = datetime.now(timezone.utc)
        return list(self.reminders.find({
            "remind_at": {"$lte": now},
            "sent": False,
        }))

    def mark_reminder_sent(self, reminder_id) -> bool:
        """סימון תזכורת כנשלחה"""
        from bson import ObjectId
        if not isinstance(reminder_id, ObjectId):
            reminder_id = ObjectId(reminder_id)
        result = self.reminders.update_one(
            {"_id": reminder_id},
            {"$set": {"sent": True, "sent_at": datetime.now(timezone.utc)}}
        )
        return bool(result.modified_count)

    def get_user_reminders(self, user_id: int) -> list:
        """קבלת כל התזכורות הפעילות של משתמש"""
        return list(self.reminders.find({
            "user_id": user_id,
            "sent": False,
        }).sort("remind_at", 1))

    def delete_reminder(self, reminder_id, user_id: int) -> bool:
        """מחיקת תזכורת (רק אם שייכת למשתמש)"""
        from bson import ObjectId
        if not isinstance(reminder_id, ObjectId):
            reminder_id = ObjectId(reminder_id)
        result = self.reminders.delete_one({
            "_id": reminder_id,
            "user_id": user_id,
        })
        return bool(result.deleted_count)

    def increment_reminder_attempts(self, reminder_id) -> bool:
        """העלאת מונה נסיונות שליחה של תזכורת"""
        from bson import ObjectId
        if not isinstance(reminder_id, ObjectId):
            reminder_id = ObjectId(reminder_id)
        result = self.reminders.update_one(
            {"_id": reminder_id},
            {"$inc": {"send_attempts": 1}}
        )
        return bool(result.modified_count)


# יצירת instance גלובלי
db = Database()
