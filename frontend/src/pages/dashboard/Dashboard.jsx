/**
 * Dashboard — real aggregation from GET /api/dashboard (see
 * backend/app/services/dashboard_service.py). Every KPI comes from a
 * live query; a metric with no meaningful historical baseline (e.g. a
 * rate on its first day of data) shows "—" for its trend rather than a
 * fabricated arrow — see that module's docstring.
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { BarChart3, FileSearch, ShieldCheck, Activity, GitBranch, AlertTriangle } from 'lucide-react';
import { dashboardApi } from '@/services/api/dashboardApi.js';
import './dashboard.css';

const STATUS_VARIANTS = { SUCCEEDED: 'success', FAILED: 'danger', RUNNING: 'info', PENDING: 'info', CANCELLED: 'warning' };
const STATUS_LABEL = { SUCCEEDED: 'completed', FAILED: 'failed', RUNNING: 'running', PENDING: 'running', CANCELLED: 'warning' };

function fmtPct(v) { return v == null ? '—' : `${v}%`; }
function fmtTrend(v) { return v == null ? null : `${v > 0 ? '+' : ''}${v}%`; }

export default function Dashboard() {
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

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
