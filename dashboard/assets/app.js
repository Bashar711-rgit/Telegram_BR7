/* ═══════════════════════════════════════════════════════════════════
   Telegram BR7 — لوحة التحكم الخفيفة (v15.1.0) — Vanilla JS
   Polling منضبط: تبويب الحالة فقط، كل 20 ثانية، ويتوقف كلياً عند
   إخفاء الصفحة — لا مهام خلفية ولا استعلامات حين لا أحد يراقب.
   ═══════════════════════════════════════════════════════════════════ */
"use strict";

/* ── أدوات عامة ── */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts < 1e12 ? ts * 1000 : ts);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleString("ar", { hour12: false });
}
function fmtAge(sec) {
  if (sec == null) return "—";
  if (sec < 60) return `قبل ${Math.round(sec)} ث`;
  if (sec < 3600) return `قبل ${Math.round(sec / 60)} د`;
  if (sec < 86400) return `قبل ${Math.round(sec / 3600)} س`;
  return `قبل ${Math.round(sec / 86400)} ي`;
}
function fmtNum(n) { return (n == null ? 0 : n).toLocaleString("en"); }

function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("err", !!isErr);
  t.classList.remove("hidden");
  clearTimeout(t._tm);
  t._tm = setTimeout(() => t.classList.add("hidden"), 3800);
}

let modalResolver = null;
function confirmModal(msg, okLabel) {
  $("modalBody").textContent = msg;
  $("modalOk").textContent = okLabel || "تأكيد";
  $("modal").classList.remove("hidden");
  return new Promise((res) => { modalResolver = res; });
}
$("modalOk").addEventListener("click", () => { $("modal").classList.add("hidden"); if (modalResolver) modalResolver(true); modalResolver = null; });
$("modalCancel").addEventListener("click", () => { $("modal").classList.add("hidden"); if (modalResolver) modalResolver(false); modalResolver = null; });

/* ── طبقة API (CSRF + 401) ── */
function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)br7dash=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function api(path, opts) {
  opts = opts || {};
  const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  if (init.method !== "GET") init.headers["X-CSRF-Token"] = csrfToken();
  let resp;
  try {
    resp = await fetch(path, init);
  } catch (e) {
    return { ok: false, status: 0, data: { error: "network_error" } };
  }
  let data = {};
  try { data = await resp.json(); } catch (e) { data = {}; }
  if (resp.status === 401 && !path.endsWith("/login")) {
    showLogin();
    return { ok: false, status: 401, data };
  }
  return { ok: resp.ok, status: resp.status, data };
}

/* ── المصادقة ── */
let authed = false;
let username = "";

function showLogin() {
  authed = false;
  $("loginOverlay").classList.remove("hidden");
}
function hideLogin() {
  authed = true;
  $("loginOverlay").classList.add("hidden");
}

$("loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("loginBtn").disabled = true;
  const errBox = $("loginError");
  errBox.classList.add("hidden");
  const r = await api("/admin/api/login", {
    method: "POST",
    body: { username: $("loginUser").value.trim(), password: $("loginPass").value },
  });
  $("loginBtn").disabled = false;
  if (r.ok && r.data.ok) {
    $("loginPass").value = "";
    hideLogin();
    username = r.data.username;
    $("versionBadge").textContent = "v" + (r.data.version || "");
    toast("تم تسجيل الدخول");
    loadOpsBanner();
    switchTab(currentTab);
  } else {
    errBox.textContent = r.data.note || r.data.error === "invalid_credentials"
      ? (r.data.note || "بيانات الدخول غير صحيحة")
      : "تعذر الاتصال — أعد المحاولة";
    errBox.classList.remove("hidden");
  }
});

$("logoutBtn").addEventListener("click", async () => {
  await api("/admin/api/logout", { method: "POST", body: {} });
  showLogin();
  toast("تم تسجيل الخروج");
});

/* ── التبويبات ── */
let currentTab = "overview";
const tabLoaders = {};

$("tabs").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".tab");
  if (!btn) return;
  switchTab(btn.dataset.tab);
});

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + tab));
  const loader = tabLoaders[tab];
  if (loader && authed) loader().catch((e) => toast("فشل تحميل البيانات", true));
}

/* ── بانر مفاتيح الإيقاف ── */
async function loadOpsBanner() {
  const r = await api("/admin/api/ops");
  if (!r.ok) return;
  const ops = (r.data.ops || {});
  const pausedF = !!ops.filtering_paused, pausedA = !!ops.alerts_paused;
  const banner = $("opsBanner");
  const parts = [];
  if (pausedF) parts.push("⛔ الفلترة موقوفة مؤقتاً");
  if (pausedA) parts.push("⛔ التنبيهات موقوفة مؤقتاً");
  if (parts.length) {
    banner.textContent = parts.join(" · ");
    banner.classList.remove("hidden");
    const ov = $("opsBannerOv");
    ov.textContent = "تنبيه: " + parts.join(" · ") + " — يمكنك تفعيلها من تبويب الإعدادات";
    ov.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
    $("opsBannerOv").classList.add("hidden");
  }
  $("swFilter").checked = pausedF;
  $("swAlerts").checked = pausedA;
}

