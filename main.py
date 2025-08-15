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
from state_monitor import state_monitor
from ci_hooks_server import run_server_background

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
        self.app.add_handler(CallbackQueryHandler(self.manage_service_callback, pattern="^manage_"))
        self.app.add_handler(CallbackQueryHandler(self.service_action_callback, pattern="^suspend_|^resume_|^back_to_manage$"))
        self.app.add_handler(CallbackQueryHandler(self.suspend_button_callback, pattern="^confirm_suspend_all|cancel_suspend$"))
        self.app.add_handler(CommandHandler("deploy_start", self.deploy_start_command))
        self.app.add_handler(CommandHandler("deploy_end", self.deploy_end_command))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודת התחלה"""
        message = "🤖 שלום! זה בוט ניטור Render\n\n"
        message += "הבוט מנטר את השירותים שלך ומשעה אותם אוטומטית במידת הצורך.\n\n"
        message += "הקש /help לרשימת פקודות"
        await update.message.reply_text(message)
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """רשימת פקודות מעודכנת"""
        message = "📋 <b>רשימת פקודות:</b>\n\n"
        message += "/start - התחלה\n"
        message += "/status - הצגת כל השירותים\n"
        message += "/manage - ניהול שירותים (השעיה/הפעלה עם כפתורים)\n"
        message += "/suspend - השעיית כל השירותים (עם אישור)\n"
        message += "/resume - החזרת כל השירותים המושעים\n"
        message += "/list_suspended - רשימת שירותים מושעים\n"
        message += "/deploy_start <minutes> [service_id1 service_id2 ...] - התחלת חלון דיפלוי\n"
        message += "/deploy_end [service_id1 service_id2 ...] - סיום חלון דיפלוי\n"
        message += "/help - עזרה\n"
        await update.message.reply_text(message, parse_mode="HTML")
    
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
        """השעיית שירות ספציפי לפי ID"""
        if not context.args:
            await update.message.reply_text("יש לציין ID של שירות. לדוגמה: /suspend_one srv-12345")
            return

        service_id = context.args[0]
        try:
            print(f"Attempting to suspend service with ID: {service_id}")
            self.render_api.suspend_service(service_id)
            self.db.update_service_activity(service_id, status="suspended")
            self.db.increment_suspend_count(service_id)
            self.db.record_our_action(service_id, action_type="manual_suspend")
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
        """מציג תפריט ניהול עם כפתורים לכל שירות"""
        services = self.db.get_all_services()
        if not services:
            await update.message.reply_text("לא נמצאו שירותים לניהול.")
            return

        keyboard = []
        for service in services:
            service_name = service.get("service_name", service['_id'])
            # כל כפתור שולח callback עם קידומת ושם השירות
            callback_data = f"manage_{service['_id']}"
            keyboard.append([InlineKeyboardButton(service_name, callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("בחר שירות לניהול:", reply_markup=reply_markup)

    async def manage_service_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מציג אפשרויות ניהול לשירות שנבחר"""
        query = update.callback_query
        await query.answer()

        # מחלצים את ה-ID מה-callback_data
        service_id = query.data.split('_')[1]
        service = self.db.get_service_activity(service_id)
        service_name = service.get("service_name", service_id) if service else service_id
        status = service.get("status", "unknown") if service else "unknown"

        # כפתורים להשעיה או הפעלה מחדש
        keyboard = [
            [
                InlineKeyboardButton("🔴 השהה", callback_data=f"suspend_{service_id}"),
                InlineKeyboardButton("🟢 הפעל מחדש", callback_data=f"resume_{service_id}")
            ],
            [InlineKeyboardButton("« חזור לרשימה", callback_data="back_to_manage")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text=f"ניהול שירות: <b>{service_name}</b>\nסטטוס נוכחי: {status}",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )

    async def service_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """מבצע פעולת השעיה או הפעלה על שירות"""
        query = update.callback_query
        await query.answer()

        action, service_id = query.data.split('_')[0], '_'.join(query.data.split('_')[1:])

        if action == "suspend":
            try:
                self.render_api.suspend_service(service_id)
                self.db.update_service_activity(service_id, status="suspended")
                self.db.record_our_action(service_id, action_type="manual_suspend")
                await query.edit_message_text(text=f"✅ השירות {service_id} הושהה בהצלחה.")
            except Exception as e:
                await query.edit_message_text(text=f"❌ כישלון בהשעיית {service_id}: {e}")
        elif action == "resume":
            try:
                self.render_api.resume_service(service_id)
                self.db.update_service_activity(service_id, status="active")
                self.db.record_our_action(service_id, action_type="manual_resume")
                await query.edit_message_text(text=f"✅ השירות {service_id} הופעל מחדש.")
            except Exception as e:
                await query.edit_message_text(text=f"❌ כישלון בהפעלת {service_id}: {e}")
        elif action == "back":  # מטפל בכפתור "חזור"
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
                        self.render_api.suspend_service(service['_id'])
                        self.db.update_service_activity(service['_id'], status="suspended")
                        self.db.increment_suspend_count(service['_id'])
                        self.db.record_our_action(service['_id'], action_type="manual_suspend")
                        suspended_count += 1
                    except Exception as e:
                        print(f"Could not suspend service {service['_id']}: {e}")
            
            await query.edit_message_text(text=f"✅ הושלם. {suspended_count} שירותים הושהו.")

        elif query.data == "cancel_suspend":
            await query.edit_message_text(text="הפעולה בוטלה.")

    async def deploy_start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """התחלת חלון דיפלוי ידני (לשימוש מ-CI או ידנית)"""
        if not context.args:
            await update.message.reply_text("שימוש: /deploy_start <minutes> [service_id1 service_id2 ...]")
            return
        try:
            minutes = int(context.args[0])
        except ValueError:
            await update.message.reply_text("הדקות חייבות להיות מספר.")
            return
        service_ids = context.args[1:] if len(context.args) > 1 else [s for s in config.SERVICES_TO_MONITOR]
        db.start_deploy_window(service_ids, minutes)
        await update.message.reply_text(f"✅ חלון דיפלוי הופעל ל-{len(service_ids)} שירותים למשך {minutes} דק'.")

    async def deploy_end_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """סיום חלון דיפלוי ידני (לשימוש מ-CI או ידנית)"""
        service_ids = context.args if context.args else [s for s in config.SERVICES_TO_MONITOR]
        db.end_deploy_window(service_ids)
        await update.message.reply_text(f"✅ חלון דיפלוי הסתיים עבור {len(service_ids)} שירותים.")

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
    
    # ניטור סטטוסים שוטף
    if config.ENABLE_STATE_MONITOR:
        schedule.every(config.STATUS_POLL_INTERVAL_MINUTES).minutes.do(state_monitor.check_services_state)
    
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

    # הפעלת שרת Webhook ל-CI (אופציונלי)
    run_server_background()
    
    # שליחת התראת הפעלה
    send_startup_notification()
    
    # בדיקה ראשונית
    print("מבצע בדיקה ראשונית...")
    activity_tracker.check_inactive_services()
    if config.ENABLE_STATE_MONITOR:
        state_monitor.check_services_state()
    
    print("✅ הבוט פועל! לחץ Ctrl+C להפסקה")
    
    # הפעלת הבוט
    bot.app.run_polling()

if __name__ == "__main__":
    main()
