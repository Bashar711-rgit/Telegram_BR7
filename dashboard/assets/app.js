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
/* نافذة مع إدخال نصي (v15.7.0) — تعيد القيمة أو null عند الإلغاء */
function promptModal(msg, placeholder, defVal, okLabel) {
  $("modalBody").innerHTML = esc(msg) +
    '<br><input id="modalInput" type="text" style="width:100%;margin-top:12px" placeholder="' +
    esc(placeholder || "") + '" value="' + esc(defVal || "") + '">';
  $("modalOk").textContent = okLabel || "حفظ";
  $("modal").classList.remove("hidden");
  return new Promise((res) => { modalResolver = res; });
}
$("modalOk").addEventListener("click", () => {
  const inp = $("modalInput");
  const val = inp ? inp.value.trim() : true;
  $("modal").classList.add("hidden");
  if (modalResolver) modalResolver(val);
  modalResolver = null;
});
$("modalCancel").addEventListener("click", () => {
  $("modal").classList.add("hidden");
  if (modalResolver) modalResolver(null);
  modalResolver = null;
});

/* ── طبقة API (CSRF + 401) ── */
function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)br7dash_csrf=([^;]*)/);
  if (m) return decodeURIComponent(m[1]);
  return sessionStorage.getItem("br7dash_csrf") || "";
}

/* بوابة بمنفذ مُحوّل (XTransformPort): تُلحق المعلمة بكل استدعاءات API
   تلقائياً — في الإنتاج (Render) لا توجد المعلمة فتبقى المسارات كما هي. */
const GW_PORT = new URLSearchParams(window.location.search).get("XTransformPort") || "";
const GW_Q = GW_PORT ? "XTransformPort=" + encodeURIComponent(GW_PORT) : "";
function gwPath(p) { return GW_Q ? p + (p.includes("?") ? "&" : "?") + GW_Q : p; }

async function api(path, opts) {
  opts = opts || {};
  path = gwPath(path);
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
/* P1-UI: سياق المستخدم الحالي والصلاحيات (تُملأ من /login و /session و /auth/me) */
let myPermissions = [];
let myRole = "";
let myUserId = null;
let mustChangePw = false;
let meCache = null;
let sessScope = "self";
let usrRoles = [];
const ROLE_AR = { owner: "المالك", admin: "مدير", operator: "مشغل", viewer: "مشاهد" };

function showLogin() {
  authed = false;
  resetAuthState();
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
  const okBox = $("loginSuccess");
  errBox.classList.add("hidden");
  okBox.classList.add("hidden");
  const totp = $("loginTotp").value.trim();
  const body = { username: $("loginUser").value.trim(), password: $("loginPass").value };
  if (totp) body.totp_code = totp; /* P1-UI: رمز المصادقة الثنائية الاختياري */
  const r = await api("/admin/api/login", { method: "POST", body });
  $("loginBtn").disabled = false;
  const d = r.data || {};
  if (r.ok && d.ok) {
    $("loginPass").value = "";
    $("loginTotp").value = "";
    if (d.csrf) sessionStorage.setItem("br7dash_csrf", d.csrf);
    setAuthContext(d);
    hideLogin();
    username = d.username;
    $("versionBadge").textContent = "v" + (d.version || "");
    toast("تم تسجيل الدخول");
    loadOpsBanner();
    switchTab(currentTab);
    if (d.must_change_password) openMustPwModal(); /* بوابة تغيير كلمة المرور الإجبارية */
  } else if (d.error === "totp_required") {
    errBox.textContent = d.note || "هذا الحساب محمي بالتحقق بخطوتين — أدخل رمز تطبيق المصادقة ثم أعد الدخول";
    errBox.classList.remove("hidden");
    $("loginTotp").focus();
  } else if (d.error === "locked" || d.error === "rate_limited") {
    errBox.textContent = d.note || "تم تجاوز عدد المحاولات — حاول بعد قليل";
    errBox.classList.remove("hidden");
  } else {
    errBox.textContent = d.note || (d.error === "invalid_credentials" ? "بيانات الدخول غير صحيحة" : "تعذر الاتصال — أعد المحاولة");
    errBox.classList.remove("hidden");
  }
});