/* ═════════ الوحدة 1: الحالة ═════════ */
let overviewLoadedOnce = false;

tabLoaders.overview = async function () {
  overviewLoadedOnce = true;
  const r = await api("/admin/api/overview");
  if (!r.ok) { toast("تعذر تحميل الحالة", true); return; }
  renderOverview(r.data);
};

function statCard(k, v, cls, sub) {
  return `<div class="stat ${cls || ""}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>${sub ? `<div class="s">${esc(sub)}</div>` : ""}</div>`;
}

function renderOverview(d) {
  $("versionBadge").textContent = "v" + (d.bot_version || "");
  const st = d.staleness || {};
  const sb = $("stalenessBanner");
  if (st.stale) {
    sb.textContent = "⚠️ " + (st.note || "توقف استقبال الرسائل رغم أن الحسابات تظهر متصلة") +
      ` — آخر نشاط: ${st.age_minutes != null ? fmtAge(st.age_minutes * 60) : "غير معروف"}`;
    sb.classList.remove("hidden");
  } else sb.classList.add("hidden");

  const acc = d.accounts || {};
  const msg = d.messages || {};
  const flt = d.filter || {};
  const alr = d.alerts || {};
  const sys = d.system || {};
  $("ovCards").innerHTML =
    statCard("حالة البوت", d.status === "ok" ? "يعمل" : (d.status === "starting" ? "يبدأ" : "متدهور"), d.status === "ok" ? "ok" : "warn", `مدة التشغيل: ${d.uptime_text || "—"}`) +
    statCard("الحسابات المتصلة", `${acc.connected || 0}/${acc.total || 0}`, (acc.connected || 0) > 0 ? "ok" : "bad", `المجموعات المراقبة: ${fmtNum((d.groups || {}).sources || 0)}${(d.groups || {}).allow_all ? " (الجميع)" : ""}`) +
    statCard("رسائل اليوم", fmtNum(msg.today), "", `آخر ساعة: ${fmtNum(msg.last_hour)}`) +
    statCard("اجتازت الفلترة", fmtNum(flt.accepted), flt.accepted ? "ok" : "", `مرفوضة: ${fmtNum(flt.rejected)} · مراجعة: ${fmtNum(flt.review)}`) +
    statCard("تنبيهات ناجحة", fmtNum(alr.sent_ok || 0), alr.sent_ok ? "ok" : "", `اليوم: ${fmtNum(alr.today)}`) +
    statCard("تنبيهات فاشلة", fmtNum(alr.failed || 0), (alr.failed || 0) > 0 ? "bad" : "", `آخر ساعة: ${fmtNum(alr.last_hour)}`) +
    statCard("معدل التحويل", `${alr.conversion_pct || 0}%`, "", "رسالة → تنبيه") +
    statCard("الذاكرة", `${sys.rss_mb || 0} MB`, sys.limit_mb && sys.rss_mb > sys.limit_mb * 0.85 ? "bad" : "ok", `الحد: ${sys.limit_mb || "—"} MB`) +
    statCard("المعالج", `${sys.cpu_percent || 0}%`, "", `DB: ${sys.db_healthy ? "سليم" : "متقطع"}${sys.db_latency_ms != null ? " · " + sys.db_latency_ms + "ms" : ""}`);

  // الرسم البياني 24 ساعة
  const chart = $("chart24");
  const data = d.chart_24h || [];
  const maxV = Math.max(1, ...data.map((x) => Math.max(x.msgs, x.alerts)));
  chart.innerHTML = data.map((x) => {
    const hm = new Date(x.h * 1000).getHours();
    const h1 = Math.round((x.msgs / maxV) * 100);
    const h2 = Math.round((x.alerts / maxV) * 100);
    return `<div class="bar"><div class="b-msgs" style="height:${h1}%" title="رسائل: ${x.msgs}"></div><div class="b-alerts" style="height:${h2}%" title="تنبيهات: ${x.alerts}"></div><div class="hour">${hm}</div></div>`;
  }).join("");

  // الأخطاء
  const errs = d.errors || {};
  const list = [];
  (errs.system_health || []).forEach((h) => list.push(`[${h.component}] ${h.details || h.status}`));
  if (errs.dead_letters_unresolved) list.push(`أحداث فاشلة غير محلولة: ${errs.dead_letters_unresolved}`);
  (errs.monitors_with_errors || []).forEach((n) => list.push(`حساب به أخطاء: ${n}`));
  $("ovErrors").innerHTML = list.length ? list.map((x) => `<li>${esc(x)}</li>`).join("") : "<li>لا أخطاء مسجلة ✅</li>";

  // الحسابات
  const rows = (acc.monitors || []).map((m) => {
    const pill = m.connected ? '<span class="pill ok">متصل</span>' : '<span class="pill bad">منفصل</span>';
    return `<tr><td>${esc(m.name)}</td><td>${pill}</td><td>${fmtNum(m.events_received)}</td><td>${m.last_event_age_s != null ? fmtAge(m.last_event_age_s) : "—"}</td></tr>`;
  }).join("");
  $("ovMonitors").innerHTML = "<thead><tr><th>الحساب</th><th>الحالة</th><th>الأحداث المستلمة</th><th>آخر نشاط</th></tr></thead><tbody>" + rows + "</tbody>";
}

