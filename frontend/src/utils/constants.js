/** Application-wide constants */

export const APP_NAME = import.meta.env.VITE_APP_NAME || 'Data Intelligence Platform';
export const APP_VERSION = import.meta.env.VITE_APP_VERSION || '1.0.0';

/** Storage keys */
export const STORAGE_KEYS = {
  ACCESS_TOKEN:  'dip_access_token',
  REFRESH_TOKEN: 'dip_refresh_token',
  USER:          'dip_user',
  ORGANIZATION:  'dip_organization',
  // Seller Portal impersonation (Phase 2) — the Seller's own session,
  // stashed while an impersonated customer session is active, so
  // "Exit Impersonation" can restore it. See context/AuthContext.jsx's
  // impersonate()/exitImpersonation() and components/seller/
  // ImpersonationBanner.jsx.
  IMPERSONATION_ORIGINAL: 'dip_impersonation_original',
};

/** User roles (must match backend enum) */
export const ROLES = {
  SUPER_ADMIN:   'super_admin',
  SELLER:        'seller',
  ORG_ADMIN:     'org_admin',
  DATA_ENGINEER: 'data_engineer',
  ANALYST:       'analyst',
  VIEWER:        'viewer',
};

/** Module keys (must match backend licensed_modules list) */
export const MODULES = {
  DOCUMENT_INTELLIGENCE: 'DOCUMENT_INTELLIGENCE',
  OCR:                   'OCR',
  AI_EXTRACTION:         'AI_EXTRACTION',
  TRANSFORMATION:        'TRANSFORMATION',
  VALIDATION:            'VALIDATION',
  RECONCILIATION:        'RECONCILIATION',
  SQL_WORKSPACE:         'SQL_WORKSPACE',
  DATA_CATALOG:          'DATA_CATALOG',
  LINEAGE:               'LINEAGE',
  PIPELINE_SCHEDULER:    'PIPELINE_SCHEDULER',
  OBSERVABILITY:         'OBSERVABILITY',
  INCIDENTS:             'INCIDENTS',
  ANOMALY_DETECTION:     'ANOMALY_DETECTION',
  INVOICE_INTELLIGENCE:  'INVOICE_INTELLIGENCE',
  PAYMENT_APPROVAL:      'PAYMENT_APPROVAL',
  BULK_MIGRATION:        'BULK_MIGRATION',
  TALLY_INTEGRATION:     'TALLY_INTEGRATION',
};

/** License status */
export const LICENSE_STATUS = {
  TRIAL:     'trial',
  ACTIVE:    'active',
  EXPIRING:  'expiring',
  EXPIRED:   'expired',
  SUSPENDED: 'suspended',
};

/** Permission actions — what a user can do within a module */
export const ACTIONS = {
  VIEW:    'VIEW',
  CREATE:  'CREATE',
  EDIT:    'EDIT',
  DELETE:  'DELETE',
  EXECUTE: 'EXECUTE',
  APPROVE: 'APPROVE',
  PUBLISH: 'PUBLISH',
  ADMIN:   'ADMIN',
  PROFILE: 'PROFILE', // e.g. DATA_CATALOG_PROFILE — run/view data profiling
};

/**
 * Documents module — centralized permission map.
 * Never hardcode module/action pairs inline in a page; import the named
 * permission from here and spread it into <PermissionGate {...perm}> or
 * usePermission().can(perm). All resolve through the same three-layer
 * evaluateAccess() as every other module — Documents has no bespoke
 * permission system of its own.
 */
export const DOCUMENT_PERMISSIONS = {
  DOCUMENT_VIEW:      { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.VIEW },
  DOCUMENT_UPLOAD:    { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.CREATE },
  DOCUMENT_PROCESS:   { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.EXECUTE },
  DOCUMENT_REPROCESS: { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.EXECUTE },
  DOCUMENT_DELETE:    { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.DELETE },
  DOCUMENT_DOWNLOAD:  { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.VIEW },
  DOCUMENT_REVIEW:    { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.APPROVE },
};

/**
 * Review Queue module — centralized permission map. Same pattern as
 * DOCUMENT_PERMISSIONS above: resolves through the existing three-layer
 * evaluateAccess(), never a bespoke check inline in a page.
 */
export const REVIEW_QUEUE_PERMISSIONS = {
  REVIEW_QUEUE_VIEW:     { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.VIEW },
  REVIEW_QUEUE_ASSIGN:   { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.EDIT },
  REVIEW_QUEUE_APPROVE:  { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.APPROVE },
  REVIEW_QUEUE_REJECT:   { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.APPROVE },
  REVIEW_QUEUE_ESCALATE: { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.APPROVE },
  REVIEW_QUEUE_EDIT:     { module: MODULES.DOCUMENT_INTELLIGENCE, action: ACTIONS.EDIT },
};