$("logoutBtn").addEventListener("click", async () => {
  await api("/admin/api/logout", { method: "POST", body: {} });
  sessionStorage.removeItem("br7dash_csrf");
  resetAuthState();
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

const ACC_STATUS_AR = {
  active: '<span class="pill ok">نشط</span>',
  ready: '<span class="pill muted">جاهز</span>',
  stopped: '<span class="pill muted">متوقف</span>',
  needs_relogin: '<span class="pill warn">يحتاج إعادة دخول</span>',
  error: '<span class="pill bad">خطأ</span>',
};

function renderAccounts(d) {
  $("accNote").textContent = d.note || "";
  const rows = (d.items || []).map((a) => {
    const isPanel = a.kind === "panel";
    const conn = isPanel
      ? (ACC_STATUS_AR[a.status] || '<span class="pill muted">—</span>')
      : (a.terminal_error
          ? '<span class="pill bad">متوقف نهائياً</span>'
          : (a.connected ? '<span class="pill ok">متصل</span>' : '<span class="pill bad">منفصل</span>'));
    const term = a.terminal_error ? ` <span class="pill bad" title="${esc(a.terminal_error)}">نهائي</span>` : "";
    const ident = a.identity ? `<small>${esc(a.identity.first_name || "")}${a.identity.username ? " · @" + esc(a.identity.username) : ""} · ${esc(a.identity.id || "")}</small>` : '<small>لم يُختبر</small>';
    const mem = a.membership ? (a.membership.status === "ok" ? '<span class="pill ok">عضو</span>' : `<span class="pill bad">${esc(a.membership.status)}</span>`) : '<span class="pill muted">لم يُفحص</span>';
    const err = a.last_error ? `<span title="${esc(a.last_error)}" style="color:#ffb4b4">${esc(String(a.last_error).slice(0, 40))}…</span>` : "—";
    let actions;
    if (isPanel) {
      const tog = a.monitoring_enabled
        ? `<button class="btn sm" data-act="rowToggle" data-row="${a.row_id}" data-enable="false">إيقاف المراقبة</button>`
        : `<button class="btn sm primary" data-act="rowToggle" data-row="${a.row_id}" data-enable="true">تفعيل المراقبة</button>`;
      actions = `${tog}
        <button class="btn sm" data-act="rowReconnect" data-row="${a.row_id}">إعادة ربط</button>
        <button class="btn sm" data-act="rowRename" data-row="${a.row_id}" data-name="${esc(a.name)}">تسمية</button>
        <button class="btn sm danger" data-act="rowDelete" data-row="${a.row_id}">حذف</button>`;
    } else {
      actions = `<button class="btn sm" data-act="test" data-name="${esc(a.name)}">اختبار</button>
        <button class="btn sm" data-act="membership" data-name="${esc(a.name)}">عضوية</button>
        <button class="btn sm" data-act="reconnect" data-name="${esc(a.name)}">إعادة ربط</button>`;
      if (a.prefix) {
        actions += ` <button class="btn sm danger" data-act="envDelete" data-prefix="${esc(a.prefix)}" data-name="${esc(a.name)}">حذف نهائي</button>`;
      }
    }
    const kindBadge = isPanel ? ' <span class="pill muted">لوحة</span>' : ' <span class="pill muted">env</span>';
    return `<tr>
      <td><b>${esc(a.name)}</b>${a.is_main ? ' <span class="pill muted">رئيسي</span>' : ""}${kindBadge}<br>${ident}</td>
      <td>${esc(a.phone_masked)}</td><td>${conn}${term}</td><td>${mem}</td>
      <td>${fmtNum(a.events_received)}</td><td>${fmtNum(a.alerts_sent)}</td>
      <td>${fmtNum(a.errors + a.send_errors)}</td><td>${err}</td>
      <td>${actions}</td></tr>`;
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
    } else if (act === "rowToggle") {
      const enable = btn.dataset.enable === "true";
      const r = await api("/admin/api/accounts/row/toggle", { method: "POST", body: { row_id: Number(btn.dataset.row), enable } });
      if (r.ok && r.data.ok) toast(enable ? "✅ تُفعّل المراقبة — الحساب يتصل الآن" : "✅ أُوقفت المراقبة — الحساب محفوظ معطلاً");
      else toast("❌ " + ((r.data || {}).error || (r.data || {}).note || "فشل التغيير"), true);
    } else if (act === "rowReconnect") {
      const go = await confirmModal("إعادة اتصال حساب اللوحة؟ (دون إعادة نشر)", "إعادة الربط");
      if (!go) return;
      const r = await api("/admin/api/accounts/row/reconnect", { method: "POST", body: { row_id: Number(btn.dataset.row) } });
      toast(r.ok && r.data.ok && r.data.connected ? "✅ تم الاتصال" : "❌ فشل الاتصال — راجع آخر خطأ في الجدول", !(r.ok && r.data.ok && r.data.connected));
    } else if (act === "rowRename") {
      const newName = await promptModal("اسم العرض الجديد لهذا الحساب:", "الاسم", btn.dataset.name || "", "حفظ");
      if (!newName) return;
      const r = await api("/admin/api/accounts/row/rename", { method: "POST", body: { row_id: Number(btn.dataset.row), display_name: newName } });
      toast(r.ok && r.data.ok ? "✅ حُدّث الاسم" : "❌ " + ((r.data || {}).error || "فشل"), !(r.ok && r.data.ok));
    } else if (act === "rowDelete") {
      const go = await confirmModal("حذف الحساب وجلسته المشفّرة نهائياً؟ لا يمكن التراجع. (آخر حساب نشط محمي من الحذف)", "حذف نهائي");
      if (!go) return;
      const r = await api("/admin/api/accounts/row/delete", { method: "POST", body: { row_id: Number(btn.dataset.row) } });
      if (r.ok && r.data.ok) toast("✅ تم حذف الحساب — يمكن الآن إعادة تسجيل نفس الرقم عبر «إضافة حساب»");
      else toast("❌ " + ((r.data || {}).error || "فشل الحذف"), true);
    } else if (act === "envDelete") {
      const go = await confirmModal(
        `حذف الحساب «${btn.dataset.name}» نهائياً من النظام؟\nسيُفصل ويُستبعد حتى بعد إعادة التشغيل، ويمكن بعدها إعادة تسجيل رقمه عبر «إضافة حساب» بدون تعديل متغيرات Render.`,
        "حذف نهائي");
      if (!go) return;
      const r = await api("/admin/api/accounts/env/exclude", { method: "POST", body: { prefix: btn.dataset.prefix } });
      if (r.ok && r.data.ok) toast("✅ حُذف الحساب نهائياً — أعد تسجيل رقمه عبر «إضافة حساب»");
      else toast("❌ " + ((r.data || {}).error || "فشل الحذف"), true);
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
  grpState.items = d.items || [];
  const q = ($("grpSearch").value || "").trim().toLowerCase();
  const items = q
    ? grpState.items.filter((g) =>
        String(g.title || "").toLowerCase().includes(q) ||
        String(g.username || "").toLowerCase().includes(q) ||
        String(g.chat_id || "").includes(q))
    : grpState.items;
  const rows = items.map((g) => {
    const st = g.enabled ? '<span class="pill ok">مراقبة</span>' : '<span class="pill warn">موقوفة</span>';
    const stale = g.stale ? ' <span class="pill bad" title="توقف استقبال الرسائل من هذه المجموعة">متوقفة؟</span>' : "";
    return `<tr>
      <td><input type="checkbox" data-grpcb="${esc(g.chat_id)}"></td>
      <td><b>${esc(g.title)}</b>${g.username ? `<br><small>@${esc(g.username)}</small>` : ""}</td>
      <td><code>${esc(g.chat_id)}</code></td><td>${st}${stale}</td>
      <td>${fmtNum(g.msgs_total)}</td><td>${fmtNum(g.alerts_24h)}</td>
      <td>${g.last_msg_age_minutes != null ? fmtAge(g.last_msg_age_minutes * 60) : "—"}</td>
      <td>
        <button class="btn sm" data-act="toggle" data-id="${g.chat_id}" data-en="${g.enabled ? 1 : 0}">${g.enabled ? "إيقاف" : "تفعيل"}</button>
        <button class="btn sm danger" data-act="remove" data-id="${g.chat_id}">إزالة</button>
      </td></tr>`;
  }).join("");
  $("grpTable").innerHTML = "<thead><tr><th><input type=\"checkbox\" id=\"grpAll\"></th><th>المجموعة</th><th>المعرف</th><th>الحالة</th><th>الرسائل (7 أيام)</th><th>تنبيهات 24س</th><th>آخر رسالة</th><th>إجراءات</th></tr></thead><tbody>" + (rows || `<tr><td colspan='8'>${q ? "لا نتائج مطابقة" : "لا مجموعات محددة"}</td></tr>`) + "</tbody>";
  updateGrpBulk();
}

let grpState = { items: [] };
function grpSelected() {
  return Array.from(document.querySelectorAll("#grpTable input[data-grpcb]:checked"))
    .map((cb) => Number(cb.dataset.grpcb)).filter((n) => Number.isFinite(n));
}
function updateGrpBulk() {
  const n = grpSelected().length;
  $("grpBulkBar").classList.toggle("hidden", n === 0);
  $("grpBulkCount").textContent = `${n} محدد`;
}
$("grpSearch").addEventListener("input", () => {
  clearTimeout($("grpSearch")._tm);
  $("grpSearch")._tm = setTimeout(() => { if (grpState.items.length) renderGroups({ items: grpState.items, allow_all: !$("grpAllowAll").classList.contains("hidden") }); }, 250);
});
$("grpTable").addEventListener("change", (ev) => {
  if (ev.target.id === "grpAll") {
    const checked = ev.target.checked;
    document.querySelectorAll("#grpTable input[data-grpcb]").forEach((cb) => { cb.checked = checked; });
  }
  updateGrpBulk();
});
async function grpBulkToggle(enable) {
  const ids = grpSelected();
  if (!ids.length) return;
  const go = await confirmModal(`${enable ? "تفعيل" : "إيقاف"} مراقبة ${ids.length} مصدر؟`, "تنفيذ");
  if (!go) return;
  let done = 0;
  for (const chat_id of ids) {
    const r = await api("/admin/api/groups/toggle", { method: "POST", body: { chat_id, enabled: enable } });
    if (r.ok) done++;
  }
  toast(`✅ تم تحديث ${done}/${ids.length}`);
  tabLoaders.groups().catch(() => {});
}
$("grpBulkPause").addEventListener("click", () => grpBulkToggle(false));
$("grpBulkResume").addEventListener("click", () => grpBulkToggle(true));
$("grpBulkRemove").addEventListener("click", async () => {
  const ids = grpSelected();
  if (!ids.length) return;
  const go = await confirmModal(`إزالة ${ids.length} مصدر من المراقبة؟ (لن تُحذف رسائلها)`, "إزالة المحدد");
  if (!go) return;
  let done = 0;
  for (const chat_id of ids) {
    const r = await api("/admin/api/groups/remove", { method: "POST", body: { chat_id } });
    if (r.ok) done++;
  }
  toast(`✅ أُزيل ${done}/${ids.length}`);
  tabLoaders.groups().catch(() => {});
});

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
let audFilters = { q: "", hours: 0 };
async function loadAudit(page) {
  audPage = page || 1;
  let qs = `page=${audPage}`;
  if (audFilters.q) qs += `&q=${encodeURIComponent(audFilters.q)}`;
  if (audFilters.hours) qs += `&hours=${audFilters.hours}`;
  const r = await api(`/admin/api/ops/audit?${qs}`);
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
$("audApply").addEventListener("click", () => {
  audFilters.q = $("audSearch").value.trim();
  audFilters.hours = Number($("audHours").value) || 0;
  loadAudit(1).catch(() => {});
});

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

/* ── v15.7.1: حذف الطابور + تنظيف الذاكرة ── */
$("purgeQueueBtn").addEventListener("click", async () => {
  const go1 = await confirmModal("حذف جميع الأحداث المعلقة في الطابور نهائياً؟ الأحداث المحذوفة لن تُعالج ولن تُسترد. الأحداث الجديدة تُعالج طبيعياً.", "تأكيد");
  if (!go1) return;
  const typed = await promptModal("عملية حساسة — اكتب حذف للتأكيد النهائي:", "", "", "تأكيد نهائي");
  if (typed === null) return;
  if ((typed || "").trim() !== "حذف") { toast("لم يُكتب تأكيد صحيح — أُلغيت العملية", true); return; }
  const r = await api("/admin/api/ops/purge_queue", { method: "POST", body: {} });
  if (r.ok && r.data.ok) {
    toast(`🗑 حُذف ${r.data.total} حدثاً (قاعدة بيانات: ${r.data.db_purged} · ذاكرة: ${r.data.memory_drained}) — المتبقي: ${r.data.after}`);
    refreshQueueMemInfo();
  } else toast((r.data && (r.data.note || r.data.error)) || "فشل الحذف", true);
});

$("memCleanBtn").addEventListener("click", async () => {
  const go = await confirmModal("تنظيف الذاكرة الآن؟ مسح الكاشات (الكيانات/الفلترة/قاعدة البيانات) + جامع النفايات — آمن ولا يقاطع المعالجة.", "تنظيف");
  if (!go) return;
  const r = await api("/admin/api/ops/memory_cleanup", { method: "POST", body: {} });
  if (r.ok && r.data.ok) {
    const fb = (r.data.freed_mb || 0);
    toast(`🧹 تم التنظيف — RSS: ${r.data.rss_before_mb} ← ${r.data.rss_after_mb} MB · gc: ${r.data.gc_objects} كائن${fb > 0 ? ` · تحرر ${fb} MB` : ""}`);
    refreshQueueMemInfo();
  } else toast((r.data && (r.data.note || r.data.error)) || "فشل التنظيف", true);
});

async function refreshQueueMemInfo() {
  try {
    const [rq, rm] = await Promise.all([
      api("/admin/api/overview"),
      api("/admin/api/ops"),
    ]);
    const parts = [];
    if (rq.ok) {
      const q = (rq.data.system && rq.data.system.queue_size) ?? rq.data.queue_size;
      if (q !== undefined) parts.push(`الطابور الآن: ${q}`);
    }
    if (rm.ok) {
      const mg = rm.data.mem_guard || {};
      if (mg.limit_mb) parts.push(`الذاكرة: ${mg.last_rss_mb || 0}/${mg.limit_mb} MB${mg.manual_cleanups ? ` · تنظيفات يدوية: ${mg.manual_cleanups}` : ""}`);
    }
    $("queueMemInfo").textContent = parts.join(" · ");
  } catch { /* بلا صوت */ }
}

/* ═════════ P1-UI: المصادقة متعددة المستخدمين — تبويبا الأمان والمستخدمين ═════════ */

/* ── سياق الجلسة الحالية والصلاحيات ── */
function setAuthContext(d) {
  if (d && d.username) username = d.username;
  myRole = (d && d.role) || myRole || "";
  if (d && Array.isArray(d.permissions)) myPermissions = d.permissions;
  mustChangePw = !!(d && d.must_change_password);
  applyPermissionsUI();
}

function resetAuthState() {
  myPermissions = [];
  myRole = "";
  myUserId = null;
  mustChangePw = false;
  meCache = null;
  $("mustPwModal").classList.add("hidden");
  applyPermissionsUI();
}

/* بوابة الصلاحيات: إظهار/إخفاء تبويب المستخدمين حسب users.manage */
function applyPermissionsUI() {
  const canUsers = myPermissions.includes("users.manage");
  const tabBtn = document.querySelector('.tab[data-tab="users"]');
  if (tabBtn) tabBtn.classList.toggle("hidden", !canUsers);
  if (!canUsers && currentTab === "users") switchTab("overview");
}

function showLoginWithMsg(msg) {
  showLogin();
  const okBox = $("loginSuccess");
  okBox.textContent = msg;
  okBox.classList.remove("hidden");
}

/* ── طبقة API للاستجابات الموحدة {success, data, error, note} ── */
async function apiData(path, opts) {
  const r = await api(path, opts);
  const d = r.data || {};
  if (r.ok && d.success) return { ok: true, status: r.status, data: d.data || {}, note: d.note || "" };
  return { ok: false, status: r.status, error: d.error || "خطأ غير متوقع", note: d.note || "" };
}
function apiFailMsg(res) { return (res && (res.note || res.error)) || "خطأ غير متوقع"; }

/* ── نسخ إلى الحافظة (بدون مكتبات وبدون موارد خارجية) ── */
function copyText(txt, label) {
  const done = () => toast("✅ تم النسخ" + (label ? " — " + label : ""));
  const legacy = () => {
    const ta = document.createElement("textarea");
    ta.value = txt;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } catch (e) { toast("تعذر النسخ — انسخ يدوياً", true); }
    document.body.removeChild(ta);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(done).catch(legacy);
  } else legacy();
}
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-copy]");
  if (!btn) return;
  copyText(btn.dataset.copy || "", btn.dataset.copyLabel || "");
});

/* ── تبويب الأمان: حسابي (auth/me) ── */
async function ensureMe(force) {
  if (meCache && !force) return meCache;
  const res = await apiData("/admin/api/auth/me");
  if (!res.ok) return null;
  meCache = res.data;
  const u = res.data.user || {};
  if (u.id != null) myUserId = u.id;
  if (u.role) myRole = u.role;
  mustChangePw = !!u.must_change_password;
  if (Array.isArray(res.data.permissions)) myPermissions = res.data.permissions;
  return meCache;
}

function renderSecMe(d) {
  const u = (d && d.user) || {};
  const s = (d && d.session) || {};
  $("secMe").innerHTML = `<div class="kvlist">
    <span>اسم المستخدم</span><b>${esc(u.username || "—")}</b>
    <span>الاسم المعروض</span><b>${esc(u.display_name || "—")}</b>
    <span>الدور</span><b>${esc(ROLE_AR[u.role] || u.role || "—")}</b>
    <span>المصادقة الثنائية</span><b>${u.totp_enabled ? "مفعّلة ✅" : "غير مفعّلة"}</b>
    <span>آخر دخول</span><b>${fmtTs(u.last_login_at)}</b>
    <span>أُنشئ الحساب</span><b>${fmtTs(u.created_at)}</b>
    <span>IP الجلسة الحالية</span><b>${esc(s.ip || "—")}</b>
    <span>بداية الجلسة</span><b>${fmtTs(s.created_at)}</b>
  </div>`;
  const on = !!u.totp_enabled;
  $("totpStatus").innerHTML = on ? '<span class="pill ok">مفعّلة</span>' : '<span class="pill warn">غير مفعّلة</span>';
  const tbtn = $("totpToggleBtn");
  tbtn.textContent = on ? "تعطيل" : "تفعيل";
  tbtn.classList.toggle("primary", !on);
  tbtn.classList.toggle("danger", on);
  tbtn.dataset.enabled = on ? "1" : "0";
}

tabLoaders.security = async function () {
  const me = await ensureMe();
  if (me) renderSecMe(me);
  $("sessScopeBtn").classList.toggle("hidden", myRole !== "owner");
  await loadSessions();
};

async function refreshSecurity() {
  meCache = null;
  const me = await ensureMe(true);
  if (me) renderSecMe(me);
  await loadSessions();
}

/* ── تبويب الأمان: الجلسات النشطة ── */
async function loadSessions() {
  const res = await apiData(`/admin/api/auth/sessions?scope=${encodeURIComponent(sessScope)}`);
  if (!res.ok) { toast(apiFailMsg(res), true); return; }
  renderSessions(res.data);
}

function renderSessions(d) {
  const list = d.sessions || [];
  const rows = list.map((s) => {
    const curBadge = s.current ? ' <span class="pill ok">الحالية</span>' : "";
    const stBadge = s.revoked ? '<span class="pill bad">ملغاة</span>' : '<span class="pill ok">نشطة</span>';
    const act = (!s.revoked && !s.current)
      ? `<button class="btn sm danger" data-sessact="revoke" data-hash="${esc(s.token_hash || "")}">إبطال</button>`
      : "—";
    return `<tr>
      <td><b>${esc(s.username || "—")}</b>${curBadge}</td>
      <td>${esc(ROLE_AR[s.role] || s.role || "—")}</td>
      <td><code>${esc(s.ip || "—")}</code></td>
      <td title="${esc(s.user_agent || "")}">${esc(String(s.user_agent || "—").slice(0, 36))}</td>
      <td>${fmtTs(s.last_seen)}</td>
      <td>${fmtTs(s.expires_at)}</td>
      <td>${stBadge}</td>
      <td>${act}</td>
    </tr>`;
  }).join("");
  $("sessTable").innerHTML = "<thead><tr><th>المستخدم</th><th>الدور</th><th>IP</th><th>المتصفح</th><th>آخر ظهور</th><th>ينتهي</th><th>الحالة</th><th>إجراء</th></tr></thead><tbody>" +
    (rows || "<tr><td colspan='8'>لا جلسات</td></tr>") + "</tbody>";
  $("sessScopeNote").textContent = sessScope === "all" ? "تُعرض جلسات جميع المستخدمين" : "تُعرض جلساتك فقط";
}

$("sessRefresh").addEventListener("click", () => loadSessions().catch(() => {}));
$("sessScopeBtn").addEventListener("click", () => {
  sessScope = sessScope === "self" ? "all" : "self";
  $("sessScopeBtn").textContent = sessScope === "all" ? "عرض جلساتي فقط" : "عرض كل المستخدمين";
  loadSessions().catch(() => {});
});
$("sessRevokeAll").addEventListener("click", async () => {
  const go = await confirmModal("إبطال جميع الجلسات الأخرى (ما عدا جلستك الحالية)؟ سيُجبر أصحابها على تسجيل الدخول من جديد.", "إبطال الكل");
  if (!go) return;
  const res = await apiData("/admin/api/auth/sessions/revoke-all", { method: "POST", body: {} });
  if (res.ok) toast(`✅ أُبطلت ${res.data.revoked_count ?? 0} جلسة — جلستك الحالية محفوظة`);
  else toast(apiFailMsg(res), true);
  loadSessions().catch(() => {});
});
$("sessTable").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-sessact]");
  if (!btn) return;
  const go = await confirmModal("إبطال هذه الجلسة؟ سيُجبر مستخدمها على تسجيل الدخول من جديد.", "إبطال");
  if (!go) return;
  btn.disabled = true;
  const res = await apiData("/admin/api/auth/sessions/revoke", { method: "POST", body: { token_hash: btn.dataset.hash } });
  if (res.ok) toast("✅ أُبطلت الجلسة");
  else toast(apiFailMsg(res), true);
  loadSessions().catch(() => {});
});

