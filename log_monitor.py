import logging
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set
from collections import defaultdict

import config
from database import db
from notifications import send_notification
from render_api import render_api

logger = logging.getLogger(__name__)


class LogMonitor:
    """מנטר לוגים של שירותים וזיהוי שגיאות"""

    def __init__(self):
        self.monitoring_thread = None
        self.stop_monitoring = threading.Event()
        self.check_interval = 60  # בדיקה כל דקה
        
        # קאש של לוגים שכבר נבדקו (למניעת התראות כפולות)
        self.seen_errors: Dict[str, Set[str]] = defaultdict(set)
        
        # Patterns לזיהוי שגיאות
        self.error_patterns = [
            r'(?i)\berror\b',
            r'(?i)\bexception\b',
            r'(?i)\bfailed\b',
            r'(?i)\bcrash\b',
            r'(?i)\bfatal\b',
            r'(?i)traceback',
            r'(?i)stack trace',
            r'\b[45]\d{2}\b',  # HTTP error codes (4xx, 5xx)
            r'(?i)uncaught',
            r'(?i)unhandled',
        ]
        
        # Patterns לזיהוי שגיאות קריטיות
        self.critical_patterns = [
            r'(?i)fatal',
            r'(?i)segmentation fault',
            r'(?i)out of memory',
            r'(?i)disk full',
            r'(?i)database.*(?:down|unreachable)',
            r'(?i)connection refused',
            r'(?i)timeout',
        ]
        
        # מילים שמסננות false positives
        self.ignore_patterns = [
            r'(?i)error:\s*0',  # error: 0 = no error
            r'(?i)no error',
            r'(?i)errorless',
        ]

    def start_monitoring(self):
        """הפעלת ניטור לוגים ברקע"""
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            logger.info("Log monitoring already running")
            return

        self.stop_monitoring.clear()
        self.monitoring_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitoring_thread.start()
        logger.info("Log monitoring started")

    def stop_monitoring_thread(self):
        """עצירת ניטור הלוגים"""
        self.stop_monitoring.set()
        if self.monitoring_thread:
            self.monitoring_thread.join(timeout=5)
        logger.info("Log monitoring stopped")

    def _monitor_loop(self):
        """לולאת הניטור הראשית"""
        while not self.stop_monitoring.is_set():
            try:
                self.check_all_services_logs()
            except Exception as e:
                logger.error(f"Error in log monitoring loop: {e}")

            # המתנה עם אפשרות לעצירה מיידית
            self.stop_monitoring.wait(self.check_interval)

    def check_all_services_logs(self):
        """בדיקת לוגים של כל השירותים עם ניטור מופעל"""
        logger.info("Checking logs for monitored services")
        
        # קבלת רשימת השירותים עם ניטור לוגים מופעל
        monitored_services = db.get_log_monitored_services()
        
        if not monitored_services:
            logger.debug("No services with log monitoring enabled")
            return
        
        for service in monitored_services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)
            
            try:
                self._check_service_logs(service_id, service_name)
            except Exception as e:
                logger.error(f"Error checking logs for {service_name}: {e}")

    def _check_service_logs(self, service_id: str, service_name: str):
        """בדיקת לוגים של שירות מסוים"""
        # קבלת הלוגים מה-5 דקות האחרונות
        logs = render_api.get_service_logs(service_id, tail=500)
        
        if not logs:
            logger.debug(f"No logs retrieved for {service_name}")
            return
        
        errors_found = []
        critical_errors = []
        
        for log_entry in logs:
            # כל log entry הוא dict עם timestamp, text, stream (stdout/stderr)
            log_text = log_entry.get("text", "")
            log_id = log_entry.get("id", "")
            
            # דילוג על לוגים שכבר ראינו
            if log_id and log_id in self.seen_errors[service_id]:
                continue
            
            # בדיקה אם זה false positive
            if self._is_false_positive(log_text):
                continue
            
            # בדיקה אם יש שגיאה
            if self._contains_error(log_text):
                error_info = {
                    "timestamp": log_entry.get("timestamp"),
                    "text": log_text,
                    "stream": log_entry.get("stream", "unknown"),
                }
                
                # סימון כראינו
                if log_id:
                    self.seen_errors[service_id].add(log_id)
                
                # בדיקה אם זו שגיאה קריטית
                if self._is_critical_error(log_text):
                    critical_errors.append(error_info)
                else:
                    errors_found.append(error_info)
        
        # ניקוי קאש ישן (שמור רק 1000 אחרונים)
        if len(self.seen_errors[service_id]) > 1000:
            old_ids = list(self.seen_errors[service_id])[:500]
            self.seen_errors[service_id] = set(list(self.seen_errors[service_id])[500:])
        
        # שליחת התראות
        if critical_errors:
            self._send_error_alert(service_id, service_name, critical_errors, is_critical=True)
        elif errors_found:
            # התראה רגילה רק אם יש הרבה שגיאות או אם הוגדר threshold נמוך
            service_settings = db.get_log_monitoring_settings(service_id)
            error_threshold = service_settings.get("error_threshold", 5)
            
            if len(errors_found) >= error_threshold:
                self._send_error_alert(service_id, service_name, errors_found, is_critical=False)

    def _contains_error(self, text: str) -> bool:
        """בדיקה אם הטקסט מכיל שגיאה"""
        for pattern in self.error_patterns:
            if re.search(pattern, text):
                return True
        return False

    def _is_critical_error(self, text: str) -> bool:
        """בדיקה אם זו שגיאה קריטית"""
        for pattern in self.critical_patterns:
            if re.search(pattern, text):
                return True
        return False

    def _is_false_positive(self, text: str) -> bool:
        """בדיקה אם זה false positive"""
        for pattern in self.ignore_patterns:
            if re.search(pattern, text):
                return True
        return False

    def _send_error_alert(self, service_id: str, service_name: str, errors: List[Dict], is_critical: bool):
        """שליחת התראה על שגיאות שזוהו"""
        emoji = "🔥" if is_critical else "⚠️"
        severity = "קריטית" if is_critical else "רגילה"
        
        message = f"{emoji} *התראת שגיאה {severity}*\n\n"
        message += f"🤖 שירות: *{service_name}*\n"
        message += f"🆔 ID: `{service_id}`\n"
        message += f"📊 זוהו {len(errors)} שגיאות\n\n"
        
        # הצגת עד 3 שגיאות ראשונות
        message += "*שגיאות אחרונות:*\n"
        for i, error in enumerate(errors[:3], 1):
            timestamp = error.get("timestamp", "")
            text = error.get("text", "")
            
            # קיצור הטקסט אם ארוך מדי
            if len(text) > 200:
                text = text[:200] + "..."
            
            # הסרת תווים מיוחדים שמפריעים ב-Markdown
            text = text.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
            
            message += f"\n{i}. ```\n{text}\n```"
        
        if len(errors) > 3:
            message += f"\n\n_ועוד {len(errors) - 3} שגיאות נוספות..._"
        
        message += f"\n\n💡 הקש `/logs {service_id}` לצפייה מלאה"
        
        # שליחת ההתראה
        send_notification(message)
        
        # רישום במסד הנתונים
        db.record_log_error(service_id, len(errors), is_critical)
        
        logger.info(f"Sent {'critical' if is_critical else 'regular'} error alert for {service_name}")

    def enable_monitoring(self, service_id: str, user_id: int, service_name: Optional[str] = None,
                         error_threshold: int = 5) -> bool:
        """הפעלת ניטור לוגים לשירות"""
        try:
            if not service_name:
                service_info = render_api.get_service_info(service_id)
                if not service_info:
                    return False
                service_name = service_info.get("name", service_id)
            
            # עדכון במסד הנתונים
            db.enable_log_monitoring(service_id, user_id, service_name, error_threshold)
            
            logger.info(f"Log monitoring enabled for {service_name} by user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error enabling log monitoring for {service_id}: {e}")
            return False

    def disable_monitoring(self, service_id: str, user_id: int) -> bool:
        """כיבוי ניטור לוגים לשירות"""
        try:
            db.disable_log_monitoring(service_id, user_id)
            
            # ניקוי קאש
            if service_id in self.seen_errors:
                del self.seen_errors[service_id]
            
            logger.info(f"Log monitoring disabled for {service_id} by user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error disabling log monitoring for {service_id}: {e}")
            return False

    def get_monitoring_status(self, service_id: str) -> dict:
        """קבלת סטטוס ניטור הלוגים של שירות"""
        service = db.get_service_activity(service_id)
        if not service:
            return {"enabled": False, "service_exists": False}
        
        log_monitoring = service.get("log_monitoring", {})
        return {
            "enabled": log_monitoring.get("enabled", False),
            "service_exists": True,
            "error_threshold": log_monitoring.get("error_threshold", 5),
            "enabled_by": log_monitoring.get("enabled_by"),
            "enabled_at": log_monitoring.get("enabled_at"),
            "last_error_count": log_monitoring.get("last_error_count", 0),
            "last_checked": log_monitoring.get("last_checked"),
        }

    def get_all_monitored_services(self) -> List[dict]:
        """קבלת רשימת כל השירותים עם ניטור לוגים"""
        return list(db.get_log_monitored_services())


# יצירת instance גלובלי
log_monitor = LogMonitor()
