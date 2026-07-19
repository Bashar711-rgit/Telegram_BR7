#!/bin/bash
# ================================================================
# start.sh – Telegram Bot v13.0 (IntentEngine Edition)
# Usage: ./start.sh [local|render]
# متوافق مع: Render, Local, Termux
# ================================================================

set -e

MODE="${1:-local}"

# ========== الألوان للطباعة ==========
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=========================================="
echo "  🤖 Telegram Bot v13.0"
echo "  IntentEngine Edition"
echo -e "==========================================${NC}"
echo -e "${YELLOW}📌 Mode: $MODE${NC}"

# ========== التحقق من Python ==========
echo -e "${BLUE}📌 Checking Python version...${NC}"
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo -e "${GREEN}✅ Python version: $python_version${NC}"

# التحقق من أن Python >= 3.10
python_major=$(echo $python_version | cut -d. -f1)
python_minor=$(echo $python_version | cut -d. -f2)
if [ "$python_major" -lt 3 ] || [ "$python_minor" -lt 10 ]; then
    echo -e "${RED}❌ Python 3.10+ is required!${NC}"
    exit 1
fi

# ========== تثبيت المكتبات ==========
echo -e "${BLUE}📌 Installing dependencies...${NC}"

if [ "$MODE" = "local" ]; then
    if [ ! -d ".venv" ]; then
        echo -e "${YELLOW}📌 Creating virtual environment...${NC}"
        python3 -m venv .venv
    fi
    echo -e "${YELLOW}📌 Activating virtual environment...${NC}"
    source .venv/bin/activate
else
    # في Render، استخدم البيئة الافتراضية إن وجدت
    if [ -d ".venv" ]; then
        source .venv/bin/activate
    fi
fi

# تثبيت المكتبات (مع تجاهل الأخطاء البسيطة)
pip install -r requirements.txt --quiet 2>/dev/null || {
    echo -e "${YELLOW}⚠️  Retrying installation...${NC}"
    pip install -r requirements.txt
}

echo -e "${GREEN}✅ Dependencies installed${NC}"

# ========== التحقق من البيئة ==========
echo -e "${BLUE}📌 Checking environment...${NC}"

# التحقق من ملف accounts.env
if [ ! -f "accounts.env" ] && [ "$MODE" = "local" ]; then
    echo -e "${YELLOW}⚠️  WARNING: accounts.env not found!${NC}"
    echo -e "${YELLOW}   Please create accounts.env with your credentials${NC}"
    exit 1
fi

# التحقق من وجود keywords.json
if [ ! -f "keywords.json" ]; then
    echo -e "${RED}❌ ERROR: keywords.json not found!${NC}"
    exit 1
fi
echo -e "${GREEN}✅ keywords.json found${NC}"

# التحقق من وجود main.py
if [ ! -f "main.py" ]; then
    echo -e "${RED}❌ ERROR: main.py not found!${NC}"
    exit 1
fi
echo -e "${GREEN}✅ main.py found${NC}"

# ========== التحقق من قاعدة البيانات ==========
echo -e "${BLUE}📌 Database check...${NC}"

if [ "$DB_TYPE" = "postgresql" ] || [ "$MODE" = "render" ]; then
    if [ -n "$DATABASE_URL" ]; then
        echo -e "${GREEN}✅ PostgreSQL: DATABASE_URL set${NC}"
    else
        echo -e "${YELLOW}⚠️  WARNING: DATABASE_URL not set! Falling back to SQLite${NC}"
        export DB_TYPE=sqlite
    fi
else
    echo -e "${GREEN}✅ Database: SQLite${NC}"
fi

# ========== إنشاء المجلدات اللازمة ==========
echo -e "${BLUE}📌 Creating required directories...${NC}"
mkdir -p downloads sessions templates 2>/dev/null || true
echo -e "${GREEN}✅ Directories created${NC}"

# ========== إعداد متغيرات إضافية للـ Dashboard ==========
if [ "$DASHBOARD_ENABLED" = "true" ]; then
    echo -e "${GREEN}✅ Dashboard enabled on port ${DASHBOARD_PORT:-8080}${NC}"
fi

# ========== عرض معلومات النظام ==========
echo -e "${BLUE}=========================================="
echo -e "  📊 System Information"
echo -e "==========================================${NC}"
echo -e "🖥️  OS: $(uname -a | cut -d' ' -f1-3)"
echo -e "🧠 Memory: $(free -h 2>/dev/null | grep Mem | awk '{print $2}' || echo 'N/A')"
echo -e "💾 Disk: $(df -h . 2>/dev/null | tail -1 | awk '{print $2" used "$3" free "$4}' || echo 'N/A')"
echo -e "🐍 Python: $python_version"

# ========== بدء التشغيل ==========
echo -e "${GREEN}=========================================="
echo -e "  🚀 Starting Bot v13.0..."
echo -e "==========================================${NC}"

# تشغيل البوت مع إعادة التشغيل التلقائي في حالة الفشل (اختياري)
if [ "$MODE" = "render" ]; then
    # في السحابة، نريد إعادة تشغيل تلقائي عند الفشل
    while true; do
        echo -e "${GREEN}🔄 Starting bot...${NC}"
        python3 main.py
        echo -e "${YELLOW}⚠️  Bot stopped with exit code $?${NC}"
        echo -e "${YELLOW}🔄 Restarting in 5 seconds...${NC}"
        sleep 5
    done
else
    # في الوضع المحلي، شغل مرة واحدة
    python3 main.py
fi
