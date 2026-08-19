/**
 * Connections — /app/connections
 * Centralized connection management for databases, applications, storage,
 * API and cloud endpoints. Reuses MainLayout/Header/Sidebar/Modal/DataTable/
 * StatusBadge/Button/apiClient/auth/permissions — no duplicate components.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  Plus, RefreshCcw, X, Eye, Pencil, PlayCircle, MoreVertical, Copy, Power, Trash2,
} from 'lucide-react';
import { Button, StatusBadge, DataTable, SearchBox, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAGINATION, CONNECTION_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { NewConnectionModal } from './NewConnectionModal.jsx';
import { ConnectionFormModal } from './ConnectionFormModal.jsx';
import './connections.css';

const STATUS_VARIANT = { ACTIVE: 'success', INACTIVE: 'gray', ERROR: 'danger', UNTESTED: 'info', UNSUPPORTED: 'warning' };
const EMPTY_FILTERS = { category: 'All', status: 'All' };

export default function Connections() {
  const [stats, setStats] = useState(null);
  const [catalogue, setCatalogue] = useState(null);

  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');

  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [newModalOpen, setNewModalOpen] = useState(false);
  const [formTarget, setFormTarget] = useState(null); // { category, connectionType, connection, startEditable } | null

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadStats = useCallback(() => { connectionApi.getStats().then(setStats).catch(() => {}); }, []);
  useEffect(() => { loadStats(); connectionApi.getTypes().then(setCatalogue).catch(() => {}); }, [loadStats]);

  const loadConnections = useCallback(() => {
    setLoading(true);
    setError(null);
    connectionApi
      .list({ q: search, category: filters.category, status: filters.status, page, page_size: pageSize })
      .then((res) => { setRows(res.items); setTotal(res.total); })
      .catch(() => setError('Failed to load connections. Please try again.'))
      .finally(() => setLoading(false));
  }, [search, filters, page, pageSize]);

  useEffect(() => { loadConnections(); }, [loadConnections]);

  const refreshAll = () => { loadStats(); loadConnections(); };
  const handleClearSearch = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const openNewType = (category, connectionType) => {
    setNewModalOpen(false);
    setFormTarget({ category, connectionType, connection: null, startEditable: true });
  };
  const openView = (row) => setFormTarget({ category: row.category, connectionType: row.connection_type, connection: row, startEditable: false });
  const openEdit = (row) => setFormTarget({ category: row.category, connectionType: row.connection_type, connection: row, startEditable: true });

  const runTest = async (row) => {
    await connectionApi.test(row.id);
    refreshAll();
  };
  const runClone = async (row) => {
    await connectionApi.clone(row.id);
    refreshAll();
  };
  const runToggleDisabled = async (row) => {
    await connectionApi.toggleDisabled(row.id);
    refreshAll();
  };
  const runDelete = async (row) => {
    if (!window.confirm(`Delete connection "${row.name}"? This cannot be undone.`)) return;
    await connectionApi.remove(row.id);
    refreshAll();
  };

  const columns = [
    {
      key: 'name', header: 'Connection Name', sortable: true,
      render: (v) => <span className="conn-name-text">{v}</span>,
    },
    { key: 'connection_type', header: 'Type', sortable: true, render: (v) => catalogue?.schemas?.[v]?.label ?? v },
    { key: 'environment', header: 'Environment', render: (v) => v?.charAt(0) + v?.slice(1).toLowerCase() },
    { key: 'host_display', header: 'Host / Endpoint', render: (v) => v || '—' },
    { key: 'database_display', header: 'Database', render: (v) => v || '—' },
    { key: 'owner_name', header: 'Owner', render: (v) => v ?? '—' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'last_tested_at', header: 'Last Tested', sortable: true, render: (v) => v ? formatDateTime(v) : 'Never' },
    { key: 'created_at', header: 'Created', sortable: true, render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="conn-row-actions" onClick={(e) => e.stopPropagation()}>
          <button className="conn-icon-btn" title="View" onClick={() => openView(row)}><Eye size={14} /></button>
          <PermissionGate {...CONNECTION_PERMISSIONS.CONNECTION_EDIT} fallback={null}>
            <button className="conn-icon-btn" title="Edit" onClick={() => openEdit(row)}><Pencil size={14} /></button>
          </PermissionGate>
          <PermissionGate {...CONNECTION_PERMISSIONS.CONNECTION_TEST} fallback={null}>
            <button className="conn-icon-btn" title="Test Connection" onClick={() => runTest(row)}><PlayCircle size={14} /></button>
          </PermissionGate>
          <RowMenu row={row} onClone={() => runClone(row)} onToggleDisabled={() => runToggleDisabled(row)} onDelete={() => runDelete(row)} />
        </div>
      ),
    },
  ];

  const kpis = [
    { label: 'Total Connections', value: stats ? stats.total : '—' },
    { label: 'Active', value: stats ? stats.active : '—', variant: 'success' },
    { label: 'Inactive', value: stats ? stats.inactive : '—' },
    { label: 'Connection Errors', value: stats ? stats.errors : '—', variant: 'danger' },
    { label: 'Recently Tested', value: stats ? stats.recently_tested : '—', variant: 'info' },
  ];

  const hasActiveFilters = search || filters.category !== 'All' || filters.status !== 'All';

  return (
    <PermissionGate {...CONNECTION_PERMISSIONS.CONNECTION_VIEW} showReason>
      <div className="conn-page">
        {/* Header */}
        <div className="conn-header">
          <div>
            <h1 className="conn-title">Connections</h1>
            <p className="conn-subtitle">Manage data sources, databases, APIs and storage connections.</p>
          </div>
          <div className="conn-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={refreshAll}>Refresh</Button>
            <PermissionGate {...CONNECTION_PERMISSIONS.CONNECTION_CREATE} fallback={null}>
              <Button variant="primary" icon={<Plus size={14} />} onClick={() => setNewModalOpen(true)}>New Connection</Button>
            </PermissionGate>
          </div>
        </div>

        {/* KPIs */}
        <div className="conn-kpis" style={{ gridTemplateColumns: 'repeat(5, 1fr)' }}>
          {kpis.map((k) => (
            <div key={k.label} className="conn-kpi-card">
              <div className={`conn-kpi-val ${k.variant ? `conn-kpi-val-${k.variant}` : ''}`}>{k.value}</div>
              <div className="conn-kpi-label">{k.label}</div>
            </div>
          ))}
        </div>

        {/* Search + inline filters */}
        <div className="conn-search-row">
          <SearchBox value={searchInput} onChange={setSearchInput} placeholder="Search by name, type, host, owner…" className="conn-search-box" />
          {catalogue && (
            <select className="conn-input conn-inline-filter" value={filters.category} onChange={(e) => { setFilters((f) => ({ ...f, category: e.target.value })); setPage(1); }}>
              <option value="All">All Categories</option>
              {catalogue.categories.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
            </select>
          )}
          <select className="conn-input conn-inline-filter" value={filters.status} onChange={(e) => { setFilters((f) => ({ ...f, status: e.target.value })); setPage(1); }}>
            <option value="All">All Statuses</option>
            <option value="ACTIVE">Active</option>
            <option value="INACTIVE">Inactive</option>
            <option value="ERROR">Error</option>
            <option value="UNTESTED">Untested</option>
            <option value="UNSUPPORTED">Unsupported</option>
          </select>
          {searchInput && (
            <button className="conn-clear-search-btn" onClick={handleClearSearch}><X size={13} /> Clear</button>
          )}
        </div>

        {/* Table */}
        <div className="conn-table-panel">
          <DataTable
            columns={columns}
            data={rows}
            loading={loading}
            error={error}
            onRowClick={openView}
            pagination={{ page, pageSize, total, onChange: setPage }}
            emptyState={
              <EmptyState
                icon={<Plus size={28} />}
                title={hasActiveFilters ? 'No matching connections' : 'No connections yet'}
                description={hasActiveFilters ? 'Try a different search term or clear your filters.' : 'Add a connection to start pulling data in.'}
                action={
                  hasActiveFilters
                    ? { label: 'Clear filters', onClick: () => { handleClearSearch(); setFilters(EMPTY_FILTERS); } }
                    : { label: 'New Connection', onClick: () => setNewModalOpen(true) }
                }
              />
            }
          />
        </div>
      </div>

      <NewConnectionModal open={newModalOpen} onClose={() => setNewModalOpen(false)} catalogue={catalogue} onSelect={openNewType} />

      {formTarget && (
        <ConnectionFormModal
          open={!!formTarget}
          onClose={() => { setFormTarget(null); refreshAll(); }}
          catalogue={catalogue}
          category={formTarget.category}
          connectionType={formTarget.connectionType}
          connection={formTarget.connection}
          startEditable={formTarget.startEditable}
          onSaved={refreshAll}
        />
      )}
    </PermissionGate>
  );
}

