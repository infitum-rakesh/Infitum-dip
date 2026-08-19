/**
 * AccessDenied — shown when a non-Seller user directly navigates to a
 * /app/seller/* route (SELLER PORTAL spec section 15: "If a user directly
 * enters /seller/customers without permission: return HTTP 403 and
 * display 'Access Denied'"). The real enforcement is server-side
 * (core/deps.require_seller_action 403s every /api/seller/* call
 * regardless of what the frontend does) — this is the client-side
 * companion so a stray direct link doesn't even render the Seller UI shell.
 */
import { Link } from 'react-router-dom';
import { ShieldAlert, Home } from 'lucide-react';

export default function AccessDenied() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: '70vh', gap: 16, padding: 24, textAlign: 'center' }}>
      <div style={{ width: 64, height: 64, borderRadius: '50%', background: 'var(--color-danger-bg)', color: 'var(--color-danger)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <ShieldAlert size={30} />
      </div>
      <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--color-danger)', letterSpacing: 0.4 }}>HTTP 403</div>
      <h1 style={{ fontSize: 24, fontWeight: 700, color: 'var(--text-primary)' }}>Access Denied</h1>
      <p style={{ color: 'var(--text-muted)', maxWidth: 380 }}>
        The Seller Portal is restricted to Seller-tier accounts. Your account doesn't have permission to view this page.
      </p>
      <Link to="/app/dashboard" style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '9px 20px', background: 'var(--color-primary)', color: '#fff', borderRadius: 8, fontSize: 14, fontWeight: 500 }}>
        <Home size={15} /> Back to Dashboard
      </Link>
    </div>
  );
}
