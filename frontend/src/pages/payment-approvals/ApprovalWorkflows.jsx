/**
 * Approval Workflows — /app/payment-approvals/workflows
 * Every ApprovalWorkflow instance for this org (not just the ones
 * assigned to me — see My Approvals for that view). Read-only visibility
 * into the real state machine PAY4-PAY9 built: which rule matched, how
 * far the approval/peer-review phases have progressed, and the terminal
 * outcome. Row click opens the full Detail/Timeline view (PAY12).
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { History } from 'lucide-react';
import { DataTable, StatusBadge, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAYMENT_APPROVAL_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { paymentApprovalApi, APPROVAL_WORKFLOW_STATUSES } from '@/services/api/paymentApprovalApi.js';
import './payment-approvals.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail?.message || err?.response?.data?.detail || err?.message || fallback; }

function formatCurrency(n, currency) {
  if (n === null || n === undefined) return '—';
  try { return new Intl.NumberFormat('en-US', { style: 'currency', currency: currency || 'USD', maximumFractionDigits: 2 }).format(n); }
  catch { return n; }
}

const STATUS_VARIANT = { PENDING: 'gray', IN_PROGRESS: 'info', APPROVED: 'success', REJECTED: 'danger', CANCELLED: 'gray', EXPIRED: 'danger', AUTO_APPROVED: 'success' };

export default function ApprovalWorkflows() {
  const navigate = useNavigate();
  const [rows, setRows] = useState([]);
  const [statusFilter, setStatusFilter] = useState('All');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    paymentApprovalApi
      .listWorkflows(statusFilter === 'All' ? {} : { status: statusFilter })
      .then(setRows)
      .catch((err) => setError(apiErrorMessage(err, 'Failed to load approval workflows.')))
      .finally(() => setLoading(false));
  }, [statusFilter]);

  useEffect(() => { load(); }, [load]);

  const columns = [
    { key: 'invoice_number', header: 'Invoice', render: (v, row) => <span className="par-name-cell">{v || `#${row.invoice_id}`}</span> },
    { key: 'vendor_name', header: 'Vendor', render: (v) => v || '—' },
    { key: 'total', header: 'Amount', render: (v, row) => <span className="pa-amount-cell">{formatCurrency(v, row.currency)}</span> },
    { key: 'rule_name', header: 'Rule', render: (v, row) => <span className="pa-rule-cell">{v ? `${v} v${row.rule_version}` : 'No rule matched'}</span> },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v.replace('_', ' ')}</StatusBadge> },
    {
      key: 'approval_progress', header: 'Approval',
      render: (_v, row) => (row.approval_required ? `${row.approval_count_completed} / ${row.approval_count_required}` : '—'),
    },
    {
      key: 'peer_review_progress', header: 'Peer Review',
      render: (_v, row) => (row.peer_review_required ? `${row.peer_review_count_completed} / ${row.peer_review_count_required}` : '—'),
    },
    { key: 'started_at', header: 'Started', render: (v) => (v ? formatDateTime(v) : '—') },
    { key: 'completed_at', header: 'Completed', render: (v) => (v ? formatDateTime(v) : '—') },
  ];

  return (
    <PermissionGate {...PAYMENT_APPROVAL_PERMISSIONS.APPROVAL_VIEW} showReason>
      <div className="pa-page">
        <div className="pa-header">
          <div>
            <h1 className="pa-title"><History size={22} /> Approval Workflows</h1>
            <p className="pa-subtitle">Every invoice evaluated by the Payment Approval engine — matched rule (or auto-approved), progress, and outcome.</p>
          </div>
          <div className="pa-actions">
            <select className="par-select" style={{ width: 180 }} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="All">All Statuses</option>
              {APPROVAL_WORKFLOW_STATUSES.map((s) => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
            </select>
          </div>
        </div>

        <div className="pa-table-panel">
          <DataTable
            columns={columns}
            data={rows}
            loading={loading}
            error={error}
            onRowClick={(row) => navigate(`/app/payment-approvals/workflows/${row.id}`)}
            emptyState={
              <EmptyState
                icon={<History size={28} />}
                title="No approval workflows yet"
                description="Workflows appear here once an invoice reaches the Payment Approval engine — whether it's auto-approved or matches a rule that needs a human."
              />
            }
          />
        </div>
      </div>
    </PermissionGate>
  );
}
