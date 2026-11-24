import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { createTag } from '@/lib/github';
import { logAudit } from '@/lib/audit';
import { z } from 'zod';

export const dynamic = 'force-dynamic';

const Body = z.object({ tag: z.string().min(1).max(80).regex(/^[A-Za-z0-9._\-\/]+$/) }).strict();

/** POST /api/github/tag — admin+. Creates a lightweight tag on current main HEAD. */
export const POST = withApi(
  { name: 'github_tag', minRole: 'admin', rate: { limit: 10, windowMs: 300000 } },
  async ({ email, body, rid }) => {
    const parsed = Body.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const { tag } = parsed.data;
    const r = await createTag(tag);
    if (r.status === 201) {
      await logAudit({
        actor: email, action: 'github_tag_created', objectType: 'github_tag', objectId: tag,
        after: { ref: `refs/tags/${tag}` }, requestId: rid,
      });
      return ok({ tag, sha: (r.data as Record<string, unknown>)?.object });
    }
    if (r.status === 422) return err('tag_exists', 409);
    return err(r.error || `github_http_${r.status}`, 500);
  },
);
