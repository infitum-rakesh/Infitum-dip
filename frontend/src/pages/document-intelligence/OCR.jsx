/**
 * Stage 2 — OCR
 * Side-by-side: document preview with bounding box overlays | extracted text.
 * Runs OCR on mount, lets user review, then advances to AI Extraction.
 *
 * Real backend (see backend/app/ocr/engine.py — Tesseract): runs the
 * Documents module's process pipeline (POST /documents/{id}/process),
 * which performs real OCR on the actual uploaded file, then fetches the
 * real result (GET /documents/{id}/ocr). Re-runs whenever a different
 * document is selected — the effect is keyed on doc.id (previously it
 * ran once on mount with an empty dependency array, which is why
 * selecting a new image kept showing the first image's stale result —
 * see PR discussion). A request-id guard discards a late-resolving
 * response for a document the user has since navigated away from.
 */
import { useState, useEffect, useRef } from 'react';
import { ArrowLeft, ArrowRight, FileText, ZoomIn, ZoomOut, RotateCw } from 'lucide-react';
import { Button, LoadingSpinner, StatusBadge } from '@/components/common/index.js';
import { documentApi } from '@/services/api/documentApi.js';
import './document-intelligence.css';

function confColor(pct) { return pct >= 90 ? 'high' : pct >= 75 ? 'med' : 'low'; }
function confVariant(pct) { return pct >= 90 ? 'success' : pct >= 75 ? 'warning' : 'danger'; }

// Real backend Document.status values while a background process_document()
// run is in flight — see documentApi.pollUntilDone().
const STATUS_LABEL = {
  SCANNING: 'Scanning document…',
  OCR_PROCESSING: 'Running OCR Engine…',
  AI_EXTRACTION: 'Running AI Extraction…',
};

/** Real backend shape ({full_text, confidence, regions:[{page,text,bbox,confidence}], pages:[{page,width,height}]})
 * adapted into the shape this component's UI already renders (percentage-based bounding boxes, per-page grouping). */
function adaptOcrResponse(ocr) {
  const dimsByPage = Object.fromEntries((ocr.pages || []).map((p) => [p.page, p]));
  const regionsByPage = {};
  for (const r of ocr.regions || []) {
    (regionsByPage[r.page] ??= []).push(r);
  }
  const pageNums = Object.keys(dimsByPage).map(Number).sort((a, b) => a - b);
  const pages = (pageNums.length ? pageNums : [1]).map((pageNum) => {
    const dims = dimsByPage[pageNum] || { width: 0, height: 0 };
    const regions = regionsByPage[pageNum] || [];
    return {
      pageNum,
      width: dims.width,
      height: dims.height,
      confidence: ocr.confidence ?? 0,
      text: regions.map((r) => r.text).join('\n'),
      boundingBoxes: regions.map((r, i) => {
        const [x, y, w, h] = r.bbox;
        const safe = (v, total) => (total > 0 ? (v / total) * 100 : 0);
        return {
          id: `p${pageNum}-r${i}`,
          label: r.text.length > 40 ? `${r.text.slice(0, 40)}…` : r.text,
          top: safe(y, dims.height), left: safe(x, dims.width),
          width: safe(w, dims.width), height: safe(h, dims.height),
        };
      }),
    };
  });
  const wordCount = ocr.full_text ? ocr.full_text.split(/\s+/).filter(Boolean).length : 0;
  return {
    id: `ocr_${ocr.document_id}`,
    documentId: ocr.document_id,
    confidence: ocr.confidence ?? 0,
    pageCount: pages.length,
    wordCount,
    charCount: ocr.full_text?.length ?? 0,
    tableCount: 0, // real table detection isn't implemented — 0 is "not detected", never a fabricated count
    pages,
    fullText: ocr.full_text,
    // "tesseract" (default) or "ai_vision" — set when Tesseract genuinely
    // found no readable text and a vision-capable AI connection
    // transcribed the actual image instead (see backend document_service.py
    // _run_vision_ocr_fallback()). Surfaced in the UI so it's never
    // presented as if it were Tesseract's own (per-word-confidence) output.
    source: ocr.source || 'tesseract',
  };
}

