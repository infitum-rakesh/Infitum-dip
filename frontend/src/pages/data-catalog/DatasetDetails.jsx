/**
 * DatasetDetails — /app/catalog/:assetId
 * Tabs: Overview | Columns | Profile | Lineage | Quality | Validations | Incidents | Usage
 */
import { useEffect, useState, useCallback } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import {
  ArrowLeft, Eye, Sparkles, ShieldCheck, Network, Code2, Shuffle, GitBranch, Tag,
  Database, AlertTriangle, Activity, Info,
} from 'lucide-react';
import { Button, StatusBadge, DataTable, EmptyState, Modal, PermissionGate, FullPageSpinner } from '@/components/common/index.js';
import { MODULES, ACTIONS } from '@/utils/constants.js';
import { formatNumber, formatDateTime, formatDate } from '@/utils/formatters.js';
import { dataCatalogApi } from '@/services/api/dataCatalogApi.js';
import './data-catalog.css';

const STATUS_VARIANT = { Active: 'success', Deprecated: 'warning', Draft: 'info', Archived: 'gray' };
const SEVERITY_VARIANT = { high: 'danger', medium: 'warning', low: 'info' };
const INCIDENT_STATUS_VARIANT = { open: 'danger', investigating: 'warning', resolved: 'success' };

function qualityVariant(score) {
  if (score == null) return 'gray';
  if (score >= 90) return 'success';
  if (score >= 70) return 'warning';
  return 'danger';
}

const TABS = [
  { key: 'overview',    label: 'Overview',    icon: Info },
  { key: 'columns',     label: 'Columns',     icon: Database },
  { key: 'profile',     label: 'Profile',     icon: Sparkles },
  { key: 'lineage',     label: 'Lineage',     icon: Network },
  { key: 'quality',     label: 'Quality',     icon: ShieldCheck },
  { key: 'validations', label: 'Validations', icon: ShieldCheck },
  { key: 'incidents',   label: 'Incidents',   icon: AlertTriangle },
  { key: 'usage',       label: 'Usage',       icon: Activity },
];