/**
 * Connections module — centralized permission map.
 * Deliberately has no `module` key: Connections is a foundational,
 * always-available module (every pipeline needs a connection), not gated
 * behind a licensed module the way Document Intelligence's sub-pages are.
 * evaluateAccess() skips the module-license layers entirely when `module`
 * is omitted and falls through to role/action + org-license-active checks
 * — mirrored server-side by passing module=None to require_action().
 */
export const CONNECTION_PERMISSIONS = {
  CONNECTION_VIEW:          { action: ACTIONS.VIEW },
  CONNECTION_CREATE:        { action: ACTIONS.CREATE },
  CONNECTION_EDIT:          { action: ACTIONS.EDIT },
  CONNECTION_TEST:          { action: ACTIONS.EXECUTE },
  CONNECTION_DELETE:        { action: ACTIONS.DELETE },
  CONNECTION_MANAGE_SECRET: { action: ACTIONS.ADMIN },
};

/**
 * Data Format Transformation Engine — centralized permission map.
 * Deliberately has no `module` key, same "foundational, always-available
 * utility" reasoning as CONNECTION_PERMISSIONS above (mirrored
 * server-side by require_action(..., module=None) in api/transformation.py).
 */
export const TRANSFORMATION_PERMISSIONS = {
  TRANSFORMATION_VIEW:   { action: ACTIONS.VIEW },
  TRANSFORMATION_CREATE: { action: ACTIONS.CREATE },
  TRANSFORMATION_RUN:    { action: ACTIONS.EXECUTE },
  TRANSFORMATION_DELETE: { action: ACTIONS.DELETE },
};

/**
 * Schedules module — centralized permission map. Same "no module key"
 * reasoning as CONNECTION_PERMISSIONS: Schedules automates existing
 * pipelines across every other module, so it isn't gated behind any one
 * of their licenses — just role/action tier, mirrored via module=None on
 * the backend's require_action().
 */
export const SCHEDULE_PERMISSIONS = {
  SCHEDULE_VIEW:   { action: ACTIONS.VIEW },
  SCHEDULE_CREATE: { action: ACTIONS.CREATE },
  SCHEDULE_EDIT:   { action: ACTIONS.EDIT },
  SCHEDULE_RUN:    { action: ACTIONS.EXECUTE },
  SCHEDULE_DELETE: { action: ACTIONS.DELETE },
  SCHEDULE_PAUSE:  { action: ACTIONS.EDIT },
};

/**
 * Incidents module — centralized permission map. Same "no module key"
 * reasoning as CONNECTION_PERMISSIONS/SCHEDULE_PERMISSIONS: Incidents
 * monitors failures across every other module (Pipeline, Validation,
 * Reconciliation, Connections, Document Intelligence, Schedules), so it
 * isn't gated behind any one of their licenses — just role/action tier,
 * mirrored via module=None on the backend's require_action().
 */
export const INCIDENT_PERMISSIONS = {
  INCIDENT_VIEW:    { action: ACTIONS.VIEW },
  INCIDENT_CREATE:  { action: ACTIONS.CREATE },
  INCIDENT_ASSIGN:  { action: ACTIONS.EDIT },
  INCIDENT_UPDATE:  { action: ACTIONS.EDIT },
  INCIDENT_RESOLVE: { action: ACTIONS.APPROVE },
  INCIDENT_DELETE:  { action: ACTIONS.DELETE },
};

/**
 * Administration → Users — centralized permission map. Every entry carries
 * `role: ROLES.ORG_ADMIN` (not just an action) because ROLE_DEFAULT_ACTIONS
 * gives DATA_ENGINEER most of the same actions (VIEW/CREATE/EDIT) as
 * ORG_ADMIN — an action check alone wouldn't stop a Data Engineer from
 * managing users. hasRole()'s hierarchy still lets SELLER/SUPER_ADMIN
 * through. Mirrored server-side by every /admin/users/* route requiring
 * the ADMIN action tier, which only ORG_ADMIN+ roles have.
 */
export const USER_PERMISSIONS = {
  USER_VIEW:        { role: ROLES.ORG_ADMIN, action: ACTIONS.VIEW },
  USER_CREATE:      { role: ROLES.ORG_ADMIN, action: ACTIONS.CREATE },
  USER_EDIT:        { role: ROLES.ORG_ADMIN, action: ACTIONS.EDIT },
  USER_DISABLE:     { role: ROLES.ORG_ADMIN, action: ACTIONS.EDIT },
  USER_DELETE:      { role: ROLES.ORG_ADMIN, action: ACTIONS.DELETE },
  USER_ASSIGN_ROLE: { role: ROLES.ORG_ADMIN, action: ACTIONS.ADMIN },
};

