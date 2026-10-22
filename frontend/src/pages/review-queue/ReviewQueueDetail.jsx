/**
 * ReviewQueueDetail — /app/doc-intel/review/:reviewId
 * Split-screen human review: original document (left) + extracted data (right).
 * Reuses Documents/Document Intelligence APIs for the file and OCR text —
 * never re-implements OCR or AI Extraction.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft, ZoomIn, ZoomOut, RotateCw, Maximize2, ChevronLeft, ChevronRight,
  Check, X, AlertTriangle, ArrowUpCircle, UserPlus, FileSearch, ScrollText,
} from 'lucide-react';
import { Button, StatusBadge, DataTable, EmptyState, Modal, PermissionGate, FullPageSpinner } from '@/components/common/index.js';
import { REVIEW_QUEUE_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { languageName } from '@/utils/languageNames.js';
import { reviewQueueApi } from '@/services/api/reviewQueueApi.js';
import { documentApi } from '@/services/api/documentApi.js';
import { RunExtractionControl } from '../documents/RunExtractionControl.jsx';
import './review-queue.css';

const PRIORITY_VARIANT = { CRITICAL: 'danger', HIGH: 'warning', MEDIUM: 'info', LOW: 'gray' };
const STATUS_VARIANT = { PENDING: 'gray', IN_PROGRESS: 'info', APPROVED: 'success', REJECTED: 'danger', ESCALATED: 'warning', CANCELLED: 'gray' };
const STATUS_LABEL = { PENDING: 'Pending', IN_PROGRESS: 'In Progress', APPROVED: 'Approved', REJECTED: 'Rejected', ESCALATED: 'Escalated', CANCELLED: 'Cancelled' };

function confidenceVariant(v) {
  if (v == null) return 'gray';
  if (v >= 90) return 'success';
  if (v >= 80) return 'warning';
  return 'danger';
}

export default function ReviewQueueDetail() {
  const { reviewId } = useParams();
  const navigate = useNavigate();

  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const [selectedField, setSelectedField] = useState(null);
  const [drafts, setDrafts] = useState({}); // fieldName -> draft corrected value (unsaved)

  const [rejectModal, setRejectModal] = useState(false);
  const [rejectReason, setRejectReason] = useState('');
  const [escalateModal, setEscalateModal] = useState(false);
  const [escalateReason, setEscalateReason] = useState('');
  const [escalateTo, setEscalateTo] = useState('');
  const [assignModal, setAssignModal] = useState(false);
  const [assignTo, setAssignTo] = useState('');
  const [assignees, setAssignees] = useState([]);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    reviewQueueApi.get(reviewId)
      .then((d) => {
        setDetail(d);
        setDrafts({ ...d.corrections });
        if (!selectedField && d.field_name) setSelectedField(d.field_name);
      })
      .catch(() => setError('Review item not found or failed to load.'))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reviewId]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { reviewQueueApi.getFacets().then((f) => setAssignees(f.assignees)).catch(() => {}); }, []);

  const runAssign = async (userId) => {
    setBusy(true);
    try { await reviewQueueApi.assign(reviewId, userId || null); load(); } finally { setBusy(false); setAssignModal(false); }
  };
  const saveCorrection = async (fieldName) => {
    const value = drafts[fieldName];
    if (value == null) return;
    setBusy(true);
    try { await reviewQueueApi.updateField(reviewId, fieldName, value, 'Corrected via Review Queue'); load(); } finally { setBusy(false); }
  };
  const runApprove = async () => {
    setBusy(true);
    try { await reviewQueueApi.approve(reviewId); load(); } finally { setBusy(false); }
  };
  const submitReject = async () => {
    if (!rejectReason.trim()) return;
    setBusy(true);
    try { await reviewQueueApi.reject(reviewId, rejectReason.trim()); setRejectModal(false); setRejectReason(''); load(); } finally { setBusy(false); }
  };
  const submitEscalate = async () => {
    setBusy(true);
    try { await reviewQueueApi.escalate(reviewId, escalateReason || null, escalateTo || null); setEscalateModal(false); setEscalateReason(''); setEscalateTo(''); load(); } finally { setBusy(false); }
  };

  if (loading) return <FullPageSpinner />;
  if (error || !detail) {
    return (
      <div className="rq-page">
        <EmptyState icon={<AlertTriangle size={28} />} title="Review item not found" description={error ?? `No review item matches "${reviewId}".`}
          action={{ label: 'Back to Review Queue', onClick: () => navigate('/app/doc-intel/review') }} />
      </div>
    );
  }

  const isOpen = ['PENDING', 'IN_PROGRESS', 'ESCALATED'].includes(detail.status);
  const selectedFieldData = detail.fields.find((f) => f.name === selectedField);

  return (
    <PermissionGate {...REVIEW_QUEUE_PERMISSIONS.REVIEW_QUEUE_VIEW} showReason>
      <div className="rq-page">
        {/* Header */}
        <div className="rqd-header">
          <button className="rqd-back-btn" onClick={() => navigate('/app/doc-intel/review')}>
            <ArrowLeft size={14} /> Review Queue
          </button>
          <div className="rqd-header-row">
            <div>
              <div className="rqd-title-row">
                <h1 className="rqd-title">{detail.document_name}</h1>
                <StatusBadge variant={PRIORITY_VARIANT[detail.priority] ?? 'gray'} dot>{detail.priority}</StatusBadge>
                <StatusBadge variant={STATUS_VARIANT[detail.status] ?? 'gray'} dot>{STATUS_LABEL[detail.status] ?? detail.status}</StatusBadge>
              </div>
              <p className="rqd-subtitle">{detail.document_type} &middot; {detail.issue} &middot; {detail.review_type.replace('_', ' ')}</p>
            </div>

            <div className="rqd-actions-col">
              <div className="rqd-actions">
                <PermissionGate {...REVIEW_QUEUE_PERMISSIONS.REVIEW_QUEUE_ASSIGN} fallback={null}>
                  <Button variant="secondary" size="sm" icon={<UserPlus size={13} />} onClick={() => setAssignModal(true)}>
                    {detail.assigned_to_name ? `Assigned: ${detail.assigned_to_name}` : 'Assign'}
                  </Button>
                </PermissionGate>
                {isOpen && (
                  <>
                    <PermissionGate {...REVIEW_QUEUE_PERMISSIONS.REVIEW_QUEUE_APPROVE} fallback={null}>
                      <Button variant="primary" size="sm" icon={<Check size={13} />} loading={busy} onClick={runApprove}>Accept</Button>
                    </PermissionGate>
                    <PermissionGate {...REVIEW_QUEUE_PERMISSIONS.REVIEW_QUEUE_REJECT} fallback={null}>
                      <Button variant="danger" size="sm" icon={<X size={13} />} onClick={() => setRejectModal(true)}>Reject</Button>
                    </PermissionGate>
                    <PermissionGate {...REVIEW_QUEUE_PERMISSIONS.REVIEW_QUEUE_ESCALATE} fallback={null}>
                      <Button variant="secondary" size="sm" icon={<ArrowUpCircle size={13} />} onClick={() => setEscalateModal(true)}>Escalate</Button>
                    </PermissionGate>
                  </>
                )}
              </div>

              {/* Own row — two dropdowns + a button, not a single-icon
                  button like its neighbors above (see documents.css's
                  .docd-actions-col comment; same pattern reused here). */}
              {detail.document_id && (
                <div className="rqd-extract-row">
                  <RunExtractionControl documentId={detail.document_id} onDone={load} />
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Split screen */}
        <div className="rqd-split">
          <DocumentPane documentId={detail.document_id} previewAvailable={detail.document_preview_available} fileType={detail.document_file_type} selectedField={selectedFieldData} />
          <FieldReviewPane
            detail={detail}
            drafts={drafts}
            setDrafts={setDrafts}
            selectedField={selectedField}
            setSelectedField={setSelectedField}
            onSaveCorrection={saveCorrection}
            busy={busy}
            isOpen={isOpen}
          />
        </div>

        {/* Audit trail */}
        <div className="rqd-audit">
          <div className="card-title" style={{ margin: '20px 0 10px', display: 'flex', alignItems: 'center', gap: 6 }}>
            <ScrollText size={15} /> Audit Trail
          </div>
          <DataTable
            columns={[
              { key: 'user_name', header: 'User', render: (v) => v ?? 'System' },
              { key: 'action', header: 'Action' },
              { key: 'old_value', header: 'Old Value', render: (v) => v ?? '—' },
              { key: 'new_value', header: 'New Value', render: (v) => v ?? '—' },
              { key: 'reason', header: 'Reason', render: (v) => v ?? '—' },
              { key: 'created_at', header: 'Timestamp', render: (v) => formatDateTime(v) },
            ]}
            data={detail.audit_log}
            emptyState={<EmptyState icon={<ScrollText size={24} />} title="No audit history" description="No actions have been logged for this item yet." />}
          />
        </div>
      </div>

      {/* Reject modal */}
      <Modal open={rejectModal} onClose={() => setRejectModal(false)} title="Reject Review Item" size="sm"
        footer={<><Button variant="secondary" onClick={() => setRejectModal(false)}>Cancel</Button><Button variant="danger" loading={busy} disabled={!rejectReason.trim()} onClick={submitReject}>Reject</Button></>}>
        <label className="rq-filter-label" style={{ display: 'block', marginBottom: 6 }}>Rejection reason (required)</label>
        <textarea className="rq-select" style={{ width: '100%', minHeight: 80, resize: 'vertical' }}
          value={rejectReason} onChange={(e) => setRejectReason(e.target.value)}
          placeholder="Explain why this needs to be rejected — the document goes back for reprocessing…" autoFocus />
      </Modal>

      {/* Escalate modal */}
      <Modal open={escalateModal} onClose={() => setEscalateModal(false)} title="Escalate to Senior Reviewer" size="sm"
        footer={<><Button variant="secondary" onClick={() => setEscalateModal(false)}>Cancel</Button><Button variant="primary" loading={busy} onClick={submitEscalate}>Escalate</Button></>}>
        <label className="rq-filter-label" style={{ display: 'block', marginBottom: 6 }}>Escalate to (optional)</label>
        <select className="rq-select" style={{ width: '100%', marginBottom: 12 }} value={escalateTo} onChange={(e) => setEscalateTo(e.target.value)}>
          <option value="">Leave assignment as-is</option>
          {assignees.map((u) => <option key={u.id} value={u.id}>{u.full_name}</option>)}
        </select>
        <label className="rq-filter-label" style={{ display: 'block', marginBottom: 6 }}>Reason (optional)</label>
        <textarea className="rq-select" style={{ width: '100%', minHeight: 70, resize: 'vertical' }}
          value={escalateReason} onChange={(e) => setEscalateReason(e.target.value)} placeholder="Why does this need senior review?" />
      </Modal>

      {/* Assign modal */}
      <Modal open={assignModal} onClose={() => setAssignModal(false)} title="Assign Reviewer" size="sm"
        footer={<><Button variant="secondary" onClick={() => setAssignModal(false)}>Cancel</Button><Button variant="primary" loading={busy} onClick={() => runAssign(assignTo)}>Assign</Button></>}>
        <label className="rq-filter-label" style={{ display: 'block', marginBottom: 6 }}>Reviewer</label>
        <select className="rq-select" style={{ width: '100%' }} value={assignTo} onChange={(e) => setAssignTo(e.target.value)}>
          <option value="">Unassign</option>
          {assignees.map((u) => <option key={u.id} value={u.id}>{u.full_name}</option>)}
        </select>
      </Modal>
    </PermissionGate>
  );
}

