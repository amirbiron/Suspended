import asyncio
import schedule
import time
import threading
import os
import sys
import atexit
import requests

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
        self.app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
        self.db = db
        self.render_api = render_api
        self.setup_handlers()
        self.setup_bot_commands()  # Add bot commands setup
        
    def setup_bot_commands(self):
        """הגדרת תפריט הפקודות בטלגרם"""
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
            BotCommand("test_monitor", "🧪 בדיקת התראות ניטור"),
            BotCommand("help", "❓ עזרה"),
        ]
        
        # Set the commands asynchronously
        async def set_commands():
            await self.app.bot.set_my_commands(commands)
            print("✅ תפריט פקודות הוגדר בהצלחה")
        
        # Run the async function
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If loop is already running, create a task
                asyncio.create_task(set_commands())
            else:
                # If loop is not running, run until complete
                loop.run_until_complete(set_commands())
        except Exception as e:
            print(f"⚠️ לא הצלחתי להגדיר תפריט פקודות: {e}")
        
    def setup_handlers(self):
        """הוספת command handlers"""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("status", self.status_command))
        self.app.add_handler(CommandHandler("suspend", self.suspend_command))
        self.app.add_handler(CommandHandler("resume", self.resume_command))
        self.app.add_handler(CommandHandler("list_suspended", self.list_suspended_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("suspend_one", self.suspend_one_command))
        self.app.add_handler(CommandHandler("manage", self.manage_command))
        
        # New status monitoring commands
        self.app.add_handler(CommandHandler("monitor", self.monitor_command))
        self.app.add_handler(CommandHandler("unmonitor", self.unmonitor_command))
        self.app.add_handler(CommandHandler("list_monitored", self.list_monitored_command))
        self.app.add_handler(CommandHandler("status_history", self.status_history_command))
        self.app.add_handler(CommandHandler("monitor_manage", self.monitor_manage_command)) # New handler
        self.app.add_handler(CommandHandler("test_monitor", self.test_monitor_command))  # Test command
        self.app.add_handler(CommandHandler("check_config", self.check_config_command))  # Config check command
        
        self.app.add_handler(CallbackQueryHandler(self.manage_service_callback, pattern="^manage_|^go_to_monitor_manage$|^suspend_all$"))
        self.app.add_handler(CallbackQueryHandler(self.service_action_callback, pattern="^suspend_|^resume_|^back_to_manage$"))
        self.app.add_handler(CallbackQueryHandler(self.suspend_button_callback, pattern="^confirm_suspend_all|^cancel_suspend$"))
        self.app.add_handler(CallbackQueryHandler(self.monitor_detail_callback, pattern="^monitor_detail_"))
        self.app.add_handler(CallbackQueryHandler(self.monitor_action_callback, pattern="^enable_monitor_|^disable_monitor_|^back_to_monitor_list|^refresh_monitor_manage|^show_monitored_only|^full_history_"))
    
    async def check_config_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """בדיקת תקינות הגדרות הבוט"""
        logger = logging.getLogger(__name__)
        logger.info("Running configuration check")
        
        message = "🔧 *בדיקת הגדרות הבוט*\n\n"
        
        # בדיקת TELEGRAM_BOT_TOKEN
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_BOT_TOKEN != "your_telegram_bot_token_here":
            message += "✅ TELEGRAM_BOT_TOKEN מוגדר\n"
            # נסיון לקבל מידע על הבוט
            try:
                url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe"
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    bot_info = response.json().get('result', {})
                    bot_name = bot_info.get('username', 'Unknown')
                    message += f"   └─ בוט: @{bot_name}\n"
                else:
                    message += "   └─ ⚠️ הטוקן לא תקין\n"
            except:
                message += "   └─ ⚠️ לא ניתן לאמת את הטוקן\n"
        else:
            message += "❌ TELEGRAM_BOT_TOKEN לא מוגדר\n"
        
        # בדיקת ADMIN_CHAT_ID
        if config.ADMIN_CHAT_ID and config.ADMIN_CHAT_ID != "your_admin_chat_id_here":
            message += f"✅ ADMIN_CHAT_ID מוגדר: `{config.ADMIN_CHAT_ID}`\n"
            
            # נסיון לשלוח הודעת בדיקה
            if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_BOT_TOKEN != "your_telegram_bot_token_here":
                from notifications import send_notification
                test_result = send_notification("🔔 הודעת בדיקה - הגדרות תקינות!")
                if test_result:
                    message += "   └─ ✅ הודעת בדיקה נשלחה בהצלחה\n"
                else:
                    message += "   └─ ❌ נכשל בשליחת הודעת בדיקה\n"
        else:
            message += "❌ ADMIN_CHAT_ID לא מוגדר\n"
            message += "   └─ 💡 השתמש ב-/start בצ'אט פרטי עם הבוט כדי לקבל את ה-Chat ID שלך\n"
        
        # בדיקת RENDER_API_KEY
        if config.RENDER_API_KEY and config.RENDER_API_KEY != "your_render_api_key_here":
            message += "✅ RENDER_API_KEY מוגדר\n"
            # נסיון להתחבר ל-Render API
            try:
                from render_api import render_api
                services = render_api.get_all_services()
                if services is not None:
                    message += f"   └─ ✅ חיבור ל-Render API תקין ({len(services)} שירותים)\n"
                else:
                    message += "   └─ ❌ לא ניתן להתחבר ל-Render API\n"
            except Exception as e:
                message += f"   └─ ❌ שגיאה בחיבור ל-Render API: {str(e)}\n"
        else:
            message += "❌ RENDER_API_KEY לא מוגדר\n"
        
        # בדיקת MongoDB
        message += "\n*מסד נתונים:*\n"
        try:
            service_count = self.db.services.count_documents({})
            monitored_count = self.db.services.count_documents({"status_monitoring.enabled": True})
            message += f"✅ MongoDB מחובר\n"
            message += f"   ├─ שירותים במערכת: {service_count}\n"
            message += f"   └─ שירותים בניטור: {monitored_count}\n"
        except Exception as e:
            message += f"❌ בעיה בחיבור ל-MongoDB: {str(e)}\n"
        
        # בדיקת ניטור סטטוס
        message += "\n*ניטור סטטוס:*\n"
        if status_monitor.monitoring_thread and status_monitor.monitoring_thread.is_alive():
            message += "✅ שרשור ניטור פעיל\n"
            message += f"   └─ בודק כל {config.STATUS_CHECK_INTERVAL_SECONDS} שניות\n"
        else:
            message += "❌ שרשור ניטור לא פעיל\n"
        
        # הצגת Chat ID של המשתמש הנוכחי
        user_chat_id = str(update.effective_chat.id)
        message += f"\n📝 *ה-Chat ID שלך:* `{user_chat_id}`\n"
        
        if user_chat_id != config.ADMIN_CHAT_ID:
            message += "⚠️ שים לב: ה-Chat ID שלך שונה מה-ADMIN_CHAT_ID המוגדר\n"
            message += "כדי לקבל התראות, עדכן את ADMIN_CHAT_ID למספר הזה\n"
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
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
/suspend_one [service_id] - השעיית שירות ספציפי
/resume - החזרת כל השירותים המושעים
/list_suspended - רשימת שירותים מושעים
/manage - ניהול שירותים עם כפתורים

*פקודות ניטור סטטוס:*
/monitor [service_id] - הפעלת ניטור סטטוס לשירות
/unmonitor [service_id] - כיבוי ניטור סטטוס לשירות
/list_monitored - רשימת שירותים בניטור
/monitor_manage - ניהול ניטור עם כפתורים
/test_monitor [service_id] [action] - בדיקת התראות

*פקודות אבחון:*
/check_config - בדיקת הגדרות הבוט והתראות
/status_history [service_id] - היסטוריית שינויי סטטוס

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
    
    async def status_history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הצגת היסטוריית שינויי סטטוס של שירות"""
        if not context.args:
            await update.message.reply_text("❌ חסר service ID\nשימוש: /status_history [service_id]")
            return
        
        service_id = context.args[0]
        
        # קבלת היסטוריה
        history = db.get_status_history(service_id, limit=10)
        
        if not history:
            await update.message.reply_text(f"📭 אין היסטוריית שינויי סטטוס עבור {service_id}")
            return
        
        # קבלת שם השירות
        service = db.get_service_activity(service_id)
        service_name = service.get("service_name", service_id) if service else service_id
        
        message = f"📊 *היסטוריית סטטוס - {service_name}*\n\n"
        
        for change in history:
            old_status = change.get("old_status", "unknown")
            new_status = change.get("new_status", "unknown")
            timestamp = change.get("timestamp")
            
            # אימוג'י לשינוי
            if new_status == "online":
                emoji = "🟢"
            elif new_status == "offline":
                emoji = "🔴"
            else:
                emoji = "🟡"
            
            if timestamp:
                try:
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    time_str = timestamp.strftime("%d/%m %H:%M")
                except:
                    time_str = "לא ידוע"
            else:
                time_str = "לא ידוע"
            
            message += f"{emoji} {time_str}: {old_status} ➡️ {new_status}\n"
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
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
        """מטפל בלחיצה על כפתורי האישור להשעיה"""
        query = update.callback_query
        await query.answer()

        if query.data == "confirm_suspend_all":
            await query.edit_message_text(text="מאשר... מתחיל בתהליך השעיה כללי.")
            services = self.db.get_all_services()
            suspended_count = 0
            for service in services:
                if service.get("status") != "suspended":
                    try:
                        service_id = service['_id']
                        # סימון פעולה ידנית במנטר הסטטוס
                        status_monitor.mark_manual_action(service_id)
                        
                        self.render_api.suspend_service(service_id)
                        self.db.update_service_activity(service_id, status="suspended")
                        self.db.increment_suspend_count(service_id)
                        suspended_count += 1
                    except Exception as e:
                        print(f"Could not suspend service {service['_id']}: {e}")
            
            await query.edit_message_text(text=f"✅ הושלם. {suspended_count} שירותים הושהו.")

        elif query.data == "cancel_suspend":
            await query.edit_message_text(text="הפעולה בוטלה.")

    async def monitor_manage_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ניהול ניטור סטטוס עם כפתורים אינטראקטיביים"""
        # קבלת רשימת השירותים
        services = self.db.get_all_services()
        
        if not services:
            await update.message.reply_text("📭 אין שירותים במערכת")
            return
        
        # יצירת כפתורים
        keyboard = []
        
        for service in services:
            service_id = service["_id"]
            service_name = service.get("service_name", service_id)
            
            # בדיקה אם הניטור מופעל
            monitoring_status = status_monitor.get_monitoring_status(service_id)
            is_monitored = monitoring_status.get("enabled", False)
            
            # סטטוס נוכחי
            current_status = service.get("last_known_status", "unknown")
            status_emoji = "🟢" if current_status == "online" else "🔴" if current_status == "offline" else "🟡"
            
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
        
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def monitor_detail_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הצגת פרטי שירות וכפתורי ניהול ניטור"""
        query = update.callback_query
        await query.answer()
        
        service_id = query.data.replace("monitor_detail_", "")
        
        # קבלת מידע על השירות
        service = self.db.get_service_activity(service_id)
        if not service:
            await query.edit_message_text("❌ שירות לא נמצא")
            return
        
        service_name = service.get("service_name", service_id)
        monitoring_status = status_monitor.get_monitoring_status(service_id)
        is_monitored = monitoring_status.get("enabled", False)
        
        # קבלת היסטוריה אחרונה
        history = self.db.get_status_history(service_id, limit=3)
        
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
        
        # סטטוס נוכחי
        current_status = service.get("last_known_status", "unknown")
        status_emoji = "🟢" if current_status == "online" else "🔴" if current_status == "offline" else "🟡"
        message += f"\nסטטוס נוכחי: {status_emoji} {current_status}\n"
        
        # היסטוריה אחרונה
        if history:
            message += "\n📊 *שינויים אחרונים:*\n"
            for change in history[:3]:
                old_status = change.get("old_status", "?")
                new_status = change.get("new_status", "?")
                timestamp = change.get("timestamp")
                if timestamp:
                    time_str = timestamp.strftime("%d/%m %H:%M")
                    message += f"• {time_str}: {old_status}→{new_status}\n"
        
        # כפתורים
        keyboard = []
        
        if is_monitored:
            keyboard.append([
                InlineKeyboardButton("🔕 כבה ניטור", callback_data=f"disable_monitor_{service_id}")
            ])
            keyboard.append([
                InlineKeyboardButton("📜 היסטוריה מלאה", callback_data=f"full_history_{service_id}")
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("🔔 הפעל ניטור", callback_data=f"enable_monitor_{service_id}")
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
        await query.answer()
        
        data = query.data
        user_id = query.from_user.id
        
        if data.startswith("enable_monitor_"):
            service_id = data.replace("enable_monitor_", "")
            
            if status_monitor.enable_monitoring(service_id, user_id):
                await query.answer("✅ ניטור הופעל", show_alert=True)
                # רענון התצוגה
                query.data = f"monitor_detail_{service_id}"
                await self.monitor_detail_callback(update, context)
            else:
                await query.answer("❌ שגיאה בהפעלת ניטור", show_alert=True)
        
        elif data.startswith("disable_monitor_"):
            service_id = data.replace("disable_monitor_", "")
            
            if status_monitor.disable_monitoring(service_id, user_id):
                await query.answer("✅ ניטור כובה", show_alert=True)
                # רענון התצוגה
                query.data = f"monitor_detail_{service_id}"
                await self.monitor_detail_callback(update, context)
            else:
                await query.answer("❌ שגיאה בכיבוי ניטור", show_alert=True)
        
        elif data == "back_to_monitor_list":
            # חזרה לרשימה הראשית
            await self.refresh_monitor_manage(query)
        
        elif data == "refresh_monitor_manage":
            await self.refresh_monitor_manage(query)
        
        elif data == "show_monitored_only":
            await self.show_monitored_only(query)
        
        elif data.startswith("full_history_"):
            service_id = data.replace("full_history_", "")
            await self.show_full_history(query, service_id)
    
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
            
            # סטטוס נוכחי
            current_status = service.get("last_known_status", "unknown")
            status_emoji = "🟢" if current_status == "online" else "🔴" if current_status == "offline" else "🟡"
            
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
            current_status = service.get("last_known_status", "unknown")
            status_emoji = "🟢" if current_status == "online" else "🔴" if current_status == "offline" else "🟡"
            
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
    
    async def show_full_history(self, query, service_id: str):
        """הצגת היסטוריה מלאה"""
        history = self.db.get_status_history(service_id, limit=20)
        service = self.db.get_service_activity(service_id)
        service_name = service.get("service_name", service_id) if service else service_id
        
        message = f"📊 *היסטוריית סטטוס - {service_name}*\n\n"
        
        if not history:
            message += "אין היסטוריית שינויים"
        else:
            for change in history:
                old_status = change.get("old_status", "unknown")
                new_status = change.get("new_status", "unknown")
                timestamp = change.get("timestamp")
                
                emoji = "🟢" if new_status == "online" else "🔴" if new_status == "offline" else "🟡"
                
                if timestamp:
                    time_str = timestamp.strftime("%d/%m %H:%M")
                    message += f"{emoji} {time_str}: {old_status}→{new_status}\n"
        
        keyboard = [[
            InlineKeyboardButton("🔙 חזור", callback_data=f"monitor_detail_{service_id}")
        ]]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
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
            message += "• `cycle` - מחזור מלא (ירידה ואז עלייה)\n\n"
            message += "*דוגמה:*\n"
            message += "`/test_monitor srv-123456 offline`"
            await update.message.reply_text(message, parse_mode='Markdown')
            return
        
        service_id = context.args[0]
        action = context.args[1] if len(context.args) > 1 else "cycle"
        
        # לוג של הפעולה
        logger = logging.getLogger(__name__)
        logger.info(f"test_monitor_command called with service_id={service_id}, action={action}")
        
        # בדיקה אם השירות קיים
        service = self.db.get_service_activity(service_id)
        if not service:
            # נסיון ליצור רשומה בסיסית לשירות לצורך הבדיקה
            logger.warning(f"Service {service_id} not found in database, creating temporary entry for testing")
            await update.message.reply_text(
                f"⚠️ שירות {service_id} לא נמצא במערכת.\n"
                f"יוצר רשומה זמנית לצורך הבדיקה..."
            )
            
            # יצירת רשומה זמנית
            self.db.services.insert_one({
                "_id": service_id,
                "service_name": f"Test Service {service_id[:8]}",
                "status": "active",
                "last_known_status": "online",  # מתחילים עם סטטוס ידוע במקום unknown
                "created_at": datetime.now(timezone.utc),
                "is_test": True  # סימון שזו רשומה לבדיקה
            })
            
            service = self.db.get_service_activity(service_id)
            if not service:
                await update.message.reply_text(f"❌ לא הצלחתי ליצור רשומה זמנית עבור {service_id}")
                return
        
        service_name = service.get("service_name", service_id)
        
        # בדיקה אם הניטור מופעל
        monitoring_status = status_monitor.get_monitoring_status(service_id)
        if not monitoring_status.get("enabled", False):
            # הפעלת ניטור אוטומטית לצורך הבדיקה
            logger.info(f"Monitoring not enabled for {service_id}, enabling it for test")
            await update.message.reply_text(
                f"⚠️ ניטור לא מופעל עבור {service_name}\n"
                f"מפעיל ניטור אוטומטית לצורך הבדיקה..."
            )
            
            user_id = update.effective_user.id
            if not status_monitor.enable_monitoring(service_id, user_id):
                await update.message.reply_text(f"❌ לא הצלחתי להפעיל ניטור עבור {service_id}")
                return
            
            await asyncio.sleep(1)  # המתנה קצרה להפעלת הניטור
        
        # בדיקת הגדרות התראות
        if not config.ADMIN_CHAT_ID or config.ADMIN_CHAT_ID == "your_admin_chat_id_here":
            await update.message.reply_text(
                "⚠️ *אזהרה: ADMIN_CHAT_ID לא מוגדר!*\n"
                "התראות לא יישלחו ללא הגדרה זו.\n"
                "הגדר את ADMIN_CHAT_ID במשתני הסביבה.",
                parse_mode='Markdown'
            )
            logger.error("ADMIN_CHAT_ID not configured properly")
        
        # קבלת הסטטוס הנוכחי
        current_status = service.get("last_known_status", "unknown")
        
        # אם הסטטוס unknown, נגדיר אותו לפי הפעולה
        if current_status == "unknown":
            if action == "online":
                # אם רוצים לסמלץ עלייה, נגדיר שהשירות כרגע offline
                current_status = "offline"
                self.db.update_service_status(service_id, "offline")
                logger.info(f"Status was unknown, setting to offline for online simulation")
            elif action == "offline":
                # אם רוצים לסמלץ ירידה, נגדיר שהשירות כרגע online
                current_status = "online"
                self.db.update_service_status(service_id, "online")
                logger.info(f"Status was unknown, setting to online for offline simulation")
            else:
                # למחזור, נתחיל מ-online
                current_status = "online"
                self.db.update_service_status(service_id, "online")
                logger.info(f"Status was unknown, setting to online for cycle simulation")
        
        await update.message.reply_text(
            f"🧪 מתחיל בדיקה עבור {service_name}...\n"
            f"📊 סטטוס נוכחי: {current_status}\n"
            f"🎯 פעולה: {action}"
        )
        
        # לוג לפני ביצוע הסימולציה
        logger.info(f"Starting simulation for {service_id}: current_status={current_status}, action={action}")
        
        if action == "online":
            # סימולציה של עלייה
            if current_status == "online":
                # אם כבר online, קודם נוריד ואז נעלה
                logger.info(f"Service {service_id} already online, simulating down then up")
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
                logger.info(f"Simulating service {service_id} going online")
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
                logger.info(f"Service {service_id} already offline, simulating up then down")
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
                logger.info(f"Simulating service {service_id} going offline")
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
                logger.info(f"Cycle step {i}: {previous} -> {new_status}")
                await self._simulate_status_change(service_id, previous, new_status)
                message += f"{i}️⃣ {previous} ➡️ {new_status}\n"
                previous = new_status
                await asyncio.sleep(2)  # המתנה בין שינויים
            
            await update.message.reply_text(
                f"✅ מחזור בדיקה הושלם!\n\n{message}\n"
                f"🔔 אמורת לקבל {len(statuses)} התראות"
            )
        else:
            await update.message.reply_text(
                f"❌ פעולה לא מוכרת: {action}\n"
                "השתמש ב: online, offline, או cycle"
            )
    
    async def _simulate_status_change(self, service_id: str, old_status: str, new_status: str):
        """סימולציה של שינוי סטטוס"""
        logger = logging.getLogger(__name__)
        logger.info(f"=== START _simulate_status_change ===")
        logger.info(f"Service ID: {service_id}")
        logger.info(f"Old Status: {old_status}")
        logger.info(f"New Status: {new_status}")
        
        # עדכון הסטטוס במסד הנתונים
        logger.info(f"Updating database status to: {new_status}")
        self.db.update_service_status(service_id, new_status)
        self.db.record_status_change(service_id, old_status, new_status)
        
        # קבלת מידע על השירות
        service = self.db.get_service_activity(service_id)
        service_name = service.get("service_name", service_id)
        logger.info(f"Service name: {service_name}")
        
        # בדיקה אם השינוי משמעותי
        logger.info(f"Checking if change is significant: {old_status} -> {new_status}")
        is_significant = status_monitor._is_significant_change(old_status, new_status)
        logger.info(f"Is significant: {is_significant}")
        
        # שליחת התראה אם השינוי משמעותי
        if is_significant:
            logger.info(f"Change IS significant, preparing notification")
            
            # יצירת אימוג'י מתאים
            if new_status == "online":
                emoji = "🟢"
                action = "עלה (בדיקה)"
            elif new_status == "offline":
                emoji = "🔴"
                action = "ירד (בדיקה)"
            else:
                emoji = "🟡"
                action = f"שינה סטטוס ל-{new_status} (בדיקה)"
            
            # שליחת התראת בדיקה
            from notifications import send_notification
            
            test_message = f"{emoji} *התראת בדיקה - שינוי סטטוס*\n\n"
            test_message += f"🧪 זוהי הודעת בדיקה!\n\n"
            test_message += f"🤖 השירות: *{service_name}*\n"
            test_message += f"🆔 ID: `{service_id}`\n"
            test_message += f"📊 הפעולה: {action}\n"
            test_message += f"⬅️ סטטוס קודם: {old_status}\n"
            test_message += f"➡️ סטטוס חדש: {new_status}\n\n"
            
            if new_status == "online":
                test_message += "✅ השירות חזר לפעילות תקינה"
            elif new_status == "offline":
                test_message += "⚠️ השירות ירד ואינו זמין"
            
            logger.info(f"Sending notification with message length: {len(test_message)}")
            logger.info(f"ADMIN_CHAT_ID configured: {config.ADMIN_CHAT_ID}")
            
            # שליחת ההתראה עם בדיקת תוצאה
            try:
                result = send_notification(test_message)
                logger.info(f"Notification send result: {result}")
                if result:
                    logger.info(f"✅ Test notification sent successfully for {service_id}")
                else:
                    logger.error(f"❌ Failed to send test notification for {service_id}")
                    logger.error(f"Check ADMIN_CHAT_ID and TELEGRAM_BOT_TOKEN configuration")
            except Exception as e:
                logger.error(f"❌ Exception while sending notification: {str(e)}")
                logger.error(f"Exception type: {type(e).__name__}")
        else:
            logger.info(f"Change is NOT significant, no notification will be sent")
        
        logger.info(f"=== END _simulate_status_change ===\n")

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
