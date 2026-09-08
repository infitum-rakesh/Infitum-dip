/**
 * Lineage — Data Lineage Workspace (Phase MM/OO). Every tab reads live
 * off /api/lineage/* (see backend app/lineage/graph.py) — the graph is
 * derived from the real saved pipeline definition + column mappings on
 * every call, never a separately maintained copy.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { AlertTriangle, ArrowRight, Search } from 'lucide-react';
import { Button } from '@/components/common/index.js';
import { MAPPING_TYPE_LABELS, lineageApi } from '@/services/api/lineageApi.js';
import { invoiceApi } from '@/services/api/invoiceApi.js';
import { LineageGraph } from './LineageGraph.jsx';
import './lineage.css';

const TABS = [
  { id: 'overview', label: 'Lineage Overview' },
  { id: 'pipeline', label: 'Pipeline Lineage' },
  { id: 'invoice', label: 'Invoice Lineage' },
  { id: 'column', label: 'Column Lineage' },
  { id: 'execution', label: 'Execution Lineage' },
  { id: 'impact', label: 'Impact Analysis' },
  { id: 'search', label: 'Search' },
];

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

export default function Lineage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState(searchParams.get('tab') || 'overview');
  const [pipelineId, setPipelineId] = useState(searchParams.get('pipelineId') ? Number(searchParams.get('pipelineId')) : null);
  const [jobId, setJobId] = useState(searchParams.get('jobId') ? Number(searchParams.get('jobId')) : null);
  const [invoiceId, setInvoiceId] = useState(searchParams.get('invoiceId') ? Number(searchParams.get('invoiceId')) : null);

  // Real bug fix: this used to only push the id into the URL's query
  // string (setSearchParams) without ever updating the actual pipelineId/
  // jobId/invoiceId state those tabs render from. useSearchParams() does
  // NOT re-derive component state on its own — a tab's own "select an
  // invoice/pipeline" dropdown called this and the URL bar updated, but
  // the tab kept rendering its old (usually null) id forever. Only the
  // one call site that happened to ALSO call the real setState directly
  // (Overview's onOpenPipeline) ever worked. Now selectTab is the single
  // place that keeps both the URL and the real state in sync.
  const selectTab = (id, extra = {}) => {
    setActiveTab(id);
    if ('pipelineId' in extra) setPipelineId(extra.pipelineId ?? null);
    if ('jobId' in extra) setJobId(extra.jobId ?? null);
    if ('invoiceId' in extra) setInvoiceId(extra.invoiceId ?? null);
    const params = { tab: id, ...extra };
    Object.keys(params).forEach((k) => params[k] == null && delete params[k]);
    setSearchParams(params, { replace: true });
  };

  return (
    <div className="lin-page">
      <div className="lin-header">
        <div>
          <h1 className="lin-title">Data Lineage</h1>
          <p className="lin-subtitle">Source-to-target lineage, derived live from real pipeline definitions and column mappings.</p>
        </div>
      </div>

      <div className="lin-tabs">
        {TABS.map((t) => (
          <button key={t.id} className={`lin-tab-btn ${activeTab === t.id ? 'active' : ''}`} onClick={() => selectTab(t.id)}>{t.label}</button>
        ))}
      </div>

      {activeTab === 'overview' && <OverviewTab onOpenPipeline={(id) => { setPipelineId(id); selectTab('pipeline', { pipelineId: id }); }} />}
      {activeTab === 'pipeline' && <PipelineLineageTab pipelineId={pipelineId} setPipelineId={(id) => selectTab('pipeline', { pipelineId: id })} />}
      {activeTab === 'invoice' && <InvoiceLineageTab invoiceId={invoiceId} setInvoiceId={(id) => selectTab('invoice', { invoiceId: id })} />}
      {activeTab === 'column' && <ColumnLineageTab />}
      {activeTab === 'execution' && <ExecutionLineageTab jobId={jobId} setJobId={(id) => selectTab('execution', { jobId: id, pipelineId })} />}
      {activeTab === 'impact' && <ImpactTab />}
      {activeTab === 'search' && <SearchTab onOpenPipeline={(id) => selectTab('pipeline', { pipelineId: id })} onOpenMapping={() => selectTab('column')} />}
    </div>
  );
}

// ── Overview ──

function OverviewTab({ onOpenPipeline }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => { lineageApi.getOverview().then(setData).catch((e) => setError(apiErrorMessage(e, 'Could not load lineage overview.'))); }, []);

  if (error) return <div className="lin-error-text">{error}</div>;
  if (!data) return <div className="lin-muted">Loading…</div>;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="lin-kpi-grid">
        <div className="lin-kpi-card"><span className="lin-kpi-label">Pipelines</span><span className="lin-kpi-value">{data.pipeline_count}</span></div>
        <div className="lin-kpi-card"><span className="lin-kpi-label">Column Mappings</span><span className="lin-kpi-value">{data.column_mapping_count}</span></div>
        <div className="lin-kpi-card"><span className="lin-kpi-label">Connections</span><span className="lin-kpi-value">{data.connection_count}</span></div>
      </div>
      <div className="lin-card">
        <div className="lin-card-title">Pipelines</div>
        {data.pipelines.length === 0 ? <div className="lin-muted">No pipelines yet — build one in Pipeline Builder.</div> : (
          <div className="lin-pipeline-list">
            {data.pipelines.map((p) => (
              <div className="lin-pipeline-item" key={p.id} onClick={() => onOpenPipeline(p.id)}>
                <span>{p.name} <span className="lin-muted">v{p.current_version}</span></span>
                <ArrowRight size={14} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Pipeline Lineage ──

function PipelineLineageTab({ pipelineId, setPipelineId }) {
  const navigate = useNavigate();
  const [pipelines, setPipelines] = useState([]);
  const [versions, setVersions] = useState([]);
  const [version, setVersion] = useState(null);
  const [graph, setGraph] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => { lineageApi.getOverview().then((o) => setPipelines(o.pipelines)).catch(() => {}); }, []);
  useEffect(() => {
    if (!pipelineId) return;
    setVersion(null);
    lineageApi.listPipelineVersions(pipelineId).then(setVersions).catch(() => setVersions([]));
  }, [pipelineId]);
  useEffect(() => {
    if (!pipelineId) { setGraph(null); return; }
    lineageApi.getPipelineLineage(pipelineId, version || undefined).then(setGraph).catch((e) => setError(apiErrorMessage(e, 'Could not load pipeline lineage.')));
  }, [pipelineId, version]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="lin-filters">
        <select className="lin-select" value={pipelineId ?? ''} onChange={(e) => setPipelineId(e.target.value ? Number(e.target.value) : null)}>
          <option value="">Select a pipeline…</option>
          {pipelines.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        {versions.length > 0 && (
          <select className="lin-select" value={version ?? ''} onChange={(e) => setVersion(e.target.value ? Number(e.target.value) : null)}>
            {versions.map((v) => <option key={v.version_number} value={v.version_number}>v{v.version_number}{v.is_current ? ' (current)' : ''}</option>)}
          </select>
        )}
        {graph && (
          <Button size="sm" variant="ghost" onClick={() => navigate('/app/pipeline')}>Open in Pipeline Builder</Button>
        )}
      </div>
      {error && <div className="lin-error-text">{error}</div>}
      {!pipelineId ? (
        <div className="lin-card"><div className="lin-muted">Select a pipeline above to see its lineage graph.</div></div>
      ) : graph ? (
        <LineageGraph nodes={graph.nodes} edges={graph.edges} />
      ) : (
        <div className="lin-muted">Loading…</div>
      )}
    </div>
  );
}

// ── Invoice Lineage (INV22) ──

function InvoiceLineageTab({ invoiceId, setInvoiceId }) {
  const navigate = useNavigate();
  const [invoices, setInvoices] = useState([]);
  const [graph, setGraph] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    invoiceApi.list({ page_size: 100, sort_by: 'created_at', sort_dir: 'desc' })
      .then((r) => setInvoices(r.items || []))
      .catch(() => {});
  }, []);
  useEffect(() => {
    if (!invoiceId) { setGraph(null); return; }
    setError('');
    lineageApi.getInvoiceLineage(invoiceId).then(setGraph).catch((e) => setError(apiErrorMessage(e, 'Could not load invoice lineage.')));
  }, [invoiceId]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="lin-filters">
        <select className="lin-select" value={invoiceId ?? ''} onChange={(e) => setInvoiceId(e.target.value ? Number(e.target.value) : null)}>
          <option value="">Select an invoice…</option>
          {invoices.map((inv) => <option key={inv.id} value={inv.id}>{inv.invoice_number || `Invoice #${inv.id}`} — {inv.vendor_name || 'Unknown vendor'}</option>)}
        </select>
        {graph && (
          <Button size="sm" variant="ghost" onClick={() => navigate(`/app/invoice-intel/${invoiceId}`)}>Open in Invoice Detail</Button>
        )}
      </div>
      {error && <div className="lin-error-text">{error}</div>}
      {!invoiceId ? (
        <div className="lin-card"><div className="lin-muted">Select an invoice above to trace its real Document → OCR → AI/Custom-Engine extraction → normalized fields → validation → ERP mapping → posting → reconciliation chain — only stages that actually happened for that invoice are shown.</div></div>
      ) : graph ? (
        <LineageGraph nodes={graph.nodes} edges={graph.edges} />
      ) : (
        <div className="lin-muted">Loading…</div>
      )}
    </div>
  );
}

// ── Column Lineage ──

function ColumnLineageTab() {
  const [mappings, setMappings] = useState([]);
  const [mappingId, setMappingId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => { lineageApi.listColumnMappings().then(setMappings).catch(() => {}); }, []);
  useEffect(() => {
    if (!mappingId) { setDetail(null); return; }
    lineageApi.getColumnMappingLineage(mappingId).then(setDetail).catch((e) => setError(apiErrorMessage(e, 'Could not load column lineage.')));
  }, [mappingId]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="lin-filters">
        <select className="lin-select" value={mappingId ?? ''} onChange={(e) => setMappingId(e.target.value ? Number(e.target.value) : null)}>
          <option value="">Select a mapping…</option>
          {mappings.map((m) => <option key={m.id} value={m.id}>{m.name} ({m.source_object} → {m.target_object})</option>)}
        </select>
      </div>
      {error && <div className="lin-error-text">{error}</div>}
      {!mappingId ? (
        <div className="lin-card"><div className="lin-muted">Select a saved Object &amp; Column Mapping above to see its column-level lineage, including real transformation expressions.</div></div>
      ) : !detail ? <div className="lin-muted">Loading…</div> : (
        <div className="lin-card">
          <div className="lin-card-title">{detail.source_connection}: {detail.source_object} <ArrowRight size={12} style={{ display: 'inline', verticalAlign: 'middle' }} /> {detail.target_connection}: {detail.target_object}</div>
          <div style={{ overflowX: 'auto' }}>
            <table className="lin-col-table">
              <thead><tr><th>Source Column(s)</th><th>Transformation</th><th>Target Column</th><th>Mapping Type</th><th>Status</th></tr></thead>
              <tbody>
                {detail.columns.map((c, i) => (
                  <tr key={i}>
                    <td>{c.source_columns.join(', ') || (c.transformation ? '(constant/derived)' : '—')}</td>
                    <td>{c.transformation ? <span className="lin-expr">{c.transformation}</span> : <span className="lin-muted">passthrough</span>}</td>
                    <td>{c.target_column}</td>
                    <td><span className="lin-mtype-chip">{MAPPING_TYPE_LABELS[c.mapping_type] || c.mapping_type}</span></td>
                    <td>{c.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Execution Lineage ──

function ExecutionLineageTab({ jobId, setJobId }) {
  const [id, setId] = useState(jobId ?? '');
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  const load = (jid) => {
    lineageApi.getExecutionLineage(jid).then(setData).catch((e) => setError(apiErrorMessage(e, 'Could not load execution lineage — check the job ID.')));
  };
  useEffect(() => { if (jobId) load(jobId); }, [jobId]); // eslint-disable-line

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="lin-filters">
        <input className="lin-input" placeholder="Pipeline Job ID" value={id} onChange={(e) => setId(e.target.value)} style={{ width: 160 }} />
        <Button size="sm" onClick={() => { setError(''); setJobId(Number(id)); load(Number(id)); }} disabled={!id}>Load</Button>
      </div>
      {error && <div className="lin-error-text">{error}</div>}
      {!data ? (
        <div className="lin-card"><div className="lin-muted">Enter a Pipeline Job ID (from Monitoring → Pipeline Runs) to see its execution lineage — the pipeline's version graph overlaid with real per-node row counts from that run.</div></div>
      ) : (
        <>
          <div className="lin-card">
            <div className="lin-card-title">Job #{data.job_id} — {data.pipeline_name} v{data.pipeline_version}</div>
            <div className="lin-muted">Status: {data.status} · Records: {data.records_in ?? 0} → {data.records_out ?? 0}</div>
          </div>
          <LineageGraph nodes={data.graph.nodes} edges={data.graph.edges} />
        </>
      )}
    </div>
  );
}

// ── Impact Analysis ──

function ImpactTab() {
  const [connectionId, setConnectionId] = useState('');
  const [objectName, setObjectName] = useState('');
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');

  const run = () => {
    setError('');
    lineageApi.getImpact({ connection_id: connectionId || undefined, object_name: objectName || undefined })
      .then(setResult).catch((e) => setError(apiErrorMessage(e, 'Could not run impact analysis.')));
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="lin-filters">
        <input className="lin-input" placeholder="Connection ID (optional)" value={connectionId} onChange={(e) => setConnectionId(e.target.value)} style={{ width: 180 }} />
        <input className="lin-input" placeholder="Object name (table/dataset)" value={objectName} onChange={(e) => setObjectName(e.target.value)} style={{ width: 220 }} />
        <Button size="sm" onClick={run} disabled={!connectionId && !objectName}>Analyze</Button>
      </div>
      {error && <div className="lin-error-text">{error}</div>}
      {!result ? (
        <div className="lin-card"><div className="lin-muted">Enter a connection and/or object name to see every pipeline and column mapping that uses it, upstream and downstream.</div></div>
      ) : (
        <>
          <div className="lin-warning-banner"><AlertTriangle size={13} style={{ verticalAlign: 'middle', marginRight: 6 }} />{result.warning_summary}</div>
          <div className="lin-card">
            <div className="lin-card-title">Used By Pipelines</div>
            {result.used_by_pipelines.length === 0 ? <div className="lin-muted">None.</div> : (
              <table className="lin-col-table">
                <thead><tr><th>Pipeline</th><th>Version</th><th>Role</th></tr></thead>
                <tbody>{result.used_by_pipelines.map((p, i) => <tr key={i}><td>{p.pipeline_name}</td><td>v{p.version_number}</td><td>{p.role}</td></tr>)}</tbody>
              </table>
            )}
          </div>
          <div className="lin-card">
            <div className="lin-card-title">Used By Object &amp; Column Mappings</div>
            {result.used_by_mappings.length === 0 ? <div className="lin-muted">None.</div> : (
              <table className="lin-col-table">
                <thead><tr><th>Mapping</th><th>Role</th></tr></thead>
                <tbody>{result.used_by_mappings.map((m, i) => <tr key={i}><td>{m.name}</td><td>{m.role}</td></tr>)}</tbody>
              </table>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// ── Global Search ──

function SearchTab({ onOpenPipeline, onOpenMapping }) {
  const [q, setQ] = useState('');
  const [results, setResults] = useState([]);
  const [error, setError] = useState('');

  const run = () => {
    if (!q.trim()) return;
    lineageApi.search(q).then(setResults).catch((e) => setError(apiErrorMessage(e, 'Search failed.')));
  };

  return (
    <div className="lin-card">
      <div className="lin-filters">
        <input className="lin-input" placeholder="Search by database, schema, table, column, pipeline, dataset…" value={q}
          onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && run()} style={{ width: 340 }} />
        <Button size="sm" icon={<Search size={12} />} onClick={run}>Search</Button>
      </div>
      {error && <div className="lin-error-text">{error}</div>}
      <div className="lin-search-results">
        {results.map((r, i) => (
          <div className="lin-search-row" key={i} onClick={() => (r.ref.kind === 'pipeline' ? onOpenPipeline(r.ref.pipeline_id) : r.ref.kind === 'mapping' ? onOpenMapping() : null)}>
            <span><strong>{r.asset}</strong> <span className="lin-muted">{r.asset_type} · {r.system}</span></span>
            <span className="lin-muted">{r.used_by != null ? `${r.used_by} pipeline(s)` : ''}</span>
          </div>
        ))}
        {q && results.length === 0 && <div className="lin-muted">No results.</div>}
      </div>
    </div>
  );
}