/* ── تبويب الأمان: تغيير كلمة المرور ── */
function pwValidate(cur, nw, nw2) {
  if (!cur || !nw) return "أدخل كلمة المرور الحالية والجديدة";
  if (nw !== nw2) return "كلمتا المرور الجديدتان غير متطابقتين";
  return "";
}

$("pwForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const e = $("pwError");
  e.classList.add("hidden");
  const msg = pwValidate($("pwCurrent").value, $("pwNew").value, $("pwNew2").value);
  if (msg) { e.textContent = msg; e.classList.remove("hidden"); return; }
  $("pwSaveBtn").disabled = true;
  const res = await apiData("/admin/api/auth/change-password", {
    method: "POST",
    body: { current_password: $("pwCurrent").value, new_password: $("pwNew").value },
  });
  $("pwSaveBtn").disabled = false;
  if (res.ok) {
    sessionStorage.removeItem("br7dash_csrf");
    $("pwForm").reset();
    resetAuthState();
    showLoginWithMsg("✅ " + ((res.data && res.data.message) || "تم تغيير كلمة المرور — سجّل الدخول من جديد"));
  } else {
    e.textContent = apiFailMsg(res);
    e.classList.remove("hidden");
  }
});

/* ── بوابة تغيير كلمة المرور الإجبارية (تغطي اللوحة، غير قابلة للإغلاق) ── */
function openMustPwModal() {
  ["mpCurrent", "mpNew", "mpNew2"].forEach((id) => { $(id).value = ""; });
  const e = $("mpError");
  e.textContent = "";
  e.classList.add("hidden");
  $("mustPwModal").classList.remove("hidden");
  setTimeout(() => $("mpCurrent").focus(), 60);
}

