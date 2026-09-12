import { Navigate, Outlet } from 'react-router-dom';
import { createBrowserRouter } from 'react-router-dom';

// Home / Landing page
import Home from '@/pages/home/Home.jsx';
import Docs from '@/pages/docs/Docs.jsx';

// Layouts
import { AuthLayout }  from '@/layouts/AuthLayout.jsx';
import { MainLayout }  from '@/layouts/MainLayout.jsx';

// Auth pages
import Login              from '@/pages/auth/Login.jsx';
import Register           from '@/pages/auth/Register.jsx';
import ForgotPassword     from '@/pages/auth/ForgotPassword.jsx';
import SsoCallback        from '@/pages/auth/SsoCallback.jsx';
import MustChangePassword from '@/pages/auth/MustChangePassword.jsx';

// Dashboard (implemented)
import Dashboard from '@/pages/dashboard/Dashboard.jsx';

// Data Catalog (implemented)
import DataCatalog     from '@/pages/data-catalog/DataCatalog.jsx';
import DatasetDetails  from '@/pages/data-catalog/DatasetDetails.jsx';

// Connections (implemented)
import Connections from '@/pages/connections/Connections.jsx';

// Document Intelligence (implemented)
import DocumentIntelligence from '@/pages/document-intelligence/DocumentIntelligence.jsx';

// Documents (implemented)
import Documents        from '@/pages/documents/Documents.jsx';
import DocumentDetails  from '@/pages/documents/DocumentDetails.jsx';

// Review Queue (implemented)
import ReviewQueue       from '@/pages/review-queue/ReviewQueue.jsx';
import ReviewQueueDetail from '@/pages/review-queue/ReviewQueueDetail.jsx';

// Incidents (implemented)
import Incidents        from '@/pages/incidents/Incidents.jsx';
import IncidentDetails  from '@/pages/incidents/IncidentDetails.jsx';

// Administration → Users (implemented)
import Users             from '@/pages/administration/users/Users.jsx';
import ActiveSessions    from '@/pages/administration/sessions/ActiveSessions.jsx';
import UserDetails       from '@/pages/administration/users/UserDetails.jsx';

// Administration → Roles (implemented)
import Roles              from '@/pages/administration/roles/Roles.jsx';
import RoleDetails        from '@/pages/administration/roles/RoleDetails.jsx';

// Administration → Licenses (implemented)
import Licenses            from '@/pages/administration/licenses/Licenses.jsx';

// Administration → Audit Logs (implemented)
import AuditLogs            from '@/pages/administration/audit-logs/AuditLogs.jsx';

// Administration → Settings (implemented)
import Settings              from '@/pages/administration/settings/Settings.jsx';

// Transformation (implemented)
import Transformation from '@/pages/transformation/Transformation.jsx';

// Object & Column Mapping (implemented)
import ObjectColumnMapping from '@/pages/mapping/ObjectColumnMapping.jsx';

// Monitoring (implemented)
import Monitoring from '@/pages/monitoring/Monitoring.jsx';

// Lineage (implemented)
import Lineage from '@/pages/lineage/Lineage.jsx';

// Data Quality (implemented)
import Validation     from '@/pages/validation/Validation.jsx';
import ValidationList from '@/pages/validation/ValidationList.jsx';
import Reconciliation     from '@/pages/reconciliation/Reconciliation.jsx';
import ReconciliationList from '@/pages/reconciliation/ReconciliationList.jsx';
import AnomalyOverview    from '@/pages/anomaly-detection/AnomalyOverview.jsx';
import AnomalyJobs        from '@/pages/anomaly-detection/AnomalyJobs.jsx';
import AnomalyJobEditor   from '@/pages/anomaly-detection/AnomalyJobEditor.jsx';
import AnomalyResults        from '@/pages/anomaly-detection/AnomalyResults.jsx';
import AnomalyExecutionDetail from '@/pages/anomaly-detection/AnomalyExecutionDetail.jsx';
import AnomalyReports        from '@/pages/anomaly-detection/AnomalyReports.jsx';
import AnomalyConfiguration  from '@/pages/anomaly-detection/AnomalyConfiguration.jsx';
import InvoiceInbox    from '@/pages/invoice-intelligence/InvoiceInbox.jsx';
import InvoiceDetail   from '@/pages/invoice-intelligence/InvoiceDetail.jsx';
import InvoiceSettings from '@/pages/invoice-intelligence/InvoiceSettings.jsx';

// Payment Approvals (PAY10) — My Approvals worklist is the landing page.
// Approval Rules (PAY11) — rule configuration + simulate.
// Approval Workflows + Detail/Timeline (PAY12) — org-wide workflow
// visibility and per-workflow drill-down.
import MyApprovals from '@/pages/payment-approvals/MyApprovals.jsx';
import ApprovalRules from '@/pages/payment-approvals/ApprovalRules.jsx';
import ApprovalWorkflows from '@/pages/payment-approvals/ApprovalWorkflows.jsx';
import ApprovalWorkflowDetail from '@/pages/payment-approvals/ApprovalWorkflowDetail.jsx';

