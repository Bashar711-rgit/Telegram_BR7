import { NextRequest } from 'next/server';
import { withApi, parseSearch } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { getEntity } from '@/lib/entities';
import { requireService } from '@/lib/supabase';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/** GET /api/db/:table?limit=&offset=&order=&desc=1&filter=col:value */
export const GET = withApi(
  { name: 'db_get', minRole: 'viewer', rate: { limit: 120, windowMs: 60000 } },
  async ({ req, role, seg }) => {
    const { table } = await seg.params;
    const ent = getEntity(table);
    if (!ent) return err('unknown_table', 404);
    const p = parseSearch(req);
    const sb = requireService();
    let q = sb.from(ent.table).select(ent.selectCols.join(','));
    if (p.filter) {
      const idx = p.filter.indexOf(':');
      if (idx > 0) {
        const col = p.filter.slice(0, idx);
        const val = p.filter.slice(idx + 1);
        if (ent.selectCols.includes(col) && /^[A-Za-z0-9_.\-]+$/.test(col) && val.length <= 200) {
          q = q.eq(col, val);
        }
      }
    }
    const orderCol = p.order && ent.selectCols.includes(p.order) ? p.order : ent.orderDefault;
    q = q.order(orderCol, { ascending: !p.desc }).range(p.offset, p.offset + p.limit - 1);
    const { data, error } = await q;
    if (error) return err(error.message, 500);
    let rows = (data ?? []) as unknown as Array<Record<string, unknown>>;
    if (ent.adminOnlyCols && role !== 'admin' && role !== 'super_admin') {
      rows = rows.map((r) => {
        const c = { ...r };
        for (const col of ent.adminOnlyCols!) {
          if (c[col] !== null && c[col] !== undefined) c[col] = `•••••(len:${String(c[col]).length})`;
        }
        return c;
      });
    }
    return ok({ table: ent.table, pk: ent.pk, count: rows.length, rows });
  },
);

/** POST /api/db/:table — admin+ , Zod-strict, unknown fields rejected. */
export const POST = withApi(
  { name: 'db_post', minRole: 'admin', rate: { limit: 30, windowMs: 60000 } },
  async ({ req, email, body, seg, rid }) => {
    const { table } = await seg.params;
    const ent = getEntity(table);
    if (!ent) return err('unknown_table', 404);
    if (ent.readOnly) return err('read_only_table', 403);
    if (!ent.createSchema) return err('create_not_supported', 400);
    const parsed = ent.createSchema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);

    const row: Record<string, unknown> = { ...(parsed.data as Record<string, unknown>) };
    if (ent.selectCols.includes('created_by')) row.created_by = email;
    if (ent.selectCols.includes('added_by')) row.added_by = email;
    if (ent.selectCols.includes('blocked_by')) row.blocked_by = email;

    const sb = requireService();
    const { data, error } = await sb.from(ent.table).insert(row).select(ent.selectCols.join(',')).single();
    if (error) return err(error.message, 500);
    await logAudit({
      actor: email, action: `db_insert_${table}`, objectType: table,
      objectId: ((data as unknown as Record<string, unknown>)?.[ent.pk] ?? null) as string | number | null, after: data,
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '', requestId: rid,
    });
    return ok(data);
  },
);
