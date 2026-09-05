/**
 * AccessSummaryContext (SB30) — one shared fetch of GET /access/modules-
 * summary per session, consumed by Sidebar (locked nav items), MainLayout
 * (all-expired block screen), and Dashboard (subscription widgets) so all
 * three agree on the same real, live snapshot instead of drifting.
 *
 * Skipped entirely for Seller/Super Admin (no organization_id — the
 * backend already returns an empty, always-accessible summary for them,
 * see api/access.py's modules_summary()) and for unauthenticated users.
 */
import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { AuthContext } from './AuthContext.jsx';
import { accessApi } from '@/services/api/accessApi.js';

const AccessSummaryContext = createContext(null);

const EMPTY_SUMMARY = { active_modules: [], expired_modules: {}, expiring_modules: [], has_tenant_access: true };

export function AccessSummaryProvider({ children }) {
  const { organization, isAuthenticated } = useContext(AuthContext);
  const [summary, setSummary] = useState(EMPTY_SUMMARY);
  const [loading, setLoading] = useState(false);

  const refetch = useCallback(() => {
    if (!isAuthenticated || !organization) {
      setSummary(EMPTY_SUMMARY);
      return Promise.resolve();
    }
    setLoading(true);
    return accessApi.getModulesSummary()
      .then(setSummary)
      .catch(() => setSummary(EMPTY_SUMMARY))
      .finally(() => setLoading(false));
  }, [isAuthenticated, organization]);

  useEffect(() => { refetch(); }, [refetch]);

  return (
    <AccessSummaryContext.Provider value={{ summary, loading, refetch }}>
      {children}
    </AccessSummaryContext.Provider>
  );
}

export function useAccessSummary() {
  const ctx = useContext(AccessSummaryContext);
  if (!ctx) throw new Error('useAccessSummary must be used inside <AccessSummaryProvider>');
  return ctx;
}