/* Polling منضبط: 20 ثانية، الحالة فقط، يتوقف عند الإخفاء */
setInterval(() => {
  if (!authed) return;
  if (document.visibilityState !== "visible") return;
  if (currentTab !== "overview" || !overviewLoadedOnce) return;
  loadOpsBanner();
  tabLoaders.overview().catch(() => {});
}, 20000);

/* ═════════ الوحدة 2: الحسابات ═════════ */
tabLoaders.accounts = async function () {
  const r = await api("/admin/api/accounts");
  if (!r.ok) { toast("تعذر تحميل الحسابات", true); return; }
  renderAccounts(r.data);
};

function renderAccounts(d) {
  $("accNote").textContent = d.note || "";
  const rows = (d.items || []).map((a) => {
    const conn = a.connected ? '<span class="pill ok">متصل</span>' : '<span class="pill bad">منفصل</span>';
    const term = a.terminal_error ? ` <span class="pill bad" title="${esc(a.terminal_error)}">نهائي</span>` : "";
    const ident = a.identity ? `<small>${esc(a.identity.first_name || "")}${a.identity.username ? " · @" + esc(a.identity.username) : ""} · ${esc(a.identity.id || "")}</small>` : '<small>لم يُختبر</small>';
    const mem = a.membership ? (a.membership.status === "ok" ? '<span class="pill ok">عضو</span>' : `<span class="pill bad">${esc(a.membership.status)}</span>`) : '<span class="pill muted">لم يُفحص</span>';
    const err = a.last_error ? `<span title="${esc(a.last_error)}" style="color:#ffb4b4">${esc(String(a.last_error).slice(0, 40))}…</span>` : "—";
    return `<tr>
      <td><b>${esc(a.name)}</b>${a.is_main ? ' <span class="pill muted">رئيسي</span>' : ""}<br>${ident}</td>
      <td>${esc(a.phone_masked)}</td><td>${conn}${term}</td><td>${mem}</td>
      <td>${fmtNum(a.events_received)}</td><td>${fmtNum(a.alerts_sent)}</td>
      <td>${fmtNum(a.errors + a.send_errors)}</td><td>${err}</td>
      <td>
        <button class="btn sm" data-act="test" data-name="${esc(a.name)}">اختبار</button>
        <button class="btn sm" data-act="membership" data-name="${esc(a.name)}">عضوية</button>
        <button class="btn sm" data-act="reconnect" data-name="${esc(a.name)}">إعادة ربط</button>
      </td></tr>`;
  }).join("");
  $("accTable").innerHTML = "<thead><tr><th>الحساب</th><th>الهاتف</th><th>الاتصال</th><th>العضوية بالهدف</th><th>الأحداث</th><th>تنبيهات</th><th>أخطاء</th><th>آخر خطأ</th><th>إجراءات</th></tr></thead><tbody>" + (rows || "<tr><td colspan='9'>لا حسابات محملة</td></tr>") + "</tbody>";
}

$("accTable").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const name = btn.dataset.name, act = btn.dataset.act;
  btn.disabled = true;
  try {
    if (act === "test") {
      const r = await api("/admin/api/accounts/test", { method: "POST", body: { name } });
      if (r.ok && r.data.ok) toast(`✅ ${name}: متصل باسم ${r.data.identity.first_name || r.data.identity.username || r.data.identity.id}`);
      else toast(`❌ ${name}: ${r.data.reason || "فشل"}`, true);
    } else if (act === "membership") {
      const r = await api("/admin/api/accounts/membership", { method: "POST", body: { name } });
      if (r.ok) toast(r.data.ok ? `✅ ${name}: عضو في مجموعة الهدف` : `⚠️ ${name}: ${r.data.status}`, !r.data.ok);
      else toast(r.data.error || "فشل", true);
    } else if (act === "reconnect") {
      const go = await confirmModal(`إعادة اتصال الحساب ${name}؟ (دون إعادة نشر)`, "إعادة الربط");
      if (!go) return;
      const r = await api("/admin/api/accounts/reconnect", { method: "POST", body: { name } });
      toast(r.ok && r.data.ok ? `✅ ${name}: أُعيد الربط` : `❌ ${name}: ${r.data.reason || "فشل"}`, !(r.ok && r.data.ok));
    }
    tabLoaders.accounts().catch(() => {});
  } finally { btn.disabled = false; }
});

