#!/usr/bin/env python3
"""
generate_session.py – مولّد Session String لحسابات تيليجرام
=============================================================
شغّله محلياً (هاتف Pydroid / كمبيوتر) وليس على Render.

الوضع 1: تسجيل دخول جديد برقم الهاتف + رمز التحقق (OTP).
الوضع 2: تحويل ملف .session موجود مسبقاً إلى Session String.

بعد الحصول على النص، أضفه في Render:
    Environment > {PREFIX}_SESSION_STRING = <النص>
مثال:  MAIN_SESSION_STRING=1BZW...   أو   ACCOUNT_1_SESSION_STRING=1BZW...
"""

import asyncio
import os
import sys


async def fresh_login() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import SessionPasswordNeededError

    print("=" * 55)
    print("  تسجيل دخول جديد - توليد Session String")
    print("=" * 55)
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()
    phone = input("رقم الهاتف مع رمز الدولة (+966...): ").strip()

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    sent = await client.send_code_request(phone)
    code = input("رمز التحقق (من تيليجرام): ").strip().replace(" ", "")
    try:
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        password = input("كلمة مرور التحقق بخطوتين (2FA): ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    session_string = client.session.save()
    print(f"\n✅ تم تسجيل الدخول: {me.first_name} (@{me.username or me.id})")
    print("\n─── Session String ───")
    print(session_string)
    print("──────────────────────\n")
    await client.disconnect()


async def convert_file() -> None:
    from telethon import TelegramClient

    print("=" * 55)
    print("  تحويل ملف .session موجود إلى Session String")
    print("=" * 55)
    path = input("مسار ملف الجلسة (مثال: main_account.session): ").strip()
    if path.endswith(".session"):
        path = path[:-8]
    if not os.path.exists(path + ".session"):
        print(f"❌ الملف غير موجود: {path}.session")
        sys.exit(1)
    api_id = int(input("API_ID الخاص بهذا الحساب: ").strip())
    api_hash = input("API_HASH: ").strip()

    client = TelegramClient(path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ هذه الجلسة منتهية أو غير صالحة - استخدم الوضع 1")
        await client.disconnect()
        sys.exit(1)
    me = await client.get_me()
    session_string = client.session.save()
    print(f"\n✅ الجلسة صالحة: {me.first_name} (@{me.username or me.id})")
    print("\n─── Session String ───")
    print(session_string)
    print("──────────────────────\n")
    await client.disconnect()


def main() -> None:
    try:
        import telethon  # noqa: F401
    except ImportError:
        print("❌ ثبّت المكتبة أولاً:  pip install telethon")
        sys.exit(1)

    print("\nاختر الوضع:")
    print("  1) تسجيل دخول جديد (هاتف + رمز OTP)")
    print("  2) تحويل ملف .session موجود")
    choice = input("اختيارك [1/2]: ").strip()

    if choice == "2":
        asyncio.run(convert_file())
    else:
        asyncio.run(fresh_login())


if __name__ == "__main__":
    main()
