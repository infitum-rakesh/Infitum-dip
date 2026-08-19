/**
 * Stage 4 — Structured Dataset
 * Publishes the AI extraction result as a typed, profiled dataset.
 * Tabs: Table | JSON | Schema | Profile
 *
 * "Continue to Transformation" used to just navigate('/app/transform')
 * with no data — the extracted fields were silently discarded. It now
 * genuinely persists the extracted record as a real file (via the same
 * /api/pipeline-files/upload endpoint the Source Dataset node's own
 * "Upload File" option uses — see pipelineFileApi.js) and hands Transform
 * Workspace a real starting pipeline: Source (that uploaded file) →
 * Output Dataset, so the user can pick a real DB table or file as the
 * write target, then Save (persists via /api/pipelines) and Run it from
 * Pipeline Builder like any other pipeline.
 */
import { useState, useEffect } from 'react';
import { ArrowLeft, Download, ArrowRight, CheckCircle2 } from 'lucide-react';
import { Button, LoadingSpinner, StatusBadge, DataTable } from '@/components/common/index.js';
import { extractionApi } from '@/services/api/extractionApi.js';
import { pipelineFileApi } from '@/services/api/pipelineFileApi.js';
import { createNode, createEdge } from '@/components/pipeline/nodeTypes.js';
import './document-intelligence.css';

const TABS = [
  { id: 'table',   label: '≡ Table' },
  { id: 'json',    label: '{ } JSON' },
  { id: 'schema',  label: '⚙ Schema' },
  { id: 'profile', label: '📊 Profile' },
];

