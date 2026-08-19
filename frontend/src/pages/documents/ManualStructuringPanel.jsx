/**
 * ManualStructuringPanel — shown on the AI Extraction tab's empty state
 * (see DocumentDetails.jsx's ExtractionTab) when AI Extraction found
 * nothing to extract — no AI Provider connection configured, or the model
 * came back with zero usable fields. The document's real OCR text is
 * always available once OCR itself has run, so the user reads it here and
 * types in the fields by hand; saving calls the same
 * POST /documents/{id}/manual-extraction backend endpoint that stores the
 * result in the identical shape AI Extraction would have produced — the
 * Extraction and Structured Dataset tabs treat it exactly the same way
 * afterward, no special-casing.
 */
import { useEffect, useState } from 'react';
import { Plus, Trash2, Save, FileSearch, PenLine } from 'lucide-react';
import { Button, EmptyState } from '@/components/common/index.js';
import { documentApi } from '@/services/api/documentApi.js';

export function ManualStructuringPanel({ documentId, reason, onSaved }) {
  const [ocrText, setOcrText] = useState(null);
  const [ocrLoading, setOcrLoading] = useState(true);
  const [rows, setRows] = useState([{ name: '', value: '' }, { name: '', value: '' }]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    documentApi.getOcr(documentId)
      .then((ocr) => setOcrText(ocr.full_text || ''))
      .catch(() => setOcrText(null))
      .finally(() => setOcrLoading(false));
  }, [documentId]);

  const setRow = (i, key, val) => setRows((rs) => rs.map((r, idx) => (idx === i ? { ...r, [key]: val } : r)));
  const addRow = () => setRows((rs) => [...rs, { name: '', value: '' }]);
  const removeRow = (i) => setRows((rs) => rs.filter((_, idx) => idx !== i));

  const save = async () => {
    const fields = rows.map((r) => ({ name: r.name.trim(), value: r.value })).filter((f) => f.name);
    if (fields.length === 0) { setError('Add at least one field name before saving.'); return; }
    setError(null);
    setSaving(true);
    try {
      await documentApi.saveManualExtraction(documentId, fields);
      onSaved?.();
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || 'Failed to save.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ padding: 20 }}>
      <EmptyState
        icon={<PenLine size={24} />}
        title="No structured data found"
        description={reason || "AI Extraction didn't produce any fields for this document — build the structured data by hand below, using the document's real OCR text as reference."}
      />

      <div className="docd-ocr-grid" style={{ marginTop: 16 }}>
        <div className="docd-profile-card">
          <div className="card-title"><FileSearch size={14} style={{ verticalAlign: -2 }} /> OCR Text (reference)</div>
          <div className="card-subtitle">Read this to fill in the fields on the right — this is the document's real, already-extracted text.</div>
          {ocrLoading ? (
            <p className="text-muted text-sm" style={{ marginTop: 8 }}>Loading OCR text…</p>
          ) : ocrText ? (
            <pre className="docd-ocr-text">{ocrText}</pre>
          ) : (
            <p className="text-muted text-sm" style={{ marginTop: 8 }}>No OCR text available for this document either — you can still enter fields from the file preview on the Overview tab.</p>
          )}
        </div>

        <div className="docd-profile-card">
          <div className="card-title">Build Structured Data</div>
          <div className="card-subtitle">One row per field. Values you enter are saved with 100% confidence — you're the source of truth here.</div>

          <div className="manual-struct-rows" style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
            {rows.map((r, i) => (
              <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <input
                  className="doc-select" style={{ flex: '0 0 40%' }} placeholder="Field name (e.g. invoice_number)"
                  value={r.name} onChange={(e) => setRow(i, 'name', e.target.value)}
                />
                <input
                  className="doc-select" style={{ flex: 1 }} placeholder="Value"
                  value={r.value} onChange={(e) => setRow(i, 'value', e.target.value)}
                />
                <button className="doc-icon-btn" title="Remove row" onClick={() => removeRow(i)} disabled={rows.length === 1}>
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 12 }}>
            <Button variant="secondary" size="sm" icon={<Plus size={13} />} onClick={addRow}>Add Field</Button>
            <Button variant="primary" size="sm" icon={<Save size={13} />} loading={saving} onClick={save}>Save Structured Data</Button>
          </div>
          {error && <p style={{ color: 'var(--color-danger)', fontSize: 12, marginTop: 8 }}>{error}</p>}
        </div>
      </div>
    </div>
  );
}
