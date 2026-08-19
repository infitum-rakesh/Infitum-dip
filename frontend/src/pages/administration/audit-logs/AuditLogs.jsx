/**
 * AuditLogs — /app/admin/audit
 * A complete, read-only audit trail — merged from every audit table the
 * backend already keeps (see backend/app/services/audit_log_service.py).
 * Reuses MainLayout/Header/Sidebar/DataTable/StatusBadge/PermissionContext
 * — no duplicate components, no bespoke permission system.
 */
import { useCallback, useEffect, useState } from 'react';
import { Download, FileSpreadsheet, RefreshCcw, ScrollText, X } from 'lucide-react';
import { Button, StatusBadge, DataTable, SearchBox, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAGINATION, AUDIT_LOG_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { auditLogApi } from '@/services/api/auditLogApi.js';
import { AuditLogDetailModal } from './AuditLogDetailModal.jsx';
import './audit-logs.css';

const STATUS_VARIANT = { SUCCESS: 'success', FAILURE: 'danger', WARNING: 'warning', INFO: 'info' };
const EMPTY_FILTERS = { user: '', module: 'All', action: '', status: 'All', resource: '', dateFrom: '', dateTo: '' };

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export default function AuditLogs() {
  const [modules, setModules] = useState([]);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');

  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [exporting, setExporting] = useState(false);

  const [detailId, setDetailId] = useState(null);

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const queryParams = () => ({
    q: search,
    user: filters.user || undefined,
    module: filters.module !== 'All' ? filters.module : undefined,
    action: filters.action || undefined,
    status: filters.status !== 'All' ? filters.status : undefined,
    resource: filters.resource || undefined,
    date_from: filters.dateFrom ? new Date(filters.dateFrom).toISOString() : undefined,
    date_to: filters.dateTo ? new Date(filters.dateTo + 'T23:59:59').toISOString() : undefined,
  });

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    auditLogApi
      .list({ ...queryParams(), page, page_size: pageSize })
      .then((res) => { setRows(res.items); setTotal(res.total); setModules(res.modules); })
      .catch(() => setError('Failed to load audit logs. Please try again.'))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, filters, page, pageSize]);

  useEffect(() => { load(); }, [load]);

  const handleClearFilters = () => { setFilters(EMPTY_FILTERS); setSearchInput(''); setSearch(''); setPage(1); };
  const setFilter = (key) => (e) => { setFilters((f) => ({ ...f, [key]: e.target.value })); setPage(1); };

  const runExport = async (format) => {
    setExporting(true);
    try {
      const blob = await auditLogApi.export(format, queryParams());
      downloadBlob(blob, `audit-logs.${format === 'xlsx' ? 'xlsx' : 'csv'}`);
    } catch {
      window.alert('Could not export audit logs.');
    } finally {
      setExporting(false);
    }
  };

  const columns = [
    { key: 'timestamp', header: 'Timestamp', sortable: true, render: (v) => <span className="aud-timestamp-cell">{formatDateTime(v)}</span> },
    { key: 'user_name', header: 'User', render: (v) => <span className="aud-user-cell">{v ?? 'System'}</span> },
    { key: 'action', header: 'Action', render: (v) => v },
    { key: 'module', header: 'Module', render: (v) => <span className="aud-module-pill">{v}</span> },
    { key: 'resource', header: 'Resource', render: (v) => v ? <span className="aud-resource-cell">{v}</span> : '—' },
    { key: 'ip_address', header: 'IP Address', render: (v) => <span className="aud-ip-cell">{v ?? '—'}</span> },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'details', header: 'Details', render: (v) => v ? <span className="aud-details-cell" title={v}>{v}</span> : '—' },
  ];

  const hasActiveFilters = search || filters.user || filters.module !== 'All' || filters.action || filters.status !== 'All' || filters.resource || filters.dateFrom || filters.dateTo;

  return (
    <PermissionGate {...AUDIT_LOG_PERMISSIONS.AUDIT_LOG_VIEW} showReason>
      <div className="aud-page">
        {/* Header */}
        <div className="aud-header">
          <div>
            <h1 className="aud-title">Audit Logs</h1>
            <p className="aud-subtitle">A complete trail of important actions performed within the application.</p>
          </div>
          <div className="aud-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={load}>Refresh</Button>
            <PermissionGate {...AUDIT_LOG_PERMISSIONS.AUDIT_LOG_EXPORT} fallback={null}>
              <Button variant="secondary" icon={<Download size={14} />} loading={exporting} onClick={() => runExport('csv')}>Export CSV</Button>
              <Button variant="secondary" icon={<FileSpreadsheet size={14} />} loading={exporting} onClick={() => runExport('xlsx')}>Export Excel</Button>
            </PermissionGate>
          </div>
        </div>

        {/* Filters */}
        <div className="aud-filters-panel">
          <div className="aud-search-row">
            <SearchBox value={searchInput} onChange={setSearchInput} placeholder="Search user, action, module, resource, details…" className="aud-search-box" />
            {hasActiveFilters && (
              <button className="aud-clear-filters-btn" onClick={handleClearFilters}><X size={13} /> Clear all filters</button>
            )}
          </div>
          <div className="aud-filter-grid">
            <div>
              <label className="aud-field-label">User</label>
              <input className="aud-input" value={filters.user} onChange={setFilter('user')} placeholder="Filter by user…" />
            </div>
            <div>
              <label className="aud-field-label">Module</label>
              <select className="aud-input" value={filters.module} onChange={setFilter('module')}>
                <option value="All">All Modules</option>
                {modules.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </div>
            <div>
              <label className="aud-field-label">Action</label>
              <input className="aud-input" value={filters.action} onChange={setFilter('action')} placeholder="Filter by action…" />
            </div>
            <div>
              <label className="aud-field-label">Status</label>
              <select className="aud-input" value={filters.status} onChange={setFilter('status')}>
                <option value="All">All Statuses</option>
                {['SUCCESS', 'FAILURE', 'WARNING', 'INFO'].map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            <div>
              <label className="aud-field-label">Resource</label>
              <input className="aud-input" value={filters.resource} onChange={setFilter('resource')} placeholder="Filter by resource…" />
            </div>
            <div>
              <label className="aud-field-label">Date From</label>
              <input type="date" className="aud-input" value={filters.dateFrom} onChange={setFilter('dateFrom')} />
            </div>
            <div>
              <label className="aud-field-label">Date To</label>
              <input type="date" className="aud-input" value={filters.dateTo} onChange={setFilter('dateTo')} />
            </div>
          </div>
        </div>

        {/* Table */}
        <div className="aud-table-panel">
          <DataTable
            columns={columns}
            data={rows}
            loading={loading}
            error={error}
            onRowClick={(row) => setDetailId(row.id)}
            pagination={{ page, pageSize, total, onChange: setPage }}
            emptyState={
              <EmptyState
                icon={<ScrollText size={28} />}
                title={hasActiveFilters ? 'No matching events' : 'No audit events yet'}
                description={hasActiveFilters ? 'Try a different search term or clear your filters.' : 'Actions performed across the application will show up here.'}
                action={hasActiveFilters ? { label: 'Clear filters', onClick: handleClearFilters } : undefined}
              />
            }
          />
        </div>
      </div>

      <AuditLogDetailModal entryId={detailId} onClose={() => setDetailId(null)} />
    </PermissionGate>
  );
}
