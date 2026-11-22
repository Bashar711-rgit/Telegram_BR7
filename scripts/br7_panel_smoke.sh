#!/usr/bin/env bash
# فحص دخان للوحة v15.2.0 — ربط حساب تيليجرام جديد عبر /admin.
# يشغّل البوت بإعدادات مينيمال ويفحص: الصحة، اللوحة، المصادقة+CSRF،
# معالج الربط (تحقق الرقم/منع التكرار/استدعاء تيليجرام الفعلي)، وجدول panel_accounts.
set -e
cd /home/z/my-project/br7_bot

export DB_TYPE=sqlite
export DB_FILE=/tmp/br7_smoke3.db
rm -f /tmp/br7_smoke3.db
export PORT=18080
export KEEP_ALIVE_INTERVAL_SECONDS=0
export TARGET_GROUP_ID=1
export ADMIN_CHAT_ID=1
export LOG_FILE=/tmp/br7_smoke3.log
export PYTHONPATH=/home/z/my-project/br7_bot
export MAIN_API_ID=123456
export MAIN_API_HASH=0123456789abcdef0123456789abcdef
export MAIN_PHONE=+9999999999
export MAIN_SESSION_NAME=smoke3
export DASHBOARD_USERNAME=bashar
export DASHBOARD_PASSWORD=bashar123r

.venv/bin/python main.py > /tmp/br7_smoke3_boot.log 2>&1 &
BOT_PID=$!
cleanup() { kill $BOT_PID 2>/dev/null || true; }
trap cleanup EXIT

for i in $(seq 1 40); do
  if curl -s -m 2 "http://127.0.0.1:18080/health" | grep -q 'status'; then break; fi
  sleep 1
done

echo '=== 1) /health ==='
curl -s -m 5 http://127.0.0.1:18080/health | head -c 120; echo
echo "panel_html=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/admin)"
echo "panel_js=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/admin/assets/app.js)"

echo '=== 2) API محمي بلا جلسة → 401 ==='
echo "accounts=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/admin/api/accounts)"
echo "add_start=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:18080/admin/api/accounts/add/start -H 'Content-Type: application/json' -d '{"phone":"+966501234567"}')"

echo '=== 3) دخول bashar/bashar123r → كوكي جلسة ==='
curl -s -c /tmp/br7_dash_cookies.txt -X POST http://127.0.0.1:18080/admin/api/login \
  -H 'Content-Type: application/json' -d '{"username":"bashar","password":"bashar123r"}' | head -c 120; echo
TOKEN=$(grep br7dash /tmp/br7_dash_cookies.txt | awk '{print $7}')
echo "cookie_len=${#TOKEN}"

echo '=== 4) CSRF مفقود في POST → 403 ==='
echo "no_csrf=$(curl -s -o /dev/null -w '%{http_code}' -b /tmp/br7_dash_cookies.txt -X POST http://127.0.0.1:18080/admin/api/accounts/add/cancel -H 'Content-Type: application/json' -d '{}')"

echo '=== 5) رقم غير صالح → phone_invalid ==='
curl -s -b /tmp/br7_dash_cookies.txt -X POST http://127.0.0.1:18080/admin/api/accounts/add/start \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $TOKEN" -d '{"phone":"abc"}' | head -c 150; echo

echo '=== 6) رقم صالح → استدعاء تيليجرام فعلي (ApiIdInvalid متوقع لبيانات وهمية) ==='
curl -s -m 60 -b /tmp/br7_dash_cookies.txt -X POST http://127.0.0.1:18080/admin/api/accounts/add/start \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $TOKEN" -d '{"phone":"+966501234567"}' | head -c 200; echo

echo '=== 7) منع التكرار: إدراج مسبق بنفس الرقم ثم بدء ثانٍ ==='
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('/tmp/br7_smoke3.db')
c.execute("INSERT OR IGNORE INTO panel_accounts (phone, display_name, session_encrypted, status, monitoring_enabled) VALUES ('+201122334455', 'سابق', 'x', 'ready', 1)")
c.commit()
print("pre-inserted:", c.execute("SELECT id, phone FROM panel_accounts").fetchall())
PY
curl -s -m 15 -b /tmp/br7_dash_cookies.txt -X POST http://127.0.0.1:18080/admin/api/accounts/add/start \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $TOKEN" -d '{"phone":"+201122334455"}' | head -c 150; echo

echo '=== 8) حالة عملية غير موجودة → 404 ==='
echo "status404=$(curl -s -o /dev/null -w '%{http_code}' -b /tmp/br7_dash_cookies.txt 'http://127.0.0.1:18080/admin/api/accounts/add/status?op_id=nope')"

echo '=== 9) قائمة الحسابات تتضمن صف اللوحة ==='
curl -s -b /tmp/br7_dash_cookies.txt http://127.0.0.1:18080/admin/api/accounts | head -c 400; echo

echo '=== 10) سجلات اللوحة ==='
grep -E '\[dashboard' /tmp/br7_smoke3.log | tail -4
echo 'SMOKE3_DONE'
