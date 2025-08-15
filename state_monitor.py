from datetime import datetime, timezone, timedelta
from typing import Optional

import config
from render_api import render_api
from database import db
from notifications import send_notification, format_down_alert, format_up_alert


class StateMonitor:
    def __init__(self):
        self.down_statuses = set(config.RENDER_DOWN_STATUSES)
        self.transient_statuses = set(config.RENDER_TRANSIENT_STATUSES)
        self.suppress_after_our_action_minutes = config.ALERT_SUPPRESSION_MINUTES_AFTER_OUR_ACTION
        self.deploy_suppression_minutes = config.DEPLOY_SUPPRESSION_MINUTES

    def _is_down(self, status: Optional[str]) -> bool:
        if not status:
            return False
        return status in self.down_statuses

    def _is_transient(self, status: Optional[str]) -> bool:
        if not status:
            return False
        return status in self.transient_statuses

    def _should_suppress_due_to_our_action(self, service_doc: dict) -> bool:
        last_action_at = service_doc.get("last_our_action_at")
        if not last_action_at:
            return False
        if last_action_at.tzinfo is None:
            last_action_at = last_action_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_action_at) <= timedelta(minutes=self.suppress_after_our_action_minutes)

    def _should_suppress_due_to_deploy(self, service_doc: dict) -> bool:
        last_transient = service_doc.get("last_transient_status_at")
        if not last_transient:
            return False
        if last_transient.tzinfo is None:
            last_transient = last_transient.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_transient) <= timedelta(minutes=self.deploy_suppression_minutes)

    def _maybe_notify(self, service_id: str, service_name: str, old_status: Optional[str], new_status: Optional[str], service_doc: dict):
        was_down = self._is_down(old_status)
        is_down = self._is_down(new_status)

        if self._is_transient(new_status):
            # מסמן חלון דיפלוי/בניה כדי להשתיק התראות
            db.record_transient_status_seen(service_id)
            return

        if old_status is None:
            # אתחול ראשון - לא שולחים התראה כדי לא להציף
            return

        if was_down == is_down:
            # לא שינוי בכיוון (עדיין DOWN או עדיין UP) – לא מתריעים
            return

        if self._should_suppress_due_to_our_action(service_doc) or self._should_suppress_due_to_deploy(service_doc):
            return

        if is_down:
            message = format_down_alert(service_name, service_id, new_status or "unknown")
        else:
            message = format_up_alert(service_name, service_id, new_status or "unknown")
        send_notification(message)

    def check_services_state(self):
        """פולינג סטטוסי שירותים ב-Render ושליחת התראות על שינויי UP/DOWN"""
        for service_id in config.SERVICES_TO_MONITOR:
            info = render_api.get_service_info(service_id)
            if not info:
                continue
            current_status = info.get("status", "unknown")

            # קריאת המידע הקיים מה-DB
            service_doc = db.get_service_activity(service_id) or {}
            service_name = service_doc.get("service_name") or info.get("name") or service_id
            last_status = service_doc.get("render_status")

            # שליחת התראות לפי הצורך
            self._maybe_notify(service_id, service_name, last_status, current_status, service_doc)

            # עדכון סטטוס אחרון שנשמר
            db.update_render_status(service_id, current_status, service_name=service_name)


# יצירת instance גלובלי
state_monitor = StateMonitor()