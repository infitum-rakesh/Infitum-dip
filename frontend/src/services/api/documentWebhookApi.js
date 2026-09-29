/**
 * Document Webhook API service — wraps /api/document-webhook. Separate
 * feature from Processed Data Storage (documentStorageApi.js) — pushes
 * processed document output to a customer-owned external HTTP endpoint
 * instead of a database/file, with idempotency/retry/circuit-breaker on
 * the backend (see services/document_webhook_service.py).
 */
import apiClient from './apiClient.js';

export const documentWebhookApi = {
  get: () => apiClient.get('/document-webhook/config').then((r) => r.data),

  update: (body) => apiClient.put('/document-webhook/config', body).then((r) => r.data),

  getSigningSecret: () => apiClient.get('/document-webhook/config/signing-secret').then((r) => r.data),

  test: () => apiClient.post('/document-webhook/config/test').then((r) => r.data),

  listDeliveries: (params = {}) => apiClient.get('/document-webhook/deliveries', { params }).then((r) => r.data),
};
