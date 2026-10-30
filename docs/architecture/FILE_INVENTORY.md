# FILE INVENTORY — Telegram_BR7 (مولَّد آلياً من الشجرة الفعلية)

تاريخ التوليد: جلسة v12.2.0 | إجمالي الملفات المتتبعة من الأنواع المدققة: 118

## ملفات Python (مرتبة بالحجم)

| الملف | الأسطر |
|---|---|
| database.py | 5741 |
| dashboard.py | 4583 |
| monitors.py | 4205 |
| filter_engine.py | 2515 |
| main.py | 2094 |
| config.py | 1387 |
| sender_resolver.py | 1154 |
| alert_bot.py | 1103 |
| webadmin/routes.py | 924 |
| nav_resolver.py | 885 |
| message_context.py | 851 |
| tests/test_nav_resolver.py | 839 |
| tests/test_contact_target_v11_2.py | 735 |
| antispam.py | 717 |
| tests/test_sender_mention_v2.py | 667 |
| tests/test_alert_bot_v10_8.py | 638 |
| tests/test_sender_access_v10_7.py | 637 |
| tests/test_sender_resolver.py | 635 |
| contact_target.py | 592 |
| dashboard_store.py | 592 |
| tests/test_message_context.py | 558 |
| tests/test_rbac.py | 546 |
| tests/test_dashboard_store.py | 503 |
| tests/test_alert_ui_v11.py | 471 |
| reply_fix.py | 470 |
| tests/test_alert_template_v11_6.py | 438 |
| tests/test_audit_v11_7.py | 419 |
| tests/test_fast_capture.py | 408 |
| alert_ui.py | 405 |
| tests/test_otp_delivery.py | 386 |
| security.py | 376 |
| tests/test_qr_sms_login.py | 368 |
| tests/test_alert_regression.py | 363 |
| tests/test_account_management.py | 354 |
| classifier.py | 345 |
| tests/test_dashboard.py | 338 |
| tests/test_unified_v12_1.py | 338 |
| scripts/apply_br7_schema.py | 328 |
| tests/test_intent_fixes_v11_8.py | 315 |
| tests/test_v11_7_1_regressions.py | 313 |
| similarity.py | 298 |
| tests/test_echo_guard_v11_5_2.py | 296 |
| tests/test_v11_8_2_fixes.py | 296 |
| tests/test_auth_guard_retention.py | 272 |
| dedup.py | 270 |
| tests/test_reply_fix_v11_5.py | 267 |
| tests/test_rules.py | 266 |
| webadmin/keywords_store.py | 264 |
| webadmin/backup.py | 259 |
| tests/test_antispam_edges.py | 256 |
| unified_alerts/card.py | 252 |
| tests/test_precision_upgrade.py | 246 |
| tests/test_intel_store_v11_1.py | 245 |
| tests/test_accounts_db.py | 242 |
| tests/test_filter_accuracy.py | 223 |
| tests/test_length_filter_v11_5.py | 219 |
| tests/test_database.py | 214 |
| tests/test_runtime_monitor_replacement.py | 211 |
| tests/test_data_backup.py | 201 |
| tests/test_dedup.py | 196 |
| webadmin/auth.py | 191 |
| tests/test_filter_lab.py | 191 |
| tests/test_intent_fixes_v11_8_1.py | 182 |
| webadmin/render_api.py | 179 |
| tests/test_client_ip_security.py | 179 |
| tests/test_rbac_expansion.py | 167 |
| unified_alerts/gate.py | 165 |
| negative_memory.py | 164 |
| unified_alerts/filter_policy.py | 161 |
| webadmin/github_api.py | 153 |
| tests/test_filter_engine.py | 151 |
| tests/test_monitors.py | 149 |
| tests/test_features.py | 146 |
| webadmin/settings_store.py | 141 |
| tests/test_audit_v2.py | 138 |
| tests/test_classifier.py | 138 |
| clauses.py | 134 |
| unified_alerts/maintenance.py | 132 |
| scripts/migrate_sqlite_to_pg.py | 126 |
| scripts/test_alert_live.py | 123 |
| tests/test_config.py | 118 |
| metrics.py | 116 |
| unified_alerts/stats.py | 104 |
| tests/conftest.py | 104 |
| tests/test_webadmin.py | 104 |
| generate_session.py | 100 |
| unified_alerts/__init__.py | 76 |
| scripts/golden_alert_baseline.py | 72 |
| adaptive.py | 70 |
| scripts/probe_tg_user_links.py | 67 |
| scripts/live_production_path_test.py | 64 |
| webadmin/logs_reader.py | 61 |
| scripts/live_ladder_tests.py | 51 |
| webadmin/__init__.py | 33 |
| task_util.py | 30 |

## ملفات أخرى (SQL/MD/JSON/YAML/SH/HTML)

| الملف | الأسطر |
|---|---|
| templates/dashboard.html | 4272 |
| keywords.json | 2315 |
| migrations/br7_schema_v1.sql | 1424 |
| templates/admin.html | 1357 |
| render.yaml | 680 |
| README.md | 365 |
| worklog.md | 243 |
| start.sh | 139 |
| scripts/qa_v931.sh | 93 |
| scripts/qa_round.sh | 88 |
| requirements.lock | 78 |
| migrations/validate_br7_v1.sql | 69 |
| docs/EXECUTION_V11_8.md | 61 |
| requirements.txt | 59 |
| scripts/supabase_rls.sql | 56 |
| unified_alerts/README.md | 53 |
| migrations/README.md | 40 |
| migrations/0003_queue_inflight.sql | 40 |
| migrations/0002_unified_alerts.sql | 35 |
| requirements-dev.txt | 12 |
| .pytest_cache/README.md | 8 |
| pytest.ini | 6 |
| runtime.txt | 1 |

## ملاحظات منهجية

* المجلدات المستثناة من الجرد: .git, __pycache__, .venv, downloads, sessions (بيئات/بيانات تشغيل، ليست مصدراً).
* «الجرد ≠ التدقيق»: القسم 4 من المواصفة يفرض تحديد مسؤولية كل ملف — يُغطى ذلك في FUNCTION_AUDIT.md للملفات الحرجة وARCHITECTURE.md للبقية.
* sessions/ وaccounts.env غير متتبعة عمداً (أسرار) — انظر SECURITY_AUDIT.md.