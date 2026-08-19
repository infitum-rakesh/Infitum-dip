import { createContext, useContext } from 'react';
import { AuthContext } from './AuthContext.jsx';
import { orgHasModule, getTrialDaysLeft, isOverLimit, usagePercent } from '@/utils/permissions.js';

export const OrganizationContext = createContext(null);

export function OrganizationProvider({ children }) {
  const { organization } = useContext(AuthContext);

  const value = {
    organization,
    licensedModules:      organization?.licensed_modules ?? [],
    sellerGrantedModules: organization?.seller_granted_modules ?? organization?.licensed_modules ?? [],
    licenseStatus:        organization?.license_status ?? null,
    maxUsers:             organization?.max_users ?? 0,
    usage:                organization?.usage ?? {},
    limits:               organization?.limits ?? {},
    trialDaysLeft:        getTrialDaysLeft(organization),
    hasModule:            (moduleKey) => orgHasModule(organization, moduleKey),
    isOverLimit:          (resource)  => isOverLimit(organization, resource),
    usagePercent:         (resource)  => usagePercent(organization, resource),
  };

  return (
    <OrganizationContext.Provider value={value}>
      {children}
    </OrganizationContext.Provider>
  );
}
