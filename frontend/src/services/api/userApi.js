/**
 * User management API.
 * Real: replace mock bodies with apiClient calls to POST/GET /api/users/…
 */
import { ROLES, MODULES } from '@/utils/constants.js';

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

const MOCK_USERS = [
  { id: 'u1', email: 'admin@acme.com',    full_name: 'Alex Johnson',  role: ROLES.ORG_ADMIN,     is_active: true,  organization_id: 'org_1', created_at: '2024-01-15T10:00:00Z', last_login: new Date(Date.now() - 3_600_000).toISOString(),   accessible_modules: null },
  { id: 'u2', email: 'sarah@acme.com',    full_name: 'Sarah Chen',    role: ROLES.DATA_ENGINEER, is_active: true,  organization_id: 'org_1', created_at: '2024-02-01T10:00:00Z', last_login: new Date(Date.now() - 86_400_000).toISOString(),  accessible_modules: [MODULES.DOCUMENT_INTELLIGENCE, MODULES.OCR, MODULES.AI_EXTRACTION, MODULES.TRANSFORMATION] },
  { id: 'u3', email: 'mike@acme.com',     full_name: 'Mike Davis',    role: ROLES.ANALYST,       is_active: true,  organization_id: 'org_1', created_at: '2024-03-01T10:00:00Z', last_login: new Date(Date.now() - 172_800_000).toISOString(), accessible_modules: [MODULES.TRANSFORMATION, MODULES.VALIDATION] },
  { id: 'u4', email: 'jane@acme.com',     full_name: 'Jane Wilson',   role: ROLES.VIEWER,        is_active: true,  organization_id: 'org_1', created_at: '2024-04-01T10:00:00Z', last_login: null,                                              accessible_modules: [MODULES.TRANSFORMATION] },
  { id: 'u5', email: 'inactive@acme.com', full_name: 'Tom Brown',     role: ROLES.DATA_ENGINEER, is_active: false, organization_id: 'org_1', created_at: '2024-01-20T10:00:00Z', last_login: '2024-06-01T10:00:00Z',                            accessible_modules: null },
];

let _users = MOCK_USERS.map((u) => ({ ...u }));

export const userApi = {
  async list(_orgId, { page = 1, pageSize = 20 } = {}) {
    await delay(400);
    return { items: _users, total: _users.length, page, pageSize };
  },

  async get(id) {
    await delay(200);
    return _users.find((u) => u.id === id) ?? null;
  },

  async create(data) {
    await delay(400);
    const user = { id: `u_${Date.now()}`, is_active: true, created_at: new Date().toISOString(), last_login: null, ...data };
    _users = [..._users, user];
    return user;
  },

  async update(id, patch) {
    await delay(300);
    _users = _users.map((u) => u.id === id ? { ...u, ...patch } : u);
    return _users.find((u) => u.id === id);
  },

  async deactivate(id) {
    await delay(300);
    return userApi.update(id, { is_active: false });
  },

  async resetAccess(id) {
    await delay(300);
    return userApi.update(id, { accessible_modules: null });
  },

  async updateModules(id, modules) {
    await delay(300);
    return userApi.update(id, { accessible_modules: modules });
  },
};
