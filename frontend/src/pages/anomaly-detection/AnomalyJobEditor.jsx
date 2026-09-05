/**
 * AnomalyJobEditor — page orchestrator for one AnomalyJob (create/edit).
 * Picks a real source object + columns, saves a real AnomalyJob, and runs
 * it for real via backend/app/anomaly/engine.py. Mirrors
 * Reconciliation.jsx's structure (header/back-link/job-switcher/dirty-
 * state save/run). The run summary panel shown after "Run Now" is a
 * lightweight, real read of the just-created AnomalyExecution — "View
 * Full Results" links into the full drill-down, AnomalyExecutionDetail.jsx
 * (AD8: all columns incl. PASS, baseline/current stats, records,
 * feedback). A brand-new job's baseline/retention/incident/threshold
 * defaults come from the org's real configured defaults (AD9,
 * GET /anomaly/configuration) rather than being hardcoded here.
 */
import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, Play, Save } from 'lucide-react';
import { Button, StatusBadge } from '@/components/common/index.js';
import { anomalyApi } from '@/services/api/anomalyApi.js';
import AnomalyJobSetup from './AnomalyJobSetup.jsx';
import AnomalyScheduleCard from './AnomalyScheduleCard.jsx';
import './anomaly-detection.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }
function emptyObjectRef() { return { connection_id: null, schema_name: '', object_name: '', uploaded_file_id: null }; }

