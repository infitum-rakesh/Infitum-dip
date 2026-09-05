/**
 * Notifications API — wraps /api/notifications/* (SB23/SB24, Multi-
 * Subscription Billing & Payments Platform). Backs the real header Bell
 * (components/navigation/Header.jsx + NotificationDropdown.jsx) — every
 * row here is a real Notification the backend's expiry/billing
 * schedulers (or a payment/invoice event) actually wrote, never mocked.
 */
import apiClient from './apiClient.js';

export const notificationApi = {
  /** params: { unread_only, page, page_size } */
  list: (params = {}) => apiClient.get('/notifications', { params }).then((r) => r.data),

  markRead: (id) => apiClient.post(`/notifications/${id}/read`).then((r) => r.data),

  markAllRead: () => apiClient.post('/notifications/read-all').then((r) => r.data),
};
