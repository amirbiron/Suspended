#!/usr/bin/env python3
"""
בדיקה פשוטה של הפונקציה _is_significant_change
"""

# בדיקה פשוטה ללא תלויות
def _is_significant_change(old_status: str, new_status: str) -> bool:
    """בדיקה אם השינוי משמעותי ודורש התראה"""
    # שינויים משמעותיים: online <-> offline
    significant_changes = [
        ("online", "offline"),
        ("offline", "online"),
        ("deploying", "online"),  # סיום פריסה מוצלח
        ("deploying", "offline"),  # כשלון בפריסה
    ]
    
    return (old_status, new_status) in significant_changes

# בדיקות
print("בדיקת הפונקציה _is_significant_change:")
print("-" * 50)

test_cases = [
    ("online", "offline", True),
    ("offline", "online", True),
    ("online", "online", False),
    ("offline", "offline", False),
    ("deploying", "online", True),
    ("deploying", "offline", True),
    ("unknown", "online", False),
    ("online", "unknown", False),
]

for old_status, new_status, expected in test_cases:
    result = _is_significant_change(old_status, new_status)
    status = "✅" if result == expected else "❌"
    print(f"{status} {old_status} -> {new_status}: {result} (expected: {expected})")

print("\n" + "="*50)
print("סיכום:")
print("השינויים שאמורים להפעיל התראה:")
print("• online -> offline")
print("• offline -> online")
print("• deploying -> online")
print("• deploying -> offline")

print("\n" + "="*50)
print("במקרה שלך:")
print("srv-d2d0dnc9c44c73b5d6q0 עם action='online'")
print("\nאם השירות כבר במצב 'online':")
print("1. online -> offline: ", _is_significant_change("online", "offline"), " (צריך התראה)")
print("2. offline -> online: ", _is_significant_change("offline", "online"), " (צריך התראה)")
print("\nסה\"כ: 2 התראות צריכות להישלח")