export default function AnomalyJobEditor() {
  const { jobId } = useParams();
  const navigate = useNavigate();

  const [id, setId] = useState(jobId ? Number(jobId) : null);
  const [name, setName] = useState('Untitled Anomaly Job');
  const [objectRef, setObjectRef] = useState(emptyObjectRef());
  const [selectedColumns, setSelectedColumns] = useState([]);
  const [idColumn, setIdColumn] = useState('');
  const [methods, setMethods] = useState(['Z_SCORE', 'NULL_RATE']);
  const [baselineSource, setBaselineSource] = useState('PREVIOUS_RUN');
  const [baselineNRuns, setBaselineNRuns] = useState(5);
  const [baselineNDays, setBaselineNDays] = useState(30);
  const [requireApproval, setRequireApproval] = useState(false);
  const [createIncidentOnCritical, setCreateIncidentOnCritical] = useState(false);
  const [retentionDays, setRetentionDays] = useState(null);
  // Org-default score bands (AD9, /anomaly/configuration) — a brand-new
  // job's threshold_config starts with these; an existing job keeps its
  // own saved bands (never overwritten by a later configuration change).
  const [scoreBands, setScoreBands] = useState(null);
  const [savedSnapshot, setSavedSnapshot] = useState(null);

  const [jobs, setJobs] = useState([]);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [execution, setExecution] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(!!jobId);

  const locked = !!id;
  const ready = !!(objectRef.object_name && objectRef.connection_id != null && selectedColumns.length && methods.length);

  const buildDetectionConfig = () => ({
    methods,
    params: {},
    id_column: idColumn || null,
  });
  const buildBaselineConfig = () => ({
    source: baselineSource,
    n_runs: baselineNRuns,
    n_days: baselineNDays,
    require_approval: requireApproval,
  });
  const buildOnCritical = () => ({ create_incident: createIncidentOnCritical });

  const draft = { name, objectRef, selectedColumns, idColumn, methods, baselineSource, baselineNRuns, baselineNDays, requireApproval, createIncidentOnCritical, retentionDays };
  const dirty = savedSnapshot !== null ? JSON.stringify(draft) !== savedSnapshot : (objectRef.object_name || selectedColumns.length);

  useEffect(() => { anomalyApi.listJobs().then(setJobs).catch(() => {}); }, [id]);

  useEffect(() => {
    if (!jobId) {
      setId(null); setName('Untitled Anomaly Job');
      setObjectRef(emptyObjectRef()); setSelectedColumns([]); setIdColumn('');
      setMethods(['Z_SCORE', 'NULL_RATE']);
      setBaselineSource('PREVIOUS_RUN'); setBaselineNRuns(5); setBaselineNDays(30); setRequireApproval(false);
      setCreateIncidentOnCritical(false); setRetentionDays(null); setScoreBands(null);
      setSavedSnapshot(null); setExecution(null); setError('');
      setLoading(false);
      // Real wiring (AD9): a brand-new job starts from the org's actual
      // configured defaults, not hardcoded ones — see
      // AnomalyConfiguration.jsx / GET /anomaly/configuration. Applied
      // only on the create path; an existing job's own saved values
      // (loaded below) are never overwritten by a later default change.
      anomalyApi.getConfiguration().then((cfg) => {
        setBaselineSource(cfg.default_baseline_source);
        setRequireApproval(cfg.default_require_baseline_approval);
        setCreateIncidentOnCritical(cfg.default_create_incident_on_critical);
        setRetentionDays(cfg.default_retention_days);
        setScoreBands(cfg.default_score_bands);
      }).catch(() => {}); // no org context (e.g. super-admin) — hardcoded defaults above stand
      return;
    }
    setLoading(true);
    setExecution(null); setError('');
    anomalyApi.getJob(jobId).then((d) => {
      setId(d.id); setName(d.name);
      const ref = { connection_id: d.connection_id, schema_name: d.schema_name || '', object_name: d.object_name, uploaded_file_id: d.uploaded_file_id || null };
      setObjectRef(ref);
      setSelectedColumns(d.columns || []);
      const dc = d.detection_config || {};
      setIdColumn(dc.id_column || '');
      setMethods(dc.methods || []);
      const bc = d.baseline_config || {};
      setBaselineSource(bc.source || 'PREVIOUS_RUN');
      setBaselineNRuns(bc.n_runs ?? 5);
      setBaselineNDays(bc.n_days ?? 30);
      setRequireApproval(!!bc.require_approval);
      setCreateIncidentOnCritical(!!(d.on_critical || {}).create_incident);
      setRetentionDays(d.retention_days ?? null);
      setSavedSnapshot(JSON.stringify({
        name: d.name, objectRef: ref, selectedColumns: d.columns || [], idColumn: dc.id_column || '',
        methods: dc.methods || [], baselineSource: bc.source || 'PREVIOUS_RUN', baselineNRuns: bc.n_runs ?? 5,
        baselineNDays: bc.n_days ?? 30, requireApproval: !!bc.require_approval,
        createIncidentOnCritical: !!(d.on_critical || {}).create_incident, retentionDays: d.retention_days ?? null,
      }));
    }).catch((err) => setError(apiErrorMessage(err, 'Could not load this job.'))).finally(() => setLoading(false));
  }, [jobId]);

  useEffect(() => {
    if (!id) return;
    anomalyApi.listExecutions(id).then((execs) => setExecution(execs[0] ?? null)).catch(() => {});
  }, [id]);

  const runSave = async () => {
    setSaving(true); setError('');
    try {
      if (id) {
        const r = await anomalyApi.updateJob(id, {
          name, columns: selectedColumns, detection_config: buildDetectionConfig(),
          baseline_config: buildBaselineConfig(), on_critical: buildOnCritical(), retention_days: retentionDays,
        });
        setSavedSnapshot(JSON.stringify({ ...draft, name: r.name }));
      } else {
        const r = await anomalyApi.createJob({
          name,
          connection_id: objectRef.connection_id, schema_name: objectRef.schema_name || null, object_name: objectRef.object_name,
          uploaded_file_id: objectRef.uploaded_file_id || null,
          columns: selectedColumns, detection_config: buildDetectionConfig(), baseline_config: buildBaselineConfig(),
          threshold_config: scoreBands ? { score_bands: scoreBands } : {}, on_critical: buildOnCritical(),
          enabled: true, retention_days: retentionDays,
        });
        setId(r.id);
        setSavedSnapshot(JSON.stringify({ ...draft, name: r.name }));
        navigate(`/app/anomaly/jobs/${r.id}`, { replace: true });
      }
    } catch (err) { setError(apiErrorMessage(err, 'Could not save job.')); }
    finally { setSaving(false); }
  };

  const handleRun = async () => {
    if (!id) { setError('Save the job before running it.'); return; }
    if (dirty) { setError('You have unsaved changes — save first so the run reads the current configuration.'); return; }
    setError(''); setRunning(true);
    try {
      const exec = await anomalyApi.runJob(id);
      setExecution(exec);
    } catch (err) { setError(apiErrorMessage(err, 'Anomaly detection run failed.')); }
    finally { setRunning(false); }
  };

  if (loading) return <div><p style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</p></div>;

  return (
    <div>
      <div className="anm-header">
        <div>
          <Link to="/app/anomaly/jobs" className="anm-back-link"><ArrowLeft size={13} /> All jobs</Link>
          <input className="anm-name-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Job name…" />
          <p className="anm-subtitle">
            Scan a real object's columns for statistical, format, categorical, duplicate, distribution or time-series anomalies
            {jobs.length > 0 && (
              <select className="anm-job-select" value={id ?? ''} onChange={(e) => { if (e.target.value) navigate(`/app/anomaly/jobs/${e.target.value}`); else navigate('/app/anomaly/jobs/new'); }}>
                <option value="">+ New job…</option>
                {jobs.map((j) => <option key={j.id} value={j.id}>{j.name}</option>)}
              </select>
            )}
          </p>
        </div>
        <div className="anm-actions">
          {execution && execution.status !== 'FAILED' && (
            <StatusBadge variant={execution.overall_status === 'CRITICAL' || execution.overall_status === 'ERROR' ? 'danger' : execution.overall_status === 'WARNING' ? 'warning' : 'success'} dot>
              {(execution.anomalies_found ?? 0).toLocaleString()} anomalies
            </StatusBadge>
          )}
          <Button variant="secondary" icon={<Save size={14} />} loading={saving} disabled={!ready || !dirty} onClick={runSave}>
            {id ? 'Save Changes' : 'Save Job'}
          </Button>
          <Button variant="primary" icon={<Play size={14} fill="currentColor" />} loading={running} disabled={!id || dirty} onClick={handleRun}>
            {running ? 'Running…' : 'Run Now'}
          </Button>
        </div>
      </div>

      {error && (
        <div style={{ padding: '10px 14px', marginBottom: 16, background: 'var(--color-danger-bg)', border: '1px solid var(--color-danger-100, #FEE2E2)', borderRadius: 8, fontSize: 13, color: 'var(--color-danger-dark)' }}>
          {error}
        </div>
      )}

      {execution && (
        <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 'var(--radius-xl)', padding: 16, marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
            <div className="anm-setup-section-title" style={{ marginBottom: 0 }}>Last Run</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>
                {execution.status === 'FAILED' ? (execution.error_message || 'Failed') : `${execution.status} · ${execution.total_ms != null ? `${execution.total_ms}ms` : ''}`}
              </span>
              {execution.status !== 'FAILED' && (
                <Link to={`/app/anomaly/executions/${execution.id}`} style={{ fontSize: 11.5, fontWeight: 700, color: 'var(--color-primary)', textDecoration: 'none' }}>
                  View Full Results →
                </Link>
              )}
            </div>
          </div>
          {execution.status !== 'FAILED' ? (
            <div className="anm-run-summary">
              <div className="anm-run-summary-card">
                <div className="anm-run-summary-val">{(execution.records_scanned ?? 0).toLocaleString()}</div>
                <div className="anm-run-summary-label">Records Scanned</div>
              </div>
              <div className="anm-run-summary-card">
                <div className="anm-run-summary-val">{execution.columns_scanned ?? 0}</div>
                <div className="anm-run-summary-label">Columns Scanned</div>
              </div>
              <div className="anm-run-summary-card">
                <div className="anm-run-summary-val">{(execution.anomalies_found ?? 0).toLocaleString()}</div>
                <div className="anm-run-summary-label">Anomalies Found</div>
              </div>
              <div className="anm-run-summary-card">
                <div className="anm-run-summary-val critical">{execution.critical_count ?? 0}</div>
                <div className="anm-run-summary-label">Critical</div>
              </div>
              <div className="anm-run-summary-card">
                <div className="anm-run-summary-val warning">{execution.warning_count ?? 0}</div>
                <div className="anm-run-summary-label">Warning</div>
              </div>
            </div>
          ) : (
            <div style={{ fontSize: 13, color: 'var(--color-danger-dark)' }}>{execution.error_message}</div>
          )}
        </div>
      )}

      {id && <AnomalyScheduleCard jobId={id} jobName={name} />}

      <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 'var(--radius-xl)', overflow: 'hidden' }}>
        <AnomalyJobSetup
          objectRef={objectRef} setObjectRef={setObjectRef}
          selectedColumns={selectedColumns} setSelectedColumns={setSelectedColumns}
          idColumn={idColumn} setIdColumn={setIdColumn}
          methods={methods} setMethods={setMethods}
          baselineSource={baselineSource} setBaselineSource={setBaselineSource}
          baselineNRuns={baselineNRuns} setBaselineNRuns={setBaselineNRuns}
          baselineNDays={baselineNDays} setBaselineNDays={setBaselineNDays}
          requireApproval={requireApproval} setRequireApproval={setRequireApproval}
          createIncidentOnCritical={createIncidentOnCritical} setCreateIncidentOnCritical={setCreateIncidentOnCritical}
          retentionDays={retentionDays} setRetentionDays={setRetentionDays}
          locked={locked}
        />
      </div>
    </div>
  );
}
