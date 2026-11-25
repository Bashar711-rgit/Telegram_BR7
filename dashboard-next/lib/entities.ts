import { z } from 'zod';

/**
 * Entity registry for the generic DB CRUD layer.
 * selectCols is a COLUMN-LEVEL allowlist — secrets never leave the server
 * (e.g. panel_accounts.api_hash_encrypted / session_encrypted are not selectable).
 */
export type EntityDef = {
  key: string;
  table: string;
  pk: string;
  label: string;
  readOnly: boolean;
  selectCols: string[];
  orderDefault: string;
  createSchema?: z.ZodTypeAny;
  updateSchema?: z.ZodTypeAny;
  adminOnlyCols?: string[]; // masked for non-admin readers
  createDisabled?: boolean;
};

const flag = z.union([z.literal(0), z.literal(1)]);
const intId = z.coerce.number().int().min(0);

export const WRITABLE_ENTITIES: Record<string, EntityDef> = {
  rules: {
    key: 'rules',
    table: 'rules',
    pk: 'id',
    label: 'القواعد',
    readOnly: false,
    selectCols: ['id', 'name', 'conditions', 'action', 'action_value', 'priority', 'hits', 'enabled', 'created_by', 'created_at'],
    orderDefault: 'id',
    createSchema: z
      .object({
        name: z.string().min(1).max(120),
        conditions: z.string().min(2).max(4000),
        action: z.string().min(1).max(40),
        action_value: z.string().max(2000).nullable().optional(),
        priority: z.number().int().min(0).max(10000).optional(),
        enabled: flag.optional(),
      })
      .strict(),
    updateSchema: z
      .object({
        name: z.string().min(1).max(120).optional(),
        conditions: z.string().min(2).max(4000).optional(),
        action: z.string().min(1).max(40).optional(),
        action_value: z.string().max(2000).nullable().optional(),
        priority: z.number().int().min(0).max(10000).optional(),
        enabled: flag.optional(),
      })
      .strict(),
  },
  sources: {
    key: 'sources',
    table: 'sources',
    pk: 'id',
    label: 'المصادر',
    readOnly: false,
    selectCols: ['id', 'chat_id', 'username', 'title', 'type', 'enabled', 'added_by', 'notes', 'created_at', 'chat_type', 'member_count', 'last_seen_at'],
    orderDefault: 'id',
    createSchema: z
      .object({
        chat_id: intId,
        username: z.string().max(64).nullable().optional(),
        title: z.string().max(120).nullable().optional(),
        type: z.string().min(1).max(20),
        enabled: flag.optional(),
        notes: z.string().max(500).nullable().optional(),
        chat_type: z.string().max(20).nullable().optional(),
      })
      .strict(),
    updateSchema: z
      .object({
        username: z.string().max(64).nullable().optional(),
        title: z.string().max(120).nullable().optional(),
        type: z.string().min(1).max(20).optional(),
        enabled: flag.optional(),
        notes: z.string().max(500).nullable().optional(),
      })
      .strict(),
  },
  blocked_senders: {
    key: 'blocked_senders',
    table: 'blocked_senders',
    pk: 'sender_id',
    label: 'المحظورون',
    readOnly: false,
    selectCols: ['sender_id', 'reason', 'blocked_by', 'blocked_at'],
    orderDefault: 'blocked_at',
    createSchema: z
      .object({ sender_id: intId, reason: z.string().min(1).max(300) })
      .strict(),
    updateSchema: z.object({ reason: z.string().min(1).max(300) }).strict(),
  },
  allowed_entities: {
    key: 'allowed_entities',
    table: 'allowed_entities',
    pk: 'id',
    label: 'المسموحون',
    readOnly: false,
    selectCols: ['id', 'entity_type', 'entity_id', 'note', 'created_at'],
    orderDefault: 'id',
    createSchema: z
      .object({
        entity_type: z.string().min(1).max(30),
        entity_id: intId,
        note: z.string().max(500).nullable().optional(),
      })
      .strict(),
    updateSchema: z.object({ note: z.string().max(500).nullable().optional() }).strict(),
  },
  app_settings: {
    key: 'app_settings',
    table: 'app_settings',
    pk: 'key',
    label: 'الإعدادات (KV)',
    readOnly: false,
    selectCols: ['key', 'value', 'updated_at'],
    orderDefault: 'key',
    createSchema: z
      .object({ key: z.string().min(1).max(100).regex(/^[\w:.\-]+$/), value: z.string().max(100000) })
      .strict(),
    updateSchema: z.object({ value: z.string().max(100000) }).strict(),
    adminOnlyCols: ['value'],
  },
  panel_accounts: {
    key: 'panel_accounts',
    table: 'panel_accounts',
    pk: 'id',
    label: 'حسابات التشغيل',
    readOnly: false,
    selectCols: ['id', 'phone', 'display_name', 'username', 'tg_user_id', 'status', 'monitoring_enabled', 'priority', 'is_main', 'last_error', 'last_error_at', 'created_at', 'updated_at'],
    orderDefault: 'id',
    createDisabled: true,
    updateSchema: z
      .object({
        display_name: z.string().max(64).optional(),
        status: z.string().max(30).optional(),
        monitoring_enabled: flag.optional(),
        priority: z.number().int().min(0).max(100).optional(),
        is_main: flag.optional(),
      })
      .strict(),
  },
};

