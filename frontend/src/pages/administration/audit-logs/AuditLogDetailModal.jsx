/**
 * AuditLogDetailModal — full event detail on row click.
 * Before/After values and details are already masked server-side (see
 * backend/app/services/audit_log_service.py _mask_*) — this never
 * re-exposes anything the API withheld.
 */
import { useEffect, useState } from 'react';
import { ShieldAlert } from 'lucide-react';
import { Modal, StatusBadge } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { auditLogApi } from '@/services/api/auditLogApi.js';
import './audit-logs.css';

const STATUS_VARIANT = { SUCCESS: 'success', FAILURE: 'danger', WARNING: 'warning', INFO: 'info' };

export function AuditLogDetailModal({ entryId, onClose }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!entryId) return;
    setDetail(null);
    setError(null);
    auditLogApi.get(entryId).then(setDetail).catch(() => setError('Failed to load this event.'));
  }, [entryId]);

  return (
    <Modal open={!!entryId} onClose={onClose} title="Audit Event" size="lg">
      {error && <p className="aud-value-empty">{error}</p>}
      {!error && !detail && <p className="aud-value-empty">Loading…</p>}
      {detail && (
        <>
          <div className="aud-detail-grid">
            <div className="aud-detail-item">
              <span className="aud-detail-label">Event ID</span>
              <span className="aud-detail-value aud-detail-value-mono">{detail.id}</span>
            </div>
            <div className="aud-detail-item">
              <span className="aud-detail-label">Timestamp</span>
              <span className="aud-detail-value">{formatDateTime(detail.timestamp)}</span>
            </div>
            <div className="aud-detail-item">
              <span className="aud-detail-label">User</span>
              <span className="aud-detail-value">{detail.user_name ?? 'System'}</span>
            </div>
            <div className="aud-detail-item">
              <span className="aud-detail-label">Status</span>
              <span><StatusBadge variant={STATUS_VARIANT[detail.status] ?? 'gray'} dot>{detail.status}</StatusBadge></span>
            </div>
            <div className="aud-detail-item">
              <span className="aud-detail-label">Action</span>
              <span className="aud-detail-value">{detail.action}</span>
            </div>
            <div className="aud-detail-item">
              <span className="aud-detail-label">Module</span>
              <span className="aud-detail-value">{detail.module}</span>
            </div>
            <div className="aud-detail-item">
              <span className="aud-detail-label">Resource</span>
              <span className="aud-detail-value aud-detail-value-mono">{detail.resource ?? '—'}</span>
            </div>
            <div className="aud-detail-item">
              <span className="aud-detail-label">IP Address</span>
              <span className="aud-detail-value aud-detail-value-mono">{detail.ip_address ?? '—'}</span>
            </div>
            <div className="aud-detail-item" style={{ gridColumn: '1 / -1' }}>
              <span className="aud-detail-label">User Agent</span>
              <span className="aud-detail-value aud-detail-value-mono">{detail.user_agent ?? '—'}</span>
            </div>
          </div>

          {detail.details && (
            <>
              <div className="aud-detail-section-title">Details</div>
              <div className="aud-value-box">{detail.details}</div>
            </>
          )}

          <div className="aud-detail-section-title">Before Value</div>
          {detail.before_value ? <pre className="aud-value-box">{JSON.stringify(detail.before_value, null, 2)}</pre> : <p className="aud-value-empty">No before value recorded.</p>}

          <div className="aud-detail-section-title">After Value</div>
          {detail.after_value ? <pre className="aud-value-box">{JSON.stringify(detail.after_value, null, 2)}</pre> : <p className="aud-value-empty">No after value recorded.</p>}

          <div className="aud-mask-note">
            <ShieldAlert size={12} /> Passwords, tokens and credentials are always masked and never shown here.
          </div>
        </>
      )}
    </Modal>
  );
}
