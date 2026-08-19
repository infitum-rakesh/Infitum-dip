/**
 * Validation — page orchestrator (Phase 7).
 * Picks a real connection + object, edits a real rule set (saved as a
 * ValidationRuleSet row), and runs it for real via
 * backend/app/validation/engine.py. Tabs: Rules (configure) | Results
 * (after run) — same shape as before, now backed by real data.
 */
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Play, Save, Search } from 'lucide-react';
import { Button, StatusBadge } from '@/components/common/index.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { validationApi } from '@/services/api/validationApi.js';
import { ValidationRules } from './ValidationRules.jsx';
import { ValidationResults } from './ValidationResults.jsx';
import './validation.css';

function apiErrorMessage(err, fallback) { return err?.message || fallback; }

function ObjectPicker({ value, onChange }) {
  const [connections, setConnections] = useState([]);
  const [schemas, setSchemas] = useState([]);
  const [objects, setObjects] = useState([]);
  const [search, setSearch] = useState('');
  const [loadingSchemas, setLoadingSchemas] = useState(false);
  const [loadingObjects, setLoadingObjects] = useState(false);

  useEffect(() => { connectionApi.list({ category: 'DATABASE', page_size: 200 }).then((r) => setConnections(r.items)).catch(() => {}); }, []);

  useEffect(() => {
    if (!value.connection_id) { setSchemas([]); return; }
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
  }, [value.connection_id]);

  useEffect(() => {
    if (!value.connection_id || !value.schema_name) { setObjects([]); return; }
    setLoadingObjects(true);
    const t = setTimeout(() => {
      connectionApi.getObjects(value.connection_id, { schema: value.schema_name, q: search || undefined })
        .then((r) => setObjects(r.items)).catch(() => setObjects([])).finally(() => setLoadingObjects(false));
    }, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value.connection_id, value.schema_name, search]);

  return (
    <div className="vld-picker-row">
      <select className="vld-picker-select" value={value.connection_id ?? ''}
        onChange={(e) => onChange({ connection_id: e.target.value ? Number(e.target.value) : null, schema_name: '', object_name: '' })}>
        <option value="">Connection…</option>
        {connections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
      </select>
      <select className="vld-picker-select" disabled={!value.connection_id} value={value.schema_name}
        onChange={(e) => onChange({ ...value, schema_name: e.target.value, object_name: '' })}>
        <option value="">{loadingSchemas ? 'Loading…' : 'Schema…'}</option>
        {schemas.map((s) => <option key={s} value={s}>{s}</option>)}
      </select>
      <select className="vld-picker-select" disabled={!value.schema_name} value={value.object_name}
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
  );
}

export default function Validation() {
  const { ruleSetId } = useParams();
  const navigate = useNavigate();

  const [id, setId] = useState(ruleSetId ? Number(ruleSetId) : null);
  const [name, setName] = useState('Untitled Rule Set');
  const [target, setTarget] = useState({ connection_id: null, schema_name: '', object_name: '' });
  const [columns, setColumns] = useState([]);
  const [rules, setRules] = useState([]);
  const [savedSnapshot, setSavedSnapshot] = useState(null);

  const [ruleSets, setRuleSets] = useState([]);

  const [activeTab, setActiveTab] = useState('rules');
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [run, setRun] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(!!ruleSetId);

  const locked = !!id;
  const ready = !!(target.connection_id && target.object_name);
  const dirty = savedSnapshot !== null ? JSON.stringify({ name, rules }) !== savedSnapshot : rules.length > 0;

  useEffect(() => { validationApi.listRuleSets().then((r) => setRuleSets(r.items)).catch(() => {}); }, [id]);

  useEffect(() => {
    if (!ruleSetId) return;
    setLoading(true);
    validationApi.getRuleSet(ruleSetId).then((d) => {
      setId(d.id); setName(d.name);
      setTarget({ connection_id: d.connection_id, schema_name: d.schema_name || '', object_name: d.object_name });
      setRules(d.rules);
      setSavedSnapshot(JSON.stringify({ name: d.name, rules: d.rules }));
    }).catch((err) => setError(apiErrorMessage(err, 'Could not load this rule set.'))).finally(() => setLoading(false));
  }, [ruleSetId]);

  useEffect(() => {
    if (!ready) { setColumns([]); return; }
    connectionApi.getColumns(target.connection_id, target.object_name, { schema: target.schema_name || undefined })
      .then((r) => setColumns(r.columns)).catch(() => setColumns([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target.connection_id, target.schema_name, target.object_name]);

  const runSave = async () => {
    setSaving(true); setError('');
    try {
      if (id) {
        const r = await validationApi.updateRuleSet(id, { name, rules });
        setSavedSnapshot(JSON.stringify({ name: r.name, rules: r.rules }));
      } else {
        const r = await validationApi.createRuleSet({ name, connection_id: target.connection_id, schema_name: target.schema_name || null, object_name: target.object_name, rules });
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
          <input className="vld-name-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Rule set name…" />
          <p className="vld-subtitle">
            Data Validation — real rule checks against a live connection
            {ruleSets.length > 0 && (
              <select className="vld-ruleset-select" value={id ?? ''} onChange={(e) => { if (e.target.value) navigate(`/app/validation/${e.target.value}`); else navigate('/app/validation'); }}>
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
          <ValidationRules rules={rules} onRulesChange={setRules} columns={columns} disabled={!ready} />
        )}
        {activeTab === 'results' && (
          <ValidationResults run={run} objectName={target.object_name} onReRun={handleRun} />
        )}
      </div>
    </div>
  );
}
