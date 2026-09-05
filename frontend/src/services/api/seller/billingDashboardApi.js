/**
 * Seller Portal -> Billing Dashboard API — wraps GET /api/seller/billing/
 * dashboard (SB25). Real KPIs aggregated from Invoice/Payment/
 * Subscription/PaymentGateway rows — see backend/app/services/billing/
 * billing_dashboard_service.py. Nothing here is mocked.
 */
import apiClient from '../apiClient.js';

export const billingDashboardApi = {
  getDashboard: () => apiClient.get('/seller/billing/dashboard').then((r) => r.data),
};
