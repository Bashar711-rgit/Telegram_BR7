#!/usr/bin/env python3
"""Compute the GOLDEN alert output from the CURRENT (pre-change) code.

These exact strings become the EXPECTED_* constants in
tests/test_alert_regression.py. After the sender-intelligence changes,
_build_alert() must produce byte-identical output for the same inputs.
"""
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault("TARGET_GROUP_ID", "-1001234567890")
os.environ.setdefault("ADMIN_CHAT_ID", "654321")
os.environ.setdefault("DASHBOARD_AUTH_TOKEN", "test-dashboard-token-0123456789")
os.environ.setdefault("DB_TYPE", "sqlite")
os.environ["DB_FILE"] = str(PROJECT_DIR / "tests" / "_golden.db")
os.environ["LOG_FILE"] = str(PROJECT_DIR / "tests" / "_golden.log")
os.environ["DASHBOARD_ENABLED"] = "false"

from telethon import Button  # noqa: E402
from config import CFG  # noqa: E402
from monitors import EnhancedAccountMonitor, FastCaptureBuffer  # noqa: E402

account = {
    "name": "GoldenAcc", "api_id": 12345, "api_hash": "h",
    "phone": "+90000000000", "session": "golden", "priority": 1,
}
mon = EnhancedAccountMonitor(account, db=None, flt=None)

S1 = {"id": 555000111, "display": "أحمد محمد", "username": "ahmed_99", "access_hash": 7234567890123}
C1 = {"group_link": "https://t.me/mygroup", "title": "مجموعة الطلاب", "msg_link": "https://t.me/mygroup/123"}
S2 = {"id": 777000222, "display": "سارة", "username": None, "access_hash": None}
C2 = {"group_link": "https://t.me/c/1234567890", "title": "مجموعة خاصة", "msg_link": "https://t.me/c/1234567890/456"}
S3 = {"id": 888000333, "display": "خالد", "username": None, "access_hash": 9998877766655}
C3 = {"group_link": "#", "title": None, "msg_link": "#"}
S4 = {"id": 999000444, "display": "مستخدم", "username": "user_x", "access_hash": 111222333}
C4 = {"group_link": "https://t.me/eng_group", "title": "غير معروف", "msg_link": "https://t.me/eng_group/789"}

TEXT = "أبي مساعدة في واجب الاحصاء ضروري"

scenarios = [
    ("S1_username_chat", S1, C1),
    ("S2_private_chat", S2, C2),
    ("S3_no_username_hash", S3, C3),
    ("S4_unknown_title", S4, C4),
]

print(f"ALERT_WITH_BUTTONS={CFG.ALERT_WITH_BUTTONS} ALERT_WITH_COPY_BUTTON={CFG.ALERT_WITH_COPY_BUTTON}")
for name, sender, chat in scenarios:
    alert, buttons = mon._build_alert(sender, chat, "واجب", TEXT, {"msg_hash": "abc123"})
    btn_repr = None
    if buttons:
        btn_repr = [[(b.__dict__.get("text") if hasattr(b, "__dict__") else str(b)) for b in row] for row in buttons]
    print("=" * 20, name)
    print("ALERT:", repr(alert))
    if buttons:
        import json
        rows = []
        for row in buttons:
            r = []
            for b in row:
                data = getattr(b, "data", None)
                r.append({
                    "text": getattr(b, "text", None),
                    "url": getattr(b, "url", None),
                    "data": data.decode() if isinstance(data, bytes) else data,
                    "cls": type(b).__name__,
                })
            rows.append(r)
        print("BUTTONS:", json.dumps(rows, ensure_ascii=False))