/**
 * Administration → Active Sessions — who's currently signed in (password
 * or SSO) and seat usage against Organization.max_users. Mirrored
 * server-side by /admin/sessions/* requiring the ADMIN action tier (see
 * api/sessions.py) — both viewing and revoking need it, since seeing who
 * holds a seat is itself sensitive.
 */
export const SESSION_PERMISSIONS = {
  SESSION_VIEW:   { role: ROLES.ORG_ADMIN, action: ACTIONS.ADMIN },
  SESSION_REVOKE: { role: ROLES.ORG_ADMIN, action: ACTIONS.ADMIN },
};

/**
 * Settings -> Single Sign-On (SAML 2.0) tab. Mirrored server-side by
 * /admin/sso/* requiring the ADMIN action tier (see api/sso_admin.py).
 */
export const SSO_PERMISSIONS = {
  SSO_VIEW: { role: ROLES.ORG_ADMIN, action: ACTIONS.ADMIN },
  SSO_EDIT: { role: ROLES.ORG_ADMIN, action: ACTIONS.ADMIN },
};

/**
 * Settings -> Database tab (Bring-Your-Own-Database, Phase 1). Mirrored
 * server-side by /settings/database/* requiring the ADMIN action tier
 * (see api/settings_database.py) — same tier as every other Settings
 * sub-section.
 */
export const DATABASE_PERMISSIONS = {
  DATABASE_VIEW: { role: ROLES.ORG_ADMIN, action: ACTIONS.ADMIN },
  DATABASE_EDIT: { role: ROLES.ORG_ADMIN, action: ACTIONS.ADMIN },
};

/**
 * Administration → Roles — centralized permission map. Same reasoning as
 * USER_PERMISSIONS: every entry carries `role: ROLES.ORG_ADMIN` so an
 * action check alone can't be satisfied by a lower tier that happens to
 * share the action. Mirrored server-side by every /roles/* route requiring
 * the ADMIN action tier.
 */
export const ROLE_PERMISSIONS = {
  ROLE_VIEW:   { role: ROLES.ORG_ADMIN, action: ACTIONS.VIEW },
  ROLE_CREATE: { role: ROLES.ORG_ADMIN, action: ACTIONS.CREATE },
  ROLE_EDIT:   { role: ROLES.ORG_ADMIN, action: ACTIONS.EDIT },
  ROLE_DELETE: { role: ROLES.ORG_ADMIN, action: ACTIONS.DELETE },
  ROLE_ASSIGN: { role: ROLES.ORG_ADMIN, action: ACTIONS.ADMIN },
};

/**
 * Administration → Licenses — centralized permission map. Read-only page:
 * a single VIEW entry, same `role: ROLES.ORG_ADMIN` reasoning as its sibling
 * admin pages. The Seller controls purchased modules/plan/limits/expiration;
 * org admins can only view this data (per-user module access is granted
 * from Administration → Users instead).
 */
export const LICENSE_PERMISSIONS = {
  LICENSE_VIEW: { role: ROLES.ORG_ADMIN, action: ACTIONS.VIEW },
};

/**
 * Administration → Audit Logs — centralized permission map. Read-only,
 * same `role: ROLES.ORG_ADMIN` reasoning as every sibling admin page.
 * Export shares the exact same gate as viewing — "Respect permissions"
 * means export is never looser than view.
 */
export const AUDIT_LOG_PERMISSIONS = {
  AUDIT_LOG_VIEW:   { role: ROLES.ORG_ADMIN, action: ACTIONS.VIEW },
  AUDIT_LOG_EXPORT: { role: ROLES.ORG_ADMIN, action: ACTIONS.VIEW },
};

/**
 * Administration → Settings — centralized permission map, same
 * `role: ROLES.ORG_ADMIN` reasoning as every sibling admin page. Security is
 * split out at ACTIONS.ADMIN (a step above plain EDIT) since it's the one
 * section — session timeout, password policy, MFA, IP restrictions — with
 * real security blast radius; AI and Notifications are their own named
 * permissions per the spec but share the same EDIT tier as the general
 * sections, so any role that can edit Settings at all can edit those tabs.
 */