export default function DatasetDetails() {
  const { assetId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = TABS.some((t) => t.key === searchParams.get('tab')) ? searchParams.get('tab') : 'overview';

  const [asset, setAsset] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tagModalOpen, setTagModalOpen] = useState(false);
  const [newTag, setNewTag] = useState('');
  const [localTags, setLocalTags] = useState([]);

  useEffect(() => {
    setLoading(true);
    setError(null);
    dataCatalogApi.getAsset(assetId)
      .then((a) => { setAsset(a); setLocalTags(a.tags ?? []); })
      .catch(() => setError('Asset not found or failed to load.'))
      .finally(() => setLoading(false));
  }, [assetId]);

  const setTab = useCallback((tab) => setSearchParams({ tab }), [setSearchParams]);

  const handleAddTag = () => {
    const t = newTag.trim();
    if (t && !localTags.includes(t)) setLocalTags((prev) => [...prev, t]);
    setNewTag('');
    setTagModalOpen(false);
  };

  if (loading) return <FullPageSpinner />;

  if (error || !asset) {
    return (
      <div className="dc-page">
        <EmptyState
          icon={<AlertTriangle size={28} />}
          title="Asset not found"
          description={error ?? `No asset matches "${assetId}".`}
          action={{ label: 'Back to Data Catalog', onClick: () => navigate('/app/catalog') }}
        />
      </div>
    );
  }

  return (
    <PermissionGate module={MODULES.DATA_CATALOG} action={ACTIONS.VIEW} showReason>
      <div className="dc-page">
        {/* Header */}
        <div className="dsd-header">
          <button className="dsd-back-btn" onClick={() => navigate('/app/catalog')}>
            <ArrowLeft size={14} /> Data Catalog
          </button>
          <div className="dsd-header-row">
            <div>
              <div className="dsd-title-row">
                <h1 className="dsd-title">{asset.name}</h1>
                <StatusBadge variant={STATUS_VARIANT[asset.status] ?? 'gray'} dot>{asset.status}</StatusBadge>
              </div>
              <p className="dsd-subtitle">{asset.description}</p>
              <div className="dc-tag-row" style={{ marginTop: 8 }}>
                {localTags.map((t) => <span key={t} className="dc-tag-chip">{t}</span>)}
              </div>
            </div>

            {/* Actions bar */}
            <div className="dsd-actions">
              <Button variant="secondary" size="sm" icon={<Eye size={13} />} onClick={() => setTab('overview')}>View</Button>

              <PermissionGate module={MODULES.DATA_CATALOG} action={ACTIONS.PROFILE} fallback={null}>
                <Button variant="secondary" size="sm" icon={<Sparkles size={13} />} onClick={() => setTab('profile')}>Profile</Button>
              </PermissionGate>

              <PermissionGate module={MODULES.VALIDATION} action={ACTIONS.EXECUTE} fallback={null}>
                <Button variant="secondary" size="sm" icon={<ShieldCheck size={13} />} onClick={() => navigate('/app/validation')}>Run Validation</Button>
              </PermissionGate>

              <PermissionGate module={MODULES.LINEAGE} fallback={null}>
                <Button variant="secondary" size="sm" icon={<Network size={13} />} onClick={() => setTab('lineage')}>View Lineage</Button>
              </PermissionGate>

              <PermissionGate module={MODULES.SQL_WORKSPACE} fallback={null}>
                <Button variant="secondary" size="sm" icon={<Code2 size={13} />} onClick={() => navigate('/app/sql')}>Open SQL Workspace</Button>
              </PermissionGate>

              <PermissionGate module={MODULES.TRANSFORMATION} action={ACTIONS.CREATE} fallback={null}>
                <Button variant="secondary" size="sm" icon={<Shuffle size={13} />} onClick={() => navigate('/app/transform')}>Create Transformation</Button>
              </PermissionGate>

              <PermissionGate module={MODULES.DATA_CATALOG} action={ACTIONS.CREATE} fallback={null}>
                <Button variant="secondary" size="sm" icon={<GitBranch size={13} />} onClick={() => navigate('/app/pipeline')}>Create Pipeline</Button>
              </PermissionGate>

              <PermissionGate module={MODULES.DATA_CATALOG} action={ACTIONS.EDIT} fallback={null}>
                <Button variant="primary" size="sm" icon={<Tag size={13} />} onClick={() => setTagModalOpen(true)}>Add Tag</Button>
              </PermissionGate>
            </div>
          </div>
        </div>

        {/* Tab nav */}
        <div className="dsd-tab-nav">
          {TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button key={t.key} className={`dsd-tab-btn ${activeTab === t.key ? 'active' : ''}`} onClick={() => setTab(t.key)}>
                <Icon size={13} /> {t.label}
              </button>
            );
          })}
        </div>

        <div className="dsd-tab-content">
          {activeTab === 'overview'    && <OverviewTab asset={asset} />}
          {activeTab === 'columns'     && <ColumnsTab assetId={assetId} />}
          {activeTab === 'profile'     && <ProfileTab assetId={assetId} isStructured={asset.is_structured} />}
          {activeTab === 'lineage'     && <LineageTab assetId={assetId} />}
          {activeTab === 'quality'     && <QualityTab assetId={assetId} />}
          {activeTab === 'validations' && <ValidationsTab assetId={assetId} />}
          {activeTab === 'incidents'   && <IncidentsTab assetId={assetId} />}
          {activeTab === 'usage'       && <UsageTab assetId={assetId} />}
        </div>
      </div>

      <Modal open={tagModalOpen} onClose={() => setTagModalOpen(false)} title="Add Tag" size="sm"
        footer={<><Button variant="secondary" onClick={() => setTagModalOpen(false)}>Cancel</Button><Button variant="primary" onClick={handleAddTag}>Add</Button></>}>
        <label className="dc-filter-label" style={{ marginBottom: 6, display: 'block' }}>Tag name</label>
        <input
          className="dc-select" style={{ width: '100%' }}
          value={newTag}
          onChange={(e) => setNewTag(e.target.value)}
          placeholder="e.g. pii, certified, finance"
          onKeyDown={(e) => e.key === 'Enter' && handleAddTag()}
          autoFocus
        />
      </Modal>
    </PermissionGate>
  );
}

