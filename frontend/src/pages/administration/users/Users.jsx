/**
 * Users — /app/admin/users
 * Organization administrators managing users belonging to their org.
 * Reuses MainLayout/Header/Sidebar/DataTable/StatusBadge/Button/Modal/
 * apiClient/auth/PermissionContext — no duplicate components, no
 * bespoke permission system (see USER_PERMISSIONS in utils/constants.js).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import {
  Plus, RefreshCcw, X, Eye, MoreVertical, Pencil, Power, PowerOff,
  KeyRound, ShieldCheck, Trash2, Users as UsersIcon,
} from 'lucide-react';
import { Button, StatusBadge, DataTable, SearchBox, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAGINATION, USER_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime, initials } from '@/utils/formatters.js';
import { useAuth } from '@/hooks/useAuth.js';
import { userAdminApi } from '@/services/api/userAdminApi.js';
import { InviteUserModal } from './InviteUserModal.jsx';
import { ChangeRoleModal, ResetPasswordModal } from './UserActionModals.jsx';
import './users.css';

const STATUS_VARIANT = { ACTIVE: 'success', INACTIVE: 'gray', PENDING: 'warning' };
const ROLE_LABEL = {
  super_admin: 'Super Admin', seller: 'Seller', org_admin: 'Org Admin',
  data_engineer: 'Data Engineer', analyst: 'Analyst', viewer: 'Viewer',
};
const ROLE_OPTIONS = ['org_admin', 'data_engineer', 'analyst', 'viewer'];

const EMPTY_FILTERS = { role: 'All', status: 'All' };

export default function Users() {
  const navigate = useNavigate();
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
  const [roleTarget, setRoleTarget] = useState(null);
  const [resetTarget, setResetTarget] = useState(null);

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadStats = useCallback(() => { userAdminApi.getStats().then(setStats).catch(() => {}); }, []);
  useEffect(() => { loadStats(); }, [loadStats]);

  const loadUsers = useCallback(() => {
    setLoading(true);
    setError(null);
    userAdminApi
      .list({ q: search, role: filters.role, status: filters.status, page, page_size: pageSize })
      .then((res) => { setRows(res.items); setTotal(res.total); })
      .catch(() => setError('Failed to load users. Please try again.'))
      .finally(() => setLoading(false));
  }, [search, filters, page, pageSize]);

  useEffect(() => { loadUsers(); }, [loadUsers]);

  const refreshAll = () => { loadStats(); loadUsers(); };
  const handleClearSearch = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const openDetail = (row) => navigate(`/app/admin/users/${row.id}`);

  const runDisable = async (row) => { await userAdminApi.disable(row.id); refreshAll(); };
  const runEnable = async (row) => { await userAdminApi.enable(row.id); refreshAll(); };
  const runDelete = async (row) => {
    if (!window.confirm(`Delete "${row.full_name}"? This cannot be undone.`)) return;
    try { await userAdminApi.delete(row.id); refreshAll(); }
    catch (err) { window.alert(err?.message ?? 'Could not delete user.'); }
  };

  const columns = [
    {
      key: 'full_name', header: 'Name', sortable: true,
      render: (v, row) => (
        <div className="usr-name-cell">
          <div className="usr-avatar">{initials(v)}</div>
          <span className="usr-name-text">{v}</span>
        </div>
      ),
    },
    { key: 'email', header: 'Email', render: (v) => v },
    { key: 'role', header: 'Role', render: (v) => <span className="usr-role-pill">{ROLE_LABEL[v] ?? v}</span> },
    { key: 'department', header: 'Department', render: (v) => v || '—' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v === 'PENDING' ? 'Pending Invitation' : v.charAt(0) + v.slice(1).toLowerCase()}</StatusBadge> },
    { key: 'last_login', header: 'Last Login', sortable: true, render: (v) => v ? formatDateTime(v) : 'Never' },
    { key: 'created_at', header: 'Created', sortable: true, render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="usr-row-actions" onClick={(e) => e.stopPropagation()}>
          <button className="usr-icon-btn" title="View" onClick={() => openDetail(row)}><Eye size={14} /></button>
          <RowMenu
            row={row}
            isSelf={row.id === me?.id}
            onEdit={() => openDetail(row)}
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
    { label: 'Total Users', value: stats ? stats.total : '—' },
    { label: 'Active', value: stats ? stats.active : '—', variant: 'success' },
    { label: 'Inactive', value: stats ? stats.inactive : '—' },
    { label: 'Pending Invitations', value: stats ? stats.pending : '—', variant: 'warning' },
    { label: 'Administrators', value: stats ? stats.administrators : '—', variant: 'info' },
  ];

  const hasActiveFilters = search || filters.role !== 'All' || filters.status !== 'All';

  return (
    <PermissionGate {...USER_PERMISSIONS.USER_VIEW} showReason>
      <div className="usr-page">
        {/* Header */}
        <div className="usr-header">
          <div>
            <h1 className="usr-title">Users</h1>
            <p className="usr-subtitle">Manage users belonging to your organization.</p>
          </div>
          <div className="usr-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={refreshAll}>Refresh</Button>
            <PermissionGate {...USER_PERMISSIONS.USER_CREATE} fallback={null}>
              <Button variant="primary" icon={<Plus size={14} />} onClick={() => setInviteOpen(true)}>Invite User</Button>
            </PermissionGate>
          </div>
        </div>

        {/* KPIs */}
        <div className="usr-kpis">
          {kpis.map((k) => (
            <div key={k.label} className="usr-kpi-card">
              <div className={`usr-kpi-val ${k.variant ? `usr-kpi-val-${k.variant}` : ''}`}>{k.value}</div>
              <div className="usr-kpi-label">{k.label}</div>
            </div>
          ))}
        </div>

        {/* Search + inline filters */}
        <div className="usr-search-row">
          <SearchBox value={searchInput} onChange={setSearchInput} placeholder="Search by name, email, department…" className="usr-search-box" />
          <select className="usr-input usr-inline-filter" value={filters.role} onChange={(e) => { setFilters((f) => ({ ...f, role: e.target.value })); setPage(1); }}>
            <option value="All">All Roles</option>
            {['super_admin', 'seller', ...ROLE_OPTIONS].filter((v, i, a) => a.indexOf(v) === i).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
          </select>
          <select className="usr-input usr-inline-filter" value={filters.status} onChange={(e) => { setFilters((f) => ({ ...f, status: e.target.value })); setPage(1); }}>
            <option value="All">All Statuses</option>
            <option value="ACTIVE">Active</option>
            <option value="INACTIVE">Inactive</option>
            <option value="PENDING">Pending Invitation</option>
          </select>
          {searchInput && (
            <button className="usr-clear-search-btn" onClick={handleClearSearch}><X size={13} /> Clear</button>
          )}
        </div>

        {/* Table */}
        <div className="usr-table-panel">
          <DataTable
            columns={columns}
            data={rows}
            loading={loading}
            error={error}
            onRowClick={openDetail}
            pagination={{ page, pageSize, total, onChange: setPage }}
            emptyState={
              <EmptyState
                icon={<UsersIcon size={28} />}
                title={hasActiveFilters ? 'No matching users' : 'No users yet'}
                description={hasActiveFilters ? 'Try a different search term or clear your filters.' : 'Invite a teammate to get started.'}
                action={
                  hasActiveFilters
                    ? { label: 'Clear filters', onClick: () => { handleClearSearch(); setFilters(EMPTY_FILTERS); } }
                    : { label: 'Invite User', onClick: () => setInviteOpen(true) }
                }
              />
            }
          />
        </div>
      </div>

      <InviteUserModal open={inviteOpen} onClose={() => setInviteOpen(false)} onInvited={() => refreshAll()} />
      <ChangeRoleModal user={roleTarget} onClose={() => setRoleTarget(null)} onDone={refreshAll} />
      <ResetPasswordModal user={resetTarget} onClose={() => setResetTarget(null)} />
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
    <div className="usr-row-menu">
      <button ref={btnRef} className="usr-icon-btn" title="More actions" onClick={toggle}><MoreVertical size={14} /></button>
      {open && coords && createPortal(
        <>
          <div className="usr-row-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="usr-row-menu-list" style={{ position: 'fixed', top: coords.top, left: coords.left }}>
            <PermissionGate {...USER_PERMISSIONS.USER_EDIT} fallback={null}>
              <button onClick={act(onEdit)}><Pencil size={13} /> Edit</button>
            </PermissionGate>
            <PermissionGate {...USER_PERMISSIONS.USER_DISABLE} fallback={null}>
              {row.status === 'INACTIVE'
                ? <button onClick={act(onEnable)}><Power size={13} /> Enable</button>
                : <button onClick={act(onDisable)} disabled={isSelf} title={isSelf ? "You can't disable your own account" : undefined}><PowerOff size={13} /> Disable</button>}
            </PermissionGate>
            <PermissionGate {...USER_PERMISSIONS.USER_EDIT} fallback={null}>
              <button onClick={act(onResetPassword)}><KeyRound size={13} /> Reset Password</button>
            </PermissionGate>
            <PermissionGate {...USER_PERMISSIONS.USER_ASSIGN_ROLE} fallback={null}>
              <button onClick={act(onChangeRole)}><ShieldCheck size={13} /> Change Role</button>
            </PermissionGate>
            <PermissionGate {...USER_PERMISSIONS.USER_DELETE} fallback={null}>
              <button className="usr-row-menu-danger" onClick={act(onDelete)} disabled={isSelf} title={isSelf ? "You can't delete your own account" : undefined}>
                <Trash2 size={13} /> Delete
              </button>
            </PermissionGate>
          </div>
        </>,
        document.body
      )}
    </div>
  );
}
