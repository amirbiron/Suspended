import asyncio
import atexit
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import quote, unquote

import schedule
from pymongo.errors import DuplicateKeyError
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import config
from activity_tracker import activity_tracker
from database import db
from notifications import send_daily_report, send_startup_notification
from render_api import render_api
try:
    from status_monitor import status_monitor  # New import
except Exception:
    # Fallback: אם הייבוא נכשל (למשל בקומיט ביניים), ניצור אינסטנס כדי למנוע קריסה
    from types import SimpleNamespace

    class _FallbackStatusMonitor:
        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                logging.getLogger(__name__).warning(
                    "Fallback status_monitor noop called: %s", name
                )
                return None

            return _noop

    status_monitor = _FallbackStatusMonitor()

try:
    from log_monitor import log_monitor  # Log monitoring import
except Exception:
    # Fallback for log_monitor
    class _FallbackLogMonitor:
        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                logging.getLogger(__name__).warning(
                    "Fallback log_monitor noop called: %s", name
                )
                return None
            return _noop
    
    log_monitor = _FallbackLogMonitor()

# הגדרת לוגים - המקום הטוב ביותר הוא כאן, פעם אחת בתחילת הקובץ
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

logging.getLogger("httpx").setLevel(logging.WARNING)

# --- מנגנון נעילה חדש מבוסס MongoDB ---

LOCK_ID = "render_monitor_bot_lock"  # מזהה ייחודי למנעול שלנו


def cleanup_mongo_lock():
    """מנקה את נעילת ה-MongoDB ביציאה"""
    try:
        db.db.locks.delete_one({"_id": LOCK_ID})
        print("INFO: MongoDB lock released.")
    except Exception as e:
        print(f"ERROR: Could not release MongoDB lock on exit: {e}")


def manage_mongo_lock():
    """מנהל נעילה ב-MongoDB כדי למנוע ריצה כפולה עם יציאה נקייה."""
    pid = os.getpid()
    now = datetime.now(timezone.utc)

    lock = db.db.locks.find_one({"_id": LOCK_ID})
    if lock:
        lock_time = lock.get("timestamp", now)
        if getattr(lock_time, "tzinfo", None) is None:
            lock_time = lock_time.replace(tzinfo=timezone.utc)

        # אם הנעילה טרייה יחסית — נצא; אם ישנה — נדרוס
        if (now - lock_time) <= timedelta(minutes=10):
            print("INFO: Lock exists and is recent. Another instance likely running. Exiting.")
            sys.exit(0)
        # נסיון לזהות נעילה ישנה אך עדיין יש מופע פעיל באוויר באמצעות pid
        other_pid = lock.get("pid")
        if other_pid and other_pid != pid:
            try:
                # ב־Linux, os.kill(pid, 0) בודקת קיום תהליך בלי להרוג
                import signal

                os.kill(int(other_pid), 0)
                # אם לא זרק — התהליך עדיין חי; נצא
                print("INFO: Existing process seems alive. Exiting.")
                sys.exit(0)
            except Exception:
                # אין תהליך — נמחק נעילה
                print(f"WARNING: Found stale MongoDB lock from {lock_time} with dead pid {other_pid}. Overwriting.")
                db.db.locks.delete_one({"_id": LOCK_ID})

    try:
        db.db.locks.insert_one({"_id": LOCK_ID, "pid": pid, "timestamp": now})
        atexit.register(cleanup_mongo_lock)
        print(f"INFO: MongoDB lock acquired by process {pid}.")
    except DuplicateKeyError:
        print("INFO: Lock was acquired by another process just now. Exiting gracefully.")
        sys.exit(0)
    except Exception as e:
        print(f"ERROR: Failed to acquire MongoDB lock: {e}")
        sys.exit(1)


