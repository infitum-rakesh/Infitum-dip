import { AuthProvider }         from '@/context/AuthContext.jsx';
import { OrganizationProvider }  from '@/context/OrganizationContext.jsx';
import { PermissionProvider }    from '@/context/PermissionContext.jsx';
import { AccessSummaryProvider } from '@/context/AccessSummaryContext.jsx';
import { SqlWorkspaceProvider }  from '@/context/SqlWorkspaceContext.jsx';

/**
 * Providers — wraps the entire application.
 * Order matters: Auth → Org (reads auth) → Permission (reads both) →
 * AccessSummary (SB30, reads Auth directly — real-time modules-summary
 * shared by Sidebar/MainLayout/Dashboard).
 *
 * SqlWorkspaceProvider is independent of the auth chain — it just needs
 * to live above the router's route-driven page tree so navigating away
 * from /app/sql-workspace and back doesn't unmount-and-lose its tabs
 * (SQL text, in-flight run, result). See SqlWorkspaceContext.jsx.
 */
export function Providers({ children }) {
  return (
    <AuthProvider>
      <OrganizationProvider>
        <PermissionProvider>
          <AccessSummaryProvider>
            <SqlWorkspaceProvider>
              {children}
            </SqlWorkspaceProvider>
          </AccessSummaryProvider>
        </PermissionProvider>
      </OrganizationProvider>
    </AuthProvider>
  );
}
