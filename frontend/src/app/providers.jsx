import { AuthProvider }         from '@/context/AuthContext.jsx';
import { OrganizationProvider }  from '@/context/OrganizationContext.jsx';
import { PermissionProvider }    from '@/context/PermissionContext.jsx';

/**
 * Providers — wraps the entire application.
 * Order matters: Auth → Org (reads auth) → Permission (reads both).
 */
export function Providers({ children }) {
  return (
    <AuthProvider>
      <OrganizationProvider>
        <PermissionProvider>
          {children}
        </PermissionProvider>
      </OrganizationProvider>
    </AuthProvider>
  );
}
