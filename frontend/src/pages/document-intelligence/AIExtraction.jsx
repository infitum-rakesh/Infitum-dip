/**
 * Stage 3 — AI Extraction
 * Left: AI connection picker + schema builder (add/remove/configure fields).
 * Right: extracted values with confidence scores.
 * Auto-detects schema from the document's real OCR text using whichever
 * AI Provider connection (OpenAI/Anthropic/Ollama — see Connections page)
 * is selected; user can customise the schema before running. Real calls
 * only — see backend/app/services/extraction_service.py.
 *
 * The picker also always offers "Custom (No AI Required)" — the real,
 * deterministic, rule-based Custom Extraction Engine (backend
 * app/extraction/custom_engine.py, NOT a language model) — and it's
 * auto-selected once connections finish loading and none exist, so this
 * stage still works with zero setup. Its id is the sentinel 0 (see
 * extraction_service.CUSTOM_ENGINE_CONNECTION_ID) — safe because real
 * Connection ids are DB autoincrement starting at 1. Every connectionId
 * check below is a strict null-check (never plain truthy) specifically
 * so 0 isn't mistaken for "nothing selected".
 */
import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, ArrowRight, Plus, Trash2, Sparkles } from 'lucide-react';
import { Button, LoadingSpinner, StatusBadge } from '@/components/common/index.js';
import { extractionApi } from '@/services/api/extractionApi.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { documentApi } from '@/services/api/documentApi.js';
import './document-intelligence.css';

const FIELD_TYPES = ['string', 'number', 'date', 'boolean', 'array', 'object'];
const DOC_TYPE_LABEL = { INVOICE: 'Invoice', GENERIC: 'General (wide-net)' };

function confColor(pct) { return pct >= 90 ? 'high' : pct >= 75 ? 'med' : 'low'; }
function confVariant(pct) { return pct >= 90 ? 'success' : pct >= 75 ? 'warning' : 'danger'; }