// ── Row kebab menu ───────────────────────────────────────────────

function RowMenu({ row, onClone, onToggleDisabled, onDelete }) {
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

  return (
    <div className="conn-row-menu">
      <button ref={btnRef} className="conn-icon-btn" title="More actions" onClick={toggle}><MoreVertical size={14} /></button>
      {open && coords && createPortal(
        <>
          <div className="conn-row-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="conn-row-menu-list" style={{ position: 'fixed', top: coords.top, left: coords.left }}>
            <PermissionGate {...CONNECTION_PERMISSIONS.CONNECTION_CREATE} fallback={null}>
              <button onClick={() => { setOpen(false); onClone(); }}><Copy size={13} /> Clone</button>
            </PermissionGate>
            <PermissionGate {...CONNECTION_PERMISSIONS.CONNECTION_EDIT} fallback={null}>
              <button onClick={() => { setOpen(false); onToggleDisabled(); }}>
                <Power size={13} /> {row.status === 'INACTIVE' ? 'Enable' : 'Disable'}
              </button>
            </PermissionGate>
            <PermissionGate {...CONNECTION_PERMISSIONS.CONNECTION_DELETE} fallback={null}>
              <button className="conn-row-menu-danger" onClick={() => { setOpen(false); onDelete(); }}><Trash2 size={13} /> Delete</button>
            </PermissionGate>
          </div>
        </>,
        document.body
      )}
    </div>
  );
}
