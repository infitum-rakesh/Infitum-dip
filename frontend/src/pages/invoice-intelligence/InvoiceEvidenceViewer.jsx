/**
 * InvoiceEvidenceViewer — split-screen document + extracted-field evidence,
 * the genuinely new piece INV15 adds (nothing like this existed before).
 * Reuses documentApi.getFileBlob() (same call Documents/Review Queue use)
 * and GET /invoices/{id}/evidence (INV4's real bbox wiring — Custom
 * Extraction Engine's own computed boxes, or a heuristic OCR-region match
 * for the AI-extraction path) — nothing here re-implements OCR or extraction.
 *
 * Correctness note vs. the existing Review Queue viewer (rqd-*): that one
 * positions bbox overlays as raw pixel offsets against a FIXED 620px-wide
 * page container, which only looks right for tiny/synthetic boxes — a real
 * extraction's bbox is expressed in the source image's native pixel space
 * (e.g. a 1600px-wide scan), so a fixed-pixel overlay misaligns badly. This
 * viewer instead converts each bbox to a PERCENTAGE of the image's own
 * naturalWidth/naturalHeight, so the overlay tracks the rendered image at
 * any container width or zoom level.
 *
 * PDF documents render via the browser's native PDF viewer in an <iframe>
 * (same approach DocumentDetails.jsx uses) — there's no per-page rendered
 * image to overlay boxes on top of without a client-side PDF-rasterization
 * library, so bbox highlighting is disclosed as image-documents-only rather
 * than faked against the wrong coordinate space.
 *
 * INV16 — field-correction-with-evidence: each row now carries an editable
 * "Corrected Value" input + Save button right next to the evidence that
 * justifies (or fails to justify) the AI's original value. Saving calls
 * back up to InvoiceDetail.jsx, which posts through the real
 * POST /invoices/{id}/corrections route (invoiceApi.applyCorrections) —
 * the same review_queue_service.update_field()+approve() path every other
 * document type's corrections already use, which genuinely re-runs
 * materialize/validate/decide server-side. Nothing here fakes a save.
 */
import { useEffect, useRef, useState } from 'react';
import { ZoomIn, ZoomOut, Maximize2, Download, ImageOff, FileWarning, Check } from 'lucide-react';
import { StatusBadge, LoadingSpinner, Button, PermissionGate } from '@/components/common/index.js';
import { INVOICE_PERMISSIONS } from '@/utils/constants.js';
import { documentApi } from '@/services/api/documentApi.js';

function confidenceVariant(v) {
  if (v == null) return 'gray';
  if (v >= 90) return 'success';
  if (v >= 70) return 'warning';
  return 'danger';
}

