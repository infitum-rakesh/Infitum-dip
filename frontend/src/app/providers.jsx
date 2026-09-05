import { AuthProvider }         from '@/context/AuthContext.jsx';
import { OrganizationProvider }  from '@/context/OrganizationContext.jsx';
import { PermissionProvider }    from '@/context/PermissionContext.jsx';
import { AccessSummaryProvider } from '@/context/AccessSummaryContext.jsx';

/**
 * Providers — wraps the entire application.
 * Order matters: Auth → Org (reads auth) → Permission (reads both) →
 * AccessSummary (SB30, reads Auth directly — real-time modules-summary
 * shared by Sidebar/MainLayout/Dashboard).
 */
export function Providers({ children }) {
  return (
    <AuthProvider>
      <OrganizationProvider>
        <PermissionProvider>
          <AccessSummaryProvider>
            {children}
          </AccessSummaryProvider>
        </PermissionProvider>
      </OrganizationProvider>
    </AuthProvider>
  );
}
