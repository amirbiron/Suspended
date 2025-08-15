from datetime import datetime, timezone
from typing import Optional

from database import db
from render_api import render_api
from notifications import send_notification


class UptimeMonitor:
    def __init__(self):
        self.down_statuses = {"suspended", "failed", "degraded", "inactive"}
        self.up_statuses = {"active", "live", "ok", "running"}

    def _normalize_status(self, status: Optional[str]) -> Optional[str]:
        if status is None:
            return None
        return str(status).strip().lower()

    def _compose_transition_message(self, service: dict, old_status: Optional[str], new_status: Optional[str]) -> str:
        service_id = service["_id"]
        service_name = service.get("service_name", service_id)
        old = old_status or "unknown"
        new = new_status or "unknown"
        if new in self.down_statuses:
            prefix = "🟥 ירידה בזמינות"
        elif new in self.up_statuses:
            prefix = "🟩 חזרה לזמינות"
        else:
            prefix = "ℹ️ שינוי סטטוס"
        message = f"{prefix}\n"
        message += f"שירות: {service_name}\n"
        message += f"ID: {service_id}\n"
        message += f"סטטוס קודם: {old}\n"
        message += f"סטטוס נוכחי: {new}"
        return message

    def check_services(self):
        monitored = db.get_monitored_services()
        if not monitored:
            return
        for service in monitored:
            service_id = service["_id"]
            try:
                current_status = render_api.get_service_status(service_id)
                current_status = self._normalize_status(current_status)
            except Exception as e:
                # אם לא הצלחנו לקבל סטטוס, נשלח התראה רק אם זה שינוי מ"ידוע"
                current_status = None
            previous_status = self._normalize_status(service.get("last_known_status"))

            print(f"[uptime] service={service_id} prev={previous_status} curr={current_status}")

            # אם אין סטטוס ידוע קודם, נשמור ונמשיך בלי התראה (למידה ראשונית)
            if previous_status is None:
                if current_status is not None:
                    db.update_last_known_status(service_id, current_status)
                    print(f"[uptime] baseline learned for {service_id}: {current_status}")
                continue

            # שינוי סטטוס? נתריע ונעדכן
            if current_status != previous_status and current_status is not None:
                message = self._compose_transition_message(service, previous_status, current_status)
                send_notification(message)
                db.update_last_known_status(service_id, current_status)
                print(f"[uptime] transition detected for {service_id}: {previous_status} -> {current_status} (notification sent)")
            elif current_status is not None and previous_status is None:
                db.update_last_known_status(service_id, current_status)


uptime_monitor = UptimeMonitor()