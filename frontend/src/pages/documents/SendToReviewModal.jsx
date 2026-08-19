import { useEffect, useState } from 'react';
import { Button, Modal } from '@/components/common/index.js';

/**
 * SendToReviewModal — confirmation + optional reviewer assignment, used by
 * both Documents.jsx (row/bulk) and DocumentDetails.jsx before flagging a
 * document for human review.
 */
export function SendToReviewModal({ open, onClose, onConfirm, assignees = [], count = 1 }) {
  const [reviewerId, setReviewerId] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => { if (open) { setReviewerId(''); setNote(''); } }, [open]);

  const submit = async () => {
    setBusy(true);
    try {
      await onConfirm(reviewerId || null, note.trim() || null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={count > 1 ? `Send ${count} Documents to Review` : 'Send Document to Review'}
      size="sm"
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button><Button variant="primary" loading={busy} onClick={submit}>Send to Review</Button></>}
    >
      <p className="text-muted text-sm" style={{ marginBottom: 14 }}>
        {count > 1 ? 'These documents' : 'This document'} will be flagged for human review in the Review Queue.
      </p>
      <label className="doc-filter-label" style={{ display: 'block', marginBottom: 6 }}>Assign to reviewer (optional)</label>
      <select className="doc-select" style={{ width: '100%', marginBottom: 12 }} value={reviewerId} onChange={(e) => setReviewerId(e.target.value)}>
        <option value="">Leave unassigned</option>
        {assignees.map((u) => <option key={u.id} value={u.id}>{u.full_name}</option>)}
      </select>
      <label className="doc-filter-label" style={{ display: 'block', marginBottom: 6 }}>Note (optional)</label>
      <textarea
        className="doc-select" style={{ width: '100%', minHeight: 70, resize: 'vertical' }}
        value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why does this need review?"
      />
    </Modal>
  );
}