class RenderMonitorBot:
    def __init__(self):
        self.app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(self.setup_bot_commands).build()
        self.db = db
        self.render_api = render_api
        self.setup_handlers()
        # הפקודות יוגדרו ב-post_init

    def _is_admin_user(self, user_obj) -> bool:
        """בדיקה בטוחה האם המשתמש הוא אדמין לפי ADMIN_CHAT_ID."""
        try:
            if user_obj is None:
                return False
            user_id = getattr(user_obj, "id", None)
            if user_id is None:
                return False
            return str(user_id) == str(config.ADMIN_CHAT_ID)
        except Exception:
            return False

    def _simplified_status_live_or_db(self, service: dict) -> str:
        """מחזיר סטטוס מפושט (online/offline/deploying/unknown) לפי מצב חי מ-Render,
        ובנפילה חוזר לערך שמור במסד הנתונים.
        """
        service_id = service.get("_id")
        try:
            live_status = self.render_api.get_service_status(service_id)
            if live_status:
                return status_monitor._simplify_status(live_status)
        except Exception as e:
            logging.debug(f"Live status check failed for {service_id}: {e}")
        # נפילה או אין סטטוס חי – נשתמש ב-last_known_status אם קיים
        fallback = service.get("last_known_status", "unknown")
        return status_monitor._simplify_status(fallback) if fallback else "unknown"

    def _status_to_emoji(self, simplified_status: str) -> str:
        """מפה סטטוס מפושט לאימוג'י תצוגה."""
        if simplified_status == "online":
            return "🟢"
        if simplified_status == "offline":
            return "🔴"
        # ל-deploying/unknown נחזיר צהוב
        return "🟡"

    def _get_status_emoji_for_service(self, service: dict) -> str:
        """נוחות: סטטוס חי->מפושט->אימוג'י עבור שירות."""
        simplified = self._simplified_status_live_or_db(service)
        return self._status_to_emoji(simplified)

    def _service_recency_key(self, service: dict) -> datetime:
        """מחשב timestamp להשוואת עדכניות בין רשומות שירות."""
        for field in ("updated_at", "last_user_activity", "resumed_at", "suspended_at", "created_at"):
            value = service.get(field)
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value
        return datetime.min.replace(tzinfo=timezone.utc)

    def _is_preferred_service_id(self, service_id: Optional[str]) -> bool:
        """בודק אם ה-ID נראה כמו מזהה Render אמיתי (לדוגמה srv-abcdef)."""
        if not service_id:
            return False
        sid = str(service_id).strip().lower()
        return sid.startswith("srv-") or sid.startswith("srv_")

    def _deduplicate_services_by_display_name(self, services: List[dict]) -> List[dict]:
        """מסנן כפילויות לפי service_name ומעדיף את הרשומה העדכנית ביותר."""
        unique: dict[str, dict] = {}
        for service in services:
            key = str(service.get("service_name") or service.get("_id") or "").strip().lower()
            if not key:
                key = str(service.get("_id") or id(service))
            recency = self._service_recency_key(service)
            preferred = self._is_preferred_service_id(service.get("_id"))
            current = unique.get(key)
            if current is None:
                unique[key] = {"service": service, "recency": recency, "preferred": preferred}
                continue
            if preferred and not current["preferred"]:
                unique[key] = {"service": service, "recency": recency, "preferred": preferred}
                continue
            if preferred == current["preferred"] and recency > current["recency"]:
                unique[key] = {"service": service, "recency": recency, "preferred": preferred}
        filtered = [entry["service"] for entry in unique.values()]
        filtered.sort(key=lambda svc: str(svc.get("service_name") or svc.get("_id") or "").lower())
        return filtered

    def _get_visible_services(self) -> List[dict]:
        """מחזיר רשימת שירותים נקייה מכפילויות לתצוגה."""
        services = self.db.get_all_services()
        if not services:
            return []
        return self._deduplicate_services_by_display_name(services)

    async def setup_bot_commands(self, app: Application):
        """הגדרת תפריט הפקודות בטלגרם (מורץ לאחר אתחול האפליקציה)"""
        from telegram import BotCommand

        commands = [
            BotCommand("start", "🚀 הפעלת הבוט"),
            BotCommand("status", "📊 סטטוס כל השירותים"),
            BotCommand("plans", "💳 מידע על תוכנית ודיסק לכל שירות"),
            BotCommand("add_service", "➕ הוספת שירות למערכת"),
            BotCommand("manage", "🎛️ ניהול שירותים"),
            BotCommand("monitor_manage", "👁️ ניהול ניטור סטטוס"),
            BotCommand("suspend", "⏸️ השעיית כל השירותים"),
            BotCommand("resume", "▶️ החזרת שירותים מושעים"),
            BotCommand("list_suspended", "📋 רשימת מושעים"),
            BotCommand("list_monitored", "👁️ רשימת מנוטרים"),
            BotCommand("monitor", "🔔 הפעלת ניטור סטטוס"),
            BotCommand("unmonitor", "🔕 כיבוי ניטור סטטוס"),
            BotCommand("test_monitor", "🧪 בדיקת ניטור"),
            BotCommand("logs", "📋 צפייה בלוגים של שירות"),
            BotCommand("errors", "🔥 צפייה רק בשגיאות"),
            BotCommand("logs_monitor", "🔍 הפעלת ניטור לוגים"),
        		BotCommand("logs_unmonitor", "🔇 כיבוי ניטור לוגים"),
        		BotCommand("logs_manage", "🎛️ ניהול ניטור לוגים"),
        		# Alias נוח ללא קו תחתון
        		BotCommand("logsmanage", "🎛️ ניהול ניטור לוגים (כינוי)"),
        		BotCommand("env_list", "📝 רשימת משתני סביבה"),
        		BotCommand("env_set", "✏️ עדכון משתנה סביבה"),
        		BotCommand("env_delete", "🗑️ מחיקת משתנה סביבה"),
        		BotCommand("remind", "⏰ יצירת תזכורת"),
        		BotCommand("reminders", "📋 רשימת תזכורות"),
        		BotCommand("delete_reminder", "🗑️ מחיקת תזכורת"),
        		BotCommand("help", "❓ עזרה ומידע"),
        ]

        # הגדרת הפקודות בבוט לאחר שהלולאה פעילה
        await app.bot.set_my_commands(commands)

    def setup_handlers(self):
        """הוספת command handlers"""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("status", self.status_command))
        self.app.add_handler(CommandHandler("add_service", self.add_service_command))
        self.app.add_handler(CommandHandler("manage", self.manage_command))
        self.app.add_handler(CommandHandler("delete_service", self.delete_service_command))
        self.app.add_handler(CommandHandler("suspend", self.suspend_command))
        self.app.add_handler(CommandHandler("resume", self.resume_command))
        self.app.add_handler(CommandHandler("list_suspended", self.list_suspended_command))
        self.app.add_handler(CommandHandler("plans", self.plans_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("diag", self.diag_command))

        # Monitor commands
        self.app.add_handler(CommandHandler("monitor", self.monitor_command))
        self.app.add_handler(CommandHandler("unmonitor", self.unmonitor_command))
        self.app.add_handler(CommandHandler("list_monitored", self.list_monitored_command))
        self.app.add_handler(CommandHandler("monitor_manage", self.monitor_manage_command))  # New handler
        self.app.add_handler(CommandHandler("test_monitor", self.test_monitor_command))  # Test command

        # Cleanup command for test data
        self.app.add_handler(CommandHandler("clear_test_data", self.clear_test_data_command))

        # Log monitoring commands
        self.app.add_handler(CommandHandler("logs", self.logs_command))
        self.app.add_handler(CommandHandler("errors", self.errors_command))  # קיצור דרך לשגיאות
        self.app.add_handler(CommandHandler("logs_monitor", self.logs_monitor_command))
        self.app.add_handler(CommandHandler("logs_unmonitor", self.logs_unmonitor_command))
        self.app.add_handler(CommandHandler("logs_manage", self.logs_manage_command))
        # Alias ללא קו תחתון עבור נוחות המשתמשים
        self.app.add_handler(CommandHandler("logsmanage", self.logs_manage_command))

        # Environment variables commands
        self.app.add_handler(CommandHandler("env_list", self.env_list_command))
        self.app.add_handler(CommandHandler("env_set", self.env_set_command))
        self.app.add_handler(CommandHandler("env_delete", self.env_delete_command))

        # Reminder commands
        self.app.add_handler(CommandHandler("remind", self.remind_command))
        self.app.add_handler(CommandHandler("reminders", self.reminders_command))
        self.app.add_handler(CommandHandler("delete_reminder", self.delete_reminder_command))

        self.app.add_handler(
            CallbackQueryHandler(self.manage_service_callback, pattern="^manage_|^go_to_monitor_manage$|^suspend_all$")
        )
        self.app.add_handler(
            CallbackQueryHandler(
                self.service_action_callback,
                pattern="^suspend_|^resume_|^back_to_manage$",
            )
        )
        self.app.add_handler(
            CallbackQueryHandler(
                self.remove_service_action_callback,
                pattern="^confirmremove_|^remove_|^back_to_service_",
            )
        )
        self.app.add_handler(
            CallbackQueryHandler(self.suspend_button_callback, pattern="^confirm_suspend_all|^cancel_suspend$")
        )
        self.app.add_handler(
            CallbackQueryHandler(self.delete_service_confirm_callback, pattern="^confirm_delete_|^cancel_delete_")
        )
        self.app.add_handler(CallbackQueryHandler(self.monitor_detail_callback, pattern="^monitor_detail_"))
        self.app.add_handler(
            CallbackQueryHandler(
                self.monitor_action_callback,
                pattern=(
                    "^enable_monitor_|^disable_monitor_|^back_to_monitor_list|^refresh_"
                    "monitor_manage|^show_monitored_only|^enable_deploy_notif_|^disable_deploy_notif_"
                ),
            )
        )
        self.app.add_handler(
            CallbackQueryHandler(
                self.logs_action_callback,
                pattern=(
                    "^enable_log_monitor_|^disable_log_monitor_|^log_detail_|^back_to_logs_list|"
                    "^refresh_logs_manage|^show_logs_monitored_only|^set_log_threshold_"
                ),
            )
        )
        self.app.add_handler(
            CallbackQueryHandler(
                self.env_action_callback,
                pattern="^confirm_env_set_|^confirm_env_delete_|^cancel_env_action",
            )
        )

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודת התחלה"""
        message = "🤖 שלום! זה בוט ניטור Render\n\n"
        message += "הבוט מנטר את השירותים שלך ומשעה אותם אוטומטית במידת הצורך.\n\n"
        message += "הקש /help לרשימת פקודות"
        msg = update.message
        if msg is None:
            return
        await msg.reply_text(message)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הצגת עזרה"""
        help_text = """
📚 *רשימת פקודות:*

/start - הפעלת הבוט
/status - בדיקת סטטוס השירותים
/add_service [service_id] [name] - הוספת שירות למערכת (עם אימות מול Render)
/suspend - השעיית כל השירותים
/resume - החזרת כל השירותים המושעים
/list_suspended - רשימת שירותים מושעים
/plans - מידע על תוכנית (חינמי/בתשלום) ודיסק מחובר
/manage - ניהול שירותים עם כפתורים

*פקודות ניטור סטטוס:*
/monitor [service_id] - הפעלת ניטור סטטוס לשירות
/unmonitor [service_id] - כיבוי ניטור סטטוס לשירות
/monitor_manage - ניהול ניטור עם כפתורים
/list_monitored - רשימת שירותים בניטור סטטוס
/test_monitor [service_id] [action] - בדיקת התראות
/diag - דיאגנוסטיקה מהירה

*פקודות ניטור לוגים:* 🆕
/logs [service_id] [lines] [min] [filter] - צפייה בלוגים
  • lines - כמה שורות (ברירת מחדל: 100)
  • minutes - מכמה דקות אחורה (אופציונלי)
  • filter - all/errors/stdout/stderr (אופציונלי)
  דוגמאות:
    /logs srv-123 100 5 - 100 שורות מ-5 דקות
    /logs srv-123 100 5 errors - רק שגיאות מ-5 דקות 🔥
    /logs srv-123 50 - errors - רק 50 שגיאות אחרונות

/errors [service_id] [lines] [minutes] - צפייה רק בשגיאות 🔥
  קיצור דרך נוח! דוגמה: /errors srv-123 50 5

/logs_monitor [service_id] [threshold] - הפעלת ניטור לוגים
/logs_unmonitor [service_id] - כיבוי ניטור לוגים
/logs_manage - ניהול ניטור לוגים עם כפתורים

*ניהול משתני סביבה:* 🆕
/env_list [service_id] - הצגת משתני סביבה של שירות
/env_set [service_id] [key] [value] - עדכון/הוספת משתנה
/env_delete [service_id] [key] - מחיקת משתנה
  דוגמאות:
    /env_list srv-123456
    /env_set srv-123456 API_KEY new_value_here
    /env_delete srv-123456 OLD_VAR

*תזכורות:* ⏰
/remind [time] [text] - יצירת תזכורת
  • פורמטי זמן: 30m (דקות), 2h (שעות), 7d (ימים), 1w (שבועות)
  דוגמאות:
    /remind 7d לחדש שירות API
    /remind 30d להשעות את הבוט
    /remind 2h לבדוק דיפלוי
/reminders - רשימת התזכורות הפעילות שלך
/delete\_reminder [id] - מחיקת תזכורת

*אדמין:*
/delete_service [service_id] - מחיקת שירות מה-DB בלבד
/clear_test_data - ניקוי נתוני בדיקות

מידע חשוב:
• ניטור לוגים: זיהוי אוטומטי של שגיאות והתראות בזמן אמת
• סף שגיאות: קובע כמה שגיאות נדרשות להתראה (ברירת מחדל: 5)
• איך למצוא ID: דרך `/status` או מדשבורד Render
• דוגמה: `/logs srv-1234567890 100`

/help - הצגת הודעה זו
        """
        msg = update.message
        if msg is None:
            return
        await msg.reply_text(help_text, parse_mode="Markdown")

    async def add_service_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הוספת שירות חדש למעקב: /add_service <service_id> <name>

        הבוט מוודא שהשירות קיים דרך Render API (GET /services/{service_id}),
        ואם קיים — שומר ב-MongoDB עם owner_id.
        """
        msg = update.message
        if msg is None:
            return

        user = update.effective_user
        if user is None:
            return

        if not context.args or len(context.args) < 1:
            await msg.reply_text(
                "שימוש: `/add_service <service_id> <name>`\n\n"
                "דוגמה: `/add_service srv-123456 My Bot`",
                parse_mode="Markdown",
            )
            return

        service_id = str(context.args[0]).strip()
        requested_name = " ".join(context.args[1:]).strip() if len(context.args) > 1 else ""

        # אימות מול Render: GET /services/{service_id}
        service_info = None
        try:
            service_info = self.render_api.get_service_info(service_id)
        except Exception:
            service_info = None

        if not service_info:
            await msg.reply_text(
                f"❌ השירות לא נמצא ב-Render או שה-ID שגוי\n\n"
                f"בדוק את המזהה ונסה שוב: `{service_id}`",
                parse_mode="Markdown",
            )
            return

        render_name = str(service_info.get("name") or service_info.get("serviceName") or service_info.get("slug") or "").strip()
        final_name = requested_name or render_name or service_id
        owner_id = str(user.id)

        # מניעת "חטיפה": אם השירות כבר רשום עם owner אחר, רק אדמין יכול להעביר בעלות.
        # אם השירות קיים במסד ללא owner (למשל seeded מ-SERVICES_TO_MONITOR), המשתמש הראשון שיקרא /add_service "יתפוס" בעלות.
        try:
            existing = self.db.get_service_activity(service_id)
        except Exception as e:
            await msg.reply_text(
                "❌ לא הצלחתי לקרוא מה-DB כדי לוודא בעלות (ייתכן תקלה זמנית).\n"
                f"נסה שוב בעוד רגע.\n\nשגיאה: {e}"
            )
            return

        force_owner_update = False
        claim_owner_if_unowned = False
        if existing is not None:
            existing_owner = existing.get("owner_id")
            if existing_owner:
                # יש בעלות קיימת - לא מאפשרים שינוי למשתמש רגיל
                if str(existing_owner) != owner_id:
                    if not self._is_admin_user(user):
                        await msg.reply_text(
                            "❌ השירות הזה כבר רשום במערכת תחת owner אחר.\n"
                            "אם זה שירות שלך, פנה לאדמין כדי להעביר בעלות.",
                        )
                        return
                    force_owner_update = True
            else:
                # שירות קיים אך ללא owner_id - נאפשר למשתמש לתפוס בעלות
                claim_owner_if_unowned = True

        try:
            result = self.db.register_service(
                service_id,
                owner_id=owner_id,
                service_name=final_name,
                force_owner_update=force_owner_update,
                claim_owner_if_unowned=claim_owner_if_unowned,
            )
        except DuplicateKeyError:
            # מצב קצה: התנגשות בזמן upsert/insert. אם ניסינו claim — סביר שמישהו אחר תפס קודם.
            if claim_owner_if_unowned:
                await msg.reply_text(
                    "❌ לא הצלחתי לתפוס בעלות על השירות כי הוא נרשם בדיוק עכשיו על ידי משתמש אחר.\n"
                    "נסה שוב או פנה לאדמין אם צריך להעביר בעלות."
                )
                return
            await msg.reply_text("❌ כשל ברישום השירות במסד הנתונים (Duplicate key). נסה שוב.")
            return
        except Exception as e:
            await msg.reply_text(f"❌ כשל ברישום השירות במסד הנתונים: {e}")
            return

        # אם ניסינו "לתפוס" בעלות והשירות נתפס במקביל ע"י משתמש אחר, העדכון לא יתאים לפילטר
        if claim_owner_if_unowned:
            matched = getattr(result, "matched_count", 0)
            modified = getattr(result, "modified_count", 0)
            if matched == 0 and modified == 0:
                # או שהשירות כבר קיבל owner, או שהוא נמחק/לא קיים — נבדוק כדי להחזיר הודעה נכונה
                try:
                    current = self.db.get_service_activity(service_id)
                except Exception:
                    current = None
                if current and current.get("owner_id") and str(current.get("owner_id")) != owner_id:
                    await msg.reply_text(
                        "❌ לא הצלחתי לתפוס בעלות על השירות כי הוא נרשם בדיוק עכשיו על ידי משתמש אחר.\n"
                        "נסה שוב או פנה לאדמין אם צריך להעביר בעלות."
                    )
                    return
                if current and current.get("owner_id") and str(current.get("owner_id")) == owner_id:
                    # מצב נפוץ ברייס: בקשה מקבילה כבר תפסה בעלות עבור אותו משתמש.
                    # נעדכן רק את שם השירות/updated_at בלי לגעת בבעלות, ונחזיר הצלחה.
                    try:
                        self.db.register_service(service_id, owner_id=owner_id, service_name=final_name)
                    except Exception:
                        pass
                    safe_name = str(final_name).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
                    await msg.reply_text(
                        f"✅ השירות כבר רשום על שמך!\n\n"
                        f"🤖 שם: *{safe_name}*\n"
                        f"🆔 ID: `{service_id}`",
                        parse_mode="Markdown",
                    )
                    return
                if current is None:
                    await msg.reply_text("❌ השירות לא נמצא במסד הנתונים כרגע (ייתכן שנמחק). נסה שוב.")
                    return
                # אם owner עדיין חסר אך לא הצלחנו לעדכן—נחזיר הודעה כללית כדי לא להטעות
                await msg.reply_text("❌ לא הצלחתי לעדכן בעלות כרגע. נסה שוב בעוד רגע.")
                return

        safe_name = str(final_name).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
        await msg.reply_text(
            f"✅ השירות נוסף בהצלחה!\n\n"
            f"🤖 שם: *{safe_name}*\n"
            f"🆔 ID: `{service_id}`",
            parse_mode="Markdown",
        )

    async def delete_service_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מחיקת שירות והיסטורייתו מה-DB (אדמין בלבד)"""
        msg = update.message
        if msg is None:
            return
        user = update.effective_user
        if not user or str(user.id) != config.ADMIN_CHAT_ID:
            await msg.reply_text("❌ פקודה זו זמינה רק למנהל המערכת")
            return
        if not context.args:
            await msg.reply_text("שימוש: /delete_service [service_id]")
            return
        service_id = context.args[0]
        # שלב אימות לפני מחיקה
        warning = "⚠️ *אישור מחיקה*\n\n"
        warning += f"האם אתה בטוח שברצונך למחוק לצמיתות את השירות עם ה-ID: `{service_id}`?\n\n"
        warning += "מה יימחק מהמסד (לא ב-Render):\n"
        warning += "• `service_activity`\n"
        warning += "• `user_interactions`\n"
        warning += "• `manual_actions`\n"
        warning += "• `status_changes`\n"
        warning += "• `deploy_events`\n\n"
        warning += "פעולה זו בלתי הפיכה."

        keyboard = [
            [
                InlineKeyboardButton("✅ כן, מחק", callback_data=f"confirm_delete_{service_id}"),
                InlineKeyboardButton("❌ בטל", callback_data=f"cancel_delete_{service_id}"),
            ]
        ]
        await msg.reply_text(warning, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def delete_service_confirm_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """טיפול באישור/ביטול מחיקה ב-inline keyboard"""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()

        data = query.data
        user = query.from_user
        if not user or str(user.id) != config.ADMIN_CHAT_ID:
            await query.answer("אין הרשאה", show_alert=True)
            return

        try:
            back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזור לניהול", callback_data="back_to_manage")]])
            if data.startswith("confirm_delete_"):
                service_id = data.replace("confirm_delete_", "")
                result = self.db.delete_service(service_id)
                summary = (
                    f"✅ נמחק השירות `{service_id}` מה-DB\n"
                    f"🗂️ services: {result.get('services', 0)} | interactions: {result.get('user_interactions', 0)} | "
                    f"manual: {result.get('manual_actions', 0)} | status: {result.get('status_changes', 0)} | "
                    f"deploy: {result.get('deploy_events', 0)}"
                )
                await query.edit_message_text(summary, reply_markup=back_markup, parse_mode="Markdown")
            elif data.startswith("cancel_delete_"):
                service_id = data.replace("cancel_delete_", "")
                await query.edit_message_text(
                    f"❎ המחיקה בוטלה עבור `{service_id}`", reply_markup=back_markup, parse_mode="Markdown"
                )
        except Exception as e:
            await query.edit_message_text(f"❌ שגיאה במחיקה: {e}")

    async def plans_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מציג עבור כל שירות אם הוא בתוכנית חינמית/בתשלום והאם יש לו דיסק מחובר"""
        msg = update.message
        if msg is None:
            return
        try:
            # רשימת שירותים חיים מה-API כדי לכלול גם שירותים שאינם במסד
            services_live = self.render_api.list_services()
            # רשימת דיסקים מה-API (אם הנתיב קיים)
            disks = self.render_api.list_disks()
            service_id_to_disks = {}
            for d in disks:
                sid = d.get("serviceId") or d.get("service_id") or d.get("service")
                if sid:
                    service_id_to_disks.setdefault(str(sid), []).append(d)

            if not services_live:
                await msg.reply_text("לא נמצאו שירותים מה-Render API")
                return

            lines = ["💳 *מידע תוכניות ודיסקים*\n"]
            for svc in services_live:
                sid = str(
                    svc.get("id")
                    or svc.get("serviceId")
                    or svc.get("_id")
                    or svc.get("uuid")
                    or "?"
                )

                name = str(
                    svc.get("name")
                    or svc.get("serviceName")
                    or svc.get("slug")
                    or svc.get("displayName")
                    or sid
                )

                plan_str = self.render_api.get_service_plan_string(svc)
                is_free = self.render_api.is_free_plan(plan_str)

                # נזהה דיסק לפי רשימת הדיסקים, ואם ריק ננסה לזהות מתוך השירות עצמו
                disk_list = service_id_to_disks.get(sid, [])
                has_disk = bool(disk_list) or self.render_api.service_has_disk(svc)

                # נסה לקבל מידע מפורט אם לא זוהה מזהה/שם/תוכנית
                if (not plan_str or is_free is None) or (name == sid or name == "?"):
                    try:
                        if sid and sid != "?":
                            svc_info = self.render_api.get_service_info(sid)
                            if isinstance(svc_info, dict) and svc_info:
                                # עדכון שם אם חסר
                                if name == sid or name == "?":
                                    name = str(
                                        svc_info.get("name")
                                        or svc_info.get("serviceName")
                                        or svc_info.get("slug")
                                        or name
                                    )
                                # עדכון תוכנית
                                if not plan_str or is_free is None:
                                    plan_str = self.render_api.get_service_plan_string(svc_info) or plan_str
                                    is_free = self.render_api.is_free_plan(plan_str)
                                # עדכון מידע דיסק אם עדיין לא זוהה
                                if not has_disk:
                                    has_disk = self.render_api.service_has_disk(svc_info)
                    except Exception:
                        pass

                status_emoji = "🆓" if is_free is True else ("💰" if is_free is False else "❔")
                disk_emoji = "💽" if has_disk else "—"

                plan_display = plan_str or "לא ידוע"
                kind_text = "חינמי" if is_free is True else ("בתשלום" if is_free is False else "לא ידוע")
                lines.append(
                    f"{status_emoji} *{name}*\n   ID: `{sid}`\n   תוכנית: {plan_display} ({kind_text})\n   דיסק: {disk_emoji}\n"
                )

            await msg.reply_text("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            await msg.reply_text(f"❌ כשל בקבלת מידע תוכניות/דיסקים: {e}")

    async def diag_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מציג דיאגנוסטיקה מהירה של מצב הניטור וההתראות"""
        msg = update.message
        if msg is None:
            return
        try:
            from database import db

            monitored = db.get_status_monitored_services()
            deploy_enabled = db.get_services_with_deploy_notifications_enabled()

            message = "🛠️ *דיאגנוסטיקה מהירה*\n\n"
            message += f"🔁 ניטור רץ: {'כן' if (status_monitor.monitoring_thread and status_monitor.monitoring_thread.is_alive()) else 'לא'}\n"
            message += f"⏱️ מרווח בדיקה: {status_monitor.deploy_check_interval if status_monitor.deploying_active else status_monitor.check_interval}s\n"
            message += f"👁️ שירותים בניטור סטטוס: {len(monitored)}\n"
            message += f"🚀 שירותים עם התראות דיפלוי: {len(deploy_enabled)}\n"
            if not monitored and not deploy_enabled and not config.SERVICES_TO_MONITOR:
                message += "⚠️ אין שירותים לבדיקה (DB ריק ואין SERVICES_TO_MONITOR)\n"
            await msg.reply_text(message, parse_mode="Markdown")
        except Exception as e:
            await msg.reply_text(f"❌ כשל בדיאגנוסטיקה: {e}")

    async def monitor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הפעלת ניטור סטטוס לשירות"""
        msg = update.message
        if msg is None:
            return
        if not context.args:
            await msg.reply_text("❌ חסר service ID\nשימוש: /monitor [service_id]")
            return

        service_id = context.args[0]
        user = update.effective_user
        if user is None:
            return
        user_id = user.id

        # הפעלת הניטור
        if status_monitor.enable_monitoring(service_id, user_id):
            await msg.reply_text(f"✅ ניטור סטטוס הופעל עבור השירות {service_id}\n" f"תקבל התראות כשהשירות יעלה או ירד.")
            # ודא שהלולאת ניטור רצה גם אם כובהה בקובץ ההגדרות
            try:
                status_monitor.start_monitoring()
            except Exception:
                pass
        else:
            await msg.reply_text(f"❌ לא הצלחתי להפעיל ניטור עבור {service_id}\n" f"ודא שה-ID נכון ושהשירות קיים ב-Render.")

    async def unmonitor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """כיבוי ניטור סטטוס לשירות"""
        msg = update.message
        if msg is None:
            return
        if not context.args:
            await msg.reply_text("❌ חסר service ID\nשימוש: /unmonitor [service_id]")
            return

        service_id = context.args[0]
        user = update.effective_user
        if user is None:
            return
        user_id = user.id

        # כיבוי הניטור
        if status_monitor.disable_monitoring(service_id, user_id):
            await msg.reply_text(f"✅ ניטור סטטוס כובה עבור השירות {service_id}")
        else:
            await msg.reply_text(f"❌ לא הצלחתי לכבות ניטור עבור {service_id}")

    async def list_monitored_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הצגת רשימת שירותים בניטור סטטוס"""
        msg = update.message
        if msg is None:
            return
        monitored_services = status_monitor.get_all_monitored_services()

        if not monitored_services:
            await msg.reply_text("📭 אין שירותים בניטור סטטוס כרגע")
            return

        message = "👁️ *שירותים בניטור סטטוס:*\n\n"

        for service in monitored_services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)
            last_status = service.get("last_known_status", "unknown")
            monitoring_info = service.get("status_monitoring", {})
            enabled_at = monitoring_info.get("enabled_at")

            # אימוג'י לפי סטטוס
            status_emoji = "🟢" if last_status == "online" else "🔴" if last_status == "offline" else "🟡"

            message += f"{status_emoji} *{service_name}*\n"
            message += f"   ID: `{service_id}`\n"
            message += f"   סטטוס: {last_status}\n"

            if enabled_at:
                try:
                    if enabled_at.tzinfo is None:
                        enabled_at = enabled_at.replace(tzinfo=timezone.utc)
                    days_monitored = (datetime.now(timezone.utc) - enabled_at).days
                    message += f"   בניטור: {days_monitored} ימים\n"
                except Exception as e:
                    logging.debug(f"Failed to compute monitored days for {service_id}: {e}")

            message += "\n"

        await msg.reply_text(message, parse_mode="Markdown")

    async def monitor_manage_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ניהול ניטור סטטוס דרך פקודה"""
        msg = update.message
        if msg is None:
            return
        services = self._get_visible_services()

        if not services:
            await msg.reply_text("📭 אין שירותים במערכת")
            return

        keyboard = []

        for service in services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)

            # סטטוס נוכחי (חי מ-Render עם נפילה ל-DB)
            status_emoji = self._get_status_emoji_for_service(service)

            # אימוג'י ניטור
            monitoring_status = status_monitor.get_monitoring_status(service_id)
            is_monitored = monitoring_status.get("enabled", False)
            monitor_emoji = "👁️" if is_monitored else "👁️‍🗨️"

            button_text = f"{status_emoji} {monitor_emoji} {service_name[:20]}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"monitor_detail_{service_id}")])

        # כפתורים נוספים
        keyboard.append([InlineKeyboardButton("📊 הצג רק מנוטרים", callback_data="show_monitored_only")])
        keyboard.append([InlineKeyboardButton("🔄 רענן", callback_data="refresh_monitor_manage")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        message = "🎛️ *ניהול ניטור סטטוס*\n\n"
        message += "👁️ = בניטור | 👁️‍🗨️ = לא בניטור\n"
        message += "🟢 = פעיל | 🔴 = כבוי | 🟡 = לא ידוע\n\n"
        message += "בחר שירות לניהול:"

        await msg.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    async def clear_test_data_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מחיקת נתוני בדיקות דמה"""
        # בדיקת הרשאות (רק אדמין)
        msg = update.message
        if msg is None:
            return
        user = update.effective_user
        if user is None:
            return
        if str(user.id) != config.ADMIN_CHAT_ID:
            await msg.reply_text("❌ פקודה זו זמינה רק למנהל המערכת")
            return

        count = db.clear_test_data()
        await msg.reply_text(f"✅ נמחקו {count} פעולות בדיקה\n✅ אופסו סטטוסים ונתוני פעילות של שירותים בבדיקה")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הצגת מצב כל השירותים"""
        msg = update.message
        if msg is None:
            return
        services = db.get_all_services()

        print(f"נמצאו {len(services)} שירותים במסד הנתונים לבדיקה.")

        if not services:
            await msg.reply_text("אין שירותים רשומים במערכת")
            return

        message = "📊 *מצב השירותים:*\n\n"

        for service in services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)
            status = service.get("status", "unknown")
            last_activity = service.get("last_user_activity")

            if status == "suspended":
                status_emoji = "🔴"
            else:
                status_emoji = "🟢"

            message += f"{status_emoji} *{service_name}*\n"
            message += f"   ID: `{service_id}`\n"
            message += f"   סטטוס: {status}\n"

            if last_activity:
                if last_activity.tzinfo is None:
                    last_activity = last_activity.replace(tzinfo=timezone.utc)
                days_inactive = (datetime.now(timezone.utc) - last_activity).days
                message += f"   פעילות אחרונה: {days_inactive} ימים\n"
            else:
                message += "   פעילות אחרונה: לא ידוע\n"

            message += "\n"

        await msg.reply_text(message, parse_mode="Markdown")

    async def suspend_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """שולח בקשת אישור להשעיית כל השירותים"""
        msg = update.message
        if msg is None:
            return
        keyboard = [
            [
                InlineKeyboardButton("✅ כן, השעה הכל", callback_data="confirm_suspend_all"),
                InlineKeyboardButton("❌ בטל", callback_data="cancel_suspend"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await msg.reply_text(
            "⚠️ האם אתה בטוח שברצונך להשהות את <b>כל</b> השירותים?", reply_markup=reply_markup, parse_mode="HTML"
        )

    async def suspend_one_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """השעיית שירות ספציפי"""
        msg = update.message
        if msg is None:
            return
        if not context.args:
            await msg.reply_text("❌ חסר service ID\nשימוש: /suspend_one [service_id]")
            return

        service_id = context.args[0]

        # סימון פעולה ידנית במנטר הסטטוס
        status_monitor.mark_manual_action(service_id)

        try:
            self.render_api.suspend_service(service_id)
            self.db.update_service_activity(service_id, status="suspended")
            self.db.increment_suspend_count(service_id)
            await msg.reply_text(f"✅ השירות {service_id} הושהה בהצלחה.")
            print(f"Successfully suspended service {service_id}.")
        except Exception as e:
            await msg.reply_text(f"❌ כישלון בהשעיית השירות {service_id}.\nשגיאה: {e}")
            print(f"Failed to suspend service {service_id}. Error: {e}")

    async def resume_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """החזרת כל השירותים המושעים"""
        msg = update.message
        if msg is None:
            return
        suspended_services = db.get_suspended_services()

        if not suspended_services:
            await msg.reply_text("אין שירותים מושעים")
            return

        await msg.reply_text("מתחיל החזרת שירותים לפעילות...")

        messages = []
        for service in suspended_services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)

            result = activity_tracker.manual_resume_service(service_id)

            if result["success"]:
                messages.append(f"✅ {service_name} - הוחזר לפעילות")
                # התחלת מעקב אקטיבי אחר דיפלוי בעקבות ההפעלה
                try:
                    status_monitor.watch_deploy_until_terminal(service_id, service_name)
                except Exception:
                    pass
            else:
                messages.append(f"❌ {service_name} - כשלון: {result['message']}")

        response = "תוצאות החזרה לפעילות:\n\n" + "\n".join(messages)
        await msg.reply_text(response)

    async def list_suspended_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """רשימת שירותים מושעים"""
        msg = update.message
        if msg is None:
            return
        suspended_services = db.get_suspended_services()

        if not suspended_services:
            await msg.reply_text("אין שירותים מושעים כרגע")
            return

        message = "🔴 *שירותים מושעים:*\n\n"

        for service in suspended_services:
            service_name = service.get("service_name", service["_id"])
            suspended_at = service.get("suspended_at")

            message += f"• *{service_name}*\n"
            if suspended_at:
                if suspended_at.tzinfo is None:
                    suspended_at = suspended_at.replace(tzinfo=timezone.utc)
                days_suspended = (datetime.now(timezone.utc) - suspended_at).days
                message += f"  מושעה כבר {days_suspended} ימים\n"
            message += "\n"

        await msg.reply_text(message, parse_mode="Markdown")

    async def manage_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ניהול שירותים עם כפתורים אינטראקטיביים"""
        msg = update.message
        if msg is None:
            return
        services = self._get_visible_services()

        if not services:
            await msg.reply_text("📭 אין שירותים במערכת")
            return

        keyboard = []

        # כפתור לניהול ניטור סטטוס
        keyboard.append([InlineKeyboardButton("👁️ ניהול ניטור סטטוס", callback_data="go_to_monitor_manage")])

        # רשימת שירותים
        for service in services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)
            status = await self._get_live_status(service_id)

            # אימוג'י לפי סטטוס
            if status == "suspended":
                emoji = "🔴"
            else:
                emoji = "🟢"

            # שם מקוצר אם ארוך מדי
            display_name = service_name[:25] + "..." if len(service_name) > 25 else service_name

            row = [InlineKeyboardButton(f"{emoji} {display_name}", callback_data=f"manage_{service_id}")]
            keyboard.append(row)

        # כפתור השעיה כללית
        keyboard.append([InlineKeyboardButton("⏸️ השעה הכל", callback_data="suspend_all")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        message = "🎛️ *ניהול שירותים*\n\n"
        message += "🟢 = פעיל | 🔴 = מושעה\n\n"
        message += "בחר שירות לניהול או פעולה כללית:"

        await msg.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    async def show_manage_menu(self, query: CallbackQuery):
        """מציג את תפריט הניהול בהודעה קיימת (עריכה)"""
        services = self._get_visible_services()

        if not services:
            await query.edit_message_text("📭 אין שירותים במערכת")
            return

        keyboard = []

        # כפתור לניהול ניטור סטטוס
        keyboard.append([InlineKeyboardButton("👁️ ניהול ניטור סטטוס", callback_data="go_to_monitor_manage")])

        # רשימת שירותים
        for service in services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)
            status = await self._get_live_status(service_id)

            # אימוג'י לפי סטטוס
            if status == "suspended":
                emoji = "🔴"
            else:
                emoji = "🟢"

            # שם מקוצר אם ארוך מדי
            display_name = service_name[:25] + "..." if len(service_name) > 25 else service_name

            row = [InlineKeyboardButton(f"{emoji} {display_name}", callback_data=f"manage_{service_id}")]
            keyboard.append(row)

        # כפתור השעיה כללית
        keyboard.append([InlineKeyboardButton("⏸️ השעה הכל", callback_data="suspend_all")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        message = "🎛️ *ניהול שירותים*\n\n"
        message += "🟢 = פעיל | 🔴 = מושעה\n\n"
        message += "בחר שירות לניהול או פעולה כללית:"

        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    async def manage_service_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מציג אפשרויות ניהול לשירות שנבחר"""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()

        # Handle navigation to monitor management
        if query.data == "go_to_monitor_manage":
            # Call monitor_manage as if it was called directly
            await self.refresh_monitor_manage(query)
            return

        # Handle suspend all
        if query.data == "suspend_all":
            keyboard = [
                [InlineKeyboardButton("✅ אישור", callback_data="confirm_suspend_all")],
                [InlineKeyboardButton("❌ ביטול", callback_data="cancel_suspend")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("⚠️ האם אתה בטוח שברצונך להשעות את כל השירותים?", reply_markup=reply_markup)
            return

        # Extract service_id from callback data
        service_id = query.data.replace("manage_", "")
        await self._show_service_manage_actions_menu(query, service_id)

    async def _get_live_status(self, service_id: str) -> str:
        """בודק סטטוס חי מ-Render API ומסנכרן עם הדאטאבייס.
        מחזיר 'suspended' או 'active'.
        """
        try:
            loop = asyncio.get_event_loop()
            live_status = await loop.run_in_executor(
                None, self.render_api.get_service_status, service_id
            )
            if live_status == "suspended":
                # עדכון DB רק אם הסטטוס השתנה, כדי לא לדרוס את suspended_at
                service = self.db.get_service_activity(service_id)
                if not service or service.get("status") != "suspended":
                    self.db.update_service_activity(service_id, status="suspended")
                return "suspended"
            elif live_status in ("unknown", None):
                # סטטוס לא ברור — fallback לדאטאבייס, לא לדרוס
                service = self.db.get_service_activity(service_id)
                return service.get("status", "active") if service else "active"
            else:
                # סטטוס ברור שאינו suspended (online, deploying וכו׳) — עדכון DB אם צריך
                service = self.db.get_service_activity(service_id)
                if service and service.get("status") == "suspended":
                    self.db.update_service_activity(service_id, status="active")
                return "active"
        except Exception:
            # fallback לסטטוס מהדאטאבייס
            service = self.db.get_service_activity(service_id)
            return service.get("status", "active") if service else "active"

    def _escape_markdown(self, text: str) -> str:
        """Escape בסיסי כדי למנוע שבירת Markdown בטלגרם."""
        return str(text).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")

    def _can_remove_service(self, user_obj, service_doc: dict) -> bool:
        """הרשאות הסרה: owner או אדמין. לשירות ללא owner - רק אדמין."""
        if self._is_admin_user(user_obj):
            return True
        if not user_obj:
            return False
        owner_id = service_doc.get("owner_id")
        if owner_id:
            return str(owner_id) == str(getattr(user_obj, "id", ""))
        return False

    async def _show_service_manage_actions_menu(self, query: CallbackQuery, service_id: str) -> None:
        """מציג תפריט פעולות לשירות (השעיה/הפעלה/הסרה) לאחר בחירת שירות."""
        service = self.db.get_service_activity(service_id)
        if not service or service.get("removed") is True:
            await query.edit_message_text("❌ שירות לא נמצא")
            return

        service_name = service.get("service_name", service_id)
        status = await self._get_live_status(service_id)

        keyboard = []
        if status == "suspended":
            keyboard.append([InlineKeyboardButton("▶️ הפעל מחדש", callback_data=f"resume_{service_id}")])
        else:
            keyboard.append([InlineKeyboardButton("⏸️ השעה", callback_data=f"suspend_{service_id}")])

        # כפתור הסרה (רק אם יש הרשאה)
        if self._can_remove_service(query.from_user, service):
            keyboard.append([InlineKeyboardButton("🗑 הסר שירות", callback_data=f"confirmremove_{service_id}")])

        keyboard.append([InlineKeyboardButton("🔙 חזור", callback_data="back_to_manage")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        safe_name = self._escape_markdown(str(service_name))
        message = f"🤖 *{safe_name}*\n"
        message += f"🆔 `{self._escape_markdown(service_id)}`\n"
        message += f"📊 סטטוס: {'🔴 מושעה' if status == 'suspended' else '🟢 פעיל'}\n\n"
        message += "בחר פעולה:"

        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    async def remove_service_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """אישור והסרה של שירות מרשימת הניהול (DB בלבד; לא מוחק מ-Render)."""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()

        data = query.data
        user = query.from_user
        user_id = getattr(user, "id", None)

        # חזרה למסך שירות
        if data.startswith("back_to_service_"):
            service_id = data.split("_", 3)[3]
            await self._show_service_manage_actions_menu(query, service_id)
            return

        # שלב 1: אישור הסרה
        if data.startswith("confirmremove_"):
            service_id = data.split("_", 1)[1]
            service = self.db.get_service_activity(service_id)
            if not service or service.get("removed") is True:
                await query.edit_message_text("❌ שירות לא נמצא")
                return

            if not self._can_remove_service(user, service):
                await query.answer("❌ אין הרשאה להסיר שירות זה", show_alert=True)
                return

            name = self._escape_markdown(str(service.get("service_name", service_id)))
            safe_id = self._escape_markdown(service_id)
            text = (
                "🗑 *האם להסיר את השירות?*\n\n"
                f"🤖 {name}\n"
                f"🆔 `{safe_id}`\n\n"
                "השירות יוסר מרשימת הניהול בלבד — הוא לא יימחק מ-Render."
            )

            keyboard = [
                [InlineKeyboardButton("✅ כן, הסר", callback_data=f"remove_{service_id}")],
                [InlineKeyboardButton("◀️ ביטול", callback_data=f"back_to_service_{service_id}")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return

        # שלב 2: ביצוע הסרה
        if data.startswith("remove_"):
            service_id = data.split("_", 1)[1]
            service = self.db.get_service_activity(service_id)
            if not service or service.get("removed") is True:
                await query.edit_message_text("❌ שירות לא נמצא")
                return

            if not self._can_remove_service(user, service):
                await query.answer("❌ אין הרשאה להסיר שירות זה", show_alert=True)
                return

            removed = False
            try:
                if user_id is None:
                    raise ValueError("missing user_id")
                removed = bool(self.db.remove_service_from_management(service_id, user_id=str(user_id)))
            except Exception as e:
                await query.edit_message_text(f"❌ שגיאה בהסרת השירות: {e}")
                return

            service_name = self._escape_markdown(str(service.get("service_name", service_id)))
            if removed:
                back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזור לניהול", callback_data="back_to_manage")]])
                await query.edit_message_text(
                    f"✅ השירות *{service_name}* הוסר מרשימת הניהול.",
                    reply_markup=back_markup,
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text("❌ שגיאה בהסרת השירות")
            return

    async def service_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מטפל בלחיצה על כפתורי השעיה/הפעלה של שירות"""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()

        data = query.data

        if data.startswith("suspend_"):
            service_id = data.replace("suspend_", "")

            # סימון פעולה ידנית במנטר הסטטוס
            status_monitor.mark_manual_action(service_id)

            try:
                self.render_api.suspend_service(service_id)
                self.db.update_service_activity(service_id, status="suspended")
                self.db.increment_suspend_count(service_id)
                await query.edit_message_text(text=f"✅ השירות {service_id} הושהה.")
            except Exception as e:
                await query.edit_message_text(text=f"❌ כישלון בהשעיית {service_id}: {e}")

        elif data.startswith("resume_"):
            service_id = data.replace("resume_", "")

            # סימון פעולה ידנית במנטר הסטטוס
            status_monitor.mark_manual_action(service_id)

            try:
                self.render_api.resume_service(service_id)
                self.db.update_service_activity(service_id, status="active")
                await query.edit_message_text(text=f"✅ השירות {service_id} הופעל מחדש.")
                # התחלת מעקב אקטיבי אחר דיפלוי בעקבות ההפעלה
                try:
                    service = self.db.get_service_activity(service_id) or {}
                    service_name = service.get("service_name", service_id)
                    status_monitor.watch_deploy_until_terminal(service_id, service_name)
                except Exception:
                    pass
            except Exception as e:
                await query.edit_message_text(text=f"❌ כישלון בהפעלת {service_id}: {e}")
        elif data == "back_to_manage":  # מטפל בכפתור "חזור"
            # מציג מחדש את תפריט הניהול בעזרת עריכת ההודעה
            await self.show_manage_menu(query)
        

    async def suspend_button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """טיפול בכפתורי אישור/ביטול השעיה כללית"""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()

        if query.data == "confirm_suspend_all":
            # השעיית כל השירותים
            suspended_count = 0
            all_services = db.get_all_services()

            for service in all_services:
                service_id = service["_id"]
                if service.get("status") != "suspended":
                    success = render_api.suspend_service(service_id)
                    if success:
                        db.update_service_activity(service_id, status="suspended")
                        suspended_count += 1

            await query.edit_message_text(f"✅ הושעו {suspended_count} שירותים", parse_mode="Markdown")
        else:
            # ביטול - חזרה לתפריט הניהול
            await self.show_manage_menu(query)

    async def monitor_detail_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        service_id_override: Optional[str] = None,
    ):
        """הצגת פרטי שירות וכפתורי ניהול ניטור"""
        query = update.callback_query
        if query is None:
            return
        if service_id_override is None and query.data is None:
            return
        await query.answer()

        service_id = service_id_override or (query.data or "").replace("monitor_detail_", "")

        # קבלת מידע על השירות
        service = self.db.get_service_activity(service_id)
        if not service:
            await query.edit_message_text("❌ שירות לא נמצא")
            return

        service_name = service.get("service_name", service_id)
        monitoring_status = status_monitor.get_monitoring_status(service_id)
        is_monitored = monitoring_status.get("enabled", False)
        deploy_notifications = self.db.get_deploy_notification_status(service_id)

        message = f"🤖 *{service_name}*\n"
        message += f"🆔 `{service_id}`\n\n"

        # סטטוס ניטור
        if is_monitored:
            message += "✅ *ניטור פעיל*\n"
            enabled_at = monitoring_status.get("enabled_at")
            if enabled_at:
                message += f"מנוטר מאז: {enabled_at.strftime('%d/%m/%Y')}\n"
        else:
            message += "❌ *ניטור כבוי*\n"

        # סטטוס התראות דיפלוי
        if deploy_notifications:
            message += "🚀 *התראות דיפלוי: מופעלות*\n"
        else:
            message += "🔇 *התראות דיפלוי: כבויות*\n"

        # סטטוס נוכחי (חי)
        simplified_status = self._simplified_status_live_or_db(service)
        status_emoji = self._status_to_emoji(simplified_status)
        message += f"\nסטטוס נוכחי: {status_emoji} {simplified_status}\n"

        # כפתורים
        keyboard = []

        if is_monitored:
            keyboard.append([InlineKeyboardButton("🔕 כבה ניטור", callback_data=f"disable_monitor_{service_id}")])
        else:
            keyboard.append([InlineKeyboardButton("🔔 הפעל ניטור", callback_data=f"enable_monitor_{service_id}")])

        # כפתור התראות דיפלוי
        if deploy_notifications:
            keyboard.append([InlineKeyboardButton("🔇 כבה התראות דיפלוי", callback_data=f"disable_deploy_notif_{service_id}")])
        else:
            keyboard.append([InlineKeyboardButton("🚀 הפעל התראות דיפלוי", callback_data=f"enable_deploy_notif_{service_id}")])

        keyboard.append([InlineKeyboardButton("🔙 חזור לרשימה", callback_data="back_to_monitor_list")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    async def monitor_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """טיפול בפעולות ניטור"""
        query = update.callback_query
        if query is None or query.data is None:
            return

        data = query.data
        user = query.from_user
        if user is None:
            return
        user_id = user.id

        if data.startswith("enable_monitor_"):
            service_id = data.replace("enable_monitor_", "")

            if status_monitor.enable_monitoring(service_id, user_id):
                await query.answer("✅ ניטור הופעל בהצלחה!", show_alert=True)
                # רענון התצוגה ללא שינוי query.data
                await self.monitor_detail_callback(update, context, service_id_override=service_id)
            else:
                await query.answer("❌ שגיאה בהפעלת ניטור", show_alert=True)

        elif data.startswith("disable_monitor_"):
            service_id = data.replace("disable_monitor_", "")

            if status_monitor.disable_monitoring(service_id, user_id):
                await query.answer("✅ ניטור כובה בהצלחה!", show_alert=True)
                # רענון התצוגה ללא שינוי query.data
                await self.monitor_detail_callback(update, context, service_id_override=service_id)
            else:
                await query.answer("❌ שגיאה בכיבוי ניטור", show_alert=True)

        elif data == "back_to_monitor_list":
            # חזרה לרשימה הראשית
            await query.answer()
            await self.refresh_monitor_manage(query)

        elif data == "refresh_monitor_manage":
            await query.answer()
            await self.refresh_monitor_manage(query)

        elif data == "show_monitored_only":
            await query.answer()
            await self.show_monitored_only(query)

        elif data.startswith("enable_deploy_notif_"):
            service_id = data.replace("enable_deploy_notif_", "")
            self.db.toggle_deploy_notifications(service_id, True)
            await query.answer("🚀 התראות דיפלוי הופעלו בהצלחה!", show_alert=True)
            # רענון התצוגה ללא שינוי query.data
            await self.monitor_detail_callback(update, context, service_id_override=service_id)
            # הפעל לולאת ניטור אם לא רצה כדי שנאתר אירועי דיפלוי
            try:
                status_monitor.start_monitoring()
            except Exception:
                pass

        elif data.startswith("disable_deploy_notif_"):
            service_id = data.replace("disable_deploy_notif_", "")
            self.db.toggle_deploy_notifications(service_id, False)
            await query.answer("🔇 התראות דיפלוי כבויות בהצלחה!", show_alert=True)
            # רענון התצוגה ללא שינוי query.data
            await self.monitor_detail_callback(update, context, service_id_override=service_id)

    async def refresh_monitor_manage(self, query: CallbackQuery):
        """רענון רשימת הניטור"""
        # קבלת רשימת השירותים
        services = self._get_visible_services()

        if not services:
            await query.edit_message_text("📭 אין שירותים במערכת")
            return

        # יצירת כפתורים
        keyboard = []

        for service in services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)

            # בדיקה אם הניטור מופעל
            monitoring_status = status_monitor.get_monitoring_status(service_id)
            is_monitored = monitoring_status.get("enabled", False)

            # סטטוס נוכחי (חי)
            status_emoji = self._get_status_emoji_for_service(service)

            # אימוג'י ניטור
            monitor_emoji = "👁️" if is_monitored else "👁️‍🗨️"

            # טקסט הכפתור
            button_text = f"{status_emoji} {monitor_emoji} {service_name[:20]}"

            row = [InlineKeyboardButton(button_text, callback_data=f"monitor_detail_{service_id}")]
            keyboard.append(row)

        # כפתור לרשימת המנוטרים
        keyboard.append([InlineKeyboardButton("📊 הצג רק מנוטרים", callback_data="show_monitored_only")])

        # כפתור רענון
        keyboard.append([InlineKeyboardButton("🔄 רענן", callback_data="refresh_monitor_manage")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        message = "🎛️ *ניהול ניטור סטטוס*\n\n"
        message += "👁️ = בניטור | 👁️‍🗨️ = לא בניטור\n"
        message += "🟢 = פעיל | 🔴 = כבוי | 🟡 = לא ידוע\n\n"
        message += "בחר שירות לניהול:"

        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    async def show_monitored_only(self, query: CallbackQuery):
        """הצגת רק שירותים מנוטרים"""
        monitored_services = status_monitor.get_all_monitored_services()

        if not monitored_services:
            await query.answer("אין שירותים בניטור", show_alert=True)
            return

        keyboard = []

        for service in monitored_services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)
            # סטטוס נוכחי (חי)
            status_emoji = self._get_status_emoji_for_service(service)

            button_text = f"{status_emoji} 👁️ {service_name[:20]}"

            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"monitor_detail_{service_id}")])

        keyboard.append([InlineKeyboardButton("🔙 הצג הכל", callback_data="refresh_monitor_manage")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        message = "👁️ *שירותים בניטור פעיל*\n\n"
        message += f'סה"כ {len(monitored_services)} שירותים בניטור\n\n'
        message += "בחר שירות לניהול:"

        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    async def test_monitor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודת בדיקה לסימולציית שינויי סטטוס"""
        msg = update.message
        if msg is None:
            return
        if not context.args:
            message = "🧪 *פקודת בדיקת ניטור*\n\n"
            message += "שימוש: `/test_monitor [service_id] [action]`\n\n"
            message += "*פעולות אפשריות:*\n"
            message += "• `online` - סימולציה שהשירות עלה\n"
            message += "• `offline` - סימולציה שהשירות ירד\n"
            message += "• `deploy_ok` - סימולציה: פריסה ואז עלייה (אם התראות דיפלוי מופעלות)\n"
            message += "• `cycle` - מחזור מלא (ירידה ואז עלייה)\n\n"
            message += "*דוגמה:*\n"
            message += "`/test_monitor srv-123456 offline`"
            await msg.reply_text(message, parse_mode="Markdown")
            return

        service_id = context.args[0]
        action = context.args[1] if len(context.args) > 1 else "cycle"

        # בדיקה אם השירות קיים
        service = self.db.get_service_activity(service_id)
        if not service:
            await msg.reply_text(f"❌ שירות {service_id} לא נמצא במערכת")
            return

        service_name = service.get("service_name", service_id)

        # בדיקה אם הניטור מופעל
        monitoring_status = status_monitor.get_monitoring_status(service_id)
        if not monitoring_status.get("enabled", False):
            await msg.reply_text(
                f"⚠️ ניטור לא מופעל עבור {service_name}\n" f"הפעל ניטור תחילה עם: `/monitor {service_id}`",
                parse_mode="Markdown",
            )
            return

        # קבלת הסטטוס הנוכחי
        current_status = service.get("last_known_status", "unknown")

        await msg.reply_text(f"🧪 מתחיל בדיקה עבור {service_name}...")

        if action == "online":
            # סימולציה של עלייה
            if current_status == "online":
                # אם כבר online, קודם נוריד ואז נעלה
                await self._simulate_status_change(service_id, "online", "offline")
                await asyncio.sleep(2)
                await self._simulate_status_change(service_id, "offline", "online")
                await msg.reply_text(
                    "✅ סימולציה הושלמה:\n"
                    "1️⃣ השירות ירד (offline)\n"
                    "2️⃣ השירות עלה (online)\n\n"
                    "🔔 אם הניטור פעיל, אמורת לקבל 2 התראות"
                )
            else:
                await self._simulate_status_change(service_id, current_status, "online")
                await msg.reply_text("✅ סימולציה הושלמה:\n" "השירות עלה (online)\n\n" "🔔 אם הניטור פעיל, אמורת לקבל התראה")

        elif action == "offline":
            # סימולציה של ירידה
            if current_status == "offline":
                # אם כבר offline, קודם נעלה ואז נוריד
                await self._simulate_status_change(service_id, "offline", "online")
                await asyncio.sleep(2)
                await self._simulate_status_change(service_id, "online", "offline")
                await msg.reply_text(
                    "✅ סימולציה הושלמה:\n"
                    "1️⃣ השירות עלה (online)\n"
                    "2️⃣ השירות ירד (offline)\n\n"
                    "🔔 אם הניטור פעיל, אמורת לקבל 2 התראות"
                )
            else:
                await self._simulate_status_change(service_id, current_status, "offline")
                await msg.reply_text("✅ סימולציה הושלמה:\n" "השירות ירד (offline)\n\n" "🔔 אם הניטור פעיל, אמורת לקבל התראה")

        elif action == "cycle":
            # מחזור מלא
            statuses = ["offline", "online", "offline", "online"]
            previous = current_status

            message = "🔄 מבצע מחזור בדיקה מלא...\n\n"

            for i, new_status in enumerate(statuses, 1):
                await self._simulate_status_change(service_id, previous, new_status)
                message += f"{i}️⃣ {previous} ➡️ {new_status}\n"
                previous = new_status
                await asyncio.sleep(2)  # המתנה בין שינויים

            await msg.reply_text(f"✅ מחזור בדיקה הושלם!\n\n{message}\n" f"🔔 אמורת לקבל {len(statuses)} התראות")
        elif action == "deploy_ok":
            # בדיקת דגל התראות דיפלוי
            deploy_enabled = self.db.get_deploy_notification_status(service_id)
            steps = ["deploying", "online"]
            previous = current_status if current_status else "offline"
            for new_status in steps:
                await self._simulate_status_change(service_id, previous, new_status)
                previous = new_status
                await asyncio.sleep(1)
            if deploy_enabled:
                await msg.reply_text("✅ סימולציית דיפלוי הסתיימה. אמור להתקבל עדכון 'סיום פריסה'.")
            else:
                await msg.reply_text("ℹ️ התראות דיפלוי כבויות לשירות זה, לא אמורה לצאת התראת 'סיום פריסה'. הפעל דרך המסך.")
        else:
            await msg.reply_text(f"❌ פעולה לא מוכרת: {action}\n" "השתמש ב: online, offline, או cycle")

    async def _simulate_status_change(self, service_id: str, old_status: str, new_status: str):
        """סימולציה של שינוי סטטוס"""
        # עדכון הסטטוס במסד הנתונים
        self.db.update_service_status(service_id, new_status)
        self.db.record_status_change(service_id, old_status, new_status)

        # קבלת מידע על השירות
        service = self.db.get_service_activity(service_id)
        service_name = service.get("service_name", service_id)

        # שליחת התראה אם השינוי משמעותי (כולל דיפלוי כאשר מופעל לשירות)
        if status_monitor._is_significant_change(old_status, new_status, service_id):
            # שליחת ההתראה האמיתית לפי הלוגיקה של המנטר
            status_monitor._send_status_notification(service_id, service_name, old_status, new_status)

            # בנוסף, שליחת הודעת בדיקה קצרה לצורך ויזואליזציה
            from notifications import send_notification

            emoji = "🟢" if new_status == "online" else "🔴" if new_status == "offline" else "🟡"
            test_message = f"{emoji} *התראת בדיקה - שינוי סטטוס*\n\n"
            test_message += "🧪 זוהי הודעת בדיקה!\n\n"
            safe_name = str(service_name).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
            safe_id = str(service_id).replace("`", "\\`")
            safe_old = str(old_status).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
            safe_new = str(new_status).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
            test_message += f"🤖 השירות: *{safe_name}*\n"
            test_message += f"🆔 ID: `{safe_id}`\n"
            test_message += f"⬅️ סטטוס קודם: {safe_old}\n"
            test_message += f"➡️ סטטוס חדש: {safe_new}\n"
            send_notification(test_message)

    # ===== פקודות ניטור לוגים =====

    async def logs_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """צפייה בלוגים של שירות"""
        msg = update.message
        if msg is None:
            return
        
        if not context.args:
            await msg.reply_text(
                "❌ חסר service ID\n\n"
                "**שימוש:**\n"
                "`/logs [service_id] [lines] [minutes] [filter]`\n\n"
                "**פרמטרים:**\n"
                "• `lines` - כמה שורות להציג (ברירת מחדל: 100, מקס: 200)\n"
                "• `minutes` - מכמה דקות אחורה (אופציונלי)\n"
                "• `filter` - סינון: `all`, `errors`, `stdout`, `stderr` (אופציונלי)\n\n"
                "**דוגמאות:**\n"
                "`/logs srv-123456` - 100 שורות אחרונות (הכל)\n"
                "`/logs srv-123456 50` - 50 שורות אחרונות\n"
                "`/logs srv-123456 100 5` - 100 שורות מה-5 דקות האחרונות\n"
                "`/logs srv-123456 100 5 errors` - רק שגיאות מ-5 דקות 🔥\n"
                "`/logs srv-123456 50 - errors` - רק שגיאות (50 אחרונות) 🔥\n"
                "`/logs srv-123456 100 - stdout` - רק STDOUT\n\n"
                "**קיצורי דרך:**\n"
                "`/errors srv-123456 [lines] [minutes]` - רק שגיאות\n\n"
                "💡 **טיפ:** השורות מוצגות מהישן לחדש (כרונולוגית)",
                parse_mode="Markdown"
            )
            return

        service_id = context.args[0]
        lines = int(context.args[1]) if len(context.args) > 1 else 100
        
        # פרמטר minutes יכול להיות מספר או "-" (skip)
        minutes_arg = context.args[2] if len(context.args) > 2 else None
        minutes = int(minutes_arg) if minutes_arg and minutes_arg != "-" else None
        
        # פרמטר filter (all/errors/stdout/stderr)
        filter_type = context.args[3].lower() if len(context.args) > 3 else "all"

        # בדיקה אם השירות קיים
        service = self.db.get_service_activity(service_id)
        service_name = service.get("service_name", service_id) if service else service_id

        # ודא שהשירות קיים ב-Render, כדי להבדיל בין "אין לוגים" ל"שירות לא נמצא"
        try:
            service_info = self.render_api.get_service_info(service_id)
        except Exception:
            service_info = None
        if not service_info:
            await msg.reply_text(
                f"❌ השירות לא נמצא ב-Render או שה-ID שגוי\n\n"
                f"בדוק את המזהה ונסה שוב: `{service_id}`",
                parse_mode="Markdown",
            )
            return

        # הודעת סטטוס
        time_range = f"מה-{minutes} דקות האחרונות" if minutes else "הכי אחרונים"
        filter_text = {
            "errors": "שגיאות בלבד 🔥",
            "stdout": "STDOUT בלבד",
            "stderr": "STDERR בלבד",
            "all": "הכל"
        }.get(filter_type, "הכל")
        
        await msg.reply_text(
            f"📋 מביא {lines} לוגים {time_range}\n"
            f"🤖 שירות: *{service_name}*\n"
            f"🔍 סינון: {filter_text}",
            parse_mode="Markdown"
        )

        try:
            # קבלת הלוגים
            if minutes:
                # לוגים מטווח זמן ספציפי
                logs = self.render_api.get_recent_logs(service_id, minutes=minutes)
                # הגבלה למספר השורות המבוקש
                logs = logs[-lines:] if len(logs) > lines else logs

                # אם לא התקבלו לוגים בטווח – בצע נפילה חכמה לאחרונים בכלל
                if not logs:
                    try:
                        await msg.reply_text("ℹ️ לא נמצאו לוגים בטווח הזמן המבוקש – מציג האחרונות מכל הזמן")
                    except Exception:
                        pass
                    logs = self.render_api.get_service_logs(service_id, tail=min(lines, 200))
            else:
                # לוגים אחרונים (ברירת מחדל)
                logs = self.render_api.get_service_logs(service_id, tail=min(lines, 200))
            
            if not logs:
                # נסה אסטרטגיות נוספות לפני הודעת ריקנות
                try:
                    alt_logs = []
                    # 1) נסה טווח זמן של 15 דקות באמצעות האלגוריתם הלוגי
                    alt_logs = self.render_api.get_recent_logs(service_id, minutes=15)
                    if not alt_logs:
                        # 2) נסה להביא יותר שורות אחרונות (עד 1000)
                        alt_logs = self.render_api.get_service_logs(service_id, tail=min(1000, max(lines, 200)))
                    if alt_logs:
                        logs = alt_logs[-lines:] if len(alt_logs) > lines else alt_logs
                except Exception:
                    pass

            if not logs:
                await msg.reply_text(
                    "📭 לא נמצאו לוגים לשירות זה כרגע\n\n"
                    "אפשרויות להמשך:\n"
                    f"• נסה להרחיב טווח: `/logs {service_id} {lines} 15`\n"
                    f"• ודא שיש פעילות בשירות (שולח פלט)\n"
                    f"• אם מדובר בסביבת Free ייתכן שיש פחות שימור לוגים",
                    parse_mode="Markdown",
                )
                return

            # סינון לפי הבקשה
            if filter_type == "errors":
                # זיהוי שגיאות באמצעות patterns (כמו ב-log_monitor)
                error_patterns = [
                    r'(?i)\berror\b', r'(?i)\bexception\b', r'(?i)\bfailed\b',
                    r'(?i)\bcrash\b', r'(?i)\bfatal\b', r'(?i)traceback',
                    r'\b[45]\d{2}\b', r'(?i)uncaught', r'(?i)unhandled'
                ]
                filtered_logs = []
                for log in logs:
                    text = log.get("text", "")
                    # בדיקה אם זה STDERR או מכיל pattern של שגיאה
                    if log.get("stream") == "stderr":
                        filtered_logs.append(log)
                    else:
                        for pattern in error_patterns:
                            if re.search(pattern, text):
                                filtered_logs.append(log)
                                break
                logs = filtered_logs
                
                if not logs:
                    await msg.reply_text("✅ מצוין! לא נמצאו שגיאות בתקופה זו 🎉")
                    return
                    
            elif filter_type == "stdout":
                logs = [log for log in logs if log.get("stream") == "stdout"]
                if not logs:
                    await msg.reply_text("📭 לא נמצאו לוגי STDOUT")
                    return
                    
            elif filter_type == "stderr":
                logs = [log for log in logs if log.get("stream") == "stderr"]
                if not logs:
                    await msg.reply_text("✅ אין STDERR - אין שגיאות!")
                    return

            # פיצול ללוגים של stdout ו-stderr (אחרי סינון)
            stdout_logs = [log for log in logs if log.get("stream") == "stdout"]
            stderr_logs = [log for log in logs if log.get("stream") == "stderr"]

            # הצגת הלוגים (מוגבל לתווים בטלגרם)
            message = f"📋 *לוגים של {service_name}*\n\n"
            
            # הוספת מידע על טווח זמן
            if logs and len(logs) > 0:
                first_log = logs[0]
                last_log = logs[-1]
                first_time = first_log.get("timestamp", "")
                last_time = last_log.get("timestamp", "")
                
                if first_time and last_time:
                    # המרה לפורמט קריא
                    from datetime import datetime
                    try:
                        first_dt = datetime.fromisoformat(first_time.replace('Z', '+00:00'))
                        last_dt = datetime.fromisoformat(last_time.replace('Z', '+00:00'))
                        message += f"🕐 טווח: {first_dt.strftime('%H:%M:%S')} - {last_dt.strftime('%H:%M:%S')}\n"
                    except:
                        pass
            
            message += f"📊 סה\"כ: {len(logs)} שורות"
            if minutes:
                message += f" (מה-{minutes} דקות האחרונות)"
            if filter_type != "all":
                message += f" | 🔍 סינון: {filter_text}"
            message += "\n\n"
            
            if stderr_logs:
                message += "🔴 *STDERR (שגיאות):*\n"
                for log in stderr_logs[-10:]:  # 10 אחרונים
                    text = log.get("text", "")[:200]
                    text = text.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
                    message += f"```\n{text}\n```\n"
                
                if len(message) > 3500:
                    await msg.reply_text(message[:3500] + "\n\n_...קיצור בגלל הגבלת אורך_", parse_mode="Markdown")
                    message = f"\n\n📝 *STDOUT (פלט רגיל):*\n"
                else:
                    message += f"\n\n📝 *STDOUT (פלט רגיל):*\n"

            for log in stdout_logs[-10:]:  # 10 אחרונים
                if len(message) > 3500:
                    break
                text = log.get("text", "")[:200]
                text = text.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
                message += f"```\n{text}\n```\n"

            message += f"\n\n💡 **עצות:**\n"
            message += f"• הקש `/logs_monitor {service_id}` להפעלת ניטור אוטומטי\n"
            if not minutes:
                message += f"• הוסף פרמטר זמן: `/logs {service_id} 100 5` (5 דקות אחרונות)\n"
            message += f"• השורות מוצגות מהישן לחדש (כרונולוגית)"

            await msg.reply_text(message, parse_mode="Markdown")

        except Exception as e:
            await msg.reply_text(f"❌ שגיאה בקבלת לוגים: {e}")

    async def errors_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """קיצור דרך לצפייה רק בשגיאות"""
        msg = update.message
        if msg is None:
            return
        
        if not context.args:
            await msg.reply_text(
                "🔥 *צפייה בשגיאות בלבד*\n\n"
                "**שימוש:**\n"
                "`/errors [service_id] [lines] [minutes]`\n\n"
                "**דוגמאות:**\n"
                "`/errors srv-123456` - 100 שגיאות אחרונות\n"
                "`/errors srv-123456 50` - 50 שגיאות אחרונות\n"
                "`/errors srv-123456 100 5` - שגיאות מ-5 דקות אחרונות\n\n"
                "💡 זה קיצור דרך ל: `/logs [id] [lines] [min] errors`",
                parse_mode="Markdown"
            )
            return
        
        # הוסף "errors" לסוף הפרמטרים
        new_args = list(context.args)
        
        # אם יש פחות מ-3 פרמטרים, הוסף "-" למילוי
        while len(new_args) < 3:
            if len(new_args) == 1:
                new_args.append("100")  # ברירת מחדל ל-lines
            elif len(new_args) == 2:
                new_args.append("-")  # דלג על minutes
        
        new_args.append("errors")
        context.args = new_args
        
        # קרא לפונקציה הרגילה
        await self.logs_command(update, context)

    async def logs_monitor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הפעלת ניטור לוגים לשירות"""
        msg = update.message
        if msg is None:
            return
        
        if not context.args:
            await msg.reply_text(
                "❌ חסר service ID\n\n"
                "שימוש: `/logs_monitor [service_id] [threshold]`\n\n"
                "threshold = מספר שגיאות להתראה (ברירת מחדל: 5)\n"
                "דוגמה: `/logs_monitor srv-123456 3`",
                parse_mode="Markdown"
            )
            return

        service_id = context.args[0]
        threshold = int(context.args[1]) if len(context.args) > 1 else 5

        user = update.effective_user
        if user is None:
            return
        user_id = user.id

        # הפעלת הניטור
        if log_monitor.enable_monitoring(service_id, user_id, error_threshold=threshold):
            await msg.reply_text(
                f"✅ ניטור לוגים הופעל עבור השירות\n"
                f"🔍 סף שגיאות: {threshold}\n\n"
                f"תקבל התראה כאשר יזוהו {threshold}+ שגיאות בדקה"
            )
            # הפעל את לולאת הניטור אם לא רצה
            try:
                log_monitor.start_monitoring()
            except Exception:
                pass
        else:
            await msg.reply_text(
                f"❌ לא הצלחתי להפעיל ניטור לוגים עבור {service_id}\n"
                f"ודא שה-ID נכון ושהשירות קיים ב-Render"
            )

    async def logs_unmonitor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """כיבוי ניטור לוגים לשירות"""
        msg = update.message
        if msg is None:
            return
        
        if not context.args:
            await msg.reply_text("❌ חסר service ID\nשימוש: /logs_unmonitor [service_id]")
            return

        service_id = context.args[0]
        user = update.effective_user
        if user is None:
            return
        user_id = user.id

        # כיבוי הניטור
        if log_monitor.disable_monitoring(service_id, user_id):
            await msg.reply_text(f"✅ ניטור לוגים כובה עבור השירות {service_id}")
        else:
            await msg.reply_text(f"❌ לא הצלחתי לכבות ניטור לוגים עבור {service_id}")

    async def logs_manage_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ניהול ניטור לוגים דרך פקודה"""
        msg = update.message
        if msg is None:
            return
        
        services = self.db.get_all_services()

        if not services:
            await msg.reply_text("📭 אין שירותים במערכת")
            return

        keyboard = []

        for service in services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)

            # בדיקה אם ניטור לוגים מופעל
            log_monitoring = service.get("log_monitoring", {})
            is_monitored = log_monitoring.get("enabled", False)
            
            # אימוג'י ניטור
            monitor_emoji = "🔍" if is_monitored else "💤"
            
            # התראה אם היו שגיאות לאחרונה
            last_error_count = log_monitoring.get("last_error_count", 0)
            error_emoji = "🔥" if last_error_count > 0 else ""

            button_text = f"{monitor_emoji} {error_emoji} {service_name[:20]}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"log_detail_{service_id}")])

        # כפתורים נוספים
        keyboard.append([InlineKeyboardButton("📊 הצג רק מנוטרים", callback_data="show_logs_monitored_only")])
        keyboard.append([InlineKeyboardButton("🔄 רענן", callback_data="refresh_logs_manage")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        message = "🎛️ *ניהול ניטור לוגים*\n\n"
        message += "🔍 = ניטור פעיל | 💤 = ניטור כבוי\n"
        message += "🔥 = שגיאות זוהו לאחרונה\n\n"
        message += "בחר שירות לניהול:"

        await msg.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    async def logs_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """טיפול בפעולות ניטור לוגים"""
        query = update.callback_query
        if query is None or query.data is None:
            return

        data = query.data
        user = query.from_user
        if user is None:
            return
        user_id = user.id

        if data.startswith("enable_log_monitor_"):
            service_id = data.replace("enable_log_monitor_", "")

            if log_monitor.enable_monitoring(service_id, user_id):
                await query.answer("✅ ניטור לוגים הופעל!", show_alert=True)
                # רענון התצוגה
                await self._show_log_detail(query, service_id)
            else:
                await query.answer("❌ שגיאה בהפעלת ניטור לוגים", show_alert=True)

        elif data.startswith("disable_log_monitor_"):
            service_id = data.replace("disable_log_monitor_", "")

            if log_monitor.disable_monitoring(service_id, user_id):
                await query.answer("✅ ניטור לוגים כובה!", show_alert=True)
                # רענון התצוגה
                await self._show_log_detail(query, service_id)
            else:
                await query.answer("❌ שגיאה בכיבוי ניטור לוגים", show_alert=True)

        elif data.startswith("log_detail_"):
            service_id = data.replace("log_detail_", "")
            await query.answer()
            await self._show_log_detail(query, service_id)

        elif data == "back_to_logs_list":
            await query.answer()
            await self._refresh_logs_manage(query)

        elif data == "refresh_logs_manage":
            await query.answer()
            await self._refresh_logs_manage(query)

        elif data == "show_logs_monitored_only":
            await query.answer()
            await self._show_logs_monitored_only(query)

    async def _show_log_detail(self, query: CallbackQuery, service_id: str):
        """הצגת פרטי ניטור לוגים של שירות"""
        service = self.db.get_service_activity(service_id)
        if not service:
            await query.edit_message_text("❌ שירות לא נמצא")
            return

        service_name = service.get("service_name", service_id)
        log_monitoring = service.get("log_monitoring", {})
        is_monitored = log_monitoring.get("enabled", False)
        
        message = f"🤖 *{service_name}*\n"
        message += f"🆔 `{service_id}`\n\n"

        if is_monitored:
            message += "✅ *ניטור לוגים פעיל*\n"
            threshold = log_monitoring.get("error_threshold", 5)
            message += f"🎯 סף שגיאות: {threshold}\n"
            
            last_error_count = log_monitoring.get("last_error_count", 0)
            if last_error_count > 0:
                message += f"🔥 שגיאות אחרונות: {last_error_count}\n"
                last_was_critical = log_monitoring.get("last_was_critical", False)
                if last_was_critical:
                    message += "⚠️ *שגיאה קריטית זוהתה!*\n"
            
            total_errors = log_monitoring.get("total_errors", 0)
            message += f"📊 סה\"כ שגיאות: {total_errors}\n"
        else:
            message += "❌ *ניטור לוגים כבוי*\n"

        # כפתורים
        keyboard = []

        if is_monitored:
            keyboard.append([InlineKeyboardButton("🔇 כבה ניטור", callback_data=f"disable_log_monitor_{service_id}")])
        else:
            keyboard.append([InlineKeyboardButton("🔍 הפעל ניטור", callback_data=f"enable_log_monitor_{service_id}")])

        keyboard.append([InlineKeyboardButton("🔙 חזור לרשימה", callback_data="back_to_logs_list")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    async def _refresh_logs_manage(self, query: CallbackQuery):
        """רענון רשימת ניטור לוגים"""
        services = self.db.get_all_services()

        if not services:
            await query.edit_message_text("📭 אין שירותים במערכת")
            return

        keyboard = []

        for service in services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)

            log_monitoring = service.get("log_monitoring", {})
            is_monitored = log_monitoring.get("enabled", False)
            
            monitor_emoji = "🔍" if is_monitored else "💤"
            
            last_error_count = log_monitoring.get("last_error_count", 0)
            error_emoji = "🔥" if last_error_count > 0 else ""

            button_text = f"{monitor_emoji} {error_emoji} {service_name[:20]}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"log_detail_{service_id}")])

        keyboard.append([InlineKeyboardButton("📊 הצג רק מנוטרים", callback_data="show_logs_monitored_only")])
        keyboard.append([InlineKeyboardButton("🔄 רענן", callback_data="refresh_logs_manage")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        message = "🎛️ *ניהול ניטור לוגים*\n\n"
        message += "🔍 = ניטור פעיל | 💤 = ניטור כבוי\n"
        message += "🔥 = שגיאות זוהו לאחרונה\n\n"
        message += "בחר שירות לניהול:"

        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    async def _show_logs_monitored_only(self, query: CallbackQuery):
        """הצגת רק שירותים עם ניטור לוגים פעיל"""
        monitored_services = log_monitor.get_all_monitored_services()

        if not monitored_services:
            await query.answer("אין שירותים עם ניטור לוגים פעיל", show_alert=True)
            return

        keyboard = []

        for service in monitored_services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)
            
            log_monitoring = service.get("log_monitoring", {})
            last_error_count = log_monitoring.get("last_error_count", 0)
            error_emoji = "🔥" if last_error_count > 0 else ""

            button_text = f"🔍 {error_emoji} {service_name[:20]}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"log_detail_{service_id}")])

        keyboard.append([InlineKeyboardButton("🔙 הצג הכל", callback_data="refresh_logs_manage")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        message = "🔍 *שירותים עם ניטור לוגים פעיל*\n\n"
        message += f'סה"כ {len(monitored_services)} שירותים\n\n'
        message += "בחר שירות לניהול:"

        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    # ===== פקודות משתני סביבה =====

    async def env_list_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        	"""הצגת משתני סביבה של שירות (אדמין בלבד)"""
        	msg = update.message
        	if msg is None:
        		return
        	
        	user = update.effective_user
        	if not self._is_admin_user(user):
        		await msg.reply_text("❌ פקודה זו זמינה רק למנהל המערכת")
        		return
        	
        	if not context.args:
        		await msg.reply_text(
        			"❌ חסר service ID\n\n"
        			"שימוש: `/env_list [service_id]`\n\n"
        			"דוגמה: `/env_list srv-123456`",
        			parse_mode="Markdown"
        		)
        		return
        	
        	service_id = context.args[0]
        	
        	# בדיקה אם השירות קיים
        	service_info = self.render_api.get_service_info(service_id)
        	if not service_info:
        		await msg.reply_text(
        			f"❌ השירות לא נמצא ב-Render או שה-ID שגוי\n\n"
        			f"בדוק את המזהה: `{service_id}`",
        			parse_mode="Markdown"
        		)
        		return
        	
        	service_name = service_info.get("name", service_id)
        	
        	await msg.reply_text(f"📋 מביא רשימת משתני סביבה של *{service_name}*...", parse_mode="Markdown")
        	
        	try:
        		env_vars = self.render_api.get_env_vars(service_id)
        		
        		if not env_vars:
        			await msg.reply_text(
        				f"📭 לא נמצאו משתני סביבה לשירות *{service_name}*\n\n"
        				f"ייתכן שהשירות חדש או שאין הרשאות מתאימות",
        				parse_mode="Markdown"
        			)
        			return
        		
        		message = f"📝 *משתני סביבה של {service_name}*\n\n"
        		message += f"🆔 Service ID: `{service_id}`\n"
        		message += f"📊 סה\"כ משתנים: {len(env_vars)}\n\n"
        		
        		# מיון לפי שם
        		sorted_vars = sorted(env_vars, key=lambda x: x.get("key", ""))
        		
        		for env_var in sorted_vars:
        			key = env_var.get("key", "")
        			value = env_var.get("value")
        			
        			# Render לא מחזיר ערכים של משתנים סודיים
        			if value is None or value == "":
        				message += f"🔐 `{key}`: *[מוסתר/סודי]*\n"
        			else:
        				# הצגת חלק מהערך (לא הכל מסיבות אבטחה)
        				display_value = str(value)
        				if len(display_value) > 50:
        					display_value = display_value[:47] + "..."
        				# Escape characters for Markdown
        				display_value = display_value.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
        				message += f"📌 `{key}`: `{display_value}`\n"
        		
        		message += f"\n💡 **טיפים:**\n"
        		message += f"• עדכון משתנה: `/env_set {service_id} KEY value`\n"
        		message += f"• מחיקת משתנה: `/env_delete {service_id} KEY`\n"
        		message += f"⚠️ שינוי משתני סביבה עשוי לגרום לדיפלוי מחדש של השירות"
        		
        		await msg.reply_text(message, parse_mode="Markdown")
        		
        	except Exception as e:
        		await msg.reply_text(f"❌ שגיאה בקבלת משתני סביבה: {e}")

    async def env_set_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        	"""עדכון או הוספת משתנה סביבה (אדמין בלבד)"""
        	msg = update.message
        	if msg is None:
        		return
        	
        	user = update.effective_user
        	if not self._is_admin_user(user):
        		await msg.reply_text("❌ פקודה זו זמינה רק למנהל המערכת")
        		return
        	
        	if len(context.args) < 3:
        		await msg.reply_text(
        			"❌ חסרים פרמטרים\n\n"
        			"שימוש: `/env_set [service_id] [key] [value]`\n\n"
        			"דוגמאות:\n"
        			"• `/env_set srv-123456 API_KEY new_api_key_value`\n"
        			"• `/env_set srv-123456 DEBUG true`\n"
        			"• `/env_set srv-123456 DATABASE_URL postgresql://...`\n\n"
        			"⚠️ שינוי משתני סביבה עשוי לגרום לדיפלוי מחדש",
        			parse_mode="Markdown"
        		)
        		return
        	
        	service_id = context.args[0]
        	key = context.args[1]
        	# Value יכול להכיל רווחים, לכן נקח את כל השאר
        	value = " ".join(context.args[2:])
        	
        	# בדיקה אם השירות קיים
        	service_info = self.render_api.get_service_info(service_id)
        	if not service_info:
        		await msg.reply_text(
        			f"❌ השירות לא נמצא ב-Render או שה-ID שגוי\n\n"
        			f"בדוק את המזהה: `{service_id}`",
        			parse_mode="Markdown"
        		)
        		return
        	
        	service_name = service_info.get("name", service_id)
        	
        	# אישור מהמשתמש
        	confirm_message = f"⚠️ **אישור עדכון משתנה סביבה**\n\n"
        	confirm_message += f"🤖 שירות: *{service_name}*\n"
        	confirm_message += f"🆔 ID: `{service_id}`\n"
        	confirm_message += f"🔑 משתנה: `{key}`\n"
        	confirm_message += f"📝 ערך חדש: `{value[:50]}{'...' if len(value) > 50 else ''}`\n\n"
        	confirm_message += "האם אתה בטוח? פעולה זו עשויה לגרום לדיפלוי מחדש של השירות."
        	
	        keyboard = [
	        	[
	        		InlineKeyboardButton(
	        			"✅ כן, עדכן",
	        			callback_data=f"confirm_env_set_{quote(service_id, safe='')}_{quote(key, safe='')}"
	        		),
	        		InlineKeyboardButton("❌ בטל", callback_data="cancel_env_action"),
	        	]
	        ]
        	
        	# שמירת הערך בזיכרון זמני (context.user_data)
        	if context.user_data is not None:
        		context.user_data[f"env_value_{service_id}_{key}"] = value
        	
        	await msg.reply_text(
        		confirm_message,
        		reply_markup=InlineKeyboardMarkup(keyboard),
        		parse_mode="Markdown"
        	)

    async def env_delete_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        	"""מחיקת משתנה סביבה (אדמין בלבד)"""
        	msg = update.message
        	if msg is None:
        		return
        	
        	user = update.effective_user
        	if not self._is_admin_user(user):
        		await msg.reply_text("❌ פקודה זו זמינה רק למנהל המערכת")
        		return
        	
        	if len(context.args) < 2:
        		await msg.reply_text(
        			"❌ חסרים פרמטרים\n\n"
        			"שימוש: `/env_delete [service_id] [key]`\n\n"
        			"דוגמה: `/env_delete srv-123456 OLD_API_KEY`\n\n"
        			"⚠️ פעולה זו בלתי הפיכה ועשויה לגרום לדיפלוי מחדש",
        			parse_mode="Markdown"
        		)
        		return
        	
        	service_id = context.args[0]
        	key = context.args[1]
        	
        	# בדיקה אם השירות קיים
        	service_info = self.render_api.get_service_info(service_id)
        	if not service_info:
        		await msg.reply_text(
        			f"❌ השירות לא נמצא ב-Render או שה-ID שגוי\n\n"
        			f"בדוק את המזהה: `{service_id}`",
        			parse_mode="Markdown"
        		)
        		return
        	
        	service_name = service_info.get("name", service_id)
        	
        	# אישור מהמשתמש
        	confirm_message = f"⚠️ **אישור מחיקת משתנה סביבה**\n\n"
        	confirm_message += f"🤖 שירות: *{service_name}*\n"
        	confirm_message += f"🆔 ID: `{service_id}`\n"
        	confirm_message += f"🗑️ משתנה למחיקה: `{key}`\n\n"
        	confirm_message += "האם אתה בטוח? פעולה זו בלתי הפיכה ועשויה לגרום לדיפלוי מחדש."
        	
	        keyboard = [
	        	[
	        		InlineKeyboardButton(
	        			"✅ כן, מחק",
	        			callback_data=f"confirm_env_delete_{quote(service_id, safe='')}_{quote(key, safe='')}"
	        		),
	        		InlineKeyboardButton("❌ בטל", callback_data="cancel_env_action"),
	        	]
	        ]
        	
        	await msg.reply_text(
        		confirm_message,
        		reply_markup=InlineKeyboardMarkup(keyboard),
        		parse_mode="Markdown"
        	)

    async def env_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        	"""טיפול באישורי עדכון/מחיקה של משתני סביבה"""
        	query = update.callback_query
        	if query is None or query.data is None:
        		return

        	data = query.data
        	user = query.from_user
        	if user is None:
        		return

        	# בדיקת הרשאות אדמין
        	if not self._is_admin_user(user):
        		await query.answer("❌ אין הרשאה", show_alert=True)
        		return

        	if data == "cancel_env_action":
        		await query.answer()
        		await query.edit_message_text("❌ הפעולה בוטלה")
        		return

	        if data.startswith("confirm_env_set_"):
	        	# פורמט: confirm_env_set_{urlencoded(service_id)}_{urlencoded(key)}
	        	payload = data[len("confirm_env_set_"):]
	        	parts = payload.split("_", 1)
	        	if len(parts) < 2:
	        		await query.answer("❌ שגיאה בפורמט הנתונים", show_alert=True)
	        		return

	        	service_id = unquote(parts[0])
	        	key = unquote(parts[1])
        		
        		# שליפת הערך מהזיכרון הזמני
        		value_key = f"env_value_{service_id}_{key}"
        		if context.user_data is None or value_key not in context.user_data:
        			await query.answer("❌ הערך לא נמצא בזיכרון, נסה שוב", show_alert=True)
        			return
        		
        		value = context.user_data[value_key]
        		
        		await query.answer()
        		await query.edit_message_text("⏳ מעדכן משתנה סביבה...")
        		
        		# ביצוע העדכון
        		result = self.render_api.update_env_var(service_id, key, value)
        		
        		# ניקוי הזיכרון
        		del context.user_data[value_key]
        		
        		if result["success"]:
        			service_info = self.render_api.get_service_info(service_id)
        			service_name = service_info.get("name", service_id) if service_info else service_id
        			
        			message = f"✅ *עדכון מוצלח!*\n\n"
        			message += f"🤖 שירות: *{service_name}*\n"
        			message += f"🆔 ID: `{service_id}`\n"
        			message += f"🔑 משתנה: `{key}`\n"
        			message += f"✨ {result['message']}\n\n"
        			message += "⚠️ השירות עשוי לעבור דיפלוי מחדש כעת.\n"
        			message += f"💡 הקש `/env_list {service_id}` לראות את כל המשתנים"
        			
        			await query.edit_message_text(message, parse_mode="Markdown")
        		else:
        			await query.edit_message_text(
        				f"❌ *כשלון בעדכון*\n\n"
        				f"שגיאה: {result['message']}\n"
        				f"קוד: {result['status_code']}",
        				parse_mode="Markdown"
        			)

	        elif data.startswith("confirm_env_delete_"):
	        	# פורמט: confirm_env_delete_{urlencoded(service_id)}_{urlencoded(key)}
	        	payload = data[len("confirm_env_delete_"):]
	        	parts = payload.split("_", 1)
	        	if len(parts) < 2:
	        		await query.answer("❌ שגיאה בפורמט הנתונים", show_alert=True)
	        		return

	        	service_id = unquote(parts[0])
	        	key = unquote(parts[1])
        		
        		await query.answer()
        		await query.edit_message_text("⏳ מוחק משתנה סביבה...")
        		
        		# ביצוע המחיקה
        		result = self.render_api.delete_env_var(service_id, key)
        		
        		if result["success"]:
        			service_info = self.render_api.get_service_info(service_id)
        			service_name = service_info.get("name", service_id) if service_info else service_id
        			
        			message = f"✅ *מחיקה מוצלחת!*\n\n"
        			message += f"🤖 שירות: *{service_name}*\n"
        			message += f"🆔 ID: `{service_id}`\n"
        			message += f"🗑️ משתנה נמחק: `{key}`\n"
        			message += f"✨ {result['message']}\n\n"
        			message += "⚠️ השירות עשוי לעבור דיפלוי מחדש כעת.\n"
        			message += f"💡 הקש `/env_list {service_id}` לראות את כל המשתנים"
        			
        			await query.edit_message_text(message, parse_mode="Markdown")
        		else:
        			await query.edit_message_text(
        				f"❌ *כשלון במחיקה*\n\n"
        				f"שגיאה: {result['message']}\n"
        				f"קוד: {result['status_code']}",
        				parse_mode="Markdown"
        			)

    # ===== פקודות תזכורות =====

    async def remind_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """יצירת תזכורת: /remind <time> <text>

        פורמטי זמן נתמכים:
          30m  = 30 דקות
          2h   = 2 שעות
          7d   = 7 ימים
          1w   = שבוע
        """
        msg = update.message
        if msg is None:
            return

        if not context.args or len(context.args) < 2:
            await msg.reply_text(
                "❌ חסרים פרמטרים\n\n"
                "שימוש: `/remind <זמן> <טקסט>`\n\n"
                "פורמטי זמן:\n"
                "• `30m` – 30 דקות\n"
                "• `2h` – 2 שעות\n"
                "• `7d` – 7 ימים\n"
                "• `1w` – שבוע אחד\n\n"
                "דוגמאות:\n"
                "• `/remind 7d לחדש שירות API`\n"
                "• `/remind 30d להשעות את הבוט`\n"
                "• `/remind 2h לבדוק דיפלוי`",
                parse_mode="Markdown",
            )
            return

        time_str = context.args[0].lower()
        reminder_text = " ".join(context.args[1:])

        # הגבלת אורך טקסט תזכורת
        if len(reminder_text) > 500:
            await msg.reply_text("❌ טקסט התזכורת ארוך מדי (מקסימום 500 תווים)")
            return

        # פירוק זמן
        time_match = re.match(r"^(\d+)([mhdw])$", time_str)
        if not time_match:
            await msg.reply_text(
                "❌ פורמט זמן לא תקין\n\n"
                "פורמטים נתמכים: `30m`, `2h`, `7d`, `1w`\n"
                "m=דקות, h=שעות, d=ימים, w=שבועות",
                parse_mode="Markdown",
            )
            return

        amount = int(time_match.group(1))
        unit = time_match.group(2)

        # הגנה מפני ערכים גדולים מדי שגורמים ל-OverflowError ב-timedelta
        max_amounts = {"m": 525960, "h": 8766, "d": 365, "w": 52}  # ~שנה לכל יחידה
        if amount > max_amounts.get(unit, 365):
            await msg.reply_text("❌ לא ניתן ליצור תזכורת ליותר משנה")
            return

        if unit == "m":
            delta = timedelta(minutes=amount)
            unit_label = "דקות"
        elif unit == "h":
            delta = timedelta(hours=amount)
            unit_label = "שעות"
        elif unit == "d":
            delta = timedelta(days=amount)
            unit_label = "ימים"
        elif unit == "w":
            delta = timedelta(weeks=amount)
            unit_label = "שבועות"
        else:
            await msg.reply_text("❌ יחידת זמן לא מוכרת")
            return

        if delta.total_seconds() < 60:
            await msg.reply_text("❌ לא ניתן ליצור תזכורת לפחות מדקה")
            return

        if delta.total_seconds() > 365 * 24 * 3600:
            await msg.reply_text("❌ לא ניתן ליצור תזכורת ליותר משנה")
            return

        remind_at = datetime.now(timezone.utc) + delta
        user = update.effective_user
        user_id = user.id if user else 0
        chat_id = msg.chat_id

        reminder_id = self.db.create_reminder(
            user_id=user_id,
            text=reminder_text,
            remind_at=remind_at,
            chat_id=chat_id,
        )

        # פורמט תאריך בשעון ישראלי
        import pytz
        tz_il = pytz.timezone("Asia/Jerusalem")
        remind_at_local = remind_at.astimezone(tz_il)
        date_str = remind_at_local.strftime("%d/%m/%Y %H:%M")

        safe_text = str(reminder_text).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
        await msg.reply_text(
            f"✅ תזכורת נוצרה בהצלחה!\n\n"
            f"📌 *{safe_text}*\n"
            f"⏰ תישלח בעוד {amount} {unit_label}\n"
            f"📅 ({date_str})\n\n"
            f"🆔 מזהה: `{reminder_id}`",
            parse_mode="Markdown",
        )

    async def reminders_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הצגת כל התזכורות הפעילות: /reminders"""
        msg = update.message
        if msg is None:
            return

        user = update.effective_user
        if user is None:
            return

        reminders = self.db.get_user_reminders(user.id)

        if not reminders:
            await msg.reply_text("📭 אין תזכורות פעילות\n\nהקש `/remind` ליצירת תזכורת חדשה", parse_mode="Markdown")
            return

        import pytz
        tz_il = pytz.timezone("Asia/Jerusalem")

        message = f"⏰ *התזכורות שלך ({len(reminders)}):*\n\n"
        for i, reminder in enumerate(reminders, 1):
            remind_at = reminder["remind_at"]
            if remind_at.tzinfo is None:
                remind_at = remind_at.replace(tzinfo=timezone.utc)
            remind_at_local = remind_at.astimezone(tz_il)
            date_str = remind_at_local.strftime("%d/%m/%Y %H:%M")

            # חישוב הזמן שנותר
            now = datetime.now(timezone.utc)
            remaining = remind_at - now
            if remaining.total_seconds() <= 0:
                time_remaining = "ממתינה לשליחה"
            elif remaining.days > 0:
                time_remaining = f"בעוד {remaining.days} ימים"
            elif remaining.seconds >= 3600:
                time_remaining = f"בעוד {remaining.seconds // 3600} שעות"
            else:
                time_remaining = f"בעוד {remaining.seconds // 60} דקות"

            safe_text = str(reminder["text"]).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
            entry = f"{i}. 📌 {safe_text}\n"
            entry += f"   📅 {date_str} ({time_remaining})\n"
            entry += f"   🆔 `{reminder['_id']}`\n\n"

            # פיצול הודעות אם חורגים ממגבלת טלגרם (4096 תווים)
            if len(message) + len(entry) > 3900:
                await msg.reply_text(message, parse_mode="Markdown")
                message = ""
            message += entry

        message += "למחיקת תזכורת: `/delete_reminder <id>`"
        await msg.reply_text(message, parse_mode="Markdown")

    async def delete_reminder_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מחיקת תזכורת: /delete_reminder <id>"""
        msg = update.message
        if msg is None:
            return

        user = update.effective_user
        if user is None:
            return

        if not context.args:
            await msg.reply_text(
                "❌ חסר מזהה תזכורת\n\n"
                "שימוש: `/delete_reminder <id>`\n\n"
                "הקש `/reminders` לראות את כל התזכורות שלך",
                parse_mode="Markdown",
            )
            return

        reminder_id_str = context.args[0]
        try:
            from bson import ObjectId
            ObjectId(reminder_id_str)  # וידוא שזה ID תקין
        except Exception:
            await msg.reply_text("❌ מזהה תזכורת לא תקין")
            return

        deleted = self.db.delete_reminder(reminder_id_str, user.id)
        if deleted:
            await msg.reply_text("✅ התזכורת נמחקה בהצלחה")
        else:
            await msg.reply_text("❌ התזכורת לא נמצאה או שאינה שייכת לך")


# ✨ פונקציה שמטפלת בשגיאות
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """לוכד את כל השגיאות ושולח אותן ללוג."""
    logger = logging.getLogger(__name__)
    if isinstance(context.error, Conflict):
        # יש מופע נוסף שרץ עם אותו token. כדי למנוע ריקות/בלגן — נסגור את התהליך הנוכחי
        logger.warning("⚠️ Conflict error detected: another bot instance is running. Exiting this instance.")
        try:
            # נסיון לשחרר נעילה לפני יציאה שקטה
            db.db.locks.delete_one({"_id": LOCK_ID})
        except Exception:
            pass
        sys.exit(0)

    # עבור כל שגיאה אחרת, מדפיסים את המידע המלא
    logging.error("❌ Exception while handling an update:", exc_info=context.error)


def check_and_send_reminders():
    """בדיקה ושליחת תזכורות שהגיע זמנן"""
    from notifications import send_reminder_notification

    MAX_SEND_ATTEMPTS = 5

    try:
        pending = db.get_pending_reminders()
        for reminder in pending:
            chat_id = str(reminder.get("chat_id", ""))
            text = reminder.get("text", "")
            attempts = reminder.get("send_attempts", 0)

            if not chat_id or not text or attempts >= MAX_SEND_ATTEMPTS:
                # תזכורת לא תקינה או חרגה מנסיונות — נסמן כנשלחה
                db.mark_reminder_sent(reminder["_id"])
                if attempts >= MAX_SEND_ATTEMPTS:
                    logging.warning(f"Reminder {reminder['_id']} abandoned after {attempts} failed attempts")
                continue

            # נסה לשלוח הודעה פרטית למשתמש תחילה, ואם נכשל — לצ'אט המקורי
            user_id = str(reminder.get("user_id", ""))
            sent = False
            if user_id and user_id != chat_id:
                sent = send_reminder_notification(user_id, text)
            if not sent:
                sent = send_reminder_notification(chat_id, text)
            if sent:
                db.mark_reminder_sent(reminder["_id"])
                logging.info(f"Reminder sent: {reminder['_id']}")
            else:
                # עדכון מונה נסיונות כדי למנוע ניסיונות אינסופיים
                db.increment_reminder_attempts(reminder["_id"])
                logging.warning(f"Failed to send reminder: {reminder['_id']} (attempt {attempts + 1})")
    except Exception as e:
        logging.error(f"Error checking reminders: {e}")


def run_scheduler():
    """הרצת המתזמן ברקע"""
    # בדיקה יומית בשעה 09:00
    schedule.every().day.at("09:00").do(activity_tracker.check_inactive_services)

    # דוח יומי בשעה 20:00
    schedule.every().day.at("20:00").do(send_daily_report)

    # בדיקת תזכורות כל דקה
    schedule.every(1).minutes.do(check_and_send_reminders)

    while True:
        schedule.run_pending()
        time.sleep(60)  # בדיקה כל דקה


def main():
    """פונקציה ראשית"""
    manage_mongo_lock()
    print("🚀 מפעיל בוט ניטור Render...")

    # בדיקת הגדרות חיוניות
    # nosec B105 - placeholder in config for local development, not a secret
    fatal = False
    if not config.TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN == "your_telegram_bot_token_here":  # nosec B105
        print("❌ חסר TELEGRAM_BOT_TOKEN בקובץ .env")
        fatal = True

    if not config.ADMIN_CHAT_ID or config.ADMIN_CHAT_ID == "your_admin_chat_id_here":
        print("❌ חסר ADMIN_CHAT_ID בקובץ .env")
        fatal = True

    if not config.RENDER_API_KEY or config.RENDER_API_KEY == "your_render_api_key_here":
        print("❌ חסר RENDER_API_KEY בקובץ .env")
        fatal = True

    if fatal:
        # נמשיך להריץ כדי שהבוט ינסה להדפיס עוד דיאגנוסטיקות/לוגים
        print("⚠️ ממשיך לרוץ במצב דיאגנוסטיקה למרות חסרים בהגדרות…")

    # תאימות לאחור: ודא ששירותים מה-config נשמרים במסד כדי שיופיעו ב-/status וב-/manage
    try:
        seed_services = list(getattr(config, "SERVICES_TO_MONITOR", []) or [])
        if seed_services:
            owner_for_seed = None
            if config.ADMIN_CHAT_ID and config.ADMIN_CHAT_ID != "your_admin_chat_id_here":
                owner_for_seed = str(config.ADMIN_CHAT_ID)
            inserted_count = db.ensure_services_exist(seed_services, owner_id=owner_for_seed)
            print(f"INFO: Seeded/ensured {inserted_count} services from config.SERVICES_TO_MONITOR")
    except Exception as e:
        print(f"WARNING: Failed to seed SERVICES_TO_MONITOR into MongoDB: {e}")

    # יצירת בוט
    bot = RenderMonitorBot()
    bot.app.add_error_handler(error_handler)  # רישום מטפל השגיאות

    # הפעלת המתזמן ברקע
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # הפעלת ניטור סטטוס תמידית; אם לא רוצים — ניתן לכבות ע"י אי-הפעלת שירותים
    try:
        status_monitor.start_monitoring()
        print("✅ ניטור סטטוס הופעל")
    except Exception as e:
        print(f"❌ שגיאה בהפעלת ניטור סטטוס: {e}")

    # הפעלת ניטור לוגים
    try:
        log_monitor.start_monitoring()
        print("✅ ניטור לוגים הופעל")
    except Exception as e:
        print(f"❌ שגיאה בהפעלת ניטור לוגים: {e}")

    # שליחת התראת הפעלה
    try:
        send_startup_notification()
    except Exception as e:
        print(f"⚠️ לא הצלחתי לשלוח התראת הפעלה: {e}")

    # בדיקה ראשונית
    print("מבצע בדיקה ראשונית...")
    try:
        activity_tracker.check_inactive_services()
    except Exception as e:
        print(f"⚠️ שגיאה בבדיקה ראשונית: {e}")

    # דיאגנוסטיקה אוטומטית בהפעלה
    if getattr(config, "DIAG_ON_START", False):
        try:
            from database import db

            monitored = db.get_status_monitored_services()
            deploy_enabled = db.get_services_with_deploy_notifications_enabled()
            print("=== DIAG ON START ===")
            print(f"Monitor thread alive: {bool(status_monitor.monitoring_thread and status_monitor.monitoring_thread.is_alive())}")
            print(f"Check interval: {status_monitor.deploy_check_interval if status_monitor.deploying_active else status_monitor.check_interval}s")
            print(f"Monitored services: {len(monitored)} | Deploy alerts: {len(deploy_enabled)}")
            print(f"SERVICES_TO_MONITOR fallback: {len(getattr(config, 'SERVICES_TO_MONITOR', []))}")
            print("======================")
        except Exception as e:
            print(f"⚠️ DIAG_ON_START failed: {e}")

    print("✅ הבוט פועל! לחץ Ctrl+C להפסקה")

    # הפעלת הבוט
    bot.app.run_polling()


if __name__ == "__main__":
    main()
