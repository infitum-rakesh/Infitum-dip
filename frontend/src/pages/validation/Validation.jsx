/**
 * Validation — page orchestrator (Phase 7).
 * Picks a real connection + object, edits a real rule set (saved as a
 * ValidationRuleSet row), and runs it for real via
 * backend/app/validation/engine.py. Tabs: Rules (configure) | Results
 * (after run) — same shape as before, now backed by real data.
 */
import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ArrowLeft, Loader2, Play, Save, Search, Trash2, Upload } from 'lucide-react';
import { Button, StatusBadge } from '@/components/common/index.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { uploadedFileApi } from '@/services/api/uploadedFileApi.js';
import { validationApi } from '@/services/api/validationApi.js';
import { ValidationRules } from './ValidationRules.jsx';
import { ValidationResults } from './ValidationResults.jsx';
import './validation.css';

function apiErrorMessage(err, fallback) { return err?.message || fallback; }

// 0 is the real "not a real Connection — see uploaded_file_id instead"
// sentinel this feature is built on (backend/app/models/validation.py).
const FILE_BACKED_SENTINEL = 0;

function ObjectPicker({ value, onChange, disabled }) {
  const [connections, setConnections] = useState([]);
  const [schemas, setSchemas] = useState([]);
  const [objects, setObjects] = useState([]);
  const [search, setSearch] = useState('');
  const [loadingSchemas, setLoadingSchemas] = useState(false);
  const [loadingObjects, setLoadingObjects] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');

  const mode = value.uploaded_file_id ? 'file' : (value.mode || 'connection');

  useEffect(() => { connectionApi.list({ category: 'DATABASE', page_size: 200 }).then((r) => setConnections(r.items)).catch(() => {}); }, []);

  useEffect(() => {
    if (mode !== 'connection' || !value.connection_id) { setSchemas([]); return; }
    setLoadingSchemas(true);
    connectionApi.getSchemas(value.connection_id).then((r) => {
      setSchemas(r.schemas);
      // Single-database dialects (MySQL) report exactly one "schema" —
      // the database the connection already points at. Auto-select it
      // rather than making the user pick from a one-item list before
      // objects can load.
      if (r.schemas.length === 1 && !value.schema_name) onChange({ ...value, schema_name: r.schemas[0] });
    }).catch(() => setSchemas([])).finally(() => setLoadingSchemas(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, value.connection_id]);

  useEffect(() => {
    if (mode !== 'connection' || !value.connection_id || !value.schema_name) { setObjects([]); return; }
    setLoadingObjects(true);
    const t = setTimeout(() => {
      connectionApi.getObjects(value.connection_id, { schema: value.schema_name, q: search || undefined })
        .then((r) => setObjects(r.items)).catch(() => setObjects([])).finally(() => setLoadingObjects(false));
    }, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, value.connection_id, value.schema_name, search]);

  const switchMode = (nextMode) => {
    if (disabled || nextMode === mode) return;
    onChange({ connection_id: null, schema_name: '', object_name: '', uploaded_file_id: null, mode: nextMode });
  };

  const handleUpload = async (file) => {
    if (!file) return;
    setUploading(true); setUploadError('');
    try {
      const record = await uploadedFileApi.upload(file);
      onChange({ connection_id: FILE_BACKED_SENTINEL, schema_name: '', object_name: record.filename, uploaded_file_id: record.id, mode: 'file' });
    } catch (err) {
      setUploadError(err?.message || 'Upload failed.');
    } finally {
      setUploading(false);
    }
  };

  const clearUpload = () => onChange({ connection_id: null, schema_name: '', object_name: '', uploaded_file_id: null, mode: 'file' });

  return (
    <div>
      <div className="vld-mode-toggle" role="tablist">
        <button type="button" role="tab" aria-selected={mode === 'connection'}
          className={`vld-mode-btn ${mode === 'connection' ? 'active' : ''}`}
          disabled={disabled} onClick={() => switchMode('connection')}>
          Connection
        </button>
        <button type="button" role="tab" aria-selected={mode === 'file'}
          className={`vld-mode-btn ${mode === 'file' ? 'active' : ''}`}
          disabled={disabled} onClick={() => switchMode('file')}>
          Upload File
        </button>
      </div>

      {mode === 'connection' ? (
        <div className="vld-picker-row">
          <select className="vld-picker-select" disabled={disabled} value={value.connection_id ?? ''}
            onChange={(e) => onChange({ connection_id: e.target.value ? Number(e.target.value) : null, schema_name: '', object_name: '', uploaded_file_id: null, mode: 'connection' })}>
            <option value="">Connection…</option>
            {connections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <select className="vld-picker-select" disabled={disabled || !value.connection_id} value={value.schema_name}
            onChange={(e) => onChange({ ...value, schema_name: e.target.value, object_name: '' })}>
            <option value="">{loadingSchemas ? 'Loading…' : 'Schema…'}</option>
            {schemas.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select className="vld-picker-select" disabled={disabled || !value.schema_name} value={value.object_name}
            onChange={(e) => onChange({ ...value, object_name: e.target.value })}>
            <option value="">{loadingObjects ? 'Loading…' : 'Object…'}</option>
            {objects.map((o) => <option key={o.name} value={o.name}>{o.name}</option>)}
          </select>
          {value.connection_id && value.schema_name && (
            <div className="vld-picker-search">
              <Search size={12} />
              <input placeholder="Search objects…" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
          )}
        </div>
      ) : (
        <div className="vld-picker-row">
          {value.uploaded_file_id ? (
            <div className="vld-uploaded-file-card">
              <span className="vld-uploaded-file-name">{value.object_name}</span>
              {!disabled && (
                <button type="button" className="vld-icon-btn" title="Remove file" onClick={clearUpload}>
                  <Trash2 size={12} />
                </button>
              )}
            </div>
          ) : (
            <>
              <label className="vld-upload-btn" style={{ cursor: disabled || uploading ? 'default' : 'pointer' }}>
                {uploading ? <Loader2 size={12} className="vld-spin" /> : <Upload size={12} />}
                {uploading ? 'Uploading…' : 'Upload File'}
                <input
                  type="file" style={{ display: 'none' }} disabled={disabled || uploading}
                  accept=".csv,.tsv,.txt,.dat,.xlsx,.jsonl,.ndjson"
                  onChange={(e) => { handleUpload(e.target.files?.[0]); e.target.value = ''; }}
                />
              </label>
              <span style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                CSV, TSV, TXT, Excel (.xlsx) or JSON Lines. Only Not Null / Range / Format / Uniqueness rules apply to files.
              </span>
            </>
          )}
          {uploadError && <span className="vld-upload-error">{uploadError}</span>}
        </div>
      )}
    </div>
  );
}

export default function Validation() {
  const { ruleSetId } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const [id, setId] = useState(ruleSetId ? Number(ruleSetId) : null);
  const [name, setName] = useState('Untitled Rule Set');
  const [target, setTarget] = useState({ connection_id: null, schema_name: '', object_name: '', uploaded_file_id: null });
  const [columns, setColumns] = useState([]);
  const [rules, setRules] = useState([]);
  const [savedSnapshot, setSavedSnapshot] = useState(null);

  const [ruleSets, setRuleSets] = useState([]);

  // "View Result" from the list page links here with ?tab=results so the
  // editor opens straight on the Results tab instead of Rules.
  const [activeTab, setActiveTab] = useState(searchParams.get('tab') === 'results' ? 'results' : 'rules');
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [run, setRun] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(!!ruleSetId);

  const locked = !!id;
  // connection_id can legitimately be 0 (the file-backed sentinel — see
  // ObjectPicker above), so this can't just truthy-check connection_id.
  const ready = !!(target.object_name && target.connection_id != null);
  const dirty = savedSnapshot !== null ? JSON.stringify({ name, rules }) !== savedSnapshot : rules.length > 0;

  useEffect(() => { validationApi.listRuleSets().then((r) => setRuleSets(r.items)).catch(() => {}); }, [id]);

  useEffect(() => {
    if (!ruleSetId) {
      // "+ New rule set…" from the dropdown navigates here from an
      // existing rule set's editor — same routed component re-rendering,
      // not a fresh mount, so the previous rule set's state has to be
      // reset by hand or it would still be sitting in every field below.
      setId(null); setName('Untitled Rule Set');
      setTarget({ connection_id: null, schema_name: '', object_name: '', uploaded_file_id: null });
      setRules([]); setSavedSnapshot(null); setRun(null); setError(''); setActiveTab('rules');
      setLoading(false);
      return;
    }
    setLoading(true);
    // Switching rule sets via the dropdown re-renders this same routed
    // component with a new :ruleSetId — it does NOT remount, so the
    // previously open rule set's last run and error banner would otherwise
    // still be sitting there while the new one's data loads.
    setRun(null); setError('');
    validationApi.getRuleSet(ruleSetId).then((d) => {
      setId(d.id); setName(d.name);
      setTarget({ connection_id: d.connection_id, schema_name: d.schema_name || '', object_name: d.object_name, uploaded_file_id: d.uploaded_file_id || null });
      setRules(d.rules);
      setSavedSnapshot(JSON.stringify({ name: d.name, rules: d.rules }));
    }).catch((err) => setError(apiErrorMessage(err, 'Could not load this rule set.'))).finally(() => setLoading(false));
  }, [ruleSetId]);

  // Load this rule set's real, persisted latest run (not just whatever the
  // user happens to trigger from this page in the current session) so the
  // Results tab has something to show the moment it's opened. GET
  // /validation/rulesets/{id}/runs returns a raw JSON array sorted
  // newest-first, so runs[0] is it. This effect only re-fires when `id`
  // itself changes (initial load, or the rule-set dropdown switching to a
  // different one) — never while staying on the same rule set — so it's
  // always correct to overwrite `run` outright.
  useEffect(() => {
    if (!id) return;
    validationApi.listRuns(id).then((runs) => setRun(runs[0] ?? null)).catch(() => {});
  }, [id]);

  useEffect(() => {
    if (target.uploaded_file_id) {
      uploadedFileApi.getColumns(target.uploaded_file_id)
        .then((r) => setColumns(r.columns.map((name) => ({ name })))).catch(() => setColumns([]));
      return;
    }
    if (!ready) { setColumns([]); return; }
    connectionApi.getColumns(target.connection_id, target.object_name, { schema: target.schema_name || undefined })
      .then((r) => setColumns(r.columns)).catch(() => setColumns([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target.connection_id, target.schema_name, target.object_name, target.uploaded_file_id]);

  const runSave = async () => {
    setSaving(true); setError('');
    try {
      if (id) {
        const r = await validationApi.updateRuleSet(id, { name, rules });
        setSavedSnapshot(JSON.stringify({ name: r.name, rules: r.rules }));
      } else {
        const r = await validationApi.createRuleSet({
          name, connection_id: target.connection_id, schema_name: target.schema_name || null, object_name: target.object_name,
          uploaded_file_id: target.uploaded_file_id || null, rules,
        });
        setId(r.id);
        setSavedSnapshot(JSON.stringify({ name: r.name, rules: r.rules }));
        navigate(`/app/validation/${r.id}`, { replace: true });
      }
    } catch (err) { setError(apiErrorMessage(err, 'Could not save rule set.')); }
    finally { setSaving(false); }
  };

  const handleRun = async () => {
    if (!id) { setError('Save the rule set before running it.'); return; }
    if (dirty) { setError('You have unsaved changes — save first so the run reads the current rules.'); return; }
    const enabled = rules.filter((r) => r.enabled);
    if (!enabled.length) { setError('Enable at least one rule before running.'); return; }
    setError(''); setRunning(true);
    try {
      const r = await validationApi.runRuleSet(id);
      setRun(r);
      setActiveTab('results');
    } catch (err) { setError(apiErrorMessage(err, 'Validation run failed.')); }
    finally { setRunning(false); }
  };

  const enabledCount = rules.filter((r) => r.enabled).length;

  if (loading) return <div className="vld-page"><p style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</p></div>;

  return (
    <div className="vld-page">
      <div className="vld-header">
        <div>
          <Link to="/app/validation" className="vld-back-link"><ArrowLeft size={13} /> All rule sets</Link>
          <input className="vld-name-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Rule set name…" />
          <p className="vld-subtitle">
            Data Validation — real rule checks against a live connection
            {ruleSets.length > 0 && (
              <select className="vld-ruleset-select" value={id ?? ''} onChange={(e) => { if (e.target.value) navigate(`/app/validation/${e.target.value}`); else navigate('/app/validation/new'); }}>
                <option value="">+ New rule set…</option>
                {ruleSets.map((rs) => <option key={rs.id} value={rs.id}>{rs.name}</option>)}
              </select>
            )}
          </p>
        </div>
        <div className="vld-actions">
          {run && (
            <StatusBadge variant={run.results.some((r) => r.status === 'FAILED') ? 'danger' : 'success'} dot>
              {run.results.filter((r) => r.status === 'PASSED').length}/{run.results.length} passed
            </StatusBadge>
          )}
          <Button variant="secondary" icon={<Save size={14} />} loading={saving} disabled={!ready || !dirty} onClick={runSave}>
            {id ? 'Save Changes' : 'Save Rule Set'}
          </Button>
          <Button variant="primary" icon={<Play size={14} fill="currentColor" />} loading={running} disabled={!id || dirty} onClick={handleRun}>
            {running ? 'Running…' : `Run Validation (${enabledCount} rules)`}
          </Button>
        </div>
      </div>

      <div className="vld-object-row">
        <ObjectPicker value={target} onChange={setTarget} disabled={locked} />
      </div>
      {target.uploaded_file_id && (
        <div className="vld-info-banner">
          File-backed rule set — only Not Null, Range, Format and Uniqueness rules run against uploaded files
          (Referential Integrity and SQL Condition need a real database).
        </div>
      )}

      {error && (
        <div style={{ padding: '10px 14px', marginBottom: 16, background: 'var(--color-danger-bg)', border: '1px solid var(--color-danger-100, #FEE2E2)', borderRadius: 8, fontSize: 13, color: 'var(--color-danger-dark)' }}>
          {error}
        </div>
      )}
      {!ready && <div className="vld-info-banner">Select a connection, schema and object above to start defining rules.</div>}

      <div className="vld-tab-nav">
        <button className={`vld-tab-btn ${activeTab === 'rules' ? 'active' : ''}`} onClick={() => setActiveTab('rules')}>
          🛡️ Rules ({enabledCount} enabled)
        </button>
        <button className={`vld-tab-btn ${activeTab === 'results' ? 'active' : ''}`} onClick={() => setActiveTab('results')}>
          📊 Results {run && `— ${new Date(run.completed_at || run.started_at).toLocaleTimeString()}`}
        </button>
      </div>

      <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderTop: 'none', borderRadius: '0 0 var(--radius-xl) var(--radius-xl)', overflow: 'hidden' }}>
        {activeTab === 'rules' && (
          <ValidationRules rules={rules} onRulesChange={setRules} columns={columns} disabled={!ready} fileBacked={!!target.uploaded_file_id} />
        )}
        {activeTab === 'results' && (
          <ValidationResults run={run} objectName={target.object_name} onReRun={handleRun} onGoToRules={() => setActiveTab('rules')} />
        )}
      </div>
    </div>
  );
}
