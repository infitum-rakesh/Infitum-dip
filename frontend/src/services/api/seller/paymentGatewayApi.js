/**
 * Seller Portal -> Payment Gateways API — wraps /api/payment-gateways/*
 * (SB18/SB27). Real gateway configuration lifecycle — see
 * backend/app/services/billing/payment_gateway_service.py. Credentials
 * are write-only from this client's point of view: the backend never
 * returns a decrypted value, only masked field-presence flags.
 */
import apiClient from '../apiClient.js';

export const paymentGatewayApi = {
  list: () => apiClient.get('/payment-gateways').then((r) => r.data),

  /** body: { credentials_environment, credentials, webhook_secret, set_environment } */
  update: (id, body) => apiClient.put(`/payment-gateways/${id}`, body).then((r) => r.data),

  testConnection: (id, environment) => apiClient.post(`/payment-gateways/${id}/test`, { environment }).then((r) => r.data),

  activate: (id) => apiClient.post(`/payment-gateways/${id}/activate`).then((r) => r.data),

  deactivate: (id) => apiClient.post(`/payment-gateways/${id}/deactivate`).then((r) => r.data),
};
