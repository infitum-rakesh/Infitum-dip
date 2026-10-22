/**
 * Tally Agent API service — wraps /api/tally-agents/* (backend/app/api/
 * tally_agents.py), the Windows Client Agent enrollment/lifecycle
 * endpoints for the Multi-Client TallyPrime Integration Platform (Agent
 * Relay). A second transport alongside the direct-HTTP TALLY_PRIME
 * connection type — see tallyApi.js and connectionApi.js for the
 * generic connection CRUD/test flow this complements.
 *
 * POST /tally-agents/enroll is deliberately NOT wrapped here — it's the
 * one-shot, unauthenticated exchange the Windows agent process itself
 * performs with its enrollment token, never called from this admin UI.
 */
import apiClient from './apiClient.js';

const tallyAgentApi = {
  listInstallations: () => apiClient.get('/tally-agents').then((r) => r.data),

  getInstallation: (installationId) =>
    apiClient.get(`/tally-agents/${installationId}`).then((r) => r.data),

  createInstallation: ({ name, tallyHost, tallyPort }) =>
    apiClient.post('/tally-agents', {
      name,
      tally_host: tallyHost || undefined,
      tally_port: tallyPort || undefined,
    }).then((r) => r.data),

  /** Returned exactly once — the plaintext token is never retrievable again. */
  createEnrollmentToken: (installationId) =>
    apiClient.post(`/tally-agents/${installationId}/enrollment-tokens`).then((r) => r.data),

  /** Returned exactly once, same discipline as enrollment — immediately disconnects the agent if it's currently connected. */
  rotateCredential: (installationId) =>
    apiClient.post(`/tally-agents/${installationId}/rotate-credential`).then((r) => r.data),

  /** Permanent — no un-revoke; re-permitting means a fresh installation + enrollment. */
  revokeInstallation: (installationId, reason) =>
    apiClient.post(`/tally-agents/${installationId}/revoke`, { reason: reason || null }).then((r) => r.data),
};

export default tallyAgentApi;
