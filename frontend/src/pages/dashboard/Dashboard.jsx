/**
 * Dashboard — real aggregation from GET /api/dashboard (see
 * backend/app/services/dashboard_service.py). Every KPI comes from a
 * live query; a metric with no meaningful historical baseline (e.g. a
 * rate on its first day of data) shows "—" for its trend rather than a
 * fabricated arrow — see that module's docstring.
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { BarChart3, FileSearch, ShieldCheck, Activity, GitBranch, AlertTriangle, CheckCircle2, Clock, XCircle, Receipt, Wallet, CalendarClock, Eye, AlertOctagon } from 'lucide-react';
import { dashboardApi } from '@/services/api/dashboardApi.js';
import { billingApi } from '@/services/api/billingApi.js';
import { useAccessSummary } from '@/context/AccessSummaryContext.jsx';
import { useAuth } from '@/hooks/useAuth.js';
import './dashboard.css';

const STATUS_VARIANTS = { SUCCEEDED: 'success', FAILED: 'danger', RUNNING: 'info', PENDING: 'info', CANCELLED: 'warning' };
const STATUS_LABEL = { SUCCEEDED: 'completed', FAILED: 'failed', RUNNING: 'running', PENDING: 'running', CANCELLED: 'warning' };

function fmtPct(v) { return v == null ? '—' : `${v}%`; }
function fmtTrend(v) { return v == null ? null : `${v > 0 ? '+' : ''}${v}%`; }

export default function Dashboard() {
  const navigate = useNavigate();
  const { organization } = useAuth();
  const { summary: accessSummary } = useAccessSummary();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // SB30 — Subscription Status widgets: real counts from GET /access/
  // modules-summary + real outstanding-invoice totals from GET /billing/
  // invoices, org-scoped only (Seller/Super Admin see no organization).
  const [billingSummary, setBillingSummary] = useState(null);
  useEffect(() => {
    if (!organization) return;
    billingApi.listInvoices({ status: 'PAYMENT_PENDING', page_size: 100 })
      .then((d) => {
        const total = d.items.reduce((sum, inv) => sum + (inv.total || 0), 0);
        setBillingSummary({ count: d.total, total, currency: d.items[0]?.currency || 'USD' });
      })
      .catch(() => setBillingSummary(null));
  }, [organization]);

  useEffect(() => {
    dashboardApi.get()
      .then(setData)
      .catch(() => setError('Could not load dashboard data.'))
      .finally(() => setLoading(false));
  }, []);

  const m = data?.metrics;

  const METRICS = m ? [
    { label: 'Documents Processed', value: m.documents_processed.toLocaleString(), trend: fmtTrend(m.documents_processed_trend_pct), icon: FileSearch, color: 'primary' },
    { label: 'OCR Success Rate',     value: fmtPct(m.ocr_success_rate),  trend: null, icon: BarChart3, color: 'success' },
    { label: 'Validation Pass Rate', value: fmtPct(m.validation_pass_rate), trend: null, icon: ShieldCheck, color: 'warning' },
    { label: 'Active Incidents',     value: String(m.active_incidents), trend: null, icon: AlertTriangle, color: 'danger' },
    { label: 'Pipeline Runs Today',  value: String(m.pipeline_runs_today), trend: fmtTrend(m.pipeline_runs_today_trend_pct), icon: GitBranch, color: 'info' },
    { label: 'Pipeline Success %',   value: fmtPct(m.pipeline_success_rate), trend: null, icon: Activity, color: 'success' },
  ] : [];

  return (
    <div className="db-page">
      <div className="db-header">
        <div>
          <h1 className="db-title">Dashboard</h1>
          <p className="db-subtitle">Welcome back — here's what's happening today</p>
        </div>
        <div className="db-header-actions">
          <button className="db-action-btn primary" onClick={() => navigate('/app/transform')}>+ Create Pipeline</button>
        </div>
      </div>

      {loading && <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>Loading dashboard…</div>}
      {error && <div style={{ padding: 12, color: 'var(--color-danger-dark)', fontSize: 13 }}>{error}</div>}

      {!loading && !error && (
        <>
          {/* KPI Metrics */}
          <div className="db-metrics">
            {METRICS.map((m) => {
              const Icon = m.icon;
              return (
                <div key={m.label} className="db-metric-card">
                  <div className={`db-metric-icon db-metric-icon-${m.color}`}>
                    <Icon size={18} />
                  </div>
                  <div className="db-metric-val">{m.value}</div>
                  <div className="db-metric-label">{m.label}</div>
                  {m.trend ? (
                    <div className={`db-metric-trend ${m.trend.startsWith('+') ? 'up' : 'down'}`}>
                      {m.trend} <span>vs yesterday</span>
                    </div>
                  ) : (
                    <div className="db-metric-trend"><span>no trend data yet</span></div>
                  )}
                </div>
              );
            })}
          </div>

          {/* SB30 — Subscription Status widgets: real Active/Expiring/
              Expired module counts + real outstanding-invoice total, org-
              scoped only. Hidden for Seller/Super Admin (no organization). */}
          {organization && (
            <div className="card" style={{ marginBottom: 20 }}>
              <div className="card-header">
                <div>
                  <div className="card-title">Subscription Status</div>
                  <div className="card-subtitle">Your organization's real module entitlements and outstanding invoices</div>
                </div>
                <button className="db-action-btn secondary" style={{ fontSize: 12 }} onClick={() => navigate('/app/billing')}>View Billing</button>
              </div>
              <div className="db-sub-widgets">
                <div className="db-sub-widget">
                  <div className="db-sub-widget-icon db-sub-widget-icon-success"><CheckCircle2 size={16} /></div>
                  <div className="db-sub-widget-val">{accessSummary.active_modules.length}</div>
                  <div className="db-sub-widget-label">Active Modules</div>
                </div>
                <div className="db-sub-widget">
                  <div className="db-sub-widget-icon db-sub-widget-icon-warning"><Clock size={16} /></div>
                  <div className="db-sub-widget-val">{accessSummary.expiring_modules.length}</div>
                  <div className="db-sub-widget-label">Expiring Soon</div>
                </div>
                <div className="db-sub-widget">
                  <div className="db-sub-widget-icon db-sub-widget-icon-danger"><XCircle size={16} /></div>
                  <div className="db-sub-widget-val">{Object.keys(accessSummary.expired_modules).length}</div>
                  <div className="db-sub-widget-label">Expired Modules</div>
                </div>
                <div className="db-sub-widget">
                  <div className="db-sub-widget-icon db-sub-widget-icon-info"><Receipt size={16} /></div>
                  <div className="db-sub-widget-val">{billingSummary?.count ?? '—'}</div>
                  <div className="db-sub-widget-label">Pending Payments</div>
                </div>
                <div className="db-sub-widget">
                  <div className="db-sub-widget-icon db-sub-widget-icon-info"><Wallet size={16} /></div>
                  <div className="db-sub-widget-val">
                    {billingSummary ? new Intl.NumberFormat('en-US', { style: 'currency', currency: billingSummary.currency, maximumFractionDigits: 0 }).format(billingSummary.total) : '—'}
                  </div>
                  <div className="db-sub-widget-label">Outstanding Invoices</div>
                </div>
              </div>
            </div>
          )}

          {/* Scheduled Document Intelligence Workflow — real counts off
              DocumentScheduleConfig/DocumentSourceRegistry (see
              dashboard_service.py). Same tables the Schedules module's
              Document Source runs write to; no separate rollup. */}
          <div className="card" style={{ marginBottom: 20 }}>
            <div className="card-header">
              <div>
                <div className="card-title">Scheduled Document Processing</div>
                <div className="card-subtitle">Documents discovered and processed by Document Source schedules</div>
              </div>
              <button className="db-action-btn secondary" style={{ fontSize: 12 }} onClick={() => navigate('/app/schedules')}>View Schedules</button>
            </div>
            <div className="db-sub-widgets">
              <div className="db-sub-widget">
                <div className="db-sub-widget-icon db-sub-widget-icon-info"><CalendarClock size={16} /></div>
                <div className="db-sub-widget-val">{m.active_document_schedules}</div>
                <div className="db-sub-widget-label">Active Document Schedules</div>
              </div>
              <div className="db-sub-widget">
                <div className="db-sub-widget-icon db-sub-widget-icon-success"><CheckCircle2 size={16} /></div>
                <div className="db-sub-widget-val">{m.scheduled_docs_today}</div>
                <div className="db-sub-widget-label">Processed Today</div>
              </div>
              <div className="db-sub-widget">
                <div className="db-sub-widget-icon db-sub-widget-icon-warning"><Eye size={16} /></div>
                <div className="db-sub-widget-val">{m.scheduled_docs_review_required}</div>
                <div className="db-sub-widget-label">Awaiting Review</div>
              </div>
              <div className="db-sub-widget">
                <div className="db-sub-widget-icon db-sub-widget-icon-danger"><AlertOctagon size={16} /></div>
                <div className="db-sub-widget-val">{m.scheduled_docs_failed_today}</div>
                <div className="db-sub-widget-label">Failed Today</div>
              </div>
            </div>
          </div>

          {/* Recent runs */}
          <div className="card">
            <div className="card-header">
              <div>
                <div className="card-title">Recent Pipeline Runs</div>
                <div className="card-subtitle">Most recent jobs across every pipeline</div>
              </div>
              <button className="db-action-btn secondary" style={{ fontSize: 12 }} onClick={() => navigate('/app/pipeline-runs')}>View all</button>
            </div>
            <div className="db-runs-table-wrap">
              <table className="db-runs-table">
                <thead>
                  <tr>
                    <th>Job ID</th>
                    <th>Pipeline</th>
                    <th>Status</th>
                    <th>Records</th>
                    <th>Duration</th>
                    <th>Trigger</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_runs.length === 0 ? (
                    <tr><td colSpan={6} style={{ textAlign: 'center', padding: 24, color: 'var(--text-muted)' }}>No pipeline runs yet — run one from Pipeline Builder.</td></tr>
                  ) : (
                    data.recent_runs.map((run) => (
                      <tr key={run.job_id}>
                        <td><code className="mono" style={{ fontSize: 12 }}>#{run.job_id}</code></td>
                        <td style={{ fontWeight: 500 }}>{run.pipeline_name}</td>
                        <td>
                          <span className={`status-badge status-badge-${STATUS_VARIANTS[run.status] ?? 'gray'}`}>
                            <span className="status-dot" /> {STATUS_LABEL[run.status] ?? run.status.toLowerCase()}
                          </span>
                        </td>
                        <td>{run.records_out?.toLocaleString() ?? '—'}</td>
                        <td className="text-muted">{run.duration_sec != null ? `${Math.round(run.duration_sec)}s` : '—'}</td>
                        <td className="text-muted">{run.trigger}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
