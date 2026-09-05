/**
 * AnomalyOverview — /app/anomaly
 * Real cross-job dashboard: fetches every saved AnomalyJob
 * (anomalyApi.listJobs()) then fans out anomalyApi.listExecutions(jobId)
 * per job (same N+1-but-bounded-by-job-count pattern
 * ReconciliationList.jsx already uses for its "last run" column — job
 * counts are small, this isn't a scale concern) to build one flat,
 * real execution history across the whole module. KPIs/donut/table below
 * are all computed from that real data — nothing here is a placeholder
 * or a mocked number.
 *
 * Anomaly Jobs (AD7, /app/anomaly/jobs) and Results (AD8,
 * /app/anomaly/results + /app/anomaly/executions/:id) now exist — linked
 * from the header and the Recent Executions rows below. Reports/
 * Configuration land as sibling routes in AD9 and will get the same
 * treatment once built.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Activity, AlertTriangle, Database, FileDown, ListChecks, Plus, Settings2, SlidersHorizontal } from 'lucide-react';
import { Button, StatusBadge } from '@/components/common/index.js';
import { formatDateTime, formatNumber } from '@/utils/formatters.js';
import { anomalyApi } from '@/services/api/anomalyApi.js';
import './anomaly-detection.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

const OVERALL_STATUS_VARIANT = { PASS: 'success', WARNING: 'warning', CRITICAL: 'danger', ERROR: 'gray' };
const RUN_STATUS_VARIANT = { SUCCESS: 'success', PARTIAL_SUCCESS: 'warning', FAILED: 'danger', RUNNING: 'info', QUEUED: 'gray', CANCELLED: 'gray' };
const DONUT_COLORS = { PASS: '#10B981', WARNING: '#F59E0B', CRITICAL: '#EF4444', ERROR: '#6B7280' };

/** Stroke-dasharray SVG ring — same technique ValidationResults.jsx's
 * DonutChart uses, generalized to N segments instead of a fixed 3. */
