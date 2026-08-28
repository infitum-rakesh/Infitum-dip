/**
 * Bring-Your-Own-Database Phase 4/5/6 — migration/cutover jobs, wrapping
 * /api/settings/database/migrate|migrations (org-scoped) and
 * /api/seller/settings/database/migrate|migrations (Seller/platform-level
 * — Phase 6), both backed by the same services/tenant_migration_service.py
 * on the backend. One factory here, two instances below, mirroring
 * tenantDatabaseApi.js's org/seller split. Same get/getLogs polling shape
 * as pipelineJobApi, since TenantMigrationLogDrawer.jsx mirrors
 * JobLogDrawer.jsx.
 */
import apiClient from './apiClient.js';

function makeTenantMigrationApi(basePath) {
  return {
    /** Kicks off a real migration to the configured+tested backend
     * database. Throws with a real validation message (not configured,
     * not tested, already active, unsupported dialect, already running)
     * on failure. */
    start: () => apiClient.post(`${basePath}/migrate`).then((r) => r.data),

    /** Migration history, newest first. */
    list: () => apiClient.get(`${basePath}/migrations`).then((r) => r.data),

    get: (jobId) => apiClient.get(`${basePath}/migrations/${jobId}`).then((r) => r.data),

    /** Pass sinceId to tail only new lines while a migration is running. */
    getLogs: (jobId, sinceId) =>
      apiClient.get(`${basePath}/migrations/${jobId}/logs`, { params: sinceId ? { since_id: sinceId } : {} }).then((r) => r.data),
  };
}

export const tenantMigrationApi = makeTenantMigrationApi('/settings/database');
export const sellerTenantMigrationApi = makeTenantMigrationApi('/seller/settings/database');
