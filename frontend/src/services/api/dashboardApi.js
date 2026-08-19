/**
 * Dashboard API — wraps GET /api/dashboard. Real aggregation from live
 * tables — see backend/app/services/dashboard_service.py.
 */
import apiClient from './apiClient.js';

export const dashboardApi = {
  get: () => apiClient.get('/dashboard').then((r) => r.data),
};
