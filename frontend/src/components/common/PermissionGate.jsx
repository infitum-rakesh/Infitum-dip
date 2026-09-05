/**
 * PermissionGate — wraps any UI in a declarative permission check.
 *
 * Usage:
 *   <PermissionGate module={MODULES.VALIDATION} action={ACTIONS.CREATE}>
 *     <Button>Create Rule</Button>
 *   </PermissionGate>
 *
 *   <PermissionGate role={ROLES.ORG_ADMIN} fallback={<p>Admin only</p>}>
 *     <AdminPanel />
 *   </PermissionGate>
 *
 *   <PermissionGate module={MODULES.RECONCILIATION} showReason>
 *     <ReconPage />
 *   </PermissionGate>
 */
import { usePermission } from '@/hooks/usePermission.js';
import { SubscriptionExpiredNotice } from './SubscriptionExpiredNotice.jsx';

// Fallback copy for non-module denials (role/action gates) — a
// SubscriptionExpiredNotice needs a module key to call GET /access/check
// against, so these stay static; module-gated denials render the real
// backend-driven notice instead (see DeniedPlaceholder below, SB30).
const REASON_MESSAGES = {
  not_authenticated:          'You must be logged in.',
  license_expired:            'Your licence has expired. Please renew to continue.',
  license_suspended:          'Your account has been suspended.',
  action_not_permitted_for_role: 'Your role does not have permission for this action.',
  insufficient_role:          'You do not have the required role for this.',
};

function DeniedPlaceholder({ reason, module: moduleKey, showReason }) {
  if (!showReason) return null;
  // SB30 — module-gated denials get the real, live SubscriptionExpiredNotice
  // (GET /api/access/check) instead of a hardcoded generic message.
  if (moduleKey) {
    return <SubscriptionExpiredNotice module={moduleKey} fallbackMessage={REASON_MESSAGES[reason]} />;
  }
  const message = REASON_MESSAGES[reason] ?? 'You do not have access to this feature.';
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', alignItems: 'center',
      justifyContent: 'center', gap: 8, padding: '24px 16px',
      background: '#FEF2F2', borderRadius: 10, border: '1px solid #FEE2E2',
      textAlign: 'center',
    }}>
      <span style={{ fontSize: 24 }}>🔒</span>
      <p style={{ fontSize: 13, color: '#7F1D1D', maxWidth: 320, lineHeight: 1.6 }}>{message}</p>
    </div>
  );
}

export function PermissionGate({
  children,
  module: moduleKey,
  action,
  role,
  fallback = null,
  showReason = false,
}) {
  const { evaluate } = usePermission();
  const { granted, reason } = evaluate({ module: moduleKey, action, minRole: role });

  if (granted) return children;

  if (fallback !== null) return fallback;
  return <DeniedPlaceholder reason={reason} module={moduleKey} showReason={showReason} />;
}

/**
 * LicenseBadge — inline status indicator for the current org's license.
 * Shows: Trial (N days), Active, Expiring (N days), Expired, Suspended
 */
export function LicenseBadge({ size = 'sm' }) {
  const { licenseStatus, trialDaysLeft, isTrialing } = usePermission();
  if (!licenseStatus) return null;

  const BADGE_STYLE = {
    trial:     { bg: '#FFFBEB', color: '#B45309', border: '#FDE68A' },
    active:    { bg: '#ECFDF5', color: '#047857', border: '#A7F3D0' },
    expiring:  { bg: '#FFF7ED', color: '#C2410C', border: '#FDBA74' },
    expired:   { bg: '#FEF2F2', color: '#B91C1C', border: '#FECACA' },
    suspended: { bg: '#F1F5F9', color: '#475569', border: '#CBD5E1' },
  };
  const style = BADGE_STYLE[licenseStatus] ?? BADGE_STYLE.active;
  const label = isTrialing && trialDaysLeft != null
    ? `Trial — ${trialDaysLeft}d left`
    : licenseStatus.charAt(0).toUpperCase() + licenseStatus.slice(1);

  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      padding: size === 'sm' ? '2px 8px' : '4px 12px',
      borderRadius: 999, fontSize: size === 'sm' ? 11 : 13,
      fontWeight: 600, letterSpacing: '0.3px',
      background: style.bg, color: style.color,
      border: `1px solid ${style.border}`,
    }}>
      {licenseStatus === 'trial' && '🕐 '}
      {licenseStatus === 'expired' && '⚠️ '}
      {label}
    </span>
  );
}
