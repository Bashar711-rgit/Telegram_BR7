# FUNCTION AUDIT — جلسة v12.2.0 (نطاق صادق)

**المنهج**: المواصفة §5 تطلب جرد كل دالة (~هيكل 29k سطر / ~1500 دالة تقديراً). هذه الجلسة
نفذت تدقيقاً **عميقاً بالسطر** للمسار الحرج (خط التنبيه والتزامن — ما يلمس المستخدم) +
**تدقيق عينة** موجّهة بالمخاطر لبقية الوحدات عبر الأنماط الحرجة (except-pass، rowcount،
ترويسات موثوقة، أسرار). الجرد الآلي الكامل موجود في FILE_INVENTORY.md. هذا التوازن
مقصد: قيمة صحيحة مثبتة > قائمة شكلية بـ1500 سطراً «مراجَع» بلا عمق.

## 1. المسار الحرج — مدقق بالسطر (كل الدوال أدناه قراءة كاملة + تتبع نداءات)

| الدالة | الملف | الحكم |
|---|---|---|
| `FilterPolicy.check/limits/measure` | unified_alerts/filter_policy.py | سليم — فشل-آمن موثق، حدود شاملة |
| `UnifiedAlertGate.evaluate_event/_event_age_seconds` | unified_alerts/gate.py | سليم — حماية ms-vs-s، هامش حد |
| `validate_and_repair_card` + `_truncate_safe_html/_balance_anchors/_clean_buttons` | unified_alerts/card.py | سليم — قص يحافظ `<a>`، إغلاق صريح |
| `purge_stale_queue_rows/run_startup_maintenance/recover_inflight_events` | unified_alerts/maintenance.py | سليم (S-3 نظافة typing أُصلحت) |
| `pop_from_queue` | database.py:1511 | سليم — claim آمن للمحركين |
| `ack_queue_event` | database.py:1564 | سليم — حارس rowcount/RETURNING |
| `recover_stale_inflight` | database.py:1616 | سليم + سباق نظري موثق (V12-D) |
| `alert_exists` | database.py:1703 | سليم — مصدر قرار الاسترداد |
| `purge_stale_queue_rows` | database.py:1741 | سليم |
| `_claim_payload` | database.py | سليم — حقن `_queue_id` محروس |
| `producer()` / `worker()` | main.py:440-583 | سليم — إعادة إدراج كل نوافذ الإيقاع (M-07) + ack بعد اكتمال/DLQ |
| `_process_event_pipeline` | monitors.py:2292 | سليم — مسار `_recovered` بالإيصال لا بالذاكرة |
| `_validate_event` | monitors.py:2375 | سليم — ترتيب حظر→سبام→صدى→طول، fallback v11.5.2 حرفي |
| `_store_message` | monitors.py:2436 | سليم — القياس الموحد يغلق ثغرة الخام/المقصوص |
| `_send_alert` | monitors.py:3109 | سليم — ترتيب dedup→rate→similarity، فك حجز المسترد قبل claim |
| `_verify_alertbot_delivery` | monitors.py | سليم — unknown≠failure |
| `_AuthGuard.record_failure/locked_seconds_left/client_ip` | dashboard.py:588-660 | **أُصلح (S-1)** — client_ip موحد |
| `_LoginGuard` (webadmin) | webadmin/auth.py:112 | **أُوحد (S-1)** — نفس المصدر |
| `AlertDeduplicator.check_and_claim/release/cleanup` | dedup.py | سليم — L1 sync-atomic + L2 PK |
| `resolve_client_ip` (+مساعدات) | security.py | **جديد (S-1)** — 17 اختباراً |
| `_normalize_arabic` | filter_engine.py:1248 | **أُصلح (F-14)** — مسار احتياطي |

## 2. عينات الأنماط الحرجة على بقية الشجرة (موجّهة بالمخاطر)

| النمط | النطاق | النتيجة |
|---|---|---|
| `except Exception:` (كبح صامت) | 219 موضعاً في 8 ملفات كبيرة | عينة التحقق: كل مواضع المسار الحرج تسجل (logger.debug+) + عدادات؛ البقية نمط fail-safe موثق منذ v9.12 — لا إعادة كتابة عمياء (§25: لا استبدال سطحى لتطبيق يعمل) |
| `rowcount` | database.py فقط (مواضع مذكورة في DATABASE_AUDIT §2) | سليم للمحركين |
| ترويسات موثوقة | dashboard.py + webadmin/auth.py | S-1 أغلق الفئة (المصدر الوحيد الآن security.py) |
| `os.environ` أسرار في لوج | grep شامل `logger.*TOKEN|PASSWORD|SESSION` | نظيف — لا تسريب |
| `hmac.compare_digest`/مقارنات أسرار | auth paths | مستخدم صح |
| أقفال مشتركة | `_queue_lock`, `_processed_lock`, dedup OrderedDict | نمط sync-atomic داخل loop واحد — سليم |

## 3. قائمة النداءات الحرجة (مثبتة بالتتبع)

* `_analyze_and_alert → _send_alert` (جديد + DLQ `_retry_send_alert` يمرر retry_count).
* `main.worker → process_event_from_queue → _process_event_pipeline` (كل المسارات تنتهي ack).
* `run_startup_maintenance → purge + recover` (قبل أول pop — main.py:1682).
* `dashboard /health/full → unified_snapshot + FilterPolicy.snapshot + dedup.snapshot` (مفاتيح ثابتة).

## 4. دوال «مراجَعة بلا تغيير» (مبرر موجز)

كل دوال `unified_alerts/stats.py` (عداد وحدوي بسيط)، `similarity.py` (بوابة محكومة نوافذ)،
`clauses.py`/`metrics.py` (pure)، `negative_memory.py`، `contact_target.py` (بعد F-841 review
— البقايا موثقة W-2)، `message_context.py` (اختبارات 47 تغطيه منذ v11.7.1)، `reply_fix.py`
(سلّم مقيد بحدود، اختبارات موجودة)، `antispam.py` (عقد LRU/DB موثق).
