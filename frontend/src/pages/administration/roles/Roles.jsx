/**
 * Roles — /app/admin/roles
 * Create role-based access control: named, org-scoped bundles of
 * (category, action) permission grants. Reuses MainLayout/Header/Sidebar/
 * DataTable/StatusBadge/Button/Modal/apiClient/auth/PermissionContext —
 * no duplicate components, no bespoke permission system (see
 * ROLE_PERMISSIONS in utils/constants.js).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import { Plus, RefreshCcw, Eye, MoreVertical, Pencil, Power, Trash2, ShieldCheck } from 'lucide-react';
import { Button, StatusBadge, DataTable, SearchBox, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAGINATION, ROLE_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { roleApi } from '@/services/api/roleApi.js';
import { CreateRoleModal } from './CreateRoleModal.jsx';
import './roles.css';

const STATUS_VARIANT = { ACTIVE: 'success', INACTIVE: 'gray' };

export default function Roles() {
  const navigate = useNavigate();

  const [data, setData] = useState(null); // { items, total, categories, licensed_categories }
  const [searchInput, setSearchInput] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    roleApi.list().then(setData).catch(() => setError('Failed to load roles. Please try again.')).finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const openDetail = (row) => navigate(`/app/admin/roles/${row.id}`);

  const runToggleStatus = async (row) => {
    await roleApi.update(row.id, { status: row.status === 'ACTIVE' ? 'INACTIVE' : 'ACTIVE' });
    load();
  };
  const runDelete = async (row) => {
    if (!window.confirm(`Delete role "${row.name}"? This cannot be undone.`)) return;
    try { await roleApi.delete(row.id); load(); }
    catch (err) { window.alert(err?.message ?? 'Could not delete role.'); }
  };

  const filtered = (data?.items ?? []).filter((r) =>
    !searchInput || r.name.toLowerCase().includes(searchInput.toLowerCase()) || (r.description ?? '').toLowerCase().includes(searchInput.toLowerCase())
  );

  const columns = [
    {
      key: 'name', header: 'Role', sortable: true,
      render: (v, row) => (
        <div className="rol-name-cell">
          <span className="rol-name-text">{v}</span>
          {row.is_system && <span className="rol-system-badge">Starter</span>}
        </div>
      ),
    },
    { key: 'description', header: 'Description', render: (v) => <span className="rol-desc-text" title={v}>{v || '—'}</span> },
    { key: 'user_count', header: 'Users', sortable: true, render: (v) => <span className="rol-count-pill">{v}</span> },
    { key: 'permission_count', header: 'Permissions', sortable: true, render: (v) => <span className="rol-count-pill">{v}</span> },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v.charAt(0) + v.slice(1).toLowerCase()}</StatusBadge> },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="rol-row-actions" onClick={(e) => e.stopPropagation()}>
          <button className="rol-icon-btn" title="View" onClick={() => openDetail(row)}><Eye size={14} /></button>
          <RowMenu row={row} onEdit={() => openDetail(row)} onToggleStatus={() => runToggleStatus(row)} onDelete={() => runDelete(row)} />
        </div>
      ),
    },
  ];

  return (
    <PermissionGate {...ROLE_PERMISSIONS.ROLE_VIEW} showReason>
      <div className="rol-page">
        {/* Header */}
        <div className="rol-header">
          <div>
            <h1 className="rol-title">Roles</h1>
            <p className="rol-subtitle">Create role-based access control for your organization.</p>
          </div>
          <div className="rol-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={load}>Refresh</Button>
            <PermissionGate {...ROLE_PERMISSIONS.ROLE_CREATE} fallback={null}>
              <Button variant="primary" icon={<Plus size={14} />} onClick={() => setCreateOpen(true)}>Create Role</Button>
            </PermissionGate>
          </div>
        </div>

        {/* Search */}
        <div className="rol-search-row">
          <SearchBox value={searchInput} onChange={setSearchInput} placeholder="Search by role name or description…" className="rol-search-box" />
        </div>

        {/* Table */}
        <div className="rol-table-panel">
          <DataTable
            columns={columns}
            data={filtered}
            loading={loading}
            error={error}
            onRowClick={openDetail}
            emptyState={
              <EmptyState
                icon={<ShieldCheck size={28} />}
                title={searchInput ? 'No matching roles' : 'No roles yet'}
                description={searchInput ? 'Try a different search term.' : 'Create a role to start assigning fine-grained permissions.'}
                action={searchInput ? undefined : { label: 'Create Role', onClick: () => setCreateOpen(true) }}
              />
            }
          />
        </div>
      </div>

      {data && (
        <CreateRoleModal
          open={createOpen}
          onClose={() => setCreateOpen(false)}
          categories={data.categories}
          licensedCategories={data.licensed_categories}
          categoryActions={data.category_actions}
          onCreated={() => load()}
        />
      )}
    </PermissionGate>
  );
}

// ── Row kebab menu ───────────────────────────────────────────────

function RowMenu({ row, onEdit, onToggleStatus, onDelete }) {
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState(null);
  const btnRef = useRef(null);

  const toggle = () => {
    if (!open && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setCoords({ top: r.bottom + 4, left: r.right - 170 });
    }
    setOpen((o) => !o);
  };

  const act = (fn) => () => { setOpen(false); fn(); };

  return (
    <div className="rol-row-menu">
      <button ref={btnRef} className="rol-icon-btn" title="More actions" onClick={toggle}><MoreVertical size={14} /></button>
      {open && coords && createPortal(
        <>
          <div className="rol-row-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="rol-row-menu-list" style={{ position: 'fixed', top: coords.top, left: coords.left }}>
            <PermissionGate {...ROLE_PERMISSIONS.ROLE_EDIT} fallback={null}>
              <button onClick={act(onEdit)}><Pencil size={13} /> Edit</button>
            </PermissionGate>
            <PermissionGate {...ROLE_PERMISSIONS.ROLE_EDIT} fallback={null}>
              <button onClick={act(onToggleStatus)}><Power size={13} /> {row.status === 'ACTIVE' ? 'Deactivate' : 'Activate'}</button>
            </PermissionGate>
            <PermissionGate {...ROLE_PERMISSIONS.ROLE_DELETE} fallback={null}>
              <button
                className="rol-row-menu-danger"
                onClick={act(onDelete)}
                disabled={row.is_system || row.user_count > 0}
                title={row.is_system ? 'Starter roles cannot be deleted' : row.user_count > 0 ? 'Unassign all users first' : undefined}
              >
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
