/**
 * DataCatalog — page orchestrator.
 * Central place to discover & manage structured (SQL Server, PostgreSQL, Oracle,
 * Snowflake, SAP, Salesforce, NetSuite, Fabric, CSV, Excel, JSON, XML) and
 * unstructured (PDF, Images, Documents, Text files) data assets.
 */
import { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Database, Table2, Files, Layers, Clock, Gauge, RefreshCcw, Eye, Sparkles, X,
} from 'lucide-react';
import { Button, StatusBadge, DataTable, SearchBox, EmptyState, PermissionGate } from '@/components/common/index.js';
import { MODULES, ACTIONS, PAGINATION } from '@/utils/constants.js';
import { formatNumber, formatDateTime } from '@/utils/formatters.js';
import { dataCatalogApi } from '@/services/api/dataCatalogApi.js';
import { SchemaBrowser } from './SchemaBrowser.jsx';
import './data-catalog.css';

const STATUS_VARIANT = { Active: 'success', Deprecated: 'warning', Draft: 'info', Archived: 'gray' };

function qualityVariant(score) {
  if (score >= 90) return 'success';
  if (score >= 70) return 'warning';
  return 'danger';
}

const EMPTY_FILTERS = {
  source_type: 'All', environment: 'All', database: 'All', schema: 'All',
  asset_type: 'All', source: 'All', owner: 'All', status: 'All', quality: 'All', tag: 'All',
};

