import { Role } from './auth';

/**
 * RBAC matrix — documented source of truth:
 *   viewer:      قراءة كل ما هو قابل للقراءة (GET)
 *   operator:    + deploys / restart (Render) + redeploy preview (Vercel)
 *   admin:       + كتابة DB (الجداول البيضاء) + env vars (Render) + tag/rerun (GitHub)
 *   super_admin: + إدارة المستخدمين + عرض أسماء المفاتيح
 */
export const READ_MIN: Role = 'viewer';
export const DEPLOY_MIN: Role = 'operator';
export const WRITE_MIN: Role = 'admin';
export const ADMIN_MIN: Role = 'admin';
export const SUPER_MIN: Role = 'super_admin';
