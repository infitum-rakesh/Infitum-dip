/**
 * Seller Portal -> Usage API — wraps /api/seller/usage/* (Phase 6). See
 * backend/app/services/seller/usage_service.py. Nothing here is mocked.
 */
import apiClient from '../apiClient.js';

function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const usageApi = {
  getKpis: () => apiClient.get('/seller/usage/kpis').then((r) => r.data),

  /** params: { customer_id, module, plan } — no `environment`, see backend module docstring */
  getTable: (params = {}) => apiClient.get('/seller/usage/table', { params: cleanParams(params) }).then((r) => r.data.items),

  getCharts: () => apiClient.get('/seller/usage/charts').then((r) => r.data),

  getCustomerUsage: (customerId) => apiClient.get(`/seller/usage/customers/${customerId}`).then((r) => r.data),
};
