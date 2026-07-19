# 🤖 Telegram Bot v13.0 – IntentEngine Edition (Render Cloud)

نظام متقدم لمراقبة مجموعات تيليجرام واصطياد طلبات المساعدة الطلابية، مهيأ للتشغيل **24/7 على Render المجاني** كـ Web Service.

---

## 🚀 أبرز التعديلات لنسخة Render

- **Web Service على الخطة المجانية** — منفذ واحد `$PORT` يخدم لوحة التحكم + `/health`.
- **صفحة تسجيل دخول مدمجة `/login`** — توليد Session String لكل حساب من المتصفح (OTP + 2FA) وحفظه تلقائياً في متغيرات Render عبر Render API مع إعادة نشر تلقائية.
- **دعم `*_SESSION_STRING`** — لا حاجة لملفات جلسة أو إدخال تفاعلي.
- **Keep-Alive ذاتي** — ping دوري كل 10 دقائق لمنع سبات الخطة المجانية (يُنصح أيضاً بربط UptimeRobot على `/health`).
- **إصلاح `keywords.json`** وإعادة تسمية `dashboard.py` للتوافق مع لينكس.

---

## 📂 هيكل المشروع

```
├── main.py                 # نقطة التشغيل الرئيسية
├── config.py               # مدير الإعدادات (+SESSION_STRING لكل حساب)
├── database.py             # طبقة قاعدة البيانات (SQLite/PostgreSQL)
├── filter_engine.py        # محرك الفلترة IntentEngine
├── monitors.py             # مراقبو الحسابات (StringSession)
├── dashboard.py            # لوحة التحكم + /health + /login
├── keywords.json           # قاعدة الكلمات المفتاحية
├── templates/dashboard.html
├── generate_session.py     # توليد Session String محلياً (احتياطي)
├── render.yaml             # Blueprint (بدون أسرار)
├── requirements.txt
├── runtime.txt             # python-3.11.9
└── .gitignore              # يستثني accounts.env والجلسات والأسرار
```

---

## ☁️ النشر على Render (الطريقة المعتمدة)

1. ارفع المشروع إلى GitHub (بدون `accounts.env` — محمي بـ `.gitignore`).
2. أنشئ **Web Service** جديداً (plan: free) واربطه بالمستودع:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python main.py`
   - Health Check Path: `/health`
3. أضف متغيرات البيئة من لوحة Render > Environment (انظر الجدول أدناه).
4. بعد تشغيل الخدمة افتح `https://<service>.onrender.com/login` وسجّل كل حساب برمز OTP — تُحفظ الجلسة تلقائياً ويُعاد النشر.
5. (موصى به) اربط [UptimeRobot](https://uptimerobot.com) بـ `/health` كل 5 دقائق لضمان عدم السبات.

> **ملاحظة:** قاعدة SQLite على الخطة المجانية مؤقتة (تُصفَّر عند إعادة النشر). للاستمرارية أنشئ PostgreSQL وأضف `DATABASE_URL`.

---

## 🔑 متغيرات البيئة المطلوبة

| المتغير | الوصف |
|---|---|
| `TARGET_GROUP_ID` | معرف المجموعة المستهدفة للتنبيهات |
| `ADMIN_CHAT_ID` | معرف شات الأدمن |
| `DASHBOARD_ENABLED` | `true` |
| `DASHBOARD_AUTH_TOKEN` | رمز قوي للوحة التحكم و`/login` |
| `SECRET_KEY_OVERRIDE` | مفتاح Fernet |
| `RENDER_API_KEY` | مفتاح Render API (لحفظ الجلسات تلقائياً من `/login`) |
| `RENDER_SERVICE_ID` | معرف الخدمة `srv-...` |
| `{PREFIX}_API_ID / _API_HASH / _PHONE / _SESSION_NAME` | بيانات كل حساب (MAIN, ACCOUNT_1..5) |
| `{PREFIX}_SESSION_STRING` | تُملأ تلقائياً عبر `/login` أو يدوياً |

متغيرات IntentEngine والأداء لها قيم افتراضية جاهزة (انظر `render.yaml`).

---

## 🔐 توليد Session String يدوياً (اختياري)

على هاتفك أو جهازك:

```bash
pip install telethon
python generate_session.py
```

ثم انسخ الناتج إلى `{PREFIX}_SESSION_STRING` في متغيرات Render.

---

## 📋 أوامر الأدمن (في شات الأدمن)

`/help` · `/stats` · `/status` · `/accounts` · `/health` · `/filter_stats` · `/dashboard` · `/block <id>` · `/unblock <id>` · `/purge`

---

## 🌐 لوحة التحكم

- `/` — لوحة التحكم الكاملة (تحتاج `DASHBOARD_AUTH_TOKEN`)
- `/login` — تسجيل دخول الحسابات وتوليد الجلسات
- `/health` — فحص الصحة (بدون مصادقة) لـ Render وUptimeRobot

---

📅 **الإصدار:** v13.0 – IntentEngine Edition (Render Cloud) – 2026-07