// ── Left: document viewer ────────────────────────────────────────

function DocumentPane({ documentId, previewAvailable, fileType, selectedField }) {
  const [blobUrl, setBlobUrl] = useState(null);
  const [zoom, setZoom] = useState(1);
  const [rotation, setRotation] = useState(0);
  const [showOcr, setShowOcr] = useState(false);
  const [ocr, setOcr] = useState(null);

  useEffect(() => {
    let url;
    if (previewAvailable) {
      documentApi.getFileBlob(documentId).then((blob) => { url = URL.createObjectURL(blob); setBlobUrl(url); }).catch(() => setBlobUrl(null));
    }
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [documentId, previewAvailable]);

  const toggleOcr = () => {
    if (!showOcr && !ocr) documentApi.getOcr(documentId).then(setOcr).catch(() => {});
    setShowOcr((s) => !s);
  };

  return (
    <div className="rqd-doc-pane">
      <div className="rqd-doc-toolbar">
        <button className="doc-icon-btn" title="Zoom In" onClick={() => setZoom((z) => Math.min(3, z + 0.25))}><ZoomIn size={14} /></button>
        <button className="doc-icon-btn" title="Zoom Out" onClick={() => setZoom((z) => Math.max(0.25, z - 0.25))}><ZoomOut size={14} /></button>
        <button className="doc-icon-btn" title="Rotate" onClick={() => setRotation((r) => (r + 90) % 360)}><RotateCw size={14} /></button>
        <button className="doc-icon-btn" title="Fit Page" onClick={() => { setZoom(1); setRotation(0); }}><Maximize2 size={14} /></button>
        <span className="docd-viewer-zoom">{Math.round(zoom * 100)}%</span>
        <div className="docd-viewer-page-nav">
          <button className="doc-icon-btn" disabled><ChevronLeft size={14} /></button>
          <span>Page 1</span>
          <button className="doc-icon-btn" disabled><ChevronRight size={14} /></button>
        </div>
        <button className={`rq-pill ${showOcr ? 'active' : ''}`} style={{ marginLeft: 'auto' }} onClick={toggleOcr}>
          <FileSearch size={12} /> OCR Overlay
        </button>
      </div>

      <div className="rqd-doc-canvas">
        <div className="rqd-page" style={{ transform: `scale(${zoom}) rotate(${rotation}deg)` }}>
          {blobUrl ? (
            fileType === 'PDF' ? <iframe title="Document" src={blobUrl} /> : <img src={blobUrl} alt="Document" />
          ) : (
            <div className="rqd-page-placeholder" />
          )}

          {/* Bounding-box overlay */}
          {selectedField?.bbox && (
            <div
              className="rqd-bbox rqd-bbox-active"
              style={{ left: selectedField.bbox[0], top: selectedField.bbox[1], width: selectedField.bbox[2], height: selectedField.bbox[3] }}
              title={`${selectedField.name}: ${selectedField.value || '(missing)'}`}
            />
          )}
        </div>
      </div>

      {showOcr && (
        <div className="rqd-ocr-panel">
          <div className="card-title">OCR Text {ocr?.confidence != null && <span className="text-muted text-sm">({ocr.confidence}% confidence)</span>}</div>
          <LanguageInfo language={ocr?.language} qualityFlags={ocr?.quality_flags} />
          <pre className="docd-ocr-text">{ocr?.full_text ?? 'Loading OCR text…'}</pre>
        </div>
      )}
    </div>
  );
}

// Multilingual Document Processing, Phase 9 — real detected-language +
// OCR-quality display, straight from doc.ocr_result (Phases 4/6 already
// populate "language"/"quality_flags" there; GET /documents/{id}/ocr
// already returns them verbatim, so this is pure display, no new API).
function LanguageInfo({ language, qualityFlags }) {
  if (!language && !(qualityFlags && qualityFlags.length)) return null;
  return (
    <div className="rqd-lang-info">
      {language && (
        <div className="rqd-lang-row">
          <StatusBadge variant="info" dot>{languageName(language.primary_language)}</StatusBadge>
          {language.language_detection_confidence != null && (
            <span className="text-muted text-sm">{language.language_detection_confidence.toFixed(0)}% confidence · {language.language_detection_method}</span>
          )}
          {language.is_multilingual && (
            <span className="rqd-lang-pages">
              {(language.pages || []).map((p) => (
                <StatusBadge key={p.page_number} variant="gray" size="sm">
                  p{p.page_number}: {p.languages.map((c) => `${languageName(c.language)} ${c.confidence.toFixed(0)}%`).join(' + ')}
                </StatusBadge>
              ))}
            </span>
          )}
        </div>
      )}
      {qualityFlags && qualityFlags.length > 0 && (
        <div className="rqd-lang-row">
          {qualityFlags.map((f) => (
            <span key={f.code} title={f.message}>
              <StatusBadge variant="warning">{f.code.replace(/_/g, ' ')}</StatusBadge>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Right: field-level review ────────────────────────────────────

function FieldReviewPane({ detail, drafts, setDrafts, selectedField, setSelectedField, onSaveCorrection, busy, isOpen }) {
  const primary = detail.fields.find((f) => f.name === detail.field_name);

  return (
    <div className="rqd-field-pane">
      {primary && (
        <div className="rqd-primary-card">
          <div className="rqd-primary-hdr">
            <span className="card-title">{primary.name}</span>
            <StatusBadge variant={confidenceVariant(primary.confidence)} dot>{primary.confidence != null ? `${primary.confidence}%` : '—'}</StatusBadge>
          </div>
          <div className="rqd-primary-row">
            <div>
              <span className="docd-meta-label">AI Value</span>
              <div className="rqd-primary-value">{primary.value || <em className="text-muted">missing</em>}</div>
            </div>
            <div>
              <span className="docd-meta-label">Corrected Value</span>
              <input
                className="rq-select"
                value={drafts[primary.name] ?? primary.suggested_correction ?? ''}
                onChange={(e) => setDrafts((d) => ({ ...d, [primary.name]: e.target.value }))}
                placeholder="Enter corrected value…"
                disabled={!isOpen}
              />
            </div>
          </div>
          {isOpen && (
            <PermissionGate {...REVIEW_QUEUE_PERMISSIONS.REVIEW_QUEUE_EDIT} fallback={null}>
              <Button variant="secondary" size="sm" loading={busy} onClick={() => onSaveCorrection(primary.name)} style={{ marginTop: 10 }}>
                Save Correction
              </Button>
            </PermissionGate>
          )}
        </div>
      )}

      <div className="card-title" style={{ margin: '18px 0 10px' }}>All Extracted Fields</div>
      <div className="rqd-field-list">
        {detail.fields.map((f) => (
          <div
            key={f.name}
            className={`rqd-field-row ${selectedField === f.name ? 'active' : ''}`}
            onClick={() => setSelectedField(f.name)}
          >
            <div className="rqd-field-row-hdr">
              <span className="rqd-field-name">{f.name}</span>
              {f.is_generic && <StatusBadge variant="info" size="sm">Generic Extraction</StatusBadge>}
              <StatusBadge variant={confidenceVariant(f.confidence)} dot>{f.confidence != null ? `${f.confidence}%` : '—'}</StatusBadge>
            </div>
            <div className="rqd-field-row-meta">
              <span>Page {f.source_page ?? 1}</span>
              <span>&middot;</span>
              <span>{f.bbox ? `[${f.bbox.join(', ')}]` : 'no bbox'}</span>
              <span>&middot;</span>
              <StatusBadge
                variant={
                  f.validation_status === 'valid' ? 'success'
                    : f.validation_status === 'warning' ? 'warning'
                    : f.validation_status === 'not_found' ? 'gray'
                    : 'danger'
                }
                size="sm"
              >
                {f.validation_status === 'not_found' ? 'not found' : (f.validation_status ?? 'unknown')}
              </StatusBadge>
            </div>
            <div className="rqd-field-row-value">
              <span className="rqd-field-ai-value">{f.value || <em className="text-muted">missing</em>}</span>
              {f.is_generic && f.type === 'table' && Array.isArray(f.rows) && f.rows.length > 0 ? (
                // Repeated label or ad-hoc table — show every row with its
                // OWN grounding evidence, not just one representative
                // snippet, so "preserved as an array, never overwritten"
                // is actually visible, not just counted.
                <div className="text-muted text-sm" style={{ marginTop: 4 }}>
                  {f.rows.map((row, i) => {
                    const rowEvidence = f.row_evidence?.[i];
                    return (
                      <div key={i} style={{ marginBottom: 2 }}>
                        &bull; {Object.values(row).join(' — ')}
                        {rowEvidence && (
                          <span style={{ fontStyle: 'italic' }} title={rowEvidence}>
                            {' '}(evidence: "{rowEvidence.length > 50 ? `${rowEvidence.slice(0, 50)}…` : rowEvidence}")
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              ) : f.is_generic && f.evidence && (
                <div className="text-muted text-sm" style={{ marginTop: 2, fontStyle: 'italic' }} title={f.evidence}>
                  Evidence: "{f.evidence.length > 80 ? `${f.evidence.slice(0, 80)}…` : f.evidence}"
                </div>
              )}
              {isOpen && (
                <input
                  className="rq-select rqd-field-input"
                  value={drafts[f.name] ?? f.suggested_correction ?? ''}
                  onChange={(e) => { e.stopPropagation(); setDrafts((d) => ({ ...d, [f.name]: e.target.value })); }}
                  onClick={(e) => e.stopPropagation()}
                  placeholder="Corrected value…"
                />
              )}
              {isOpen && (
                <PermissionGate {...REVIEW_QUEUE_PERMISSIONS.REVIEW_QUEUE_EDIT} fallback={null}>
                  <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); onSaveCorrection(f.name); }}>Save</Button>
                </PermissionGate>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
