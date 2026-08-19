/** Auth API service — wraps all /api/auth/* endpoints */
import apiClient from './apiClient.js';

export const authApi = {
  login: (email, password) =>
    apiClient.post('/auth/login', { email, password }).then((r) => r.data),

  register: (payload) =>
    apiClient.post('/auth/register', payload).then((r) => r.data),

  refresh: (refreshToken) =>
    apiClient.post('/auth/refresh', { refresh_token: refreshToken }).then((r) => r.data),

  logout: () =>
    apiClient.post('/auth/logout').then((r) => r.data),

  forgotPassword: (email) =>
    apiClient.post('/auth/forgot-password', { email }).then((r) => r.data),

  resetPassword: (token, newPassword) =>
    apiClient.post('/auth/reset-password', { token, new_password: newPassword }).then((r) => r.data),

  me: () =>
    apiClient.get('/auth/me').then((r) => r.data),
};