export default function DataCatalog() {
  const navigate = useNavigate();

  const [stats, setStats]   = useState(null);
  const [facets, setFacets] = useState(null);

  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');

  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  const [rows, setRows]       = useState([]);
  const [total, setTotal]     = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState(null);

  // Debounce free-text search
  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  // Load header KPIs + filter facets once
  useEffect(() => {
    dataCatalogApi.getStats().then(setStats).catch(() => {});
    dataCatalogApi.getFacets().then(setFacets).catch(() => {});
  }, []);

  const loadAssets = useCallback(() => {
    setLoading(true);
    setError(null);
    dataCatalogApi
      .listAssets({ q: search, ...filters, page, page_size: pageSize })
      .then((res) => {
        setRows(res.items);
        setTotal(res.total);
      })
      .catch(() => setError('Failed to load catalog assets. Please try again.'))
      .finally(() => setLoading(false));
  }, [search, filters, page, pageSize]);

  useEffect(() => { loadAssets(); }, [loadAssets]);

  const handleFiltersChange = (next) => { setFilters(next); setPage(1); };
  const handleClearFilters  = () => { setFilters(EMPTY_FILTERS); setPage(1); };
  const handleClearSearch   = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const columns = [
    {
      key: 'name', header: 'Asset Name', sortable: true,
      render: (val, row) => (
        <div className="dc-asset-cell">
          <span className="dc-asset-name">{val}</span>
          {row.tags?.length > 0 && (
            <span className="dc-tag-row">
              {row.tags.slice(0, 3).map((t) => <span key={t} className="dc-tag-chip">{t}</span>)}
            </span>
          )}
        </div>
      ),
    },
    { key: 'asset_type', header: 'Asset Type', sortable: true },
    { key: 'source', header: 'Source', sortable: true },
    { key: 'database', header: 'Database', sortable: true },
    { key: 'schema', header: 'Schema', sortable: true },
    { key: 'rows', header: 'Rows', render: (v) => v == null ? '—' : `${formatNumber(v)} rows` },
    { key: 'columns', header: 'Columns', render: (v) => v == null ? '—' : `${v} columns` },
    {
      key: 'quality_score', header: 'Quality Score', sortable: true,
      render: (v) => <StatusBadge variant={qualityVariant(v)} dot>{v}%</StatusBadge>,
    },
    { key: 'owner', header: 'Owner' },
    { key: 'last_updated', header: 'Last Updated', sortable: true, render: (v) => formatDateTime(v) },
    {
      key: 'status', header: 'Status',
      render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge>,
    },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="dc-row-actions" onClick={(e) => e.stopPropagation()}>
          <button className="dc-icon-btn" title="View" onClick={() => navigate(`/app/catalog/${row.id}`)}>
            <Eye size={14} />
          </button>
          <PermissionGate module={MODULES.DATA_CATALOG} action={ACTIONS.PROFILE} fallback={null}>
            <button
              className="dc-icon-btn"
              title="Profile"
              onClick={() => navigate(`/app/catalog/${row.id}?tab=profile`)}
            >
              <Sparkles size={14} />
            </button>
          </PermissionGate>
        </div>
      ),
    },
  ];

  const kpis = [
    { label: 'Total Assets', value: stats ? formatNumber(stats.total_assets) : '—', icon: Layers, color: 'primary' },
    { label: 'Datasets', value: stats ? formatNumber(stats.datasets) : '—', icon: Database, color: 'info' },
    { label: 'Tables', value: stats ? formatNumber(stats.tables) : '—', icon: Table2, color: 'success' },
    { label: 'Files', value: stats ? formatNumber(stats.files) : '—', icon: Files, color: 'warning' },
    { label: 'Recent Assets', value: stats ? formatNumber(stats.recent_assets?.length ?? 0) : '—', icon: Clock, color: 'info' },
    { label: 'Data Quality Score', value: stats ? `${stats.data_quality_score}%` : '—', icon: Gauge, color: qualityVariant(stats?.data_quality_score ?? 0) },
  ];

  return (
    <PermissionGate module={MODULES.DATA_CATALOG} action={ACTIONS.VIEW} showReason>
      <div className="dc-page">
        {/* Header */}
        <div className="dc-header">
          <div>
            <h1 className="dc-title">Data Catalog</h1>
            <p className="dc-subtitle">Browse and manage datasets, schemas and data assets.</p>
          </div>
          <div className="dc-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={() => { dataCatalogApi.getStats().then(setStats); loadAssets(); }}>
              Refresh
            </Button>
          </div>
        </div>

        {/* KPIs */}
        <div className="dc-kpis">
          {kpis.map((k) => {
            const Icon = k.icon;
            return (
              <div key={k.label} className="dc-kpi-card">
                <div className={`dc-kpi-icon dc-kpi-icon-${k.color}`}><Icon size={16} /></div>
                <div className="dc-kpi-val">{k.value}</div>
                <div className="dc-kpi-label">{k.label}</div>
              </div>
            );
          })}
        </div>

        {/* Search */}
        <div className="dc-search-row">
          <SearchBox
            value={searchInput}
            onChange={setSearchInput}
            placeholder="Search by dataset, table, schema, column, file, owner, source or tag…"
            className="dc-search-box"
          />
          {searchInput && (
            <button className="dc-clear-search-btn" onClick={handleClearSearch}>
              <X size={13} /> Clear
            </button>
          )}
        </div>

        {/* Body: filters + table */}
        <div className="dc-body">
          <SchemaBrowser facets={facets} filters={filters} onChange={handleFiltersChange} onClear={handleClearFilters} />

          <div className="dc-table-panel">
            <DataTable
              columns={columns}
              data={rows}
              loading={loading}
              error={error}
              onRowClick={(row) => navigate(`/app/catalog/${row.id}`)}
              pagination={{ page, pageSize, total, onChange: setPage }}
              emptyState={
                <EmptyState
                  icon={<Database size={28} />}
                  title={search || Object.values(filters).some((v) => v && v !== 'All') ? 'No matching assets' : 'No assets yet'}
                  description={
                    search || Object.values(filters).some((v) => v && v !== 'All')
                      ? 'Try a different search term or clear your filters.'
                      : 'Connect a data source to start populating the catalog.'
                  }
                  action={
                    (search || Object.values(filters).some((v) => v && v !== 'All'))
                      ? { label: 'Clear search & filters', onClick: () => { handleClearSearch(); handleClearFilters(); } }
                      : undefined
                  }
                />
              }
            />
          </div>
        </div>
      </div>
    </PermissionGate>
  );
}
