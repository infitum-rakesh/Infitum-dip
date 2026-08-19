/**
 * SellerAuditLogs — /app/seller/audit-logs (Seller Portal Phase 8).
 * A real, read-only audit trail over the SellerAuditLog table every
 * prior Seller Portal phase already writes to (Customers, Subscriptions,
 * Purchased Modules, License Management, Seller Users) — see
 * backend/app/services/seller/seller_audit_service.py. This page reads
 * only; it never creates events itself.
 */
import { useCallback, useEffect, useState } from 'react';
import { Download, FileSpreadsheet, RefreshCcw, ScrollText, X } from 'lucide-react';
import { Button, DataTable, SearchBox, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAGINATION, SELLER_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { sellerAuditLogApi } from '@/services/api/seller/sellerAuditLogApi.js';
import { SellerAuditDetailModal } from './SellerAuditDetailModal.jsx';
import './seller-audit-logs.css';

const EMPTY_FILTERS = { sellerUser: '', customer: '', action: 'All', module: 'All', dateFrom: '', dateTo: '' };

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

export default function SellerAuditLogs() {
  const [stats, setStats] = useState(null);
  const [actions, setActions] = useState([]);
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

  const loadStats = useCallback(() => { sellerAuditLogApi.getStats().then(setStats).catch(() => {}); }, []);
  useEffect(() => { loadStats(); }, [loadStats]);

  const queryParams = () => ({
    q: search,
    seller_user: filters.sellerUser || undefined,
    customer: filters.customer || undefined,
    action: filters.action !== 'All' ? filters.action : undefined,
    module: filters.module !== 'All' ? filters.module : undefined,
    date_from: filters.dateFrom ? new Date(filters.dateFrom).toISOString() : undefined,
    date_to: filters.dateTo ? new Date(filters.dateTo + 'T23:59:59').toISOString() : undefined,
  });

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    sellerAuditLogApi
      .list({ ...queryParams(), page, page_size: pageSize })
      .then((res) => { setRows(res.items); setTotal(res.total); setActions(res.actions); setModules(res.modules); })
      .catch(() => setError('Failed to load the Seller audit trail. Please try again.'))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, filters, page, pageSize]);

  useEffect(() => { load(); }, [load]);

  const refreshAll = () => { loadStats(); load(); };
  const handleClearFilters = () => { setFilters(EMPTY_FILTERS); setSearchInput(''); setSearch(''); setPage(1); };
  const setFilter = (key) => (e) => { setFilters((f) => ({ ...f, [key]: e.target.value })); setPage(1); };

  const runExport = async (format) => {
    setExporting(true);
    try {
      const blob = await sellerAuditLogApi.export(format, queryParams());
      downloadBlob(blob, `seller-audit-log.${format === 'xlsx' ? 'xlsx' : 'csv'}`);
    } catch {
      window.alert('Could not export the audit trail.');
    } finally {
      setExporting(false);
    }
  };

  const columns = [
    { key: 'timestamp', header: 'Timestamp', sortable: true, render: (v) => <span className="sal-timestamp-cell">{formatDateTime(v)}</span> },
    { key: 'seller_user_name', header: 'Seller User', render: (v) => <span className="sal-user-cell">{v ?? 'System'}</span> },
    { key: 'customer_name', header: 'Customer', render: (v) => v ?? '—' },
    { key: 'action', header: 'Action', render: (v) => v },
    { key: 'module_label', header: 'Module', render: (v, row) => (v || row.module) ? <span className="sal-module-pill">{v ?? row.module}</span> : '—' },
    { key: 'resource', header: 'Resource', render: (v) => v ? <span className="sal-resource-cell">{v}</span> : '—' },
    { key: 'ip_address', header: 'IP Address', render: (v) => <span className="sal-ip-cell">{v ?? '—'}</span> },
    { key: 'details', header: 'Details', render: (v) => v ? <span className="sal-details-cell" title={v}>{v}</span> : '—' },
  ];

  const hasActiveFilters = search || filters.sellerUser || filters.customer || filters.action !== 'All' || filters.module !== 'All' || filters.dateFrom || filters.dateTo;

  const kpis = [
    { label: 'Total Events', value: stats ? stats.total : '—' },
    { label: 'Today', value: stats ? stats.today : '—' },
    { label: 'This Week', value: stats ? stats.this_week : '—' },
    { label: 'Customers Touched', value: stats ? stats.customers_touched : '—' },
  ];

  return (
    <PermissionGate {...SELLER_PERMISSIONS.SELLER_PORTAL_ACCESS} showReason>
      <div className="sal-page">
        <div className="sal-header">
          <div>
            <h1 className="sal-title">Seller Audit Logs</h1>
            <p className="sal-subtitle">Every customer, subscription, module, license and Seller Portal user change — in one trail.</p>
          </div>
          <div className="sal-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={refreshAll}>Refresh</Button>
            <Button variant="secondary" icon={<Download size={14} />} loading={exporting} onClick={() => runExport('csv')}>Export CSV</Button>
            <Button variant="secondary" icon={<FileSpreadsheet size={14} />} loading={exporting} onClick={() => runExport('xlsx')}>Export Excel</Button>
          </div>
        </div>

        <div className="sal-kpis">
          {kpis.map((k) => (
            <div key={k.label} className="sal-kpi-card">
              <div className="sal-kpi-val">{k.value}</div>
              <div className="sal-kpi-label">{k.label}</div>
            </div>
          ))}
        </div>

        <div className="sal-filters-panel">
          <div className="sal-search-row">
            <SearchBox value={searchInput} onChange={setSearchInput} placeholder="Search seller user, customer, action, module, resource, details…" className="sal-search-box" />
            {hasActiveFilters && (
              <button className="sal-clear-filters-btn" onClick={handleClearFilters}><X size={13} /> Clear all filters</button>
            )}
          </div>
          <div className="sal-filter-grid">
            <div>
              <label className="sal-field-label">Seller User</label>
              <input className="sal-input" value={filters.sellerUser} onChange={setFilter('sellerUser')} placeholder="Filter by Seller user…" />
            </div>
            <div>
              <label className="sal-field-label">Customer</label>
              <input className="sal-input" value={filters.customer} onChange={setFilter('customer')} placeholder="Filter by customer…" />
            </div>
            <div>
              <label className="sal-field-label">Action</label>
              <select className="sal-input" value={filters.action} onChange={setFilter('action')}>
                <option value="All">All Actions</option>
                {actions.map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
            </div>
            <div>
              <label className="sal-field-label">Module</label>
              <select className="sal-input" value={filters.module} onChange={setFilter('module')}>
                <option value="All">All Modules</option>
                {modules.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </div>
            <div>
              <label className="sal-field-label">Date From</label>
              <input type="date" className="sal-input" value={filters.dateFrom} onChange={setFilter('dateFrom')} />
            </div>
            <div>
              <label className="sal-field-label">Date To</label>
              <input type="date" className="sal-input" value={filters.dateTo} onChange={setFilter('dateTo')} />
            </div>
          </div>
        </div>

        <div className="sal-table-panel">
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
                title={hasActiveFilters ? 'No matching events' : 'No Seller Portal events yet'}
                description={hasActiveFilters ? 'Try a different search term or clear your filters.' : 'Customer, subscription, module, license and Seller user changes will show up here.'}
                action={hasActiveFilters ? { label: 'Clear filters', onClick: handleClearFilters } : undefined}
              />
            }
          />
        </div>
      </div>

      <SellerAuditDetailModal entryId={detailId} onClose={() => setDetailId(null)} />
    </PermissionGate>
  );
}
