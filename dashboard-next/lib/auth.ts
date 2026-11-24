export const ROLES = ['viewer', 'operator', 'admin', 'super_admin'] as const;
export type Role = (typeof ROLES)[number];

export const ROLE_RANK: Record<Role, number> = {
  viewer: 0,
  operator: 1,
  admin: 2,
  super_admin: 3,
};

export function rankOf(role: string): number {
  return ROLE_RANK[role as Role] ?? -1;
}

export function hasMin(role: string, min: Role): boolean {
  return rankOf(role) >= ROLE_RANK[min];
}
