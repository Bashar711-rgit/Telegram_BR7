#!/usr/bin/env python3
"""
generate_session.py – مولّد Session String لحسابات تيليجرام — v14.5.1
=====================================================================
شغّله محلياً (هاتف Pydroid / كمبيوتر) وليس على Render.

الوضع 1: تسجيل دخول جديد برقم الهاتف + رمز التحقق (OTP).
الوضع 2: تحويل ملف .session موجود مسبقاً إلى Session String.

بعد الحصول على النص، أضفه في Render:
    Environment > {PREFIX}_SESSION_STRING = <النص>
مثال:  MAIN_SESSION_STRING=1BZW...   أو   ACCOUNT_1_SESSION_STRING=1BZW...

v14.5.1 — correction pass:
  * convert_file() was producing a USELESS 32-char hex string, not a
    session string. When TelegramClient is constructed with a file path,
    `client.session` is a SQLiteSession — and SQLiteSession.save()
    returns the internal session_id hex, NOT the auth_key. Pasting that
    hex into Render's *_SESSION_STRING env var caused the bot to fail at
    is_user_authorized() with "Session expired - update *_SESSION_STRING
    env var" — silent because the string looked plausible. The fix
    builds a fresh StringSession from the live client's actual
    auth_key/dc_id/server_address/port:
        string_session = StringSession()
        string_session.set_dc(client.session.dc_id, ...)
        string_session.auth_key = client.session.auth_key
        session_string = string_session.save()
    (fresh_login() was correct all along — it starts from a
    StringSession, so its save() returns the right string.)
  * Both interactive functions now wrap the client in try/finally and
    await client.disconnect() unconditionally. Previously an exception
    during sign_in/get_me/input() left the client connected until the
    process exited. On Pydroid that window can be long; on any host a
    lingering connection with the same session can trigger
    AUTH_KEY_DUPLICATED when the bot later tries to log in from Render
    with the same session string.
  * Both functions now verify that session.save() returned a non-empty
    string before printing. An empty result (rare but possible) used to
    print a blank line and let the operator paste nothing into Render's
    env var without any warning.
  * KeyboardInterrupt during any input() now exits with a clean Arabic
    message instead of a full traceback (this is an interactive
    utility, not a library — a stack trace is noise).
  * __version__ = "14.5.1".

قاعدة التشخيص السريعة (v14.5.1):
  ✅ StringSession الصحيح : يبدأ بـ "1" ثم base64، طوله مئات الأحرف.
  ❌ session_id hex       : 32 حرفاً فقط [0-9a-f]، لا يُقبل على Render.

ملاحظة: هذا الملف أداة معزولة — لا يُستورد من أي ملف آخر، ولا يعمل
       أثناء تشغيل البوت. يستخدمه المشغّل مرة واحدة لكل حساب.
"""

import asyncio
import os
import sys

__version__ = "14.5.1"


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
    try:
        sent = await client.send_code_request(phone)
        code = input("رمز التحقق (من تيليجرام): ").strip().replace(" ", "")
        try:
            await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            password = input("كلمة مرور التحقق بخطوتين (2FA): ").strip()
            await client.sign_in(password=password)

        me = await client.get_me()
        session_string = client.session.save()
        # v14.5.1: لا تطبع سلسلة فارغة بصمت — إذا فشل save() لأي سبب
        # داخلي، يجب أن يعرف المشغّل قبل أن يلصق "" في Render.
        if not session_string:
            print("\n❌ فشل توليد Session String (auth_key فارغ). أعد المحاولة.")
            return
        print(f"\n✅ تم تسجيل الدخول: {me.first_name} (@{me.username or me.id})")
        print("\n─── Session String ───")
        print(session_string)
        print("──────────────────────\n")
    finally:
        # v14.5.1: فصل نظيف — لا اتصال معلق عند أي خطأ.
        await client.disconnect()


async def convert_file() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

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

    # v14.5.1: تمرير path → TelegramClient يستخدم SQLiteSession تلقائياً.
    # SQLiteSession.save() يعيد session_id hex (32 حرفاً)، وليس الـauth_key.
    # لذلك: نفتح الجلسة، نتحقق أنها حيّة، ثم نبني StringSession من
    # الـauth_key الفعلي — وإلا نُلصق في Render قيمة "تبدو صحيحة" لكنها
    # ميتة (32 حرف hex). قاعدة التشخيص: StringSession يبدأ بـ"1" وطويل.
    client = TelegramClient(path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("❌ هذه الجلسة منتهية أو غير صالحة - استخدم الوضع 1")
            return
        me = await client.get_me()

        # v14.5.1: بناء StringSession من auth_key الحيّ — وليس من
        # client.session.save() (الذي يعيد hex للـSQLite فقط).
        string_session = StringSession()
        try:
            string_session.set_dc(
                client.session.dc_id,
                client.session.server_address,
                client.session.port,
            )
            string_session.auth_key = client.session.auth_key
        except Exception as e:
            print(f"❌ فشل نسخ بيانات الجلسة الحية: {type(e).__name__}: {e}")
            return

        session_string = string_session.save()
        if not session_string:
            print("\n❌ فشل توليد Session String (auth_key فارغ). الجلسة تالفة.")
            return
        # v14.5.1: تحقق إضافي — StringSession الصحيح طويل ويبدأ بـ"1"؛
        # hex 32 حرفاً = session_id ميت لن يُقبل على Render.
        if len(session_string) < 100 or not session_string.startswith("1"):
            print(
                f"\n⚠️ الناتج لا يبدو كـStringSession صحيح "
                f"(طول={len(session_string)}، يبدأ بـ={session_string[:4]!r}). "
                "لا تلصقه في Render — أعد المحاولة أو استخدم الوضع 1."
            )
            return

        print(f"\n✅ الجلسة صالحة: {me.first_name} (@{me.username or me.id})")
        print("\n─── Session String ───")
        print(session_string)
        print("──────────────────────\n")
    finally:
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
    try:
        main()
    except KeyboardInterrupt:
        # v14.5.1: خروج نظيف بدل traceback — سكربت تفاعلي.
        print("\n\n⏹ تم الإلغاء بواسطة المستخدم.")
        sys.exit(130)