async function submitMustPassword() {
  const e = $("mpError");
  e.classList.add("hidden");
  const msg = pwValidate($("mpCurrent").value, $("mpNew").value, $("mpNew2").value);
  if (msg) { e.textContent = msg; e.classList.remove("hidden"); return; }
  $("mpSaveBtn").disabled = true;
  const res = await apiData("/admin/api/auth/change-password", {
    method: "POST",
    body: { current_password: $("mpCurrent").value, new_password: $("mpNew").value },
  });
  $("mpSaveBtn").disabled = false;
  if (res.ok) {
    sessionStorage.removeItem("br7dash_csrf");
    $("mustPwModal").classList.add("hidden");
    resetAuthState();
    showLoginWithMsg("✅ " + ((res.data && res.data.message) || "تم تغيير كلمة المرور — سجّل الدخول من جديد"));
  } else {
    e.textContent = apiFailMsg(res);
    e.classList.remove("hidden");
  }
}
$("mpSaveBtn").addEventListener("click", submitMustPassword);
["mpCurrent", "mpNew", "mpNew2"].forEach((id) => {
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") submitMustPassword(); });
});

/* ── تبويب الأمان: المصادقة الثنائية TOTP ── */
$("totpToggleBtn").addEventListener("click", async () => {
  if ($("totpToggleBtn").dataset.enabled === "1") {
    $("totpSetupBox").classList.add("hidden");
    $("totpRecoveryBox").classList.add("hidden");
    $("totpDisablePass").value = "";
    $("totpDisableBox").classList.remove("hidden");
    setTimeout(() => $("totpDisablePass").focus(), 60);
  } else {
    $("totpDisableBox").classList.add("hidden");
    await totpStartSetup();
  }
});

async function totpStartSetup() {
  const res = await apiData("/admin/api/auth/totp/setup", { method: "POST", body: {} });
  if (!res.ok) { toast(apiFailMsg(res), true); return; }
  const d = res.data;
  $("totpUri").textContent = d.otpauth_uri || "—";
  $("totpSecret").textContent = d.secret || "—";
  $("totpUriCopy").dataset.copy = d.otpauth_uri || "";
  $("totpSecretCopy").dataset.copy = d.secret || "";
  $("totpCodeInput").value = "";
  $("totpSetupBox").classList.remove("hidden");
  setTimeout(() => $("totpCodeInput").focus(), 60);
}

$("totpCancelBtn").addEventListener("click", () => $("totpSetupBox").classList.add("hidden"));

$("totpEnableBtn").addEventListener("click", async () => {
  const code = $("totpCodeInput").value.trim();
  if (!code) { toast("أدخل الرمز من تطبيق المصادقة", true); return; }
  $("totpEnableBtn").disabled = true;
  const res = await apiData("/admin/api/auth/totp/enable", { method: "POST", body: { code } });
  $("totpEnableBtn").disabled = false;
  if (!res.ok) { toast(apiFailMsg(res), true); return; }
  $("totpSetupBox").classList.add("hidden");
  renderRecoveryCodes(res.data.recovery_codes || []);
  toast("✅ فُعّلت المصادقة الثنائية — احفظ رموز الاسترداد الآن");
  refreshSecurity().catch(() => {});
});

$("totpCodeInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("totpEnableBtn").click(); });

function renderRecoveryCodes(codes) {
  $("totpDisableBox").classList.add("hidden");
  $("totpRecoveryCodes").innerHTML = (codes || []).map((c) => `<code>${esc(c)}</code>`).join("");
  $("totpRecoveryCopy").dataset.copy = (codes || []).join("\n");
  $("totpRecoveryCopy").dataset.copyLabel = "رموز الاسترداد";
  $("totpRecoveryBox").classList.remove("hidden");
}

$("totpRecoveryDone").addEventListener("click", () => $("totpRecoveryBox").classList.add("hidden"));
$("totpDisableCancel").addEventListener("click", () => $("totpDisableBox").classList.add("hidden"));

async function totpDisableNow() {
  const pass = $("totpDisablePass").value;
  if (!pass) { toast("أدخل كلمة المرور لتأكيد التعطيل", true); return; }
  $("totpDisableConfirm").disabled = true;
  const res = await apiData("/admin/api/auth/totp/disable", { method: "POST", body: { password: pass } });
  $("totpDisableConfirm").disabled = false;
  $("totpDisablePass").value = "";
  if (!res.ok) { toast(apiFailMsg(res), true); return; }
  $("totpDisableBox").classList.add("hidden");
  toast("✅ عُطّلت المصادقة الثنائية");
  refreshSecurity().catch(() => {});
}
$("totpDisableConfirm").addEventListener("click", totpDisableNow);
$("totpDisablePass").addEventListener("keydown", (e) => { if (e.key === "Enter") totpDisableNow(); });

/* ── تبويب المستخدمون (صلاحية users.manage فقط) ── */
/* نافذة إدخال كلمة مرور — بنفس نمط promptModal لكن الحقل مخفي */
function promptPasswordModal(msg, okLabel) {
  $("modalBody").innerHTML = esc(msg) +
    '<br><input id="modalInput" type="password" style="width:100%;margin-top:12px" autocomplete="new-password">';
  $("modalOk").textContent = okLabel || "حفظ";
  $("modal").classList.remove("hidden");
  return new Promise((res) => { modalResolver = res; });
}

function roleOptions(selected) {
  return usrRoles.map((r) =>
    `<option value="${esc(r)}"${r === selected ? " selected" : ""}>${esc(ROLE_AR[r] || r)}</option>`).join("");
}

tabLoaders.users = async function () {
  if (!myPermissions.includes("users.manage")) { switchTab("overview"); return; }
  await ensureMe();
  await loadUsers();
};

async function loadUsers() {
  const res = await apiData("/admin/api/users");
  if (!res.ok) { toast(apiFailMsg(res), true); return; }
  usrRoles = (res.data.roles && res.data.roles.length) ? res.data.roles : ["owner", "admin", "operator", "viewer"];
  $("usrAddRole").innerHTML = roleOptions("");
  $("usrAddRole").value = usrRoles.includes("admin") ? "admin" : (usrRoles[0] || "viewer");
  const users = res.data.users || [];
  $("usrNote").textContent = `${users.length} مستخدم مسجّل`;
  renderUsers(users);
}

function renderUsers(users) {
  const rows = users.map((u) => {
    const isSelf = (myUserId != null && u.id === myUserId) || (u.username && u.username === username);
    const activePill = u.is_active ? '<span class="pill ok">نشط</span>' : '<span class="pill bad">معطّل</span>';
    const mc = u.must_change_password ? ' <span class="pill warn">يغيّر كلمة المرور</span>' : "";
    const totpPill = u.totp_enabled ? '<span class="pill ok">TOTP</span>' : '<span class="pill muted">بدون TOTP</span>';
    const roleSel = `<select data-usrsel="${u.id}" data-prev="${esc(u.role)}" data-uname="${esc(u.username)}">${roleOptions(u.role)}</select>`;
    const acts = [
      `<button class="btn sm" data-usrcode="rename" data-id="${u.id}" data-name="${esc(u.display_name || "")}" data-uname="${esc(u.username)}">تعديل الاسم</button>`,
      `<button class="btn sm" data-usrcode="resetpw" data-id="${u.id}" data-uname="${esc(u.username)}">كلمة مرور جديدة</button>`,
      `<button class="btn sm ${u.is_active ? "warn" : "primary"}" data-usrcode="active" data-id="${u.id}" data-en="${u.is_active ? 1 : 0}" data-uname="${esc(u.username)}">${u.is_active ? "تعطيل" : "تفعيل"}</button>`,
    ];
    if (!isSelf) { /* لا زر حذف على حسابك الحالي */
      acts.push(`<button class="btn sm danger" data-usrcode="delete" data-id="${u.id}" data-uname="${esc(u.username)}">حذف</button>`);
    }
    return `<tr>
      <td><b>${esc(u.username)}</b>${isSelf ? ' <span class="pill muted">أنت</span>' : ""}${mc}</td>
      <td>${esc(u.display_name || "—")}</td>
      <td>${roleSel}</td>
      <td>${activePill}</td>
      <td>${totpPill}</td>
      <td>${fmtTs(u.last_login_at)}</td>
      <td>${fmtTs(u.created_at)}</td>
      <td>${acts.join(" ")}</td>
    </tr>`;
  }).join("");
  $("usrTable").innerHTML = "<thead><tr><th>المستخدم</th><th>الاسم المعروض</th><th>الدور</th><th>الحالة</th><th>TOTP</th><th>آخر دخول</th><th>أُنشئ</th><th>إجراءات</th></tr></thead><tbody>" +
    (rows || "<tr><td colspan='8'>لا مستخدمين</td></tr>") + "</tbody>";
}

$("usrRefresh").addEventListener("click", () => loadUsers().catch(() => {}));

$("usrTable").addEventListener("change", async (ev) => {
  const sel = ev.target.closest("select[data-usrsel]");
  if (!sel) return;
  const id = Number(sel.dataset.usrsel);
  const prevRole = sel.dataset.prev || "";
  const newRole = sel.value;
  const uname = sel.dataset.uname || ("#" + id);
  if (newRole === prevRole) return;
  const go = await confirmModal(`تغيير دور «${uname}» من «${ROLE_AR[prevRole] || prevRole}» إلى «${ROLE_AR[newRole] || newRole}»؟ تُطبَّق الصلاحيات فوراً.`, "تغيير الدور");
  if (!go) { sel.value = prevRole; return; }
  const res = await apiData("/admin/api/users/update", { method: "POST", body: { user_id: id, role: newRole } });
  if (res.ok) toast(`✅ صار دور «${uname}»: ${ROLE_AR[newRole] || newRole}`);
  else toast(apiFailMsg(res), true);
  loadUsers().catch(() => {});
});

$("usrTable").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-usrcode]");
  if (!btn) return;
  const act = btn.dataset.usrcode;
  const id = Number(btn.dataset.id);
  const uname = btn.dataset.uname || ("#" + id);
  if (act === "rename") {
    const name = await promptModal(`الاسم المعروض الجديد لـ «${uname}»:`, "الاسم", btn.dataset.name || "", "حفظ");
    if (!name) return;
    const res = await apiData("/admin/api/users/update", { method: "POST", body: { user_id: id, display_name: name } });
    if (res.ok) toast("✅ حُدّث الاسم المعروض");
    else toast(apiFailMsg(res), true);
    loadUsers().catch(() => {});
  } else if (act === "resetpw") {
    const np = await promptPasswordModal(`كلمة مرور جديدة للمستخدم «${uname}»:`, "إعادة التعيين");
    if (!np) return;
    const res = await apiData("/admin/api/users/reset-password", { method: "POST", body: { user_id: id, new_password: np } });
    if (res.ok) toast(`✅ أُعيد تعيين كلمة مرور «${uname}» — سيُطلب منه تغييرها عند الدخول`);
    else toast(apiFailMsg(res), true);
  } else if (act === "active") {
    const enable = btn.dataset.en !== "1";
    const go = await confirmModal(enable
      ? `تفعيل المستخدم «${uname}»؟`
      : `تعطيل المستخدم «${uname}»؟ لن يتمكن من الدخول وتُبطل جلساته.`, enable ? "تفعيل" : "تعطيل");
    if (!go) return;
    const res = await apiData("/admin/api/users/update", { method: "POST", body: { user_id: id, is_active: enable } });
    if (res.ok) toast(enable ? `✅ فُعّل «${uname}»` : `⛔ عُطّل «${uname}»`);
    else toast(apiFailMsg(res), true);
    loadUsers().catch(() => {});
  } else if (act === "delete") {
    /* تأكيد خطير بنفس نمط حذف الطابور: تأكيد + كتابة اسم المستخدم يدوياً */
    const go1 = await confirmModal(`حذف المستخدم «${uname}» نهائياً؟ تُبطل جميع جلساته ولا يمكن التراجع.`, "متابعة الحذف");
    if (!go1) return;
    const typed = await promptModal(`عملية حساسة — اكتب اسم المستخدم «${uname}» للتأكيد النهائي:`, "", "", "تأكيد نهائي");
    if (typed === null) return;
    if ((typed || "").trim() !== uname) { toast("الاسم المكتوب لا يطابق — أُلغيت العملية", true); return; }
    const res = await apiData("/admin/api/users/delete", { method: "POST", body: { user_id: id } });
    if (res.ok) toast(`✅ حُذف المستخدم «${uname}»`);
    else toast(apiFailMsg(res), true);
    loadUsers().catch(() => {});
  }
});

