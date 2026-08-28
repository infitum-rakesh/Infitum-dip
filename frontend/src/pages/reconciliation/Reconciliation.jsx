/**
 * Reconciliation — page orchestrator (Phase 7).
 * Picks real source/target connections+objects, saves a real
 * ReconciliationJob, and runs it for real via
 * backend/app/reconciliation/engine.py. Tabs: Setup | Results.
 */
import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ArrowLeft, Play, Save } from 'lucide-react';
import { Button, StatusBadge } from '@/components/common/index.js';
import { reconciliationApi } from '@/services/api/reconciliationApi.js';
import { ReconciliationRules } from './ReconciliationRules.jsx';
import { ReconciliationResults } from './ReconciliationResults.jsx';
import './reconciliation.css';

function apiErrorMessage(err, fallback) { return err?.message || fallback; }
function emptyRef() { return { connection_id: null, schema_name: '', object_name: '', uploaded_file_id: null }; }

export default function Reconciliation() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const [id, setId] = useState(jobId ? Number(jobId) : null);
  const [name, setName] = useState('Untitled Reconciliation Job');
  const [source, setSource] = useState(emptyRef());
  const [target, setTarget] = useState(emptyRef());
  const [keyColumns, setKeyColumns] = useState([]);
  const [compareColumns, setCompareColumns] = useState([]);
  const [comparisonTypes, setComparisonTypes] = useState(['COUNT']);
  // {source_column: target_column} — only for columns that differ by name
  // between the two sides; anything not in here falls back to matching by
  // name on the target, same as before this existed.
  const [columnMapping, setColumnMapping] = useState({});
  const [savedSnapshot, setSavedSnapshot] = useState(null);

  const [jobs, setJobs] = useState([]);
  // "View Result" from the list page links here with ?tab=results so the
  // editor opens straight on the Results tab instead of Setup.
  const [activeTab, setActiveTab] = useState(searchParams.get('tab') === 'results' ? 'results' : 'setup');
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [run, setRun] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(!!jobId);

  const locked = !!id;
  // connection_id can legitimately be 0 (the file-backed sentinel — see
  // ReconciliationRules.jsx), so this can't just truthy-check connection_id.
  const bothReady = !!(source.object_name && source.connection_id != null && target.object_name && target.connection_id != null);
  const draft = { name, source, target, keyColumns, compareColumns, comparisonTypes, columnMapping };
  const dirty = savedSnapshot !== null ? JSON.stringify(draft) !== savedSnapshot : (source.object_name || target.object_name);

  useEffect(() => { reconciliationApi.listJobs().then((r) => setJobs(r.items)).catch(() => {}); }, [id]);

  useEffect(() => {
    if (!jobId) {
      // "+ New job…" from the dropdown navigates here from an existing
      // job's editor — same routed component re-rendering, not a fresh
      // mount, so the previous job's state has to be reset by hand or it
      // would still be sitting in every field below.
      setId(null); setName('Untitled Reconciliation Job');
      setSource(emptyRef()); setTarget(emptyRef());
      setKeyColumns([]); setCompareColumns([]); setComparisonTypes(['COUNT']); setColumnMapping({});
      setSavedSnapshot(null); setRun(null); setError(''); setActiveTab('setup');
      setLoading(false);
      return;
    }
    setLoading(true);
    // Switching jobs via the dropdown re-renders this same routed component
    // with a new :jobId — it does NOT remount, so any state from the
    // previously open job (its last run, its error banner) would otherwise
    // still be sitting there while the new job's data loads. Clear both
    // right away so the Results tab doesn't flash the old job's numbers.
    setRun(null); setError('');
    reconciliationApi.getJob(jobId).then((d) => {
      setId(d.id); setName(d.name);
      const src = { connection_id: d.source_connection_id, schema_name: d.source_schema || '', object_name: d.source_object, uploaded_file_id: d.source_uploaded_file_id || null };
      const tgt = { connection_id: d.target_connection_id, schema_name: d.target_schema || '', object_name: d.target_object, uploaded_file_id: d.target_uploaded_file_id || null };
      setSource(src);
      setTarget(tgt);
      setKeyColumns(d.key_columns); setCompareColumns(d.compare_columns); setComparisonTypes(d.comparison_types);
      setColumnMapping(d.column_mapping || {});
      setSavedSnapshot(JSON.stringify({
        name: d.name, source: src, target: tgt,
        keyColumns: d.key_columns, compareColumns: d.compare_columns, comparisonTypes: d.comparison_types,
        columnMapping: d.column_mapping || {},
      }));
    }).catch((err) => setError(apiErrorMessage(err, 'Could not load this job.'))).finally(() => setLoading(false));
  }, [jobId]);

  // Load this job's real, persisted latest run (not just whatever the user
  // happens to trigger from this page in the current session) so the
  // Results tab has something to show the moment it's opened. GET
  // /reconciliation/jobs/{id}/runs returns a raw JSON array sorted
  // newest-first, so runs[0] is it. This effect only re-fires when `id`
  // itself changes (initial load, or the job dropdown switching to a
  // different job) — never while staying on the same job — so it's always
  // correct to overwrite `run` outright rather than guard against clobbering
  // a same-session handleRun() result.
  useEffect(() => {
    if (!id) return;
    reconciliationApi.listRuns(id).then((runs) => setRun(runs[0] ?? null)).catch(() => {});
  }, [id]);

  const runSave = async () => {
    setSaving(true); setError('');
    try {
      if (id) {
        const r = await reconciliationApi.updateJob(id, { name, key_columns: keyColumns, compare_columns: compareColumns, comparison_types: comparisonTypes, column_mapping: columnMapping });
        setSavedSnapshot(JSON.stringify({ name: r.name, source, target, keyColumns: r.key_columns, compareColumns: r.compare_columns, comparisonTypes: r.comparison_types, columnMapping: r.column_mapping || {} }));
      } else {
        const r = await reconciliationApi.createJob({
          name,
          source_connection_id: source.connection_id, source_schema: source.schema_name || null, source_object: source.object_name,
          source_uploaded_file_id: source.uploaded_file_id || null,
          target_connection_id: target.connection_id, target_schema: target.schema_name || null, target_object: target.object_name,
          target_uploaded_file_id: target.uploaded_file_id || null,
          key_columns: keyColumns, compare_columns: compareColumns, comparison_types: comparisonTypes, column_mapping: columnMapping,
        });
        setId(r.id);
        setSavedSnapshot(JSON.stringify({ name: r.name, source, target, keyColumns: r.key_columns, compareColumns: r.compare_columns, comparisonTypes: r.comparison_types, columnMapping: r.column_mapping || {} }));
        navigate(`/app/reconciliation/${r.id}`, { replace: true });
      }
    } catch (err) { setError(apiErrorMessage(err, 'Could not save job.')); }
    finally { setSaving(false); }
  };

  const handleRun = async () => {
    if (!id) { setError('Save the job before running it.'); return; }
    if (dirty) { setError('You have unsaved changes — save first so the run reads the current configuration.'); return; }
    if (!comparisonTypes.length) { setError('Select at least one comparison type.'); return; }
    setError(''); setRunning(true);
    try {
      const r = await reconciliationApi.runJob(id);
      setRun(r);
      setActiveTab('results');
    } catch (err) { setError(apiErrorMessage(err, 'Reconciliation run failed.')); }
    finally { setRunning(false); }
  };

  if (loading) return <div><p style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</p></div>;

  return (
    <div>
      <div className="rcn-header">
        <div>
          <Link to="/app/reconciliation" className="rcn-back-link"><ArrowLeft size={13} /> All jobs</Link>
          <input className="rcn-name-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Job name…" />
          <p className="rcn-subtitle">
            Compare source and target datasets for real discrepancies
            {jobs.length > 0 && (
              <select className="rcn-job-select" value={id ?? ''} onChange={(e) => { if (e.target.value) navigate(`/app/reconciliation/${e.target.value}`); else navigate('/app/reconciliation/new'); }}>
                <option value="">+ New job…</option>
                {jobs.map((j) => <option key={j.id} value={j.id}>{j.name}</option>)}
              </select>
            )}
          </p>
        </div>
        <div className="rcn-actions">
          {run && run.status === 'SUCCEEDED' && (
            <StatusBadge variant={(run.missing_count || 0) + (run.extra_count || 0) + (run.mismatched_count || 0) === 0 && run.checksum_match !== false ? 'success' : 'warning'} dot>
              {(run.matched_count ?? run.target_count ?? 0).toLocaleString()} / {(run.source_count ?? 0).toLocaleString()} matched
            </StatusBadge>
          )}
          <Button variant="secondary" icon={<Save size={14} />} loading={saving} disabled={!bothReady || !dirty} onClick={runSave}>
            {id ? 'Save Changes' : 'Save Job'}
          </Button>
          <Button variant="primary" icon={<Play size={14} fill="currentColor" />} loading={running} disabled={!id || dirty} onClick={handleRun}>
            {running ? 'Running…' : 'Run Reconciliation'}
          </Button>
        </div>
      </div>

      {error && (
        <div style={{ padding: '10px 14px', marginBottom: 16, background: 'var(--color-danger-bg)', border: '1px solid var(--color-danger-100, #FEE2E2)', borderRadius: 8, fontSize: 13, color: 'var(--color-danger-dark)' }}>
          {error}
        </div>
      )}

      <div className="rcn-tab-nav">
        <button className={`rcn-tab-btn ${activeTab === 'setup' ? 'active' : ''}`} onClick={() => setActiveTab('setup')}>
          ⚙️ Setup &amp; Configuration
        </button>
        <button className={`rcn-tab-btn ${activeTab === 'results' ? 'active' : ''}`} onClick={() => setActiveTab('results')}>
          📊 Results {run && run.source_count != null && `— ${((run.matched_count ?? run.target_count ?? 0) / (run.source_count || 1) * 100).toFixed(1)}% match`}
        </button>
      </div>

      <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderTop: 'none', borderRadius: '0 0 var(--radius-xl) var(--radius-xl)', overflow: 'hidden' }}>
        {activeTab === 'setup' && (
          <ReconciliationRules
            source={source} setSource={setSource} target={target} setTarget={setTarget}
            keyColumns={keyColumns} setKeyColumns={setKeyColumns}
            compareColumns={compareColumns} setCompareColumns={setCompareColumns}
            comparisonTypes={comparisonTypes} setComparisonTypes={setComparisonTypes}
            columnMapping={columnMapping} setColumnMapping={setColumnMapping}
            locked={locked}
          />
        )}
        {activeTab === 'results' && (
          <ReconciliationResults run={run} sourceObject={source.object_name} targetObject={target.object_name} onReRun={handleRun} onGoToSetup={() => setActiveTab('setup')} />
        )}
      </div>
    </div>
  );
}
