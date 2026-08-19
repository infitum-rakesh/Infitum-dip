import { useContext } from 'react';
import { PermissionContext } from '@/context/PermissionContext.jsx';

/**
 * Access permission helpers anywhere in the component tree.
 *
 * const { can, evaluate, hasRole, effectiveModules, trialDaysLeft } = usePermission();
 *
 * // Three-layer check:
 * can({ module: MODULES.VALIDATION, action: ACTIONS.CREATE })
 *
 * // Detailed result with reason:
 * const { granted, reason } = evaluate({ module: MODULES.RECONCILIATION })
 */
export function usePermission() {
  const ctx = useContext(PermissionContext);
  if (!ctx) throw new Error('usePermission must be used inside <PermissionProvider>');
  return ctx;
}
