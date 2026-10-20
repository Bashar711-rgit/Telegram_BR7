#!/usr/bin/env python3
"""Live ladder tests: case 12 (BUTTON_USER_INVALID retry) + case 11 (no msg link).
Case C: fake nonexistent user id + no username -> tg button rejected by server
        -> production ladder must retry WITHOUT «مراسلة» and deliver 1/2 buttons.
Case D: no message link at all -> «عرض الرسالة» honestly absent; «مراسلة» remains.
Deletes nothing (evidence for user), prints no secrets."""
import asyncio, sys, os
sys.path.insert(0, "/home/z/Telegram_BR7")
os.chdir("/home/z/Telegram_BR7")

TOKEN = open('/home/z/.secrets/bot_token').read().strip()
CHAT = -1002707889921


async def main():
    from alert_bot import AlertBot
    bot = AlertBot(token=TOKEN, chat_id=CHAT, timeout=15.0)
    me = await bot.get_me()
    print(f"getMe: @{me.get('username')}\n")

    # ── Case C: nonexistent user id -> BUTTON_USER_INVALID ladder ──
    data_c = {
        "sender_id": 1,               # id=1 does not exist on Telegram (proven by probe)
        "sender_username": None,
        "sender_first_name": "مرسل محذوف",
        "sender_last_name": None,
        "sender_type": "user",
        "text": "الاختبار الفعلي ج — زر مراسلة بمعرف غير موجود: يجب أن يُسقَط الزر وحده ويصل التنبيه بزر العرض.",
        "chat_id": CHAT,
        "message_id": 1,
    }
    ok_c, method_c, reason_c = await bot.send(data_c, msg_link="https://t.me/c/2707889921/177710")
    print(f"CASE C: ok={ok_c} method={method_c} tier={bot.last_tier} reason={reason_c!r}")

    # ── Case D: no message link -> view button honestly absent ──
    data_d = {
        "sender_id": 7886937106,
        "sender_username": "BZ711BR_bot",
        "sender_first_name": "محمد",
        "sender_last_name": None,
        "sender_type": "user",
        "text": "الاختبار الفعلي د — لا يوجد رابط رسالة أصلي: زر عرض الرسالة يختفي بصدق ويبقى زر مراسلة.",
        "chat_id": 0,
        "message_id": None,
    }
    ok_d, method_d, reason_d = await bot.send(data_d, msg_link=None, group_link=None)
    print(f"CASE D: ok={ok_d} method={method_d} tier={bot.last_tier} reason={reason_d!r}")

    print("\nالحكم: C و D يجب أن تصل كلاهما (ok=True) — التنبيه لا يضيع أبداً.")

asyncio.run(main())
