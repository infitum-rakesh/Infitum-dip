/**
 * Subscriptions — /app/seller/subscriptions (Seller Portal Phase 3).
 * Manage customer subscriptions — Create/Edit/Renew/Upgrade/Downgrade/
 * Suspend/Resume/Cancel, all against backend/app/services/seller/
 * subscription_service.py. Reuses DataTable/StatusBadge/Button/SearchBox/
 * EmptyState/PermissionGate — no duplicated components.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Plus, RefreshCcw, MoreVertical, Pencil, RotateCw, ArrowUpCircle,
  ArrowDownCircle, PauseCircle, PlayCircle, XCircle, CreditCard, CalendarPlus,
} from 'lucide-react';
import { Button, StatusBadge, DataTable, SearchBox, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAGINATION, SELLER_PERMISSIONS } from '@/utils/constants.js';
import { formatDate } from '@/utils/formatters.js';
import { planApi, subscriptionApi } from '@/services/api/seller/subscriptionApi.js';
import { CreateSubscriptionModal, EditSubscriptionModal, ChangePlanModal, ExtendSubscriptionModal } from './SubscriptionModals.jsx';
import './subscriptions.css';

const STATUS_VARIANT = { TRIAL: 'info', ACTIVE: 'success', EXPIRING: 'warning', EXPIRED: 'gray', SUSPENDED: 'danger', CANCELLED: 'gray' };
const STATUSES = ['TRIAL', 'ACTIVE', 'EXPIRING', 'EXPIRED', 'SUSPENDED', 'CANCELLED'];
// PaymentStatus values (models/billing/payment.py doesn't apply here —
// this reflects Invoice.status, see subscription_service.list_subscriptions).
const PAYMENT_STATUS_VARIANT = {
  PAID: 'success', NO_PAYMENT_REQUIRED: 'success',
  PAYMENT_PENDING: 'warning', PARTIALLY_PAID: 'warning',
  OVERDUE: 'danger', VOID: 'gray', CANCELLED: 'gray', DRAFT: 'gray', ISSUED: 'info',
};

export default function Subscriptions() {
  const { subscriptionId } = useParams();
  const navigate = useNavigate();

  const [plans, setPlans] = useState([]);
  const [statusFilter, setStatusFilter] = useState('All');
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [statusCounts, setStatusCounts] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState(null);
  const [planModal, setPlanModal] = useState(null); // { subscription, mode }
  const [extendTarget, setExtendTarget] = useState(null);

  useEffect(() => { planApi.list().then(setPlans).catch(() => {}); }, []);

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadCounts = useCallback(() => {
    subscriptionApi.list({ page_size: 1000 }).then((r) => {
      const counts = {};
      r.items.forEach((s) => { counts[s.status] = (counts[s.status] || 0) + 1; });
      setStatusCounts(counts);
    }).catch(() => {});
  }, []);
  useEffect(() => { loadCounts(); }, [loadCounts]);

  const loadSubs = useCallback(() => {
    setLoading(true);
    setError(null);
    subscriptionApi.list({ q: search, status_filter: statusFilter === 'All' ? undefined : statusFilter, page, page_size: pageSize })
      .then((res) => { setRows(res.items); setTotal(res.total); })
      .catch(() => setError('Failed to load subscriptions. Please try again.'))
      .finally(() => setLoading(false));
  }, [search, statusFilter, page, pageSize]);

  useEffect(() => { loadSubs(); }, [loadSubs]);

  // If arriving via /app/seller/subscriptions/:subscriptionId, open it for edit.
  useEffect(() => {
    if (!subscriptionId) return;
    subscriptionApi.get(subscriptionId).then((s) => setEditTarget({ ...s, organization_name: '' })).catch(() => {});
  }, [subscriptionId]);

  const refreshAll = () => { loadCounts(); loadSubs(); };
  const handleClearSearch = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const runRenew = async (row) => { await subscriptionApi.renew(row.id); refreshAll(); };
  const runSuspend = async (row) => {
    if (!window.confirm(`Suspend the subscription for "${row.organization_name}"?`)) return;
    await subscriptionApi.suspend(row.id); refreshAll();
  };
  const runResume = async (row) => { await subscriptionApi.resume(row.id); refreshAll(); };
  const runCancel = async (row) => {
    if (!window.confirm(`Cancel the subscription for "${row.organization_name}"? Auto-renew will be turned off.`)) return;
    await subscriptionApi.cancel(row.id); refreshAll();
  };

  const closeEditModal = () => { setEditTarget(null); if (subscriptionId) navigate('/app/seller/subscriptions'); };

  const columns = [
    { key: 'id', header: 'ID', render: (v) => `#${v}` },
    { key: 'organization_name', header: 'Customer', sortable: true },
    { key: 'plan_name', header: 'Plan', render: (v) => v ? <span className="ss-plan-pill">{v}</span> : <span style={{ color: 'var(--text-faint)' }}>—</span> },
    { key: 'start_date', header: 'Start Date', sortable: true, render: (v) => formatDate(v) },
    { key: 'end_date', header: 'End Date', sortable: true, render: (v) => v ? formatDate(v) : '—' },
    { key: 'billing_cycle', header: 'Billing Cycle', render: (v) => v },
    { key: 'amount', header: 'Amount', render: (v, row) => v > 0 ? <span className="ss-amount">${v.toLocaleString()} {row.currency}</span> : <span className="ss-amount-zero">Free</span> },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    {
      key: 'payment_status', header: 'Payment',
      render: (v) => v
        ? <StatusBadge variant={PAYMENT_STATUS_VARIANT[v] ?? 'gray'} dot>{v.replace(/_/g, ' ')}</StatusBadge>
        : <span style={{ color: 'var(--text-faint)' }}>No invoices yet</span>,
    },
    { key: 'auto_renew', header: 'Auto Renew', render: (v) => v ? <StatusBadge variant="success">On</StatusBadge> : <StatusBadge variant="gray">Off</StatusBadge> },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div onClick={(e) => e.stopPropagation()}>
          <RowMenu
            row={row}
            onEdit={() => setEditTarget(row)}
            onRenew={() => runRenew(row)}
            onExtend={() => setExtendTarget(row)}
            onUpgrade={() => setPlanModal({ subscription: row, mode: 'upgrade' })}
            onDowngrade={() => setPlanModal({ subscription: row, mode: 'downgrade' })}
            onSuspend={() => runSuspend(row)}
            onResume={() => runResume(row)}
            onCancel={() => runCancel(row)}
          />
        </div>
      ),
    },
  ];

  return (
    <PermissionGate {...SELLER_PERMISSIONS.SELLER_PORTAL_ACCESS} showReason>
      <div className="ss-page">
        <div className="ss-header">
          <div>
            <h1>Subscriptions</h1>
            <p className="ss-subtitle">Manage customer subscriptions.</p>
          </div>
          <div className="ss-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={refreshAll}>Refresh</Button>
            <Button variant="primary" icon={<Plus size={14} />} onClick={() => setCreateOpen(true)}>New Subscription</Button>
          </div>
        </div>

        <div className="ss-status-strip">
          <button className={`ss-status-chip ${statusFilter === 'All' ? 'active' : ''}`} onClick={() => { setStatusFilter('All'); setPage(1); }}>
            All <b>{total}</b>
          </button>
          {STATUSES.map((s) => (
            <button key={s} className={`ss-status-chip ${statusFilter === s ? 'active' : ''}`} onClick={() => { setStatusFilter(s); setPage(1); }}>
              {s} <b>{statusCounts[s] ?? 0}</b>
            </button>
          ))}
        </div>

        <div className="ss-search-row">
          <SearchBox value={searchInput} onChange={setSearchInput} placeholder="Search by customer name…" className="ss-search-box" />
          {searchInput && <button className="sc-clear-search-btn" onClick={handleClearSearch}>Clear</button>}
        </div>

        <DataTable
          columns={columns}
          data={rows}
          loading={loading}
          error={error}
          pagination={{ page, pageSize, total, onChange: setPage }}
          emptyState={
            <EmptyState
              icon={<CreditCard size={28} />}
              title={search || statusFilter !== 'All' ? 'No matching subscriptions' : 'No subscriptions yet'}
              description={search || statusFilter !== 'All' ? 'Try a different search term or clear your filters.' : 'Create a subscription for a customer to get started.'}
              action={{ label: 'New Subscription', onClick: () => setCreateOpen(true) }}
            />
          }
        />
      </div>

      <CreateSubscriptionModal open={createOpen} onClose={() => setCreateOpen(false)} plans={plans} onCreated={refreshAll} />
      <EditSubscriptionModal subscription={editTarget} onClose={closeEditModal} onSaved={() => { closeEditModal(); refreshAll(); }} />
      {planModal && (
        <ChangePlanModal
          subscription={planModal.subscription} plans={plans} mode={planModal.mode}
          onClose={() => setPlanModal(null)}
          onSaved={() => { setPlanModal(null); refreshAll(); }}
        />
      )}
      {extendTarget && (
        <ExtendSubscriptionModal
          subscription={extendTarget}
          onClose={() => setExtendTarget(null)}
          onSaved={() => { setExtendTarget(null); refreshAll(); }}
        />
      )}
    </PermissionGate>
  );
}

// ── Row kebab menu ───────────────────────────────────────────────

function RowMenu({ row, onEdit, onRenew, onExtend, onUpgrade, onDowngrade, onSuspend, onResume, onCancel }) {
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState(null);
  const btnRef = useRef(null);

  const toggle = () => {
    if (!open && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setCoords({ top: r.bottom + 4, left: r.right - 190 });
    }
    setOpen((o) => !o);
  };

  const act = (fn) => () => { setOpen(false); fn(); };
  const suspended = row.status === 'SUSPENDED';
  const cancelled = row.status === 'CANCELLED';

  return (
    <div className="ss-row-menu">
      <button ref={btnRef} className="ss-icon-btn" title="More actions" onClick={toggle}><MoreVertical size={14} /></button>
      {open && coords && createPortal(
        <>
          <div className="ss-row-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="ss-row-menu-list" style={{ position: 'fixed', top: coords.top, left: coords.left }}>
            <button onClick={act(onEdit)}><Pencil size={13} /> Edit</button>
            <button onClick={act(onRenew)}><RotateCw size={13} /> Renew</button>
            <button onClick={act(onExtend)}><CalendarPlus size={13} /> Extend</button>
            <button onClick={act(onUpgrade)}><ArrowUpCircle size={13} /> Upgrade</button>
            <button onClick={act(onDowngrade)}><ArrowDownCircle size={13} /> Downgrade</button>
            {suspended
              ? <button onClick={act(onResume)}><PlayCircle size={13} /> Resume</button>
              : <button className="ss-row-menu-warn" onClick={act(onSuspend)}><PauseCircle size={13} /> Suspend</button>}
            {!cancelled && <button className="ss-row-menu-danger" onClick={act(onCancel)}><XCircle size={13} /> Cancel</button>}
          </div>
        </>,
        document.body
      )}
    </div>
  );
}
