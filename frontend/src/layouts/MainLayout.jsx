import { useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { Sidebar } from '@/components/navigation/Sidebar.jsx';
import { Header }  from '@/components/navigation/Header.jsx';
import { ImpersonationBanner } from '@/components/seller/ImpersonationBanner.jsx';
import { AllExpiredBlock } from '@/components/common/AllExpiredBlock.jsx';
import { useAccessSummary } from '@/context/AccessSummaryContext.jsx';
import { useAuth } from '@/hooks/useAuth.js';
import './MainLayout.css';

// SB30 spec section 60's explicit non-billable-route allowlist — reachable
// even when has_tenant_access is false, so a customer can always see why
// access is blocked and actually renew. Deliberately does NOT include
// /app/dashboard — that page shows real KPI data pulled from the very
// modules this screen exists to gate, so an all-expired org must see the
// block immediately on landing, not a working Dashboard.
const ALLOWLIST_PREFIXES = ['/app/billing', '/app/admin/settings'];

export function MainLayout() {
  const [collapsed, setCollapsed] = useState(false);
  const { organization } = useAuth();
  const { summary } = useAccessSummary();
  const location = useLocation();

  const isAllowlisted = ALLOWLIST_PREFIXES.some((p) => location.pathname.startsWith(p));
  // has_tenant_access is always true for Seller/Super Admin (no org — see
  // api/access.py's modules_summary()), so this only ever blocks a real
  // org-scoped customer whose every subscription has actually lapsed.
  const blocked = !!organization && summary.has_tenant_access === false && !isAllowlisted;

  return (
    <div className="app-shell">
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
      <div className="app-main">
        <ImpersonationBanner />
        <Header collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
        <main className="app-content">
          {blocked ? <AllExpiredBlock /> : <Outlet />}
        </main>
      </div>
    </div>
  );
}
