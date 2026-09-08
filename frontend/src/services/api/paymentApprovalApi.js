/**
 * Payment Approvals API service — wraps /api/approvals/* (PAY2-PAY9).
 * Every call hits a real, live-verified backend route: the rule engine
 * (evaluate/resolve/conflict-detect), the workflow engine (SEQUENTIAL/
 * PARALLEL advance, REJECT short-circuit, peer review), and the
 * decision-pipeline integration that auto-creates a workflow whenever a
 * matching rule claims a STRAIGHT_THROUGH invoice. Nothing here is
 * mocked.
 */
import apiClient from './apiClient.js';

function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const APPROVAL_RULE_STATUSES = ['ACTIVE', 'INACTIVE'];
// PAY22 — AUTO_APPROVED added: create_workflow() now persists a terminal,
// zero-step workflow row for every invoice reaching the decision engine,
// even ones no ApprovalRule claimed, so the Workflows list/filter can show
// them alongside the ones that actually needed a human.
export const APPROVAL_WORKFLOW_STATUSES = ['PENDING', 'IN_PROGRESS', 'APPROVED', 'REJECTED', 'CANCELLED', 'EXPIRED', 'AUTO_APPROVED'];

export const paymentApprovalApi = {
  // ── Rules (rule CRUD/versioning + simulate) ──────────────────────
  listRules: (params = {}) => apiClient.get('/approvals/rules', { params: cleanParams(params) }).then((r) => r.data),
  getRule: (ruleId) => apiClient.get(`/approvals/rules/${ruleId}`).then((r) => r.data),
  publishRule: (body) => apiClient.post('/approvals/rules', body).then((r) => r.data),
  setRuleStatus: (ruleId, status) => apiClient.post(`/approvals/rules/${ruleId}/status`, { status }).then((r) => r.data),
  simulateRule: (body) => apiClient.post('/approvals/rules/simulate', body).then((r) => r.data),

  // ── Workflows ─────────────────────────────────────────────────────
  listWorkflows: (params = {}) => apiClient.get('/approvals/workflows', { params: cleanParams(params) }).then((r) => r.data),
  getWorkflow: (workflowId) => apiClient.get(`/approvals/workflows/${workflowId}`).then((r) => r.data),
  createWorkflow: (body) => apiClient.post('/approvals/workflows', body).then((r) => r.data),

  // ── My Approvals — the assignee-facing worklist ──────────────────
  myApprovals: () => apiClient.get('/approvals/my-approvals').then((r) => r.data),

  // ── Act on a step (all four routes gated on the same APPROVE action) ──
  approveStep: (workflowId, stepId, comment) =>
    apiClient.post(`/approvals/workflows/${workflowId}/steps/${stepId}/approve`, { comment }).then((r) => r.data),
  rejectStep: (workflowId, stepId, comment) =>
    apiClient.post(`/approvals/workflows/${workflowId}/steps/${stepId}/reject`, { comment }).then((r) => r.data),
  reviewPeerStep: (workflowId, stepId, comment) =>
    apiClient.post(`/approvals/workflows/${workflowId}/peer-review-steps/${stepId}/review`, { comment }).then((r) => r.data),
  flagPeerStep: (workflowId, stepId, comment) =>
    apiClient.post(`/approvals/workflows/${workflowId}/peer-review-steps/${stepId}/flag`, { comment }).then((r) => r.data),
};
