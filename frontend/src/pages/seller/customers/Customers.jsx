/**
 * Customers — /app/seller/customers (Seller Portal Phase 2).
 * Customer == Organization (see backend/app/services/seller/customer_service.py).
 * Reuses DataTable/StatusBadge/Button/Modal/SearchBox/EmptyState/
 * PermissionGate — no duplicated components.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import {
  Plus, RefreshCcw, Eye, MoreVertical, Pencil, Power, PowerOff,
  ShieldAlert, Building2, X,
} from 'lucide-react';
import { Button, StatusBadge, DataTable, SearchBox, EmptyState, PermissionGate, Modal } from '@/components/common/index.js';
import { PAGINATION, SELLER_PERMISSIONS } from '@/utils/constants.js';
import { formatDate } from '@/utils/formatters.js';
import { useAuth } from '@/hooks/useAuth.js';
import { customerApi } from '@/services/api/seller/customerApi.js';
import { CreateCustomerModal } from './CreateCustomerModal.jsx';
import './customers.css';

const STATUS_VARIANT = { active: 'success', trial: 'info', expiring: 'warning', expired: 'gray', suspended: 'danger' };
const EMPTY_FILTERS = { status: 'All', plan: 'All' };

export default function Customers() {
  const navigate = useNavigate();
  const auth = useAuth();

  const [stats, setStats] = useState(null);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [impersonateTarget, setImpersonateTarget] = useState(null);
  const [impersonating, setImpersonating] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadStats = useCallback(() => { customerApi.getStats().then(setStats).catch(() => {}); }, []);
  useEffect(() => { loadStats(); }, [loadStats]);

  const loadCustomers = useCallback(() => {
    setLoading(true);
    setError(null);
    customerApi.list({ q: search, status_filter: filters.status === 'All' ? undefined : filters.status, plan: filters.plan === 'All' ? undefined : filters.plan, page, page_size: pageSize })
      .then((res) => { setRows(res.items); setTotal(res.total); })
      .catch(() => setError('Failed to load customers. Please try again.'))
      .finally(() => setLoading(false));
  }, [search, filters, page, pageSize]);

  useEffect(() => { loadCustomers(); }, [loadCustomers]);

  const refreshAll = () => { loadStats(); loadCustomers(); };
  const handleClearSearch = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const openDetail = (row) => navigate(`/app/seller/customers/${row.id}`);

  const runActivate = async (row) => { await customerApi.activate(row.id); refreshAll(); };
  const runSuspend = async (row) => {
    if (!window.confirm(`Suspend "${row.name}"? Their users will lose access immediately.`)) return;
    await customerApi.suspend(row.id); refreshAll();
  };

  const confirmImpersonate = async () => {
    if (!impersonateTarget) return;
    setImpersonating(true);
    try {
      const session = await customerApi.impersonate(impersonateTarget.id);
      auth.impersonate(session);
      setImpersonateTarget(null);
      navigate('/app/dashboard');
    } catch (err) {
      window.alert(err?.message ?? 'Could not impersonate this customer.');
    } finally {
      setImpersonating(false);
    }
  };

  const columns = [
    {
      key: 'name', header: 'Organization', sortable: true,
      render: (v, row) => (
        <div className="sc-org-cell">
          <span className="sc-org-name">{v}</span>
          {row.legal_name && <span className="sc-org-legal">{row.legal_name}</span>}
        </div>
      ),
    },
    {
      key: 'primary_contact_name', header: 'Primary Contact',
      render: (v, row) => (
        <div className="sc-contact-cell">
          <span className="sc-contact-name">{v || '—'}</span>
          {row.primary_contact_email && <span className="sc-contact-email">{row.primary_contact_email}</span>}
        </div>
      ),
    },
    { key: 'plan', header: 'Plan', render: (v) => <span className="sc-plan-pill">{v}</span> },
    { key: 'license_status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v.charAt(0).toUpperCase() + v.slice(1)}</StatusBadge> },
    { key: 'users_count', header: 'Users', render: (v, row) => `${v} / ${row.max_users > 0 ? row.max_users : '∞'}` },
    { key: 'modules_count', header: 'Modules', render: (v) => v },
    { key: 'subscription_end', header: 'Expiry', sortable: true, render: (v) => v ? formatDate(v) : '—' },
    { key: 'created_at', header: 'Created', sortable: true, render: (v) => formatDate(v) },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="sc-row-actions" onClick={(e) => e.stopPropagation()}>
          <button className="sc-icon-btn" title="View" onClick={() => openDetail(row)}><Eye size={14} /></button>
          <RowMenu
            row={row}
            onEdit={() => openDetail(row)}
            onActivate={() => runActivate(row)}
            onSuspend={() => runSuspend(row)}
            onImpersonate={() => setImpersonateTarget(row)}
          />
        </div>
      ),
    },
  ];

  const kpis = [
    { label: 'Total Customers', value: stats ? stats.total : '—' },
    { label: 'Active', value: stats ? stats.active : '—', variant: 'success' },
    { label: 'Trial', value: stats ? stats.trial : '—' },
    { label: 'Suspended', value: stats ? stats.suspended : '—', variant: 'danger' },
    { label: 'Expired', value: stats ? stats.expired : '—', variant: 'warning' },
  ];

  const hasActiveFilters = search || filters.status !== 'All' || filters.plan !== 'All';

  return (
    <PermissionGate {...SELLER_PERMISSIONS.SELLER_PORTAL_ACCESS} showReason>
      <div className="sc-page">
        <div className="sc-header">
          <div>
            <h1>Customers</h1>
            <p className="sc-subtitle">Manage organizations using the platform.</p>
          </div>
          <div className="sc-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={refreshAll}>Refresh</Button>
            <Button variant="primary" icon={<Plus size={14} />} onClick={() => setCreateOpen(true)}>New Customer</Button>
          </div>
        </div>

        <div className="sc-kpis">
          {kpis.map((k) => (
            <div key={k.label} className="sc-kpi-card">
              <div className={`sc-kpi-val ${k.variant ? `sc-kpi-val-${k.variant}` : ''}`}>{k.value}</div>
              <div className="sc-kpi-label">{k.label}</div>
            </div>
          ))}
        </div>

        <div className="sc-search-row">
          <SearchBox value={searchInput} onChange={setSearchInput} placeholder="Search by organization, legal name, contact email…" className="sc-search-box" />
          <select className="sc-input sc-inline-filter" value={filters.status} onChange={(e) => { setFilters((f) => ({ ...f, status: e.target.value })); setPage(1); }}>
            <option value="All">All Statuses</option>
            {['trial', 'active', 'expiring', 'expired', 'suspended'].map((s) => <option key={s} value={s}>{s.charAt(0).toUpperCase() + s.slice(1)}</option>)}
          </select>
          <select className="sc-input sc-inline-filter" value={filters.plan} onChange={(e) => { setFilters((f) => ({ ...f, plan: e.target.value })); setPage(1); }}>
            <option value="All">All Plans</option>
            {['trial', 'basic', 'professional', 'enterprise'].map((p) => <option key={p} value={p}>{p.charAt(0).toUpperCase() + p.slice(1)}</option>)}
          </select>
          {searchInput && (
            <button className="sc-clear-search-btn" onClick={handleClearSearch}><X size={13} /> Clear</button>
          )}
        </div>

        <DataTable
          columns={columns}
          data={rows}
          loading={loading}
          error={error}
          onRowClick={openDetail}
          pagination={{ page, pageSize, total, onChange: setPage }}
          emptyState={
            <EmptyState
              icon={<Building2 size={28} />}
              title={hasActiveFilters ? 'No matching customers' : 'No customers yet'}
              description={hasActiveFilters ? 'Try a different search term or clear your filters.' : 'Onboard your first customer to get started.'}
              action={
                hasActiveFilters
                  ? { label: 'Clear filters', onClick: () => { handleClearSearch(); setFilters(EMPTY_FILTERS); } }
                  : { label: 'New Customer', onClick: () => setCreateOpen(true) }
              }
            />
          }
        />
      </div>

      <CreateCustomerModal open={createOpen} onClose={() => setCreateOpen(false)} onCreated={refreshAll} />

      <Modal
        open={!!impersonateTarget}
        onClose={() => setImpersonateTarget(null)}
        title="Impersonate Customer"
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setImpersonateTarget(null)} disabled={impersonating}>Cancel</Button>
            <Button variant="primary" onClick={confirmImpersonate} loading={impersonating}>Log In As Admin</Button>
          </>
        }
      >
        <div className="sc-impersonate-warning">
          <ShieldAlert size={18} style={{ flexShrink: 0 }} />
          <span>
            You are about to log in as the primary admin of <strong>{impersonateTarget?.name}</strong>. This action is
            audited and cannot be done silently — a permanent record will be created with your identity, the target
            account, and a timestamp. Your Seller session will be restored when you exit impersonation.
          </span>
        </div>
      </Modal>
    </PermissionGate>
  );
}

// ── Row kebab menu ───────────────────────────────────────────────

function RowMenu({ row, onEdit, onActivate, onSuspend, onImpersonate }) {
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState(null);
  const btnRef = useRef(null);

  const toggle = () => {
    if (!open && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setCoords({ top: r.bottom + 4, left: r.right - 200 });
    }
    setOpen((o) => !o);
  };

  const act = (fn) => () => { setOpen(false); fn(); };
  const suspended = row.license_status === 'suspended';

  return (
    <div className="sc-row-menu">
      <button ref={btnRef} className="sc-icon-btn" title="More actions" onClick={toggle}><MoreVertical size={14} /></button>
      {open && coords && createPortal(
        <>
          <div className="sc-row-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="sc-row-menu-list" style={{ position: 'fixed', top: coords.top, left: coords.left }}>
            <button onClick={act(onEdit)}><Pencil size={13} /> Edit</button>
            {suspended
              ? <button onClick={act(onActivate)}><Power size={13} /> Activate</button>
              : <button className="sc-row-menu-warn" onClick={act(onSuspend)}><PowerOff size={13} /> Suspend</button>}
            <button className="sc-row-menu-warn" onClick={act(onImpersonate)}><ShieldAlert size={13} /> Impersonate / Login As</button>
          </div>
        </>,
        document.body
      )}
    </div>
  );
}
