import { withApi, parseSearch } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { requireService } from '@/lib/supabase';

export const dynamic = 'force-dynamic';

/** GET /api/audit?limit=&offset=&search=&actor=&format=csv */
export const GET = withApi(
  { name: 'audit_get', minRole: 'viewer', rate: { limit: 120, windowMs: 60000 } },
  async ({ req }) => {
    const p = parseSearch(req);
    const sb = requireService();
    let q = sb.from('audit_logs').select('id,actor,action,object_type,object_id,old_value,new_value,source,created_at,ip,user_agent,request_id');
    if (p.search) {
      const s = p.search.replace(/[(),*%]/g, '').trim();
      if (s) q = q.or(`action.ilike.%${s}%,actor.ilike.%${s}%,object_type.ilike.%${s}%`);
    }
    const actor = req.nextUrl.searchParams.get('actor');
    if (actor && /^[^()%,*]{1,120}$/.test(actor)) q = q.eq('actor', actor);
    q = q.order('id', { ascending: false }).range(p.offset, p.offset + p.limit - 1);
    const { data, error } = await q;
    if (error) return err(error.message, 500);
    const rows = (data ?? []) as Array<Record<string, unknown>>;

    if (p.format === 'csv') {
      const cols = ['id', 'created_at', 'actor', 'action', 'object_type', 'object_id', 'old_value', 'new_value', 'source', 'ip', 'request_id'];
      const esc = (v: unknown) => {
        const s = v === null || v === undefined ? '' : String(v);
        return '"' + s.replace(/"/g, '""').replace(/\r?\n/g, ' ') + '"';
      };
      const lines = [cols.join(',')].concat(rows.map((r) => cols.map((c) => esc(r[c])).join(',')));
      return new Response('\ufeff' + lines.join('\n'), {
        status: 200,
        headers: { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': 'attachment; filename="br7_audit.csv"', 'Cache-Control': 'no-store' },
      });
    }
    return ok({ count: rows.length, rows });
  },
);
