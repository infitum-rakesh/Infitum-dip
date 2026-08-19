/**
 * Seller Portal -> Plans + Subscriptions API — wraps /api/seller/plans/*
 * and /api/seller/subscriptions/* (Phase 3). See
 * backend/app/services/seller/subscription_service.py. Nothing here is mocked.
 */
import apiClient from '../apiClient.js';

function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const planApi = {
  list: () => apiClient.get('/seller/plans').then((r) => r.data.items),
  create: (body) => apiClient.post('/seller/plans', body).then((r) => r.data),
  update: (id, body) => apiClient.put(`/seller/plans/${id}`, body).then((r) => r.data),
};

export const subscriptionApi = {
  /** params: { customer_id, status_filter, q, page, page_size } */
  list: (params = {}) => apiClient.get('/seller/subscriptions', { params: cleanParams(params) }).then((r) => r.data),

  get: (id) => apiClient.get(`/seller/subscriptions/${id}`).then((r) => r.data),

  create: (body) => apiClient.post('/seller/subscriptions', body).then((r) => r.data),

  update: (id, body) => apiClient.put(`/seller/subscriptions/${id}`, body).then((r) => r.data),

  renew: (id) => apiClient.post(`/seller/subscriptions/${id}/renew`).then((r) => r.data),

  changePlan: (id, planId) => apiClient.post(`/seller/subscriptions/${id}/change-plan`, { plan_id: planId }).then((r) => r.data),

  suspend: (id) => apiClient.post(`/seller/subscriptions/${id}/suspend`).then((r) => r.data),

  resume: (id) => apiClient.post(`/seller/subscriptions/${id}/resume`).then((r) => r.data),

  cancel: (id) => apiClient.post(`/seller/subscriptions/${id}/cancel`).then((r) => r.data),
};
