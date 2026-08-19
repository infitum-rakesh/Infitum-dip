import { Navigate, Outlet } from 'react-router-dom';
import { createBrowserRouter } from 'react-router-dom';

// Home / Landing page
import Home from '@/pages/home/Home.jsx';

// Layouts
import { AuthLayout }  from '@/layouts/AuthLayout.jsx';
import { MainLayout }  from '@/layouts/MainLayout.jsx';

// Auth pages
import Login          from '@/pages/auth/Login.jsx';
import Register       from '@/pages/auth/Register.jsx';
import ForgotPassword from '@/pages/auth/ForgotPassword.jsx';

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

// Data Quality (implemented)
import Validation     from '@/pages/validation/Validation.jsx';
import Reconciliation from '@/pages/reconciliation/Reconciliation.jsx';

// SQL Workspace (implemented)
import SqlWorkspace from '@/pages/sql-workspace/SqlWorkspace.jsx';

// Pipeline (implemented)
import PipelineBuilder from '@/pages/pipeline/PipelineBuilder.jsx';
import PipelineRuns    from '@/pages/pipeline/PipelineRuns.jsx';

// Schedules (implemented)
import Schedules from '@/pages/schedules/Schedules.jsx';

// Seller Portal (Phase 1: Dashboard, Phase 2: Customers, Phase 3:
// Subscriptions, Phase 4: Purchased Modules, Phase 5: License Management
// implemented; rest are placeholders per SELLER PORTAL spec's phased
// implementation strategy)
import SellerDashboard from '@/pages/seller/dashboard/SellerDashboard.jsx';
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
  const { isAuthenticated, isInitialised } = useAuth();
  if (!isInitialised) return <FullPageSpinner />;
  return isAuthenticated ? <Outlet /> : <Navigate to="/login" replace />;
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
  const { isAuthenticated } = useAuth();
  return isAuthenticated ? <Navigate to="/app/dashboard" replace /> : <Outlet />;
}

/** Shorthand to produce a lazy placeholder element */
const ph = (title, icon, description, phase) => (
  <PlaceholderPage title={title} icon={icon} description={description} phase={phase} />
);

export const router = createBrowserRouter([
  // Landing page — public, visible to all (authenticated users see dashboard link)
  { path: '/', element: <Home /> },

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

          // Data Quality
          { path: 'validation',              element: <Validation /> },
          { path: 'validation/:ruleSetId',   element: <Validation /> },
          { path: 'reconciliation',          element: <Reconciliation /> },
          { path: 'reconciliation/:jobId',   element: <Reconciliation /> },

          // Observability
          { path: 'lineage',    element: ph('Data Lineage', '🌐', 'Trace data from source document to final target.', 'Phase 12') },
          { path: 'monitoring', element: ph('Monitoring',   '📊', 'Monitor pipeline health, freshness and anomalies.', 'Phase 12') },
          { path: 'incidents',              element: <Incidents /> },
          { path: 'incidents/:incidentId',  element: <IncidentDetails /> },

          // Operations
          { path: 'pipeline-runs', element: <PipelineRuns /> },
          { path: 'schedules',     element: <Schedules /> },

          // Admin
          { path: 'admin/users',            element: <Users /> },
          { path: 'admin/users/:userId',    element: <UserDetails /> },
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
