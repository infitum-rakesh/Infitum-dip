/**
 * India Business/Tax Identity/Corporate Document Verification API service
 * — wraps /api/compliance/* (backend/app/api/compliance.py).
 *
 * Phase 1 (IV1-IV6) built PAN; Phase Group 2 (IV7-IV13) added GSTIN/TAN/
 * MCA/Udyam/Aadhaar on the same two generic endpoints — runVerification()
 * (new case + first check) and runCheckOnCase() (run/retry one check on
 * an existing case), both already check_type-parametrized since Phase 1.
 * MCA search uses cin/llpin/legalName instead of identifier (a legal-name
 * search can return several real candidates — never auto-picked, see
 * matches on the returned result). Aadhaar requires consentReference +
 * purpose (never optional — see backend's CONSENT_REQUIRED outcome).
 */
import apiClient from './apiClient.js';

const indiaVerificationApi = {
  /** Creates a new VerificationCase AND runs the given check on it in
   * one call — the normal "start a verification" entry point. */
  runVerification: ({
    checkType, subjectType, subjectName, subjectId, businessProcess,
    identifier, cin, llpin, legalName, submittedName, connectionId,
    consentReference, purpose, forceRefresh,
  }) =>
    apiClient.post('/compliance/verifications', {
      check_type: checkType, subject_type: subjectType, subject_name: subjectName,
      subject_id: subjectId ?? null, business_process: businessProcess ?? null,
      identifier: identifier ?? null, cin: cin ?? null, llpin: llpin ?? null, legal_name: legalName ?? null,
      submitted_name: submittedName ?? null, connection_id: connectionId ?? null,
      consent_reference: consentReference ?? null, purpose: purpose ?? null, force_refresh: !!forceRefresh,
    }).then((r) => r.data),

  listVerifications: (params = {}) => apiClient.get('/compliance/verifications', { params }).then((r) => r.data),

  getVerification: (caseId) => apiClient.get(`/compliance/verifications/${caseId}`).then((r) => r.data),

  /** Runs (or retries) ONE check type on an EXISTING case — the
   * identifier value is never persisted in the clear, so a retry needs
   * it supplied again. checkType is case-insensitive (backend uppercases). */
  runCheckOnCase: (caseId, checkType, {
    identifier, cin, llpin, legalName, submittedName, connectionId, consentReference, purpose, forceRefresh,
  }) =>
    apiClient.post(`/compliance/verifications/${caseId}/checks/${checkType.toLowerCase()}`, {
      identifier: identifier ?? null, cin: cin ?? null, llpin: llpin ?? null, legal_name: legalName ?? null,
      submitted_name: submittedName ?? null, connection_id: connectionId ?? null,
      consent_reference: consentReference ?? null, purpose: purpose ?? null, force_refresh: !!forceRefresh,
    }).then((r) => r.data),

  /** Verification Provider connections (category=VERIFICATION_PROVIDER)
   * — reuses the generic Connections module's own CRUD for create/edit/
   * delete; these two routes are the Compliance-scoped read/test views. */
  listProviders: () => apiClient.get('/compliance/providers').then((r) => r.data),

  testProvider: (connectionId) => apiClient.post(`/compliance/providers/${connectionId}/test`).then((r) => r.data),

  /** MOA is not an identifier check — it ASSESSES a document already run
   * through Document Intelligence with document_type "MOA" (see
   * documentApi.runExtraction). Either caseId (append to an existing
   * case) or subjectType+subjectName (create a new case) is required. */
  verifyMoa: ({ documentId, caseId, subjectType, subjectName, businessProcess }) =>
    apiClient.post('/compliance/moa/verify', {
      document_id: documentId,
      case_id: caseId ?? null,
      subject_type: subjectType ?? null,
      subject_name: subjectName ?? null,
      business_process: businessProcess ?? null,
    }).then((r) => r.data),

  // ── Verification Rules Engine (Phase Group 4, IV19) ──────────────
  listRules: () => apiClient.get('/compliance/rules').then((r) => r.data),

  getRule: (ruleId) => apiClient.get(`/compliance/rules/${ruleId}`).then((r) => r.data),

  publishRule: ({ name, description, businessProcess, subjectType, requiredCheckTypes, freshnessDays, priority, effectiveFrom, effectiveTo }) =>
    apiClient.post('/compliance/rules', {
      name, description: description || null, business_process: businessProcess, subject_type: subjectType || null,
      required_check_types: requiredCheckTypes, freshness_days: freshnessDays ?? null, priority: priority ?? 100,
      effective_from: effectiveFrom || null, effective_to: effectiveTo || null,
    }).then((r) => r.data),

  setRuleStatus: (ruleId, newStatus) => apiClient.post(`/compliance/rules/${ruleId}/status`, { status: newStatus }).then((r) => r.data),

  simulateRule: ({ businessProcess, subjectType }) =>
    apiClient.post('/compliance/rules/simulate', { business_process: businessProcess, subject_type: subjectType || null }).then((r) => r.data),

  // ── Batch verification jobs (Phase Group 4, IV21) ────────────────
  listJobs: (params = {}) => apiClient.get('/compliance/jobs', { params }).then((r) => r.data),

  getJob: (runId) => apiClient.get(`/compliance/jobs/${runId}`).then((r) => r.data),

  createJob: ({ businessProcess, checkTypes, connectionId, subjects }) =>
    apiClient.post('/compliance/jobs', {
      business_process: businessProcess || null, check_types: checkTypes, connection_id: connectionId ?? null, subjects,
    }).then((r) => r.data),

  retryFailedJobItems: (runId) => apiClient.post(`/compliance/jobs/${runId}/retry-failed`).then((r) => r.data),

  /** Phase Group 5 (IV25) — same filter set as listVerifications(), so
   * "what you see in History is exactly what you export." Returns a
   * Blob — caller triggers the save (see AuditLogs.jsx's downloadBlob()
   * for the exact pattern this mirrors). */
  exportVerifications: (format, params = {}) =>
    apiClient.get('/compliance/verifications/export', { params: { ...params, format }, responseType: 'blob' }).then((r) => r.data),
};

export default indiaVerificationApi;
