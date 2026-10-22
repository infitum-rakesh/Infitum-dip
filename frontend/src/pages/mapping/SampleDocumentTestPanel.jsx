/**
 * SampleDocumentTestPanel — "Test Against Sample Document" (Mapping
 * Editor UI, Phase 4 of the Document Intelligence: Dynamic JSON
 * Extraction spec). Replaces SQL Preview for a document-sourced mapping
 * profile — there's no connector to query, so instead this runs the
 * exact same engine a real apply would (mapping_execution.apply_mapping_to_dataframe)
 * against one real, already-processed sample document, entirely as a
 * dry-run: see document_mapping_service.preview_mapping_against_document's
 * own docstring — nothing is ever persisted here, and the mapping being
 * tested doesn't need to be saved or published first.
 */
import { useEffect, useState } from 'react';
import { Play, Loader2, AlertTriangle, FlaskConical } from 'lucide-react';
import { Button } from '@/components/common/index.js';
import { mappingApi } from '@/services/api/mappingApi.js';

function apiErrorMessage(err, fallback) {
  return err?.response?.data?.detail?.message || err?.response?.data?.detail || err?.message || fallback;
}

export function SampleDocumentTestPanel({ sourceDocumentType, targetFormat, mappings }) {
  const [documents, setDocuments] = useState([]);
  const [selectedId, setSelectedId] = useState('');
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    setDocuments([]); setSelectedId(''); setResult(null); setError('');
    if (!sourceDocumentType) return;
    mappingApi.getSampleDocuments(sourceDocumentType).then((r) => {
      setDocuments(r.items);
      if (r.items.length > 0) setSelectedId(String(r.items[0].id));
    }).catch(() => setDocuments([]));
  }, [sourceDocumentType]);

  const run = async () => {
    if (!selectedId || mappings.length === 0) return;
    setRunning(true); setError(''); setResult(null);
    try {
      const r = await mappingApi.previewAgainstDocument({
        document_id: Number(selectedId), source_document_type: sourceDocumentType,
        target_format: targetFormat || null, mappings,
      });
      setResult(r);
    } catch (err) {
      setError(apiErrorMessage(err, 'Could not test this mapping against the selected document.'));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="ocm-preview-panel">
      <div className="ocm-preview-title"><FlaskConical size={14} /> Test Against Sample Document</div>
      {documents.length === 0 ? (
        <p className="ocm-muted">No processed documents of type "{sourceDocumentType}" are available to test against yet.</p>
      ) : (
        <div className="ocm-action-bar" style={{ marginBottom: 10 }}>
          <select className="ocm-select" style={{ maxWidth: 320 }} value={selectedId} onChange={(e) => setSelectedId(e.target.value)}>
            {documents.map((d) => <option key={d.id} value={d.id}>{d.filename} (#{d.id})</option>)}
          </select>
          <Button variant="secondary" size="sm" icon={running ? <Loader2 size={13} className="sdc-spin" /> : <Play size={13} />} disabled={running || mappings.length === 0} onClick={run}>
            Run Test
          </Button>
        </div>
      )}
      {error && <div className="ocm-banner error">{error}</div>}
      {result && (
        <>
          <p className="ocm-muted" style={{ marginBottom: 8 }}>
            {result.row_count} row(s){result.rejected_rows?.length > 0 ? ` — ${result.rejected_rows.length} rejected` : ''}. Dry-run only — nothing was saved.
          </p>
          <div className="ocm-grid-wrap">
            <table className="ocm-grid">
              <thead><tr>{result.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>
                {result.rows.map((row, i) => (
                  <tr key={i}>{result.columns.map((c) => <td key={c} className="ocm-mono">{row[c] === null || row[c] === undefined ? <em className="ocm-muted">null</em> : String(row[c])}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
          {result.rejected_rows?.length > 0 && (
            <p className="ocm-muted" style={{ marginTop: 6 }}><AlertTriangle size={12} style={{ verticalAlign: 'text-bottom' }} /> {result.rejected_rows.length} row(s) failed the mapping and were excluded above.</p>
          )}
        </>
      )}
    </div>
  );
}