$("accRefresh").addEventListener("click", () => tabLoaders.accounts().catch(() => {}));
$("accTestAll").addEventListener("click", async () => {
  const r = await api("/admin/api/accounts");
  if (!r.ok) return;
  const names = (r.data.items || []).map((a) => a.name);
  toast(`اختبار ${names.length} حساب (تزامن محدود)...`);
  for (const n of names) {
    await api("/admin/api/accounts/test", { method: "POST", body: { name: n } });
  }
  toast("انتهى اختبار الجميع");
  tabLoaders.accounts().catch(() => {});
});

/* ═════════ الوحدة 3: الكلمات ═════════ */
let kwState = { units: {}, runtimeWeights: {} };

tabLoaders.keywords = async function () {
  await Promise.all([loadKeywords(), loadKeywordStats()]);
};

const KW_UNIT_LABELS = {
  intent_verbs: "أفعال النية", implicit_request_patterns: "أنماط الطلب الضمني",
  action_verbs: "أفعال الإجراء", solve_actions: "أفعال الحل",
  academic_objects: "المواد والأهداف الأكاديمية", request_phrases: "عبارات الطلب",
  help_expressions: "عبارات الاستغاثة", urgency_markers: "مؤشرات الاستعجال",
  subject_markers: "مؤشرات الموضوع", ignore_signals: "كلمات مستبعدة",
  advertisement_signals: "إشارات الإعلان", ad_blockers: "حواجز الإعلان",
};

async function loadKeywords() {
  const r = await api("/admin/api/keywords");
  if (!r.ok) { toast("تعذر تحميل الكلمات", true); return; }
  kwState = { units: r.data.units || {}, runtimeWeights: r.data.runtime_weights || {} };
  $("kwCustomBadge").classList.toggle("hidden", !r.data.custom_active);
  $("pgEngine").textContent = r.data.note || "";
  // تجميع الوحدات حسب الفئة العليا
  const byTop = {};
  Object.keys(kwState.units).forEach((path) => {
    const top = path.split(".")[0];
    (byTop[top] = byTop[top] || []).push(path);
  });
  $("kwCats").innerHTML = Object.keys(byTop).map((top) => {
    const unitsHtml = byTop[top].map((path) => {
      const entries = kwState.units[path] || [];
      return `<details class="kwcat" style="margin:6px 0">
        <summary style="font-size:13px"><code style="font-size:11px">${esc(path)}</code> <span class="cnt">${entries.length} عنصر</span></summary>
        <div class="body"><textarea data-unit="${esc(path)}" style="min-height:70px">${esc(entries.join("\n"))}</textarea></div>
      </details>`;
    }).join("");
    return `<details class="kwcat" open>
      <summary>${esc(KW_UNIT_LABELS[top] || top)} <span class="cnt">${byTop[top].length} قائمة</span></summary>
      <div class="body">${unitsHtml}</div>
    </details>`;
  }).join("");
  const w = kwState.runtimeWeights || {};
  const wNames = { intent: "النية", academic: "الأكاديمي", grammar: "النحوي", distance: "المسافة", urgency: "الاستعجال", context: "السياق" };
  $("kwWeights").innerHTML = Object.keys(w).length
    ? Object.keys(w).map((k) => `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px dashed var(--line)"><span>${esc(wNames[k] || k)}</span><b>${esc(w[k])}</b></div>`).join("")
    + "<small style='color:var(--muted);display:block;margin-top:8px'>تُدار من env (SCORE_WEIGHT_*) — للمرجعية فقط</small>"
    : "<small>لا أوزان معروضة</small>";
}

async function loadKeywordStats() {
  const r = await api("/admin/api/keywords/stats");
  if (!r.ok) return;
  const d = r.data;
  const renderList = (arr, k1, k2) => arr.length
    ? "<ul class='plainlist'>" + arr.map((x) => `<li>${esc(x[k1])} — <b>${x.count}</b></li>`).join("") + "</ul>"
    : "<small>لا بيانات بعد</small>";
  $("kwStats").innerHTML =
    "<b style='font-size:13px'>أكثر الكلمات إنتاجاً للتنبيهات:</b>" + renderList(d.top_keywords, "keyword") +
    "<b style='font-size:13px'>أسباب الرفض الأكثر:</b>" + renderList(d.top_rejections, "reason") +
    "<b style='font-size:13px'>تطابقات زائفة محتملة (كلمات مع رسائل مرفوضة):</b>" + renderList(d.false_positives, "matched");
}

