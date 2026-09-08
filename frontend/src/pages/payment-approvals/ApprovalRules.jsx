/**
 * Approval Rules — /app/payment-approvals/rules
 * Admin configuration surface for the Payment Approval rule engine
 * (PAY2/PAY3/PAY8). Rules are versioned and never mutated in place —
 * "Edit" here always publishes a new version via the same POST
 * /approvals/rules the create flow uses (see RuleFormModal); this list
 * only ever shows the current version of each rule (is_current=true).
 */
import { useCallback, useEffect, useState } from 'react';
import { Settings, Plus, PlayCircle, Power, PowerOff, Pencil } from 'lucide-react';
import { Button, StatusBadge, DataTable, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAYMENT_APPROVAL_PERMISSIONS } from '@/utils/constants.js';
import { paymentApprovalApi } from '@/services/api/paymentApprovalApi.js';
import { RuleFormModal } from './RuleFormModal.jsx';
import { SimulateRuleModal } from './SimulateRuleModal.jsx';
import './payment-approvals.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail?.message || err?.response?.data?.detail || err?.message || fallback; }

function formatCurrency(n, currency) {
  if (n === null || n === undefined) return '—';
  try { return new Intl.NumberFormat('en-US', { style: 'currency', currency: currency || 'USD', maximumFractionDigits: 2 }).format(n); }
  catch { return n; }
}

const STATUS_VARIANT = { ACTIVE: 'success', INACTIVE: 'gray' };
const ACTION_VARIANT = { REQUIRE_APPROVAL: 'warning', AUTO_APPROVE: 'success' };

export default function ApprovalRules() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [formOpen, setFormOpen] = useState(false);
  const [editingRule, setEditingRule] = useState(null); // null = create; object = publish-new-version
  const [simulateOpen, setSimulateOpen] = useState(false);
  const [actionBusy, setActionBusy] = useState(null);
  const [actionError, setActionError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    paymentApprovalApi
      .listRules()
      .then(setRows)
      .catch((err) => setError(apiErrorMessage(err, 'Failed to load approval rules.')))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const openCreate = () => { setEditingRule(null); setFormOpen(true); };
  const openEdit = async (row) => {
    try {
      const detail = await paymentApprovalApi.getRule(row.id);
      setEditingRule(detail);
      setFormOpen(true);
    } catch (err) {
      setActionError(apiErrorMessage(err, 'Could not load this rule.'));
    }
  };

  const toggleStatus = async (row) => {
    const next = row.status === 'ACTIVE' ? 'INACTIVE' : 'ACTIVE';
    setActionBusy(row.id);
    setActionError(null);
    try {
      await paymentApprovalApi.setRuleStatus(row.id, next);
      load();
    } catch (err) {
      setActionError(apiErrorMessage(err, 'Could not change rule status.'));
    } finally {
      setActionBusy(null);
    }
  };

  const columns = [
    { key: 'name', header: 'Name', render: (v, row) => <span className="par-name-cell">{v}<span className="par-version-tag">v{row.version}</span></span> },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'priority', header: 'Priority' },
    {
      key: 'range', header: 'Amount Range',
      render: (_v, row) => `${row.min_amount ?? '—'} – ${row.max_amount ?? '—'} ${row.currency || ''}`.trim(),
    },
    { key: 'action', header: 'Action', render: (v) => <StatusBadge variant={ACTION_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    {
      key: 'approval', header: 'Approval',
      render: (_v, row) => (row.approval_required ? `${row.approval_mode} × ${row.approval_count}` : '—'),
    },
    {
      key: 'peer_review', header: 'Peer Review',
      render: (_v, row) => (row.peer_review_required ? `${row.peer_review_mode} × ${row.peer_review_count}` : '—'),
    },
    {
      key: 'actions', header: '', width: '190px',
      render: (_v, row) => (
        <PermissionGate {...PAYMENT_APPROVAL_PERMISSIONS.APPROVAL_RULE_EDIT} fallback={null}>
          <div className="par-row-actions">
            <Button variant="ghost" size="sm" icon={<Pencil size={13} />} onClick={() => openEdit(row)}>Edit</Button>
            <PermissionGate {...PAYMENT_APPROVAL_PERMISSIONS.APPROVAL_RULE_STATUS} fallback={null}>
              <Button
                variant="ghost" size="sm" loading={actionBusy === row.id}
                icon={row.status === 'ACTIVE' ? <PowerOff size={13} /> : <Power size={13} />}
                onClick={() => toggleStatus(row)}
              >
                {row.status === 'ACTIVE' ? 'Deactivate' : 'Activate'}
              </Button>
            </PermissionGate>
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
            <h1 className="pa-title"><Settings size={22} /> Approval Rules</h1>
            <p className="pa-subtitle">Conditions that route an invoice into a Payment Approval workflow — evaluated automatically for every STRAIGHT_THROUGH invoice.</p>
          </div>
          <div className="pa-actions">
            <PermissionGate {...PAYMENT_APPROVAL_PERMISSIONS.APPROVAL_SIMULATE} fallback={null}>
              <Button variant="secondary" icon={<PlayCircle size={14} />} onClick={() => setSimulateOpen(true)}>Simulate</Button>
            </PermissionGate>
            <PermissionGate {...PAYMENT_APPROVAL_PERMISSIONS.APPROVAL_RULE_EDIT} fallback={null}>
              <Button variant="primary" icon={<Plus size={14} />} onClick={openCreate}>New Rule</Button>
            </PermissionGate>
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
                icon={<Settings size={28} />}
                title="No approval rules yet"
                description="Create a rule to start routing invoices into Payment Approval workflows automatically."
                action={{ label: 'New Rule', onClick: openCreate }}
              />
            }
          />
        </div>
      </div>

      <RuleFormModal
        open={formOpen}
        onClose={() => setFormOpen(false)}
        rule={editingRule}
        onSaved={() => { setFormOpen(false); load(); }}
      />
      <SimulateRuleModal open={simulateOpen} onClose={() => setSimulateOpen(false)} />
    </PermissionGate>
  );
}
