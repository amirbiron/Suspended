import asyncio
import schedule
import time
import threading
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import config
from database import db
from activity_tracker import activity_tracker
from notifications import send_notification, send_startup_notification, send_daily_report

class RenderMonitorBot:
    def __init__(self):
        self.app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
        self.setup_handlers()
        
    def setup_handlers(self):
        """הוספת command handlers"""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("status", self.status_command))
        self.app.add_handler(CommandHandler("suspend", self.suspend_command))
        self.app.add_handler(CommandHandler("resume", self.resume_command))
        self.app.add_handler(CommandHandler("list_suspended", self.list_suspended_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודת התחלה"""
        message = "🤖 שלום! זה בוט ניטור Render\n\n"
        message += "הבוט מנטר את השירותים שלך ומשעה אותם אוטומטית במידת הצורך.\n\n"
        message += "הקש /help לרשימת פקודות"
        await update.message.reply_text(message)
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """רשימת פקודות"""
        message = "📋 *רשימת פקודות:*\n\n"
        message += "/start - התחלה\n"
        message += "/status - מצב כל השירותים\n"  
        message += "/suspend - השעיית כל השירותים\n"
        message += "/resume - החזרת כל השירותים המושעים\n"
        message += "/list_suspended - רשימת שירותים מושעים\n"
        message += "/help - עזרה\n"
        await update.message.reply_text(message, parse_mode="Markdown")
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הצגת מצב כל השירותים"""
        services = db.get_all_services()
        
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
                days_inactive = (datetime.now() - last_activity.replace(tzinfo=None)).days
                message += f"   פעילות אחרונה: {days_inactive} ימים\n"
            else:
                message += f"   פעילות אחרונה: לא ידוע\n"
            
            message += "\n"
        
        await update.message.reply_text(message, parse_mode="Markdown")
    
    async def suspend_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """השעיית כל השירותים"""
        await update.message.reply_text("מתחיל השעיית שירותים...")
        
        messages = []
        for service_id in config.SERVICES_TO_MONITOR:
            result = activity_tracker.manual_suspend_service(service_id)
            
            service = db.get_service_activity(service_id)
            service_name = service.get("service_name", service_id) if service else service_id
            
            if result["success"]:
                messages.append(f"✅ {service_name} - הושעה בהצלחה")
            else:
                messages.append(f"❌ {service_name} - כשלון: {result['message']}")
        
        response = "תוצאות השעיה:\n\n" + "\n".join(messages)
        await update.message.reply_text(response)
    
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
                days_suspended = (datetime.now() - suspended_at.replace(tzinfo=None)).days
                message += f"  מושעה כבר {days_suspended} ימים\n"
            message += "\n"
        
        await update.message.reply_text(message, parse_mode="Markdown")

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
    
    # הפעלת המתזמן ברקע
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
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
