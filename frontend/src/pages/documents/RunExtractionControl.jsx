/**
 * RunExtractionControl — document-type + AI-connection picker and "Run AI
 * Extraction" button. Used on both Document Details and Review Queue
 * Detail (the two places users reported having no way to (re-)run AI
 * Extraction from). Optional document_type (e.g. "INVOICE") seeds a
 * comprehensive canonical schema instead of an ad-hoc LLM guess — see
 * backend document_taxonomies.py. Optional connection_id lets the user
 * pick a specific AI Provider connection instead of the automatic "try
 * every configured connection in order" behavior — added so a user can
 * route around a connection that's genuinely broken (e.g. an Anthropic
 * key whose account is out of API credit) without leaving this screen;
 * see document_service.run_manual_ai_extraction()'s connection_id param.
 */
import { useEffect, useState } from 'react';
import { Sparkles } from 'lucide-react';
import { Button } from '@/components/common/index.js';
import { documentApi } from '@/services/api/documentApi.js';
import { connectionApi } from '@/services/api/connectionApi.js';

const TYPE_LABEL = { INVOICE: 'Invoice', GENERIC: 'General (wide-net)' };

export function RunExtractionControl({ documentId, onDone, size = 'sm' }) {
  const [types, setTypes] = useState([]);
  const [documentType, setDocumentType] = useState('');
  const [connections, setConnections] = useState([]);
  const [connectionId, setConnectionId] = useState('');
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    documentApi.getDocumentTypes().then(setTypes).catch(() => setTypes([]));
    connectionApi.list({ category: 'AI_PROVIDER', page_size: 50 })
      .then((r) => setConnections(r.items ?? []))
      .catch(() => setConnections([]));
  }, []);

  const run = async () => {
    setError(null);
    setRunning(true);
    try {
      await documentApi.runExtraction(documentId, documentType || null, connectionId || null);
      onDone?.();
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || 'AI Extraction failed.');
    } finally {
      setRunning(false);
    }
  };

  return (
    // Fixed widths (not .doc-select's default width:100%, meant for that
    // class's original block-stacked filter-panel context) so the two
    // selects size to something sane inline instead of each stretching to
    // fill however wide their row happens to be — callers that place this
    // control inline among single-icon buttons (Document Details) should
    // give it its own row rather than mixing it into that row; see
    // documents.css's .docd-actions comment.
    <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
      <select
        className="doc-select" style={{ width: 160, height: 30, fontSize: 12 }}
        value={documentType} onChange={(e) => setDocumentType(e.target.value)}
        title="Document type — steers AI Extraction at a comprehensive field set for that type"
      >
        <option value="">Document type: Auto</option>
        {types.map((t) => <option key={t} value={t}>Document type: {TYPE_LABEL[t] || t}</option>)}
      </select>
      <select
        className="doc-select" style={{ width: 190, height: 30, fontSize: 12 }}
        value={connectionId} onChange={(e) => setConnectionId(e.target.value)}
        title="AI connection — pick a specific one to route around a broken/exhausted connection; Auto tries every active connection in order"
      >
        <option value="">AI connection: Auto</option>
        {connections.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name || `Connection #${c.id}`} — {c.connection_type}{c.status !== 'ACTIVE' ? ` (${c.status.toLowerCase()})` : ''}
          </option>
        ))}
      </select>
      <Button variant="secondary" size={size} icon={<Sparkles size={13} />} loading={running} onClick={run}>
        Run AI Extraction
      </Button>
      {error && <span style={{ color: 'var(--color-danger)', fontSize: 12 }}>{error}</span>}
    </div>
  );
}