export const SETTINGS_PERMISSIONS = {
  SETTINGS_VIEW:          { role: ROLES.ORG_ADMIN, action: ACTIONS.VIEW },
  SETTINGS_EDIT:          { role: ROLES.ORG_ADMIN, action: ACTIONS.EDIT },
  SETTINGS_SECURITY:      { role: ROLES.ORG_ADMIN, action: ACTIONS.ADMIN },
  SETTINGS_AI:            { role: ROLES.ORG_ADMIN, action: ACTIONS.EDIT },
  SETTINGS_NOTIFICATIONS: { role: ROLES.ORG_ADMIN, action: ACTIONS.EDIT },
};

/**
 * Object & Column Mapping — centralized permission map. Gated behind the
 * TRANSFORMATION module (same as Transform Workspace/Pipeline Designer —
 * mapping is a transformation-authoring capability, not a standalone
 * license), mirrored server-side by require_action(action,
 * module="TRANSFORMATION") in api/mapping.py.
 */
export const MAPPING_PERMISSIONS = {
  MAPPING_VIEW:   { module: MODULES.TRANSFORMATION, action: ACTIONS.VIEW },
  MAPPING_CREATE: { module: MODULES.TRANSFORMATION, action: ACTIONS.CREATE },
  MAPPING_EDIT:   { module: MODULES.TRANSFORMATION, action: ACTIONS.EDIT },
  MAPPING_DELETE: { module: MODULES.TRANSFORMATION, action: ACTIONS.DELETE },
  MAPPING_RUN:    { module: MODULES.TRANSFORMATION, action: ACTIONS.EXECUTE },  // Auto Map / Validate / Preview
};

export const SQL_WORKSPACE_PERMISSIONS = {
  SQL_VIEW:   { module: MODULES.SQL_WORKSPACE, action: ACTIONS.VIEW },
  SQL_CREATE: { module: MODULES.SQL_WORKSPACE, action: ACTIONS.CREATE },  // save a query
  SQL_EDIT:   { module: MODULES.SQL_WORKSPACE, action: ACTIONS.EDIT },    // update a saved query
  SQL_DELETE: { module: MODULES.SQL_WORKSPACE, action: ACTIONS.DELETE },
  SQL_RUN:    { module: MODULES.SQL_WORKSPACE, action: ACTIONS.EXECUTE }, // run SQL against a connection
};

/**
 * Anomaly Detection — centralized permission map, same shape as
 * SQL_WORKSPACE_PERMISSIONS/MAPPING_PERMISSIONS above. Mirrored
 * server-side by require_action(action, module="ANOMALY_DETECTION") in
 * api/anomaly.py. ANOMALY_APPROVE covers baseline approval and feedback
 * submission — both use the backend's EDIT/VIEW tiers respectively (see
 * api/anomaly.py's route-level Depends()), not a dedicated APPROVE
 * action, so this map only needs the same five actions every other
 * Data Quality module uses.
 */
export const ANOMALY_PERMISSIONS = {
  ANOMALY_VIEW:   { module: MODULES.ANOMALY_DETECTION, action: ACTIONS.VIEW },
  ANOMALY_CREATE: { module: MODULES.ANOMALY_DETECTION, action: ACTIONS.CREATE },
  ANOMALY_EDIT:   { module: MODULES.ANOMALY_DETECTION, action: ACTIONS.EDIT },   // also gates baseline approval
  ANOMALY_DELETE: { module: MODULES.ANOMALY_DETECTION, action: ACTIONS.DELETE },
  ANOMALY_RUN:    { module: MODULES.ANOMALY_DETECTION, action: ACTIONS.EXECUTE }, // Run Anomaly Detection
};

/**
 * Invoice Intelligence — centralized permission map, same shape as
 * ANOMALY_PERMISSIONS above. Mirrored server-side by require_action(action,
 * module="INVOICE_INTELLIGENCE") in api/invoices.py. INVOICE_EDIT also
 * gates learning-record approval (POST /learning/records/{id}/approve —
 * see api/invoices.py's route-level Depends()); INVOICE_RUN covers every
 * EXECUTE-tier action (reprocess/revalidate/ERP post/ERP cancel/run
 * reconciliation), not a dedicated action per one.
 */
export const INVOICE_PERMISSIONS = {
  INVOICE_VIEW:   { module: MODULES.INVOICE_INTELLIGENCE, action: ACTIONS.VIEW },
  INVOICE_CREATE: { module: MODULES.INVOICE_INTELLIGENCE, action: ACTIONS.CREATE }, // Upload invoice
  INVOICE_EDIT:   { module: MODULES.INVOICE_INTELLIGENCE, action: ACTIONS.EDIT },   // Corrections + learning-record approval
  INVOICE_RUN:    { module: MODULES.INVOICE_INTELLIGENCE, action: ACTIONS.EXECUTE }, // Reprocess/revalidate/ERP post/cancel/reconcile
};