$("usrAddBtn").addEventListener("click", async () => {
  const uname = $("usrAddUsername").value.trim();
  const pass = $("usrAddPass").value;
  const role = $("usrAddRole").value;
  const disp = $("usrAddDisplay").value.trim();
  if (!uname || !pass) { toast("أدخل اسم المستخدم وكلمة المرور", true); return; }
  $("usrAddBtn").disabled = true;
  const res = await apiData("/admin/api/users/create", {
    method: "POST",
    body: { username: uname, password: pass, role, display_name: disp },
  });
  $("usrAddBtn").disabled = false;
  if (res.ok) {
    toast(`✅ أُنشئ المستخدم «${uname}» بدور ${ROLE_AR[role] || role}`);
    $("usrAddUsername").value = "";
    $("usrAddPass").value = "";
    $("usrAddDisplay").value = "";
    loadUsers().catch(() => {});
  } else toast(apiFailMsg(res), true);
});

/* ── الإقلاع ── */
(async function boot() {
  const r = await api("/admin/api/session");
  const d = r.data || {};
  $("versionBadge").textContent = "v" + (d.version || "");
  if (d.authenticated) {
    setAuthContext(d);
    hideLogin();
    username = d.username || "";
    loadOpsBanner().catch(() => {});
    switchTab("overview");
    if (d.must_change_password) openMustPwModal(); /* بوابة تغيير كلمة المرور الإجبارية */
  } else {
    showLogin();
  }
})();

/* ═════════ الوحدة 2-ب: معالج ربط حساب جديد (v15.2.0) ═════════ */
const WIZ_COUNTRIES = [
  ["+966", "السعودية"], ["+971", "الإمارات"], ["+965", "الكويت"], ["+973", "البحرين"],
  ["+974", "قطر"], ["+968", "عُمان"], ["+967", "اليمن"], ["+962", "الأردن"],
  ["+963", "سوريا"], ["+961", "لبنان"], ["+964", "العراق"], ["+970", "فلسطين"],
  ["+20", "مصر"], ["+218", "ليبيا"], ["+216", "تونس"], ["+213", "الجزائر"],
  ["+212", "المغرب"], ["+249", "السودان"], ["+253", "جيبوتي"], ["+252", "الصومال"],
  ["+222", "موريتانيا"], ["+90", "تركيا"], ["+44", "المملكة المتحدة"], ["+1", "أمريكا/كندا"],
  ["+91", "الهند"], ["+92", "باكستان"], ["+62", "إندونيسيا"], ["+63", "الفلبين"],
  ["+49", "ألمانيا"], ["+33", "فرنسا"], ["+39", "إيطاليا"], ["+34", "إسبانيا"],
  ["+31", "هولندا"], ["+46", "السويد"], ["+60", "ماليزيا"], ["+84", "فيتنام"],
];
const WIZ_STATE_AR = {
  linking: "جاري الربط", waiting_code: "بانتظار الرمز", waiting_password: "بانتظار كلمة المرور",
  verifying: "جاري التحقق", ready: "جاهز", active: "نشط", error: "خطأ",
};
let wizOpId = null;
let wizTimer = null;

function wizShowStep(name) {
  ["Phone", "Code", "Pass", "Verify"].forEach((s) => {
    const el = $("wiz" + s);
    if (el) el.classList.toggle("hidden", s !== name);
  });
  const idx = { Phone: 0, Code: 1, Pass: 2, Verify: 3 }[name];
  document.querySelectorAll("#wizSteps li").forEach((li) => {
    const i = Number(li.dataset.i);
    li.className = i < idx ? "done" : i === idx ? "cur" : "";
  });
}
function wizErr(id, msg) { const el = $(id); el.textContent = msg; el.classList.remove("hidden"); }
function wizClearErr() { ["wizPhoneErr", "wizCodeErr", "wizPassErr"].forEach((id) => $(id).classList.add("hidden")); }
function wizMsg(d) { return (d && (d.error_message || d.note || d.error)) || "حدث خطأ غير متوقع"; }
function wizFmtWait(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  if (sec < 60) return sec + " ثانية";
  const m = Math.floor(sec / 60);
  if (m < 60) return m + " دقيقة";
  return Math.floor(m / 60) + " ساعة " + (m % 60) + " د";
}

function openWizard() {
  wizOpId = null;
  clearInterval(wizTimer); wizTimer = null;
  $("wizCC").innerHTML = WIZ_COUNTRIES.map(([c, n]) => `<option value="${c}">${c} — ${n}</option>`).join("");
  $("wizCC").value = "+966";
  $("wizPhoneInput").value = ""; $("wizCodeInput").value = ""; $("wizPassInput").value = "";
  wizClearErr();
  wizShowStep("Phone");
  $("wiz").classList.remove("hidden");
}

function closeWizard() {
  clearInterval(wizTimer); wizTimer = null;
  if (wizOpId) {
    api("/admin/api/accounts/add/cancel", { method: "POST", body: { op_id: wizOpId } }).catch(() => {});
    wizOpId = null;
  }
  $("wiz").classList.add("hidden");
  tabLoaders.accounts().catch(() => {});
}

function wizFlood(d) {
  if (d && d.error_code === "flood_wait" && d.flood_wait > 0)
    return `تجاوزت حد المحاولات لدى تيليجرام — انتظر ${wizFmtWait(d.flood_wait)} ثم أعد المحاولة`;
  return wizMsg(d);
}

async function wizPhoneNext() {
  const cc = $("wizCC").value;
  const national = $("wizPhoneInput").value.replace(/\D/g, "");
  wizClearErr();
  if (!national || national.length < 6) { wizErr("wizPhoneErr", "أدخل رقم الهاتف كاملاً دون مفتاح الدولة"); return; }
  const btn = $("wizPhoneNext");
  btn.disabled = true; btn.textContent = "جارٍ الإرسال…";
  try {
    const r = await api("/admin/api/accounts/add/start", { method: "POST", body: { phone: cc + national } });
    const d = r.data || {};
    if (d.ok && d.op) {
      wizOpId = d.op.op_id;
      $("wizCodePhone").textContent = cc + national;
      wizShowStep("Code");
      setTimeout(() => $("wizCodeInput").focus(), 60);
      wizPollStart();
    } else {
      wizErr("wizPhoneErr", wizFlood(d));
    }
  } finally { btn.disabled = false; btn.textContent = "إرسال رمز التحقق"; }
}

