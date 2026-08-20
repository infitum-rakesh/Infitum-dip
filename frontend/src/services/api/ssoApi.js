/**
 * Admin-configurable Single Sign-On (SAML 2.0) — wraps /api/admin/sso/*
 * (see backend/app/api/sso_admin.py). Real IdP config, not mocked — see
 * services/saml_service.py for the actual protocol work this drives.
 */
import apiClient from './apiClient.js';

export const ssoApi = {
  get: () => apiClient.get('/admin/sso').then((r) => r.data),

  /** body may omit idp_x509_cert to leave the stored certificate unchanged. */
  update: (body) => apiClient.put('/admin/sso', body).then((r) => r.data),
};
