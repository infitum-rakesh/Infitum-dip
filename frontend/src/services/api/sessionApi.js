/**
 * Active Sessions API — wraps /api/admin/sessions/* (see backend
 * app/api/sessions.py). Real, DB-backed login sessions — see
 * app/models/user_session.py / app/services/session_service.py.
 */
import apiClient from './apiClient.js';

export const sessionApi = {
  list: () => apiClient.get('/admin/sessions').then((r) => r.data),

  revoke: (sessionId) => apiClient.post(`/admin/sessions/${sessionId}/revoke`).then((r) => r.data),
};
