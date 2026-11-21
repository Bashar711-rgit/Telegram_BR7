"""v14.8.0 — اختبارات انحدار النوايا العشرة من دليل الترقية (قائمة التحقق).

الحالات العشرة هي نصياً أمثلة دليل التنفيذ الحية (المرحلة 8.1). دفعة v11.8
أصلحت المحركات المسؤولة عنها (F-01/F-03..F-08/F-15 ثم F-13/F-14 في v11.8.1)؛
هذا الملف يثبّت العقد الكامل في مكان واحد كما طلبه الدليل — كل حالة مُشغَّلة
فعلياً على المحرك الحي (14.7.0) وتمر 10/10 قبل كتابة الملف.

تعتمد على بيئة tests/conftest.py الحتمية (لا شبكة، لا أسرار).
"""

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from filter_engine import EnhancedFilter  # noqa: E402


@pytest.fixture(scope="module")
def f():
    return EnhancedFilter()


CASES = [
    # (النص, القرارات المسموحة)
    ("أبي حل", {"review", "accept"}),
    ("تكفون أحد يساعدني", {"review", "accept"}),
    ("من يعرف يحلها", {"review", "accept"}),
    ("محتاج أحد يساعدني", {"review", "accept"}),
    ("أبي أحد يساعدني في المادة", {"accept"}),
    ("من يعرف شخص يساعدني", {"review", "accept"}),
    ("محاضرة اليوم في الجامعة كانت مفيدة", {"ignore"}),
    ("نقدم حلول واجبات ومساعدة تواصل الآن", {"ignore"}),
    ("نقدم حل واجبات جميع المواد اسعار مميزه ابي احد يجرب", {"ignore"}),
    ("ما ابي حل", {"ignore"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text,allowed", CASES)
async def test_guide_intent_contract(f: EnhancedFilter, text: str, allowed: set):
    r = await f.analyze(text)
    decision = r["decision"] if isinstance(r, dict) else r.decision
    assert decision in allowed, (text, decision, r)