/**
 * Payment Approvals — centralized permission map, same shape as
 * INVOICE_PERMISSIONS above. Mirrored server-side 1:1 by
 * require_action(action, module="PAYMENT_APPROVAL") in api/approvals.py:
 * VIEW (rule/workflow reads + My Approvals), CREATE (publish a rule
 * version, create a workflow), EDIT (activate/deactivate a rule),
 * EXECUTE (simulate), APPROVE (approve/reject a step, review/flag a
 * peer-review step — all four act-on-a-step routes use the same
 * APPROVE action, matching the backend exactly).
 */
export const PAYMENT_APPROVAL_PERMISSIONS = {
  APPROVAL_VIEW:      { module: MODULES.PAYMENT_APPROVAL, action: ACTIONS.VIEW },
  APPROVAL_RULE_EDIT: { module: MODULES.PAYMENT_APPROVAL, action: ACTIONS.CREATE }, // publish a new rule version
  APPROVAL_RULE_STATUS: { module: MODULES.PAYMENT_APPROVAL, action: ACTIONS.EDIT }, // activate/deactivate
  APPROVAL_SIMULATE:  { module: MODULES.PAYMENT_APPROVAL, action: ACTIONS.EXECUTE },
  APPROVAL_ACT:       { module: MODULES.PAYMENT_APPROVAL, action: ACTIONS.APPROVE }, // approve/reject/review/flag a step
};

/**
 * Bulk Object Migration — centralized permission map, same shape as
 * INVOICE_PERMISSIONS/ANOMALY_PERMISSIONS above. Mirrored server-side by
 * require_action(action, module="BULK_MIGRATION") in api/bulk_migration.py.
 * BULK_MIGRATION_RUN covers every EXECUTE-tier action (validate/dry-run/
 * run/pause/resume/cancel/retry-failed), not a dedicated action per one.
 */
export const BULK_MIGRATION_PERMISSIONS = {
  BULK_MIGRATION_VIEW:   { module: MODULES.BULK_MIGRATION, action: ACTIONS.VIEW },
  BULK_MIGRATION_CREATE: { module: MODULES.BULK_MIGRATION, action: ACTIONS.CREATE }, // pipeline + object config, import
  BULK_MIGRATION_EDIT:   { module: MODULES.BULK_MIGRATION, action: ACTIONS.EDIT },
  BULK_MIGRATION_DELETE: { module: MODULES.BULK_MIGRATION, action: ACTIONS.DELETE },
  BULK_MIGRATION_RUN:    { module: MODULES.BULK_MIGRATION, action: ACTIONS.EXECUTE }, // validate/dry-run/run/pause/resume/cancel/retry
};

/**
 * Tally Integration (Phase A: connection + read-only company discovery
 * only — see api/tally.py). Mirrors the same shape as the other module
 * permission maps above. TALLY_DISCOVER reuses EXECUTE, same tier as
 * Test Connection on the generic Connections module.
 */
export const TALLY_PERMISSIONS = {
  TALLY_VIEW:     { module: MODULES.TALLY_INTEGRATION, action: ACTIONS.VIEW },
  TALLY_DISCOVER: { module: MODULES.TALLY_INTEGRATION, action: ACTIONS.EXECUTE },
};

/**
 * Seller Portal — role-only gate (no `module`, deliberately). A Seller-tier
 * user has no organization, so the module-license layers of evaluateAccess()
 * don't apply to them; access here is controlled purely by role rank
 * (ROLES.SELLER or above — see ROLE_RANK) at this layer. Finer-grained
 * seller_role sub-tiers (SELLER_ADMIN/SELLER_CUSTOMER_MANAGER/...) are
 * enforced server-side only for now (core/deps.require_seller_action) —
 * see Seller Users (Phase 7) for the UI-side counterpart.
 */
export const SELLER_PERMISSIONS = {
  SELLER_PORTAL_ACCESS: { role: ROLES.SELLER },
};

/** HTTP status codes */
export const HTTP = {
  OK:            200,
  CREATED:       201,
  BAD_REQUEST:   400,
  UNAUTHORIZED:  401,
  FORBIDDEN:     403,
  NOT_FOUND:     404,
  SERVER_ERROR:  500,
};

/** Pagination defaults */
export const PAGINATION = {
  DEFAULT_PAGE:      1,
  DEFAULT_PAGE_SIZE: 20,
  PAGE_SIZE_OPTIONS: [10, 20, 50, 100],
};
