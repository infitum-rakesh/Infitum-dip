/**
 * Incidents — /app/incidents
 * Centralized monitoring of failures, anomalies and data quality problems
 * across the platform. Reuses MainLayout/Header/Sidebar/DataTable/
 * StatusBadge/Button/Modal/apiClient/auth/permissions — no duplicate
 * components, no separate monitoring framework (see incident_service.py).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import { Plus, RefreshCcw, X, Eye, MoreVertical, Trash2, AlertTriangle } from 'lucide-react';
import { Button, StatusBadge, DataTable, SearchBox, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAGINATION, INCIDENT_PERMISSIONS } from '@/utils/constants.js';
import { incidentApi } from '@/services/api/incidentApi.js';
import { relativeTime } from './incidentFormatters.js';
import { CreateIncidentModal } from './CreateIncidentModal.jsx';
import './incidents.css';

const SEVERITY_VARIANT = { CRITICAL: 'danger', HIGH: 'warning', MEDIUM: 'info', LOW: 'gray' };
const STATUS_VARIANT = { OPEN: 'gray', INVESTIGATING: 'info', MITIGATED: 'warning', RESOLVED: 'success', CLOSED: 'gray' };
const SOURCE_LABEL = {
  PIPELINE: 'Pipeline', CONNECTION: 'Connection', OCR: 'OCR', AI_EXTRACTION: 'AI Extraction',
  TRANSFORMATION: 'Transformation', VALIDATION: 'Validation', RECONCILIATION: 'Reconciliation',
  SCHEDULE: 'Scheduled Job', DATA_QUALITY: 'Data Quality', ANOMALY_DETECTION: 'Anomaly Detection',
  INVOICE_ERP_POSTING: 'Invoice ERP Posting', INVOICE_RECONCILIATION: 'Invoice Reconciliation',
};

const EMPTY_FILTERS = { severity: 'All', status: 'All', source: 'All' };

export default function Incidents() {
  const navigate = useNavigate();

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

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadStats = useCallback(() => { incidentApi.getStats().then(setStats).catch(() => {}); }, []);
  useEffect(() => { loadStats(); }, [loadStats]);

  const loadIncidents = useCallback(() => {
    setLoading(true);
    setError(null);
    incidentApi
      .list({ q: search, severity: filters.severity, status: filters.status, source: filters.source, page, page_size: pageSize })
      .then((res) => { setRows(res.items); setTotal(res.total); })
      .catch(() => setError('Failed to load incidents. Please try again.'))
      .finally(() => setLoading(false));
  }, [search, filters, page, pageSize]);

  useEffect(() => { loadIncidents(); }, [loadIncidents]);

  const refreshAll = () => { loadStats(); loadIncidents(); };
  const handleClearSearch = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const openDetail = (row) => navigate(`/app/incidents/${row.id}`);

  const runDelete = async (row) => {
    if (!window.confirm(`Delete incident "${row.incident_number}"? This cannot be undone.`)) return;
    await incidentApi.delete(row.id);
    refreshAll();
  };

  const columns = [
    { key: 'incident_number', header: 'Incident ID', sortable: true, render: (v) => <span className="inc-id-text">{v}</span> },
    { key: 'title', header: 'Title', render: (v) => <span className="inc-title-text">{v}</span> },
    { key: 'source', header: 'Source', render: (v) => SOURCE_LABEL[v] ?? v },
    { key: 'severity', header: 'Severity', sortable: true, render: (v) => <StatusBadge variant={SEVERITY_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    {
      key: 'status', header: 'Status',
      render: (v, row) => (
        <span>
          <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge>
          {row.sla_breached && <span className="inc-sla-flag" title="SLA breached"><AlertTriangle size={11} /> SLA</span>}
        </span>
      ),
    },
    { key: 'dataset_name', header: 'Dataset', render: (v) => v || '—' },
    { key: 'pipeline_name', header: 'Pipeline', render: (v) => v || '—' },
    { key: 'owner_name', header: 'Owner', render: (v, row) => v ?? row.owner_team ?? '—' },
    { key: 'detected_at', header: 'Detected', sortable: true, render: (v) => relativeTime(v) },
    { key: 'updated_at', header: 'Updated', sortable: true, render: (v) => relativeTime(v) },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="inc-row-actions" onClick={(e) => e.stopPropagation()}>
          <button className="inc-icon-btn" title="View" onClick={() => openDetail(row)}><Eye size={14} /></button>
          <RowMenu row={row} onDelete={() => runDelete(row)} />
        </div>
      ),
    },
  ];

  const kpis = [
    { label: 'Open', value: stats ? stats.open : '—' },
    { label: 'Critical', value: stats ? stats.critical : '—', variant: 'danger' },
    { label: 'High', value: stats ? stats.high : '—', variant: 'warning' },
    { label: 'Investigating', value: stats ? stats.investigating : '—', variant: 'info' },
    { label: 'Resolved', value: stats ? stats.resolved : '—', variant: 'success' },
    { label: 'SLA Breached', value: stats ? stats.sla_breached : '—', variant: 'danger' },
  ];

  const hasActiveFilters = search || filters.severity !== 'All' || filters.status !== 'All' || filters.source !== 'All';

  return (
    <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_VIEW} showReason>
      <div className="inc-page">
        {/* Header */}
        <div className="inc-header">
          <div>
            <h1 className="inc-title">Incidents</h1>
            <p className="inc-subtitle">Monitor and resolve platform, pipeline and data quality incidents.</p>
          </div>
          <div className="inc-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={refreshAll}>Refresh</Button>
            <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_CREATE} fallback={null}>
              <Button variant="primary" icon={<Plus size={14} />} onClick={() => setCreateOpen(true)}>Create Incident</Button>
            </PermissionGate>
          </div>
        </div>

        {/* KPIs */}
        <div className="inc-kpis">
          {kpis.map((k) => (
            <div key={k.label} className="inc-kpi-card">
              <div className={`inc-kpi-val ${k.variant ? `inc-kpi-val-${k.variant}` : ''}`}>{k.value}</div>
              <div className="inc-kpi-label">{k.label}</div>
            </div>
          ))}
        </div>

        {/* Search + inline filters */}
        <div className="inc-search-row">
          <SearchBox value={searchInput} onChange={setSearchInput} placeholder="Search by title, ID, dataset, pipeline…" className="inc-search-box" />
          <select className="inc-input inc-inline-filter" value={filters.severity} onChange={(e) => { setFilters((f) => ({ ...f, severity: e.target.value })); setPage(1); }}>
            <option value="All">All Severities</option>
            {['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select className="inc-input inc-inline-filter" value={filters.status} onChange={(e) => { setFilters((f) => ({ ...f, status: e.target.value })); setPage(1); }}>
            <option value="All">All Statuses</option>
            {['OPEN', 'INVESTIGATING', 'MITIGATED', 'RESOLVED', 'CLOSED'].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select className="inc-input inc-inline-filter" value={filters.source} onChange={(e) => { setFilters((f) => ({ ...f, source: e.target.value })); setPage(1); }}>
            <option value="All">All Sources</option>
            {Object.entries(SOURCE_LABEL).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
          </select>
          {searchInput && (
            <button className="inc-clear-search-btn" onClick={handleClearSearch}><X size={13} /> Clear</button>
          )}
        </div>

        {/* Table */}
        <div className="inc-table-panel">
          <DataTable
            columns={columns}
            data={rows}
            loading={loading}
            error={error}
            onRowClick={openDetail}
            pagination={{ page, pageSize, total, onChange: setPage }}
            emptyState={
              <EmptyState
                icon={<AlertTriangle size={28} />}
                title={hasActiveFilters ? 'No matching incidents' : 'No incidents yet'}
                description={hasActiveFilters ? 'Try a different search term or clear your filters.' : 'Platform, pipeline and data quality incidents will show up here.'}
                action={
                  hasActiveFilters
                    ? { label: 'Clear filters', onClick: () => { handleClearSearch(); setFilters(EMPTY_FILTERS); } }
                    : { label: 'Create Incident', onClick: () => setCreateOpen(true) }
                }
              />
            }
          />
        </div>
      </div>

      <CreateIncidentModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(created) => { setCreateOpen(false); refreshAll(); navigate(`/app/incidents/${created.id}`); }}
      />
    </PermissionGate>
  );
}

// ── Row kebab menu ───────────────────────────────────────────────

function RowMenu({ row, onDelete }) {
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

  return (
    <div className="inc-row-menu">
      <button ref={btnRef} className="inc-icon-btn" title="More actions" onClick={toggle}><MoreVertical size={14} /></button>
      {open && coords && createPortal(
        <>
          <div className="inc-row-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="inc-row-menu-list" style={{ position: 'fixed', top: coords.top, left: coords.left }}>
            <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_DELETE} fallback={null}>
              <button className="inc-row-menu-danger" onClick={() => { setOpen(false); onDelete(); }}><Trash2 size={13} /> Delete</button>
            </PermissionGate>
          </div>
        </>,
        document.body
      )}
    </div>
  );
}
