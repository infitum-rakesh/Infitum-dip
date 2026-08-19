/**
 * Organization management API.
 * Real: replace with apiClient calls to /api/organizations/…
 */
import { MODULES, LICENSE_STATUS } from '@/utils/constants.js';

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

const MOCK_ORGS = [
  {
    id: 'org_1', name: 'Acme Corporation', slug: 'acme', country: 'United States', industry: 'Manufacturing',
    license_status: LICENSE_STATUS.TRIAL, plan: 'trial',
    seller_granted_modules: [MODULES.DOCUMENT_INTELLIGENCE, MODULES.OCR, MODULES.AI_EXTRACTION, MODULES.TRANSFORMATION, MODULES.VALIDATION],
    licensed_modules:       [MODULES.DOCUMENT_INTELLIGENCE, MODULES.OCR, MODULES.AI_EXTRACTION, MODULES.TRANSFORMATION, MODULES.VALIDATION],
    max_users: 10, seller_id: 'seller_1', is_active: true,
    trial_expires_at: new Date(Date.now() + 8 * 86_400_000).toISOString(),
    created_at: '2024-05-01T10:00:00Z',
    usage:  { users: 5,  documents: 482,  ai_calls: 1_240, storage_gb: 2.1 },
    limits: { users: 10, documents: 500,  ai_calls: 1_000, storage_gb: 5 },
  },
  {
    id: 'org_2', name: 'TechStart Inc.', slug: 'techstart', country: 'United Kingdom', industry: 'Technology',
    license_status: LICENSE_STATUS.ACTIVE, plan: 'enterprise',
    seller_granted_modules: Object.values(MODULES),
    licensed_modules:       Object.values(MODULES),
    max_users: -1, seller_id: 'seller_1', is_active: true,
    trial_expires_at: null,
    created_at: '2024-02-15T10:00:00Z',
    usage:  { users: 28, documents: 18_420, ai_calls: 42_300, storage_gb: 124 },
    limits: { users: -1, documents: -1,    ai_calls: -1,     storage_gb: -1 },
  },
  {
    id: 'org_3', name: 'Retail Corp', slug: 'retailcorp', country: 'Germany', industry: 'Retail',
    license_status: LICENSE_STATUS.EXPIRED, plan: 'professional',
    seller_granted_modules: [MODULES.DOCUMENT_INTELLIGENCE, MODULES.OCR, MODULES.AI_EXTRACTION, MODULES.TRANSFORMATION, MODULES.VALIDATION, MODULES.RECONCILIATION, MODULES.DATA_CATALOG],
    licensed_modules:       [MODULES.DOCUMENT_INTELLIGENCE, MODULES.OCR, MODULES.AI_EXTRACTION, MODULES.TRANSFORMATION, MODULES.VALIDATION, MODULES.RECONCILIATION, MODULES.DATA_CATALOG],
    max_users: 50, seller_id: 'seller_1', is_active: false,
    trial_expires_at: null, license_expires_at: new Date(Date.now() - 5 * 86_400_000).toISOString(),
    created_at: '2023-11-01T10:00:00Z',
    usage:  { users: 12, documents: 34_100, ai_calls: 78_900, storage_gb: 210 },
    limits: { users: 50, documents: 50_000, ai_calls: 100_000, storage_gb: 500 },
  },
  {
    id: 'org_4', name: 'FinanceCo', slug: 'financeco', country: 'Singapore', industry: 'Finance',
    license_status: LICENSE_STATUS.EXPIRING, plan: 'professional',
    seller_granted_modules: [MODULES.DOCUMENT_INTELLIGENCE, MODULES.OCR, MODULES.AI_EXTRACTION, MODULES.TRANSFORMATION, MODULES.VALIDATION, MODULES.RECONCILIATION],
    licensed_modules:       [MODULES.DOCUMENT_INTELLIGENCE, MODULES.OCR, MODULES.AI_EXTRACTION, MODULES.TRANSFORMATION, MODULES.VALIDATION, MODULES.RECONCILIATION],
    max_users: 50, seller_id: 'seller_1', is_active: true,
    trial_expires_at: null, license_expires_at: new Date(Date.now() + 12 * 86_400_000).toISOString(),
    created_at: '2024-01-10T10:00:00Z',
    usage:  { users: 18, documents: 22_800, ai_calls: 56_000, storage_gb: 180 },
    limits: { users: 50, documents: 50_000, ai_calls: 100_000, storage_gb: 500 },
  },
];

let _orgs = MOCK_ORGS.map((o) => ({ ...o }));

export const organizationApi = {
  async list() {
    await delay(400);
    return _orgs;
  },

  async get(id) {
    await delay(200);
    return _orgs.find((o) => o.id === id) ?? null;
  },

  async getCurrent() {
    await delay(200);
    return _orgs.find((o) => o.id === 'org_1') ?? null;
  },

  async create(data) {
    await delay(500);
    const org = { id: `org_${Date.now()}`, slug: data.name.toLowerCase().replace(/\s+/g, '-'), is_active: true, created_at: new Date().toISOString(), usage: { users: 0, documents: 0, ai_calls: 0, storage_gb: 0 }, ...data };
    _orgs = [..._orgs, org];
    return org;
  },

  async update(id, patch) {
    await delay(300);
    _orgs = _orgs.map((o) => o.id === id ? { ...o, ...patch } : o);
    return _orgs.find((o) => o.id === id);
  },

  /** Seller grants/revokes modules for a customer org */
  async updateGrantedModules(id, modules) {
    await delay(400);
    return organizationApi.update(id, { seller_granted_modules: modules, licensed_modules: modules });
  },

  async getUsage(id) {
    await delay(300);
    const org = _orgs.find((o) => o.id === id);
    return { usage: org?.usage ?? {}, limits: org?.limits ?? {} };
  },
};
