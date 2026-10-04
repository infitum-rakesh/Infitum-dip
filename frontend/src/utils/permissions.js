/**
 * Centralized permission evaluation — three-layer model:
 *
 *   Layer 1  Seller License   → which modules the seller granted to the org
 *   Layer 2  Org License      → org subscription status + expiry
 *   Layer 3  User Permission  → user role + per-user module access
 *
 * NEVER check permissions inline in page components.
 * ALWAYS use evaluateAccess() or the PermissionContext helpers.
 */
import { ROLES, MODULES, LICENSE_STATUS, ACTIONS } from './constants.js';

// ── Role hierarchy ──────────────────────────────────────────────

export const ROLE_RANK = {
  [ROLES.VIEWER]:        0,
  [ROLES.ANALYST]:       1,
  [ROLES.DATA_ENGINEER]: 2,
  [ROLES.ORG_ADMIN]:     3,
  [ROLES.SELLER]:        4,
  [ROLES.SUPER_ADMIN]:   5,
};

// ── Default actions granted per role ───────────────────────────

export const ROLE_DEFAULT_ACTIONS = {
  [ROLES.SUPER_ADMIN]:   Object.values(ACTIONS),
  [ROLES.SELLER]:        Object.values(ACTIONS),
  [ROLES.ORG_ADMIN]:     [ACTIONS.VIEW, ACTIONS.CREATE, ACTIONS.EDIT, ACTIONS.DELETE, ACTIONS.EXECUTE, ACTIONS.APPROVE, ACTIONS.PUBLISH, ACTIONS.ADMIN, ACTIONS.PROFILE],
  [ROLES.DATA_ENGINEER]: [ACTIONS.VIEW, ACTIONS.CREATE, ACTIONS.EDIT, ACTIONS.EXECUTE, ACTIONS.APPROVE, ACTIONS.PROFILE],
  [ROLES.ANALYST]:       [ACTIONS.VIEW, ACTIONS.EXECUTE, ACTIONS.PROFILE],
  [ROLES.VIEWER]:        [ACTIONS.VIEW],
};

// ── Module catalogue (for UI display) ──────────────────────────

export const MODULE_META = {
  [MODULES.DOCUMENT_INTELLIGENCE]: { label: 'Document Intelligence', icon: '📄', group: 'Core' },
  [MODULES.OCR]:                   { label: 'OCR Engine',            icon: '📝', group: 'Core' },
  [MODULES.AI_EXTRACTION]:         { label: 'AI Extraction',         icon: '🧠', group: 'Core' },
  [MODULES.TRANSFORMATION]:        { label: 'Transformation',        icon: '⚙️', group: 'Pipeline' },
  [MODULES.VALIDATION]:            { label: 'Data Validation',       icon: '✅', group: 'Pipeline' },
  [MODULES.RECONCILIATION]:        { label: 'Reconciliation',        icon: '⚖️', group: 'Pipeline' },
  [MODULES.SQL_WORKSPACE]:         { label: 'SQL Workspace',         icon: '💾', group: 'Pipeline' },
  [MODULES.DATA_CATALOG]:          { label: 'Data Catalog',          icon: '🗂️', group: 'Intelligence' },
  [MODULES.LINEAGE]:               { label: 'Data Lineage',          icon: '🌐', group: 'Intelligence' },
  [MODULES.PIPELINE_SCHEDULER]:    { label: 'Pipeline Scheduler',    icon: '⏰', group: 'Operations' },
  [MODULES.OBSERVABILITY]:         { label: 'Observability',         icon: '📊', group: 'Operations' },
  [MODULES.INCIDENTS]:             { label: 'Incident Management',   icon: '🚨', group: 'Operations' },
  [MODULES.ANOMALY_DETECTION]:     { label: 'Anomaly Detection',     icon: '🔎', group: 'Pipeline' },
  [MODULES.INVOICE_INTELLIGENCE]:  { label: 'Invoice Intelligence',  icon: '🧾', group: 'Intelligence' },
  [MODULES.PAYMENT_APPROVAL]:      { label: 'Payment Approvals',     icon: '🔏', group: 'Operations' },
  [MODULES.BULK_MIGRATION]:        { label: 'Bulk Object Migration', icon: '📦', group: 'Pipeline' },
  [MODULES.TALLY_INTEGRATION]:     { label: 'Tally Integration',     icon: '🧮', group: 'Intelligence' },
};

