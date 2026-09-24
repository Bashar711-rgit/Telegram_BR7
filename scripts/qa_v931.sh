#!/usr/bin/env bash
# qa_v931.sh — QA for v9.29 (RBAC) + v9.30 (notifications) + v9.31 (GitHub panel)
# ONE shell call: server + agent-browser (background processes die between calls).
set -u
TOKEN="${1:-qa-round-token-123456}"
PORT=8080
BASE="http://127.0.0.1:${PORT}"
QA_DIR="/home/z/my-project/download/qa"
cd "$(dirname "$0")/.." || exit 1
mkdir -p "$QA_DIR"

echo "── killing stale dashboards ─────────────────────────────"
pkill -f dashboard.py 2>/dev/null && sleep 1 || echo "none"

echo "── starting dashboard standalone (webadmin enabled) ─────"
rm -f /tmp/qa_bot.db /tmp/qa_bot.log
env DB_TYPE=sqlite DB_FILE=/tmp/qa_bot.db LOG_FILE=/tmp/qa_bot.log \
    TARGET_GROUP_ID=-1001234567890 ADMIN_CHAT_ID=654321 \
    DASHBOARD_AUTH_TOKEN="$TOKEN" DASHBOARD_ENABLED=false \
    DASHBOARD_USERNAME=qaadmin DASHBOARD_PASSWORD=qa-admin-pass-99 \
    nohup .venv/bin/python dashboard.py > /tmp/qa_server.log 2>&1 &
SERVER_PID=$!
for i in $(seq 1 30); do sleep 0.5; curl -sf "${BASE}/health" >/dev/null 2>&1 && break; done
echo "boot health: $(curl -s -o /dev/null -w '%{http_code}' ${BASE}/health)"

AUTH="Authorization: Bearer ${TOKEN}"
fail=0
note() { echo "$1"; [ "$2" != "0" ] && fail=1; }

echo "── API checks: RBAC ─────────────────────────────────────"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" "${BASE}/api/users");        note "GET /api/users (master) → $code (want 200)" $([ "$code" = 200 ]; echo $?)
code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/api/users");                  note "GET /api/users (no auth) → $code (want 401/403)" $([ "$code" = 401 ] || [ "$code" = 403 ]; echo $?)
me=$(curl -s -H "$AUTH" "${BASE}/api/auth/me")
echo "auth/me (master): $(echo "$me" | head -c 120)"

# create admin + viewer via master, then exercise role matrix
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"username":"qa.admin","password":"qa-pass-12345","role":"admin"}' "${BASE}/api/users" >/dev/null
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"username":"qa.viewer","password":"qa-pass-12345","role":"viewer"}' "${BASE}/api/users" >/dev/null
AT=$(curl -s -X POST -H "Content-Type: application/json" -d '{"username":"qa.admin","password":"qa-pass-12345"}' "${BASE}/api/auth/login" | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")
VT=$(curl -s -X POST -H "Content-Type: application/json" -d '{"username":"qa.viewer","password":"qa-pass-12345"}' "${BASE}/api/auth/login" | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")
[ -n "$AT" ] && echo "admin login → token OK" || { echo "admin login FAILED"; fail=1; }
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${AT}" "${BASE}/api/users"); note "GET /api/users (admin token) → $code (want 200)" $([ "$code" = 200 ]; echo $?)
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${VT}" "${BASE}/api/users"); note "GET /api/users (viewer token) → $code (want 403)" $([ "$code" = 403 ]; echo $?)
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer ${VT}" -H "Content-Type: application/json" -d '{"username":"nope","password":"longenough1"}' "${BASE}/api/users"); note "POST /api/users (viewer) → $code (want 403)" $([ "$code" = 403 ]; echo $?)

echo "── API checks: notifications + github ───────────────────"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" "${BASE}/api/notifications"); note "GET /api/notifications → $code (want 200)" $([ "$code" = 200 ]; echo $?)
code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/bot/github/status");           note "GET /bot/github/status (no session) → $code (want 401/403)" $([ "$code" = 401 ] || [ "$code" = 403 ]; echo $?)

echo "── browser: BotPanel bell ───────────────────────────────"
agent-browser open "${BASE}" >/dev/null 2>&1
sleep 1
agent-browser find placeholder "Dashboard token..." fill "${TOKEN}" >/dev/null 2>&1
agent-browser press Enter >/dev/null 2>&1
sleep 2.5
# seed a notification via lockout-free path: user.created event (create + delete qa.tmp user)
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"username":"qa.tmp","password":"qa-pass-12345","role":"operator"}' "${BASE}/api/users" >/dev/null
sleep 6   # wait for a WS stats frame carrying notifications_unread
agent-browser screenshot "${QA_DIR}/v931-topbar-bell.png" >/dev/null 2>&1
agent-browser eval "document.getElementById('notifBell') ? 'bell-present' : 'BELL-MISSING'" 2>/dev/null
agent-browser eval "toggleNotifPanel(new Event('x'))" >/dev/null 2>&1
sleep 1.5
agent-browser screenshot "${QA_DIR}/v931-notif-panel.png" >/dev/null 2>&1
agent-browser eval "document.getElementById('notifList').innerText.slice(0,200)" 2>/dev/null

echo "── browser: webadmin users tab + control github card ────"
agent-browser open "${BASE}/admin" >/dev/null 2>&1
sleep 1.5
agent-browser snapshot 2>/dev/null | head -30
agent-browser find placeholder "اسم المستخدم" fill "qaadmin" >/dev/null 2>&1 || agent-browser eval "document.querySelector('input[type=text],input:not([type])')?.focus()" >/dev/null 2>&1
agent-browser find placeholder "كلمة المرور" fill "qa-admin-pass-99" >/dev/null 2>&1
agent-browser press Enter >/dev/null 2>&1
sleep 2
agent-browser eval "typeof navigate==='function' ? navigate('users') : 'NO-NAVIGATE'" 2>/dev/null
sleep 1.5
agent-browser screenshot "${QA_DIR}/v931-users-tab.png" >/dev/null 2>&1
agent-browser eval "document.getElementById('usr-list')?.innerText.slice(0,300)" 2>/dev/null
agent-browser eval "navigate('control')" >/dev/null 2>&1
sleep 2
agent-browser screenshot "${QA_DIR}/v931-control-github.png" >/dev/null 2>&1
agent-browser eval "document.getElementById('github-body')?.innerText.slice(0,250)" 2>/dev/null

echo "── console errors ───────────────────────────────────────"
agent-browser console 2>/dev/null | grep -i "error" | grep -v "Failed to load resource" | head -8
echo "console check done"

kill $SERVER_PID 2>/dev/null
pkill -f dashboard.py 2>/dev/null
echo "── QA v9.31 complete (fail=${fail}) ──────────────────────"
exit $fail
