'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Modal, Gate, Input, Badge } from '@/components/ui';
import { OkText, ErrorText, ModeBadge, ContractNote } from '@/components/bot';

type Group = { key: string; kind: 'word_list' | 'mapping'; size: number; matchedCount?: number; words?: string[] };
type KwData = {
  stats: { totalWords: number; groupCount: number; duplicates: Array<{ group: string; word: string }> };
  groups: Group[];
};

export default function BotKeywords() {
  const { me } = useMe();
  const canEdit = me && ['operator', 'admin', 'super_admin'].includes(me.role);
  const [search, setSearch] = React.useState('');
  const { data, error, reload, loading } = useApi<KwData>(
    search.length >= 2 ? `/api/bot/keywords?search=${encodeURIComponent(search)}` : '/api/bot/keywords',
    45000,
  );
  const [active, setActive] = React.useState<string | null>(null);
  const [addOpen, setAddOpen] = React.useState(false);
  const [addCat, setAddCat] = React.useState('');
  const [wordsText, setWordsText] = React.useState('');
  const [confirmWord, setConfirmWord] = React.useState<{ group: string; word: string } | null>(null);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [showDups, setShowDups] = React.useState(false);

  const wordGroups = (data?.groups || []).filter((g) => g.kind === 'word_list');
  const mapGroups = (data?.groups || []).filter((g) => g.kind === 'mapping');
  const current = wordGroups.find((g) => g.key === active) || wordGroups[0];

  React.useEffect(() => {
    if (wordGroups.length && !wordGroups.find((g) => g.key === active)) setActive(wordGroups[0].key);
  }, [wordGroups, active]);

  const submitAdd = async () => {
    setBusy(true);
    setOkMsg(null);
    setErrMsg(null);
    const words = wordsText.split('\n').map((w) => w.trim()).filter(Boolean).slice(0, 200);
    const r = await fetch('/api/bot/keywords', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category: addCat, words }),
    });
    const j = (await r.json()) as { ok: boolean; data?: { added: number; results: Array<{ word: string; ok: boolean; error?: string }> }; error?: string };
    setBusy(false);
    if (j.ok && j.data) {
      const failed = j.data.results.filter((x) => !x.ok);
      setOkMsg(`أُضيف ${j.data.added}/${words.length} كلمة فعلياً في «${addCat}»${failed.length ? ` — مرفوضة: ${failed.map((f) => `${f.word} (${f.error})`).slice(0, 5).join('، ')}` : ''}`);
      setAddOpen(false);
      setWordsText('');
      reload();
    } else setErrMsg(`فشل: ${j.error || r.status}`);
  };

  const removeWord = async () => {
    if (!confirmWord) return;
    setBusy(true);
    const r = await fetch('/api/bot/keywords', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category: confirmWord.group, word: confirmWord.word }),
    });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`حُذفت «${confirmWord.word}» فعلياً من ${confirmWord.group}`); setConfirmWord(null); reload(); }
    else setErrMsg(`فشل الحذف: ${j.error}`);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">إدارة الكلمات المفتاحية</h1>
        <div className="flex gap-2">
          <Gate role={me?.role} min="operator">
            <Btn onClick={() => { setAddCat(current?.key || 'custom:جدید'); setAddOpen(true); }}>إضافة كلمات</Btn>
          </Gate>
          <Btn variant="ghost" onClick={() => reload()}>تحديث</Btn>
        </div>
      </div>
      {error && <div className="text-sm text-panel-bad">خطأ: {error}</div>}
      <OkText>{okMsg}</OkText>
      <ErrorText>{errMsg}</ErrorText>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-lg border border-panel-border bg-panel-bg p-3"><div className="text-xs text-panel-muted">إجمالي الكلمات</div><div className="text-lg font-bold">{data?.stats.totalWords ?? '—'}</div></div>
        <div className="rounded-lg border border-panel-border bg-panel-bg p-3"><div className="text-xs text-panel-muted">المجموعات</div><div className="text-lg font-bold">{data?.stats.groupCount ?? '—'}</div></div>
        <button onClick={() => setShowDups((v) => !v)} className="rounded-lg border border-panel-border bg-panel-bg p-3 text-right">
          <div className="text-xs text-panel-muted">تكرارات (مطبّع)</div>
          <div className={`text-lg font-bold ${(data?.stats.duplicates.length || 0) > 0 ? 'text-panel-warn' : ''}`}>{data?.stats.duplicates.length ?? '—'}</div>
        </button>
        <div className="rounded-lg border border-panel-border bg-panel-bg p-3"><div className="text-xs text-panel-muted">البحث</div><Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="حرفان على الأقل" className="mt-1" /></div>
      </div>

      {showDups && data && data.stats.duplicates.length > 0 && (
        <Card title="كشف التكرار (تطبيع الهمزات/الألف/الياء/التشكيل)">
          <div className="flex flex-wrap gap-2 text-xs">
            {data.stats.duplicates.slice(0, 30).map((d, i) => (
              <Badge key={i} tone="warn">{d.group}: {d.word}</Badge>
            ))}
          </div>
          <div className="mt-2 text-[11px] text-panel-muted">التكرار كشف حقيقي على السجل الفعلي — الدمج يتم بحذف الكلمة المكررة يدوياً (حقيقي)</div>
        </Card>
      )}

      <Card title="مجموعات الكلمات (سجل المحرك الحقيقي — dashboard:keywords_json)">
        <div className="flex flex-wrap gap-1 pb-2">
          {wordGroups.map((g) => (
            <button
              key={g.key}
              onClick={() => setActive(g.key)}
              className={`rounded-lg px-3 py-1.5 text-xs ${active === g.key ? 'bg-panel-accent/15 font-bold text-panel-accent' : 'text-panel-muted hover:text-panel-text'}`}
            >
              {g.key} <span className="text-[10px]">({g.size})</span>
            </button>
          ))}
        </div>
        {current && (
          <>
            <div className="mb-2 text-xs text-panel-muted">
              {current.key}: {current.size} كلمة {search && current.matchedCount !== undefined ? `— مطابق للبحث: ${current.matchedCount}` : ''}
            </div>
            <div className="max-h-80 overflow-y-auto rounded-lg border border-panel-border/40 bg-panel-bg p-2" style={{ scrollbarWidth: 'thin' }}>
              <div className="flex flex-wrap gap-1.5">
                {(current.words || []).map((w) => (
                  <span key={w} className="inline-flex items-center gap-1 rounded-md border border-panel-border bg-panel-surface px-2 py-1 text-xs">
                    {w}
                    <Gate role={me?.role} min="operator">
                      <button onClick={() => setConfirmWord({ group: current.key, word: w })} className="text-panel-bad hover:opacity-70" aria-label={`حذف ${w}`}>✕</button>
                    </Gate>
                  </span>
                ))}
                {search && (current.words || []).length === 0 && <span className="text-xs text-panel-muted">لا مطابقات في هذه المجموعة</span>}
              </div>
            </div>
          </>
        )}
      </Card>

      {mapGroups.length > 0 && (
        <Card title="مجموعات الخرائط (لهجات/سياقات — قراءة)">
          <div className="flex flex-wrap gap-2 text-xs">
            {mapGroups.map((g) => <Badge key={g.key} tone="muted">{g.key} ({g.size})</Badge>)}
          </div>
          <div className="mt-2 text-[11px] text-panel-muted">خريطة اللهجات والعبارات غير المباشرة — تُقرأ من السجل الحي؛ تعديلها بنفس آلية المجموعات متاح عبر custom:* حالياً (تعديل مجموعات المحرك نفسها يجري عبر نفس المسار الفعلي)</div>
        </Card>
      )}

      <Card title="القدرات">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <ModeBadge mode="live" /> قراءة/بحث/تكرار/إحصاء — حي
          <ModeBadge mode="live_persistent" /> إضافة/حذف كلمات — كتابة حقيقية مثابرة
          <ModeBadge mode="contract" /> البناء الحار على العملية الجارية — عقد §3.2
        </div>
        <ContractNote>
          الكتابة تنفذ فعلاً في سجل app_settings وتُدقق التكرار (تطبيع عربي آمن: همزات/ألف/ياء/تشكيل دون إفساد المعنى). المحرك يبني من هذا السجل عند الإقلاع (hydrate_dashboard → reload_keywords → FastGate) — البناء الفوري الحار يتطلب منفذ Render (عقد §3.2) وتظهر هذه الملاحظة مع كل عملية كتابة.
        </ContractNote>
      </Card>

      <Modal open={addOpen} title={`إضافة كلمات إلى «${addCat}»`} onClose={() => setAddOpen(false)}>
        <div className="space-y-2">
          <Input value={addCat} onChange={(e) => setAddCat(e.target.value)} placeholder="المجموعة (مجموعات المحرك موجودة، أو استخدم custom:اسم-جديد)" maxLength={60} dir="ltr" />
          <textarea
            rows={6}
            value={wordsText}
            onChange={(e) => setWordsText(e.target.value)}
            placeholder="كلمة في كل سطر — حتى 200 كلمة في العملية"
            className="w-full rounded-lg border border-panel-border bg-panel-bg px-3 py-2 text-sm text-panel-text focus:border-panel-accent focus:outline-none"
          />
          <div className="text-[11px] text-panel-muted">سيُرفض المكرر (حرفي أو بعد التطبيع العربي) بصدق مع سبب الرفض لكل كلمة.</div>
          <div className="flex justify-end gap-2 pt-1">
            <Btn variant="ghost" onClick={() => setAddOpen(false)}>إلغاء</Btn>
            <Btn disabled={busy || !wordsText.trim() || !addCat} onClick={submitAdd}>{busy ? 'جارٍ…' : 'إضافة فعلياً'}</Btn>
          </div>
        </div>
      </Modal>

      <Modal open={confirmWord !== null} title="حذف كلمة" onClose={() => setConfirmWord(null)}>
        <p className="text-sm">حذف <b>{confirmWord?.word}</b> من «{confirmWord?.group}» — حقيقي ومثابر ومسجل بالتدقيق.</p>
        <div className="mt-3 flex justify-end gap-2">
          <Btn variant="ghost" onClick={() => setConfirmWord(null)}>إلغاء</Btn>
          <Btn variant="danger" disabled={busy} onClick={removeWord}>حذف فعلي</Btn>
        </div>
      </Modal>
    </div>
  );
}