// SQL Workspace (implemented)
import SqlWorkspace from '@/pages/sql-workspace/SqlWorkspace.jsx';

// Pipeline (implemented)
import PipelineBuilder from '@/pages/pipeline/PipelineBuilder.jsx';
import PipelineRuns    from '@/pages/pipeline/PipelineRuns.jsx';

// Schedules (implemented)
import Schedules from '@/pages/schedules/Schedules.jsx';

// Bulk Object Migration (BM9-13) — Pipelines list is the landing page;
// PipelineDetail is the objects grid + validate/dry-run/run workspace;
// RunMonitor is the live-polling run-progress page.
import BulkMigrationPipelines from '@/pages/bulk-migration/BulkMigrationPipelines.jsx';
import BulkMigrationPipelineDetail from '@/pages/bulk-migration/BulkMigrationPipelineDetail.jsx';
import BulkMigrationRunMonitor from '@/pages/bulk-migration/BulkMigrationRunMonitor.jsx';

// Customer Billing (implemented, SB29)
import CustomerBilling from '@/pages/billing/CustomerBilling.jsx';

// Public Custom Gateway checkout (implemented, SB31 — found missing live)
import PayPage from '@/pages/pay/PayPage.jsx';

// Seller Portal (Phase 1: Dashboard, Phase 2: Customers, Phase 3:
// Subscriptions, Phase 4: Purchased Modules, Phase 5: License Management
// implemented; rest are placeholders per SELLER PORTAL spec's phased
// implementation strategy)
import SellerDashboard from '@/pages/seller/dashboard/SellerDashboard.jsx';
import SellerBillingDashboard from '@/pages/seller/billing/SellerBillingDashboard.jsx';
import SellerInvoices from '@/pages/seller/billing/SellerInvoices.jsx';
import SellerPayments from '@/pages/seller/billing/SellerPayments.jsx';
import PaymentGateways from '@/pages/seller/billing/PaymentGateways.jsx';
import Customers from '@/pages/seller/customers/Customers.jsx';
import CustomerDetails from '@/pages/seller/customers/CustomerDetails.jsx';
import Subscriptions from '@/pages/seller/subscriptions/Subscriptions.jsx';
import PurchasedModules from '@/pages/seller/modules/PurchasedModules.jsx';
import LicenseManagement from '@/pages/seller/licenses/LicenseManagement.jsx';
import Usage from '@/pages/seller/usage/Usage.jsx';
import SellerUsers from '@/pages/seller/users/SellerUsers.jsx';
import SellerAuditLogs from '@/pages/seller/audit-logs/SellerAuditLogs.jsx';
import SellerSettings from '@/pages/seller/settings/SellerSettings.jsx';
import AccessDenied from '@/pages/seller/AccessDenied.jsx';

// Placeholder factory
import { PlaceholderPage } from '@/components/common/PlaceholderPage.jsx';

// 404
import NotFound from '@/pages/not-found/NotFound.jsx';

// Auth gate
import { useAuth } from '@/hooks/useAuth.js';
import { FullPageSpinner } from '@/components/common/LoadingSpinner.jsx';
import { isSeller } from '@/utils/permissions.js';

function ProtectedRoute() {
  const { isAuthenticated, isInitialised, user } = useAuth();
  if (!isInitialised) return <FullPageSpinner />;
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  // Forced password reset — set on login/refresh whenever the account's
  // current password is a dummy value an admin/Seller just issued (see
  // schemas/auth.py's UserOut.must_change_password). Blocks every /app/*
  // route, including the Seller Portal subtree below (it reuses this same
  // component), until POST /auth/change-password clears it. Never true for
  // an SSO account, so SSO logins are unaffected without any extra check.
  if (user?.must_change_password) return <Navigate to="/must-change-password" replace />;
  return <Outlet />;
}

// SELLER PORTAL spec section 15: direct navigation to /app/seller/* without
// Seller-tier access must show "Access Denied", not silently redirect or
// leak the Seller UI shell. Real enforcement is server-side (every
// /api/seller/* call 403s independently via core/deps.require_seller_action)
// — this only prevents the client from rendering a page it can't fetch
// data for anyway.
function SellerRoute() {
  const { user } = useAuth();
  return isSeller(user) ? <Outlet /> : <AccessDenied />;
}

// Keeps unauthenticated users out of /login|/register when already logged in.
// Home page is deliberately outside this gate so anyone can visit it.
function PublicRoute() {
  const { isAuthenticated, user } = useAuth();
  if (!isAuthenticated) return <Outlet />;
  // Send a forced-reset account straight to that page instead of dashboard
  // — ProtectedRoute would only bounce it there anyway on the next render.
  return <Navigate to={user?.must_change_password ? '/must-change-password' : '/app/dashboard'} replace />;
}

