import { useEffect, useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import {
  LayoutDashboard, Database, Link2, FileSearch, Files, CheckSquare,
  GitBranch, Shuffle, Terminal, ShieldCheck, Scale, Network, Activity,
  AlertTriangle, PlayCircle, Clock, Users, Shield, Building2, Key,
  ScrollText, Settings, ChevronRight, LogOut, BarChart3, ArrowLeftRight,
  CreditCard, Puzzle, Gauge, UserCog, Monitor, DollarSign, Receipt, Zap, Lock,
  BadgeCheck, History, Boxes, Calculator, FileText, Landmark, FileCheck2, ListChecks, Cpu,
} from 'lucide-react';
import { useAuth } from '@/hooks/useAuth.js';
import { usePermission } from '@/hooks/usePermission.js';
import { useMediaQuery } from '@/hooks/useMediaQuery.js';
import { useAccessSummary } from '@/context/AccessSummaryContext.jsx';
import { MODULES, ROLES } from '@/utils/constants.js';
import { initials, formatDate } from '@/utils/formatters.js';
import { reviewQueueApi } from '@/services/api/reviewQueueApi.js';
import { incidentApi } from '@/services/api/incidentApi.js';
import './Sidebar.css';

const NAV = [
  {
    section: null,
    items: [
      { to: '/app/dashboard', label: 'Dashboard', icon: LayoutDashboard },
    ],
  },
  {
    section: 'DATA',
    items: [
      { to: '/app/catalog',       label: 'Data Catalog',  icon: Database },
      { to: '/app/connections',   label: 'Connections',   icon: Link2 },
      { to: '/app/format-conversion', label: 'Format Conversion', icon: Shuffle },
      { to: '/app/sql-workspace', label: 'SQL Workspace', icon: Terminal, module: MODULES.SQL_WORKSPACE },
    ],
  },
  {
    section: 'DOCUMENT INTELLIGENCE',
    module: MODULES.DOCUMENT_INTELLIGENCE,
    items: [
      { to: '/app/doc-intel',          label: 'Document Intelligence', icon: FileSearch },
      { to: '/app/doc-intel/documents',label: 'Documents',            icon: Files },
      { to: '/app/doc-intel/review',   label: 'Review Queue',         icon: CheckSquare, dynamicBadge: 'reviewQueue' },
    ],
  },
  {
    section: 'INVOICE INTELLIGENCE',
    module: MODULES.INVOICE_INTELLIGENCE,
    items: [
      { to: '/app/invoice-intel', label: 'Invoice Inbox', icon: Receipt },
      { to: '/app/invoice-intel/settings', label: 'Rules & Analytics', icon: Settings },
    ],
  },
  {
    // PAY10 — My Approvals is the landing page. PAY11 adds Approval
    // Rules configuration. PAY12 adds org-wide Workflows visibility.
    section: 'PAYMENT APPROVALS',
    module: MODULES.PAYMENT_APPROVAL,
    items: [
      { to: '/app/payment-approvals', label: 'My Approvals', icon: BadgeCheck },
      { to: '/app/payment-approvals/workflows', label: 'Workflows', icon: History },
      { to: '/app/payment-approvals/rules', label: 'Approval Rules', icon: Settings },
    ],
  },
  {
    section: 'TRANSFORMATION',
    items: [
      { to: '/app/pipeline',   label: 'Pipeline Builder',      icon: GitBranch },
      { to: '/app/transform',  label: 'Transform Workspace',   icon: Shuffle, module: MODULES.TRANSFORMATION },
      { to: '/app/mapping',    label: 'Column Mapping',        icon: ArrowLeftRight, module: MODULES.TRANSFORMATION },
    ],
  },
  {
    section: 'BULK MIGRATION',
    module: MODULES.BULK_MIGRATION,
    items: [
      { to: '/app/bulk-migration', label: 'Pipelines', icon: Boxes },
    ],
  },
  {
    // TALLY-A — read-only connection + company discovery only; write-back
    // and reconciliation land in later phases (see TALLY-B..J).
    section: 'TALLY INTEGRATION',
    module: MODULES.TALLY_INTEGRATION,
    items: [
      { to: '/app/tally/companies', label: 'Companies', icon: Calculator },
      { to: '/app/tally/vouchers', label: 'Voucher Preview', icon: FileText },
      { to: '/app/tally/reconciliation', label: 'Reconciliation', icon: Scale },
      { to: '/app/tally/agents', label: 'Agents', icon: Cpu },
    ],
  },
  {
    // India Business/Tax Identity/Corporate Document Verification —
    // PAN/GSTIN/TAN/MCA/Udyam/Aadhaar (Phase Groups 1-2) + MOA document
    // verification (Phase Group 3); Rules/Jobs items join as later
    // phases ship (see the approved plan's Phase Group 4-5).
    section: 'COMPLIANCE & VERIFICATION',
    module: MODULES.INDIA_VERIFICATION,
    items: [
      { to: '/app/india-verification', label: 'Dashboard', icon: Shield },
      { to: '/app/india-verification/identity', label: 'Identity Verification', icon: FileSearch },
      { to: '/app/india-verification/tax', label: 'Tax Registration', icon: Landmark },
      { to: '/app/india-verification/company', label: 'Company & Startup', icon: Building2 },
      { to: '/app/india-verification/moa', label: 'Corporate Documents', icon: FileCheck2 },
      { to: '/app/india-verification/rules', label: 'Rules', icon: ListChecks },
      { to: '/app/india-verification/jobs', label: 'Batches', icon: Boxes },
      { to: '/app/india-verification/history', label: 'History', icon: History },
      { to: '/app/india-verification/reports', label: 'Reports & Audit', icon: ScrollText },
    ],
  },
  {
    section: 'DATA QUALITY',
    items: [
      { to: '/app/validation',    label: 'Validation',        icon: ShieldCheck, module: MODULES.VALIDATION },
      { to: '/app/reconciliation',label: 'Reconciliation',    icon: Scale,       module: MODULES.RECONCILIATION },
      { to: '/app/anomaly',       label: 'Anomaly Detection', icon: Activity,    module: MODULES.ANOMALY_DETECTION },
    ],
  },
  {
    section: 'OBSERVABILITY',
    items: [
      { to: '/app/lineage',    label: 'Data Lineage', icon: Network,       module: MODULES.LINEAGE },
      { to: '/app/monitoring', label: 'Monitoring',   icon: BarChart3,     module: MODULES.OBSERVABILITY },
      { to: '/app/incidents',  label: 'Incidents',    icon: AlertTriangle, dynamicBadge: 'incidents' },
    ],
  },
  {
    section: 'OPERATIONS',
    items: [
      { to: '/app/pipeline-runs', label: 'Pipeline Runs', icon: PlayCircle },
      { to: '/app/schedules',     label: 'Schedules',     icon: Clock },
    ],
  },
  {
    section: 'ADMINISTRATION',
    role: ROLES.ORG_ADMIN,
    items: [
      // orgScoped: true — these all resolve the viewer's own organization
      // server-side (e.g. licenses.py's _get_org_or_404) and 404 for
      // anyone without one. hasRole(ORG_ADMIN) alone isn't enough to gate
      // them: it's a numeric hierarchy (see ROLE_RANK) where SELLER/
      // SUPER_ADMIN outrank ORG_ADMIN, but neither role actually belongs
      // to an organization — a Seller manages every org, not one, and has
      // organization_id: null same as a Super Admin. Without this check
      // those roles could open a page guaranteed to fail and show a
      // generic "Could not load licenses"-style error instead of just
      // not seeing a link that was never going to work for them.
      { to: '/app/admin/users',    label: 'Users',          icon: Users,    orgScoped: true },
      { to: '/app/admin/sessions', label: 'Active Sessions',icon: Monitor,  orgScoped: true },
      { to: '/app/admin/roles',    label: 'Roles',          icon: Shield,   orgScoped: true },
      { to: '/app/admin/orgs',     label: 'Organizations',  icon: Building2, role: ROLES.SUPER_ADMIN },
      { to: '/app/admin/licenses', label: 'Licenses',       icon: Key,      orgScoped: true },
      { to: '/app/billing',        label: 'Billing',        icon: Receipt,  orgScoped: true },
      { to: '/app/admin/audit',    label: 'Audit Logs',     icon: ScrollText, orgScoped: true },
      { to: '/app/admin/settings', label: 'Settings',       icon: Settings, orgScoped: true },
    ],
  },
  {
    section: 'SELLER PORTAL',
    role: ROLES.SELLER,
    items: [
      { to: '/app/seller',                    label: 'Seller Dashboard',   icon: BarChart3 },
      { to: '/app/seller/billing',            label: 'Billing',            icon: DollarSign },
      { to: '/app/seller/billing/invoices',   label: 'Invoices',           icon: Receipt },
      { to: '/app/seller/billing/payments',   label: 'Payments',           icon: CreditCard },
      { to: '/app/seller/billing/gateways',   label: 'Payment Gateways',   icon: Zap },
      { to: '/app/seller/customers',          label: 'Customers',          icon: Building2 },
      { to: '/app/seller/subscriptions',      label: 'Subscriptions',      icon: CreditCard },
      { to: '/app/seller/purchased-modules',  label: 'Purchased Modules',  icon: Puzzle },
      { to: '/app/seller/usage',              label: 'Usage',              icon: Gauge },
      { to: '/app/seller/license-management', label: 'License Management', icon: Key },
    ],
  },
  {
    section: 'SYSTEM',
    role: ROLES.SELLER,
    items: [
      { to: '/app/seller/users',      label: 'Seller Users', icon: UserCog },
      { to: '/app/seller/audit-logs', label: 'Audit Logs',   icon: ScrollText },
      { to: '/app/seller/settings',   label: 'Settings',     icon: Settings },
    ],
  },
];

export function Sidebar({ collapsed, onToggle, mobileOpen, onMobileClose }) {
  const { user, organization, logout } = useAuth();
  const { hasRole, hasModule, isTrialing, trialDaysLeft } = usePermission();
  const { summary } = useAccessSummary();
  const location = useLocation();
  // Below 1024px the sidebar is a full off-canvas drawer, not the desktop
  // icon-only collapse — there's no persistent screen real-estate to save
  // on a phone/tablet, so labels always render regardless of the `collapsed`
  // preference the user may have set on desktop.
  const isMobile = useMediaQuery('(max-width: 1024px)');
  const effectiveCollapsed = isMobile ? false : collapsed;
  // SB30 — modules that WERE purchased but have since expired (real
  // subscription rows past end_date, per GET /access/modules-summary),
  // distinct from modules never purchased at all. Nav items for these
  // render LOCKED (visible, dimmed, lock icon) instead of just hidden.
  const expiredModuleKeys = new Set(Object.keys(summary?.expired_modules ?? {}));

  // Real trial progress: % of the trial elapsed since the org was created,
  // through to trial_expires_at — never a hardcoded fill. Falls back to null
  // (bar hidden) if either date is missing rather than guessing.
  const trialPercentElapsed = (() => {
    if (!organization?.created_at || !organization?.trial_expires_at) return null;
    const start = new Date(organization.created_at).getTime();
    const end = new Date(organization.trial_expires_at).getTime();
    const totalMs = end - start;
    if (totalMs <= 0) return 100;
    const elapsedMs = Date.now() - start;
    return Math.min(100, Math.max(0, (elapsedMs / totalMs) * 100));
  })();

  // Review Queue's sidebar badge must reflect the real pending count from
  // the API — never hardcoded. Polled so it stays fresh as items are
  // processed elsewhere in the app.
  const [reviewQueueCount, setReviewQueueCount] = useState(null);
  useEffect(() => {
    // Runs once on mount (not on every hasModule identity change — that
    // function is recreated on every OrganizationContext render).
    if (!hasModule(MODULES.DOCUMENT_INTELLIGENCE)) return;
    let cancelled = false;
    const load = () => reviewQueueApi.getCount().then((r) => { if (!cancelled) setReviewQueueCount(r.pending); }).catch(() => {});
    load();
    const interval = setInterval(load, 60_000);
    return () => { cancelled = true; clearInterval(interval); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Incidents' sidebar badge = count of OPEN incidents, real from the API
  // (was hardcoded '3'), same polling pattern as Review Queue above.
  const [openIncidentCount, setOpenIncidentCount] = useState(null);
  useEffect(() => {
    let cancelled = false;
    const load = () => incidentApi.getStats().then((r) => { if (!cancelled) setOpenIncidentCount(r.open); }).catch(() => {});
    load();
    const interval = setInterval(load, 60_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  return (
    <>
      {/* Backdrop — only rendered on mobile/tablet while the drawer is
          open, so it can never intercept a click on desktop. Tapping it
          (or pressing the overlay) closes the drawer, same as a nav tap. */}
      {isMobile && mobileOpen && <div className="sidebar-overlay" onClick={onMobileClose} />}
      <aside className={`sidebar ${effectiveCollapsed ? 'sidebar-collapsed' : ''} ${isMobile && mobileOpen ? 'sidebar-mobile-open' : ''}`}>
      {/* Logo */}
      <div className="sidebar-logo">
        <div className="sidebar-logo-mark">DI</div>
        {!effectiveCollapsed && (
          <div className="sidebar-logo-text">
            <span className="sidebar-logo-name">DataIntelligence</span>
            <span className="sidebar-logo-tag">Platform</span>
          </div>
        )}
      </div>

      {/* Trial banner — real days-remaining from organization.trial_expires_at,
          never hardcoded (see getTrialDaysLeft() in utils/permissions.js). */}
      {!effectiveCollapsed && isTrialing && trialDaysLeft != null && (
        <div className="sidebar-trial">
          <span className="trial-label">🕐 Trial — {trialDaysLeft} day{trialDaysLeft === 1 ? '' : 's'} remaining</span>
          {trialPercentElapsed != null && (
            <div className="trial-bar"><div className="trial-fill" style={{ width: `${trialPercentElapsed}%` }} /></div>
          )}
        </div>
      )}

      {/* Navigation */}
      <nav className="sidebar-nav" aria-label="Main navigation">
        {NAV.map((group) => {
          // Section-level role gate
          if (group.role && !hasRole(group.role)) return null;
          // Section-level module gate
          if (group.module && !hasModule(group.module)) return null;

          // Item-level gates, resolved once so we can also tell whether
          // the section would end up empty (e.g. every item in
          // ADMINISTRATION is orgScoped and the viewer has no
          // organization — a Seller or Super Admin) and skip rendering
          // a section label with nothing under it.
          const visibleItems = group.items
            .filter((item) => {
              if (item.role && !hasRole(item.role)) return false;
              if (item.orgScoped && !organization) return false;
              // Module items: shown normally when currently licensed,
              // shown LOCKED when it's a real expired entitlement, hidden
              // entirely only when the org never purchased it at all.
              if (item.module && !hasModule(item.module) && !expiredModuleKeys.has(item.module)) return false;
              return true;
            })
            .map((item) => ({ ...item, locked: item.module ? !hasModule(item.module) && expiredModuleKeys.has(item.module) : false }));
          if (visibleItems.length === 0) return null;

          return (
            <div key={group.section ?? 'main'} className="nav-group">
              {group.section && !effectiveCollapsed && (
                <span className="nav-section-label">{group.section}</span>
              )}
              {visibleItems.map((item) => {
                const Icon = item.icon;
                const badgeValue = item.dynamicBadge === 'reviewQueue' ? reviewQueueCount
                  : item.dynamicBadge === 'incidents' ? openIncidentCount
                  : item.badge;
                const expiredAt = item.locked ? summary?.expired_modules?.[item.module] : null;
                const title = item.locked
                  ? `${item.label} — subscription expired${expiredAt ? ` on ${formatDate(expiredAt)}` : ''}. Click to renew.`
                  : (effectiveCollapsed ? item.label : undefined);
                return (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    title={title}
                    className={({ isActive }) =>
                      ['nav-item', isActive ? 'nav-item-active' : '', item.locked ? 'nav-item-locked' : ''].filter(Boolean).join(' ')
                    }
                  >
                    <Icon size={16} className="nav-icon" />
                    {!effectiveCollapsed && <span className="nav-label">{item.label}</span>}
                    {!effectiveCollapsed && item.locked && <Lock size={12} className="nav-lock-icon" />}
                    {!effectiveCollapsed && !item.locked && !!badgeValue && (
                      <span className="nav-badge">{badgeValue}</span>
                    )}
                  </NavLink>
                );
              })}
            </div>
          );
        })}
      </nav>

      {/* User footer */}
      <div className="sidebar-footer">
        {!effectiveCollapsed && (
          <div className="sidebar-user">
            <div className="sidebar-avatar">{initials(user?.full_name ?? 'U')}</div>
            <div className="sidebar-user-info">
              <span className="sidebar-user-name">{user?.full_name}</span>
              <span className="sidebar-user-role">{user?.role?.replace(/_/g, ' ')}</span>
            </div>
          </div>
        )}
        <button className="sidebar-logout-btn" onClick={logout} title="Logout" aria-label="Logout">
          <LogOut size={15} />
        </button>
      </div>
    </aside>
    </>
  );
}