$("pgRun").addEventListener("click", async () => {
  const text = $("pgText").value.trim();
  if (!text) { toast("اكتب رسالة أولاً", true); return; }
  $("pgRun").disabled = true;
  try {
    const r = await api("/admin/api/playground", { method: "POST", body: { text } });
    if (!r.ok) { toast(r.data.error || "فشل التحليل", true); return; }
    const a = r.data.analysis || {};
    const th = r.data.thresholds || {};
    $("pgEngine").textContent = `المحرك: v${r.data.engine_version || "?"} — نفس محرك الإنتاج`;
    const decisionPill = a.decision === "accept" ? '<span class="pill ok">قبول</span>'
      : a.decision === "review" ? '<span class="pill warn">مراجعة</span>' : '<span class="pill bad">رفض</span>';
    const items = [
      ["القرار", decisionPill],
      ["الثقة النهائية", `<b>${(a.confidence || 0).toFixed(2)}</b> <small>(قبول ≥ ${th.accept}, مراجعة ≥ ${th.review})</small>`],
      ["الكلمة المطابقة", esc(a.keyword || "—")],
      ["Intent (النية)", esc(a.intent_verb || "—")],
      ["Context (السياق)", esc((a.context_type || "—") + ` ${(a.context_confidence || 0).toFixed(2)}`)],
      ["Negative (الاستبعاد)", a.negation_detected ? "نفي مكتشف" : `إعلان: ${(a.advert_score || 0).toFixed(2)}`],
      ["Score", fmtNum(a.score)],
      ["Spam", (a.spam_score || 0).toFixed(2)],
      ["الوقت", `${(a.analysis_time_ms || 0).toFixed(1)}ms`],
    ];
    $("pgResults").innerHTML =
      '<div class="pg-results">' + items.map(([k, v]) => `<div class="pg-item"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("") + "</div>" +
      (a.reasons && a.reasons.length ? `<ul class="reasons">${a.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "");
  } finally { $("pgRun").disabled = false; }
});

$("kwSave").addEventListener("click", async () => {
  const go = await confirmModal("حفظ كل تعديلات الكلمات وتطبيقها على المحرك فوراً؟", "حفظ وتطبيق");
  if (!go) return;
  const units = {};
  document.querySelectorAll("#kwCats textarea[data-unit]").forEach((ta) => {
    units[ta.dataset.unit] = ta.value.split("\n").map((x) => x.trim()).filter(Boolean);
  });
  const r = await api("/admin/api/keywords/save", { method: "POST", body: { units } });
  if (r.ok && r.data.ok) { toast(`✅ حُفظ وطُبّق (${r.data.changed} عنصر)`); loadKeywords().catch(() => {}); }
  else toast("فشل الحفظ: " + (r.data.error || "?"), true);
});

$("kwExport").addEventListener("click", async () => {
  const r = await api("/admin/api/keywords/export");
  if (!r.ok) { toast("فشل التصدير", true); return; }
  downloadJSON(r.data, "br7-keywords.json");
  toast("تم التصدير");
});

$("kwImportBtn").addEventListener("click", () => $("kwImportFile").click());
$("kwImportFile").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const go = await confirmModal("استيراد ملف JSON واستبدال الفئات المعدلة؟", "استيراد");
    if (!go) return;
    const r = await api("/admin/api/keywords/import", { method: "POST", body: { json_text: text } });
    if (r.ok && r.data.ok) { toast(`✅ استُورد وطُبّق (${r.data.applied})`); loadKeywords().catch(() => {}); }
    else toast("فشل الاستيراد: " + (r.data.error || "?"), true);
  } catch (e) { toast("ملف غير صالح", true); }
  ev.target.value = "";
});

