/**
 * Seller Portal -> Purchased Modules API — wraps /api/seller/modules/*
 * (Phase 4). See backend/app/services/seller/module_service.py. Nothing
 * here is mocked.
 */
import apiClient from '../apiClient.js';

export const moduleApi = {
  getCatalog: () => apiClient.get('/seller/modules').then((r) => r.data.items),

  getCustomerModules: (customerId) => apiClient.get(`/seller/modules/customers/${customerId}`).then((r) => r.data),

  /** body: { enabled, license_type?, max_users?, monthly_credits?, max_documents?, expiry_date? } */
  setModule: (customerId, module, body) =>
    apiClient.put(`/seller/modules/customers/${customerId}/${module}`, body).then((r) => r.data),
};