function StatusDonut({ counts, total, size = 110 }) {
  const sw = 14;
  const r = (size - sw) / 2;
  const c = 2 * Math.PI * r;
  let cum = 0;
  const segments = Object.entries(counts).filter(([, n]) => n > 0);
  const passPct = total > 0 ? Math.round(((counts.PASS || 0) / total) * 100) : 0;

  return (
    <div className="anm-donut-wrap">
      <div style={{ position: 'relative', flexShrink: 0 }}>
        <svg width={size} height={size}>
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#F1F5F9" strokeWidth={sw} />
          {segments.map(([key, n]) => {
            const len = (n / total) * c;
            const offset = c / 4 - cum;
            cum += len;
            return (
              <circle key={key} cx={size / 2} cy={size / 2} r={r} fill="none" stroke={DONUT_COLORS[key]}
                strokeWidth={sw} strokeDasharray={`${len} ${c - len}`} strokeDashoffset={offset} />
            );
          })}
        </svg>
        <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
          <span style={{ fontSize: 22, fontWeight: 800, color: 'var(--text-primary)', lineHeight: 1 }}>{passPct}%</span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontWeight: 600 }}>Pass</span>
        </div>
      </div>
      <div className="anm-donut-legend">
        {['PASS', 'WARNING', 'CRITICAL', 'ERROR'].map((key) => (
          <div key={key} className="anm-donut-legend-item">
            <div className="anm-donut-legend-dot" style={{ background: DONUT_COLORS[key] }} />
            <span className="anm-donut-legend-val">{(counts[key] || 0).toLocaleString()}</span>
            <span className="anm-donut-legend-lbl">{key === 'PASS' ? 'Pass' : key === 'WARNING' ? 'Warning' : key === 'CRITICAL' ? 'Critical' : 'Error'}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function AnomalyOverview() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState([]);
  const [executions, setExecutions] = useState([]); // flattened, each tagged with job_name
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [jobFilter, setJobFilter] = useState('ALL');
  const [statusFilter, setStatusFilter] = useState('ALL');

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    anomalyApi.listJobs()
      .then(async (jobRows) => {
        setJobs(jobRows);
        const perJob = await Promise.all(
          jobRows.map((job) =>
            anomalyApi.listExecutions(job.id)
              .then((runs) => runs.map((r) => ({ ...r, job_name: job.name })))
              .catch(() => []) // one job's history failing to load shouldn't blank the whole dashboard
          )
        );
        const flat = perJob.flat().sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
        setExecutions(flat);
        setLoading(false);
      })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load anomaly detection jobs.')); setLoading(false); });
  }, []);

  useEffect(() => { load(); }, [load]);

  const filteredExecutions = useMemo(() => {
    return executions.filter((e) => {
      if (jobFilter !== 'ALL' && e.job_name !== jobFilter) return false;
      if (statusFilter !== 'ALL' && e.overall_status !== statusFilter) return false;
      return true;
    });
  }, [executions, jobFilter, statusFilter]);

  const kpis = useMemo(() => {
    const anomaliesFound = filteredExecutions.reduce((sum, e) => sum + (e.anomalies_found || 0), 0);
    const criticalCount = filteredExecutions.reduce((sum, e) => sum + (e.critical_count || 0), 0);
    const warningCount = filteredExecutions.reduce((sum, e) => sum + (e.warning_count || 0), 0);
    return [
      { label: 'Anomaly Jobs', value: formatNumber(jobs.length), icon: Database },
      { label: 'Executions', value: formatNumber(filteredExecutions.length), icon: ListChecks },
      { label: 'Anomalies Found', value: formatNumber(anomaliesFound), icon: AlertTriangle, variant: anomaliesFound > 0 ? 'warning' : undefined },
      { label: 'Critical / Warning', value: `${formatNumber(criticalCount)} / ${formatNumber(warningCount)}`, icon: Activity, variant: criticalCount > 0 ? 'danger' : undefined },
    ];
  }, [jobs, filteredExecutions]);

  const statusCounts = useMemo(() => {
    const counts = { PASS: 0, WARNING: 0, CRITICAL: 0, ERROR: 0 };
    for (const e of filteredExecutions) {
      if (e.overall_status && counts[e.overall_status] !== undefined) counts[e.overall_status] += 1;
    }
    return counts;
  }, [filteredExecutions]);
  const statusTotal = filteredExecutions.filter((e) => e.overall_status).length;

  return (
    <div className="anm-page">
      <div className="anm-header">
        <div>
          <div className="anm-title"><Activity size={20} style={{ verticalAlign: -3, marginRight: 8 }} />Anomaly Detection</div>
          <div className="anm-subtitle">Statistical and rule-based anomaly detection across every connected object.</div>
        </div>
        <div className="anm-actions">
          <Button variant="secondary" icon={<ListChecks size={14} />} onClick={() => navigate('/app/anomaly/results')}>
            All Results
          </Button>
          <Button variant="secondary" icon={<FileDown size={14} />} onClick={() => navigate('/app/anomaly/reports')}>
            Reports
          </Button>
          <Button variant="secondary" icon={<Settings2 size={14} />} onClick={() => navigate('/app/anomaly/jobs')}>
            Manage Jobs
          </Button>
          <Button variant="secondary" icon={<SlidersHorizontal size={14} />} onClick={() => navigate('/app/anomaly/configuration')}>
            Configuration
          </Button>
          <Button variant="primary" icon={<Plus size={14} />} onClick={() => navigate('/app/anomaly/jobs/new')}>
            New Job
          </Button>
        </div>
      </div>

      {error && <div className="anm-error-banner">{error}</div>}

      <div className="anm-filters">
        <label>
          Job
          <select value={jobFilter} onChange={(e) => setJobFilter(e.target.value)}>
            <option value="ALL">All jobs</option>
            {jobs.map((j) => <option key={j.id} value={j.name}>{j.name}</option>)}
          </select>
        </label>
        <label>
          Overall status
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="ALL">All statuses</option>
            <option value="PASS">Pass</option>
            <option value="WARNING">Warning</option>
            <option value="CRITICAL">Critical</option>
            <option value="ERROR">Error</option>
          </select>
        </label>
      </div>

      {loading ? (
        <div className="anm-loading">Loading…</div>
      ) : (
        <>
          <div className="anm-kpis">
            {kpis.map((k) => (
              <div key={k.label} className={`anm-kpi-card${k.variant ? ` anm-kpi-${k.variant}` : ''}`}>
                <k.icon size={18} className="anm-kpi-icon" />
                <div className="anm-kpi-value">{k.value}</div>
                <div className="anm-kpi-label">{k.label}</div>
              </div>
            ))}
          </div>

          <div className="anm-panels">
            <div className="anm-panel anm-panel-donut">
              <div className="anm-panel-title">Execution Outcomes</div>
              {statusTotal > 0
                ? <StatusDonut counts={statusCounts} total={statusTotal} />
                : <div className="anm-empty-inline">No completed executions yet.</div>}
            </div>

            <div className="anm-panel anm-panel-table">
              <div className="anm-panel-title">Recent Executions</div>
              {filteredExecutions.length === 0 ? (
                <div className="anm-empty-inline">
                  {jobs.length === 0
                    ? 'No anomaly detection jobs have been created yet.'
                    : 'No executions match the current filters.'}
                </div>
              ) : (
                <table className="anm-table">
                  <thead>
                    <tr>
                      <th>Job</th><th>Status</th><th>Overall</th><th>Anomalies</th><th>Critical</th><th>Warning</th><th>Records Scanned</th><th>Started</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredExecutions.slice(0, 25).map((e) => (
                      <tr key={e.id} style={{ cursor: 'pointer' }} onClick={() => navigate(`/app/anomaly/executions/${e.id}`)}>
                        <td>{e.job_name}</td>
                        <td><StatusBadge variant={RUN_STATUS_VARIANT[e.status] || 'gray'}>{e.status}</StatusBadge></td>
                        <td>{e.overall_status ? <StatusBadge variant={OVERALL_STATUS_VARIANT[e.overall_status] || 'gray'}>{e.overall_status}</StatusBadge> : '—'}</td>
                        <td>{formatNumber(e.anomalies_found || 0)}</td>
                        <td>{formatNumber(e.critical_count || 0)}</td>
                        <td>{formatNumber(e.warning_count || 0)}</td>
                        <td>{e.records_scanned != null ? formatNumber(e.records_scanned) : '—'}</td>
                        <td>{e.started_at ? formatDateTime(e.started_at) : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
