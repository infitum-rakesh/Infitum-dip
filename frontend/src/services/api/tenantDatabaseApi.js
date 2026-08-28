/**
 * Bring-Your-Own-Database (Phase 1) — wraps /api/settings/database (org)
 * and /api/seller/settings/database (Seller/platform-level), both backed
 * by the same services/tenant_database_service.py on the backend (see its
 * docstring for the organization_id-is-None-means-platform-wide
 * convention). One factory here, two instances below, rather than two
 * near-identical hand-written clients.
 *
 * get() resolves to null when nothing has been configured yet — the
 * frontend renders the "not configured" empty state rather than an error.
 * update()/test() throw on failure with a real backend-sourced message
 * (e.g. a genuine driver connection error), never a simulated result.
 */
import apiClient from './apiClient.js';

function makeTenantDatabaseApi(basePath) {
  return {
    getTypes: () => apiClient.get(`${basePath}/types`).then((r) => r.data),
    get: () => apiClient.get(basePath).then((r) => r.data),
    /** body: { connection_type: 'POSTGRESQL'|'MYSQL'|'SQL_SERVER', config: {...} } */
    update: (body) => apiClient.put(basePath, body).then((r) => r.data),
    test: () => apiClient.post(`${basePath}/test`).then((r) => r.data),
  };
}

export const tenantDatabaseApi = makeTenantDatabaseApi('/settings/database');
export const sellerTenantDatabaseApi = makeTenantDatabaseApi('/seller/settings/database');
