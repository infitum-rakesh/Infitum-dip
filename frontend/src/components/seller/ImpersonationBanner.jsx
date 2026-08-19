/**
 * ImpersonationBanner — persistent strip shown while a Seller is
 * impersonating a customer admin (SELLER PORTAL spec: "Never allow silent
 * impersonation"). Visible on every page until the Seller explicitly exits.
 */
import { useNavigate } from 'react-router-dom';
import { ShieldAlert } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth.js';
import './ImpersonationBanner.css';

export function ImpersonationBanner() {
  const { isImpersonating, user, organization, exitImpersonation } = useAuth();
  const navigate = useNavigate();

  if (!isImpersonating) return null;

  const handleExit = () => {
    exitImpersonation();
    navigate('/app/seller/customers');
  };

  return (
    <div className="imp-banner">
      <ShieldAlert size={14} />
      <span>
        You are impersonating <strong>{user?.full_name}</strong> ({user?.email}) at <strong>{organization?.name}</strong>. This session is audited.
      </span>
      <button className="imp-banner-exit" onClick={handleExit}>Exit Impersonation</button>
    </div>
  );
}
