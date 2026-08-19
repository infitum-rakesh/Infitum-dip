/**
 * Seller Dashboard API — wraps /api/seller/dashboard (Seller Portal
 * Phase 1). Real cross-organization aggregation — see
 * backend/app/services/seller/dashboard_service.py. Nothing here is mocked.
 */
import apiClient from '../apiClient.js';

export const sellerDashboardApi = {
  getDashboard: () => apiClient.get('/seller/dashboard').then((r) => r.data),
};