export function OCR({ scanData, onComplete, onBack }) {
  const { document: doc, scanResult } = scanData;

  const [processing, setProcessing]   = useState(false);
  const [processingStatus, setProcessingStatus] = useState(''); // live backend status while polling — see runOCR()
  const [ocrResult, setOcrResult]     = useState(null);
  const [activeBbox, setActiveBbox]   = useState(null);
  const [zoom, setZoom]               = useState(1);
  const [error, setError]             = useState('');
  const textPanelRef = useRef(null);
  const requestIdRef = useRef(0);

  // Re-run OCR whenever the document changes — keyed on doc.id, not an
  // empty array, so selecting a new image genuinely re-processes it
  // instead of showing whatever the previous image's result was.
  useEffect(() => {
    setOcrResult(null);
    runOCR();
  }, [doc.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const runOCR = async () => {
    const myRequestId = ++requestIdRef.current;
    setProcessing(true);
    setError('');
    try {
      // process_document() now runs as a real backend background task
      // (real Tesseract OCR + up to two live AI Extraction LLM calls) —
      // processDocument() only starts it and returns immediately, so we
      // poll for real completion instead of awaiting one long request
      // (that used to legitimately take well over a minute and blow past
      // the 30s client timeout — "No response from server").
      await documentApi.processDocument(doc.id);
      const finalDoc = await documentApi.pollUntilDone(doc.id, {
        onTick: (d) => { if (myRequestId === requestIdRef.current) setProcessingStatus(d.status); },
      });
      if (myRequestId !== requestIdRef.current) return; // a newer document was selected while this was in flight
      if (finalDoc.status === 'FAILED') {
        throw new Error(finalDoc.error_message || 'Processing failed.');
      }
      const raw = await documentApi.getOcr(doc.id);
      if (myRequestId !== requestIdRef.current) return;
      setOcrResult(adaptOcrResponse(raw));
    } catch (err) {
      if (myRequestId !== requestIdRef.current) return;
      setError(err?.response?.data?.detail || err?.message || 'OCR failed. Please try again.');
    } finally {
      if (myRequestId === requestIdRef.current) { setProcessing(false); setProcessingStatus(''); }
    }
  };

  const handleBboxClick = (bbox) => {
    setActiveBbox(bbox.id === activeBbox ? null : bbox.id);
    // Scroll text panel to top when selecting
    textPanelRef.current?.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const page = ocrResult?.pages?.[0];
  const isImage = doc?.type?.startsWith('image/');

  return (
    <div className="di-stage">
      <div className="di-stage-header">
        <div>
          <h2 className="di-stage-title">OCR Engine</h2>
          <p className="di-stage-subtitle">
            Review extracted text and bounding boxes.{' '}
            {doc?.filename && <span style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{doc.filename}</span>}
          </p>
        </div>
        {ocrResult && (
          <div className="di-stage-actions">
            <Button variant="secondary" icon={<ArrowLeft size={14} />} onClick={onBack}>Back</Button>
            <Button
              variant="primary"
              icon={<ArrowRight size={14} />}
              onClick={() => onComplete({ ...scanData, ocrResult })}
            >
              Proceed to AI Extraction
            </Button>
          </div>
        )}
      </div>

      {error && (
        <div style={{ padding: '10px 14px', marginBottom: 16, background: 'var(--color-danger-bg)', borderRadius: 8, fontSize: 13, color: 'var(--color-danger-dark)', border: '1px solid var(--color-danger-100, #FEE2E2)' }}>
          {error}
          <Button variant="ghost" size="sm" onClick={runOCR} style={{ marginLeft: 8 }}>Retry</Button>
        </div>
      )}

      {/* Stats bar */}
      {ocrResult && (
        <div className="di-ocr-stats">
          {[
            { val: ocrResult.pageCount, label: 'Pages' },
            { val: ocrResult.wordCount.toLocaleString(), label: 'Words' },
            { val: ocrResult.charCount.toLocaleString(), label: 'Characters' },
            { val: ocrResult.tableCount, label: 'Tables' },
            // A vision-model transcription has no per-word confidence score the way Tesseract
            // does (see backend's _run_vision_ocr_fallback docstring) — "N/A" is honest, "0%"
            // would misleadingly imply the model was measured and found unreliable.
            ocrResult.source === 'ai_vision'
              ? { val: 'N/A', label: 'Confidence' }
              : { val: `${ocrResult.confidence}%`, label: 'Confidence', color: confColor(ocrResult.confidence) },
          ].map((s) => (
            <div key={s.label} className="di-ocr-stat">
              <div className={`di-ocr-stat-val ${s.color ? `conf-${s.color}` : ''}`}>{s.val}</div>
              <div className="di-ocr-stat-label">{s.label}</div>
            </div>
          ))}
          {ocrResult.source === 'ai_vision' ? (
            <StatusBadge variant="primary" dot style={{ marginLeft: 'auto', alignSelf: 'center' }} title="Tesseract found no readable text on this page — a vision-capable AI connection transcribed the image directly instead.">
              AI vision fallback
            </StatusBadge>
          ) : (
            <StatusBadge variant={confVariant(ocrResult?.confidence ?? 0)} dot style={{ marginLeft: 'auto', alignSelf: 'center' }}>
              {confColor(ocrResult.confidence) === 'high' ? 'Excellent accuracy' : 'Review recommended'}
            </StatusBadge>
          )}
        </div>
      )}

      {/* Two-panel layout */}
      <div className="di-two-panel">
        {/* ── Left: document viewer ── */}
        <div className="di-panel">
          <div className="di-panel-header">
            <span className="di-panel-title">Document Preview — Bounding Boxes</span>
            <div style={{ display: 'flex', gap: 4 }}>
              <Button variant="ghost" size="sm" onClick={() => setZoom((z) => Math.max(0.5, z - 0.2))}><ZoomOut size={13} /></Button>
              <span style={{ fontSize: 12, color: 'var(--text-muted)', alignSelf: 'center', minWidth: 36, textAlign: 'center' }}>{Math.round(zoom * 100)}%</span>
              <Button variant="ghost" size="sm" onClick={() => setZoom((z) => Math.min(2, z + 0.2))}><ZoomIn size={13} /></Button>
            </div>
          </div>
          <div className="di-panel-body" style={{ position: 'relative', padding: 0 }}>
            {processing ? (
              <div className="di-processing-overlay" style={{ position: 'relative', minHeight: 480, borderRadius: 0 }}>
                <LoadingSpinner size="lg" />
                <p className="di-processing-text">{STATUS_LABEL[processingStatus] || 'Starting…'}</p>
                <p className="di-processing-sub">
                  {processingStatus === 'AI_EXTRACTION'
                    ? 'This can take a little longer — a real AI model call, not instant.'
                    : 'Detecting text, tables and regions'}
                </p>
              </div>
            ) : page ? (
              <div style={{ transform: `scale(${zoom})`, transformOrigin: 'top left', transition: 'transform 0.2s', padding: 16, position: 'relative' }}>
                {/* Preview */}
                {isImage && doc.previewUrl ? (
                  <div style={{ position: 'relative', display: 'inline-block' }}>
                    <img src={doc.previewUrl} alt="OCR preview" style={{ maxWidth: '100%', display: 'block', borderRadius: 4 }} />
                    {page.boundingBoxes.map((bb) => (
                      <div
                        key={bb.id}
                        className={`di-bbox ${activeBbox === bb.id ? 'active' : ''}`}
                        style={{ top: `${bb.top}%`, left: `${bb.left}%`, width: `${bb.width}%`, height: `${bb.height}%` }}
                        onClick={() => handleBboxClick(bb)}
                        title={bb.label}
                      >
                        <span className="di-bbox-label">{bb.label}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  /* PDF mock with overlaid bounding boxes */
                  <div style={{ position: 'relative', width: '100%' }}>
                    <div className="di-doc-preview-mock" style={{ position: 'relative' }}>
                      <div className="di-mock-line h" /><div style={{ height: 10 }} />
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 12 }}>
                        <div><div className="di-mock-line" /><div className="di-mock-line m" /><div className="di-mock-line s" /></div>
                        <div><div className="di-mock-line s" /><div className="di-mock-line m" /><div className="di-mock-line s" /></div>
                      </div>
                      <div className="di-mock-line xs" style={{ marginBottom: 12 }} />
                      <div><div className="di-mock-line m" /><div className="di-mock-line" /><div className="di-mock-line s" /></div>
                      <div className="di-mock-table" style={{ marginTop: 16 }}>
                        {[0,1,2,3].map((r) => (
                          <div key={r} className="di-mock-row">
                            {[0,1,2,3].map((c) => <div key={c} className={`di-mock-cell ${r===0?'hd':''}`} />)}
                          </div>
                        ))}
                      </div>
                      {/* Simulated bounding boxes */}
                      {page.boundingBoxes.map((bb) => (
                        <div
                          key={bb.id}
                          className={`di-bbox ${activeBbox === bb.id ? 'active' : ''}`}
                          style={{ top: `${bb.top}%`, left: `${bb.left}%`, width: `${bb.width}%`, height: `${bb.height}%` }}
                          onClick={() => handleBboxClick(bb)}
                          title={bb.label}
                        >
                          <span className="di-bbox-label">{bb.label}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : null}
          </div>
        </div>

        {/* ── Right: extracted text ── */}
        <div className="di-panel">
          <div className="di-panel-header">
            <span className="di-panel-title">Extracted Text</span>
            {activeBbox && (
              <StatusBadge variant="primary" size="sm">
                Selected: {page?.boundingBoxes.find((b) => b.id === activeBbox)?.label}
              </StatusBadge>
            )}
          </div>
          <div className="di-panel-body" ref={textPanelRef}>
            {processing ? (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: 200 }}>
                <LoadingSpinner />
              </div>
            ) : page ? (
              <>
                {/* Confidence bar */}
                <div style={{ marginBottom: 16 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                    <span style={{ fontSize: 12, color: 'var(--text-muted)', fontWeight: 500 }}>OCR Confidence</span>
                    <span className={`di-conf-pct conf-${confColor(page.confidence)}`}>{page.confidence}%</span>
                  </div>
                  <div className="di-conf-bar">
                    <div className={`di-conf-fill ${confColor(page.confidence)}`} style={{ width: `${page.confidence}%` }} />
                  </div>
                </div>

                {/* If a region is selected, highlight the label */}
                {activeBbox && (
                  <div style={{ marginBottom: 12, padding: '8px 10px', background: 'var(--color-primary-light)', borderRadius: 8, fontSize: 12, fontWeight: 600, color: 'var(--color-primary)' }}>
                    📍 Highlighted region: {page.boundingBoxes.find((b) => b.id === activeBbox)?.label}
                    <span style={{ float: 'right', cursor: 'pointer', fontWeight: 400 }} onClick={() => setActiveBbox(null)}>✕</span>
                  </div>
                )}

                <pre className="di-ocr-text-area">{page.text}</pre>
              </>
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: 200, color: 'var(--text-muted)', fontSize: 13 }}>
                OCR results will appear here
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Action bar */}
      {ocrResult && (
        <div className="di-action-bar">
          <div className="di-action-bar-left">
            <Button variant="secondary" icon={<ArrowLeft size={14} />} onClick={onBack}>Back to Scanner</Button>
            <Button variant="ghost" onClick={runOCR} icon={<RotateCw size={14} />}>Re-run OCR</Button>
          </div>
          <div className="di-action-bar-right">
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              {ocrResult.wordCount} words · {ocrResult.confidence}% confidence
            </span>
            <Button
              variant="primary"
              icon={<ArrowRight size={14} />}
              onClick={() => onComplete({ ...scanData, ocrResult })}
            >
              Proceed to AI Extraction
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