export default function InvoiceEvidenceViewer({
  documentId, mimeType, fields,
  drafts = {}, onDraftChange, onSaveCorrection, savingField, canEdit = false,
}) {
  const [blobUrl, setBlobUrl] = useState(null);
  const [blobError, setBlobError] = useState(false);
  const [naturalSize, setNaturalSize] = useState(null); // { w, h }
  const [zoom, setZoom] = useState(1);
  const [selectedField, setSelectedField] = useState(fields?.find((f) => f.bbox)?.name ?? null);
  const imgRef = useRef(null);

  const isImage = (mimeType || '').startsWith('image/');
  const isPdf = mimeType === 'application/pdf';

  useEffect(() => {
    let url;
    setBlobError(false);
    setNaturalSize(null);
    documentApi.getFileBlob(documentId)
      .then((blob) => { url = URL.createObjectURL(blob); setBlobUrl(url); })
      .catch(() => setBlobError(true));
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [documentId]);

  const handleImgLoad = () => {
    if (imgRef.current) setNaturalSize({ w: imgRef.current.naturalWidth, h: imgRef.current.naturalHeight });
  };

  const selected = fields?.find((f) => f.name === selectedField);
  const evidencedCount = fields?.filter((f) => f.bbox).length ?? 0;

  return (
    <div className="invd-ev-split">
      <div className="invd-ev-doc-pane">
        <div className="invd-ev-toolbar">
          {isImage && (
            <>
              <button className="doc-icon-btn" title="Zoom In" onClick={() => setZoom((z) => Math.min(3, z + 0.25))}><ZoomIn size={14} /></button>
              <button className="doc-icon-btn" title="Zoom Out" onClick={() => setZoom((z) => Math.max(0.5, z - 0.25))}><ZoomOut size={14} /></button>
              <button className="doc-icon-btn" title="Fit" onClick={() => setZoom(1)}><Maximize2 size={14} /></button>
              <span className="invd-ev-zoom">{Math.round(zoom * 100)}%</span>
            </>
          )}
          <span className="invd-ev-evidenced-count">{evidencedCount} of {fields?.length ?? 0} fields have evidence</span>
          <button
            className="doc-icon-btn"
            style={{ marginLeft: 'auto' }}
            title="Download original"
            onClick={() => documentApi.downloadDocument(documentId)}
          >
            <Download size={14} />
          </button>
        </div>

        <div className="invd-ev-canvas">
          {blobError && (
            <div className="invd-ev-placeholder">
              <FileWarning size={28} />
              <p>The original file could not be loaded for this document.</p>
            </div>
          )}

          {!blobError && isImage && (
            <div className="invd-ev-page-wrap" style={{ transform: `scale(${zoom})` }}>
              <img ref={imgRef} src={blobUrl} alt="Invoice document" onLoad={handleImgLoad} style={{ width: '100%', display: 'block' }} />
              {naturalSize && selected?.bbox && (() => {
                const [x, y, w, h] = selected.bbox;
                return (
                  <div
                    className="invd-ev-bbox invd-ev-bbox-active"
                    style={{
                      left: `${(x / naturalSize.w) * 100}%`,
                      top: `${(y / naturalSize.h) * 100}%`,
                      width: `${(w / naturalSize.w) * 100}%`,
                      height: `${(h / naturalSize.h) * 100}%`,
                    }}
                    title={`${selected.name}: ${selected.value || '(missing)'}`}
                  />
                );
              })()}
            </div>
          )}

          {!blobError && isPdf && blobUrl && (
            <iframe title="Invoice document" src={blobUrl} className="invd-ev-pdf-frame" />
          )}

          {!blobError && !isImage && !isPdf && (
            <div className="invd-ev-placeholder">
              <ImageOff size={28} />
              <p>No inline preview available for this file type ({mimeType || 'unknown'}).</p>
            </div>
          )}

          {!blobError && !blobUrl && <div style={{ padding: 40 }}><LoadingSpinner /></div>}
        </div>

        {isPdf && (
          <p className="invd-ev-pdf-note">
            Bounding-box highlighting is only available for image documents in this phase — this
            PDF renders via the browser's native viewer. Field coordinates are still shown in the
            list on the right.
          </p>
        )}
      </div>

      <div className="invd-ev-field-pane">
        <div className="invd-card-title" style={{ marginBottom: 12 }}>Extracted Fields</div>
        <div className="invd-ev-field-list">
          {(fields ?? []).map((f) => {
            const draft = drafts[f.name];
            const dirty = draft != null && draft !== (f.value ?? '');
            const saving = savingField === f.name;
            return (
              <div
                key={f.name}
                className={`invd-ev-field-row ${selectedField === f.name ? 'active' : ''} ${!f.bbox ? 'no-bbox' : ''}`}
                onClick={() => setSelectedField(f.name)}
              >
                <div className="invd-ev-field-hdr">
                  <span className="invd-ev-field-name">{f.name}</span>
                  <StatusBadge variant={f.missing ? 'gray' : confidenceVariant(f.confidence)} dot>
                    {f.missing ? 'missing' : f.confidence != null ? `${f.confidence}%` : '—'}
                  </StatusBadge>
                </div>
                <div className="invd-ev-field-value">{f.value || <em className="text-muted">missing</em>}</div>
                <div className="invd-ev-field-meta">
                  <span>Page {f.page ?? 1}</span>
                  <span>&middot;</span>
                  <span>{f.bbox ? `[${f.bbox.join(', ')}]` : 'no evidence bbox'}</span>
                </div>
                {canEdit && (
                  <PermissionGate {...INVOICE_PERMISSIONS.INVOICE_EDIT} fallback={null}>
                    <div className="invd-ev-correct-row" onClick={(e) => e.stopPropagation()}>
                      <input
                        className="invd-ev-correct-input"
                        placeholder="Corrected value…"
                        value={draft ?? ''}
                        onChange={(e) => onDraftChange?.(f.name, e.target.value)}
                      />
                      <Button
                        variant="secondary" size="sm" loading={saving} disabled={!dirty || saving}
                        icon={<Check size={12} />}
                        onClick={() => onSaveCorrection?.(f.name)}
                      >
                        Save
                      </Button>
                    </div>
                  </PermissionGate>
                )}
              </div>
            );
          })}
          {!fields?.length && <p className="invd-empty-note">No evidence has been captured for this invoice yet.</p>}
        </div>
      </div>
    </div>
  );
}
