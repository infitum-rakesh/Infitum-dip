/**
 * My Approvals — /app/payment-approvals
 * The assignee-facing worklist for the Payment Approval Workflow feature
 * (PAY2-PAY9): every ApprovalWorkflowStep/PeerReviewStep currently
 * ASSIGNED to the signed-in user, with enough invoice context (number,
 * vendor, amount) to actually decide — see paymentApprovalApi.myApprovals()
 * -> GET /approvals/my-approvals. Approve/Reject act on the real
 * workflow engine (approval_workflow_engine_service.act_on_approval_step);
 * Review/Flag act on the peer-review counterpart. A REJECT immediately
 * terminates the whole workflow (unconditional short-circuit, see that
 * module's docstring) and blocks the invoice from ever reaching the ERP
 * (invoice_erp_service.post_invoice()'s PAY9 gate).
 *
 * Rule Configuration and Approval Detail/Timeline pages are later
 * phases — this page is deliberately just the worklist + inline actions,
 * the smallest genuinely useful surface for a real approver today.
 */
import { useCallback, useEffect, useState } from 'react';
import { BadgeCheck, Check, X, Eye, Flag } from 'lucide-react';
import { Button, StatusBadge, DataTable, EmptyState, Modal, PermissionGate } from '@/components/common/index.js';
import { PAYMENT_APPROVAL_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { paymentApprovalApi } from '@/services/api/paymentApprovalApi.js';
import './payment-approvals.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail?.message || err?.response?.data?.detail || err?.message || fallback; }

function formatCurrency(n, currency = 'USD') {
  if (n === null || n === undefined) return '—';
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(n);
  } catch {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(n);
  }
}

function isOverdue(dueAt) { return !!dueAt && new Date(dueAt).getTime() < Date.now(); }

// TALLY-H — a row's human-readable subject for confirm dialogs, generalized
// beyond "invoice #N" since entity_type=TALLY_VOUCHER rows have no invoice_id.
function subjectLabel(row) {
  if (row.invoice_number) return row.invoice_number;
  if (row.entity_type === 'TALLY_VOUCHER') return `${row.voucher_type || 'Tally voucher'} for ${row.voucher_party_ledger || 'unknown party'}`;
  return `invoice #${row.invoice_id}`;
}

const STEP_KIND_LABEL = { APPROVAL: 'Approval', PEER_REVIEW: 'Peer Review' };

