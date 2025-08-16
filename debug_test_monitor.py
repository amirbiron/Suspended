#!/usr/bin/env python3
"""
סקריפט דיבאג לבדיקת הבעיה בפקודת test_monitor
"""

import os
import sys

# הוספת הנתיב למודולים
sys.path.insert(0, '/workspace')

def test_significant_change():
    """בדיקת הפונקציה _is_significant_change"""
    print("בודק את הפונקציה _is_significant_change:")
    print("-" * 50)
    
    # יצירת instance של StatusMonitor
    from status_monitor import StatusMonitor
    monitor = StatusMonitor()
    
    test_cases = [
        ("online", "offline"),
        ("offline", "online"),
        ("online", "online"),
        ("offline", "offline"),
        ("unknown", "online"),
        ("online", "unknown"),
        ("deploying", "online"),
        ("deploying", "offline"),
    ]
    
    for old_status, new_status in test_cases:
        result = monitor._is_significant_change(old_status, new_status)
        emoji = "✅" if result else "❌"
        print(f"{emoji} {old_status} -> {new_status}: {result}")
    
    print("\n" + "="*50)
    print("הבדיקות שאמורות להחזיר True (להפעיל התראה):")
    print("✅ online -> offline")
    print("✅ offline -> online")
    print("✅ deploying -> online")
    print("✅ deploying -> offline")

def analyze_test_monitor_flow():
    """ניתוח הזרימה של test_monitor"""
    print("\n\nניתוח זרימת test_monitor:")
    print("-" * 50)
    
    service_id = "srv-d2d0dnc9c44c73b5d6q0"
    action = "online"
    
    print(f"Service ID: {service_id}")
    print(f"Action: {action}")
    print()
    
    # סימולציה של מה שקורה
    print("אם השירות כבר במצב 'online':")
    print("1. קורא ל-_simulate_status_change(service_id, 'online', 'offline')")
    print("   - מעדכן את הסטטוס ב-DB ל-offline")
    print("   - בודק אם online->offline הוא שינוי משמעותי (צריך להיות True)")
    print("   - אם כן, שולח התראה")
    print()
    print("2. ממתין 2 שניות")
    print()
    print("3. קורא ל-_simulate_status_change(service_id, 'offline', 'online')")
    print("   - מעדכן את הסטטוס ב-DB ל-online")
    print("   - בודק אם offline->online הוא שינוי משמעותי (צריך להיות True)")
    print("   - אם כן, שולח התראה")
    print()
    print("סה\"כ: אמורות להישלח 2 התראות")
    
    print("\n" + "="*50)
    print("בעיות אפשריות:")
    print("1. ❓ האם status_monitor._is_significant_change נקרא נכון?")
    print("2. ❓ האם send_notification מחזיר True או False?")
    print("3. ❓ האם ADMIN_CHAT_ID מוגדר נכון?")
    print("4. ❓ האם יש בעיה עם הטוקן של הבוט?")
    print("5. ❓ האם הבוט חסום על ידי המשתמש?")

def check_notification_config():
    """בדיקת הגדרות התראות"""
    print("\n\nבדיקת הגדרות התראות:")
    print("-" * 50)
    
    try:
        import config
        
        if hasattr(config, 'ADMIN_CHAT_ID'):
            if config.ADMIN_CHAT_ID and config.ADMIN_CHAT_ID != "your_admin_chat_id_here":
                print(f"✅ ADMIN_CHAT_ID מוגדר: {config.ADMIN_CHAT_ID}")
            else:
                print("❌ ADMIN_CHAT_ID לא מוגדר כראוי")
        else:
            print("❌ ADMIN_CHAT_ID לא קיים בקונפיג")
            
        if hasattr(config, 'TELEGRAM_BOT_TOKEN'):
            if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_BOT_TOKEN != "your_telegram_bot_token_here":
                print(f"✅ TELEGRAM_BOT_TOKEN מוגדר: {config.TELEGRAM_BOT_TOKEN[:20]}...")
            else:
                print("❌ TELEGRAM_BOT_TOKEN לא מוגדר כראוי")
        else:
            print("❌ TELEGRAM_BOT_TOKEN לא קיים בקונפיג")
            
    except Exception as e:
        print(f"❌ שגיאה בטעינת config: {e}")

def main():
    print("\n" + "🔍"*25)
    print(" דיבאג לבעיית test_monitor")
    print("🔍"*25)
    
    # בדיקת הפונקציה _is_significant_change
    try:
        test_significant_change()
    except Exception as e:
        print(f"❌ שגיאה בבדיקת _is_significant_change: {e}")
    
    # ניתוח הזרימה
    analyze_test_monitor_flow()
    
    # בדיקת הגדרות
    check_notification_config()
    
    print("\n" + "="*50)
    print("המלצות לדיבאג:")
    print("1. הוסף print/log בתחילת _simulate_status_change")
    print("2. הוסף print/log לפני הקריאה ל-send_notification")
    print("3. הוסף print/log אחרי send_notification עם התוצאה")
    print("4. בדוק את הלוגים של הבוט ברנדר")
    print("5. נסה לשלוח הודעת בדיקה ישירות עם הטוקן וה-Chat ID")

if __name__ == "__main__":
    main()