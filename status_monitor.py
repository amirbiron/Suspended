from datetime import datetime, timezone, timedelta
import asyncio
import threading
import time
from typing import Dict, List, Optional, Set
from database import db
from render_api import render_api
from notifications import send_status_change_notification
import config
import logging

logger = logging.getLogger(__name__)

class StatusMonitor:
    """מנטר את הסטטוס של הבוטים ושולח התראות על שינויים"""
    
    def __init__(self):
        self.monitoring_enabled = {}  # Dict של service_id -> bool להפעלה/כיבוי ניטור
        self.last_known_status = {}  # Dict של service_id -> status
        self.manual_action_cache = set()  # Set של service_ids שעברו פעולה ידנית לאחרונה
        self.cache_duration = 300  # 5 דקות - זמן להתעלם משינויים אחרי פעולה ידנית
        self.check_interval = config.STATUS_CHECK_INTERVAL_SECONDS
        self.monitoring_thread = None
        self.stop_monitoring = threading.Event()
        # New: faster polling while a deployment is active
        self.deploy_check_interval = getattr(config, "DEPLOY_CHECK_INTERVAL_SECONDS", 30)
        self.deploying_active = False
        
    def start_monitoring(self):
        """הפעלת ניטור הסטטוס ברקע"""
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            logger.info("Status monitoring already running")
            return
            
        self.stop_monitoring.clear()
        self.monitoring_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitoring_thread.start()
        logger.info("Status monitoring started")
        
    def stop_monitoring_thread(self):
        """עצירת ניטור הסטטוס"""
        self.stop_monitoring.set()
        if self.monitoring_thread:
            self.monitoring_thread.join(timeout=5)
        logger.info("Status monitoring stopped")
        
    def _monitor_loop(self):
        """לולאת הניטור הראשית"""
        while not self.stop_monitoring.is_set():
            try:
                self.check_all_services()
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
            
            # המתנה עם אפשרות לעצירה מיידית
            sleep_seconds = self.deploy_check_interval if self.deploying_active else self.check_interval
            self.stop_monitoring.wait(sleep_seconds)
    
    def check_all_services(self):
        """בדיקת הסטטוס של כל השירותים המנוטרים"""
        logger.debug("Checking status of all monitored services")
        
        # קבלת רשימת השירותים לניטור מהדאטאבייס
        monitored_services = db.get_status_monitored_services()
        
        any_deploying = False
        for service_doc in monitored_services:
            service_id = service_doc["_id"]
            
            # דילוג על שירותים שלא מופעל עבורם ניטור
            if not service_doc.get("status_monitoring", {}).get("enabled", False):
                continue
            
            # בדיקה אם השירות עבר פעולה ידנית לאחרונה
            if self._is_manual_action_recent(service_id):
                logger.debug(f"Skipping {service_id} - recent manual action")
                continue
            
            try:
                # קבלת הסטטוס הנוכחי מ-Render
                current_status = render_api.get_service_status(service_id)
                
                if current_status:
                    # בדיקה האם יש שירות כלשהו במצב פריסה כדי להאיץ בדיקות
                    simplified_for_flag = self._simplify_status(current_status)
                    if simplified_for_flag == "deploying":
                        any_deploying = True
                    
                    self._process_status_change(service_id, current_status, service_doc)
                else:
                    logger.warning(f"Could not get status for service {service_id}")
                    
            except Exception as e:
                logger.error(f"Error checking status for {service_id}: {e}")
        
        # עדכון דגל פריסה פעילה עבור קצב הבדיקה
        self.deploying_active = any_deploying
    
    def _process_status_change(self, service_id: str, current_status: str, service_doc: dict):
        """עיבוד שינוי סטטוס"""
        service_name = service_doc.get("service_name", service_id)
        last_status = service_doc.get("last_known_status")
        
        # מיפוי סטטוסים של Render לסטטוסים פשוטים
        simplified_status = self._simplify_status(current_status)
        
        # אם זו הפעם הראשונה שבודקים את השירות
        if last_status is None:
            db.update_service_status(service_id, simplified_status)
            logger.info(f"Initial status for {service_name}: {simplified_status}")
            return
            
        last_simplified = self._simplify_status(last_status)
        
        # בדיקה אם יש שינוי משמעותי בסטטוס
        if simplified_status != last_simplified:
            # בדיקה אם זה שינוי שמעניין את המשתמש
            if self._is_significant_change(last_simplified, simplified_status, service_id):
                # שליחת התראה
                self._send_status_notification(
                    service_id, 
                    service_name, 
                    last_simplified, 
                    simplified_status
                )
            
            # עדכון הסטטוס במסד הנתונים
            db.update_service_status(service_id, simplified_status)
            
    def _simplify_status(self, status: str) -> str:
        """המרת סטטוס Render לסטטוסים פשוטים: online/offline/deploying/unknown"""
        if status is None:
            return "unknown"
        
        status_lower = status.lower()
        
        # Online indicators
        if (
            status_lower in ["running", "deployed", "active", "healthy", "succeeded", "success", "completed", "complete", "finished"]
            or any(k in status_lower for k in ["live", "ready", "ok", "available"])
        ):
            return "online"
        
        # Offline indicators
        if (
            status_lower in ["suspended", "stopped", "failed", "error", "crashed", "canceled", "cancelled", "aborted"]
            or any(k in status_lower for k in ["unhealthy", "inactive", "down"])
        ):
            return "offline"
        
        # Deploying/building/starting indicators
        if (
            status_lower in ["deploying", "building", "starting", "restarting"]
            or any(k in status_lower for k in [
                "deploy_in_progress", "build_in_progress", "update_in_progress",
                "progress", "provision", "initializ", "pending", "queue", "updat"
            ])
            or "deploy" in status_lower or "build" in status_lower or "start" in status_lower
        ):
            return "deploying"
        
        return "unknown"
    
    def _is_significant_change(self, old_status: str, new_status: str, service_id: str = None) -> bool:
        """בדיקה אם השינוי משמעותי ודורש התראה"""
        # שינויים משמעותיים: online <-> offline
        significant_changes = [
            ("online", "offline"),
            ("offline", "online"),
        ]
        
        # הוספת התראות על סיום דיפלוי אם מופעל עבור השירות הספציפי
        if service_id:
            deploy_notifications_enabled = db.get_deploy_notification_status(service_id)
            if deploy_notifications_enabled:
                significant_changes.extend([
                    ("deploying", "online"),  # סיום פריסה מוצלח
                    ("deploying", "offline"),  # כשלון בפריסה
                ])
        
        return (old_status, new_status) in significant_changes
    
    def _send_status_notification(self, service_id: str, service_name: str, 
                                 old_status: str, new_status: str):
        """שליחת התראה על שינוי סטטוס"""
        # בדיקה אם זה לא בגלל פעולה ידנית שלנו
        last_action = db.get_last_manual_action(service_id)
        
        if last_action:
            action_time = last_action.get("timestamp")
            if action_time:
                if action_time.tzinfo is None:
                    action_time = action_time.replace(tzinfo=timezone.utc)
                    
                time_since_action = datetime.now(timezone.utc) - action_time
                
                # אם הפעולה הידנית האחרונה הייתה בדקות האחרונות, לא שולחים התראה
                if time_since_action.total_seconds() < self.cache_duration:
                    logger.info(f"Skipping notification for {service_name} - recent manual action")
                    return
        
        # יצירת אימוג'י וטקסט פעולה מתאימים
        if old_status == "deploying" and new_status == "online":
            emoji = "🚀"
            action = "סיום פריסה מוצלח"
        elif old_status == "deploying" and new_status == "offline":
            emoji = "⚠️"
            action = "כשלון בפריסה"
        elif new_status == "online":
            emoji = "🟢"
            action = "עלה"
        elif new_status == "offline":
            emoji = "🔴"
            action = "ירד"
        else:
            emoji = "🟡"
            action = f"שינה סטטוס ל-{new_status}"
        
        # שליחת ההתראה
        send_status_change_notification(
            service_id=service_id,
            service_name=service_name,
            old_status=old_status,
            new_status=new_status,
            emoji=emoji,
            action=action
        )
    
    def _is_manual_action_recent(self, service_id: str) -> bool:
        """בדיקה אם הייתה פעולה ידנית לאחרונה"""
        return service_id in self.manual_action_cache
    
    def mark_manual_action(self, service_id: str):
        """סימון שבוצעה פעולה ידנית על שירות"""
        self.manual_action_cache.add(service_id)
        # הסרה אוטומטית מהקאש אחרי הזמן שהוגדר
        threading.Timer(
            self.cache_duration, 
            lambda: self.manual_action_cache.discard(service_id)
        ).start()
        
        # רישום במסד הנתונים
        db.record_manual_action(service_id)
    
    def enable_monitoring(self, service_id: str, user_id: int) -> bool:
        """הפעלת ניטור סטטוס לשירות מסוים"""
        try:
            # קבלת מידע על השירות
            service_info = render_api.get_service_info(service_id)
            if not service_info:
                return False
                
            service_name = service_info.get("name", service_id)
            current_status = self._simplify_status(service_info.get("status"))
            
            # עדכון במסד הנתונים
            db.enable_status_monitoring(service_id, user_id, service_name, current_status)
            
            # עדכון במטמון המקומי
            self.monitoring_enabled[service_id] = True
            self.last_known_status[service_id] = current_status
            
            logger.info(f"Status monitoring enabled for {service_name} by user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error enabling monitoring for {service_id}: {e}")
            return False
    
    def disable_monitoring(self, service_id: str, user_id: int) -> bool:
        """כיבוי ניטור סטטוס לשירות מסוים"""
        try:
            # עדכון במסד הנתונים
            db.disable_status_monitoring(service_id, user_id)
            
            # עדכון במטמון המקומי
            self.monitoring_enabled[service_id] = False
            
            logger.info(f"Status monitoring disabled for {service_id} by user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error disabling monitoring for {service_id}: {e}")
            return False
    
    def get_monitoring_status(self, service_id: str) -> dict:
        """קבלת סטטוס הניטור של שירות"""
        service = db.get_service_activity(service_id)
        if not service:
            return {"enabled": False, "service_exists": False}
            
        monitoring_info = service.get("status_monitoring", {})
        return {
            "enabled": monitoring_info.get("enabled", False),
            "service_exists": True,
            "last_status": service.get("last_known_status"),
            "enabled_by": monitoring_info.get("enabled_by"),
            "enabled_at": monitoring_info.get("enabled_at")
        }
    
    def get_all_monitored_services(self) -> List[dict]:
        """קבלת רשימת כל השירותים המנוטרים"""
        return db.get_status_monitored_services()

# יצירת instance גלובלי
status_monitor = StatusMonitor()