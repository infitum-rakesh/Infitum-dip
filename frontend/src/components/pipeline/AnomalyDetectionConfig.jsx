/**
 * AnomalyDetectionConfig — the config-panel body for the
 * 'anomaly_detection_step' node type (AD10).
 *
 * Job reference (jobId) vs inline config is a real fork in what actually
 * runs (see pipeline_job_executor.py's _run_anomaly_detection docstring):
 *   - Job selected  → that AnomalyJob's own saved columns/detection
 *     methods/baseline/threshold are used (never this node's inline
 *     fields below, which are hidden once a job is picked) — the run
 *     persists as a real AnomalyExecution against that job, with real
 *     baseline history, visible in Anomaly Results.
 *   - No job        → runs from the Columns/Methods picked below, cold-
 *     start baseline (compares each run against itself — no history to
 *     read), default scoring bands, and nothing is persisted to the
 *     anomaly module (there's no AnomalyJob to attach rows to) — only
 *     logged into this pipeline job's own log.
 *
 * There's deliberately no "Execution Mode (pre/post load)" selector here
 * — this node checks whatever DataFrame is actually flowing through it
 * at its position in the graph, so placing it before vs after the Target
 * node IS the pre/post-load choice; a redundant dropdown that did
 * nothing would violate this app's "config UI must match runtime
 * reality" rule.
 */
import { useEffect, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { anomalyApi, DETECTION_METHODS } from '@/services/api/anomalyApi.js';
import './pipeline.css';

export function AnomalyDetectionConfig({ cfg, set }) {
  const [jobs, setJobs] = useState([]);
  const [loadingJobs, setLoadingJobs] = useState(true);

  useEffect(() => {
    anomalyApi.listJobs().then(setJobs).catch(() => setJobs([])).finally(() => setLoadingJobs(false));
  }, []);

  const onCritical = cfg.onCritical || {};
  const setOnCritical = (key, value) => set('onCritical', { ...onCritical, [key]: value });

  const columnNames = Array.isArray(cfg.columnNames) ? cfg.columnNames : [];
  const methods = Array.isArray(cfg.methods) ? cfg.methods : [];
  const toggleMethod = (id) => set('methods', methods.includes(id) ? methods.filter((m) => m !== id) : [...methods, id]);

  const jobSelected = !!cfg.jobId;
  const selectedJob = jobs.find((j) => j.id === Number(cfg.jobId));

  return (
    <>
      <label className="pc-config-checkbox-row">
        <input type="checkbox" checked={cfg.enabled !== false} onChange={(e) => set('enabled', e.target.checked)} /> Enabled
      </label>

      <div className="pc-config-row">
        <label className="pc-config-label">Anomaly Job <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>(optional — real, saved job)</span></label>
        <select
          className="pc-config-select" value={cfg.jobId ?? ''}
          onChange={(e) => set('jobId', e.target.value ? Number(e.target.value) : null)}
        >
          <option value="">{loadingJobs ? 'Loading…' : 'None — use inline config below'}</option>
          {jobs.map((j) => <option key={j.id} value={j.id}>{j.name}</option>)}
        </select>
      </div>

      {jobSelected ? (
        <div className="pc-config-notice">
          <AlertTriangle size={13} />
          <span>
            Uses <strong>{selectedJob?.name ?? `job #${cfg.jobId}`}</strong>'s own saved columns, detection methods, baseline and threshold config — the fields below are ignored while a job is selected. This run is recorded as a real execution against that job, visible in Anomaly Results.
          </span>
        </div>
      ) : (
        <>
          <div className="pc-config-notice">
            <AlertTriangle size={13} />
            <span>No job selected — runs from the config below with a cold-start baseline (compares each run against itself, no history) and default scoring bands. Nothing is persisted to the Anomaly Detection module; results are logged into this pipeline job's own log only.</span>
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Columns to check (comma-separated)</label>
            <input
              className="pc-config-input" value={columnNames.join(', ')}
              onChange={(e) => set('columnNames', e.target.value.split(',').map((s) => s.trim()).filter(Boolean))}
              placeholder="amount, status, email"
            />
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Identifier column <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>(optional)</span></label>
            <input className="pc-config-input" value={cfg.idColumn ?? ''} onChange={(e) => set('idColumn', e.target.value)} placeholder="e.g. order_id" />
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Detection methods</label>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 180, overflowY: 'auto', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)', padding: 8 }}>
              {DETECTION_METHODS.map((m) => (
                <label key={m.id} className="pc-config-checkbox-row" style={{ marginBottom: 0 }}>
                  <input type="checkbox" checked={methods.includes(m.id)} onChange={() => toggleMethod(m.id)} />
                  {m.label} <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>({m.category})</span>
                </label>
              ))}
            </div>
          </div>
        </>
      )}

      <div className="pc-config-row">
        <label className="pc-config-label">On CRITICAL result</label>
        <label className="pc-config-checkbox-row">
          <input type="checkbox" checked={!!onCritical.create_incident} onChange={(e) => setOnCritical('create_incident', e.target.checked)} /> Create an incident (real)
        </label>
        <label className="pc-config-checkbox-row">
          <input type="checkbox" checked={!!onCritical.stop_pipeline} onChange={(e) => setOnCritical('stop_pipeline', e.target.checked)} /> Stop the pipeline (marks the job failed)
        </label>
      </div>
    </>
  );
}