function downloadJSON(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/* ═════════ الوحدة 4: المجموعات ═════════ */
tabLoaders.groups = async function () {
  const r = await api("/admin/api/groups");
  if (!r.ok) { toast("تعذر تحميل المجموعات", true); return; }
  renderGroups(r.data);
};

function renderGroups(d) {
  $("grpAllowAll").classList.toggle("hidden", !d.allow_all);
  const rows = (d.items || []).map((g) => {
    const st = g.enabled ? '<span class="pill ok">مراقبة</span>' : '<span class="pill warn">موقوفة</span>';
    const stale = g.stale ? ' <span class="pill bad" title="توقف استقبال الرسائل من هذه المجموعة">متوقفة؟</span>' : "";
    return `<tr>
      <td><b>${esc(g.title)}</b>${g.username ? `<br><small>@${esc(g.username)}</small>` : ""}</td>
      <td><code>${esc(g.chat_id)}</code></td><td>${st}${stale}</td>
      <td>${fmtNum(g.msgs_total)}</td><td>${fmtNum(g.alerts_24h)}</td>
      <td>${g.last_msg_age_minutes != null ? fmtAge(g.last_msg_age_minutes * 60) : "—"}</td>
      <td>
        <button class="btn sm" data-act="toggle" data-id="${g.chat_id}" data-en="${g.enabled ? 1 : 0}">${g.enabled ? "إيقاف" : "تفعيل"}</button>
        <button class="btn sm danger" data-act="remove" data-id="${g.chat_id}">إزالة</button>
      </td></tr>`;
  }).join("");
  $("grpTable").innerHTML = "<thead><tr><th>المجموعة</th><th>المعرف</th><th>الحالة</th><th>الرسائل (7 أيام)</th><th>تنبيهات 24س</th><th>آخر رسالة</th><th>إجراءات</th></tr></thead><tbody>" + (rows || "<tr><td colspan='7'>لا مجموعات محددة</td></tr>") + "</tbody>";
}

$("grpAddBtn").addEventListener("click", async () => {
  const input = $("grpAddInput").value.trim();
  if (!input) { toast("أدخل رابطاً أو معرفاً", true); return; }
  const r = await api("/admin/api/groups/add", { method: "POST", body: { input } });
  if (r.ok && r.data.ok) { toast(`✅ أُضيفت: ${r.data.title || r.data.chat_id}`); $("grpAddInput").value = ""; tabLoaders.groups().catch(() => {}); }
  else toast(r.data.note || r.data.error || "فشل", true);
});
$("grpRefresh").addEventListener("click", () => tabLoaders.groups().catch(() => {}));

$("grpTable").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.dataset.id, act = btn.dataset.act;
  if (act === "toggle") {
    const enable = btn.dataset.en !== "1";
    const r = await api("/admin/api/groups/toggle", { method: "POST", body: { chat_id: Number(id), enabled: enable } });
    if (r.ok) toast(enable ? "✅ فُعّلت المراقبة" : "⏸️ أُوقفت المراقبة");
    tabLoaders.groups().catch(() => {});
  } else if (act === "remove") {
    const go = await confirmModal(`إزالة المجموعة ${id} من المراقبة؟ (لن تُحذف رسائلها)`, "إزالة");
    if (!go) return;
    const r = await api("/admin/api/groups/remove", { method: "POST", body: { chat_id: Number(id) } });
    if (r.ok) toast("أُزيلت");
    tabLoaders.groups().catch(() => {});
  }
});

/* ═════════ الوحدة 5: التنبيهات ═════════ */
let alPage = 1;

tabLoaders.alerts = async function () {
  await Promise.all([loadAlerts(), loadAlertsStats()]);
};

async function loadAlerts() {
  const status = $("alStatusFilter").value;
  const r = await api(`/admin/api/alerts?page=${alPage}&status=${status}`);
  if (!r.ok) { toast("تعذر تحميل السجل", true); return; }
  const d = r.data;
  const rows = (d.items || []).map((a) => {
    const pill = a.status === "sent" ? '<span class="pill ok">أُرسلت</span>'
      : a.status === "failed" ? '<span class="pill bad">فشلت</span>' : '<span class="pill muted">غير معروفة</span>';
    const btns = [];
    if (a.has_msg_button) btns.push('<span class="pill muted">زر رسالة</span>');
    if (a.has_user_button) btns.push('<span class="pill muted">زر مرسل</span>');
    const resend = a.status !== "sent" ? ` <button class="btn sm" data-resend="${esc(a.id)}" data-hash="${esc((a.text || "").slice(0, 0))}" data-id2="${esc(a.id)}">إعادة</button>` : "";
    return `<tr>
      <td>${fmtTs(a.ts)}</td><td>${esc(a.keyword || "—")}</td>
      <td title="${esc(a.text)}">${esc(String(a.text || "").slice(0, 60))}…</td>
      <td>${pill}</td><td>${btns.join(" ") || "—"}</td>
      <td>${a.error ? `<span title="${esc(a.error)}" style="color:#ffb4b4">${esc(String(a.error).slice(0, 30))}…</span>` : "—"}</td>
      <td>${resend}</td></tr>`;
  }).join("");
  $("alTable").innerHTML = "<thead><tr><th>الوقت</th><th>الكلمة</th><th>النص</th><th>الحالة</th><th>الأزرار</th><th>سبب الفشل</th><th></th></tr></thead><tbody>" + (rows || "<tr><td colspan='7'>لا تنبيهات</td></tr>") + "</tbody>";
  $("alPageInfo").textContent = `صفحة ${d.page} · الإجمالي ${fmtNum(d.total)}`;
  $("alPrev").disabled = d.page <= 1;
  $("alNext").disabled = !d.has_more;
}

async function loadAlertsStats() {
  const r = await api("/admin/api/alerts/stats");
  if (!r.ok) return;
  const d = r.data;
  const bs = d.by_status || {};
  const bt = d.buttons || {};
  $("alStatsChips").innerHTML =
    `<span class="chip">الهدف: <b>${esc(d.target_group || "غير مضبوط")}</b></span>` +
    `<span class="chip">الإجمالي: <b>${fmtNum(d.total)}</b></span>` +
    `<span class="chip">اليوم: <b>${fmtNum(d.today)}</b></span>` +
    `<span class="chip">ناجحة: <b>${fmtNum(bs.sent || 0)}</b></span>` +
    `<span class="chip">فاشلة: <b>${fmtNum(bs.failed || 0)}</b></span>` +
    `<span class="chip">بزر رسالة: <b>${fmtNum(bt.with_message_button || 0)}</b></span>` +
    `<span class="chip">بزر مرسل: <b>${fmtNum(bt.with_user_button || 0)}</b></span>`;
}

