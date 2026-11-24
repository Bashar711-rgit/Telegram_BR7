import { NextRequest } from 'next/server';
import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { getEntity } from '@/lib/entities';
import { requireService } from '@/lib/supabase';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

function idFor(entPk: string, raw: string): string | number {
  if (entPk === 'key') return raw;
  return /^\d+$/.test(raw) ? Number(raw) : raw;
}

/** PATCH /api/db/:table/:id — admin+, partial strict schema, audit before/after (masked). */
export const PATCH = withApi(
  { name: 'db_patch', minRole: 'admin', rate: { limit: 30, windowMs: 60000 } },
  async ({ req, email, body, seg, rid }) => {
    const { table, id } = await seg.params;
    const ent = getEntity(table);
    if (!ent) return err('unknown_table', 404);
    if (ent.readOnly) return err('read_only_table', 403);
    if (!ent.updateSchema) return err('update_not_supported', 400);
    const parsed = ent.updateSchema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);

    const sb = requireService();
    const pkVal = idFor(ent.pk, id);
    const { data: before, error: e1 } = await sb.from(ent.table).select(ent.selectCols.join(',')).eq(ent.pk, pkVal).maybeSingle();
    if (e1) return err(e1.message, 500);
    if (!before) return err('not_found', 404);

    const { data, error } = await sb
      .from(ent.table)
      .update(parsed.data as Record<string, unknown>)
      .eq(ent.pk, pkVal)
      .select(ent.selectCols.join(','))
      .single();
    if (error) return err(error.message, 500);
    await logAudit({
      actor: email, action: `db_update_${table}`, objectType: table, objectId: id,
      before, after: data,
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '', requestId: rid,
    });
    return ok(data);
  },
);

/** DELETE /api/db/:table/:id — admin+, audit with before snapshot. */
export const DELETE = withApi(
  { name: 'db_delete', minRole: 'admin', rate: { limit: 30, windowMs: 60000 } },
  async ({ req, email, seg, rid }) => {
    const { table, id } = await seg.params;
    const ent = getEntity(table);
    if (!ent) return err('unknown_table', 404);
    if (ent.readOnly) return err('read_only_table', 403);

    const sb = requireService();
    const pkVal = idFor(ent.pk, id);
    const { data: before, error: e1 } = await sb.from(ent.table).select(ent.selectCols.join(',')).eq(ent.pk, pkVal).maybeSingle();
    if (e1) return err(e1.message, 500);
    if (!before) return err('not_found', 404);

    const { error } = await sb.from(ent.table).delete().eq(ent.pk, pkVal);
    if (error) return err(error.message, 500);
    await logAudit({
      actor: email, action: `db_delete_${table}`, objectType: table, objectId: id,
      before, after: { deleted: true },
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '', requestId: rid,
    });
    return ok({ deleted: true, pk: ent.pk, id });
  },
);
