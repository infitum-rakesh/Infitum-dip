import { createContext, useContext, useMemo } from 'react';
import { AuthContext } from './AuthContext.jsx';
import { OrganizationContext } from './OrganizationContext.jsx';
import {
  evaluateAccess,
  canAccess,
  hasRole,
  isSeller,
  isAdmin,
  getEffectiveModules,
  isLicenseExpired,
  getTrialDaysLeft,
  isLicenseActive,
} from '@/utils/permissions.js';
import { LICENSE_STATUS } from '@/utils/constants.js';

export const PermissionContext = createContext(null);

export function PermissionProvider({ children }) {
  const { user } = useContext(AuthContext);
  const { organization, hasModule } = useContext(OrganizationContext);

  const value = useMemo(() => ({
    /** Full three-layer check — returns boolean */
    can: ({ module: m, action, role } = {}) =>
      evaluateAccess(user, organization, { module: m, action, minRole: role }).granted,

    /** Detailed evaluation — returns { granted, reason } */
    evaluate: (opts) => evaluateAccess(user, organization, opts),

    /** Module access (org + user layer) */
    hasModule,
    effectiveModules: getEffectiveModules(user, organization),

    /** Role helpers */
    hasRole: (role) => hasRole(user, role),
    isSeller: () => isSeller(user),
    isAdmin:  () => isAdmin(user),

    /** License state */
    licenseStatus:    organization?.license_status ?? null,
    isLicenseExpired: isLicenseExpired(organization),
    isLicenseActive:  isLicenseActive(organization),
    isTrialing:       organization?.license_status === LICENSE_STATUS.TRIAL,
    trialDaysLeft:    getTrialDaysLeft(organization),
  }), [user, organization, hasModule]);

  return (
    <PermissionContext.Provider value={value}>{children}</PermissionContext.Provider>
  );
}
