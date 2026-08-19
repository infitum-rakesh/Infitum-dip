/**
 * SellerAuditDetailModal — full event detail on row click.
 * Before/After values and details are already masked server-side (see
 * backend/app/services/seller/seller_audit_service.py _mask_*) — this
 * never re-exposes anything the API withheld. Mirrors
 * administration/audit-logs/AuditLogDetailModal.jsx, no Status field
 * (SellerAuditLog only ever records completed mutations — see backend
 * service docstring).
 */
import { useEffect, useState } from 'react';
import { ShieldAlert } from 'lucide-react';
import { Modal } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { sellerAuditLogApi } from '@/services/api/seller/sellerAuditLogApi.js';
import './seller-audit-logs.css';

export function SellerAuditDetailModal({ entryId, onClose }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!entryId) return;
    setDetail(null);
    setError(null);
    sellerAuditLogApi.get(entryId).then(setDetail).catch(() => setError('Failed to load this event.'));
  }, [entryId]);

  return (
    <Modal open={!!entryId} onClose={onClose} title="Seller Audit Event" size="lg">
      {error && <p className="sal-value-empty">{error}</p>}
      {!error && !detail && <p className="sal-value-empty">Loading…</p>}
      {detail && (
        <>
          <div className="sal-detail-grid">
            <div className="sal-detail-item">
              <span className="sal-detail-label">Event ID</span>
              <span className="sal-detail-value sal-detail-value-mono">#{detail.id}</span>
            </div>
            <div className="sal-detail-item">
              <span className="sal-detail-label">Timestamp</span>
              <span className="sal-detail-value">{formatDateTime(detail.timestamp)}</span>
            </div>
            <div className="sal-detail-item">
              <span className="sal-detail-label">Seller User</span>
              <span className="sal-detail-value">{detail.seller_user_name ?? 'System'}</span>
            </div>
            <div className="sal-detail-item">
              <span className="sal-detail-label">Customer</span>
              <span className="sal-detail-value">{detail.customer_name ?? '—'}</span>
            </div>
            <div className="sal-detail-item">
              <span className="sal-detail-label">Action</span>
              <span className="sal-detail-value">{detail.action}</span>
            </div>
            <div className="sal-detail-item">
              <span className="sal-detail-label">Module</span>
              <span className="sal-detail-value">{detail.module_label ?? detail.module ?? '—'}</span>
            </div>
            <div className="sal-detail-item">
              <span className="sal-detail-label">Resource</span>
              <span className="sal-detail-value sal-detail-value-mono">{detail.resource ?? '—'}</span>
            </div>
            <div className="sal-detail-item">
              <span className="sal-detail-label">IP Address</span>
              <span className="sal-detail-value sal-detail-value-mono">{detail.ip_address ?? '—'}</span>
            </div>
          </div>

          {detail.details && (
            <>
              <div className="sal-detail-section-title">Details</div>
              <div className="sal-value-box">{detail.details}</div>
            </>
          )}

          <div className="sal-detail-section-title">Before Value</div>
          {detail.before_value ? <pre className="sal-value-box">{JSON.stringify(detail.before_value, null, 2)}</pre> : <p className="sal-value-empty">No before value recorded.</p>}

          <div className="sal-detail-section-title">After Value</div>
          {detail.after_value ? <pre className="sal-value-box">{JSON.stringify(detail.after_value, null, 2)}</pre> : <p className="sal-value-empty">No after value recorded.</p>}

          <div className="sal-mask-note">
            <ShieldAlert size={12} /> Passwords, tokens and credentials are always masked and never shown here.
          </div>
        </>
      )}
    </Modal>
  );
}