async function wizCodeNext() {
  const code = $("wizCodeInput").value.replace(/\D/g, "");
  wizClearErr();
  if (!code) { wizErr("wizCodeErr", "أدخل رمز التأكيد"); return; }
  const btn = $("wizCodeNext");
  btn.disabled = true; btn.textContent = "جارٍ التحقق…";
  try {
    const r = await api("/admin/api/accounts/add/code", { method: "POST", body: { op_id: wizOpId, code } });
    wizHandleResult(r.data || {}, "#wizCodeErr");
  } finally { btn.disabled = false; btn.textContent = "متابعة"; }
}

async function wizPassNext() {
  const password = $("wizPassInput").value;
  wizClearErr();
  if (!password) { wizErr("wizPassErr", "أدخل كلمة المرور"); return; }
  const btn = $("wizPassNext");
  btn.disabled = true; btn.textContent = "جارٍ التحقق…";
  try {
    const r = await api("/admin/api/accounts/add/password", { method: "POST", body: { op_id: wizOpId, password } });
    $("wizPassInput").value = "";
    wizHandleResult(r.data || {}, "#wizPassErr");
  } finally { btn.disabled = false; btn.textContent = "تأكيد كلمة المرور"; }
}

function wizHandleResult(d, errBox) {
  if (d.ok === false) {
    if (d.error_code === "code_expired" || d.exhausted || d.error_code === "duplicate_after_auth") {
      wizShowStep("Verify");
      $("wizLoading").classList.add("hidden");
      $("wizSuccess").classList.add("hidden");
      $("wizFail").classList.remove("hidden");
      $("wizFailMsg").textContent = wizFlood(d);
      clearInterval(wizTimer); wizTimer = null;
      wizOpId = null;
    } else {
      wizErr(errBox, wizFlood(d));
      if (d.op && d.op.state === "waiting_password") {
        wizShowStep("Pass");
        setTimeout(() => $("wizPassInput").focus(), 60);
      }
    }
    return;
  }
  const op = d.op;
  if (!op) return;
  const st = op.state;
  if (st === "waiting_password") {
    wizShowStep("Pass");
    setTimeout(() => $("wizPassInput").focus(), 60);
  } else if (st === "verifying") {
    wizShowVerifyLoading();
  } else if (st === "ready" || st === "active") {
    wizShowSuccess(op);
  }
}

function wizShowVerifyLoading() {
  wizShowStep("Verify");
  $("wizLoading").classList.remove("hidden");
  $("wizSuccess").classList.add("hidden");
  $("wizFail").classList.add("hidden");
  wizPollStart();
}

function wizShowSuccess(op) {
  clearInterval(wizTimer); wizTimer = null;
  wizShowStep("Verify");
  $("wizLoading").classList.add("hidden");
  $("wizFail").classList.add("hidden");
  const me = op.me || {};
  $("wizMe").innerHTML = `
    <span>الاسم</span><b>${esc(me.name || "—")}</b>
    <span>المعرف</span><b>${me.id ? esc(String(me.id)) : "—"}</b>
    <span>المستخدم</span><b>${me.username ? "@" + esc(me.username) : "—"}</b>
    <span>الهاتف</span><b>${esc(me.phone || op.phone || "—")}</b>
    <span>الحالة</span><b>${esc(WIZ_STATE_AR[op.state] || op.state)}</b>`;
  $("wizActivateNote").textContent = op.state === "active"
    ? "الحساب نشط ويشارك في المراقبة الآن."
    : "الحساب محفوظ وجلسته مشفّرة — تفعيل المراقبة جارٍ على الخادم، حدّث الجدول بعد لحظات.";
  $("wizSuccess").classList.remove("hidden");
  wizOpId = null;
}

function wizPollStart() {
  clearInterval(wizTimer);
  wizTimer = setInterval(async () => {
    if (!wizOpId) { clearInterval(wizTimer); return; }
    const r = await api(`/admin/api/accounts/add/status?op_id=${encodeURIComponent(wizOpId)}`);
    const d = r.data || {};
    if (r.status === 404) { wizShowVerifyFail("انتهت العملية على الخادم — أبدأ من جديد"); return; }
    if (d.ok && d.op) {
      const st = d.op.state;
      if (st === "verifying") wizShowVerifyLoading();
      else if (st === "ready" || st === "active") wizShowSuccess(d.op);
      else if (st === "error") wizShowVerifyFail(d.op.error_message || "فشلت عملية الربط");
    }
  }, 2500);
}

function wizShowVerifyFail(msg) {
  wizShowStep("Verify");
  $("wizLoading").classList.add("hidden");
  $("wizSuccess").classList.add("hidden");
  $("wizFail").classList.remove("hidden");
  $("wizFailMsg").textContent = msg;
  clearInterval(wizTimer); wizTimer = null;
  wizOpId = null;
}

$("accAdd").addEventListener("click", openWizard);
$("wizPhoneNext").addEventListener("click", wizPhoneNext);
$("wizCodeNext").addEventListener("click", wizCodeNext);
$("wizPassNext").addEventListener("click", wizPassNext);
$("wizPhoneCancel").addEventListener("click", closeWizard);
$("wizCodeCancel").addEventListener("click", closeWizard);
$("wizPassCancel").addEventListener("click", closeWizard);
$("wizDone").addEventListener("click", () => { wizOpId = null; $("wiz").classList.add("hidden"); tabLoaders.accounts().catch(() => {}); });
$("wizClose").addEventListener("click", () => { wizOpId = null; $("wiz").classList.add("hidden"); });
$("wizRestart").addEventListener("click", openWizard);
$("wizPhoneInput").addEventListener("keydown", (e) => { if (e.key === "Enter") wizPhoneNext(); });
$("wizCodeInput").addEventListener("keydown", (e) => { if (e.key === "Enter") wizCodeNext(); });
$("wizPassInput").addEventListener("keydown", (e) => { if (e.key === "Enter") wizPassNext(); });
$("wiz").addEventListener("click", (e) => { if (e.target === $("wiz")) closeWizard(); });
/* ═════════ نهاية معالج الربط ═════════ */

/* ═════════ الوحدة 3-ب: بحث الكلمات (v15.7.0) ═════════ */
function kwFilterApply() {
  const q = ($("kwSearch").value || "").trim().toLowerCase();
  document.querySelectorAll("#kwCats details").forEach((d) => {
    if (!q) { d.style.display = ""; d.open = false; return; }
    const hit = (d.textContent || "").toLowerCase().includes(q);
    d.style.display = hit ? "" : "none";
    if (hit) d.open = true;
  });
}
$("kwSearch").addEventListener("input", () => {
  clearTimeout($("kwSearch")._tm);
  $("kwSearch")._tm = setTimeout(kwFilterApply, 250);
});
$("kwSearchClear").addEventListener("click", () => {
  $("kwSearch").value = "";
  kwFilterApply();
});

/* ═════════ الوحدة 7: الحظر (v15.7.0) ═════════ */
let blkPage = 1;
let blkKind = "sender";

tabLoaders.blocked = async function () { await loadBlocked(); };

const BLK_PLACEHOLDERS = {
  sender: "123456789 أو @username أو t.me/name",
  bot: "123456789 أو @botname أو t.me/botname",
  chat: "t.me/group أو -1001234567890",
};

async function loadBlocked() {
  const kind = $("blkKindFilter").value;
  const q = $("blkSearch").value.trim();
  const r = await api(`/admin/api/blocked?kind=${kind}&q=${encodeURIComponent(q)}&page=${blkPage}`);
  if (!r.ok) { toast("تعذر تحميل قوائم الحظر", true); return; }
  renderBlocked(r.data, kind);
}

function renderBlocked(d, kind) {
  blkKind = kind;
  $("blkNote").textContent = d.note || "";
  const ov = d.overview || {};
  const cap = d.capture_from_bots;
  $("blkChips").innerHTML =
    `<span class="chip">مرسلون محظورون: <b>${fmtNum(ov.senders)}</b></span>` +
    `<span class="chip">مجموعات محظورة: <b>${fmtNum(ov.chats)}</b></span>` +
    `<span class="chip">بوتات محظورة: <b>${fmtNum(ov.bots)}</b></span>` +
    (cap === null ? "" : `<span class="chip">التقاط البوتات (env): <b>${cap ? "مفعّل" : "معطّل"}</b></span>`);
  const rows = (d.items || []).map((b) => {
    const who = b.username ? "@" + b.username : (b.name || "—");
    const bot = b.is_bot ? ' <span class="pill muted">بوت</span>' : "";
    const reason = b.reason ? esc(b.reason) : '<small>بدون سبب</small>';
    return `<tr>
      <td><input type="checkbox" data-blkcb="${esc(b.entity_id)}"></td>
      <td><code>${esc(b.entity_id)}</code></td>
      <td>${esc(who)}${bot}</td>
      <td>${reason}</td>
      <td><small>${esc(b.blocked_by || "")}</small></td>
      <td><small>${esc(b.blocked_at || "")}</small></td>
      <td><button class="btn sm" data-blkact="remove" data-id="${esc(b.entity_id)}">إلغاء الحظر</button></td>
    </tr>`;
  }).join("");
  $("blkTable").innerHTML = "<thead><tr><th></th><th>المعرف</th><th>الكيان</th><th>السبب</th><th>بواسطة</th><th>التاريخ</th><th></th></tr></thead><tbody>" +
    (rows || "<tr><td colspan='7'>لا حظر في هذه القائمة</td></tr>") + "</tbody>";
  $("blkPageInfo").textContent = `صفحة ${d.page} · الإجمالي ${fmtNum(d.total)}`;
  $("blkPrev").disabled = d.page <= 1;
  $("blkNext").disabled = !d.has_more;
  updateBlkBulk();
}