export function AIExtraction({ ocrData, onComplete, onBack }) {
  const navigate = useNavigate();
  const { document: doc } = ocrData;

  const [connections, setConnections]     = useState([]);
  const [loadingConns, setLoadingConns]   = useState(true);
  const [connectionId, setConnectionId]   = useState(null);

  const [docTypes, setDocTypes]           = useState([]);
  const [documentType, setDocumentType]   = useState('');

  const [schema, setSchema]               = useState([]);
  const [detecting, setDetecting]         = useState(false);
  const [extracting, setExtracting]       = useState(false);
  const [extractionResult, setResult]     = useState(null);
  const [viewMode, setViewMode]           = useState('table'); // 'table' | 'json'
  const [error, setError]                 = useState('');

  // Load AI Agent connections once, default to the first active one — or
  // to the Custom Extraction Engine (sentinel id 0) when none exist.
  useEffect(() => {
    connectionApi.list({ category: 'AI_PROVIDER', page_size: 50 })
      .then((r) => {
        const items = r.items ?? [];
        setConnections(items);
        const active = items.find((c) => c.status === 'ACTIVE') ?? items[0];
        setConnectionId(active ? active.id : 0);
      })
      .catch(() => setConnectionId(0))
      .finally(() => setLoadingConns(false));
    documentApi.getDocumentTypes().then(setDocTypes).catch(() => setDocTypes([]));
  }, []);

  const handleAutoDetect = useCallback(async () => {
    if (connectionId == null) return;
    setDetecting(true);
    setError('');
    try {
      const detected = await extractionApi.detectSchema(doc.id, connectionId, documentType || null);
      setSchema(detected);
    } catch (err) {
      setError(err?.response?.data?.detail ?? 'Could not auto-detect a schema from this document.');
      setSchema([]);
    } finally {
      setDetecting(false);
    }
  }, [doc.id, connectionId, documentType]);

  // Auto-detect once a connection is selected/available (not before), and
  // again whenever the document type changes (seeds a different schema).
  useEffect(() => {
    if (connectionId != null) handleAutoDetect();
  }, [connectionId, documentType]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleAddField = () => {
    setSchema((prev) => [
      ...prev,
      { _id: Date.now(), name: '', type: 'string', required: false, instruction: '' },
    ]);
  };

  const updateField = (id, key, value) =>
    setSchema((prev) => prev.map((f) => (f._id === id ? { ...f, [key]: value } : f)));

  const removeField = (id) =>
    setSchema((prev) => prev.filter((f) => f._id !== id));

  const handleExtract = async () => {
    const validFields = schema.filter((f) => f.name.trim());
    if (!validFields.length) { setError('Add at least one field to the schema.'); return; }
    if (connectionId == null) { setError('Select an AI connection first.'); return; }
    setExtracting(true);
    setError('');
    try {
      const result = await extractionApi.extract(doc.id, connectionId, validFields);
      setResult(result);
    } catch (err) {
      setError(err?.response?.data?.detail ?? 'AI extraction failed. Please try again.');
    } finally {
      setExtracting(false);
    }
  };

  const overallConf = extractionResult?.overallConfidence;
  const noConnections = !loadingConns && connections.length === 0;
  const usingCustomEngine = connectionId === 0;

  return (
    <div className="di-stage">
      <div className="di-stage-header">
        <div>
          <h2 className="di-stage-title">AI Extraction</h2>
          <p className="di-stage-subtitle">Define extraction schema and run AI to extract structured fields.</p>
        </div>
        {extractionResult && (
          <div className="di-stage-actions">
            <Button variant="secondary" icon={<ArrowLeft size={14} />} onClick={onBack}>Back</Button>
            <Button
              variant="primary"
              icon={<ArrowRight size={14} />}
              onClick={() => onComplete({ ...ocrData, extractionResult })}
            >
              Create Structured Dataset
            </Button>
          </div>
        )}
      </div>

      {/* AI connection + document type pickers */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14, padding: '10px 14px', background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', flexShrink: 0 }}>AI Agent</span>
        {loadingConns ? (
          <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>Loading connections…</span>
        ) : (
          <>
            <select
              className="pc-config-select"
              style={{ maxWidth: 320 }}
              value={connectionId ?? ''}
              onChange={(e) => { setConnectionId(Number(e.target.value)); setResult(null); }}
            >
              {connections.map((c) => (
                <option key={c.id} value={c.id}>{c.name || `Connection #${c.id}`} — {c.connection_type}{c.status !== 'ACTIVE' ? ` (${c.status.toLowerCase()})` : ''}</option>
              ))}
              <option value={0}>Custom (No AI Required)</option>
            </select>
            {noConnections && (
              <>
                <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>No AI Agent connection configured — using the Custom Extraction Engine.</span>
                <Button variant="ghost" size="sm" onClick={() => navigate('/app/connections')}>Add an AI connection →</Button>
              </>
            )}
          </>
        )}
        <span style={{ width: 1, alignSelf: 'stretch', background: 'var(--border)', flexShrink: 0 }} />
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', flexShrink: 0 }}>Document type</span>
        <select
          className="pc-config-select"
          style={{ maxWidth: 220 }}
          value={documentType}
          onChange={(e) => { setDocumentType(e.target.value); setResult(null); }}
          title="Steers Auto-detect at a comprehensive, consistent field set for that document type instead of an ad-hoc guess"
        >
          <option value="">Auto (ad-hoc guess)</option>
          {docTypes.map((t) => <option key={t} value={t}>{DOC_TYPE_LABEL[t] || t}</option>)}
        </select>
      </div>

      {error && (
        <div style={{ padding: '10px 14px', marginBottom: 16, background: 'var(--color-danger-bg)', borderRadius: 8, fontSize: 13, color: 'var(--color-danger-dark)', border: '1px solid var(--color-danger-100, #FEE2E2)' }}>
          {error}
        </div>
      )}

      <div className="di-extr-layout">
        {/* ── Left: schema builder ── */}
        <div className="di-schema-card">
          <div className="di-schema-header">
            <span className="di-schema-title">Extraction Schema</span>
            <Button
              variant="ghost"
              size="sm"
              icon={<Sparkles size={13} />}
              loading={detecting}
              disabled={connectionId == null}
              onClick={handleAutoDetect}
            >
              Auto-detect
            </Button>
          </div>

          {detecting ? (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 32, gap: 10 }}>
              <LoadingSpinner />
              <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>Detecting schema from document…</span>
            </div>
          ) : (
            <>
              {/* Column headers */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 80px 40px 24px', gap: 6, padding: '8px 14px 4px', borderBottom: '1px solid var(--border)' }}>
                {['Field Name', 'Type', 'Req', ''].map((h) => (
                  <span key={h} style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>{h}</span>
                ))}
              </div>

              <div className="di-schema-fields" style={{ maxHeight: 340, overflowY: 'auto' }}>
                {schema.map((field) => (
                  <div key={field._id} className="di-field-row">
                    <input
                      className="di-field-input"
                      value={field.name}
                      onChange={(e) => updateField(field._id, 'name', e.target.value)}
                      placeholder="field_name"
                    />
                    <select
                      className="di-field-select"
                      value={field.type}
                      onChange={(e) => updateField(field._id, 'type', e.target.value)}
                    >
                      {FIELD_TYPES.map((t) => <option key={t}>{t}</option>)}
                    </select>
                    <div
                      className={`di-field-req-toggle ${field.required ? 'on' : ''}`}
                      onClick={() => updateField(field._id, 'required', !field.required)}
                      title={field.required ? 'Required' : 'Optional'}
                    />
                    <div className="di-field-del" onClick={() => removeField(field._id)} title="Remove">
                      <Trash2 size={12} />
                    </div>
                  </div>
                ))}
                {schema.length === 0 && (
                  <div style={{ padding: 20, textAlign: 'center', fontSize: 12, color: 'var(--text-muted)' }}>
                    No fields yet — click Auto-detect or Add Field.
                  </div>
                )}
              </div>

              <div style={{ padding: '10px 14px', borderTop: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between' }}>
                <Button variant="ghost" size="sm" icon={<Plus size={13} />} onClick={handleAddField}>
                  Add Field
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  icon={<Sparkles size={13} />}
                  loading={extracting}
                  onClick={handleExtract}
                  disabled={extracting || connectionId == null}
                >
                  {extractionResult ? 'Re-run Extraction' : 'Run AI Extraction'}
                </Button>
              </div>
            </>
          )}

          {/* Processing overlay */}
          {extracting && (
            <div style={{ padding: 24, textAlign: 'center' }}>
              <LoadingSpinner size="lg" />
              <p style={{ marginTop: 10, fontSize: 13, fontWeight: 500 }}>AI extracting fields…</p>
              <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4 }}>
                Using {usingCustomEngine ? 'the Custom Extraction Engine (no AI)' : connections.find((c) => c.id === connectionId)?.name || (connectionId != null ? `Connection #${connectionId}` : 'the selected AI connection')}
              </p>
            </div>
          )}
        </div>

        {/* ── Right: extraction results ── */}
        <div className="di-extr-result-card">
          <div className="di-extr-result-header">
            <span className="di-schema-title">
              {extractionResult ? `Extracted Fields (${extractionResult.fields.length})` : 'Extraction Results'}
            </span>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              {extractionResult?.model && (
                <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>model: {extractionResult.model}</span>
              )}
              {overallConf != null && (
                <StatusBadge variant={confVariant(overallConf)} dot>
                  {overallConf}% avg confidence
                </StatusBadge>
              )}
              {extractionResult && (
                <div style={{ display: 'flex', border: '1px solid var(--border)', borderRadius: 6, overflow: 'hidden' }}>
                  {[['table', '≡ Table'], ['json', '{ } JSON']].map(([mode, label]) => (
                    <button
                      key={mode}
                      onClick={() => setViewMode(mode)}
                      style={{ padding: '4px 10px', fontSize: 11, fontWeight: 500, background: viewMode === mode ? 'var(--color-primary)' : 'white', color: viewMode === mode ? 'white' : 'var(--text-muted)', border: 'none', cursor: 'pointer' }}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          {extracting ? (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: 300, flexDirection: 'column', gap: 12 }}>
              <LoadingSpinner size="lg" />
              <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>Extracting structured fields with AI…</span>
            </div>
          ) : extractionResult ? (
            <>
              {viewMode === 'table' && (
                <>
                  {/* Table header */}
                  <div style={{ display: 'grid', gridTemplateColumns: '160px 1fr 100px 60px', gap: 12, padding: '9px 16px', background: '#F8FAFC', borderBottom: '1px solid var(--border)' }}>
                    {['Field', 'Value', 'Confidence', 'Page'].map((h) => (
                      <span key={h} style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>{h}</span>
                    ))}
                  </div>
                  <div style={{ maxHeight: 420, overflowY: 'auto' }}>
                    {extractionResult.fields.map((field) => (
                      <div key={field.name} className="di-extr-row">
                        <span className="di-extr-name">{field.name}</span>
                        <span className="di-extr-val">
                          {field.type === 'table'
                            ? `${field.rows?.length ?? 0} row${(field.rows?.length ?? 0) === 1 ? '' : 's'}`
                            : (field.value === '' || field.value == null ? '—' : String(field.value))}
                        </span>
                        <div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                            <div className="di-conf-bar" style={{ flex: 1 }}>
                              <div className={`di-conf-fill ${confColor(field.confidence)}`} style={{ width: `${field.confidence}%` }} />
                            </div>
                            <span className={`di-conf-pct conf-${confColor(field.confidence)}`}>{field.confidence}%</span>
                          </div>
                        </div>
                        <span className="di-extr-page">{field.page != null ? `p${field.page}` : '—'}</span>
                      </div>
                    ))}
                  </div>
                </>
              )}
              {viewMode === 'json' && (
                <div style={{ padding: 16 }}>
                  <pre className="di-json-pre">{extractionResult.rawJson}</pre>
                </div>
              )}
            </>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: 300, gap: 10, color: 'var(--text-muted)', fontSize: 13 }}>
              <Sparkles size={32} style={{ opacity: 0.3 }} />
              <span>Configure the schema and click "Run AI Extraction"</span>
            </div>
          )}
        </div>
      </div>

      {/* Action bar */}
      {extractionResult && (
        <div className="di-action-bar">
          <div className="di-action-bar-left">
            <Button variant="secondary" icon={<ArrowLeft size={14} />} onClick={onBack}>Back to OCR</Button>
          </div>
          <div className="di-action-bar-right">
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              {extractionResult.fields.length} fields · {extractionResult.overallConfidence}% avg confidence
            </span>
            <Button
              variant="primary"
              icon={<ArrowRight size={14} />}
              onClick={() => onComplete({ ...ocrData, extractionResult })}
            >
              Create Structured Dataset
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
