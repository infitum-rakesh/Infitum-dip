/**
 * Settings API service — wraps /api/settings.
 * Components must never call apiClient directly; go through this module.
 * update() sends only the section(s) actually changed — the backend does a
 * partial per-section merge under whatever's already stored, so a save from
 * one tab never touches any other tab's values.
 */
import apiClient from './apiClient.js';

export const settingsApi = {
  /** Full organization settings: General/Data/Document Intelligence/AI/Notifications/Security/Audit/Pipelines. */
  get: () => apiClient.get('/settings').then((r) => r.data),

  /** Partial update — pass only the top-level keys that changed, e.g. { ai: { temperature: 0.5 } }. */
  update: (patch) => apiClient.put('/settings', patch).then((r) => r.data),
};
