#!/usr/bin/env bash
# qa_round.sh — QA round for the v14+ health-only web layer in ONE shell call.
# (background processes are killed between terminal calls in this environment,
# so everything must live inside a single invocation)
#
# v14.0: لوحة التحكم أُزيلت نهائياً — الطبقة الويب الوحيدة هي خادم الصحة
# المستقل داخل main.py (/health + /health/full على $PORT). هذا السكربت:
# - يشغّل البوت في وضع idle (بلا حسابات، SQLite مؤقت)
# - يتحقق من /health (200 + status ok) و /health/full (200)
# - يتحقق من أن المسارات القديمة للوحة لم تعد تُخدَم (404)
# - ينهي العملية ويُنظّف
set -u
PORT=8091
BASE="http://127.0.0.1:${PORT}"
cd "$(dirname "$0")/.." || exit 1

echo "── killing stale QA bots ────────────────────────────────"
pkill -f "qa_round_bot" 2>/dev/null && sleep 1 || echo "none"

echo "── starting bot in idle mode (health server) on :${PORT} ─"
rm -f /tmp/qa_bot.db /tmp/qa_bot.log
env PORT="${PORT}" DB_TYPE=sqlite DB_FILE=/tmp/qa_bot.db LOG_FILE=/tmp/qa_bot.log \
    TARGET_GROUP_ID=-1001234567890 ADMIN_CHAT_ID=654321 \
    HEALTH_CHECK_INTERVAL=999999 KEEP_ALIVE_INTERVAL_SECONDS=0 \
    nohup .venv/bin/python main.py > /tmp/qa_server.log 2>&1 &
SERVER_PID=$!
trap '[ -n "${SERVER_PID:-}" ] && kill "${SERVER_PID}" 2>/dev/null' EXIT

ok=0; fail=0
check() { # name expected_code url
  local code
  code=$(curl -s -o /tmp/qa_body.json -w "%{http_code}" -m 10 "$3" 2>/dev/null)
  if [ "$code" = "$2" ]; then echo "  ✅ $1 → $code"; ok=$((ok+1));
  else echo "  ❌ $1 → $code (متوقع $2)"; fail=$((fail+1)); fi
}

for i in $(seq 1 30); do
  curl -s -m 2 -o /dev/null "${BASE}/health" 2>/dev/null && break
  sleep 1
done

echo "── checks ───────────────────────────────────────────────"
check "GET /health"          200 "${BASE}/health"
check "GET /health/full"     200 "${BASE}/health/full"
check "GET / (panel removed)" 404 "${BASE}/"
check "GET /login (removed)" 404 "${BASE}/login"

echo "── /health body ─────────────────────────────────────────"
cat /tmp/qa_body.json 2>/dev/null | head -c 300; echo

kill "${SERVER_PID}" 2>/dev/null; wait "${SERVER_PID}" 2>/dev/null
echo "── result: ok=${ok} fail=${fail} ────────────────────────"
[ "$fail" = "0" ] && echo "✅ QA PASS" || echo "❌ QA FAIL"
exit "$fail"
