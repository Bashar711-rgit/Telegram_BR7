#!/usr/bin/env python3
"""Live production-path test: AlertBot.send through the REAL Telegram API.
Case A: sender WITH username (real bot username)  -> expect T1/username, 2 buttons
Case B: sender WITHOUT username (real bot id)     -> expect tg:// button accepted,
        name entity dropped then SELF-HEALED to source-message link.
Keeps both alerts in the channel for manual human button inspection.
Prints no secrets."""
import asyncio, json, sys
sys.path.insert(0, "/home/z/Telegram_BR7")
import os
os.chdir("/home/z/Telegram_BR7")

TOKEN = open('/home/z/.secrets/bot_token').read().strip()
CHAT = -1002707889921
BOT_ID = 7886937106
MSG_LINK = "https://t.me/c/2707889921/177710"  # existing (deleted) post id — link format valid


async def run_case(bot, label, data, **kw):
    print(f"\n──── {label} ────")
    ok, method, reason = await bot.send(data, msg_link=kw.pop("msg_link", MSG_LINK), **kw)
    print(f"ok={ok} | method={method} | tier={bot.last_tier} | reason={reason!r}")
    return ok, method


async def main():
    from alert_bot import AlertBot
    bot = AlertBot(token=TOKEN, chat_id=CHAT, timeout=15.0)
    me = await bot.get_me()
    print(f"getMe: @{me.get('username')}")

    # ── Case A: WITH username ──
    data_a = {
        "sender_id": BOT_ID,
        "sender_username": "BZ711BR_bot",
        "sender_first_name": "محمد",
        "sender_last_name": "العمري",
        "sender_type": "user",
        "text": "الاختبار الفعلي أ — قالب الusername:\nسطر ثانٍ مع رابط https://example.com وإيموجي 🔥 و & علامة",
        "chat_id": CHAT,
        "message_id": 177710,
    }
    ok_a, method_a = await run_case(bot, "CASE A — sender WITH username", data_a,
                                    sender_username="BZ711BR_bot")

    # ── Case B: WITHOUT username (real id → tg:// button should be ACCEPTED) ──
    data_b = {
        "sender_id": BOT_ID,
        "sender_username": None,
        "sender_first_name": "صالح",
        "sender_last_name": None,
        "sender_type": "user",
        "text": "الاختبار الفعلي ب — بلا username: الاسم يُصلح ذاتياً برابط الرسالة، وزر مراسلة عبر tg://user.",
        "chat_id": CHAT,
        "message_id": 177710,
    }
    ok_b, method_b = await run_case(bot, "CASE B — sender WITHOUT username (tg:// button)", data_b)

    print("\n──── الحكم النهائي ────")
    print(f"Case A delivered={ok_a} method={method_a}")
    print(f"Case B delivered={ok_b} method={method_b}")
    print("الرسالتان متروكتان في القناة للفحص اليدوي للأزرار من عميل تيليجرام.")

asyncio.run(main())