function blkSelected() {
  return Array.from(document.querySelectorAll("#blkTable input[data-blkcb]:checked"))
    .map((cb) => Number(cb.dataset.blkcb)).filter((n) => Number.isFinite(n));
}
function updateBlkBulk() {
  const n = blkSelected().length;
  $("blkBulkRemove").disabled = n === 0;
  $("blkBulkRemove").textContent = n ? `إلغاء حظر ${n} المحدد` : "إلغاء حظر المحدد";
}
$("blkTable").addEventListener("change", () => updateBlkBulk());

$("blkKind").addEventListener("change", () => {
  $("blkInput").placeholder = BLK_PLACEHOLDERS[$("blkKind").value] || BLK_PLACEHOLDERS.sender;
});

$("blkAddBtn").addEventListener("click", async () => {
  const input = $("blkInput").value.trim();
  if (!input) { toast("أدخل المعرف أو الرابط", true); return; }
  const kind = $("blkKind").value;
  const label = kind === "chat" ? "حظر هذه المجموعة/القناة ومنع معالجة رسائلها؟"
    : kind === "bot" ? "حظر هذا البوت ومنع معالجة رسائله؟"
    : "حظر هذا المرسل ومنع معالجة رسائله؟";
  const go = await confirmModal(label, "حظر");
  if (!go) return;
  const r = await api("/admin/api/blocked/add", {
    method: "POST",
    body: { input, kind, reason: $("blkReason").value.trim() },
  });
  if (r.ok && r.data.ok) {
    const t = r.data.title ? ` (${r.data.title})` : "";
    toast(`✅ تم الحظر [${r.data.kind}] ${r.data.entity_id}${t}`);
    $("blkInput").value = "";
    $("blkReason").value = "";
    if (r.data.kind !== $("blkKindFilter").value) $("blkKindFilter").value = r.data.kind;
    blkPage = 1;
    loadBlocked().catch(() => {});
  } else {
    toast(r.data.note || r.data.error || "فشل الحظر", true);
  }
});

$("blkRefresh").addEventListener("click", () => loadBlocked().catch(() => {}));
$("blkKindFilter").addEventListener("change", () => { blkPage = 1; loadBlocked().catch(() => {}); });
$("blkSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") { blkPage = 1; loadBlocked().catch(() => {}); } });
$("blkPrev").addEventListener("click", () => { if (blkPage > 1) { blkPage--; loadBlocked().catch(() => {}); } });
$("blkNext").addEventListener("click", () => { blkPage++; loadBlocked().catch(() => {}); });

$("blkTable").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-blkact]");
  if (!btn) return;
  const go = await confirmModal(`إلغاء الحظر عن ${btn.dataset.id}؟ ستعود معالجة رسائله خلال ≤30ث`, "إلغاء الحظر");
  if (!go) return;
  const r = await api("/admin/api/blocked/remove", {
    method: "POST",
    body: { kind: $("blkKindFilter").value, entity_id: Number(btn.dataset.id) },
  });
  toast(r.ok && r.data.ok ? "✅ أُلغي الحظر" : "❌ فشل", !(r.ok && r.data.ok));
  loadBlocked().catch(() => {});
});

$("blkBulkRemove").addEventListener("click", async () => {
  const ids = blkSelected();
  if (!ids.length) return;
  const go = await confirmModal(`إلغاء حظر ${ids.length} عنصراً؟ ستعود معالجة رسائلهم خلال ≤30ث`, "تنفيذ");
  if (!go) return;
  const r = await api("/admin/api/blocked/bulk", {
    method: "POST",
    body: { kind: $("blkKindFilter").value, ids },
  });
  toast(r.ok && r.data.ok ? `✅ أُلغي حظر ${r.data.removed}` : "❌ فشل", !(r.ok && r.data.ok));
  loadBlocked().catch(() => {});
});

/* ═════════ الوحدة 8: الأشخاص (v15.7.0) ═════════ */
let pplPage = 1;

tabLoaders.people = async function () { await loadPeople(); };

async function loadPeople() {
  const flag = $("pplFlag").value;
  const q = $("pplSearch").value.trim();
  const r = await api(`/admin/api/people?flag=${flag}&q=${encodeURIComponent(q)}&page=${pplPage}`);
  if (!r.ok) { toast("تعذر تحميل الأشخاص", true); return; }
  renderPeople(r.data);
}

function renderPeople(d) {
  const ov = d.overview || {};
  $("pplChips").innerHTML =
    `<span class="chip">إجمالي المرسلين: <b>${fmtNum(ov.total)}</b></span>` +
    `<span class="chip">بوتات: <b>${fmtNum(ov.bots)}</b></span>` +
    `<span class="chip">محظورون: <b>${fmtNum(ov.blocked)}</b></span>` +
    `<span class="chip">قائمة السماح: <b>${fmtNum(ov.allowed)}</b></span>`;
  const rows = (d.items || []).map((p) => {
    const badges = [];
    if (p.is_bot) badges.push('<span class="pill muted">بوت</span>');
    if (p.is_scam) badges.push('<span class="pill bad">scam</span>');
    if (p.is_blocked) badges.push('<span class="pill bad">محظور</span>');
    if (p.is_allowed) badges.push('<span class="pill ok">مسموح</span>');
    const who = p.username ? "@" + p.username : (p.name || "—");
    const acts = [];
    if (p.is_blocked) acts.push(`<button class="btn sm" data-pplact="unblock" data-id="${p.sender_id}">إلغاء الحظر</button>`);
    else acts.push(`<button class="btn sm danger" data-pplact="block" data-id="${p.sender_id}" data-name="${esc(who)}">حظر</button>`);
    if (p.is_allowed) acts.push(`<button class="btn sm" data-pplact="disallow" data-id="${p.sender_id}">إزالة السماح</button>`);
    else acts.push(`<button class="btn sm" data-pplact="allow" data-id="${p.sender_id}">سماح</button>`);
    return `<tr>
      <td><b>${esc(who)}</b><br><code>${esc(p.sender_id)}</code> ${badges.join(" ")}</td>
      <td>${fmtNum(p.total_messages)}</td>
      <td>${fmtNum(p.alerts_sent)}</td>
      <td>${esc(String(p.reputation))}</td>
      <td>${acts.join(" ")}</td>
    </tr>`;
  }).join("");
  $("pplTable").innerHTML = "<thead><tr><th>الشخص</th><th>رسائل</th><th>تنبيهات</th><th>السمعة</th><th>إجراءات</th></tr></thead><tbody>" +
    (rows || "<tr><td colspan='5'>لا بيانات — تظهر الأشخاص بعد أول رسالة تُلتقط</td></tr>") + "</tbody>";
  $("pplPageInfo").textContent = `صفحة ${d.page}`;
  $("pplPrev").disabled = d.page <= 1;
  $("pplNext").disabled = !d.has_more;
}

$("pplRefresh").addEventListener("click", () => loadPeople().catch(() => {}));
$("pplFlag").addEventListener("change", () => { pplPage = 1; loadPeople().catch(() => {}); });
$("pplSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") { pplPage = 1; loadPeople().catch(() => {}); } });
$("pplPrev").addEventListener("click", () => { if (pplPage > 1) { pplPage--; loadPeople().catch(() => {}); } });
$("pplNext").addEventListener("click", () => { pplPage++; loadPeople().catch(() => {}); });

$("pplTable").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-pplact]");
  if (!btn) return;
  const act = btn.dataset.pplact;
  const id = btn.dataset.id;
  const name = btn.dataset.name || id;
  let note = "";
  if (act === "block") {
    const typed = await promptModal(`سبب حظر ${name}؟ (اختياري)`, "السبب", "", "حظر");
    if (typed === null) return;
    note = typed;
    const go = await confirmModal(`حظر ${name} ومنع معالجة رسائله نهائياً؟`, "حظر");
    if (!go) return;
  } else if (act === "unblock") {
    const go = await confirmModal(`إلغاء الحظر عن ${id}؟`, "تنفيذ");
    if (!go) return;
  } else if (act === "allow") {
    const go = await confirmModal(`إضافة ${id} لقائمة السماح؟ رسائله تتجاوز الفلترة العادية (داخل مفتاح allowlist).`, "سماح");
    if (!go) return;
  } else {
    const go = await confirmModal(`إزالة ${id} من قائمة السماح؟`, "تنفيذ");
    if (!go) return;
  }
  const r = await api("/admin/api/people/action", { method: "POST", body: { action: act, sender_id: Number(id), note } });
  toast(r.ok && r.data.ok ? "✅ تم التنفيذ" : "❌ " + ((r.data || {}).error || "فشل"), !(r.ok && r.data.ok));
  loadPeople().catch(() => {});
});

/* ═════════ الوحدة 9: القواعد (v15.7.0) ═════════ */
let rlEditingId = null;
const RL_COND_LABELS = {
  keyword: "كلمة", source_chat_id: "المصدر", sender_id: "المرسل", min_confidence: "ثقة ≥",
};
const RL_ACTION_LABELS = { allow: "سماح", block: "حظر", tag: "وسم" };

tabLoaders.rules = async function () { await loadRules(); };

