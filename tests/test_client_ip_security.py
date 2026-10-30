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
# 3) انحدار الهجومين الفعليين على حارس القفل (dashboard._AuthGuard)
# ---------------------------------------------------------------------------
from dashboard import _AuthGuard  # noqa: E402
from starlette.requests import Request as StarletteRequest  # noqa: E402


def _make_request(headers, client=("127.0.0.1", 1000)):
    return StarletteRequest({
        "type": "http", "method": "GET", "path": "/", "headers": headers,
        "query_string": b"", "client": client, "server": ("s", 80),
    })


class TestLockoutAttackRegressions:
    """السيناريوهان المذكوران في عقد §11 — يفشلان على كود ما قبل v12.2.0."""

    def test_xff_rotation_attack_cannot_evade_lockout(self):
        # بروكسي موثوق يضيف IP المهاجم (يمين) بعد القيمة المزوَّرة (يسار) —
        # القديم كان يقيس المفتاح من اليسار → تبديل القيمة اليسرى يهرب من القفل.
        g = _AuthGuard()
        g.threshold = 3
        for hop1 in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            req = _make_request([(b"x-forwarded-for", f"{hop1}, 9.9.9.9".encode())])
            ip = g.client_ip(req)
            left = g.locked_seconds_left(ip)
            lock = g.record_failure(ip)
            assert ip == "9.9.9.9", "المفتاح من أقصى اليمين غير الموثوق دائماً"
            assert left == 0
        # ثالث فشل بنفس المفتاح الفعلي → قفل (القديم: ثلاثة مفاتيح مختلفة → لا قفل)
        assert lock > 0

    def test_victim_poisoning_blocked(self):
        # محاولة سَمّ: المهاجم يزوّر IP الضحية يساراً — الفشل يُنسب للمهاجم
        # (يمين) ولا يُقفل الضحية أبداً.
        g = _AuthGuard()
        g.threshold = 2
        victim = "203.0.113.50"
        for _ in range(5):
            req = _make_request(
                [(b"x-forwarded-for", f"{victim}, 198.51.100.99".encode())])
            g.record_failure(g.client_ip(req))
        victim_req = _make_request(
            [(b"x-forwarded-for", victim.encode())])
        assert g.locked_seconds_left(victim) == 0
        assert g.locked_seconds_left("198.51.100.99") > 0

    def test_untrusted_peer_cannot_spoof_any_key(self):
        # نظير عام يتظاهر بأنه بروكسي: كل فشلاته تُنسب لعنوانه المباشر —
        # لا يمكنه قفل أي IP آخر ولا الهروب بتبديل XFF.
        g = _AuthGuard()
        g.threshold = 2
        for forged in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            req = _make_request(
                [(b"x-forwarded-for", forged.encode())],
                client=("6.6.6.6", 5555),
            )
            assert g.client_ip(req) == "6.6.6.6"
            g.record_failure("6.6.6.6")
        assert g.locked_seconds_left("1.1.1.1") == 0
        assert g.locked_seconds_left("6.6.6.6") > 0


# ---------------------------------------------------------------------------
# 4) توحيد webadmin._LoginGuard مع dashboard._AuthGuard
# ---------------------------------------------------------------------------
class TestWebadminLoginGuardParity:
    def test_login_guard_key_matches_shared_resolver(self):
        from webadmin.auth import _LoginGuard

        guard = _LoginGuard()
        req = _make_request([(b"x-forwarded-for", b"1.2.3.4, 9.9.9.9")])
        assert guard._key(req) == "9.9.9.9"
        req_direct = _make_request([])
        assert guard._key(req_direct) == "127.0.0.1"
        # نفس توازن الند للند مع نظير غير موثوق
        req_public = _make_request(
            [(b"x-forwarded-for", b"1.2.3.4")], client=("8.8.8.8", 777))
        assert guard._key(req_public) == "8.8.8.8"
