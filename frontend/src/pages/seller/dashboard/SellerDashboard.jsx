/**
 * SellerDashboard — /app/seller (Seller Portal Phase 1).
 * Real, cross-organization control-plane overview — every number comes
 * from GET /api/seller/dashboard (backend/app/services/seller/
 * dashboard_service.py), which aggregates the platform's actual
 * Organization/Subscription/Document/PipelineRun/etc. tables. Nothing
 * here is mocked.
 */
import { useEffect, useState } from 'react';
import {
  Building2, CheckCircle2, Clock, CreditCard, AlertTriangle,
  DollarSign, Puzzle, Activity, Loader2,
} from 'lucide-react';
import { StatusBadge, EmptyState } from '@/components/common/index.js';
import { MODULE_META } from '@/utils/permissions.js';
import { sellerDashboardApi } from '@/services/api/seller/sellerDashboardApi.js';
import './seller-dashboard.css';

function fmtCurrency(n) {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n || 0);
}
function fmtNumber(n) {
  return new Intl.NumberFormat('en-US').format(n || 0);
}
function fmtDate(iso) {
  const d = new Date(iso);
  const diffMs = Date.now() - d.getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min${mins === 1 ? '' : 's'} ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hr${hrs === 1 ? '' : 's'} ago`;
  const days = Math.round(hrs / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

const KPI_DEFS = [
  { key: 'total_customers', label: 'Customers', icon: Building2, color: 'primary' },
  { key: 'active_customers', label: 'Active Customers', icon: CheckCircle2, color: 'success' },
  { key: 'trial_customers', label: 'Trial Customers', icon: Clock, color: 'warning' },
  { key: 'active_subscriptions', label: 'Active Subscriptions', icon: CreditCard, color: 'info' },
  { key: 'expiring_subscriptions', label: 'Expiring Soon', icon: AlertTriangle, color: 'danger' },
  { key: 'monthly_revenue', label: 'Monthly Revenue', icon: DollarSign, color: 'success', money: true },
  { key: 'licensed_modules', label: 'Licensed Modules', icon: Puzzle, color: 'primary', of: 'modules_total' },
];

// ── Lightweight SVG charts (no external chart library — matches the rest
// of this codebase's hand-rolled DonutChart pattern in
// pages/validation/ValidationResults.jsx) ──────────────────────────────

function GroupedBarChart({ data, series, height = 160 }) {
  const max = Math.max(1, ...data.flatMap((d) => series.map((s) => d[s.key])));
  const barW = 10;
  const groupW = barW * series.length + 10;
  const chartW = Math.max(300, data.length * groupW + 20);
  return (
    <div className="sd-chart-scroll">
      <svg width={chartW} height={height + 24} className="sd-bar-chart">
        {data.map((d, i) => (
          <g key={d.month ?? d.date} transform={`translate(${10 + i * groupW}, 0)`}>
            {series.map((s, j) => {
              const v = d[s.key];
              const h = Math.round((v / max) * height);
              return (
                <rect key={s.key} x={j * barW} y={height - h} width={barW - 2} height={h} fill={s.color} rx={2} />
              );
            })}
            <text x={(groupW - 10) / 2} y={height + 16} textAnchor="middle" className="sd-bar-label">
              {(d.month ?? d.date).slice(5)}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

function HBarList({ items, valueKey, labelFn, max: maxOverride }) {
  const max = maxOverride ?? Math.max(1, ...items.map((i) => i[valueKey]));
  return (
    <div className="sd-hbar-list">
      {items.map((item, i) => (
        <div className="sd-hbar-row" key={i}>
          <span className="sd-hbar-label">{labelFn(item)}</span>
          <div className="sd-hbar-track">
            <div className="sd-hbar-fill" style={{ width: `${Math.max(2, (item[valueKey] / max) * 100)}%` }} />
          </div>
          <span className="sd-hbar-value">{fmtNumber(item[valueKey])}</span>
        </div>
      ))}
    </div>
  );
}

export default function SellerDashboard() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    sellerDashboardApi.getDashboard()
      .then(setData)
      .catch((err) => setError(err?.message || 'Could not load the Seller Dashboard.'))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return <div className="sd-loading"><Loader2 size={20} className="sd-spin" /> Loading Seller Dashboard…</div>;
  }
  if (error) {
    return <div className="sd-banner error"><AlertTriangle size={15} /> {error}</div>;
  }

  const { kpis, platform_usage: usage, customer_growth, subscription_overview, module_adoption, usage_trend, recent_activity } = data;

  return (
    <div className="sd-page">
      <div className="sd-header">
        <h1>Seller Dashboard</h1>
        <p className="sd-subtitle">Manage customers, subscriptions, licenses and platform usage.</p>
      </div>

      {/* KPI cards */}
      <div className="sd-kpis">
        {KPI_DEFS.map((def) => {
          const Icon = def.icon;
          const value = def.money ? fmtCurrency(kpis[def.key]) : fmtNumber(kpis[def.key]);
          return (
            <div className="sd-kpi-card" key={def.key}>
              <div className={`sd-kpi-icon sd-kpi-icon-${def.color}`}><Icon size={17} /></div>
              <div className="sd-kpi-val">{value}{def.of ? <span className="sd-kpi-of"> / {kpis[def.of]}</span> : null}</div>
              <div className="sd-kpi-label">{def.label}</div>
            </div>
          );
        })}
        <div className="sd-kpi-card">
          <div className="sd-kpi-icon sd-kpi-icon-info"><Activity size={17} /></div>
          <div className="sd-kpi-val">{fmtNumber(usage.documents_processed)}</div>
          <div className="sd-kpi-label">Platform Usage <span className="sd-kpi-sub">(documents)</span></div>
        </div>
      </div>

      {/* Charts row 1 */}
      <div className="sd-charts-row">
        <div className="sd-card">
          <div className="sd-card-title">Customer Growth</div>
          <div className="sd-legend">
            <span><i style={{ background: '#4F46E5' }} /> New</span>
            <span><i style={{ background: '#10B981' }} /> Active</span>
            <span><i style={{ background: '#EF4444' }} /> Churned</span>
          </div>
          <GroupedBarChart
            data={customer_growth}
            series={[
              { key: 'new_customers', color: '#4F46E5' },
              { key: 'active_customers', color: '#10B981' },
              { key: 'churned_customers', color: '#EF4444' },
            ]}
          />
        </div>

        <div className="sd-card">
          <div className="sd-card-title">Subscription Overview</div>
          <HBarList
            items={subscription_overview}
            valueKey="count"
            labelFn={(i) => i.status}
            max={Math.max(1, kpis.total_customers)}
          />
        </div>
      </div>

      {/* Charts row 2 */}
      <div className="sd-charts-row">
        <div className="sd-card">
          <div className="sd-card-title">Module Adoption</div>
          <HBarList
            items={module_adoption.slice(0, 8)}
            valueKey="customers"
            labelFn={(i) => MODULE_META[i.module]?.label ?? i.module}
            max={Math.max(1, kpis.total_customers)}
          />
        </div>

        <div className="sd-card">
          <div className="sd-card-title">Usage Trend <span className="sd-card-subtitle">(last 14 days)</span></div>
          <div className="sd-legend">
            <span><i style={{ background: '#4F46E5' }} /> Documents</span>
            <span><i style={{ background: '#F59E0B' }} /> Pipelines</span>
          </div>
          <GroupedBarChart
            data={usage_trend}
            series={[
              { key: 'documents_processed', color: '#4F46E5' },
              { key: 'pipeline_executions', color: '#F59E0B' },
            ]}
          />
        </div>
      </div>

      {/* Recent customer activity */}
      <div className="sd-card">
        <div className="sd-card-title">Recent Customer Activity</div>
        {recent_activity.length === 0 ? (
          <EmptyState icon={<Activity size={26} />} title="No activity yet" description="Customer and user events will appear here as they happen." />
        ) : (
          <div className="sd-table-wrap">
            <table className="sd-table">
              <thead>
                <tr>
                  <th>Customer</th><th>Activity</th><th>Module</th><th>Timestamp</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {recent_activity.map((a, i) => (
                  <tr key={i}>
                    <td>{a.customer}</td>
                    <td>{a.activity}</td>
                    <td>{a.module}</td>
                    <td>{fmtDate(a.timestamp)}</td>
                    <td><StatusBadge variant={a.status === 'Success' ? 'success' : 'danger'} dot>{a.status}</StatusBadge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
