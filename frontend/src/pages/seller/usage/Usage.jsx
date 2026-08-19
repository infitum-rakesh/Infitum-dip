/**
 * Usage — /app/seller/usage (Seller Portal Phase 6).
 * Real, platform-wide usage monitoring — every number comes from
 * /api/seller/usage/* (backend/app/services/seller/usage_service.py),
 * which counts actual Document/PipelineRun/ValidationRun/
 * ReconciliationRun/TargetLoadRun rows per customer. Nothing here is
 * mocked.
 *
 * Filters offered: Customer, Module, Plan — all real dimensions. No
 * Environment filter (no usage-generating table carries that column,
 * see usage_service.py's module docstring) and the table itself shows
 * the current billing period rather than an arbitrary date range (the
 * Daily/Monthly charts below already give real trailing-window trends).
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Gauge, Loader2, RefreshCcw, ShieldAlert, X } from 'lucide-react';
import { Modal, StatusBadge, EmptyState } from '@/components/common/index.js';
import { MODULE_META } from '@/utils/permissions.js';
import { usageApi } from '@/services/api/seller/usageApi.js';
import { customerApi } from '@/services/api/seller/customerApi.js';
import { moduleApi } from '@/services/api/seller/moduleApi.js';
import { planApi } from '@/services/api/seller/subscriptionApi.js';
import './usage.css';

function fmtNumber(n) {
  if (n === null || n === undefined) return '—';
  return new Intl.NumberFormat('en-US').format(n);
}

const STATUS_VARIANT = { HEALTHY: 'success', WARNING: 'warning', NEAR_LIMIT: 'warning', EXCEEDED: 'danger' };
const STATUS_LABEL = { HEALTHY: 'Healthy', WARNING: 'Warning', NEAR_LIMIT: 'Near Limit', EXCEEDED: 'Exceeded' };
const BAR_COLOR = { HEALTHY: '#10B981', WARNING: '#F59E0B', NEAR_LIMIT: '#F97316', EXCEEDED: '#EF4444' };

const KPI_DEFS = [
  { key: 'documents_processed', label: 'Documents Processed' },
  { key: 'ocr_pages', label: 'OCR Pages' },
  { key: 'ai_extraction_requests', label: 'AI Extraction Requests' },
  { key: 'ai_credits', label: 'AI Credits Consumed' },
  { key: 'transformation_records', label: 'Transformation Records' },
  { key: 'validation_records', label: 'Validation Records' },
  { key: 'reconciliation_records', label: 'Reconciliation Records' },
  { key: 'pipeline_executions', label: 'Pipeline Executions' },
  { key: 'storage_gb', label: 'Storage Used', suffix: ' GB' },
];

// ── Lightweight, hand-rolled SVG charts — no chart library in this
// codebase; same approach as SellerDashboard.jsx's GroupedBarChart/HBarList. ──

function HBarList({ items, valueKey, labelFn, max: maxOverride }) {
  if (items.length === 0) return <div className="us-breach-empty">No data yet.</div>;
  const max = maxOverride ?? Math.max(1, ...items.map((i) => i[valueKey]));
  return (
    <div className="us-hbar-list">
      {items.map((item, i) => (
        <div className="us-hbar-row" key={i}>
          <span className="us-hbar-label" title={labelFn(item)}>{labelFn(item)}</span>
          <div className="us-hbar-track">
            <div className="us-hbar-fill" style={{ width: `${Math.max(2, (item[valueKey] / max) * 100)}%` }} />
          </div>
          <span className="us-hbar-value">{fmtNumber(item[valueKey])}</span>
        </div>
      ))}
    </div>
  );
}

function BarChart({ data, xKey, yKeys, height = 150 }) {
  if (data.length === 0) return <div className="us-breach-empty">No data yet.</div>;
  const max = Math.max(1, ...data.flatMap((d) => yKeys.map((y) => d[y.key] || 0)));
  const barW = 10;
  const groupW = barW * yKeys.length + 8;
  const chartW = Math.max(300, data.length * groupW + 20);
  return (
    <div className="us-chart-scroll">
      <svg width={chartW} height={height + 24}>
        {data.map((d, i) => (
          <g key={d[xKey]} transform={`translate(${10 + i * groupW}, 0)`}>
            {yKeys.map((y, j) => {
              const v = d[y.key] || 0;
              const h = Math.round((v / max) * height);
              return <rect key={y.key} x={j * barW} y={height - h} width={barW - 2} height={h} fill={y.color} rx={2} />;
            })}
            <text x={(groupW - 8) / 2} y={height + 16} textAnchor="middle" className="us-bar-label">
              {String(d[xKey]).slice(5)}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

export default function Usage() {
  const [kpis, setKpis] = useState(null);
  const [charts, setCharts] = useState(null);
  const [rows, setRows] = useState([]);
  const [customers, setCustomers] = useState([]);
  const [catalog, setCatalog] = useState([]);
  const [plans, setPlans] = useState([]);

  const [customerFilter, setCustomerFilter] = useState('');
  const [moduleFilter, setModuleFilter] = useState('');
  const [planFilter, setPlanFilter] = useState('');

  const [loading, setLoading] = useState(true);
  const [tableLoading, setTableLoading] = useState(false);
  const [error, setError] = useState('');

  const [drilldown, setDrilldown] = useState(null); // { organization_name, modules, daily_usage }
  const [drilldownLoading, setDrilldownLoading] = useState(false);

  const loadStatic = useCallback(() => {
    Promise.all([
      usageApi.getKpis(),
      usageApi.getCharts(),
      customerApi.list({ page_size: 1000 }),
      moduleApi.getCatalog(),
      planApi.list(),
    ])
      .then(([k, c, custRes, cat, pl]) => {
        setKpis(k);
        setCharts(c);
        setCustomers(custRes.items);
        setCatalog(cat);
        setPlans(pl);
      })
      .catch((err) => setError(err?.message || 'Could not load platform usage.'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { loadStatic(); }, [loadStatic]);

  const loadTable = useCallback(() => {
    setTableLoading(true);
    usageApi.getTable({ customer_id: customerFilter || undefined, module: moduleFilter || undefined, plan: planFilter || undefined })
      .then(setRows)
      .catch(() => setError('Failed to load the usage table.'))
      .finally(() => setTableLoading(false));
  }, [customerFilter, moduleFilter, planFilter]);

  useEffect(() => { loadTable(); }, [loadTable]);

  const refreshAll = () => { loadStatic(); loadTable(); };

  const openDrilldown = (organizationId) => {
    setDrilldownLoading(true);
    usageApi.getCustomerUsage(organizationId)
      .then(setDrilldown)
      .catch(() => window.alert('Could not load usage details for this customer.'))
      .finally(() => setDrilldownLoading(false));
  };

  const period = rows[0]?.period ?? new Date().toLocaleDateString('en-US', { month: 'long', year: 'numeric' });

  if (loading) {
    return <div className="us-page"><Loader2 size={20} className="us-spin" /> Loading platform usage…</div>;
  }
  if (error && !kpis) {
    return <div className="us-page"><AlertTriangle size={15} /> {error}</div>;
  }

  return (
    <div className="us-page">
      <div className="us-header" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h1>Usage</h1>
          <p className="us-subtitle">Platform-wide consumption across every customer and module — {period}.</p>
        </div>
        <button className="us-input" onClick={refreshAll} style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
          <RefreshCcw size={13} /> Refresh
        </button>
      </div>

      {/* KPIs */}
      <div className="us-kpis">
        {KPI_DEFS.map((def) => {
          const v = kpis[def.key];
          return (
            <div className="us-kpi-card" key={def.key}>
              {v === null || v === undefined ? (
                <div className="us-kpi-val-untracked">Not tracked</div>
              ) : (
                <div className="us-kpi-val">{fmtNumber(def.key === 'storage_gb' ? Math.round(v * 10) / 10 : v)}{def.suffix ?? ''}</div>
              )}
              <div className="us-kpi-label">{def.label}</div>
            </div>
          );
        })}
      </div>

      {/* Filters */}
      <div className="us-filters-row">
        <select className="us-input" value={customerFilter} onChange={(e) => setCustomerFilter(e.target.value)}>
          <option value="">All Customers</option>
          {customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <select className="us-input" value={moduleFilter} onChange={(e) => setModuleFilter(e.target.value)}>
          <option value="">All Modules</option>
          {catalog.map((m) => <option key={m.module} value={m.module}>{m.label}</option>)}
        </select>
        <select className="us-input" value={planFilter} onChange={(e) => setPlanFilter(e.target.value)}>
          <option value="">All Plans</option>
          {plans.map((p) => <option key={p.id} value={p.code}>{p.name}</option>)}
        </select>
        <span className="us-filter-note">No Environment filter — no usage table in this platform carries an environment dimension.</span>
      </div>

      {/* Usage table */}
      <div className="us-table-wrap">
        {tableLoading ? (
          <div style={{ padding: 20 }}><Loader2 size={16} className="us-spin" /> Loading…</div>
        ) : rows.length === 0 ? (
          <EmptyState icon={<Gauge size={26} />} title="No usage yet" description="Usage will appear here once customers start using licensed modules." />
        ) : (
          <table className="us-table">
            <thead>
              <tr>
                <th>Customer</th><th>Module</th><th>Usage</th><th>Limit</th><th>Remaining</th><th>%</th><th>Period</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} onClick={() => openDrilldown(r.organization_id)}>
                  <td>{r.organization_name}</td>
                  <td>{MODULE_META[r.module]?.icon ?? ''} {r.module_label}</td>
                  <td>{fmtNumber(r.usage)}</td>
                  <td>{r.limit === null ? '∞' : fmtNumber(r.limit)}</td>
                  <td>{r.remaining === null ? '∞' : fmtNumber(r.remaining)}</td>
                  <td>
                    {r.percentage === null ? '—' : `${r.percentage}%`}
                    {r.percentage !== null && (
                      <span className="us-usage-bar-track">
                        <span className="us-usage-bar-fill" style={{ width: `${Math.min(100, r.percentage)}%`, background: BAR_COLOR[r.status] }} />
                      </span>
                    )}
                  </td>
                  <td>{r.period}</td>
                  <td><StatusBadge variant={STATUS_VARIANT[r.status]} dot>{STATUS_LABEL[r.status]}</StatusBadge></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Charts */}
      {charts && (
        <>
          <div className="us-charts-row">
            <div className="us-card">
              <div className="us-card-title">Usage by Customer</div>
              <HBarList items={charts.usage_by_customer.slice(0, 10)} valueKey="usage" labelFn={(i) => i.customer} />
            </div>
            <div className="us-card">
              <div className="us-card-title">Usage by Module</div>
              <HBarList items={charts.usage_by_module} valueKey="usage" labelFn={(i) => i.module} />
            </div>
          </div>

          <div className="us-charts-row">
            <div className="us-card">
              <div className="us-card-title">Daily Usage <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>(last 14 days)</span></div>
              <div className="us-legend">
                <span><i style={{ background: '#4F46E5' }} /> Documents</span>
                <span><i style={{ background: '#F59E0B' }} /> Pipelines</span>
              </div>
              <BarChart
                data={charts.daily_usage}
                xKey="date"
                yKeys={[{ key: 'documents', color: '#4F46E5' }, { key: 'pipeline_executions', color: '#F59E0B' }]}
              />
            </div>
            <div className="us-card">
              <div className="us-card-title">Monthly Usage <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>(last 6 months)</span></div>
              <div className="us-legend"><span><i style={{ background: '#4F46E5' }} /> Documents</span></div>
              <BarChart data={charts.monthly_usage} xKey="month" yKeys={[{ key: 'documents', color: '#4F46E5' }]} />
            </div>
          </div>

          <div className="us-charts-row">
            <div className="us-card">
              <div className="us-card-title">Top Consumers</div>
              {charts.top_consumers.length === 0 ? (
                <div className="us-breach-empty">No usage yet.</div>
              ) : (
                <div className="us-rank-list">
                  {charts.top_consumers.map((c, i) => (
                    <div className="us-rank-row" key={c.customer}>
                      <span className="us-rank-num">{i + 1}</span>
                      <span className="us-rank-name">{c.customer}</span>
                      <span className="us-rank-value">{fmtNumber(c.usage)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div className="us-card">
              <div className="us-card-title"><ShieldAlert size={14} style={{ verticalAlign: -2, marginRight: 4 }} />Quota Breaches</div>
              {charts.quota_breaches.length === 0 ? (
                <div className="us-breach-empty">No quotas currently exceeded.</div>
              ) : (
                <div className="us-hbar-list">
                  {charts.quota_breaches.map((b, i) => (
                    <div key={i} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 0', borderBottom: i < charts.quota_breaches.length - 1 ? '1px solid #F1F5F9' : 'none' }}>
                      <span style={{ fontSize: 12.5 }}>{b.organization_name} <span style={{ color: 'var(--text-muted)' }}>· {b.module_label}</span></span>
                      <StatusBadge variant="danger" dot>{b.percentage}%</StatusBadge>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {/* Customer usage drilldown */}
      <Modal open={!!drilldown || drilldownLoading} onClose={() => setDrilldown(null)} title={drilldown ? `Usage — ${drilldown.organization_name}` : 'Usage'} size="lg">
        {drilldownLoading || !drilldown ? (
          <div style={{ padding: 20 }}><Loader2 size={16} className="us-spin" /> Loading…</div>
        ) : (
          <>
            <div className="us-table-wrap" style={{ marginBottom: 16 }}>
              <table className="us-table">
                <thead><tr><th>Module</th><th>Usage</th><th>Limit</th><th>Remaining</th><th>%</th><th>Status</th></tr></thead>
                <tbody>
                  {drilldown.modules.map((m, i) => (
                    <tr key={i} style={{ cursor: 'default' }}>
                      <td>{MODULE_META[m.module]?.icon ?? ''} {m.module_label}</td>
                      <td>{fmtNumber(m.usage)}</td>
                      <td>{m.limit === null ? '∞' : fmtNumber(m.limit)}</td>
                      <td>{m.remaining === null ? '∞' : fmtNumber(m.remaining)}</td>
                      <td>{m.percentage === null ? '—' : `${m.percentage}%`}</td>
                      <td><StatusBadge variant={STATUS_VARIANT[m.status]} dot>{STATUS_LABEL[m.status]}</StatusBadge></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="us-card-title">Daily Documents <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>(last 14 days)</span></div>
            <BarChart data={drilldown.daily_usage} xKey="date" yKeys={[{ key: 'documents', color: '#4F46E5' }]} />
          </>
        )}
      </Modal>
    </div>
  );
}