export const SUBSCRIPTION_PLANS = {
  trial: {
    id: 'trial', name: 'Free Trial',
    modules: [MODULES.DOCUMENT_INTELLIGENCE, MODULES.OCR, MODULES.AI_EXTRACTION, MODULES.TRANSFORMATION, MODULES.VALIDATION],
    limits: { users: 5, documents: 500, ai_calls: 1000, storage_gb: 5 }, durationDays: 14,
  },
  basic: {
    id: 'basic', name: 'Basic',
    modules: [MODULES.DOCUMENT_INTELLIGENCE, MODULES.OCR, MODULES.AI_EXTRACTION, MODULES.TRANSFORMATION, MODULES.VALIDATION],
    limits: { users: 10, documents: 5000, ai_calls: 10000, storage_gb: 50 },
  },
  professional: {
    id: 'professional', name: 'Professional',
    modules: [MODULES.DOCUMENT_INTELLIGENCE, MODULES.OCR, MODULES.AI_EXTRACTION, MODULES.TRANSFORMATION, MODULES.VALIDATION, MODULES.RECONCILIATION, MODULES.ANOMALY_DETECTION, MODULES.DATA_CATALOG, MODULES.LINEAGE, MODULES.PIPELINE_SCHEDULER],
    limits: { users: 50, documents: 50000, ai_calls: 100000, storage_gb: 500 },
  },
  enterprise: {
    id: 'enterprise', name: 'Enterprise',
    modules: Object.values(MODULES),
    limits: { users: -1, documents: -1, ai_calls: -1, storage_gb: -1 },
  },
};

// ── Layer 1: Seller grant check ─────────────────────────────────

export function sellerGranted(organization, moduleKey) {
  if (!organization) return false;
  const granted = organization.seller_granted_modules ?? organization.licensed_modules ?? [];
  return granted.includes(moduleKey);
}

// ── Layer 2: License checks ─────────────────────────────────────

export function isLicenseActive(organization) {
  if (!organization) return false;
  const s = organization.license_status;
  return s === LICENSE_STATUS.ACTIVE || s === LICENSE_STATUS.TRIAL || s === LICENSE_STATUS.EXPIRING;
}

export function isLicenseExpired(organization) {
  if (!organization) return false;
  return organization.license_status === LICENSE_STATUS.EXPIRED
      || organization.license_status === LICENSE_STATUS.SUSPENDED;
}

export function orgHasModule(organization, moduleKey) {
  if (!organization) return false;
  return (organization.licensed_modules ?? []).includes(moduleKey);
}

export function getTrialDaysLeft(organization) {
  if (organization?.license_status !== LICENSE_STATUS.TRIAL) return null;
  if (!organization.trial_expires_at) return null;
  const msLeft = new Date(organization.trial_expires_at).getTime() - Date.now();
  return Math.max(0, Math.ceil(msLeft / 86_400_000));
}

// ── Layer 3: User-level checks ──────────────────────────────────

export function hasRole(user, requiredRole) {
  if (!user) return false;
  return (ROLE_RANK[user.role] ?? -1) >= (ROLE_RANK[requiredRole] ?? 99);
}

export function userHasModule(user, moduleKey) {
  if (!user) return false;
  if (!user.accessible_modules) return true; // null = inherits all org modules
  return user.accessible_modules.includes(moduleKey);
}

