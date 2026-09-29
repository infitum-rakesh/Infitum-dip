/**
 * Processed Data Storage API service — wraps /api/document-storage.
 * Independent of settingsApi's OrganizationSettings blob (its own model/
 * table, same reasoning as ssoApi.js/tenantDatabaseApi.js) — configures
 * where Document Intelligence's processed output (OCR text, extraction
 * fields, dataset, raw AI response) gets pushed after each document
 * finishes, separate from and additive to Bring-Your-Own-Database.
 */
import apiClient from './apiClient.js';

export const documentStorageApi = {
  get: () => apiClient.get('/document-storage/config').then((r) => r.data),

  update: (body) => apiClient.put('/document-storage/config', body).then((r) => r.data),

  testConnection: () => apiClient.post('/document-storage/config/test-connection').then((r) => r.data),
};
