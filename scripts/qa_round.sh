#!/usr/bin/env bash
# qa_round.sh — full QA round in ONE shell call (background processes are
# killed between terminal calls in this environment, so everything — server
# + agent-browser + screenshots — must live inside a single invocation).
#
# Usage:  scripts/qa_round.sh [DASHBOARD_TOKEN]
#
# - kills any stale dashboard.py (a stale process holds port 8080 and serves
#   old code, silently corrupting QA results)
# - starts the dashboard standalone (SQLite temp DB, no Telegram accounts)
# - checks API points (200 with token / 401 without)
# - walks every tab via agent-browser and screenshots it into
#   /home/z/my-project/download/qa/
# - reports console errors

set -u
TOKEN="${1:-qa-round-token-123456}"
PORT=8080
BASE="http://127.0.0.1:${PORT}"
QA_DIR="/home/z/my-project/download/qa"
cd "$(dirname "$0")/.." || exit 1
mkdir -p "$QA_DIR"

echo "── killing stale dashboards ─────────────────────────────"
pkill -f dashboard.py 2>/dev/null && sleep 1 || echo "none"

echo "── starting dashboard standalone on :${PORT} ────────────"
rm -f /tmp/qa_bot.db /tmp/qa_bot.log
env DB_TYPE=sqlite DB_FILE=/tmp/qa_bot.db LOG_FILE=/tmp/qa_bot.log \
    TARGET_GROUP_ID=-1001234567890 ADMIN_CHAT_ID=654321 \
    DASHBOARD_AUTH_TOKEN="$TOKEN" DASHBOARD_ENABLED=false \
    nohup .venv/bin/python dashboard.py > /tmp/qa_server.log 2>&1 &
SERVER_PID=$!

for i in $(seq 1 30); do
  sleep 0.5
  curl -sf "${BASE}/health" >/dev/null 2>&1 && break
done
echo "boot health: $(curl -s -o /dev/null -w '%{http_code}' ${BASE}/health)"

echo "── API checks ───────────────────────────────────────────"
AUTH="Authorization: Bearer ${TOKEN}"
fail=0
for ep in stats accounts alerts analytics keywords blocked/senders blocked/chats settings sources rules allowed features notifications audit; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" "${BASE}/api/${ep}")
  [ "$code" = "200" ] || { echo "FAIL /api/${ep} → ${code}"; fail=1; }
done
unauth=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/api/stats")
[ "$unauth" = "401" ] || { echo "FAIL unauth /api/stats → ${unauth} (want 401)"; fail=1; }
echo "API checks done (fail=${fail})"

echo "── browser walk ─────────────────────────────────────────"
agent-browser open "${BASE}" >/dev/null 2>&1
sleep 1
# login via the token modal
agent-browser find placeholder "Dashboard token..." fill "${TOKEN}" >/dev/null 2>&1
agent-browser snapshot >/dev/null 2>&1
agent-browser click text "دخول" >/dev/null 2>&1
sleep 2

TAB_EMOJI=(
  "الرئيسية" "التنبيهات" "الإحصاء والتحليل" "الحسابات" "الرسائل"
  "الكلمات المفتاحية" "الحظر" "المصادر" "القواعد" "السماح"
  "التدقيق" "الميزات" "الإعدادات" "السجلات"
)
i=0
for tab in "${TAB_EMOJI[@]}"; do
  i=$((i+1))
  ab click text "${tab}" >/dev/null 2>&1
  sleep 1
  ab screenshot "${QA_DIR}/v928-tab-${i}.png" >/dev/null 2>&1
done

echo "── live audit trail check ───────────────────────────────"
# a real mutation must appear in the audit tab immediately
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"user_id": 99000999, "reason": "qa-live-audit"}' \
  "${BASE}/api/blocked/senders" >/dev/null
curl -s -X DELETE -H "$AUTH" "${BASE}/api/blocked/senders/99000999" >/dev/null
agent-browser click text "التدقيق" >/dev/null 2>&1
sleep 1.5
agent-browser screenshot "${QA_DIR}/v928-audit-live.png" >/dev/null 2>&1

echo "── console errors ───────────────────────────────────────"
agent-browser console 2>/dev/null | grep -i "error" | grep -v "Failed to load resource" | head -10
echo "console check done"

kill $SERVER_PID 2>/dev/null
pkill -f dashboard.py 2>/dev/null
echo "── QA round complete (fail=${fail}) ──────────────────────"
exit $fail