$("alPrev").addEventListener("click", () => { if (alPage > 1) { alPage--; loadAlerts().catch(() => {}); } });
$("alNext").addEventListener("click", () => { alPage++; loadAlerts().catch(() => {}); });
$("alStatusFilter").addEventListener("change", () => { alPage = 1; loadAlerts().catch(() => {}); });
$("alRefresh").addEventListener("click", () => tabLoaders.alerts().catch(() => {}));

$("alTestBtn").addEventListener("click", async () => {
  const go = await confirmModal("إرسال تنبيه اختباري إلى مجموعة الهدف الآن؟", "إرسال");
  if (!go) return;
  const r = await api("/admin/api/alerts/test", { method: "POST", body: {} });
  toast(r.ok && r.data.ok ? "✅ أُرسل الاختبار — تحقق من القناة" : `❌ ${r.data.reason || r.data.error || "فشل"}`, !(r.ok && r.data.ok));
  loadAlertsStats().catch(() => {});
});

$("alTemplateBtn").addEventListener("click", async () => {
  const r = await api("/admin/api/alerts/template");
  if (!r.ok) { toast("فشل بناء المعاينة", true); return; }
  const btns = (r.data.buttons || []).flat().map((b) => `<span class="tgbtn">${esc(b.text)}</span>`).join(" ");
  $("modalBody").innerHTML =
    `<b>معاينة القالب الموحد (بنفس دوال الإنتاج):</b><br><br>` +
    `<div class="alert-preview">${r.data.text || ""}</div>` +
    `<div class="btnrow">${btns || "<small>— لا أزرار في هذا المثال —</small>"}</div>`;
  $("modalOk").classList.add("hidden");
  $("modalCancel").textContent = "إغلاق";
  $("modal").classList.remove("hidden");
  modalResolver = () => {
    $("modalOk").classList.remove("hidden");
    $("modalCancel").textContent = "إلغاء";
  };
});

$("alTable").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-id2]");
  if (!btn) return;
  const id = btn.dataset.id2;
  const go = await confirmModal("إعادة إرسال هذا التنبيه الفاشل إلى مجموعة الهدف؟", "إعادة إرسال");
  if (!go) return;
  const r = await api("/admin/api/alerts/resend", { method: "POST", body: { hash: id } });
  toast(r.ok && r.data.ok ? "✅ أُعيد الإرسال" : `❌ ${r.data.reason || r.data.error || "فشل"}`, !(r.ok && r.data.ok));
  loadAlerts().catch(() => {});
});

/* ═════════ الوحدة 6: الإعدادات ═════════ */
tabLoaders.ops = async function () {
  await Promise.all([loadOpsBanner(), loadEnv(), loadOpsErrors(), loadAudit(1)]);
  loadMemInfo().catch(() => {});
};

$("swFilter").addEventListener("change", async (ev) => {
  const enable = ev.target.checked;
  const go = await confirmModal(enable
    ? "إيقاف الفلترة مؤقتاً؟ الرسائل تُلتقط لكن لا تُحلل ولا تُنبه حتى إعادة التفعيل."
    : "إعادة تفعيل الفلترة؟", enable ? "إيقاف مؤقت" : "تفعيل");
  if (!go) { ev.target.checked = !enable; return; }
  const r = await api("/admin/api/ops/toggle", { method: "POST", body: { flag: "filtering", enabled: enable } });
  if (r.ok) toast(enable ? "⛔ أُوقفت الفلترة" : "✅ عادت الفلترة");
  else { toast("فشل", true); ev.target.checked = !enable; }
  loadOpsBanner().catch(() => {});
});

$("swAlerts").addEventListener("change", async (ev) => {
  const enable = ev.target.checked;
  const go = await confirmModal(enable
    ? "إيقاف إرسال التنبيهات مؤقتاً؟ الفلترة والتقاط مستمران — الإرسال فقط يتوقف."
    : "إعادة تفعيل إرسال التنبيهات؟", enable ? "إيقاف مؤقت" : "تفعيل");
  if (!go) { ev.target.checked = !enable; return; }
  const r = await api("/admin/api/ops/toggle", { method: "POST", body: { flag: "alerts", enabled: enable } });
  if (r.ok) toast(enable ? "⛔ أُوقفت التنبيهات" : "✅ عادت التنبيهات");
  else { toast("فشل", true); ev.target.checked = !enable; }
  loadOpsBanner().catch(() => {});
});