// Gate for the forced-reset page itself: must be logged in, and only worth
// showing while the flag that sent them here is actually still set — once
// cleared (or if it was never set), there's nothing to do on this page.
function MustChangePasswordRoute() {
  const { isAuthenticated, isInitialised, user } = useAuth();
  if (!isInitialised) return <FullPageSpinner />;
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return user?.must_change_password ? <Outlet /> : <Navigate to="/app/dashboard" replace />;
}

/** Shorthand to produce a lazy placeholder element */
const ph = (title, icon, description, phase) => (
  <PlaceholderPage title={title} icon={icon} description={description} phase={phase} />
);

export const router = createBrowserRouter([
  // Landing page — public, visible to all (authenticated users see dashboard link)
  { path: '/', element: <Home /> },
  { path: '/docs', element: <Docs /> },

  // Public Custom Gateway checkout — no auth, no app shell, reachable by
  // anyone holding the signed token (SB16/SB31: the exact same contract
  // as a real external gateway's hosted checkout link).
  { path: '/pay/:token', element: <PayPage /> },

  // Public auth routes
  {
    element: <PublicRoute />,
    children: [
      {
        element: <AuthLayout />,
        children: [
          { path: '/login',           element: <Login /> },
          { path: '/register',        element: <Register /> },
          { path: '/forgot-password', element: <ForgotPassword /> },
          { path: '/sso/callback',    element: <SsoCallback /> },
        ],
      },
    ],
  },

  // Forced password-reset — reachable only while authenticated AND
  // user.must_change_password is true (see MustChangePasswordRoute above).
  // Uses the same AuthLayout shell as /login et al since it's the same kind
  // of full-bleed auth-flow page, just requiring an existing session.
  {
    element: <MustChangePasswordRoute />,
    children: [
      {
        element: <AuthLayout />,
        children: [
          { path: '/must-change-password', element: <MustChangePassword /> },
        ],
      },
    ],
  },

  // Protected app routes
  {
    element: <ProtectedRoute />,
    children: [
      {
        path: '/app',
        element: <MainLayout />,
        children: [
          { index: true,       element: <Navigate to="/app/dashboard" replace /> },
          { path: 'dashboard', element: <Dashboard /> },

          // Data
          { path: 'catalog',              element: <DataCatalog /> },
          { path: 'catalog/:assetId',     element: <DatasetDetails /> },
          { path: 'connections', element: <Connections /> },
          { path: 'sql-workspace', element: <SqlWorkspace /> },
          { path: 'sql', element: <Navigate to="/app/sql-workspace" replace /> }, // old path — kept working

          // Document Intelligence
          { path: 'doc-intel',                       element: <DocumentIntelligence /> },
          { path: 'doc-intel/documents',              element: <Documents /> },
          { path: 'doc-intel/documents/:documentId',  element: <DocumentDetails /> },
          { path: 'doc-intel/review',           element: <ReviewQueue /> },
          { path: 'doc-intel/review/:reviewId', element: <ReviewQueueDetail /> },

          // Transformation
          { path: 'pipeline',   element: <PipelineBuilder /> },
          { path: 'transform',  element: <Transformation /> },
          { path: 'mapping',           element: <ObjectColumnMapping /> },
          { path: 'mapping/:mappingId', element: <ObjectColumnMapping /> },

          // Data Quality — the bare path is the list view (job name /
          // object name / Run / Add Rule per row, per the Validation +
          // Reconciliation UI spec); /new and /:id open the real
          // rule-set/job editor, same component either way.
          { path: 'validation',              element: <ValidationList /> },
          { path: 'validation/new',          element: <Validation /> },
          { path: 'validation/:ruleSetId',   element: <Validation /> },
          { path: 'reconciliation',          element: <ReconciliationList /> },
          { path: 'reconciliation/new',      element: <Reconciliation /> },
          { path: 'reconciliation/:jobId',   element: <Reconciliation /> },
          // Anomaly Detection — Overview is the bare-path landing page
          // (KPIs/filters/charts across every job's executions); Jobs
          // (AD7) is the list + create/edit wizard. Results/Reports/
          // Configuration land as sibling routes in later phases
          // (AD8-AD9), same incremental-route pattern every other
          // module here follows.
          { path: 'anomaly',                 element: <AnomalyOverview /> },
          { path: 'anomaly/jobs',             element: <AnomalyJobs /> },
          { path: 'anomaly/jobs/new',         element: <AnomalyJobEditor /> },
          { path: 'anomaly/jobs/:jobId',      element: <AnomalyJobEditor /> },
          { path: 'anomaly/results',              element: <AnomalyResults /> },
          { path: 'anomaly/executions/:executionId', element: <AnomalyExecutionDetail /> },
          { path: 'anomaly/reports',               element: <AnomalyReports /> },
          { path: 'anomaly/configuration',         element: <AnomalyConfiguration /> },

          // Invoice Intelligence — Inbox (INV14) is the landing page;
          // InvoiceDetail (INV15/INV16 — full workspace + evidence viewer
          // + corrections). "settings" is a static-prefix route and must
          // stay registered before the :invoiceId catch-all below it.
          { path: 'invoice-intel',            element: <InvoiceInbox /> },
          { path: 'invoice-intel/settings',   element: <InvoiceSettings /> },
          { path: 'invoice-intel/:invoiceId', element: <InvoiceDetail /> },

          // Payment Approvals — My Approvals worklist (PAY10). Rule
          // Configuration and Approval Detail/Timeline pages are later phases.
          { path: 'payment-approvals',                         element: <MyApprovals /> },
          { path: 'payment-approvals/rules',                   element: <ApprovalRules /> },
          { path: 'payment-approvals/workflows',                element: <ApprovalWorkflows /> },
          { path: 'payment-approvals/workflows/:workflowId',    element: <ApprovalWorkflowDetail /> },

          // Observability
          { path: 'lineage',    element: <Lineage /> },
          { path: 'monitoring', element: <Monitoring /> },
          { path: 'incidents',              element: <Incidents /> },
          { path: 'incidents/:incidentId',  element: <IncidentDetails /> },

          // Operations
          { path: 'pipeline-runs', element: <PipelineRuns /> },
          { path: 'schedules',     element: <Schedules /> },

          // Bulk Object Migration — Pipelines list is the bare-path
          // landing page; "new" is a static-prefix route and must stay
          // registered before the :pipelineId catch-all below it, per
          // this router's own static-before-dynamic convention.
          { path: 'bulk-migration',                        element: <BulkMigrationPipelines /> },
          { path: 'bulk-migration/runs/:runId',             element: <BulkMigrationRunMonitor /> },
          { path: 'bulk-migration/:pipelineId',             element: <BulkMigrationPipelineDetail /> },

          // Billing (SB29) — org-scoped self-service Subscriptions/
          // Invoices/Payments, reachable regardless of subscription
          // state (spec section 60's non-billable-route allowlist).
          { path: 'billing', element: <CustomerBilling /> },

          // Admin
          { path: 'admin/users',            element: <Users /> },
          { path: 'admin/users/:userId',    element: <UserDetails /> },
          { path: 'admin/sessions',         element: <ActiveSessions /> },
          { path: 'admin/roles',            element: <Roles /> },
          { path: 'admin/roles/:roleId',    element: <RoleDetails /> },
          { path: 'admin/orgs',     element: ph('Organization Management', '🏢', 'Manage organizations and tenants.', 'Phase 10') },
          { path: 'admin/licenses',         element: <Licenses /> },
          { path: 'admin/audit',            element: <AuditLogs /> },
          { path: 'admin/settings',         element: <Settings /> },

        ],
      },
    ],
  },

  // Seller Portal — separate access-gated subtree (SellerRoute), still
  // inside MainLayout/ProtectedRoute so it reuses the exact same
  // Sidebar/Header/theme as the rest of the app (spec section 16: "Reuse
  // existing Layout... Do not duplicate these components").
  {
    element: <ProtectedRoute />,
    children: [
      {
        path: '/app',
        element: <MainLayout />,
        children: [
          {
            element: <SellerRoute />,
            children: [
              { path: 'seller',                    element: <SellerDashboard /> },
              { path: 'seller/billing',            element: <SellerBillingDashboard /> },
              { path: 'seller/billing/invoices',   element: <SellerInvoices /> },
              { path: 'seller/billing/payments',   element: <SellerPayments /> },
              { path: 'seller/billing/gateways',   element: <PaymentGateways /> },
              { path: 'seller/customers',          element: <Customers /> },
              { path: 'seller/customers/:customerId', element: <CustomerDetails /> },
              { path: 'seller/subscriptions',      element: <Subscriptions /> },
              { path: 'seller/subscriptions/:subscriptionId', element: <Subscriptions /> },
              { path: 'seller/purchased-modules',  element: <PurchasedModules /> },
              { path: 'seller/usage',              element: <Usage /> },
              { path: 'seller/license-management', element: <LicenseManagement /> },
              { path: 'seller/users',              element: <SellerUsers /> },
              { path: 'seller/audit-logs',         element: <SellerAuditLogs /> },
              { path: 'seller/settings',           element: <SellerSettings /> },
            ],
          },
        ],
      },
    ],
  },

  // Catch-all
  { path: '*', element: <NotFound /> },
]);
