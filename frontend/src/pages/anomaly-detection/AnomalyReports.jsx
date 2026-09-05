/**
 * AnomalyReports — /app/anomaly/reports
 * Real background export jobs (anomaly/reporting.py — CSV/XLSX/JSON/PDF,
 * always generated as a background asyncio task, never synchronously —
 * see anomaly_export_executor.py's module docstring). Reports are scoped
 * to one execution on the backend (no cross-execution list endpoint), so
 * this page fans out the same listJobs()→listExecutions(job) pattern
 * AnomalyResults.jsx uses, then listReports(execution.id) per successful
 * execution, to build one real flat view across the whole org — plus a
 * generate panel that creates a real report and polls it live from
 * PENDING/RUNNING to a terminal state.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { FileDown, Plus } from 'lucide-react';
import { Button, StatusBadge } from '@/components/common/index.js';
import { formatDateTime, formatNumber, formatFileSize } from '@/utils/formatters.js';
import { anomalyApi, REPORT_FORMATS } from '@/services/api/anomalyApi.js';
import './anomaly-detection.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

const REPORT_STATUS_VARIANT = { PENDING: 'gray', RUNNING: 'info', SUCCEEDED: 'success', FAILED: 'danger' };
const POLL_MS = 2000;

export default function AnomalyReports() {
  const [jobs, setJobs] = useState([]);
  const [executionsByJob, setExecutionsByJob] = useState({});
  const [reports, setReports] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [genJobId, setGenJobId] = useState('');
  const [genExecutionId, setGenExecutionId] = useState('');
  const [genFormat, setGenFormat] = useState('CSV');
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState('');

  const load = useCallback(() => {
    setLoading(true); setError('');
    anomalyApi.listJobs()
      .then(async (jobRows) => {
        setJobs(jobRows);
        const execEntries = await Promise.all(
          jobRows.map((job) => anomalyApi.listExecutions(job.id).then((execs) => [job.id, execs]).catch(() => [job.id, []]))
        );
        const byJob = Object.fromEntries(execEntries);
        setExecutionsByJob(byJob);

        const successfulExecs = Object.entries(byJob).flatMap(([jobId, execs]) =>
          execs.filter((e) => e.status === 'SUCCESS' || e.status === 'PARTIAL_SUCCESS')
            .map((e) => ({ ...e, job_id: Number(jobId), job_name: jobRows.find((j) => j.id === Number(jobId))?.name }))
        );
        const reportLists = await Promise.all(
          successfulExecs.map((exec) =>
            anomalyApi.listReports(exec.id)
              .then((rs) => rs.map((r) => ({ ...r, job_name: exec.job_name, execution_id: exec.id })))
              .catch(() => [])
          )
        );
        setReports(reportLists.flat().sort((a, b) => new Date(b.created_at) - new Date(a.created_at)));
        setLoading(false);
      })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load reports.')); setLoading(false); });
  }, []);

  useEffect(() => { load(); }, [load]);

  // Poll any report still in PENDING/RUNNING until it reaches a terminal
  // status — same live-progress pattern the SQL Workspace background
  // export panel uses.
  useEffect(() => {
    const pending = reports.filter((r) => r.status === 'PENDING' || r.status === 'RUNNING');
    if (!pending.length) return undefined;
    const timer = setInterval(() => {
      Promise.all(pending.map((r) => anomalyApi.getReport(r.id).catch(() => null))).then((updated) => {
        setReports((prev) => prev.map((r) => {
          const fresh = updated.find((u) => u && u.id === r.id);
          return fresh ? { ...r, ...fresh } : r;
        }));
      });
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [reports]);

  const jobExecutions = useMemo(
    () => (genJobId ? (executionsByJob[genJobId] || []).filter((e) => e.status === 'SUCCESS' || e.status === 'PARTIAL_SUCCESS') : []),
    [genJobId, executionsByJob],
  );

  const generate = async () => {
    if (!genExecutionId) { setGenError('Pick a successful execution first.'); return; }
    setGenerating(true); setGenError('');
    try {
      const report = await anomalyApi.createReport(genExecutionId, { format: genFormat });
      const job = jobs.find((j) => j.id === Number(genJobId));
      setReports((prev) => [{ ...report, job_name: job?.name, execution_id: Number(genExecutionId) }, ...prev]);
    } catch (err) {
      setGenError(apiErrorMessage(err, 'Could not start report generation.'));
    } finally {
      setGenerating(false);
    }
  };

  const download = (report) => {
    anomalyApi.downloadReport(report.id, `anomaly_report_${report.id}.${(report.format || 'csv').toLowerCase()}`).catch((err) => {
      window.alert(apiErrorMessage(err, 'Download failed.'));
    });
  };

  return (
    <div className="anm-page">
      <div className="anm-header">
        <div>
          <h1 className="anm-title"><FileDown size={20} style={{ verticalAlign: -3, marginRight: 8 }} />Anomaly Reports</h1>
          <p className="anm-subtitle">Real background export jobs — generated as CSV/XLSX/JSON/PDF, never synchronously.</p>
        </div>
      </div>

      <div className="anm-panel" style={{ marginBottom: 20 }}>
        <div className="anm-panel-title">Generate a Report</div>
        <div className="anm-field-row">
          <div className="anm-field">
            <label className="anm-label">Job</label>
            <select
              className="anm-select anm-select-full" value={genJobId}
              onChange={(e) => { setGenJobId(e.target.value); setGenExecutionId(''); }}
            >
              <option value="">Select job…</option>
              {jobs.map((j) => <option key={j.id} value={j.id}>{j.name}</option>)}
            </select>
          </div>
          <div className="anm-field">
            <label className="anm-label">Execution</label>
            <select
              className="anm-select anm-select-full" value={genExecutionId} disabled={!genJobId}
              onChange={(e) => setGenExecutionId(e.target.value)}
            >
              <option value="">{genJobId ? (jobExecutions.length ? 'Select execution…' : 'No successful executions') : 'Pick a job first'}</option>
              {jobExecutions.map((e) => (
                <option key={e.id} value={e.id}>#{e.id} — {formatDateTime(e.started_at || e.created_at)} ({formatNumber(e.anomalies_found || 0)} anomalies)</option>
              ))}
            </select>
          </div>
          <div className="anm-field">
            <label className="anm-label">Format</label>
            <select className="anm-select anm-select-full" value={genFormat} onChange={(e) => setGenFormat(e.target.value)}>
              {REPORT_FORMATS.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </div>
          <div className="anm-field" style={{ display: 'flex', alignItems: 'flex-end' }}>
            <Button variant="primary" icon={<Plus size={14} />} loading={generating} disabled={!genExecutionId} onClick={generate}>
              Generate
            </Button>
          </div>
        </div>
        {genError && <div className="anm-upload-error">{genError}</div>}
      </div>

      {error && <div className="anm-error-banner">{error}</div>}

      <div className="anm-panel">
        <div className="anm-panel-title">All Reports ({reports.length})</div>
        {loading ? (
          <div className="anm-empty-inline">Loading…</div>
        ) : reports.length === 0 ? (
          <div className="anm-empty-inline">No reports generated yet — use the panel above to generate one.</div>
        ) : (
          <table className="anm-table">
            <thead>
              <tr>
                <th>Job</th><th>Execution</th><th>Format</th><th>Status</th><th>Rows</th><th>Size</th><th>Created</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {reports.map((r) => (
                <tr key={r.id}>
                  <td>{r.job_name || '—'}</td>
                  <td>#{r.execution_id}</td>
                  <td>{r.format}</td>
                  <td><StatusBadge variant={REPORT_STATUS_VARIANT[r.status] || 'gray'} dot={r.status === 'RUNNING'}>{r.status}</StatusBadge></td>
                  <td>{r.row_count != null ? formatNumber(r.row_count) : '—'}</td>
                  <td>{r.file_size_bytes != null ? formatFileSize(r.file_size_bytes) : '—'}</td>
                  <td>{formatDateTime(r.created_at)}</td>
                  <td>
                    {r.status === 'SUCCEEDED' ? (
                      <button className="anm-col-toolbar-btn" onClick={() => download(r)}>Download</button>
                    ) : r.status === 'FAILED' ? (
                      <span style={{ fontSize: 11.5, color: 'var(--color-danger-dark)' }} title={r.error_message}>{r.error_message || 'Failed'}</span>
                    ) : (
                      <span style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>—</span>
                    )}
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