export default function MyApprovals() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // { row, kind: 'reject'|'flag' } while the comment modal is open —
  // approve/review fire immediately (window.confirm), reject/flag
  // collect an optional reason first, same split as CustomerDetails.jsx's
  // activate-vs-suspend pattern elsewhere in this app.
  const [commentModal, setCommentModal] = useState(null);
  const [comment, setComment] = useState('');
  const [actionBusy, setActionBusy] = useState(null); // step_id + '_' + action, so only ONE row's button spins
  const [actionError, setActionError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    paymentApprovalApi
      .myApprovals()
      .then(setRows)
      .catch((err) => setError(apiErrorMessage(err, 'Failed to load your approvals. Please try again.')))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const runApprove = async (row) => {
    if (!window.confirm(`Approve ${subjectLabel(row)} for ${formatCurrency(row.total, row.currency)}?`)) return;
    setActionBusy(`${row.step_id}_approve`); setActionError(null);
    try {
      await paymentApprovalApi.approveStep(row.workflow_id, row.step_id);
      load();
    } catch (err) {
      setActionError(apiErrorMessage(err, 'Could not approve this step.'));
    } finally {
      setActionBusy(null);
    }
  };

  const runReview = async (row) => {
    if (!window.confirm(`Mark ${subjectLabel(row)} as reviewed?`)) return;
    setActionBusy(`${row.step_id}_review`); setActionError(null);
    try {
      await paymentApprovalApi.reviewPeerStep(row.workflow_id, row.step_id);
      load();
    } catch (err) {
      setActionError(apiErrorMessage(err, 'Could not record this review.'));
    } finally {
      setActionBusy(null);
    }
  };

  const openReject = (row) => { setCommentModal({ row, kind: 'reject' }); setComment(''); };
  const openFlag = (row) => { setCommentModal({ row, kind: 'flag' }); setComment(''); };

  const submitCommentModal = async () => {
    const { row, kind } = commentModal;
    setActionBusy(`${row.step_id}_${kind}`); setActionError(null);
    try {
      if (kind === 'reject') await paymentApprovalApi.rejectStep(row.workflow_id, row.step_id, comment || undefined);
      else await paymentApprovalApi.flagPeerStep(row.workflow_id, row.step_id, comment || undefined);
      setCommentModal(null);
      load();
    } catch (err) {
      setActionError(apiErrorMessage(err, kind === 'reject' ? 'Could not reject this step.' : 'Could not flag this step.'));
    } finally {
      setActionBusy(null);
    }
  };

  const columns = [
    {
      key: 'invoice_number', header: 'Item',
      // TALLY-H — entity_type=TALLY_VOUCHER rows have no invoice_number
      // (they're not an invoice); show the voucher's own identity instead.
      render: (v, row) => row.entity_type === 'TALLY_VOUCHER'
        ? <span className="pa-invoice-cell">{row.voucher_type || 'Tally Voucher'} — {row.voucher_party_ledger || 'Unknown party'}</span>
        : <span className="pa-invoice-cell">{v || `#${row.invoice_id}`}</span>,
    },
    {
      key: 'vendor_name', header: 'Vendor',
      render: (v, row) => v || (row.entity_type === 'TALLY_VOUCHER' ? row.voucher_party_ledger : null) || '—',
    },
    { key: 'total', header: 'Amount', render: (v, row) => <span className="pa-amount-cell">{formatCurrency(v, row.currency)}</span> },
    { key: 'rule_name', header: 'Rule', render: (v) => <span className="pa-rule-cell">{v}</span> },
    {
      key: 'step_kind', header: 'Type',
      render: (v) => <StatusBadge variant={v === 'PEER_REVIEW' ? 'info' : 'gray'} dot>{STEP_KIND_LABEL[v] ?? v}</StatusBadge>,
    },
    { key: 'step_number', header: 'Step #' },
    { key: 'assigned_at', header: 'Assigned', render: (v) => (v ? formatDateTime(v) : '—') },
    {
      key: 'due_at', header: 'Due',
      render: (v) => (v ? <span className={isOverdue(v) ? 'pa-overdue' : ''}>{formatDateTime(v)}</span> : '—'),
    },
    {
      key: 'actions', header: '', width: '220px',
      render: (_v, row) => (
        <PermissionGate {...PAYMENT_APPROVAL_PERMISSIONS.APPROVAL_ACT} fallback={null}>
          <div className="pa-row-actions">
            {row.step_kind === 'APPROVAL' ? (
              <>
                <Button variant="success" size="sm" icon={<Check size={13} />} loading={actionBusy === `${row.step_id}_approve`} disabled={!!actionBusy} onClick={() => runApprove(row)}>
                  Approve
                </Button>
                <Button variant="danger" size="sm" icon={<X size={13} />} loading={actionBusy === `${row.step_id}_reject`} disabled={!!actionBusy} onClick={() => openReject(row)}>
                  Reject
                </Button>
              </>
            ) : (
              <>
                <Button variant="secondary" size="sm" icon={<Eye size={13} />} loading={actionBusy === `${row.step_id}_review`} disabled={!!actionBusy} onClick={() => runReview(row)}>
                  Reviewed
                </Button>
                <Button variant="warning" size="sm" icon={<Flag size={13} />} loading={actionBusy === `${row.step_id}_flag`} disabled={!!actionBusy} onClick={() => openFlag(row)}>
                  Flag
                </Button>
              </>
            )}
          </div>
        </PermissionGate>
      ),
    },
  ];

  return (
    <PermissionGate {...PAYMENT_APPROVAL_PERMISSIONS.APPROVAL_VIEW} showReason>
      <div className="pa-page">
        <div className="pa-header">
          <div>
            <h1 className="pa-title"><BadgeCheck size={22} /> My Approvals</h1>
            <p className="pa-subtitle">Invoices routed to you by a Payment Approval rule — approve, reject, review, or flag.</p>
          </div>
        </div>

        {actionError && <div className="pa-banner-error">{actionError}</div>}

        <div className="pa-table-panel">
          <DataTable
            columns={columns}
            data={rows}
            loading={loading}
            error={error}
            emptyState={
              <EmptyState
                icon={<BadgeCheck size={28} />}
                title="Nothing waiting on you"
                description="Steps assigned to you by a Payment Approval rule will show up here."
              />
            }
          />
        </div>
      </div>

      <Modal
        open={!!commentModal}
        onClose={() => setCommentModal(null)}
        title={commentModal?.kind === 'reject' ? 'Reject Step' : 'Flag for Attention'}
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setCommentModal(null)}>Cancel</Button>
            <Button
              variant={commentModal?.kind === 'reject' ? 'danger' : 'warning'}
              loading={commentModal && actionBusy === `${commentModal.row.step_id}_${commentModal.kind}`}
              onClick={submitCommentModal}
            >
              {commentModal?.kind === 'reject' ? 'Reject' : 'Flag'}
            </Button>
          </>
        }
      >
        <p className="pa-modal-hint">
          {commentModal?.kind === 'reject'
            ? 'Rejecting immediately terminates this workflow — the invoice cannot be posted to the ERP.'
            : 'Flagging is advisory — it does not block the workflow from completing, but is recorded prominently in the audit trail.'}
        </p>
        <textarea
          className="pa-comment-input"
          rows={3}
          placeholder="Reason (optional)…"
          value={comment}
          onChange={(e) => setComment(e.target.value)}
        />
      </Modal>
    </PermissionGate>
  );
}
