/**
 * Seller Portal -> Companies API — wraps /api/seller/companies/* (see
 * backend/app/services/seller/company_service.py). A Company is a real
 * billing entity a Seller can register more than one of; each customer
 * Organization is assigned to one, and that assignment picks whose
 * logo/address/signature brand that org's invoice PDFs.
 */
import apiClient from '../apiClient.js';

export const companyApi = {
  list: () => apiClient.get('/seller/companies').then((r) => r.data.items),

  get: (id) => apiClient.get(`/seller/companies/${id}`).then((r) => r.data),

  create: (body) => apiClient.post('/seller/companies', body).then((r) => r.data),

  update: (id, body) => apiClient.put(`/seller/companies/${id}`, body).then((r) => r.data),

  activate: (id) => apiClient.post(`/seller/companies/${id}/activate`).then((r) => r.data),

  deactivate: (id) => apiClient.post(`/seller/companies/${id}/deactivate`).then((r) => r.data),

  setDefault: (id) => apiClient.post(`/seller/companies/${id}/set-default`).then((r) => r.data),
};
