#!/usr/bin/env python3
"""
test_alert_live.py — اختبار Telegram الحقيقي للقالب v11.6 (مواصفة §8/§9).

يرسل تنبيهاً تجريبياً واحداً عبر Bot API بنفس بنية AlertBot.send تماماً
(HTML + لوحة [ عرض الرسالة ][ مراسلة ]) إلى TARGET_GROUP_ID، ثم يتحقق
من الاستجابة الفعلية ويطبع تقريراً صادقاً:

  * ok=True  → الكيانات الناجية (text_link على الاسم؟) + reply_markup
               الموجود فعلاً في الرسالة المُرسَلة + وجهات كل زر.
  * ok=False → السبب الحرفي من تيليجرام.

لا يُرسل شيئاً إلى المجموعات المصدر ولا يلمس حسابات المستخدمين.
التشغيل (من مجلد المشروع، بعد ضبط البيئة):
    ALERT_BOT_TOKEN="123:abc" TARGET_GROUP_ID="-100xxxx" \
        .venv/bin/python scripts/test_alert_live.py

ملاحظات صدق (قيود لا يمكن لأي سكربت تجاوزها):
  * نجاح الإرسال يثبت: صحة HTML، ظهور النص بالقالب، إرفاق اللوحة.
  * لا يثبت: الضغط الفعلي على الأزرار (يحتاج لمساً في عميل تيليجرام
    من طرف إنسان) — لكن وجهات URL تُطبع وتُفحص شكلياً هنا.
  * كيان tg://user?id قد يُسقطه الخادم إذا كان البوت لا يعرف المرسل
    التجريبي — السكربت يكشف ذلك من الاستجابة (نفس منطق v11.3 self-heal).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from alert_bot import AlertBot, build_alert_html  # noqa: E402
from contact_target import SENDER_LABEL, utf16_len  # noqa: E402


async def main() -> int:
    token = os.environ.get("ALERT_BOT_TOKEN", "").strip()
    chat_raw = os.environ.get("TARGET_GROUP_ID", os.environ.get("ALERT_CHAT_ID", "")).strip()
    if not token or not chat_raw:
        print("❌ يلزم ALERT_BOT_TOKEN و TARGET_GROUP_ID في البيئة — لا اختبار حقيقي دون توكن.")
        return 2
    try:
        chat_id = int(chat_raw)
    except ValueError:
        print(f"❌ TARGET_GROUP_ID غير رقمي: {chat_raw!r}")
        return 2

    bot = AlertBot(token=token, chat_id=chat_id, timeout=15.0)
    me = await bot.get_me()
    if me is None:
        print(f"❌ getMe فشل — توكن غير صالح. last_error={bot.last_error}")
        return 2
    print(f"✅ getMe: @{me.get('username')} (id={me.get('id')})")

    status = await bot.check_membership()
    if status is None:
        print("⚠️ عضوية البوت في المجموعة غير مؤكدة — الإرسال قد يفشل (chat_not_found/forbidden).")

    # بيانات تنبيه تجريبي — مرسل مستخدم وهمي آمن بusername تجريبي غير موجود
    # (اختبار الشكل/الأزرار حصراً — لا تماس لبيانات حقيقية)
    data = {
        "sender_id": 1,
        "sender_username": None,
        "sender_first_name": "محمد",
        "sender_last_name": "العمري",
        "sender_type": "user",
        "text": "رسالة تجريبية للتحقق من قالب v11.6 — سطر ثانٍ <tag> & \"اقتباس\" 🔥",
        "chat_id": 0,       # بلا رابط رسالة حقيقي → زر العرض يختفي بصدق
        "message_id": None,
    }
    built = build_alert_html(data)
    # لنمنح زر «عرض الرسالة» هدفاً حقيقياً قابلاً للفحص: رابط المجموعة الهدف نفسه
    built["button_kwargs"]["group_link"] = f"https://t.me/c/{str(chat_id).replace('-100', '', 1)}/1" \
        if str(chat_id).startswith("-100") else None
    payload = bot._build_payload(built)
    payload_text = payload["text"]
    print("\n── النص المُرسل (HTML) ──")
    print(payload_text)
    print("\n── اللوحة المُرسلة ──")
    print(json.dumps(payload.get("reply_markup"), ensure_ascii=False))

    status, body = await bot._post("sendMessage", payload)
    ok = bool(status == 200 and body.get("ok"))
    print(f"\n── نتيجة Telegram الحقيقية ──\nHTTP {status} | ok={body.get('ok')}")
    if not ok:
        print(f"reason={str(body.get('description'))[:200]}")
        return 1

    result = body.get("result") or {}
    ents = result.get("entities") or []
    name_ok = any(
        e.get("type") == "text_link"
        and int(e.get("offset") or -1) == utf16_len(SENDER_LABEL)
        for e in ents
    )
    markup = result.get("reply_markup") or {}
    rows = (markup.get("inline_keyboard") or [])
    btn_texts = [b.get("text") for row in rows for b in row]
    print(f"message_id={result.get('message_id')}")
    print(f"name_clickable(entity at offset {utf16_len(SENDER_LABEL)}) = {name_ok}")
    print(f"buttons delivered = {btn_texts or 'لا شيء'}")
    for row in rows:
        for b in row:
            print(f"  زر «{b.get('text')}» → {b.get('url')}")
    print("\n── الحكم ──")
    verdict = []
    verdict.append(("القالب وصل بدون خطأ HTML", ok))
    verdict.append(("اسم المرسل قابل للنقر", name_ok))
    verdict.append(("اللوحة أُرفقت فعلاً", bool(rows)))
    all_ok = all(v for _, v in verdict)
    for label, v in verdict:
        print(("✅ " if v else "❌ ") + label)
    print("\nملاحظة: الضغط الفعلي على الأزرار يتطلب لمساً بشرياً في عميل تيليجرام —")
    print("افحص الرسالة في المجموعة يدوياً (هذا لا يستطيعه أي سكربت).")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
