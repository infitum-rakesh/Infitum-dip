/**
 * Monitoring — Monitoring Workspace (Phase LL/NN). Every tab reads live
 * off real execution data via /api/monitoring/* (see backend
 * app/monitoring/service.py) — nothing here is mocked. Pipeline Runs and
 * Job detail reuse the existing JobLogDrawer live log viewer rather than
 * duplicating it.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  AlertTriangle, Bell, CheckCircle2, Clock, Cpu, Database, ExternalLink, GitBranch,
  Loader2, Plus, RefreshCw, Trash2, X, XCircle, Zap,
} from 'lucide-react';
import { Button, DataTable, StatusBadge } from '@/components/common/index.js';
import { JobLogDrawer } from '@/components/pipeline/JobLogDrawer.jsx';
import { ALERT_CONDITIONS, SLA_STATUS_META, monitoringApi } from '@/services/api/monitoringApi.js';
import './monitoring.css';

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'invoice_processing', label: 'Invoice Processing' },
  { id: 'pipeline_runs', label: 'Pipeline Runs' },
  { id: 'job_runs', label: 'Job Runs' },
  { id: 'data_quality', label: 'Data Quality' },
  { id: 'sla', label: 'SLA Monitoring' },
  { id: 'system_health', label: 'System Health' },
  { id: 'performance', label: 'Performance' },
  { id: 'alerts', label: 'Alerts & Incidents' },
];

const STATUS_VARIANT = {
  PENDING: 'gray', RUNNING: 'info', SUCCEEDED: 'success', FAILED: 'danger', CANCELLED: 'gray',
  SUCCESS: 'success', WARNING: 'warning', CRITICAL: 'danger', PASS: 'success', ERROR: 'danger',
};

function fmtDuration(sec) {
  if (sec == null) return '—';
  if (sec < 60) return `${sec.toFixed(1)}s`;
  return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
}
function fmtDate(d) { return d ? new Date(d).toLocaleString() : '—'; }
function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

export default function Monitoring() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState(searchParams.get('tab') || 'overview');
  const [jobDrawerId, setJobDrawerId] = useState(searchParams.get('jobId') ? Number(searchParams.get('jobId')) : null);
  const navigate = useNavigate();

  const selectTab = (id) => {
    setActiveTab(id);
    const params = {};
    if (id !== 'overview') params.tab = id;
    setSearchParams(params, { replace: true });
  };

  const openJob = (jobId) => setJobDrawerId(jobId);
  const goToLineage = (pipelineId, jobId) => navigate(`/app/lineage?tab=execution&pipelineId=${pipelineId}${jobId ? `&jobId=${jobId}` : ''}`);
  const goToIncident = (incidentId) => navigate(`/app/incidents/${incidentId}`);

  return (
    <div className="mon-page">
      <div className="mon-header">
        <div>
          <h1 className="mon-title">Monitoring</h1>
          <p className="mon-subtitle">Real-time pipeline execution, data quality, SLA and system health — reads live off actual run history.</p>
        </div>
      </div>

      <div className="mon-tabs">
        {TABS.map((t) => (
          <button key={t.id} className={`mon-tab-btn ${activeTab === t.id ? 'active' : ''}`} onClick={() => selectTab(t.id)}>{t.label}</button>
        ))}
      </div>

      {activeTab === 'overview' && <OverviewTab onOpenJob={openJob} onGoTab={selectTab} />}
      {activeTab === 'invoice_processing' && <InvoiceProcessingTab />}
      {activeTab === 'pipeline_runs' && <PipelineRunsTab onOpenJob={openJob} onGoLineage={goToLineage} />}
      {activeTab === 'job_runs' && <JobRunsTab />}
      {activeTab === 'data_quality' && <DataQualityTab />}
      {activeTab === 'sla' && <SlaTab />}
      {activeTab === 'system_health' && <SystemHealthTab />}
      {activeTab === 'performance' && <PerformanceTab />}
      {activeTab === 'alerts' && <AlertsTab onGoIncident={goToIncident} onOpenJob={openJob} />}

      {jobDrawerId && (
        <JobLogDrawer jobId={jobDrawerId} onClose={() => setJobDrawerId(null)} />
      )}
    </div>
  );
}

// ── Overview ──────────────────────────────────────────────────

function OverviewTab({ onOpenJob, onGoTab }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  const load = useCallback(() => {
    monitoringApi.getOverview(7).then(setData).catch((e) => setError(apiErrorMessage(e, 'Could not load overview.')));
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);

  if (error) return <div className="mon-error-text">{error}</div>;
  if (!data) return <div className="mon-muted">Loading…</div>;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="mon-kpi-grid">
        <div className="mon-kpi-card"><span className="mon-kpi-label">Jobs (7d)</span><span className="mon-kpi-value">{data.jobs_total}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Success Rate</span><span className={`mon-kpi-value ${data.success_rate_pct >= 95 ? 'success' : data.success_rate_pct >= 80 ? 'warning' : 'danger'}`}>{data.success_rate_pct != null ? `${data.success_rate_pct}%` : '—'}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Failed</span><span className={`mon-kpi-value ${data.jobs_failed ? 'danger' : ''}`}>{data.jobs_failed}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Running Now</span><span className="mon-kpi-value">{data.jobs_running}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Records Processed</span><span className="mon-kpi-value">{data.records_processed.toLocaleString()}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Open Incidents</span><span className={`mon-kpi-value ${data.incidents_open ? 'danger' : ''}`}>{data.incidents_open}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Load Failures</span><span className="mon-kpi-value">{data.loads_failed}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Validation Failures</span><span className="mon-kpi-value">{data.validations_failed}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Critical Anomalies</span><span className={`mon-kpi-value ${data.anomalies_critical ? 'danger' : ''}`}>{data.anomalies_critical}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Warning Anomalies</span><span className={`mon-kpi-value ${data.anomalies_warning ? 'warning' : ''}`}>{data.anomalies_warning}</span></div>
      </div>

      <div className="mon-card">
        <div className="mon-card-title">
          Recent Pipeline Runs
          <button className="mon-link-btn" onClick={() => onGoTab('pipeline_runs')}>View all <ExternalLink size={11} /></button>
        </div>
        {data.recent_jobs.length === 0 ? <div className="mon-muted">No runs yet.</div> : (
          <table className="data-table" style={{ width: '100%' }}>
            <thead><tr><th>Pipeline</th><th>Status</th><th>Started</th><th>Duration</th><th>Rows Out</th><th /></tr></thead>
            <tbody>
              {data.recent_jobs.map((j) => (
                <tr key={j.id}>
                  <td>{j.pipeline_name}</td>
                  <td><StatusBadge variant={STATUS_VARIANT[j.status] ?? 'gray'} dot>{j.status}</StatusBadge></td>
                  <td>{fmtDate(j.started_at)}</td>
                  <td>{fmtDuration(j.duration_seconds)}</td>
                  <td>{j.records_out ?? '—'}</td>
                  <td><button className="mon-link-btn" onClick={() => onOpenJob(j.id)}>View Logs</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ── Invoice Processing (INV21) — real decision/ERP/review rollup off
// Invoice Intelligence's own lifecycle events, see monitoring/repository.
// py's invoice_processing_overview() for exactly what backs each number. ──

function fmtSeconds(sec) {
  if (sec == null) return '—';
  if (sec < 1) return `${Math.round(sec * 1000)}ms`;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
}

function InvoiceProcessingTab() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  const load = useCallback(() => {
    monitoringApi.getInvoiceProcessing(7).then(setData).catch((e) => setError(apiErrorMessage(e, 'Could not load invoice processing metrics.')));
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);

  if (error) return <div className="mon-error-text">{error}</div>;
  if (!data) return <div className="mon-muted">Loading…</div>;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="mon-kpi-grid">
        <div className="mon-kpi-card"><span className="mon-kpi-label">Decided (7d)</span><span className="mon-kpi-value">{data.decided_total}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Straight-Through Rate</span><span className={`mon-kpi-value ${data.straight_through_rate_pct >= 70 ? 'success' : data.straight_through_rate_pct >= 40 ? 'warning' : 'danger'}`}>{data.straight_through_rate_pct != null ? `${data.straight_through_rate_pct}%` : '—'}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Review Required</span><span className="mon-kpi-value">{data.review_required_count}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">Review Queue Depth</span><span className={`mon-kpi-value ${data.review_queue_depth ? 'warning' : ''}`}>{data.review_queue_depth}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">ERP Calls (7d)</span><span className="mon-kpi-value">{data.erp_calls_total}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">ERP Succeeded</span><span className="mon-kpi-value success">{data.erp_succeeded}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">ERP Failed</span><span className={`mon-kpi-value ${data.erp_failed ? 'danger' : ''}`}>{data.erp_failed}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">ERP Dead-Lettered</span><span className={`mon-kpi-value ${data.erp_dead_letter ? 'danger' : ''}`}>{data.erp_dead_letter}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">ERP Retry Attempts</span><span className="mon-kpi-value">{data.erp_retry_attempts}</span></div>
        <div className="mon-kpi-card"><span className="mon-kpi-label">ERP In Flight</span><span className={`mon-kpi-value ${data.erp_in_flight ? 'warning' : ''}`}>{data.erp_in_flight}</span></div>
      </div>

      <div className="mon-card">
        <div className="mon-card-title">Average Stage Duration</div>
        <p className="mon-muted" style={{ margin: '0 0 10px' }}>
          Real deltas between this invoice module's own lifecycle-event timestamps — never estimated. There is no
          persistent worker-pool concept in this build (ERP posting runs as a background task, not a worker with an
          id), so "worker activity" is reported above as ERP In Flight (real PENDING/RETRYING attempts right now)
          rather than fabricated worker identities.
        </p>
        <table className="data-table" style={{ width: '100%' }}>
          <thead><tr><th>Stage</th><th>Average Duration</th></tr></thead>
          <tbody>
            <tr><td>Decision → ERP Submitted</td><td>{fmtSeconds(data.avg_decision_to_erp_submit_seconds)}</td></tr>
            <tr><td>ERP Submitted → Succeeded</td><td>{fmtSeconds(data.avg_erp_submit_to_succeeded_seconds)}</td></tr>
            <tr><td>ERP Succeeded → Reconciled</td><td>{fmtSeconds(data.avg_succeeded_to_reconciled_seconds)}</td></tr>
          </tbody>
        </table>
      </div>

      <div className="mon-card">
        <div className="mon-card-title">Anomaly Findings</div>
        <p className="mon-muted" style={{ margin: '0 0 10px' }}>
          Real-time statistical check (same Z-SCORE detector the standalone Anomaly Detection module uses) run
          against this org's own invoice history, right before an otherwise-STRAIGHT_THROUGH invoice would auto-post.
          It never creates an Anomaly Job/Execution row on that module's own screen — a per-invoice check has no
          dataset to attach a job to — so this table is the only place its findings surface.
        </p>
        <div className="mon-kpi-grid" style={{ marginBottom: 12 }}>
          <div className="mon-kpi-card"><span className="mon-kpi-label">Checks Run (7d)</span><span className="mon-kpi-value">{data.anomaly_checks_run}</span></div>
          <div className="mon-kpi-card"><span className="mon-kpi-label">Findings</span><span className="mon-kpi-value">{data.anomaly_findings_total}</span></div>
          <div className="mon-kpi-card"><span className="mon-kpi-label">Critical</span><span className={`mon-kpi-value ${data.anomaly_critical_count ? 'danger' : ''}`}>{data.anomaly_critical_count}</span></div>
          <div className="mon-kpi-card"><span className="mon-kpi-label">Downgraded to Review</span><span className={`mon-kpi-value ${data.anomaly_downgraded_count ? 'warning' : ''}`}>{data.anomaly_downgraded_count}</span></div>
        </div>
        {data.recent_anomaly_findings.length === 0 ? (
          <p className="mon-muted" style={{ margin: 0 }}>No anomaly findings in the last 7 days.</p>
        ) : (
          <table className="data-table" style={{ width: '100%' }}>
            <thead><tr><th>Invoice</th><th>Check</th><th>Severity</th><th>Message</th><th>Outcome</th><th>When</th></tr></thead>
            <tbody>
              {data.recent_anomaly_findings.flatMap((event) => event.findings.map((f, idx) => (
                <tr key={`${event.invoice_id}-${idx}`}>
                  {idx === 0 && <td rowSpan={event.findings.length}>{event.invoice_number ?? `#${event.invoice_id}`}</td>}
                  <td>{f.check}</td>
                  <td><StatusBadge variant={f.severity === 'CRITICAL' ? 'danger' : f.severity === 'HIGH' ? 'warning' : 'gray'}>{f.severity}</StatusBadge></td>
                  <td style={{ maxWidth: 360 }}>{f.message}</td>
                  {idx === 0 && <td rowSpan={event.findings.length}>{event.downgraded_to_review ? <StatusBadge variant="warning">Downgraded to Review</StatusBadge> : <span className="mon-muted">Logged only</span>}</td>}
                  {idx === 0 && <td rowSpan={event.findings.length}>{fmtDate(event.created_at)}</td>}
                </tr>
              )))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ── Pipeline Runs ────────────────────────────────────────────

function PipelineRunsTab({ onOpenJob, onGoLineage }) {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const pageSize = 20;

  const load = useCallback(() => {
    setLoading(true);
    monitoringApi.listPipelineRuns({ page, page_size: pageSize, status: status || undefined })
      .then((r) => { setItems(r.items); setTotal(r.total); })
      .catch((e) => setError(apiErrorMessage(e, 'Could not load pipeline runs.')))
      .finally(() => setLoading(false));
  }, [page, status]);
  useEffect(() => { load(); }, [load]);

  const columns = [
    { key: 'pipeline_name', header: 'Pipeline' },
    { key: 'pipeline_version', header: 'Version', render: (_, r) => `v${r.pipeline_version}` },
    { key: 'status', header: 'Status', render: (_, r) => <StatusBadge variant={STATUS_VARIANT[r.status] ?? 'gray'} dot>{r.status}</StatusBadge> },
    { key: 'trigger', header: 'Trigger' },
    { key: 'started_at', header: 'Started', render: (_, r) => fmtDate(r.started_at) },
    { key: 'duration_seconds', header: 'Duration', render: (_, r) => fmtDuration(r.duration_seconds) },
    { key: 'records_out', header: 'Rows Out', render: (_, r) => r.records_out ?? '—' },
    { key: 'records_rejected', header: 'Rejected', render: (_, r) => r.records_rejected > 0 ? <span style={{ color: '#DC2626' }}>{r.records_rejected}</span> : '0' },
    {
      key: 'sla_status', header: 'SLA',
      render: (_, r) => { const m = SLA_STATUS_META[r.sla_status] ?? SLA_STATUS_META.NOT_EVALUATED; return <StatusBadge variant={m.variant}>{m.label}</StatusBadge>; },
    },
    {
      key: 'actions', header: '', render: (_, r) => (
        <div className="mon-row-actions">
          <button className="mon-link-btn" onClick={() => onOpenJob(r.id)}>Logs</button>
          <button className="mon-link-btn" onClick={() => onGoLineage(r.pipeline_id, r.id)}><GitBranch size={11} /> Lineage</button>
        </div>
      ),
    },
  ];

  return (
    <div className="mon-card">
      <div className="mon-filters">
        <select className="mon-select" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
          <option value="">All statuses</option>
          {['PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <Button variant="ghost" size="sm" icon={<RefreshCw size={12} />} onClick={load}>Refresh</Button>
      </div>
      {error && <div className="mon-error-text">{error}</div>}
      <DataTable
        columns={columns} data={items} loading={loading}
        pagination={{ page, pageSize, total, onChange: setPage }}
        emptyState={<div className="mon-muted">No pipeline runs yet — run a pipeline from Pipeline Builder.</div>}
      />
    </div>
  );
}

// ── Job Runs (Schedule-triggered executions) ─────────────────

function JobRunsTab() {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const pageSize = 20;

  useEffect(() => {
    setLoading(true);
    monitoringApi.listScheduleRuns({ page, page_size: pageSize })
      .then((r) => { setItems(r.items); setTotal(r.total); })
      .catch((e) => setError(apiErrorMessage(e, 'Could not load job runs.')))
      .finally(() => setLoading(false));
  }, [page]);

  const columns = [
    { key: 'schedule_id', header: 'Schedule #' },
    { key: 'status', header: 'Status', render: (_, r) => <StatusBadge variant={STATUS_VARIANT[r.status] ?? 'gray'} dot>{r.status}</StatusBadge> },
    { key: 'trigger', header: 'Trigger' },
    { key: 'started_at', header: 'Started', render: (_, r) => fmtDate(r.started_at) },
    { key: 'duration_seconds', header: 'Duration', render: (_, r) => fmtDuration(r.duration_seconds) },
    { key: 'records_processed', header: 'Processed' },
    { key: 'records_failed', header: 'Failed' },
  ];

  return (
    <div className="mon-card">
      <div className="mon-muted" style={{ marginBottom: 10 }}>
        Job Runs are Schedule-triggered executions (see the Schedules module). See the "Scheduler" check on the System Health tab for a disclosure of how these currently execute.
      </div>
      {error && <div className="mon-error-text">{error}</div>}
      <DataTable columns={columns} data={items} loading={loading} pagination={{ page, pageSize, total, onChange: setPage }}
        emptyState={<div className="mon-muted">No scheduled job runs yet.</div>} />
    </div>
  );
}

// ── Data Quality ──────────────────────────────────────────────

function DataQualityTab() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => { monitoringApi.getDataQuality({ page: 1, page_size: 20 }).then(setData).catch((e) => setError(apiErrorMessage(e, 'Could not load data quality.'))); }, []);

  if (error) return <div className="mon-error-text">{error}</div>;
  if (!data) return <div className="mon-muted">Loading…</div>;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="mon-card">
        <div className="mon-card-title">Validation Runs <span className="mon-muted">({data.validation_total})</span></div>
        {data.validation_runs.length === 0 ? <div className="mon-muted">No validation runs yet — see the Validation module.</div> : (
          <table className="data-table" style={{ width: '100%' }}>
            <thead><tr><th>Run #</th><th>Status</th><th>Total Records</th><th>Started</th></tr></thead>
            <tbody>
              {data.validation_runs.map((r) => (
                <tr key={r.id}><td>{r.id}</td><td><StatusBadge variant={STATUS_VARIANT[r.status] ?? 'gray'}>{r.status}</StatusBadge></td><td>{r.total_records ?? '—'}</td><td>{fmtDate(r.started_at)}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="mon-card">
        <div className="mon-card-title">Reconciliation Runs <span className="mon-muted">({data.reconciliation_total})</span></div>
        {data.reconciliation_runs.length === 0 ? <div className="mon-muted">No reconciliation runs yet.</div> : (
          <table className="data-table" style={{ width: '100%' }}>
            <thead><tr><th>Run #</th><th>Status</th><th>Source</th><th>Target</th><th>Matched</th><th>Missing</th><th>Extra</th><th>Mismatched</th></tr></thead>
            <tbody>
              {data.reconciliation_runs.map((r) => (
                <tr key={r.id}>
                  <td>{r.id}</td><td><StatusBadge variant={STATUS_VARIANT[r.status] ?? 'gray'}>{r.status}</StatusBadge></td>
                  <td>{r.source_count ?? '—'}</td><td>{r.target_count ?? '—'}</td><td>{r.matched_count ?? '—'}</td>
                  <td>{r.missing_count ?? '—'}</td><td>{r.extra_count ?? '—'}</td><td>{r.mismatched_count ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="mon-card">
        <div className="mon-card-title">Anomaly Executions <span className="mon-muted">({data.anomaly_total})</span></div>
        {data.anomaly_executions.length === 0 ? <div className="mon-muted">No anomaly executions yet — see the Anomaly Detection module.</div> : (
          <table className="data-table" style={{ width: '100%' }}>
            <thead><tr><th>Execution #</th><th>Job #</th><th>Status</th><th>Overall</th><th>Anomalies</th><th>Critical</th><th>Warning</th><th>Trigger</th><th>Started</th></tr></thead>
            <tbody>
              {data.anomaly_executions.map((e) => (
                <tr key={e.id}>
                  <td>{e.id}</td><td>{e.job_id}</td>
                  <td><StatusBadge variant={STATUS_VARIANT[e.status] ?? 'gray'}>{e.status}</StatusBadge></td>
                  <td>{e.overall_status ? <StatusBadge variant={STATUS_VARIANT[e.overall_status] ?? 'gray'}>{e.overall_status}</StatusBadge> : '—'}</td>
                  <td>{e.anomalies_found}</td><td>{e.critical_count}</td><td>{e.warning_count}</td>
                  <td>{e.trigger}</td><td>{fmtDate(e.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ── SLA Monitoring ────────────────────────────────────────────

function SlaTab() {
  const [items, setItems] = useState([]);
  const [pipelines, setPipelines] = useState([]);
  const [error, setError] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ pipeline_id: '', max_duration_minutes: '', max_failure_count: 1, expected_completion_time: '', auto_create_incident: true });

  const load = useCallback(() => {
    monitoringApi.listSla().then(setItems).catch((e) => setError(apiErrorMessage(e, 'Could not load SLA definitions.')));
  }, []);
  useEffect(() => {
    load();
    monitoringApi.getOverview(7).then((o) => setPipelines(o.recent_jobs ? [...new Map(o.recent_jobs.map((j) => [j.pipeline_id, j.pipeline_name])).entries()] : [])).catch(() => {});
  }, [load]);

  const handleCreate = async () => {
    try {
      await monitoringApi.createSla({
        pipeline_id: Number(form.pipeline_id),
        max_duration_minutes: form.max_duration_minutes ? Number(form.max_duration_minutes) : null,
        max_failure_count: Number(form.max_failure_count) || 1,
        expected_completion_time: form.expected_completion_time || null,
        auto_create_incident: form.auto_create_incident,
      });
      setShowCreate(false);
      setForm({ pipeline_id: '', max_duration_minutes: '', max_failure_count: 1, expected_completion_time: '', auto_create_incident: true });
      load();
    } catch (e) { setError(apiErrorMessage(e, 'Could not create SLA definition.')); }
  };

  const handleDelete = async (id) => { await monitoringApi.deleteSla(id).catch(() => {}); load(); };

  return (
    <div className="mon-card">
      <div className="mon-card-title">
        SLA Definitions
        <Button size="sm" icon={<Plus size={12} />} onClick={() => setShowCreate((s) => !s)}>New SLA</Button>
      </div>
      {error && <div className="mon-error-text">{error}</div>}
      {showCreate && (
        <div className="mon-filters" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
          <input className="mon-input" placeholder="Pipeline ID" value={form.pipeline_id} onChange={(e) => setForm({ ...form, pipeline_id: e.target.value })} style={{ width: 100 }} />
          <input className="mon-input" placeholder="Max duration (min)" value={form.max_duration_minutes} onChange={(e) => setForm({ ...form, max_duration_minutes: e.target.value })} style={{ width: 150 }} />
          <input className="mon-input" placeholder="Max failures" value={form.max_failure_count} onChange={(e) => setForm({ ...form, max_failure_count: e.target.value })} style={{ width: 100 }} />
          <input className="mon-input" placeholder="Expected completion HH:MM" value={form.expected_completion_time} onChange={(e) => setForm({ ...form, expected_completion_time: e.target.value })} style={{ width: 180 }} />
          <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 4 }}>
            <input type="checkbox" checked={form.auto_create_incident} onChange={(e) => setForm({ ...form, auto_create_incident: e.target.checked })} /> Auto-create incident
          </label>
          <Button size="sm" onClick={handleCreate} disabled={!form.pipeline_id}>Save</Button>
        </div>
      )}
      {items.length === 0 ? <div className="mon-muted">No SLA definitions yet. Create one to start tracking Met/Warning/Breached per pipeline run.</div> : (
        <table className="data-table" style={{ width: '100%' }}>
          <thead><tr><th>Pipeline</th><th>Max Duration</th><th>Max Failures</th><th>Expected Completion</th><th>Auto-Incident</th><th>Enabled</th><th /></tr></thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.id}>
                <td>{s.pipeline_name}</td>
                <td>{s.max_duration_minutes ? `${s.max_duration_minutes}m` : '—'}</td>
                <td>{s.max_failure_count}</td>
                <td>{s.expected_completion_time || '—'}</td>
                <td>{s.auto_create_incident ? 'Yes' : 'No'}</td>
                <td>{s.enabled ? 'Yes' : 'No'}</td>
                <td><button className="mon-link-btn" onClick={() => handleDelete(s.id)}><Trash2 size={12} /></button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ── System Health ─────────────────────────────────────────────

function SystemHealthTab() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const load = useCallback(() => { monitoringApi.getSystemHealth().then(setData).catch((e) => setError(apiErrorMessage(e, 'Could not load system health.'))); }, []);
  useEffect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t); }, [load]);

  if (error) return <div className="mon-error-text">{error}</div>;
  if (!data) return <div className="mon-muted">Loading…</div>;

  return (
    <div className="mon-card">
      <div className="mon-card-title">
        System Health
        <Button variant="ghost" size="sm" icon={<RefreshCw size={12} />} onClick={load}>Refresh</Button>
      </div>
      <div className="mon-muted" style={{ marginBottom: 10 }}>Last checked {fmtDate(data.checked_at)}. No credentials, connection strings or secrets are ever shown here.</div>
      <div className="mon-health-grid">
        {data.checks.map((c) => (
          <div className="mon-health-card" key={c.component}>
            <div className="mon-health-card-head">
              <span className="mon-health-name">{c.component}</span>
              <StatusBadge variant={c.status === 'UP' ? 'success' : 'danger'} dot>{c.status}</StatusBadge>
            </div>
            {c.latency_ms != null && <span className="mon-health-detail">{c.latency_ms}ms</span>}
            {c.detail && <span className="mon-health-detail">{c.detail}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Performance ────────────────────────────────────────────────

function PerformanceTab() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => { monitoringApi.getPerformance(7).then(setData).catch((e) => setError(apiErrorMessage(e, 'Could not load performance.'))); }, []);

  if (error) return <div className="mon-error-text">{error}</div>;
  if (!data) return <div className="mon-muted">Loading…</div>;

  const THRESHOLD_VARIANT = { NORMAL: 'success', WARNING: 'warning', CRITICAL: 'danger' };

  return (
    <div className="mon-card">
      <div className="mon-card-title">Performance <span className="mon-muted">avg duration {fmtDuration(data.avg_duration_seconds)}</span></div>
      <div className="mon-muted" style={{ marginBottom: 10 }}>{data.note}</div>
      {data.points.length === 0 ? <div className="mon-muted">No completed runs in the last {data.window_days} days.</div> : data.points.map((p) => (
        <div className="mon-perf-row" key={p.job_id}>
          <div className="mon-perf-row-head">
            <span><strong>{p.pipeline_name}</strong> · {fmtDate(p.completed_at)}</span>
            <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <StatusBadge variant={THRESHOLD_VARIANT[p.threshold]}>{p.threshold}</StatusBadge>
              {fmtDuration(p.duration_seconds)} {p.rows_per_sec != null && `· ${p.rows_per_sec} rows/sec`}
            </span>
          </div>
          <div className="mon-perf-stages">
            {p.stages.map((s) => <span key={s.node_id} className="mon-perf-stage-chip">{s.label}: {s.duration_ms ?? 0}ms ({s.rows_in ?? 0}→{s.rows_out ?? 0})</span>)}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Alerts & Incidents ────────────────────────────────────────

function AlertsTab({ onGoIncident, onOpenJob }) {
  const [events, setEvents] = useState([]);
  const [rules, setRules] = useState([]);
  const [channels, setChannels] = useState([]);
  const [error, setError] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ name: '', condition: 'JOB_FAILED', pipeline_id: '', threshold: '', severity: 'MEDIUM', channelType: 'IN_APP' });

  const load = useCallback(() => {
    monitoringApi.listAlertEvents({ page: 1, page_size: 30 }).then((r) => setEvents(r.items)).catch((e) => setError(apiErrorMessage(e, 'Could not load alerts.')));
    monitoringApi.listAlertRules().then(setRules).catch(() => {});
    monitoringApi.getAvailableChannels().then(setChannels).catch(() => {});
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);

  const handleCreateRule = async () => {
    try {
      await monitoringApi.createAlertRule({
        name: form.name || `${form.condition} alert`,
        condition: form.condition,
        pipeline_id: form.pipeline_id ? Number(form.pipeline_id) : null,
        threshold: form.threshold ? Number(form.threshold) : null,
        severity: form.severity,
        channels: [{ type: form.channelType }],
      });
      setShowCreate(false);
      load();
    } catch (e) { setError(apiErrorMessage(e, 'Could not create alert rule.')); }
  };
  const handleDeleteRule = async (id) => { await monitoringApi.deleteAlertRule(id).catch(() => {}); load(); };
  const handleAck = async (id) => { await monitoringApi.acknowledgeAlertEvent(id).catch(() => {}); load(); };
  const handleResolve = async (id) => { await monitoringApi.resolveAlertEvent(id).catch(() => {}); load(); };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="mon-card">
        <div className="mon-card-title">
          Alert Rules
          <Button size="sm" icon={<Plus size={12} />} onClick={() => setShowCreate((s) => !s)}>New Rule</Button>
        </div>
        {error && <div className="mon-error-text">{error}</div>}
        {showCreate && (
          <div className="mon-filters" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            <input className="mon-input" placeholder="Rule name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} style={{ width: 180 }} />
            <select className="mon-select" value={form.condition} onChange={(e) => setForm({ ...form, condition: e.target.value })}>
              {ALERT_CONDITIONS.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
            <input className="mon-input" placeholder="Pipeline ID (optional)" value={form.pipeline_id} onChange={(e) => setForm({ ...form, pipeline_id: e.target.value })} style={{ width: 150 }} />
            <input className="mon-input" placeholder="Threshold (if applicable)" value={form.threshold} onChange={(e) => setForm({ ...form, threshold: e.target.value })} style={{ width: 160 }} />
            <select className="mon-select" value={form.severity} onChange={(e) => setForm({ ...form, severity: e.target.value })}>
              {['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <select className="mon-select" value={form.channelType} onChange={(e) => setForm({ ...form, channelType: e.target.value })}>
              {channels.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <Button size="sm" onClick={handleCreateRule}>Save</Button>
          </div>
        )}
        {rules.length === 0 ? <div className="mon-muted">No alert rules yet.</div> : (
          <table className="data-table" style={{ width: '100%' }}>
            <thead><tr><th>Name</th><th>Condition</th><th>Pipeline</th><th>Severity</th><th>Channels</th><th /></tr></thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.id}>
                  <td>{r.name}</td><td>{r.condition}</td><td>{r.pipeline_id ?? 'All'}</td>
                  <td><StatusBadge variant={r.severity === 'CRITICAL' || r.severity === 'HIGH' ? 'danger' : 'warning'}>{r.severity}</StatusBadge></td>
                  <td>{r.channels.map((c) => <span key={c.type} className="mon-channel-chip">{c.type}</span>)}</td>
                  <td><button className="mon-link-btn" onClick={() => handleDeleteRule(r.id)}><Trash2 size={12} /></button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="mon-card">
        <div className="mon-card-title">Fired Alerts <Bell size={14} /></div>
        {events.length === 0 ? <div className="mon-muted">No alerts have fired yet.</div> : (
          <table className="data-table" style={{ width: '100%' }}>
            <thead><tr><th>Severity</th><th>Rule</th><th>Message</th><th>Pipeline</th><th>Delivery</th><th>Status</th><th /></tr></thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td><StatusBadge variant={e.severity === 'CRITICAL' || e.severity === 'HIGH' ? 'danger' : 'warning'}>{e.severity}</StatusBadge></td>
                  <td>{e.rule_name}</td>
                  <td>{e.message}</td>
                  <td>{e.pipeline_name ?? '—'} {e.job_id && <button className="mon-link-btn" onClick={() => onOpenJob(e.job_id)}>#{e.job_id}</button>}</td>
                  <td>
                    {e.delivery_results.map((d) => (
                      <span key={d.type} className={d.delivered ? 'mon-delivery-ok' : 'mon-delivery-fail'} title={d.detail} style={{ marginRight: 6, fontSize: 11 }}>
                        {d.delivered ? <CheckCircle2 size={11} /> : <XCircle size={11} />} {d.type}
                      </span>
                    ))}
                  </td>
                  <td><StatusBadge variant={e.status === 'OPEN' ? 'danger' : e.status === 'ACKNOWLEDGED' ? 'warning' : 'success'}>{e.status}</StatusBadge></td>
                  <td className="mon-row-actions">
                    {e.incident_id && <button className="mon-link-btn" onClick={() => onGoIncident(e.incident_id)}>Incident</button>}
                    {e.status === 'OPEN' && <button className="mon-link-btn" onClick={() => handleAck(e.id)}>Ack</button>}
                    {e.status !== 'RESOLVED' && <button className="mon-link-btn" onClick={() => handleResolve(e.id)}>Resolve</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
