#!/usr/bin/env python3
"""
סקריפט בדיקה למערכת ההתראות
מאפשר לבדוק את כל שרשרת ההתראות מקצה לקצה
"""

import os
import sys
import asyncio
import logging
from datetime import datetime, timezone

# הגדרת לוגים
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ייבוא המודולים הנדרשים
import config
from database import db
from notifications import send_notification, send_status_change_notification
from status_monitor import status_monitor
from render_api import render_api

def print_header(title):
    """הדפסת כותרת מעוצבת"""
    print("\n" + "="*50)
    print(f" {title}")
    print("="*50)

def test_config():
    """בדיקת קונפיגורציה"""
    print_header("בדיקת הגדרות")
    
    issues = []
    
    # בדיקת TELEGRAM_BOT_TOKEN
    if not config.TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN == "your_telegram_bot_token_here":
        issues.append("❌ TELEGRAM_BOT_TOKEN לא מוגדר")
        print("❌ TELEGRAM_BOT_TOKEN לא מוגדר")
    else:
        print(f"✅ TELEGRAM_BOT_TOKEN מוגדר: {config.TELEGRAM_BOT_TOKEN[:20]}...")
    
    # בדיקת ADMIN_CHAT_ID
    if not config.ADMIN_CHAT_ID or config.ADMIN_CHAT_ID == "your_admin_chat_id_here":
        issues.append("❌ ADMIN_CHAT_ID לא מוגדר")
        print("❌ ADMIN_CHAT_ID לא מוגדר")
        print("   💡 טיפ: הפעל את הבוט והשתמש בפקודה /check_config כדי לקבל את ה-Chat ID שלך")
    else:
        print(f"✅ ADMIN_CHAT_ID מוגדר: {config.ADMIN_CHAT_ID}")
    
    # בדיקת RENDER_API_KEY
    if not config.RENDER_API_KEY or config.RENDER_API_KEY == "your_render_api_key_here":
        issues.append("❌ RENDER_API_KEY לא מוגדר")
        print("❌ RENDER_API_KEY לא מוגדר")
    else:
        print(f"✅ RENDER_API_KEY מוגדר: {config.RENDER_API_KEY[:20]}...")
    
    # בדיקת MongoDB
    try:
        count = db.services.count_documents({})
        print(f"✅ MongoDB מחובר ({count} שירותים במערכת)")
    except Exception as e:
        issues.append(f"❌ בעיה בחיבור ל-MongoDB: {str(e)}")
        print(f"❌ בעיה בחיבור ל-MongoDB: {str(e)}")
    
    return issues

def test_notification():
    """בדיקת שליחת התראה"""
    print_header("בדיקת שליחת התראה")
    
    test_message = f"🧪 הודעת בדיקה מ-test_alerts.py\n"
    test_message += f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
    test_message += f"✅ אם אתה רואה הודעה זו, מערכת ההתראות עובדת!"
    
    print("שולח הודעת בדיקה...")
    result = send_notification(test_message)
    
    if result:
        print("✅ הודעת בדיקה נשלחה בהצלחה!")
        return True
    else:
        print("❌ נכשל בשליחת הודעת בדיקה")
        print("   בדוק את הלוגים למעלה לפרטים נוספים")
        return False

def test_status_change_notification():
    """בדיקת התראת שינוי סטטוס"""
    print_header("בדיקת התראת שינוי סטטוס")
    
    print("שולח התראת שינוי סטטוס מדומה...")
    result = send_status_change_notification(
        service_id="test-service-123",
        service_name="שירות בדיקה",
        old_status="online",
        new_status="offline",
        emoji="🔴",
        action="ירד (בדיקה)"
    )
    
    if result:
        print("✅ התראת שינוי סטטוס נשלחה בהצלחה!")
        return True
    else:
        print("❌ נכשל בשליחת התראת שינוי סטטוס")
        return False