function csvEscape(v) {
  const s = String(v ?? '');
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** Real CSV built from the actual extracted field values — one header
 * row + one data row (this stage extracts one record per document). */
function datasetToCsv(dataset) {
  const cols = dataset.headerFields.map((f) => f.name);
  const row = dataset.headerFields.map((f) => f.value);
  return `${cols.map(csvEscape).join(',')}\n${row.map(csvEscape).join(',')}\n`;
}

/**
 * Uploads the extracted record as a real file (real backend call — same
 * path as manually uploading a file into a Source Dataset node) and
 * returns a ready-to-edit pipeline: Source(that file) → Output Dataset.
 */
async function buildInitialPipelineFromDataset(dataset, doc) {
  const csv = datasetToCsv(dataset);
  const file = new File([csv], `${dataset.name}.csv`, { type: 'text/csv' });

  const sourceNode = createNode('source', { x: 60, y: 160 });
  const targetNode = createNode('target', { x: 420, y: 160 });

  const uploaded = await pipelineFileApi.upload(file, sourceNode.id);

  sourceNode.config = { ...sourceNode.config, objectType: 'FILE', uploadedFileId: uploaded.id };
  sourceNode.label = `${dataset.name} (extracted)`;
  targetNode.label = 'Write extracted data';

  return {
    name: `${doc?.filename ?? 'Extracted document'} — ${dataset.name}`,
    nodes: [sourceNode, targetNode],
    edges: [createEdge(sourceNode.id, targetNode.id)],
  };
}

export function StructuredDataset({ extractionData, onBack, onContinue }) {
  const { document: doc, extractionResult } = extractionData;

  const [activeTab, setActiveTab]   = useState('table');
  const [dataset, setDataset]       = useState(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState('');
  const [preparing, setPreparing]   = useState(false);

  useEffect(() => {
    buildDataset();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const buildDataset = async () => {
    setLoading(true);
    setError('');
    try {
      const ds = await extractionApi.createDataset(extractionResult);
      setDataset(ds);
    } catch {
      setError('Failed to create dataset. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  const handleExportCsv = () => {
    const blob = new Blob([datasetToCsv(dataset)], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${dataset.name}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const handleContinue = async () => {
    if (!onContinue) return;
    setPreparing(true);
    setError('');
    try {
      const initialPipeline = await buildInitialPipelineFromDataset(dataset, doc);
      onContinue(initialPipeline);
    } catch (err) {
      setError(err?.response?.data?.detail ?? 'Could not send this dataset to Transform Workspace — please try again.');
    } finally {
      setPreparing(false);
    }
  };

  // Build table columns from line items
  const lineItemCols = dataset
    ? Object.keys(dataset.lineItems[0] ?? {}).map((k) => ({
        key: k, header: k.replace(/_/g, ' '),
        sortable: true,
        render: (val) => {
          if (k === 'unit_price' || k === 'total')
            return `$${Number(val).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
          return val;
        },
      }))
    : [];

  return (
    <div className="di-stage">
      <div className="di-stage-header">
        <div>
          <h2 className="di-stage-title">Structured Dataset</h2>
          <p className="di-stage-subtitle">
            {dataset ? (
              <>Dataset <strong>{dataset.name}</strong> — {dataset.rows} records · {dataset.columns} columns · Quality Score: <strong>{dataset.qualityScore}/100</strong></>
            ) : 'Building structured dataset from extracted fields…'}
          </p>
        </div>
        <div className="di-stage-actions">
          <Button variant="secondary" icon={<ArrowLeft size={14} />} onClick={onBack}>Back</Button>
          {dataset && (
            <>
              <Button variant="secondary" icon={<Download size={14} />} onClick={handleExportCsv}>Export CSV</Button>
              {onContinue && (
                <Button variant="primary" icon={<ArrowRight size={14} />} loading={preparing} disabled={preparing} onClick={handleContinue}>
                  {preparing ? 'Preparing…' : 'Continue to Transformation'}
                </Button>
              )}
            </>
          )}
        </div>
      </div>

      {error && (
        <div style={{ padding: '10px 14px', marginBottom: 16, background: 'var(--color-danger-bg)', borderRadius: 8, fontSize: 13, color: 'var(--color-danger-dark)', border: '1px solid var(--color-danger-100,#FEE2E2)' }}>
          {error} <Button variant="ghost" size="sm" onClick={buildDataset} style={{ marginLeft: 8 }}>Retry</Button>
        </div>
      )}

      {loading ? (
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: 400, gap: 12 }}>
          <LoadingSpinner size="lg" />
          <p style={{ fontSize: 14, fontWeight: 500 }}>Publishing dataset…</p>
          <p style={{ fontSize: 13, color: 'var(--text-muted)' }}>Profiling, type-casting and quality scoring</p>
        </div>
      ) : dataset ? (
        <div className="di-dataset-layout">
          {/* ── Main dataset area ── */}
          <div className="di-dataset-main">
            {/* Tab nav */}
            <div className="di-tab-nav">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  className={`di-tab-btn ${activeTab === t.id ? 'active' : ''}`}
                  onClick={() => setActiveTab(t.id)}
                >
                  {t.label}
                </button>
              ))}
            </div>

            {/* ── Table view ── */}
            {activeTab === 'table' && (
              <div style={{ padding: 0 }}>
                {/* Header fields summary */}
                <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border)', background: 'var(--bg-body)' }}>
                  <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.4px' }}>
                    Header Fields
                  </div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                    {dataset.headerFields.slice(0, 6).map((f) => (
                      <div key={f.name} style={{ background: 'white', border: '1px solid var(--border)', borderRadius: 8, padding: '4px 10px', fontSize: 12 }}>
                        <span style={{ color: 'var(--text-muted)' }}>{f.name}: </span>
                        <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
                          {typeof f.value === 'number' && f.name.includes('amount')
                            ? `$${Number(f.value).toLocaleString('en-US', { minimumFractionDigits: 2 })}`
                            : String(f.value)}
                        </span>
                      </div>
                    ))}
                    {dataset.headerFields.length > 6 && (
                      <div style={{ background: 'var(--color-primary-light)', border: '1px solid var(--color-primary-mid)', borderRadius: 8, padding: '4px 10px', fontSize: 12, color: 'var(--color-primary)', fontWeight: 500 }}>
                        +{dataset.headerFields.length - 6} more
                      </div>
                    )}
                  </div>
                </div>
                {/* Line items table */}
                <div style={{ padding: 0 }}>
                  <div style={{ padding: '10px 16px', fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.4px', borderBottom: '1px solid var(--border)', background: 'var(--bg-body)' }}>
                    Line Items ({dataset.lineItems.length} rows)
                  </div>
                  <DataTable
                    columns={lineItemCols}
                    data={dataset.lineItems}
                  />
                </div>
              </div>
            )}

            {/* ── JSON view ── */}
            {activeTab === 'json' && (
              <div style={{ padding: 16 }}>
                <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => navigator.clipboard.writeText(JSON.stringify({ header: Object.fromEntries(dataset.headerFields.map((f) => [f.name, f.value])), line_items: dataset.lineItems }, null, 2))}
                  >
                    Copy JSON
                  </Button>
                </div>
                <pre className="di-json-pre">
{JSON.stringify({
  dataset: dataset.name,
  quality_score: dataset.qualityScore,
  header: Object.fromEntries(dataset.headerFields.map((f) => [f.name, f.value])),
  line_items: dataset.lineItems,
}, null, 2)}
                </pre>
              </div>
            )}

            {/* ── Schema view ── */}
            {activeTab === 'schema' && (
              <div>
                <div style={{ display: 'grid', gridTemplateColumns: '160px 80px 60px 1fr', gap: 8, padding: '10px 16px', background: 'var(--bg-body)', borderBottom: '1px solid var(--border)' }}>
                  {['Column', 'Type', 'Required', 'Sample Value'].map((h) => (
                    <span key={h} style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>{h}</span>
                  ))}
                </div>
                {dataset.profile.map((col) => (
                  <div key={col.column} style={{ display: 'grid', gridTemplateColumns: '160px 80px 60px 1fr', gap: 8, padding: '9px 16px', borderBottom: '1px solid #F1F5F9', fontSize: 13, alignItems: 'center' }}>
                    <span style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{col.column}</span>
                    <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-muted)' }}>{col.type}</span>
                    <StatusBadge variant={col.nullable ? 'gray' : 'primary'} size="sm">{col.nullable ? 'optional' : 'required'}</StatusBadge>
                    <span style={{ fontSize: 12, color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{col.sample}</span>
                  </div>
                ))}
              </div>
            )}

            {/* ── Profile view ── */}
            {activeTab === 'profile' && (
              <div>
                <div style={{ display: 'grid', gridTemplateColumns: '140px 60px 60px 1fr', gap: 8, padding: '10px 16px', background: 'var(--bg-body)', borderBottom: '1px solid var(--border)' }}>
                  {['Column', 'Type', 'Null %', 'Completeness'].map((h) => (
                    <span key={h} style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>{h}</span>
                  ))}
                </div>
                {dataset.profile.map((col) => (
                  <div key={col.column} className="di-profile-row">
                    <span className="di-profile-col">{col.column}</span>
                    <span className="di-profile-type">{col.type}</span>
                    <span className="di-profile-null">{col.nullPct}%</span>
                    <div className="di-profile-bar">
                      <div className="di-profile-bar-fill" style={{ width: `${100 - col.nullPct}%` }} />
                    </div>
                  </div>
                ))}
                {/* Quality summary */}
                <div style={{ padding: 16, borderTop: '1px solid var(--border)', display: 'flex', gap: 16, flexWrap: 'wrap' }}>
                  {[
                    { label: 'Quality Score', val: `${dataset.qualityScore}/100`, color: 'var(--color-success-dark)', bg: 'var(--color-success-bg)' },
                    { label: 'Total Fields', val: dataset.profile.length, color: 'var(--color-primary)', bg: 'var(--color-primary-light)' },
                    { label: 'Line Items', val: dataset.lineItems.length, color: 'var(--color-info-dark, #1D4ED8)', bg: 'var(--color-info-bg)' },
                  ].map((s) => (
                    <div key={s.label} style={{ padding: '8px 14px', background: s.bg, borderRadius: 10, textAlign: 'center', minWidth: 100 }}>
                      <div style={{ fontSize: 22, fontWeight: 800, color: s.color, letterSpacing: '-1px' }}>{s.val}</div>
                      <div style={{ fontSize: 11, color: s.color, opacity: 0.8, marginTop: 2 }}>{s.label}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* ── Source reference sidebar ── */}
          <div className="di-dataset-source-card">
            <div className="di-source-header">Source Document</div>
            <div className="di-source-body">
              <div className="di-source-thumb">📄</div>
              <div className="di-source-meta">
                <div className="di-source-row">
                  <span className="di-source-key">File</span>
                  <span className="di-source-val" style={{ fontSize: 11, maxWidth: 130, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {doc?.filename ?? 'document.pdf'}
                  </span>
                </div>
                <div className="di-source-row">
                  <span className="di-source-key">Pages</span>
                  <span className="di-source-val">{doc?.pageCount ?? 1}</span>
                </div>
                <div className="di-source-row">
                  <span className="di-source-key">Dataset</span>
                  <span className="di-source-val" style={{ fontSize: 11 }}>{dataset.name}</span>
                </div>
                <div className="di-source-row">
                  <span className="di-source-key">Created</span>
                  <span className="di-source-val" style={{ fontSize: 11 }}>{new Date(dataset.createdAt).toLocaleDateString()}</span>
                </div>
              </div>

              <div style={{ marginTop: 16 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 10px', background: 'var(--color-success-bg)', borderRadius: 8, border: '1px solid var(--color-success-100, #D1FAE5)' }}>
                  <CheckCircle2 size={15} color="var(--color-success)" />
                  <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-success-dark)' }}>Dataset Published</span>
                </div>
              </div>

              {onContinue && (
                <Button
                  variant="primary"
                  full
                  style={{ marginTop: 12 }}
                  icon={<ArrowRight size={13} />}
                  loading={preparing}
                  disabled={preparing}
                  onClick={handleContinue}
                >
                  {preparing ? 'Preparing…' : 'Go to Transformation'}
                </Button>
              )}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
