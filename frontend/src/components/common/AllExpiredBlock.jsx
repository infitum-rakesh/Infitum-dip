/**
 * AllExpiredBlock (SB30) — full-page block shown in place of the routed
 * page content when GET /access/modules-summary reports
 * has_tenant_access: false (every subscription this org ever had has
 * expired/been cancelled — access_service.has_tenant_access(), spec
 * section 60). Billing/Settings/Contact-Sales stay reachable via
 * MainLayout's allowlist; this component is never rendered for those
 * routes in the first place (see MainLayout.jsx).
 */
import { Link } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { Lock, Mail, Phone, RefreshCw } from 'lucide-react';
import { accessApi } from '@/services/api/accessApi.js';
import './AllExpiredBlock.css';

export function AllExpiredBlock() {
  const [contact, setContact] = useState(null);

  useEffect(() => {
    accessApi.getSalesContact().then(setContact).catch(() => {});
  }, []);

  const hasContact = contact && (contact.email || contact.phone);

  return (
    <div className="aeb-wrap">
      <div className="aeb-card">
        <div className="aeb-icon"><Lock size={26} /></div>
        <h1 className="aeb-title">Your subscription has expired</h1>
        <p className="aeb-message">
          Every subscription on this account has expired, so access to the platform is currently paused.
          Renew a subscription to restore access — your data is safe and nothing has been deleted.
        </p>
        <div className="aeb-actions">
          <Link to="/app/billing" className="aeb-btn aeb-btn-primary">
            <RefreshCw size={15} /> View Billing &amp; Renew
          </Link>
          {hasContact && (
            <div className="aeb-contact">
              {contact.name && <span className="aeb-contact-name">{contact.name}</span>}
              {contact.email && (
                <a href={`mailto:${contact.email}`} className="aeb-btn aeb-btn-secondary">
                  <Mail size={14} /> {contact.email}
                </a>
              )}
              {contact.phone && (
                <a href={`tel:${contact.phone}`} className="aeb-btn aeb-btn-secondary">
                  <Phone size={14} /> {contact.phone}
                </a>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