export function roleCanAction(userRole, action) {
  if (!action) return true;
  return (ROLE_DEFAULT_ACTIONS[userRole] ?? []).includes(action);
}

// ── Primary evaluation ──────────────────────────────────────────

/**
 * Full three-layer access check.
 * Returns { granted: boolean, reason: string }
 * Never throws — always returns a safe result.
 */
export function evaluateAccess(user, organization, { module: moduleKey, action, minRole } = {}) {
  if (!user) return { granted: false, reason: 'not_authenticated' };

  // Super admin bypasses everything
  if (user.role === ROLES.SUPER_ADMIN) return { granted: true, reason: 'super_admin' };

  // Sellers are structurally org-less (organization_id: null), same as
  // Super Admin — they manage every customer org's licensing, they aren't
  // themselves a subject of one org's subscription/module grant. Without
  // this, any PermissionGate a Seller happens to render (e.g. Data
  // Catalog, which Sidebar shows to every role since it's an
  // always-on/foundational page server-side — see api/data_catalog.py's
  // `module=None` gate) fell through to sellerGranted(null, ...) /
  // orgHasModule(null, ...), which unconditionally return false for a
  // null organization — denying Sellers a page the backend never actually
  // gates for them. Found live 2026-09-12: "Data Catalog access error for
  // seller admin user."
  if (user.role === ROLES.SELLER) return { granted: true, reason: 'seller_admin' };

  if (moduleKey) {
    // Layer 1 — seller must have granted this module to the org
    if (!sellerGranted(organization, moduleKey))
      return { granted: false, reason: 'not_licensed_by_seller' };

    // Layer 2a — license must be active
    if (isLicenseExpired(organization))
      return { granted: false, reason: `license_${organization?.license_status}` };

    // Layer 2b — org subscription includes this module
    if (!orgHasModule(organization, moduleKey))
      return { granted: false, reason: 'module_not_in_subscription' };

    // Layer 3a — user was granted this module by the org admin
    if (!userHasModule(user, moduleKey))
      return { granted: false, reason: 'user_not_granted_module' };
  }

  // License must be active for any protected operation
  if (organization && !isLicenseActive(organization))
    return { granted: false, reason: `license_${organization.license_status}` };

  // Layer 3b — minimum role check
  if (minRole && !hasRole(user, minRole))
    return { granted: false, reason: 'insufficient_role' };

  // Layer 3c — role action check
  if (action && !roleCanAction(user.role, action))
    return { granted: false, reason: 'action_not_permitted_for_role' };

  return { granted: true, reason: 'allowed' };
}

// ── Convenience wrappers ────────────────────────────────────────

export function canAccess(user, organization, { role, module: moduleKey } = {}) {
  return evaluateAccess(user, organization, { module: moduleKey, minRole: role }).granted;
}

export function isSeller(user) { return hasRole(user, ROLES.SELLER); }
export function isAdmin(user)  { return hasRole(user, ROLES.ORG_ADMIN); }

/** Modules the user can actually access (all three layers resolved) */
export function getEffectiveModules(user, organization) {
  if (!user || !organization) return [];
  if (user.role === ROLES.SUPER_ADMIN) return Object.values(MODULES);
  if (!isLicenseActive(organization)) return [];

  const orgModules  = organization.licensed_modules ?? [];
  const userModules = user.accessible_modules ?? orgModules;
  return orgModules.filter((m) => userModules.includes(m));
}

export function isOverLimit(organization, resource) {
  const limit = organization?.limits?.[resource];
  const used  = organization?.usage?.[resource];
  if (limit == null || limit === -1) return false;
  return (used ?? 0) >= limit;
}

export function usagePercent(organization, resource) {
  const limit = organization?.limits?.[resource];
  const used  = organization?.usage?.[resource] ?? 0;
  if (!limit || limit === -1) return 0;
  return Math.min(100, Math.round((used / limit) * 100));
}
