/**
 * SellerUsers — /app/seller/users (Seller Portal Phase 7).
 * Manages the Seller's OWN staff accounts (Seller Portal administrators
 * and support staff) — distinct from Administration -> Users, which
 * manages a *customer's* org users. Wraps /api/seller/users/* (see
 * backend/app/services/seller/seller_user_service.py). Nothing here is
 * mocked — invite/edit/role-change/disable/enable/reset-password/delete
 * all hit the real backend and are all audited (SellerAuditLog).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  Plus, RefreshCcw, X, MoreVertical, Pencil, Power, PowerOff,
  KeyRound, ShieldCheck, Trash2, Users as UsersIcon,
} from 'lucide-react';
import { Button, StatusBadge, DataTable, SearchBox, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAGINATION, SELLER_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime, initials } from '@/utils/formatters.js';
import { useAuth } from '@/hooks/useAuth.js';
import { sellerUserApi } from '@/services/api/seller/sellerUserApi.js';
import { InviteSellerUserModal, SELLER_ROLE_OPTIONS } from './InviteSellerUserModal.jsx';
import { EditSellerUserModal, ChangeSellerRoleModal, ResetSellerPasswordModal } from './SellerUserActionModals.jsx';
import './seller-users.css';

const STATUS_VARIANT = { ACTIVE: 'success', INACTIVE: 'gray', PENDING: 'warning' };
const ROLE_LABEL = Object.fromEntries(SELLER_ROLE_OPTIONS.filter((r) => r.value).map((r) => [r.value, r.label]));
const ROLE_FILTER_OPTIONS = SELLER_ROLE_OPTIONS.filter((r) => r.value); // exclude the "default" blank option from the filter dropdown

const EMPTY_FILTERS = { seller_role: 'All', status: 'All' };

export default function SellerUsers() {
  const { user: me } = useAuth();

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

  const [inviteOpen, setInviteOpen] = useState(false);
  const [editTarget, setEditTarget] = useState(null);
  const [roleTarget, setRoleTarget] = useState(null);
  const [resetTarget, setResetTarget] = useState(null);

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadStats = useCallback(() => { sellerUserApi.getStats().then(setStats).catch(() => {}); }, []);
  useEffect(() => { loadStats(); }, [loadStats]);

  const loadUsers = useCallback(() => {
    setLoading(true);
    setError(null);
    sellerUserApi
      .list({ q: search, seller_role: filters.seller_role === 'All' ? undefined : filters.seller_role, status: filters.status, page, page_size: pageSize })
      .then((res) => { setRows(res.items); setTotal(res.total); })
      .catch(() => setError('Failed to load Seller Portal users. Please try again.'))
      .finally(() => setLoading(false));
  }, [search, filters, page, pageSize]);

  useEffect(() => { loadUsers(); }, [loadUsers]);

  const refreshAll = () => { loadStats(); loadUsers(); };
  const handleClearSearch = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const runDisable = async (row) => {
    try { await sellerUserApi.disable(row.id); refreshAll(); }
    catch (err) { window.alert(err?.message ?? 'Could not disable user.'); }
  };
  const runEnable = async (row) => { await sellerUserApi.enable(row.id); refreshAll(); };
  const runDelete = async (row) => {
    if (!window.confirm(`Remove Seller Portal access for "${row.full_name}"? This cannot be undone.`)) return;
    try { await sellerUserApi.delete(row.id); refreshAll(); }
    catch (err) { window.alert(err?.message ?? 'Could not delete user.'); }
  };

  const columns = [
    {
      key: 'full_name', header: 'Name', sortable: true,
      render: (v, row) => (
        <div className="su-name-cell">
          <div className="su-avatar">{initials(v)}</div>
          <span className="su-name-text">{v}</span>
        </div>
      ),
    },
    { key: 'email', header: 'Email', render: (v) => v },
    {
      key: 'seller_role', header: 'Seller Role',
      render: (v) => <span className={`su-role-pill ${!v || v === 'SELLER_ADMIN' ? 'su-role-pill-admin' : ''}`}>{v ? ROLE_LABEL[v] ?? v : 'Seller Admin (default)'}</span>,
    },
    { key: 'department', header: 'Department', render: (v) => v || '—' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v === 'PENDING' ? 'Pending Invitation' : v.charAt(0) + v.slice(1).toLowerCase()}</StatusBadge> },
    { key: 'last_login', header: 'Last Login', sortable: true, render: (v) => v ? formatDateTime(v) : 'Never' },
    { key: 'created_at', header: 'Created', sortable: true, render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="su-row-actions" onClick={(e) => e.stopPropagation()}>
          <RowMenu
            row={row}
            isSelf={row.id === me?.id}
            onEdit={() => setEditTarget(row)}
            onDisable={() => runDisable(row)}
            onEnable={() => runEnable(row)}
            onResetPassword={() => setResetTarget(row)}
            onChangeRole={() => setRoleTarget(row)}
            onDelete={() => runDelete(row)}
          />
        </div>
      ),
    },
  ];

  const kpis = [
    { label: 'Total Seller Users', value: stats ? stats.total : '—' },
    { label: 'Active', value: stats ? stats.active : '—', variant: 'success' },
    { label: 'Inactive', value: stats ? stats.inactive : '—' },
    { label: 'Pending Invitations', value: stats ? stats.pending : '—', variant: 'warning' },
    { label: 'Admins', value: stats ? stats.admins : '—', variant: 'info' },
  ];

  const hasActiveFilters = search || filters.seller_role !== 'All' || filters.status !== 'All';

  return (
    <PermissionGate {...SELLER_PERMISSIONS.SELLER_PORTAL_ACCESS} showReason>
      <div className="su-page">
        <div className="su-header">
          <div>
            <h1 className="su-title">Seller Users</h1>
            <p className="su-subtitle">Manage Seller Portal administrators and support staff.</p>
          </div>
          <div className="su-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={refreshAll}>Refresh</Button>
            <Button variant="primary" icon={<Plus size={14} />} onClick={() => setInviteOpen(true)}>Invite Seller User</Button>
          </div>
        </div>

        <div className="su-kpis">
          {kpis.map((k) => (
            <div key={k.label} className="su-kpi-card">
              <div className={`su-kpi-val ${k.variant ? `su-kpi-val-${k.variant}` : ''}`}>{k.value}</div>
              <div className="su-kpi-label">{k.label}</div>
            </div>
          ))}
        </div>

        <div className="su-search-row">
          <SearchBox value={searchInput} onChange={setSearchInput} placeholder="Search by name, email, department…" className="su-search-box" />
          <select className="su-input su-inline-filter" value={filters.seller_role} onChange={(e) => { setFilters((f) => ({ ...f, seller_role: e.target.value })); setPage(1); }}>
            <option value="All">All Roles</option>
            {ROLE_FILTER_OPTIONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
          </select>
          <select className="su-input su-inline-filter" value={filters.status} onChange={(e) => { setFilters((f) => ({ ...f, status: e.target.value })); setPage(1); }}>
            <option value="All">All Statuses</option>
            <option value="ACTIVE">Active</option>
            <option value="INACTIVE">Inactive</option>
            <option value="PENDING">Pending Invitation</option>
          </select>
          {searchInput && (
            <button className="su-clear-search-btn" onClick={handleClearSearch}><X size={13} /> Clear</button>
          )}
        </div>

        <div className="su-table-panel">
          <DataTable
            columns={columns}
            data={rows}
            loading={loading}
            error={error}
            pagination={{ page, pageSize, total, onChange: setPage }}
            emptyState={
              <EmptyState
                icon={<UsersIcon size={28} />}
                title={hasActiveFilters ? 'No matching Seller users' : 'No Seller users yet'}
                description={hasActiveFilters ? 'Try a different search term or clear your filters.' : 'Invite a teammate to the Seller Portal to get started.'}
                action={
                  hasActiveFilters
                    ? { label: 'Clear filters', onClick: () => { handleClearSearch(); setFilters(EMPTY_FILTERS); } }
                    : { label: 'Invite Seller User', onClick: () => setInviteOpen(true) }
                }
              />
            }
          />
        </div>
      </div>

      <InviteSellerUserModal open={inviteOpen} onClose={() => setInviteOpen(false)} onInvited={() => refreshAll()} />
      <EditSellerUserModal user={editTarget} onClose={() => setEditTarget(null)} onDone={refreshAll} />
      <ChangeSellerRoleModal user={roleTarget} onClose={() => setRoleTarget(null)} onDone={refreshAll} />
      <ResetSellerPasswordModal user={resetTarget} onClose={() => setResetTarget(null)} />
    </PermissionGate>
  );
}

// ── Row kebab menu ───────────────────────────────────────────────

function RowMenu({ row, isSelf, onEdit, onDisable, onEnable, onResetPassword, onChangeRole, onDelete }) {
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

  return (
    <div className="su-row-menu">
      <button ref={btnRef} className="su-icon-btn" title="More actions" onClick={toggle}><MoreVertical size={14} /></button>
      {open && coords && createPortal(
        <>
          <div className="su-row-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="su-row-menu-list" style={{ position: 'fixed', top: coords.top, left: coords.left }}>
            <button onClick={act(onEdit)}><Pencil size={13} /> Edit</button>
            {row.status === 'INACTIVE'
              ? <button onClick={act(onEnable)}><Power size={13} /> Enable</button>
              : <button onClick={act(onDisable)} disabled={isSelf} title={isSelf ? "You can't disable your own account" : undefined}><PowerOff size={13} /> Disable</button>}
            <button onClick={act(onResetPassword)}><KeyRound size={13} /> Reset Password</button>
            <button onClick={act(onChangeRole)}><ShieldCheck size={13} /> Change Role</button>
            <button className="su-row-menu-danger" onClick={act(onDelete)} disabled={isSelf} title={isSelf ? "You can't delete your own account" : undefined}>
              <Trash2 size={13} /> Delete
            </button>
          </div>
        </>,
        document.body
      )}
    </div>
  );
}