def test_service_monitoring(service_id=None):
    """בדיקת ניטור שירות ספציפי"""
    print_header("בדיקת ניטור שירות")
    
    if not service_id:
        # נסה למצוא שירות קיים במערכת
        services = list(db.services.find({}, limit=1))
        if services:
            service_id = services[0]["_id"]
            print(f"משתמש בשירות קיים: {service_id}")
        else:
            # צור שירות בדיקה
            service_id = "test-service-" + datetime.now().strftime("%Y%m%d%H%M%S")
            db.services.insert_one({
                "_id": service_id,
                "service_name": "שירות בדיקה אוטומטי",
                "status": "active",
                "last_known_status": "online",
                "created_at": datetime.now(timezone.utc),
                "is_test": True
            })
            print(f"נוצר שירות בדיקה: {service_id}")
    
    # הפעל ניטור
    print(f"\nמפעיל ניטור עבור {service_id}...")
    if status_monitor.enable_monitoring(service_id, user_id=0):
        print("✅ ניטור הופעל בהצלחה")
    else:
        print("❌ נכשל בהפעלת ניטור")
        return False
    
    # בדוק סטטוס ניטור
    monitoring_status = status_monitor.get_monitoring_status(service_id)
    if monitoring_status.get("enabled"):
        print(f"✅ ניטור פעיל עבור {service_id}")
    else:
        print(f"❌ ניטור לא פעיל עבור {service_id}")
        return False
    
    # סימולציה של שינוי סטטוס
    print("\nמבצע סימולציה של שינוי סטטוס...")
    
    # שינוי מ-online ל-offline
    db.update_service_status(service_id, "offline")
    db.record_status_change(service_id, "online", "offline")
    
    # בדוק אם זה שינוי משמעותי
    if status_monitor._is_significant_change("online", "offline"):
        print("✅ השינוי מזוהה כמשמעותי (online -> offline)")
        
        # שלח התראה
        service = db.get_service_activity(service_id)
        if service:
            result = send_status_change_notification(
                service_id=service_id,
                service_name=service.get("service_name", service_id),
                old_status="online",
                new_status="offline",
                emoji="🔴",
                action="ירד (בדיקה אוטומטית)"
            )
            if result:
                print("✅ התראה נשלחה בהצלחה!")
            else:
                print("❌ נכשל בשליחת התראה")
    else:
        print("❌ השינוי לא מזוהה כמשמעותי")
    
    # נקה שירות בדיקה
    if service_id.startswith("test-service-"):
        print(f"\nמוחק שירות בדיקה {service_id}...")
        db.services.delete_one({"_id": service_id})
    
    return True

def main():
    """פונקציה ראשית"""
    print("\n" + "🧪"*25)
    print(" סקריפט בדיקה למערכת ההתראות של Render Monitor Bot")
    print("🧪"*25)
    
    # בדיקת קונפיגורציה
    issues = test_config()
    
    if issues:
        print_header("סיכום בעיות שנמצאו")
        for issue in issues:
            print(issue)
        print("\n⚠️ תקן את הבעיות לעיל לפני המשך הבדיקות")
        
        if "ADMIN_CHAT_ID" in str(issues):
            print("\n💡 כדי לקבל את ה-Chat ID שלך:")
            print("1. הפעל את הבוט: python main.py")
            print("2. שלח לו /start בטלגרם")
            print("3. שלח /check_config")
            print("4. העתק את ה-Chat ID שיוצג")
            print("5. הגדר אותו במשתנה סביבה ADMIN_CHAT_ID")
        
        return
    
    # בדיקת התראות
    print("\n" + "-"*50)
    print("האם לבצע בדיקת שליחת התראות? (y/n): ", end="")
    if input().lower() == 'y':
        # בדיקת התראה פשוטה
        if test_notification():
            # בדיקת התראת שינוי סטטוס
            test_status_change_notification()
            
            # בדיקת ניטור שירות
            print("\n" + "-"*50)
            print("האם לבצע בדיקת ניטור שירות מלאה? (y/n): ", end="")
            if input().lower() == 'y':
                print("הזן Service ID לבדיקה (או Enter לשירות אוטומטי): ", end="")
                service_id = input().strip()
                test_service_monitoring(service_id if service_id else None)
    
    print_header("בדיקה הסתיימה")
    print("📊 בדוק את הלוגים למעלה לפרטים מלאים")
    print("💬 אם קיבלת התראות בטלגרם, המערכת עובדת כשורה!")

if __name__ == "__main__":
    main()