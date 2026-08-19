/**
 * Validation API service — wraps /api/validation/* (Phase 7 of the
 * data-integration build). A rule set's real aggregate SQL runs against
 * the live object via backend/app/validation/engine.py — one query
 * covers every enabled rule. Nothing here is mocked.
 */
import apiClient from './apiClient.js';

export const RULE_TYPES = {
  NOT_NULL:       { label: 'Not Null',              icon: '⛔', desc: 'Column must not be null.' },
  RANGE:          { label: 'Range Check',           icon: '📊', desc: 'Numeric value within min/max.' },
  FORMAT:         { label: 'Format / Regex',        icon: '🔍', desc: 'Value matches a regex pattern. Postgres/MySQL only — no portable regex on SQL Server.' },
  UNIQUE:         { label: 'Uniqueness',            icon: '⊟', desc: 'No duplicate values across one or more columns.' },
  REF_INTEGRITY:  { label: 'Referential Integrity', icon: '🔗', desc: 'Value must exist in a reference table on the SAME connection.' },
  SQL_CONDITION:  { label: 'SQL Condition',         icon: '💾', desc: 'A boolean SQL expression every valid row must satisfy.' },
};

export const validationApi = {
  listRuleSets: () => apiClient.get('/validation/rulesets').then((r) => r.data),

  getRuleSet: (id) => apiClient.get(`/validation/rulesets/${id}`).then((r) => r.data),

  /** body: { name, connection_id, schema_name, object_name, rules } */
  createRuleSet: (body) => apiClient.post('/validation/rulesets', body).then((r) => r.data),

  /** body: { name?, rules? } */
  updateRuleSet: (id, body) => apiClient.put(`/validation/rulesets/${id}`, body).then((r) => r.data),

  deleteRuleSet: (id) => apiClient.delete(`/validation/rulesets/${id}`),

  runRuleSet: (id) => apiClient.post(`/validation/rulesets/${id}/run`).then((r) => r.data),

  listRuns: (ruleSetId) => apiClient.get(`/validation/rulesets/${ruleSetId}/runs`).then((r) => r.data),

  getRun: (runId) => apiClient.get(`/validation/runs/${runId}`).then((r) => r.data),

  getFailedRecords: (runId, ruleId, limit = 50) =>
    apiClient.get(`/validation/runs/${runId}/rules/${ruleId}/failed-records`, { params: { limit } }).then((r) => r.data),
};
