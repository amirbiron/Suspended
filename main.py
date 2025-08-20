import asyncio
import atexit
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

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
from status_monitor import status_monitor  # New import

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

    async def setup_bot_commands(self, app: Application):
        """הגדרת תפריט הפקודות בטלגרם (מורץ לאחר אתחול האפליקציה)"""
        from telegram import BotCommand

        commands = [
            BotCommand("start", "🚀 הפעלת הבוט"),
            BotCommand("status", "📊 סטטוס כל השירותים"),
            BotCommand("manage", "🎛️ ניהול שירותים"),
            BotCommand("monitor_manage", "👁️ ניהול ניטור סטטוס"),
            BotCommand("suspend", "⏸️ השעיית כל השירותים"),
            BotCommand("resume", "▶️ החזרת שירותים מושעים"),
            BotCommand("list_suspended", "📋 רשימת מושעים"),
            BotCommand("list_monitored", "👁️ רשימת מנוטרים"),
            BotCommand("monitor", "🔔 הפעלת ניטור סטטוס"),
            BotCommand("unmonitor", "🔕 כיבוי ניטור סטטוס"),
            BotCommand("test_monitor", "🧪 בדיקת ניטור"),
            BotCommand("help", "❓ עזרה ומידע"),
        ]

        # הגדרת הפקודות בבוט לאחר שהלולאה פעילה
        await app.bot.set_my_commands(commands)

    def setup_handlers(self):
        """הוספת command handlers"""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("status", self.status_command))
        self.app.add_handler(CommandHandler("manage", self.manage_command))
        self.app.add_handler(CommandHandler("suspend", self.suspend_command))
        self.app.add_handler(CommandHandler("resume", self.resume_command))
        self.app.add_handler(CommandHandler("list_suspended", self.list_suspended_command))
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
            CallbackQueryHandler(self.suspend_button_callback, pattern="^confirm_suspend_all|^cancel_suspend$")
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
/suspend - השעיית כל השירותים
/resume - החזרת כל השירותים המושעים
/list_suspended - רשימת שירותים מושעים
/manage - ניהול שירותים עם כפתורים

*פקודות ניטור סטטוס:*
/monitor [service_id] - הפעלת ניטור סטטוס לשירות
/unmonitor [service_id] - כיבוי ניטור סטטוס לשירות
/monitor_manage - ניהול ניטור עם כפתורים
/list_monitored - רשימת שירותים בניטור סטטוס
/test_monitor [service_id] [action] - בדיקת התראות
/clear_test_data - ניקוי נתוני בדיקות
/diag - דיאגנוסטיקה מהירה

/help - הצגת הודעה זו
        """
        msg = update.message
        if msg is None:
            return
        await msg.reply_text(help_text, parse_mode="Markdown")

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
        services = self.db.get_all_services()

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
        services = self.db.get_all_services()

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
            status = service.get("status", "active")

            # אימוג'י לפי סטטוס
            if status == "suspended":
                emoji = "🔴"
            else:
                emoji = "🟢"

            # שם מקוצר אם ארוך מדי
            display_name = service_name[:25] + "..." if len(service_name) > 25 else service_name

            keyboard.append([InlineKeyboardButton(f"{emoji} {display_name}", callback_data=f"manage_{service_id}")])

        # כפתור השעיה כללית
        keyboard.append([InlineKeyboardButton("⏸️ השעה הכל", callback_data="suspend_all")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        message = "🎛️ *ניהול שירותים*\n\n"
        message += "🟢 = פעיל | 🔴 = מושעה\n\n"
        message += "בחר שירות לניהול או פעולה כללית:"

        await msg.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")

    async def show_manage_menu(self, query: CallbackQuery):
        """מציג את תפריט הניהול בהודעה קיימת (עריכה)"""
        services = self.db.get_all_services()

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
            status = service.get("status", "active")

            # אימוג'י לפי סטטוס
            if status == "suspended":
                emoji = "🔴"
            else:
                emoji = "🟢"

            # שם מקוצר אם ארוך מדי
            display_name = service_name[:25] + "..." if len(service_name) > 25 else service_name

            keyboard.append([InlineKeyboardButton(f"{emoji} {display_name}", callback_data=f"manage_{service_id}")])

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
        service = self.db.get_service_activity(service_id)

        if not service:
            await query.edit_message_text("❌ שירות לא נמצא")
            return

        service_name = service.get("service_name", service_id)
        status = service.get("status", "active")

        # בניית תפריט לשירות
        keyboard = []

        if status == "suspended":
            keyboard.append([InlineKeyboardButton("▶️ הפעל מחדש", callback_data=f"resume_{service_id}")])
        else:
            keyboard.append([InlineKeyboardButton("⏸️ השעה", callback_data=f"suspend_{service_id}")])

        keyboard.append([InlineKeyboardButton("🔙 חזור", callback_data="back_to_manage")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        message = f"🤖 *{service_name}*\n"
        message += f"🆔 `{service_id}`\n"
        message += f"📊 סטטוס: {'🔴 מושעה' if status == 'suspended' else '🟢 פעיל'}\n\n"
        message += "בחר פעולה:"

        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")

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
        services = self.db.get_all_services()

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

            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"monitor_detail_{service_id}")])

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


def run_scheduler():
    """הרצת המתזמן ברקע"""
    # בדיקה יומית בשעה 09:00
    schedule.every().day.at("09:00").do(activity_tracker.check_inactive_services)

    # דוח יומי בשעה 20:00
    schedule.every().day.at("20:00").do(send_daily_report)

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
