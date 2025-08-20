import asyncio
import schedule
import time
import threading
import os
import sys
import atexit

from datetime import datetime, timezone, timedelta
from pymongo.errors import DuplicateKeyError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import Conflict

import config
from database import db
from render_api import render_api, RenderAPI
from activity_tracker import activity_tracker
from notifications import send_notification, send_startup_notification, send_daily_report
from status_monitor import status_monitor  # New import

import logging
# הגדרת לוגים - המקום הטוב ביותר הוא כאן, פעם אחת בתחילת הקובץ
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logging.getLogger("httpx").setLevel(logging.WARNING)

# --- מנגנון נעילה חדש מבוסס MongoDB ---

LOCK_ID = "render_monitor_bot_lock" # מזהה ייחודי למנעול שלנו

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
    now = datetime.now(timezone.utc) # 'now' הוא מודע לאזור זמן (aware)
    
    lock = db.db.locks.find_one({"_id": LOCK_ID})
    if lock:
        lock_time = lock.get("timestamp", now) # 'lock_time' מגיע מה-DB והוא תמים (naive)
        
        # --- התיקון נמצא כאן ---
        # אם התאריך מה-DB הוא 'תמים', אנחנו הופכים אותו ל'מודע' עם אזור זמן UTC
        if lock_time.tzinfo is None:
            lock_time = lock_time.replace(tzinfo=timezone.utc)
        
        # עכשיו שני התאריכים מודעים וניתן לבצע חישוב
        if (now - lock_time) > timedelta(hours=1):
            print(f"WARNING: Found stale MongoDB lock from {lock_time}. Overwriting.")
            db.db.locks.delete_one({"_id": LOCK_ID})
        else:
            print(f"INFO: Lock document in MongoDB exists. Another instance is running. Exiting gracefully.")
            sys.exit(0)

    try:
        db.db.locks.insert_one({
            "_id": LOCK_ID,
            "pid": pid,
            "timestamp": now
        })
        atexit.register(cleanup_mongo_lock)
        print(f"INFO: MongoDB lock acquired by process {pid}.")
    except DuplicateKeyError:
        print(f"INFO: Lock was acquired by another process just now. Exiting gracefully.")
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
            BotCommand("help", "❓ עזרה ומידע")
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
        
        # Monitor commands
        self.app.add_handler(CommandHandler("monitor", self.monitor_command))
        self.app.add_handler(CommandHandler("unmonitor", self.unmonitor_command))
        self.app.add_handler(CommandHandler("list_monitored", self.list_monitored_command))
        self.app.add_handler(CommandHandler("monitor_manage", self.monitor_manage_command)) # New handler
        self.app.add_handler(CommandHandler("test_monitor", self.test_monitor_command))  # Test command
        
        # Cleanup command for test data
        self.app.add_handler(CommandHandler("clear_test_data", self.clear_test_data_command))
        
        self.app.add_handler(CallbackQueryHandler(self.manage_service_callback, pattern="^manage_|^go_to_monitor_manage$|^suspend_all$"))
        self.app.add_handler(CallbackQueryHandler(self.service_action_callback, pattern="^suspend_|^resume_|^back_to_manage$"))
        self.app.add_handler(CallbackQueryHandler(self.suspend_button_callback, pattern="^confirm_suspend_all|^cancel_suspend$"))
        self.app.add_handler(CallbackQueryHandler(self.monitor_detail_callback, pattern="^monitor_detail_"))
        self.app.add_handler(CallbackQueryHandler(self.monitor_action_callback, pattern="^enable_monitor_|^disable_monitor_|^back_to_monitor_list|^refresh_monitor_manage|^show_monitored_only|^enable_deploy_notif_|^disable_deploy_notif_"))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודת התחלה"""
        message = "🤖 שלום! זה בוט ניטור Render\n\n"
        message += "הבוט מנטר את השירותים שלך ומשעה אותם אוטומטית במידת הצורך.\n\n"
        message += "הקש /help לרשימת פקודות"
        await update.message.reply_text(message)
    
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

/help - הצגת הודעה זו
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def monitor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הפעלת ניטור סטטוס לשירות"""
        if not context.args:
            await update.message.reply_text("❌ חסר service ID\nשימוש: /monitor [service_id]")
            return
        
        service_id = context.args[0]
        user_id = update.effective_user.id
        
        # הפעלת הניטור
        if status_monitor.enable_monitoring(service_id, user_id):
            await update.message.reply_text(
                f"✅ ניטור סטטוס הופעל עבור השירות {service_id}\n"
                f"תקבל התראות כשהשירות יעלה או ירד."
            )
        else:
            await update.message.reply_text(
                f"❌ לא הצלחתי להפעיל ניטור עבור {service_id}\n"
                f"ודא שה-ID נכון ושהשירות קיים ב-Render."
            )
    
    async def unmonitor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """כיבוי ניטור סטטוס לשירות"""
        if not context.args:
            await update.message.reply_text("❌ חסר service ID\nשימוש: /unmonitor [service_id]")
            return
        
        service_id = context.args[0]
        user_id = update.effective_user.id
        
        # כיבוי הניטור
        if status_monitor.disable_monitoring(service_id, user_id):
            await update.message.reply_text(
                f"✅ ניטור סטטוס כובה עבור השירות {service_id}"
            )
        else:
            await update.message.reply_text(
                f"❌ לא הצלחתי לכבות ניטור עבור {service_id}"
            )
    
    async def list_monitored_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הצגת רשימת שירותים בניטור סטטוס"""
        monitored_services = status_monitor.get_all_monitored_services()
        
        if not monitored_services:
            await update.message.reply_text("📭 אין שירותים בניטור סטטוס כרגע")
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
                except:
                    pass
            
            message += "\n"
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def monitor_manage_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ניהול ניטור סטטוס דרך פקודה"""
        services = self.db.get_all_services()
        
        if not services:
            await update.message.reply_text("📭 אין שירותים במערכת")
            return
        
        keyboard = []
        
        for service in services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)
            
            # סטטוס נוכחי (שאיבה חיה עם נפילה חזרה ל-DB/ניהול)
            simplified_status = None
            try:
                live_raw_status = self.render_api.get_service_status(service_id)
                if live_raw_status:
                    simplified_status = status_monitor._simplify_status(live_raw_status)
                    # עדכון ב-DB כדי לשמור עקביות לתצוגות אחרות
                    self.db.update_service_status(service_id, simplified_status)
            except Exception:
                simplified_status = None
            if not simplified_status:
                simplified_status = service.get("last_known_status")
                if not simplified_status:
                    mgmt_status = service.get("status")
                    if mgmt_status == "suspended":
                        simplified_status = "offline"
                    elif mgmt_status == "active":
                        simplified_status = "online"
                    else:
                        simplified_status = "unknown"
            status_emoji = "🟢" if simplified_status == "online" else "🔴" if simplified_status == "offline" else "🟡"
            
            # אימוג'י ניטור
            monitoring_status = status_monitor.get_monitoring_status(service_id)
            is_monitored = monitoring_status.get("enabled", False)
            monitor_emoji = "👁️" if is_monitored else "👁️‍🗨️"
            
            button_text = f"{status_emoji} {monitor_emoji} {service_name[:20]}"
            keyboard.append([
                InlineKeyboardButton(
                    button_text,
                    callback_data=f"monitor_detail_{service_id}"
                )
            ])
        
        # כפתורים נוספים
        keyboard.append([
            InlineKeyboardButton("📊 הצג רק מנוטרים", callback_data="show_monitored_only")
        ])
        keyboard.append([
            InlineKeyboardButton("🔄 רענן", callback_data="refresh_monitor_manage")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = "🎛️ *ניהול ניטור סטטוס*\n\n"
        message += "👁️ = בניטור | 👁️‍🗨️ = לא בניטור\n"
        message += "🟢 = פעיל | 🔴 = כבוי | 🟡 = לא ידוע\n\n"
        message += "בחר שירות לניהול:"
        
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    
    async def clear_test_data_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מחיקת נתוני בדיקות דמה"""
        # בדיקת הרשאות (רק אדמין)
        if str(update.effective_user.id) != config.ADMIN_CHAT_ID:
            await update.message.reply_text("❌ פקודה זו זמינה רק למנהל המערכת")
            return
        
        count = db.clear_test_data()
        await update.message.reply_text(f"✅ נמחקו {count} פעולות בדיקה\n✅ אופסו סטטוסים ונתוני פעילות של שירותים בבדיקה")
        
    
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הצגת מצב כל השירותים"""
        services = db.get_all_services()
        
        print(f"נמצאו {len(services)} שירותים במסד הנתונים לבדיקה.")
        
        if not services:
            await update.message.reply_text("אין שירותים רשומים במערכת")
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
                message += f"   פעילות אחרונה: לא ידוע\n"
            
            message += "\n"
        
        await update.message.reply_text(message, parse_mode="Markdown")
        
    
    async def suspend_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """שולח בקשת אישור להשעיית כל השירותים"""
        keyboard = [
            [
                InlineKeyboardButton("✅ כן, השעה הכל", callback_data="confirm_suspend_all"),
                InlineKeyboardButton("❌ בטל", callback_data="cancel_suspend"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "⚠️ האם אתה בטוח שברצונך להשהות את <b>כל</b> השירותים?",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        
    
    async def suspend_one_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """השעיית שירות ספציפי"""
        if not context.args:
            await update.message.reply_text("❌ חסר service ID\nשימוש: /suspend_one [service_id]")
            return
        
        service_id = context.args[0]
        
        # סימון פעולה ידנית במנטר הסטטוס
        status_monitor.mark_manual_action(service_id)
        
        try:
            self.render_api.suspend_service(service_id)
            self.db.update_service_activity(service_id, status="suspended")
            self.db.increment_suspend_count(service_id)
            await update.message.reply_text(f"✅ השירות {service_id} הושהה בהצלחה.")
            print(f"Successfully suspended service {service_id}.")
        except Exception as e:
            await update.message.reply_text(f"❌ כישלון בהשעיית השירות {service_id}.\nשגיאה: {e}")
            print(f"Failed to suspend service {service_id}. Error: {e}")
    
    async def resume_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """החזרת כל השירותים המושעים"""
        suspended_services = db.get_suspended_services()
        
        if not suspended_services:
            await update.message.reply_text("אין שירותים מושעים")
            return
        
        await update.message.reply_text("מתחיל החזרת שירותים לפעילות...")
        
        messages = []
        for service in suspended_services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)
            
            result = activity_tracker.manual_resume_service(service_id)
            
            if result["success"]:
                messages.append(f"✅ {service_name} - הוחזר לפעילות")
            else:
                messages.append(f"❌ {service_name} - כשלון: {result['message']}")
        
        response = "תוצאות החזרה לפעילות:\n\n" + "\n".join(messages)
        await update.message.reply_text(response)
    
    async def list_suspended_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """רשימת שירותים מושעים"""
        suspended_services = db.get_suspended_services()
        
        if not suspended_services:
            await update.message.reply_text("אין שירותים מושעים כרגע")
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
        
        await update.message.reply_text(message, parse_mode="Markdown")

    async def manage_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ניהול שירותים עם כפתורים אינטראקטיביים"""
        services = self.db.get_all_services()
        
        if not services:
            await update.message.reply_text("📭 אין שירותים במערכת")
            return
        
        keyboard = []
        
        # כפתור לניהול ניטור סטטוס
        keyboard.append([
            InlineKeyboardButton("👁️ ניהול ניטור סטטוס", callback_data="go_to_monitor_manage")
        ])
        
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
            
            keyboard.append([
                InlineKeyboardButton(
                    f"{emoji} {display_name}",
                    callback_data=f"manage_{service_id}"
                )
            ])
        
        # כפתור השעיה כללית
        keyboard.append([
            InlineKeyboardButton("⏸️ השעה הכל", callback_data="suspend_all")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = "🎛️ *ניהול שירותים*\n\n"
        message += "🟢 = פעיל | 🔴 = מושעה\n\n"
        message += "בחר שירות לניהול או פעולה כללית:"
        
        if isinstance(update, Update):
            await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            # אם זה callback query
            await update.edit_message_text(
                message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

    async def manage_service_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מציג אפשרויות ניהול לשירות שנבחר"""
        query = update.callback_query
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
                [InlineKeyboardButton("❌ ביטול", callback_data="cancel_suspend")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "⚠️ האם אתה בטוח שברצונך להשעות את כל השירותים?",
                reply_markup=reply_markup
            )
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
        
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def service_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מטפל בלחיצה על כפתורי השעיה/הפעלה של שירות"""
        query = update.callback_query
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
            except Exception as e:
                await query.edit_message_text(text=f"❌ כישלון בהפעלת {service_id}: {e}")
        elif data == "back_to_manage":  # מטפל בכפתור "חזור"
            # קורא מחדש לפונקציה המקורית כדי להציג את הרשימה
            await self.manage_command(update.callback_query, context)

    async def suspend_button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """טיפול בכפתורי אישור/ביטול השעיה כללית"""
        query = update.callback_query
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
            
            await query.edit_message_text(
                f"✅ הושעו {suspended_count} שירותים",
                parse_mode='Markdown'
            )
        else:
            # ביטול - חזרה לתפריט הניהול
            await self.manage_command(query, context)
    

    async def monitor_detail_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE, service_id_override: str = None):
        """הצגת פרטי שירות וכפתורי ניהול ניטור"""
        query = update.callback_query
        await query.answer()
        
        service_id = service_id_override or query.data.replace("monitor_detail_", "")
        
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
        
        # סטטוס נוכחי
        # ניסיון להביא סטטוס חי ולשמור ב-DB; נפילה חזרה לערכים קיימים אם צריך
        detail_simplified_status = None
        try:
            live_raw_status = self.render_api.get_service_status(service_id)
            if live_raw_status:
                detail_simplified_status = status_monitor._simplify_status(live_raw_status)
                self.db.update_service_status(service_id, detail_simplified_status)
        except Exception:
            detail_simplified_status = None
        if not detail_simplified_status:
            detail_simplified_status = service.get("last_known_status")
            if not detail_simplified_status:
                mgmt_status = service.get("status")
                if mgmt_status == "suspended":
                    detail_simplified_status = "offline"
                elif mgmt_status == "active":
                    detail_simplified_status = "online"
                else:
                    detail_simplified_status = "unknown"
        status_emoji = "🟢" if detail_simplified_status == "online" else "🔴" if detail_simplified_status == "offline" else "🟡"
        message += f"\nסטטוס נוכחי: {status_emoji} {detail_simplified_status}\n"
        
        # כפתורים
        keyboard = []
        
        if is_monitored:
            keyboard.append([
                InlineKeyboardButton("🔕 כבה ניטור", callback_data=f"disable_monitor_{service_id}")
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("🔔 הפעל ניטור", callback_data=f"enable_monitor_{service_id}")
            ])
        
        # כפתור התראות דיפלוי
        if deploy_notifications:
            keyboard.append([
                InlineKeyboardButton("🔇 כבה התראות דיפלוי", callback_data=f"disable_deploy_notif_{service_id}")
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("🚀 הפעל התראות דיפלוי", callback_data=f"enable_deploy_notif_{service_id}")
            ])
        
        keyboard.append([
            InlineKeyboardButton("🔙 חזור לרשימה", callback_data="back_to_monitor_list")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def monitor_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """טיפול בפעולות ניטור"""
        query = update.callback_query
        
        data = query.data
        user_id = query.from_user.id
        
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
        
        elif data.startswith("disable_deploy_notif_"):
            service_id = data.replace("disable_deploy_notif_", "")
            self.db.toggle_deploy_notifications(service_id, False)
            await query.answer("🔇 התראות דיפלוי כבויות בהצלחה!", show_alert=True)
            # רענון התצוגה ללא שינוי query.data
            await self.monitor_detail_callback(update, context, service_id_override=service_id)
    
    async def refresh_monitor_manage(self, query):
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
            
            # סטטוס נוכחי (שאיבה חיה עם נפילה חזרה ל-DB/ניהול)
            simplified_status = None
            try:
                live_raw_status = self.render_api.get_service_status(service_id)
                if live_raw_status:
                    simplified_status = status_monitor._simplify_status(live_raw_status)
                    # עדכון ב-DB כדי לשמור עקביות לתצוגות אחרות
                    self.db.update_service_status(service_id, simplified_status)
            except Exception:
                simplified_status = None
            if not simplified_status:
                simplified_status = service.get("last_known_status")
                if not simplified_status:
                    mgmt_status = service.get("status")
                    if mgmt_status == "suspended":
                        simplified_status = "offline"
                    elif mgmt_status == "active":
                        simplified_status = "online"
                    else:
                        simplified_status = "unknown"
            status_emoji = "🟢" if simplified_status == "online" else "🔴" if simplified_status == "offline" else "🟡"
            
            # אימוג'י ניטור
            monitor_emoji = "👁️" if is_monitored else "👁️‍🗨️"
            
            # טקסט הכפתור
            button_text = f"{status_emoji} {monitor_emoji} {service_name[:20]}"
            
            keyboard.append([
                InlineKeyboardButton(
                    button_text,
                    callback_data=f"monitor_detail_{service_id}"
                )
            ])
        
        # כפתור לרשימת המנוטרים
        keyboard.append([
            InlineKeyboardButton("📊 הצג רק מנוטרים", callback_data="show_monitored_only")
        ])
        
        # כפתור רענון
        keyboard.append([
            InlineKeyboardButton("🔄 רענן", callback_data="refresh_monitor_manage")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = "🎛️ *ניהול ניטור סטטוס*\n\n"
        message += "👁️ = בניטור | 👁️‍🗨️ = לא בניטור\n"
        message += "🟢 = פעיל | 🔴 = כבוי | 🟡 = לא ידוע\n\n"
        message += "בחר שירות לניהול:"
        
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def show_monitored_only(self, query):
        """הצגת רק שירותים מנוטרים"""
        monitored_services = status_monitor.get_all_monitored_services()
        
        if not monitored_services:
            await query.answer("אין שירותים בניטור", show_alert=True)
            return
        
        keyboard = []
        
        for service in monitored_services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)
            # סטטוס נוכחי (שאיבה חיה עם נפילה חזרה ל-DB/ניהול)
            simplified_status = None
            try:
                live_raw_status = self.render_api.get_service_status(service_id)
                if live_raw_status:
                    simplified_status = status_monitor._simplify_status(live_raw_status)
                    self.db.update_service_status(service_id, simplified_status)
            except Exception:
                simplified_status = None
            if not simplified_status:
                simplified_status = service.get("last_known_status")
                if not simplified_status:
                    mgmt_status = service.get("status")
                    if mgmt_status == "suspended":
                        simplified_status = "offline"
                    elif mgmt_status == "active":
                        simplified_status = "online"
                    else:
                        simplified_status = "unknown"
            status_emoji = "🟢" if simplified_status == "online" else "🔴" if simplified_status == "offline" else "🟡"
            
            button_text = f"{status_emoji} 👁️ {service_name[:20]}"
            
            keyboard.append([
                InlineKeyboardButton(
                    button_text,
                    callback_data=f"monitor_detail_{service_id}"
                )
            ])
        
        keyboard.append([
            InlineKeyboardButton("🔙 הצג הכל", callback_data="refresh_monitor_manage")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = "👁️ *שירותים בניטור פעיל*\n\n"
        message += f"סה\"כ {len(monitored_services)} שירותים בניטור\n\n"
        message += "בחר שירות לניהול:"
        
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def test_monitor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודת בדיקה לסימולציית שינויי סטטוס"""
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
            await update.message.reply_text(message, parse_mode='Markdown')
            return
        
        service_id = context.args[0]
        action = context.args[1] if len(context.args) > 1 else "cycle"
        
        # בדיקה אם השירות קיים
        service = self.db.get_service_activity(service_id)
        if not service:
            await update.message.reply_text(f"❌ שירות {service_id} לא נמצא במערכת")
            return
        
        service_name = service.get("service_name", service_id)
        
        # בדיקה אם הניטור מופעל
        monitoring_status = status_monitor.get_monitoring_status(service_id)
        if not monitoring_status.get("enabled", False):
            await update.message.reply_text(
                f"⚠️ ניטור לא מופעל עבור {service_name}\n"
                f"הפעל ניטור תחילה עם: `/monitor {service_id}`",
                parse_mode='Markdown'
            )
            return
        
        # קבלת הסטטוס הנוכחי
        current_status = service.get("last_known_status", "unknown")
        
        await update.message.reply_text(f"🧪 מתחיל בדיקה עבור {service_name}...")
        
        if action == "online":
            # סימולציה של עלייה
            if current_status == "online":
                # אם כבר online, קודם נוריד ואז נעלה
                await self._simulate_status_change(service_id, "online", "offline")
                await asyncio.sleep(2)
                await self._simulate_status_change(service_id, "offline", "online")
                await update.message.reply_text(
                    "✅ סימולציה הושלמה:\n"
                    "1️⃣ השירות ירד (offline)\n"
                    "2️⃣ השירות עלה (online)\n\n"
                    "🔔 אם הניטור פעיל, אמורת לקבל 2 התראות"
                )
            else:
                await self._simulate_status_change(service_id, current_status, "online")
                await update.message.reply_text(
                    "✅ סימולציה הושלמה:\n"
                    "השירות עלה (online)\n\n"
                    "🔔 אם הניטור פעיל, אמורת לקבל התראה"
                )
                
        elif action == "offline":
            # סימולציה של ירידה
            if current_status == "offline":
                # אם כבר offline, קודם נעלה ואז נוריד
                await self._simulate_status_change(service_id, "offline", "online")
                await asyncio.sleep(2)
                await self._simulate_status_change(service_id, "online", "offline")
                await update.message.reply_text(
                    "✅ סימולציה הושלמה:\n"
                    "1️⃣ השירות עלה (online)\n"
                    "2️⃣ השירות ירד (offline)\n\n"
                    "🔔 אם הניטור פעיל, אמורת לקבל 2 התראות"
                )
            else:
                await self._simulate_status_change(service_id, current_status, "offline")
                await update.message.reply_text(
                    "✅ סימולציה הושלמה:\n"
                    "השירות ירד (offline)\n\n"
                    "🔔 אם הניטור פעיל, אמורת לקבל התראה"
                )
                
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
            
            await update.message.reply_text(
                f"✅ מחזור בדיקה הושלם!\n\n{message}\n"
                f"🔔 אמורת לקבל {len(statuses)} התראות"
            )
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
                await update.message.reply_text("✅ סימולציית דיפלוי הסתיימה. אמור להתקבל עדכון 'סיום פריסה'.")
            else:
                await update.message.reply_text("ℹ️ התראות דיפלוי כבויות לשירות זה, לא אמורה לצאת התראת 'סיום פריסה'. הפעל דרך המסך.")
        else:
            await update.message.reply_text(
                f"❌ פעולה לא מוכרת: {action}\n"
                "השתמש ב: online, offline, או cycle"
            )
    
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
            test_message += f"🧪 זוהי הודעת בדיקה!\n\n"
            test_message += f"🤖 השירות: *{service_name}*\n"
            test_message += f"🆔 ID: `{service_id}`\n"
            test_message += f"⬅️ סטטוס קודם: {old_status}\n"
            test_message += f"➡️ סטטוס חדש: {new_status}\n"
            send_notification(test_message)

# ✨ פונקציה שמטפלת בשגיאות
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """לוכד את כל השגיאות ושולח אותן ללוג."""
    logger = logging.getLogger(__name__)
    if isinstance(context.error, Conflict):
        # מתמודד עם שגיאת הקונפליקט הנפוצה בשקט יחסי
        logger.warning("⚠️ Conflict error detected, likely another bot instance is running. Ignoring.")
        return  # יוצאים מהפונקציה כדי לא להדפיס את כל השגיאה הארוכה
    
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
    if not config.TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN == "your_telegram_bot_token_here":
        print("❌ חסר TELEGRAM_BOT_TOKEN בקובץ .env")
        return
    
    if not config.RENDER_API_KEY or config.RENDER_API_KEY == "your_render_api_key_here":
        print("❌ חסר RENDER_API_KEY בקובץ .env")
        return
    
    if not config.SERVICES_TO_MONITOR:
        print("❌ לא מוגדרים שירותים לניטור ב-config.py")
        return
    
    # יצירת בוט
    bot = RenderMonitorBot()
    bot.app.add_error_handler(error_handler)  # רישום מטפל השגיאות
    
    # הפעלת המתזמן ברקע
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    # הפעלת ניטור סטטוס אם מופעל בהגדרות
    if config.STATUS_MONITORING_ENABLED:
        status_monitor.start_monitoring()
        print("✅ ניטור סטטוס הופעל")
    
    # שליחת התראת הפעלה
    send_startup_notification()
    
    # בדיקה ראשונית
    print("מבצע בדיקה ראשונית...")
    activity_tracker.check_inactive_services()
    
    print("✅ הבוט פועל! לחץ Ctrl+C להפסקה")
    
    # הפעלת הבוט
    bot.app.run_polling()

if __name__ == "__main__":
    main()