async function loadRules() {
  const r = await api("/admin/api/rules");
  if (!r.ok) { toast("تعذر تحميل القواعد", true); return; }
  renderRules(r.data);
}

function condChips(conds) {
  return Object.keys(conds || {}).map((k) =>
    `<span class="cond-chip">${esc(RL_COND_LABELS[k] || k)}: <b>${esc(String(conds[k]))}</b></span>`
  ).join(" ");
}

function renderRules(d) {
  $("rlEngineBadge").innerHTML =
    `<span class="chip">محرك القواعد: <b>${d.rule_engine_enabled ? "مفعّل" : "معطّل"}</b></span>` +
    `<span class="chip">قائمة السماح: <b>${d.allowlist_enabled ? "مفعّلة" : "معطّلة"}</b></span>` +
    `<span class="chip">الإجمالي: <b>${fmtNum(d.total)}</b></span>`;
  const rows = (d.items || []).map((r) => {
    const st = r.enabled ? '<span class="pill ok">مفعّلة</span>' : '<span class="pill warn">معطّلة</span>';
    const av = r.action === "tag" && r.action_value ? ` → <small>${esc(r.action_value)}</small>` : "";
    return `<tr>
      <td><b>${esc(r.name)}</b><br><small>${esc(r.created_by || "")} · أولوية ${esc(String(r.priority))} · تطابقات ${fmtNum(r.hits)}</small></td>
      <td>${condChips(r.conditions)}</td>
      <td><span class="pill muted">${esc(RL_ACTION_LABELS[r.action] || r.action)}</span>${av}</td>
      <td>${st}</td>
      <td>
        <button class="btn sm" data-rlact="edit" data-id="${r.id}">تعديل</button>
        <button class="btn sm" data-rlact="toggle" data-id="${r.id}" data-en="${r.enabled ? 1 : 0}">${r.enabled ? "تعطيل" : "تفعيل"}</button>
        <button class="btn sm danger" data-rlact="delete" data-id="${r.id}">حذف</button>
      </td>
    </tr>`;
  }).join("");
  $("rlTable").innerHTML = "<thead><tr><th>القاعدة</th><th>الشروط</th><th>الإجراء</th><th>الحالة</th><th>إجراءات</th></tr></thead><tbody>" +
    (rows || "<tr><td colspan='5'>لا قواعد — أضف أول قاعدة من الأعلى</td></tr>") + "</tbody>";
}

function rlFormReset() {
  rlEditingId = null;
  $("rlName").value = "";
  $("rlCondVal").value = "";
  $("rlActionValue").value = "";
  $("rlPriority").value = 100;
  $("rlSaveBtn").textContent = "إضافة قاعدة";
  $("rlCancelEdit").classList.add("hidden");
}

$("rlCancelEdit").addEventListener("click", rlFormReset);

$("rlSaveBtn").addEventListener("click", async () => {
  const name = $("rlName").value.trim();
  const key = $("rlCondKey").value;
  const rawVal = $("rlCondVal").value.trim();
  const action = $("rlAction").value;
  if (!name) { toast("أدخل اسم القاعدة", true); return; }
  if (!rawVal) { toast("أدخل قيمة الشرط", true); return; }
  let value = rawVal;
  if (key === "min_confidence") {
    const f = parseFloat(rawVal);
    if (isNaN(f) || f < 0 || f > 1) { toast("الثقة بين 0 و 1", true); return; }
    value = f;
  } else if (key === "source_chat_id" || key === "sender_id") {
    if (!/^-?\d+$/.test(rawVal)) { toast("المعرف يجب أن يكون رقماً", true); return; }
    value = Number(rawVal);
  }
  const payload = {
    name,
    conditions: { [key]: value },
    action,
    action_value: $("rlActionValue").value.trim(),
    priority: Number($("rlPriority").value) || 100,
  };
  if (rlEditingId) {
    const r = await api("/admin/api/rules/edit", { method: "POST", body: { rule_id: rlEditingId, payload } });
    if (r.ok && r.data.ok) { toast("✅ حُدّثت القاعدة — تطبيق خلال ≤30ث"); rlFormReset(); loadRules().catch(() => {}); }
    else toast("❌ " + (r.data.error || "فشل التحديث"), true);
  } else {
    const go = await confirmModal("إضافة هذه القاعدة لمسار المعالجة الفعلي؟", "إضافة");
    if (!go) return;
    const r = await api("/admin/api/rules/add", { method: "POST", body: payload });
    if (r.ok && r.data.ok) { toast("✅ أُضيفت القاعدة — تطبيق خلال ≤30ث"); rlFormReset(); loadRules().catch(() => {}); }
    else toast("❌ " + (r.data.error || "فشل الإضافة"), true);
  }
});

$("rlTable").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-rlact]");
  if (!btn) return;
  const act = btn.dataset.rlact, id = Number(btn.dataset.id);
  if (act === "delete") {
    const go = await confirmModal("حذف هذه القاعدة نهائياً؟", "حذف");
    if (!go) return;
    const r = await api("/admin/api/rules/delete", { method: "POST", body: { rule_id: id } });
    toast(r.ok && r.data.ok ? "✅ حُذفت" : "❌ فشل", !(r.ok && r.data.ok));
    loadRules().catch(() => {});
  } else if (act === "toggle") {
    const enable = btn.dataset.en !== "1";
    const r = await api("/admin/api/rules/toggle", { method: "POST", body: { rule_id: id, enabled: enable } });
    toast(r.ok && r.data.ok ? (enable ? "✅ فُعّلت" : "⏸️ عُطّلت") : "❌ فشل", !(r.ok && r.data.ok));
    loadRules().catch(() => {});
  } else if (act === "edit") {
    const r = await api("/admin/api/rules");
    if (!r.ok) return;
    const rule = (r.data.items || []).find((x) => x.id === id);
    if (!rule) { toast("القاعدة غير موجودة", true); return; }
    rlEditingId = id;
    $("rlName").value = rule.name === "—" ? "" : rule.name;
    const keys = Object.keys(rule.conditions || {});
    $("rlCondKey").value = keys[0] || "keyword";
    $("rlCondVal").value = keys.length ? String(rule.conditions[keys[0]]) : "";
    $("rlAction").value = rule.action;
    $("rlActionValue").value = rule.action_value || "";
    $("rlPriority").value = rule.priority;
    $("rlSaveBtn").textContent = "حفظ التعديل";
    $("rlCancelEdit").classList.remove("hidden");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
});

$("rlExport").addEventListener("click", async () => {
  const r = await api("/admin/api/rules/export");
  if (!r.ok) { toast("فشل التصدير", true); return; }
  downloadJSON(r.data, "br7-rules.json");
  toast("تم التصدير");
});
$("rlImportBtn").addEventListener("click", () => $("rlImportFile").click());
$("rlImportFile").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const go = await confirmModal("استيراد القواعد من الملف وإضافتها للمحرك؟", "استيراد");
    if (!go) return;
    const r = await api("/admin/api/rules/import", { method: "POST", body: { json_text: text } });
    if (r.ok && r.data.ok) { toast(`✅ استُوردت ${r.data.applied} (متجاوزة: ${r.data.skipped})`); loadRules().catch(() => {}); }
    else toast("فشل الاستيراد: " + (r.data.error || "?"), true);
  } catch (e) { toast("ملف غير صالح", true); }
  ev.target.value = "";
});
$("rlRefresh").addEventListener("click", () => loadRules().catch(() => {}));

/* ═════════ الوحدة 10: الميزات (v15.7.0) ═════════ */
tabLoaders.features = async function () { await loadFeatures(); };

async function loadFeatures() {
  const r = await api("/admin/api/features");
  if (!r.ok) { toast("تعذر تحميل الميزات", true); return; }
  $("featNote").textContent = r.data.note || "";
  $("featList").innerHTML = (r.data.items || []).map((f) => {
    const badges = f.overridden ? '<span class="pill muted">مخصص من اللوحة</span>' : '<span class="pill muted">افتراضي</span>';
    return `<div class="featrow">
      <div class="meta">
        <b>${esc(f.label)}</b>
        <small>${esc(f.impact)}</small>
        <div class="badges">${badges}${f.updated_by ? ` <small>آخر تعديل: ${esc(f.updated_by)} · ${esc(f.updated_at || "")}</small>` : ""}</div>
      </div>
      <label class="switch"><input type="checkbox" data-feat="${esc(f.key)}" ${f.enabled ? "checked" : ""}><span class="slider"></span></label>
    </div>`;
  }).join("");
}

$("featList").addEventListener("change", async (ev) => {
  const sw = ev.target.closest("input[data-feat]");
  if (!sw) return;
  const key = sw.dataset.feat;
  const enable = sw.checked;
  const go = await confirmModal(
    enable ? `تفعيل «${key}»؟ يُطبَّق على مسار المعالجة خلال ≤30ث دون إعادة تشغيل.`
           : `تعطيل «${key}»؟ راجع وصف التأثير أعلاه قبل المتابعة.`,
    enable ? "تفعيل" : "تعطيل");
  if (!go) { sw.checked = !enable; return; }
  const r = await api("/admin/api/features/toggle", { method: "POST", body: { key, enabled: enable } });
  if (r.ok && r.data.ok) toast(`✅ ${key}: ${enable ? "مفعّل" : "معطّل"} — يُطبَّق خلال ≤30ث`);
  else { toast("❌ " + ((r.data || {}).error || "فشل"), true); sw.checked = !enable; }
  loadFeatures().catch(() => {});
});
