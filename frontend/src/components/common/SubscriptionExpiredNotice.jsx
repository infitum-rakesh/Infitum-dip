/**
 * SubscriptionExpiredNotice (SB30) — the real, professional module-denial
 * screen fed by GET /api/access/check, replacing PermissionGate's old
 * hardcoded REASON_MESSAGES text. Renders the backend's own message
 * (services/billing/messaging.py's build_access_message — spec sections
 * 27-29) verbatim, plus a real expiry date when the backend has one and
 * Renew/Contact Sales actions.
 */
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Lock, Mail, Phone, RefreshCw } from 'lucide-react';
import { accessApi } from '@/services/api/accessApi.js';
import { MODULE_META } from '@/utils/permissions.js';
import { formatDate } from '@/utils/formatters.js';
import './SubscriptionExpiredNotice.css';

export function SubscriptionExpiredNotice({ module: moduleKey, fallbackMessage }) {
  const [check, setCheck] = useState(null);
  const [contact, setContact] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([
      accessApi.checkAccess(moduleKey).catch(() => null),
      accessApi.getSalesContact().catch(() => null),
    ]).then(([checkResult, contactResult]) => {
      if (cancelled) return;
      setCheck(checkResult);
      setContact(contactResult);
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [moduleKey]);

  const meta = moduleKey ? MODULE_META[moduleKey] : null;
  const message = check?.message || fallbackMessage || 'You do not have access to this feature.';
  const hasContact = contact && (contact.email || contact.phone || contact.url);

  return (
    <div className="sen-wrap">
      <div className="sen-icon"><Lock size={22} /></div>
      {meta && <div className="sen-module">{meta.icon} {meta.label}</div>}
      {loading ? (
        <p className="sen-message">Checking your subscription…</p>
      ) : (
        <>
          <p className="sen-message">{message}</p>
          {check?.expired_at && (
            <p className="sen-expired-at">Access ended on {formatDate(check.expired_at)}.</p>
          )}
          <div className="sen-actions">
            <Link to="/app/billing" className="sen-btn sen-btn-primary">
              <RefreshCw size={14} /> View Billing &amp; Renew
            </Link>
            {hasContact && (
              <div className="sen-contact">
                {contact.name && <span className="sen-contact-name">{contact.name}</span>}
                {contact.email && (
                  <a href={`mailto:${contact.email}`} className="sen-btn sen-btn-secondary">
                    <Mail size={13} /> {contact.email}
                  </a>
                )}
                {contact.phone && (
                  <a href={`tel:${contact.phone}`} className="sen-btn sen-btn-secondary">
                    <Phone size={13} /> {contact.phone}
                  </a>
                )}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
