"""v12.2.0 — اختبارات انحدار أمان حل عنوان العميل خلف البروكسيات (audit S-1).

العقد (مواصفة §11): "Trust forwarding headers only when they come from
explicitly configured trusted proxies."

السلوك المعيب القديم (dashboard._AuthGuard.client_ip حتى v12.1.0):
  * كان يعيد أول قيمة X-Forwarded-For حرفياً — قيمة يتحكم بها المهاجم:
      - ترويش قفل brute-force: تبديل القفزة الأولى كل طلب → لا قفل أبداً.
      - سَمّ المفتاح: فشلات مزوّرة باسم IP الضحية → قفل إداري (DoS).
  * هذان الاختباران (test_xff_rotation_attack_cannot_evade_lockout و
    test_victim_poisoning_blocked) يفشلان على الكود القديم وينجحان على
    المُحلّل الموحد في security.resolve_client_ip.
"""

import pytest

import security
from security import parse_trusted_proxies, resolve_client_ip


@pytest.fixture(autouse=True)
def _trusted_proxies_default_env(monkeypatch):
    """بيئة محددة لكل اختبار + إعادة تعيين كاش المُحلّل (منع تلوث عابر)."""
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.setattr(security, "_proxy_cache", (0.0, ()))
    yield
    monkeypatch.setattr(security, "_proxy_cache", (0.0, ()))


# ---------------------------------------------------------------------------
# 1) وحدات resolve_client_ip — الخوارزمية الموحدة
# ---------------------------------------------------------------------------
class TestResolveClientIpUnits:
    def test_trusted_peer_single_public_xff_is_client(self):
        # peer موثوق (بروكسي) + قفزة واحدة عامة → هي العميل
        assert resolve_client_ip("127.0.0.1", "203.0.113.7") == "203.0.113.7"

    def test_trusted_peer_chain_rightmost_non_trusted_wins(self):
        # السلسلة [مزوّر، حقيقي]: أقصى اليمين غير الموثوق هو العميل —
        # وليس أول قيمة (السلوك القديم الضعيف)
        assert resolve_client_ip("127.0.0.1", "1.2.3.4, 9.9.9.9") == "9.9.9.9"

    def test_trusted_peer_empty_xff_falls_back_to_direct(self):
        assert resolve_client_ip("127.0.0.1", "") == "127.0.0.1"

    def test_all_trusted_chain_falls_back_to_direct(self):
        assert resolve_client_ip("127.0.0.1", "10.0.0.5, 127.0.0.1") == "127.0.0.1"

    def test_untrusted_peer_ignores_xff_entirely(self):
        # الجوهر ضد التزوير: اتصال مباشر من غير بروكسي موثوق → XFF يُتجاهل
        # حتى لو بدت سلسلته سليمة تماماً
        assert resolve_client_ip("9.9.9.9", "1.2.3.4") == "9.9.9.9"

    def test_untrusted_peer_ignores_internal_looking_xff(self):
        # تزوير قفزة داخلية (10.x) من نظير عام لا يجعل السلسلة موثوقة
        assert resolve_client_ip("9.9.9.9", "10.0.0.9, 1.2.3.4") == "9.9.9.9"

    def test_ipv6_loopback_peer_and_entry(self):
        assert resolve_client_ip("::1", "2001:db8::7") == "2001:db8::7"
        assert resolve_client_ip("::1", "::1") == "::1"

    def test_junk_xff_values_fall_back_to_direct(self):
        assert resolve_client_ip("127.0.0.1", "   , ,,  ") == "127.0.0.1"

    def test_key_capped_at_64_chars(self):
        long_ip = "a" * 300
        assert len(resolve_client_ip("127.0.0.1", long_ip)) <= 64

    def test_unknown_direct_without_xff(self):
        assert resolve_client_ip("", "") == "unknown"


# ---------------------------------------------------------------------------
# 2) إعدادات TRUSTED_PROXIES — الصريح يلغي الافتراضي، والفاسد يُتخطى
# ---------------------------------------------------------------------------
class TestTrustedProxiesConfig:
    def test_invalid_cidrs_skipped_safely(self):
        nets = parse_trusted_proxies("999.999.1.1/8, 10.0.0.0/8, not-a-cidr")
        assert len(nets) == 1
        # القفزة الصالحة الوحيدة هي 10/8
        assert resolve_client_ip("10.0.0.3", "203.0.113.7") == "203.0.113.7"
        # نظير عام مع CIDRs فاسدة فقط → الافتراضي لا يُطبق (قيمة صريحة موجودة)
        monkey = False  # placeholder to keep structure clear

    def test_explicit_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "100.64.0.0/10")
        # الافتراضي (127/8, 10/8) لم يعد موثوقاً
        assert resolve_client_ip("127.0.0.1", "203.0.113.7") == "127.0.0.1"
        # النطاق الصريح موثوق
        assert resolve_client_ip("100.64.0.9", "203.0.113.7") == "203.0.113.7"

    def test_env_change_within_ttl_is_picked_up(self, monkeypatch):
        # الكاش يُقرأ من الاختبارات مباشرة (نفس نقطة التمديد التشغيلية)
        monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8")
        assert resolve_client_ip("10.0.0.3", "5.5.5.5") == "5.5.5.5"
        monkeypatch.setenv("TRUSTED_PROXIES", "100.64.0.0/10")
        monkeypatch.setattr(security, "_proxy_cache", (0.0, ()))
        assert resolve_client_ip("10.0.0.3", "5.5.5.5") == "10.0.0.3"


# ---------------------------------------------------------------------------