export const READ_ONLY_ENTITIES: Record<string, EntityDef> = {
  messages: {
    key: 'messages', table: 'messages', pk: 'id', label: 'الرسائل', readOnly: true,
    selectCols: ['id', 'message_hash', 'chat_id', 'sender_id', 'message_text', 'keyword_found', 'score', 'spam_score', 'timestamp', 'created_at'],
    orderDefault: 'id',
  },
  alerts: {
    key: 'alerts', table: 'alerts', pk: 'id', label: 'التنبيهات', readOnly: true,
    selectCols: ['id', 'message_hash', 'chat_id', 'sender_id', 'account_name', 'keyword', 'alert_text', 'timestamp', 'created_at', 'decision', 'confidence', 'intent_verb', 'academic_object', 'alert_status', 'button_message_url'],
    orderDefault: 'id',
  },
  filter_decisions: {
    key: 'filter_decisions', table: 'filter_decisions', pk: 'id', label: 'قرارات الفلترة', readOnly: true,
    selectCols: ['id', 'msg_hash', 'chat_id', 'sender_id', 'sender_name', 'account_name', 'decision', 'confidence', 'score', 'keyword', 'intent_verb', 'academic_object', 'urgent', 'reasons', 'created_at'],
    orderDefault: 'id',
  },
  audit_logs: {
    key: 'audit_logs', table: 'audit_logs', pk: 'id', label: 'سجل التدقيق', readOnly: true,
    selectCols: ['id', 'actor', 'action', 'object_type', 'object_id', 'old_value', 'new_value', 'source', 'created_at', 'ip', 'user_agent', 'request_id'],
    orderDefault: 'id',
  },
  // v17.1.0: كيانات القراءة الجديدة (service_role يتجاوز RLS — قراءة فقط، بلا كتابة من اللوحة)
  sender_stats: {
    key: 'sender_stats', table: 'sender_stats', pk: 'sender_id', label: 'إحصائيات المرسلين', readOnly: true,
    selectCols: ['sender_id', 'messages_count', 'alerts_count', 'first_seen', 'last_seen', 'updated_at'],
    orderDefault: 'sender_id',
  },
  blocked_chats: {
    key: 'blocked_chats', table: 'blocked_chats', pk: 'id', label: 'المحادثات المحظورة', readOnly: true,
    selectCols: ['id', 'chat_id', 'reason', 'blocked_by', 'blocked_at'],
    orderDefault: 'id',
  },
  notifications: {
    key: 'notifications', table: 'notifications', pk: 'id', label: 'الإشعارات', readOnly: true,
    selectCols: ['id', 'type', 'title', 'body', 'status', 'created_at', 'sent_at'],
    orderDefault: 'id',
  },
};

export const ALL_ENTITIES: Record<string, EntityDef> = { ...WRITABLE_ENTITIES, ...READ_ONLY_ENTITIES };

export function getEntity(key: string): EntityDef | null {
  return ALL_ENTITIES[key] ?? null;
}
