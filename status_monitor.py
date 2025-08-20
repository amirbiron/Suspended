import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import config
from database import db
from notifications import send_deploy_event_notification, send_status_change_notification
from render_api import render_api

logger = logging.getLogger(__name__)


class StatusMonitor:
    """מנטר את הסטטוס של הבוטים ושולח התראות על שינויים"""

    def __init__(self):
        self.monitoring_enabled = {}
        self.last_known_status = {}
        self.manual_action_cache = set()
        self.cache_duration = 300
        self.check_interval = config.STATUS_CHECK_INTERVAL_SECONDS
        self.monitoring_thread = None
        self.stop_monitoring = threading.Event()
        # New: faster polling while a deployment is active
        self.deploy_check_interval = getattr(config, "DEPLOY_CHECK_INTERVAL_SECONDS", 30)
        self.deploying_active = False
        # זיהוי דיפלויים שהסתיימו גם אם החמצנו את מצב "deploying"
        self.last_checked_deploy_ids = {}

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
        logger.info("Checking status of services (status + deploy alerts)")

        # קבלת רשימת השירותים לניטור מהדאטאבייס
        monitored_services = db.get_status_monitored_services()
        # בנוסף: שירותים עם התראות דיפלוי מופעלות גם אם ניטור סטטוס כבוי
        try:
            deploy_notif_services = db.get_services_with_deploy_notifications_enabled()
        except Exception:
            deploy_notif_services = []
        logger.info(
            "Fetched services: status_monitored=%d, deploy_notif_enabled=%d",
            len(monitored_services),
            len(deploy_notif_services),
        )

        # מיזוג ייחודי לפי service_id
        all_relevant_services = {}
        for s in monitored_services:
            all_relevant_services[s["_id"]] = s
        for s in deploy_notif_services:
            all_relevant_services.setdefault(s["_id"], s)
        services_to_check = list(all_relevant_services.values())

        # Fallback: אם אין כלום ב-DB – נשתמש ברשימת config כדי לפחות לבדוק אירועי דיפלוי
        if not services_to_check and getattr(config, "SERVICES_TO_MONITOR", []):
            logger.warning(
                "No services found in DB; using fallback from config.SERVICES_TO_MONITOR (deploy checks only)"
            )
            services_to_check = [{"_id": sid, "service_name": sid} for sid in config.SERVICES_TO_MONITOR]

        any_deploying = False
        for service_doc in services_to_check:
            service_id = service_doc["_id"]

            # דילוג על שירותים שלא מופעל עבורם ניטור
            # אם ניטור סטטוס כבוי, עדיין נבדוק רק אירועי דיפלוי אם התראות דיפלוי מופעלות
            status_monitoring_enabled = service_doc.get("status_monitoring", {}).get("enabled", False)
            deploy_notif_enabled = db.get_deploy_notification_status(service_id)
            if not status_monitoring_enabled and not deploy_notif_enabled:
                continue

            # בדיקה אם השירות עבר פעולה ידנית לאחרונה
            manual_skip = self._is_manual_action_recent(service_id)
            if manual_skip:
                logger.debug(
                    (
                        f"Recent manual action for {service_id} - will skip "
                        f"status-change notifications but still check deploy events"
                    )
                )

            try:
                # קבלת הסטטוס הנוכחי מ-Render
                current_status = render_api.get_service_status(service_id)

                if current_status:
                    # בדיקה האם יש שירות כלשהו במצב פריסה כדי להאיץ בדיקות
                    simplified_for_flag = self._simplify_status(current_status)
                    if simplified_for_flag == "deploying":
                        any_deploying = True

                    if status_monitoring_enabled and not manual_skip:
                        self._process_status_change(service_id, current_status, service_doc)
                    elif deploy_notif_enabled:
                        # גם אם ניטור סטטוס כבוי, נטפל במעבר deploy->(online/offline) לשם התראת דיפלוי
                        self._process_deploy_transition_for_notif(service_id, current_status, service_doc)
                else:
                    logger.warning(f"Could not get status for service {service_id}")

                # בדיקת דיפלוי שהסתיים: אם התראות דיפלוי מופעלות
                if deploy_notif_enabled:
                    self._check_deploy_events(service_id, service_doc)

            except Exception as e:
                logger.error(f"Error checking status for {service_id}: {e}")

        # עדכון דגל פריסה פעילה עבור קצב הבדיקה
        # אם הופעלו התראות דיפלוי לשירותים כלשהם – נשתמש בקצב המהיר כדי לקטוף אירועי סיום מהר יותר
        self.deploying_active = any_deploying or bool(deploy_notif_services)

    def _process_deploy_transition_for_notif(self, service_id: str, current_status: str, service_doc: dict):
        """שליחת התראת סיום דיפלוי גם כאשר ניטור סטטוס כבוי, אם דגל התראות דיפלוי מופעל.

        מטרה: לכסות מקרים של Resume/Start שלא מייצרים Deploy Event, אך כן עוברים דרך
        'deploying' במצב החי של השירות.
        """
        service_name = service_doc.get("service_name", service_id)
        last_status = service_doc.get("last_known_status")

        new_simple = self._simplify_status(current_status)
        if last_status is None:
            db.update_service_status(service_id, new_simple)
            return

        old_simple = self._simplify_status(last_status)
        if old_simple != new_simple and old_simple == "deploying" and new_simple in {"online", "offline"}:
            self._send_status_notification(service_id, service_name, old_simple, new_simple)

        # עדכון הסטטוס במסד הנתונים כדי שנוכל לזהות מעברים בהמשך
        db.update_service_status(service_id, new_simple)

    def _check_deploy_events(self, service_id: str, service_doc: dict):
        """בודק אם יש דיפלוי חדש שהסתיים ושולח התראה פעם אחת"""
        try:
            logger.info("Checking latest deploy for service %s", service_id)
            info = render_api.get_latest_deploy_info(service_id)
            if not info:
                logger.debug("No deploy info returned for %s", service_id)
                return
            deploy_id = info.get("id")
            status = (info.get("status") or "").lower()
            if not deploy_id:
                logger.debug("Latest deploy has no id for %s: %s", service_id, info)
                return

            # האם כבר דווח?
            last_reported = db.get_last_reported_deploy_id(service_id)
            if last_reported == deploy_id:
                return

            # נשלח התראה רק אם הסטטוס מסמן סוף (success/failure)
            # תמיכה במגוון מצבים סופיים שה-API עשוי להחזיר, ובנוסף שימוש במיפוי הפשוט שלנו
            terminal_statuses = {
                "succeeded",
                "success",
                "completed",
                "complete",
                "finished",
                "deployed",
                "live",
                "failed",
                "error",
                "canceled",
                "cancelled",
                "aborted",
            }

            simplified = self._simplify_status(status)
            if status in terminal_statuses or simplified in {"online", "offline"}:
                logger.info(
                    "Terminal deploy detected for %s: id=%s, status=%s (simplified=%s)",
                    service_id,
                    deploy_id,
                    status,
                    simplified,
                )
                service_name = service_doc.get("service_name", service_id)
                commit_message = info.get("commitMessage")
                sent = send_deploy_event_notification(service_name, service_id, status, commit_message)
                if sent:
                    logger.info("Deploy notification sent for %s (deploy_id=%s)", service_id, deploy_id)
                    db.record_reported_deploy(service_id, deploy_id, status)
                else:
                    logger.warning(
                        "Deploy notification failed to send for %s (deploy_id=%s, status=%s); will retry next cycle",
                        service_id,
                        deploy_id,
                        status,
                    )
        except Exception as e:
            logger.error(f"Error while checking deploy events for {service_id}: {e}")

    def watch_deploy_until_terminal(self, service_id: str, service_name: Optional[str] = None, max_minutes: int = 30):
        """מעקב אקטיבי אחר דיפלוי עד לסיום ושליחת התראה פעם אחת (חיזוק לוגיקת המוניטור).

        רץ ברקע ולא חוסם. מונע כפילויות באמצעות ה-DB.
        """
        def runner():
            try:
                deadline = datetime.now(timezone.utc) + timedelta(minutes=max_minutes)
            except Exception:
                # Fallback ל-deadline נאיבי
                deadline = datetime.now() + timedelta(minutes=max_minutes)

            already_reported = db.get_last_reported_deploy_id(service_id)

            while True:
                now = datetime.now(timezone.utc)
                if now > deadline:
                    logger.info(f"Stopping deploy watch for {service_id}: deadline reached")
                    break
                try:
                    info = render_api.get_latest_deploy_info(service_id)
                    if not info:
                        time.sleep(self.deploy_check_interval)
                        continue
                    deploy_id = info.get("id")
                    status = (info.get("status") or "").lower()
                    if already_reported and deploy_id == already_reported:
                        # כבר דווח
                        break

                    simplified = self._simplify_status(status)
                    terminal_statuses = {
                        "succeeded",
                        "success",
                        "completed",
                        "complete",
                        "finished",
                        "deployed",
                        "live",
                        "failed",
                        "error",
                        "canceled",
                        "cancelled",
                        "aborted",
                    }
                    if deploy_id and (status in terminal_statuses or simplified in {"online", "offline"}):
                        name = service_name
                        if not name:
                            doc = db.get_service_activity(service_id) or {}
                            name = doc.get("service_name", service_id)
                        sent = send_deploy_event_notification(name, service_id, status, info.get("commitMessage"))
                        if sent:
                            db.record_reported_deploy(service_id, deploy_id, status)
                        break
                except Exception as e:
                    logger.error(f"Error in deploy watch for {service_id}: {e}")
                time.sleep(self.deploy_check_interval)

        threading.Thread(target=runner, daemon=True).start()

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
                    simplified_status,
                )

            # עדכון הסטטוס במסד הנתונים
            db.update_service_status(service_id, simplified_status)

    def _simplify_status(self, status: str) -> str:
        """המרת סטטוס Render לסטטוסים פשוטים: online/offline/deploying/unknown"""
        # status is typed str; keep guard for robustness
        if not isinstance(status, str) or status == "":
            return "unknown"

        status_lower = status.lower()

        # Online indicators
        if status_lower in [
            "running",
            "deployed",
            "active",
            "healthy",
            "succeeded",
            "success",
            "completed",
            "complete",
            "finished",
        ] or any(k in status_lower for k in ["live", "ready", "ok", "available"]):
            return "online"

        # Offline indicators
        if status_lower in [
            "suspended",
            "stopped",
            "failed",
            "error",
            "crashed",
            "canceled",
            "cancelled",
            "aborted",
        ] or any(k in status_lower for k in ["unhealthy", "inactive", "down"]):
            return "offline"

        # Deploying/building/starting indicators
        if (
            status_lower in ["deploying", "building", "starting", "restarting"]
            or any(
                k in status_lower
                for k in [
                    "deploy_in_progress",
                    "build_in_progress",
                    "update_in_progress",
                    "progress",
                    "provision",
                    "initializ",
                    "pending",
                    "queue",
                    "updat",
                ]
            )
            or "deploy" in status_lower
            or "build" in status_lower
            or "start" in status_lower
        ):
            return "deploying"

        return "unknown"

    def _is_significant_change(self, old_status: str, new_status: str, service_id: Optional[str] = None) -> bool:
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
                significant_changes.extend(
                    [
                        ("deploying", "online"),
                        ("deploying", "offline"),
                    ]
                )

        return (old_status, new_status) in significant_changes

    def _send_status_notification(self, service_id: str, service_name: str, old_status: str, new_status: str):
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
                # חריג: לא לדכא התראת סיום דיפלוי (deploying -> online/offline)
                if time_since_action.total_seconds() < self.cache_duration:
                    if not (old_status == "deploying" and new_status in {"online", "offline"}):
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
            action=action,
        )

    def _is_manual_action_recent(self, service_id: str) -> bool:
        """בדיקה אם הייתה פעולה ידנית לאחרונה"""
        return service_id in self.manual_action_cache

    def mark_manual_action(self, service_id: str):
        """סימון שבוצעה פעולה ידנית על שירות"""
        self.manual_action_cache.add(service_id)
        # הסרה אוטומטית מהקאש אחרי הזמן שהוגדר
        threading.Timer(self.cache_duration, lambda: self.manual_action_cache.discard(service_id)).start()

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
            status_value = service_info.get("status")
            current_status = self._simplify_status(status_value if isinstance(status_value, str) else "unknown")

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
            "enabled_at": monitoring_info.get("enabled_at"),
        }

    def get_all_monitored_services(self) -> List[dict]:
        """קבלת רשימת כל השירותים המנוטרים"""
        services = db.get_status_monitored_services()
        return list(services)  # narrow type for mypy


# יצירת instance גלובלי
status_monitor = StatusMonitor()
