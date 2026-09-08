/**
 * Approval Workflow Detail — /app/payment-approvals/workflows/:workflowId
 * The full history of one ApprovalWorkflow instance: header summary,
 * approval steps, peer review steps, and a chronological audit-log
 * timeline (PAY4-PAY9's own ApprovalWorkflowAuditLog rows — created on
 * every real state transition, nothing synthesized for display).
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, History } from 'lucide-react';
import { Button, StatusBadge, PermissionGate, LoadingSpinner } from '@/components/common/index.js';
import { PAYMENT_APPROVAL_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { paymentApprovalApi } from '@/services/api/paymentApprovalApi.js';
import './payment-approvals.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail?.message || err?.response?.data?.detail || err?.message || fallback; }

function formatCurrency(n, currency) {
  if (n === null || n === undefined) return '—';
  try { return new Intl.NumberFormat('en-US', { style: 'currency', currency: currency || 'USD', maximumFractionDigits: 2 }).format(n); }
  catch { return n; }
}

const WORKFLOW_STATUS_VARIANT = { PENDING: 'gray', IN_PROGRESS: 'info', APPROVED: 'success', REJECTED: 'danger', CANCELLED: 'gray', EXPIRED: 'danger', AUTO_APPROVED: 'success' };
const APPROVAL_STEP_VARIANT = { PENDING: 'gray', ASSIGNED: 'info', APPROVED: 'success', REJECTED: 'danger', SKIPPED: 'gray', ESCALATED: 'warning', EXPIRED: 'danger' };
const PEER_REVIEW_STEP_VARIANT = { PENDING: 'gray', ASSIGNED: 'info', REVIEWED: 'success', FLAGGED: 'warning', SKIPPED: 'gray', ESCALATED: 'warning', EXPIRED: 'danger' };
const AUDIT_EVENT_VARIANT = {
  CREATED: 'info', STEP_ASSIGNED: 'info', STEP_APPROVED: 'success', STEP_REJECTED: 'danger', STEP_SKIPPED: 'gray',
  PEER_REVIEW_ASSIGNED: 'info', PEER_REVIEW_REVIEWED: 'success', PEER_REVIEW_FLAGGED: 'warning',
  ESCALATED: 'warning', REMINDER_SENT: 'gray', COMPLETED: 'success', CANCELLED: 'gray', AUTO_APPROVED: 'success',
};

// Real bug found live (user-reported): this rendered "User #3" even
// though the backend has the real person's name in hand — get_workflow()
// now stamps approver_user_name/assigned_user_name (see api/approvals.py
// _user_name_map()); "User #{id}" is kept only as the fallback for the
// rare case a referenced user was since deleted (name genuinely missing).
function ApproverLabel({ step }) {
  if (step.approver_type === 'USER') return step.approver_user_name || `User #${step.approver_user_id ?? '—'}`;
  if (step.approver_type === 'ROLE') return `Role #${step.approver_role_id ?? '—'}`;
  if (step.approver_type === 'DEPARTMENT') return `Dept: ${step.approver_department ?? '—'}`;
  if (step.approver_type === 'POOL') return `Pool (${step.approver_pool?.length ?? 0} users)`;
  return step.approver_type;
}

function AssignedToLabel({ step }) {
  if (!step.assigned_user_id) return '—';
  return step.assigned_user_name || `User #${step.assigned_user_id}`;
}

export default function ApprovalWorkflowDetail() {
  const { workflowId } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    paymentApprovalApi
      .getWorkflow(workflowId)
      .then(setDetail)
      .catch((err) => setError(apiErrorMessage(err, 'Failed to load this workflow.')))
      .finally(() => setLoading(false));
  }, [workflowId]);

  useEffect(() => { load(); }, [load]);

  return (
    <PermissionGate {...PAYMENT_APPROVAL_PERMISSIONS.APPROVAL_VIEW} showReason>
      <div className="pa-page">
        <div className="pa-header">
          <div>
            <Button variant="ghost" size="sm" icon={<ArrowLeft size={14} />} onClick={() => navigate('/app/payment-approvals/workflows')}>
              Back to Workflows
            </Button>
            <h1 className="pa-title" style={{ marginTop: 8 }}><History size={22} /> Approval Workflow #{workflowId}</h1>
          </div>
        </div>

        {loading && <LoadingSpinner />}
        {error && <div className="pa-banner-error">{error}</div>}

        {detail && (
          <>
            <div className="pad-summary-card">
              <div className="pad-summary-grid">
                <div><div className="pad-field-label">Invoice</div><div className="pad-field-value">{detail.invoice_number || `#${detail.invoice_id}`}</div></div>
                <div><div className="pad-field-label">Vendor</div><div className="pad-field-value">{detail.vendor_name || '—'}</div></div>
                <div><div className="pad-field-label">Amount</div><div className="pad-field-value">{formatCurrency(detail.total, detail.currency)}</div></div>
                <div><div className="pad-field-label">Rule</div><div className="pad-field-value">{detail.rule_name ? `${detail.rule_name} v${detail.rule_version}` : 'No rule matched'}</div></div>
                <div><div className="pad-field-label">Status</div><div className="pad-field-value"><StatusBadge variant={WORKFLOW_STATUS_VARIANT[detail.status] ?? 'gray'} dot>{detail.status.replace('_', ' ')}</StatusBadge></div></div>
                <div><div className="pad-field-label">Started</div><div className="pad-field-value">{detail.started_at ? formatDateTime(detail.started_at) : '—'}</div></div>
                <div><div className="pad-field-label">Completed</div><div className="pad-field-value">{detail.completed_at ? formatDateTime(detail.completed_at) : '—'}</div></div>
                {detail.decision_reason && (
                  <div className="pad-summary-full"><div className="pad-field-label">Decision Reason</div><div className="pad-field-value">{detail.decision_reason}</div></div>
                )}
              </div>
            </div>

            {detail.approval_required && (
              <div className="pad-section">
                <div className="par-section-title" style={{ marginTop: 0, border: 'none' }}>Approval Steps ({detail.approval_mode}, {detail.approval_count_completed}/{detail.approval_count_required})</div>
                <table className="par-sim-table pad-steps-table">
                  <thead><tr><th>#</th><th>Approver</th><th>Assigned To</th><th>Status</th><th>Comment</th><th>Acted</th><th>Due</th></tr></thead>
                  <tbody>
                    {detail.steps.map((s) => (
                      <tr key={s.id}>
                        <td>{s.step_number}</td>
                        <td><ApproverLabel step={s} /></td>
                        <td><AssignedToLabel step={s} /></td>
                        <td><StatusBadge variant={APPROVAL_STEP_VARIANT[s.status] ?? 'gray'} dot>{s.status}</StatusBadge></td>
                        <td>{s.decision_comment || '—'}</td>
                        <td>{s.acted_at ? formatDateTime(s.acted_at) : '—'}</td>
                        <td>{s.due_at ? formatDateTime(s.due_at) : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {detail.peer_review_required && (
              <div className="pad-section">
                <div className="par-section-title" style={{ marginTop: 0, border: 'none' }}>Peer Review Steps ({detail.peer_review_mode}, {detail.peer_review_count_completed}/{detail.peer_review_count_required})</div>
                <table className="par-sim-table pad-steps-table">
                  <thead><tr><th>#</th><th>Reviewer</th><th>Assigned To</th><th>Status</th><th>Comment</th><th>Reviewed</th><th>Due</th></tr></thead>
                  <tbody>
                    {detail.peer_review_steps.map((s) => (
                      <tr key={s.id}>
                        <td>{s.step_number}</td>
                        <td><ApproverLabel step={s} /></td>
                        <td><AssignedToLabel step={s} /></td>
                        <td><StatusBadge variant={PEER_REVIEW_STEP_VARIANT[s.status] ?? 'gray'} dot>{s.status}</StatusBadge></td>
                        <td>{s.review_comment || '—'}</td>
                        <td>{s.reviewed_at ? formatDateTime(s.reviewed_at) : '—'}</td>
                        <td>{s.due_at ? formatDateTime(s.due_at) : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <div className="pad-section">
              <div className="par-section-title" style={{ marginTop: 0, border: 'none' }}>Audit Timeline</div>
              <div className="pad-timeline">
                {detail.audit_log.map((e) => (
                  <div key={e.id} className="pad-timeline-item">
                    <div className="pad-timeline-dot" />
                    <div className="pad-timeline-body">
                      <div className="pad-timeline-header">
                        <StatusBadge variant={AUDIT_EVENT_VARIANT[e.event_type] ?? 'gray'} dot>{e.event_type.replace(/_/g, ' ')}</StatusBadge>
                        <span className="pad-timeline-time">{formatDateTime(e.created_at)}</span>
                      </div>
                      {e.message && <div className="pad-timeline-message">{e.message}</div>}
                      {e.actor_id && <div className="pad-timeline-actor">by user #{e.actor_id}</div>}
                    </div>
                  </div>
                ))}
                {detail.audit_log.length === 0 && <div className="pad-field-label">No audit events yet.</div>}
              </div>
            </div>
          </>
        )}
      </div>
    </PermissionGate>
  );
}