// ── Overview ─────────────────────────────────────────────────────

function OverviewTab({ asset }) {
  const kpis = [
    { label: 'Rows', value: asset.rows == null ? '—' : formatNumber(asset.rows) },
    { label: 'Columns', value: asset.columns == null ? '—' : asset.columns },
    { label: 'Quality Score', value: asset.quality_score == null ? '—' : `${asset.quality_score}%`, variant: qualityVariant(asset.quality_score) },
    { label: 'Null %', value: asset.null_percent == null ? '—' : `${asset.null_percent}%` },
    { label: 'Duplicate %', value: asset.duplicate_percent == null ? '—' : `${asset.duplicate_percent}%` },
    { label: 'Last Profiled', value: asset.last_profiled ? formatDateTime(asset.last_profiled) : 'Open the Profile tab to run one' },
  ];

  const meta = [
    ['Dataset Name', asset.name],
    ['Owner', asset.owner],
    ['Source', asset.source],
    ['Environment', asset.environment],
    ['Database', asset.database],
    ['Schema', asset.schema],
    ['Object', asset.object],
    ['Created', formatDate(asset.created_at)],
    ['Last Modified', formatDateTime(asset.last_updated)],
  ];

  return (
    <div className="dsd-overview">
      <div className="dsd-kpi-grid">
        {kpis.map((k) => (
          <div key={k.label} className="dc-kpi-card">
            <div className={`dc-kpi-val ${k.variant ? `text-${k.variant === 'danger' ? 'danger' : k.variant === 'warning' ? 'warning' : 'success'}` : ''}`}>{k.value}</div>
            <div className="dc-kpi-label">{k.label}</div>
          </div>
        ))}
      </div>

      <div className="dsd-meta-card">
        <div className="card-title" style={{ marginBottom: 14 }}>Dataset Information</div>
        <div className="dsd-meta-grid">
          {meta.map(([label, value]) => (
            <div key={label} className="dsd-meta-item">
              <span className="dsd-meta-label">{label}</span>
              <span className="dsd-meta-value">{value ?? '—'}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Columns ──────────────────────────────────────────────────────

function ColumnsTab({ assetId }) {
  const [columns, setColumns] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    dataCatalogApi.getColumns(assetId).then((r) => setColumns(r.columns)).finally(() => setLoading(false));
  }, [assetId]);

  const cols = [
    { key: 'name', header: 'Column Name', sortable: true },
    { key: 'data_type', header: 'Data Type', sortable: true },
    { key: 'nullable', header: 'Nullable', render: (v) => v ? 'Yes' : 'No' },
    { key: 'null_percent', header: 'Null %', sortable: true, render: (v) => `${v}%` },
    { key: 'distinct_percent', header: 'Distinct %', sortable: true, render: (v) => `${v}%` },
    { key: 'min', header: 'Min', render: (v) => v ?? '—' },
    { key: 'max', header: 'Max', render: (v) => v ?? '—' },
    {
      key: 'quality_score', header: 'Quality Score', sortable: true,
      render: (v) => <StatusBadge variant={qualityVariant(v)} dot>{v}</StatusBadge>,
    },
  ];

  return (
    <div style={{ padding: 20 }}>
      <DataTable
        columns={cols}
        data={columns ?? []}
        loading={loading}
        emptyState={<EmptyState icon={<Database size={24} />} title="No column metadata" description="This is an unstructured asset — column-level profiling is not applicable." />}
      />
    </div>
  );
}

// ── Profile ──────────────────────────────────────────────────────

function ProfileTab({ assetId, isStructured }) {
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    dataCatalogApi.getProfile(assetId).then(setProfile).finally(() => setLoading(false));
  }, [assetId]);

  if (loading) return <div className="dsd-tab-loading">Loading profile…</div>;
  if (!profile) return null;

  if (!isStructured) {
    return (
      <div style={{ padding: 20 }}>
        <EmptyState icon={<Sparkles size={24} />} title="Limited profiling for unstructured assets"
          description="Row/column-level statistics don't apply to files and documents. Use Document Intelligence to extract structured data first." />
      </div>
    );
  }

  const { null_distribution, duplicate_distribution, unique_values, data_type_distribution, top_values, numeric_stats } = profile;

  return (
    <div className="dsd-profile" style={{ padding: 20 }}>
      {numeric_stats && (
        <div className="dsd-kpi-grid" style={{ marginBottom: 20 }}>
          <div className="dc-kpi-card"><div className="dc-kpi-val">{formatNumber(numeric_stats.min)}</div><div className="dc-kpi-label">Min</div></div>
          <div className="dc-kpi-card"><div className="dc-kpi-val">{formatNumber(numeric_stats.max)}</div><div className="dc-kpi-label">Max</div></div>
          <div className="dc-kpi-card"><div className="dc-kpi-val">{formatNumber(numeric_stats.average)}</div><div className="dc-kpi-label">Average</div></div>
          <div className="dc-kpi-card"><div className="dc-kpi-val">{formatNumber(numeric_stats.std_dev)}</div><div className="dc-kpi-label">Std Deviation</div></div>
        </div>
      )}

      <div className="dsd-profile-grid">
        <div className="dsd-profile-card">
          <div className="card-title">Null Distribution</div>
          <div className="card-subtitle">Overall: {null_distribution.overall_null_percent}% null</div>
          <BarList items={null_distribution.by_column.map((c) => ({ label: c.column, value: c.null_percent }))} suffix="%" color="warning" />
        </div>

        <div className="dsd-profile-card">
          <div className="card-title">Unique Values</div>
          <div className="card-subtitle">Distinct % per column</div>
          <BarList items={Object.entries(unique_values).map(([label, value]) => ({ label, value }))} suffix="%" color="info" />
        </div>

        <div className="dsd-profile-card">
          <div className="card-title">Data Type Distribution</div>
          <BarList
            items={data_type_distribution.map((d) => ({ label: d.type, value: d.count }))}
            max={Math.max(...data_type_distribution.map((d) => d.count), 1)}
            color="primary"
          />
        </div>

        <div className="dsd-profile-card">
          <div className="card-title">Duplicate Distribution</div>
          <div className="dsd-dup-stat">
            <span className="dsd-dup-pct">{duplicate_distribution.overall_duplicate_percent}%</span>
            <span className="text-muted text-sm">
              {duplicate_distribution.duplicate_rows != null ? `${formatNumber(duplicate_distribution.duplicate_rows)} duplicate rows` : 'duplicate rows'}
            </span>
          </div>
        </div>

        <div className="dsd-profile-card" style={{ gridColumn: '1 / -1' }}>
          <div className="card-title">Top Values / Value Distribution</div>
          <BarList items={top_values.map((v) => ({ label: v.value, value: v.percent, extra: `${formatNumber(v.count)} rows` }))} suffix="%" color="success" />
        </div>
      </div>
    </div>
  );
}

function BarList({ items, suffix = '', color = 'primary', max }) {
  const maxVal = max ?? Math.max(...items.map((i) => i.value), 1);
  return (
    <div className="dsd-bar-list">
      {items.map((item) => (
        <div key={item.label} className="dsd-bar-row">
          <span className="dsd-bar-label" title={item.label}>{item.label}</span>
          <div className="dsd-bar-track">
            <div className={`dsd-bar-fill dsd-bar-${color}`} style={{ width: `${Math.min(100, (item.value / maxVal) * 100)}%` }} />
          </div>
          <span className="dsd-bar-value">{item.value}{suffix}{item.extra ? ` · ${item.extra}` : ''}</span>
        </div>
      ))}
    </div>
  );
}

// ── Lineage ──────────────────────────────────────────────────────

function LineageTab({ assetId }) {
  const [lineage, setLineage] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    dataCatalogApi.getLineage(assetId).then(setLineage).finally(() => setLoading(false));
  }, [assetId]);

  if (loading) return <div className="dsd-tab-loading">Loading lineage…</div>;
  if (!lineage) return null;

  return (
    <div style={{ padding: 20 }}>
      {lineage.note && (
        <div style={{ padding: '10px 14px', marginBottom: 16, background: '#F8FAFC', border: '1px solid var(--border)', borderRadius: 8, fontSize: 13, color: 'var(--text-muted)' }}>
          {lineage.note}
        </div>
      )}
      <div className="dsd-lineage-flow">
        {lineage.nodes.map((node, i) => (
          <div key={node.id} className="dsd-lineage-step">
            <div className={`dsd-lineage-node dsd-lineage-node-${node.type} ${node.is_current ? 'current' : ''}`}>
              {node.label}
              {node.is_current && <span className="dsd-lineage-you">this asset</span>}
            </div>
            {i < lineage.nodes.length - 1 && <div className="dsd-lineage-arrow">↓</div>}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Quality ──────────────────────────────────────────────────────

function QualityTab({ assetId }) {
  const [quality, setQuality] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    dataCatalogApi.getQuality(assetId).then(setQuality).finally(() => setLoading(false));
  }, [assetId]);

  if (loading) return <div className="dsd-tab-loading">Loading quality…</div>;
  if (!quality) return null;

  const dims = Object.entries(quality.dimensions);

  const ruleCols = [
    { key: 'rule', header: 'Rule' },
    { key: 'column', header: 'Column' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={v === 'passed' ? 'success' : v === 'warning' ? 'warning' : 'danger'} dot>{v}</StatusBadge> },
    { key: 'failed_records', header: 'Failed Records', render: (v) => formatNumber(v) },
  ];

  return (
    <div style={{ padding: 20 }}>
      <div className="dsd-quality-hero">
        <div className={`dsd-quality-score dsd-quality-score-${qualityVariant(quality.overall_score)}`}>{quality.overall_score == null ? '—' : `${quality.overall_score}%`}</div>
        <div>
          <div className="card-title">Overall Quality Score</div>
          <div className="card-subtitle">
            {quality.last_evaluated ? `Last evaluated ${formatDateTime(quality.last_evaluated)}` : 'Not yet evaluated'} · {formatNumber(quality.failed_records_total)} failed records total
          </div>
          {quality.note && <div className="text-muted text-sm" style={{ marginTop: 4 }}>{quality.note}</div>}
        </div>
      </div>

      <div className="dsd-quality-dims">
        {dims.map(([dim, score]) => (
          <div key={dim} className="dsd-dim-card">
            <div className="dsd-dim-label">{dim}</div>
            <div className="dsd-bar-track"><div className={`dsd-bar-fill dsd-bar-${qualityVariant(score)}`} style={{ width: `${score}%` }} /></div>
            <div className="dsd-dim-score">{score}%</div>
          </div>
        ))}
      </div>

      <div className="card-title" style={{ margin: '20px 0 10px' }}>Quality Rules</div>
      <DataTable columns={ruleCols} data={quality.rules} />
    </div>
  );
}

// ── Validations (catalog-scoped summary; full rule authoring lives in the Validation module) ──

function ValidationsTab({ assetId }) {
  const [quality, setQuality] = useState(null);
  const navigate = useNavigate();

  useEffect(() => { dataCatalogApi.getQuality(assetId).then(setQuality); }, [assetId]);

  return (
    <div style={{ padding: 20 }}>
      <div className="dsd-meta-card" style={{ marginBottom: 16 }}>
        <div className="card-title">Validation Summary</div>
        <p className="text-muted text-sm" style={{ marginTop: 4 }}>
          Rules currently evaluated against this asset. Configure or run new rules in the full Validation module.
        </p>
      </div>
      {quality && (
        <DataTable
          columns={[
            { key: 'rule', header: 'Rule' },
            { key: 'column', header: 'Column' },
            { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={v === 'passed' ? 'success' : v === 'warning' ? 'warning' : 'danger'} dot>{v}</StatusBadge> },
            { key: 'failed_records', header: 'Failed Records', render: (v) => formatNumber(v) },
          ]}
          data={quality.rules}
        />
      )}
      <PermissionGate module={MODULES.VALIDATION} action={ACTIONS.EXECUTE} fallback={null}>
        <div style={{ marginTop: 16 }}>
          <Button variant="primary" size="sm" icon={<ShieldCheck size={13} />} onClick={() => navigate('/app/validation')}>
            Open Validation Module
          </Button>
        </div>
      </PermissionGate>
    </div>
  );
}

// ── Incidents ────────────────────────────────────────────────────

function IncidentsTab({ assetId }) {
  const [incidents, setIncidents] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    dataCatalogApi.getIncidents(assetId).then((r) => setIncidents(r.incidents)).finally(() => setLoading(false));
  }, [assetId]);

  const cols = [
    { key: 'title', header: 'Incident' },
    { key: 'severity', header: 'Severity', render: (v) => <StatusBadge variant={SEVERITY_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={INCIDENT_STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'opened_at', header: 'Opened', render: (v) => formatDateTime(v) },
    { key: 'resolved_at', header: 'Resolved', render: (v) => v ? formatDateTime(v) : '—' },
  ];

  return (
    <div style={{ padding: 20 }}>
      <DataTable
        columns={cols}
        data={incidents ?? []}
        loading={loading}
        emptyState={<EmptyState icon={<AlertTriangle size={24} />} title="No incidents" description="This asset has no open or historical data quality incidents." />}
      />
    </div>
  );
}

// ── Usage ────────────────────────────────────────────────────────

function UsageTab({ assetId }) {
  const [usage, setUsage] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    dataCatalogApi.getUsage(assetId).then(setUsage).finally(() => setLoading(false));
  }, [assetId]);

  if (loading) return <div className="dsd-tab-loading">Loading usage…</div>;
  if (!usage) return null;

  const kpis = [
    { label: 'Queries (30d)', value: formatNumber(usage.queries_last_30d) },
    { label: 'Distinct Users (30d)', value: formatNumber(usage.distinct_users_last_30d) },
    { label: 'Consuming Pipelines', value: formatNumber(usage.consuming_pipelines) },
    { label: 'Consuming Dashboards', value: formatNumber(usage.consuming_dashboards) },
  ];

  const maxQ = Math.max(...usage.daily_query_counts.map((d) => d.queries), 1);

  return (
    <div style={{ padding: 20 }}>
      {usage.tracked === false && (
        <div style={{ padding: '10px 14px', marginBottom: 16, background: '#F8FAFC', border: '1px solid var(--border)', borderRadius: 8, fontSize: 13, color: 'var(--text-muted)' }}>
          {usage.note ?? 'Query-level usage tracking is not available for this asset.'}
        </div>
      )}
      <div className="dsd-kpi-grid" style={{ marginBottom: 20 }}>
        {kpis.map((k) => (
          <div key={k.label} className="dc-kpi-card"><div className="dc-kpi-val">{k.value}</div><div className="dc-kpi-label">{k.label}</div></div>
        ))}
      </div>
      {usage.consuming_pipeline_names?.length > 0 && (
        <div className="dsd-profile-card" style={{ marginBottom: 16 }}>
          <div className="card-title">Consuming Pipelines</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
            {usage.consuming_pipeline_names.map((name) => <span key={name} className="dc-tag-chip">{name}</span>)}
          </div>
        </div>
      )}
      {usage.daily_query_counts.length > 0 && (
        <div className="dsd-profile-card">
          <div className="card-title">Query Volume — Last 14 Days</div>
          <div className="dsd-usage-chart">
            {usage.daily_query_counts.map((d) => (
              <div key={d.date} className="dsd-usage-bar-wrap" title={`${d.date}: ${d.queries} queries`}>
                <div className="dsd-usage-bar" style={{ height: `${Math.max(4, (d.queries / maxQ) * 100)}%` }} />
                <span className="dsd-usage-date">{d.date.slice(5)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