async function loadEnv() {
  const r = await api("/admin/api/ops/env");
  if (!r.ok) return;
  $("envGroups").innerHTML = (r.data.groups || []).map((g) => {
    const items = (g.items || []).filter((x) => x.status === "set" || !x.secret).slice(0, 8);
    const rows = items.map((x) =>
      `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dashed var(--line)">
        <span style="font-family:monospace;font-size:12px">${esc(x.key)}</span>
        <span>${x.status === "set" ? (x.value != null ? esc(x.value) : '<span class="pill ok">set 🔒</span>') : '<span class="pill muted">unset</span>'}</span>
      </div>`).join("");
    const setCount = (g.items || []).filter((x) => x.status === "set").length;
    return `<details class="kwcat"><summary>${esc(g.name)} <span class="cnt">${setCount} مضبوط</span></summary><div class="body">${rows}</div></details>`;
  }).join("");
}

async function loadOpsErrors() {
  const r = await api("/admin/api/ops/errors");
  if (!r.ok) return;
  const d = r.data;
  const sec = (t, items) => `<b style="font-size:13px">${t}</b>` +
    (items.length ? "<ul class='plainlist'>" + items.map((x) => `<li>${esc(x)}</li>`).join("") + "</ul>" : "<small>لا شيء ✅</small>");
  $("opsErrors").innerHTML =
    sec("حالة النظام", (d.system_health || []).map((h) => `[${h.component}] ${h.details || h.status} — ${fmtTs(h.ts)}`)) +
    sec("أحداث فاشلة (dead letters)", (d.dead_letters || []).map((x) => `#${x.id} ${x.error} (محاولات: ${x.retries})`)) +
    sec("إشعارات إدارية", (d.notifications || []).map((n) => `${n.title} — ${n.body}`)) +
    sec("أخطاء الحسابات", (d.monitors || []).map((m) => `${m.name}: ${m.error}`));
}

let audPage = 1;
async function loadAudit(page) {
  audPage = page || 1;
  const r = await api(`/admin/api/ops/audit?page=${audPage}`);
  if (!r.ok) return;
  const d = r.data;
  const rows = (d.items || []).map((a) =>
    `<tr><td>${esc(a.ts)}</td><td>${esc(a.actor)}</td><td><code>${esc(a.action)}</code></td><td>${esc(a.object_id)}</td><td>${esc(a.new_value)}</td></tr>`
  ).join("");
  $("auditTable").innerHTML = "<thead><tr><th>الوقت</th><th>المستخدم</th><th>الإجراء</th><th>الهدف</th><th>التفاصيل</th></tr></thead><tbody>" + (rows || "<tr><td colspan='5'>لا عمليات بعد</td></tr>") + "</tbody>";
  $("audPageInfo").textContent = `صفحة ${d.page}`;
  $("audPrev").disabled = d.page <= 1;
  $("audNext").disabled = !d.has_more;
}
$("audPrev").addEventListener("click", () => loadAudit(audPage - 1).catch(() => {}));
$("audNext").addEventListener("click", () => loadAudit(audPage + 1).catch(() => {}));

async function loadMemInfo() {
  const r = await api("/admin/api/ops");
  if (!r.ok) return;
  const mg = r.data.mem_guard || {};
  $("memInfo").textContent = `الذاكرة: ${mg.last_rss_mb || 0}/${mg.limit_mb || "?"} MB · إجراء أخير: ${mg.last_action || "لا شيء"} · soft/hard resets: ${mg.soft_resets || 0}/${mg.hard_resets || 0}`;
}

$("backupBtn").addEventListener("click", async () => {
  const go = await confirmModal("تنزيل نسخة احتياطية للجداول الحرجة؟", "تنزيل");
  if (!go) return;
  const r = await api("/admin/api/ops/backup");
  if (!r.ok) { toast("فشل النسخ", true); return; }
  downloadJSON(r.data, `br7-backup-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`);
  toast("✅ نُزّلت النسخة الاحتياطية");
});

$("restartBtn").addEventListener("click", async () => {
  const go1 = await confirmModal("إعادة تشغيل آمنة للبوت؟ سيُغلق بشكل رشيق (تدفق/حفظ كامل) ثم يعيد Render الإقلاع تلقائياً خلال دقيقة تقريباً.", "متابعة");
  if (!go1) return;
  const go2 = await confirmModal("تأكيد أخير: هل أنت متأكد؟ ستتوقف المراقبة لحظياً.", "نعم، أعد التشغيل");
  if (!go2) return;
  const r = await api("/admin/api/ops/restart", { method: "POST", body: {} });
  if (r.ok && r.data.ok) toast("🔄 بدأ الإيقاف الرشيق — ستعود اللوحة خلال دقيقة");
  else toast(r.data.error || "فشل", true);
});

/* ── الإقلاع ── */
(async function boot() {
  const r = await api("/admin/api/session");
  const d = r.data || {};
  $("versionBadge").textContent = "v" + (d.version || "");
  if (d.authenticated) {
    hideLogin();
    username = d.username || "";
    loadOpsBanner().catch(() => {});
    switchTab("overview");
  } else {
    showLogin();
  }
})();
