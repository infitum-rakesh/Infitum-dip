/**
 * DocumentDetails — /app/doc-intel/documents/:documentId
 * Tabs: Overview (preview + metadata) | Timeline | OCR | Extraction | Dataset | Validation | Audit
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  ArrowLeft, Eye, PlayCircle, RefreshCw, Download, Pencil, Trash2, CheckSquare, XCircle,
  ZoomIn, ZoomOut, RotateCw, Maximize2, ChevronLeft, ChevronRight, Info, Clock,
  FileSearch, Sparkles, Database, ShieldCheck, ScrollText, AlertTriangle, GitBranch, FileOutput,
} from 'lucide-react';
import { Button, StatusBadge, DataTable, EmptyState, Modal, PermissionGate, FullPageSpinner } from '@/components/common/index.js';
import { MODULES, ACTIONS, DOCUMENT_PERMISSIONS } from '@/utils/constants.js';
import { formatFileSize, formatDateTime, formatDuration } from '@/utils/formatters.js';
import { documentApi } from '@/services/api/documentApi.js';
import { reviewQueueApi } from '@/services/api/reviewQueueApi.js';
import { pipelineFileApi } from '@/services/api/pipelineFileApi.js';
import { createNode, createEdge } from '@/components/pipeline/nodeTypes.js';
import { languageName } from '@/utils/languageNames.js';
import { SendToReviewModal } from './SendToReviewModal.jsx';
import { ManualStructuringPanel } from './ManualStructuringPanel.jsx';
import { RunExtractionControl } from './RunExtractionControl.jsx';
import './documents.css';

function apiErrorMessage(err, fallback) {
  return err?.response?.data?.detail?.message || err?.response?.data?.detail || err?.message || fallback;
}

function csvEscape(v) {
  const s = String(v ?? '');
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** Real CSV built from the document's actual persisted structured dataset
 * (GET /documents/{id}/dataset's table.columns/table.rows — row-major
 * arrays aligned with columns, see build_dataset() in document_service.py). */
function tableToCsv(table) {
  const cols = table?.columns || [];
  const rows = table?.rows || [];
  const header = cols.map(csvEscape).join(',');
  const body = rows.map((row) => cols.map((_, i) => csvEscape(row[i])).join(',')).join('\n');
  return `${header}\n${body}\n`;
}

/**
 * "Open Pipeline" bug fix — mirrors StructuredDataset.jsx's
 * buildInitialPipelineFromDataset() (the DI wizard's "Continue to
 * Transformation", the proven working pattern for this exact problem):
 * uploads the document's already-persisted structured dataset as a real
 * file (same /api/pipeline-files/upload endpoint the Source node's own
 * "Upload File" option uses) and returns a ready-to-edit pipeline —
 * Source(that file) -> Output Dataset — instead of the previous bare
 * navigate('/app/pipeline') with no document data at all.
 */
async function buildInitialPipelineFromDocument(doc) {
  const ds = await documentApi.getDataset(doc.id);
  const csv = tableToCsv(ds.table);
  const baseName = doc.filename.replace(/\.[^./]+$/, '');
  const file = new File([csv], `${baseName}_dataset.csv`, { type: 'text/csv' });

  const sourceNode = createNode('source', { x: 60, y: 160 });
  const targetNode = createNode('target', { x: 420, y: 160 });

  const uploaded = await pipelineFileApi.upload(file, sourceNode.id);

  sourceNode.config = { ...sourceNode.config, objectType: 'FILE', uploadedFileId: uploaded.id };
  sourceNode.label = `${doc.filename} (extracted)`;
  targetNode.label = 'Write extracted data';

  return {
    name: `${doc.filename} — Pipeline`,
    nodes: [sourceNode, targetNode],
    edges: [createEdge(sourceNode.id, targetNode.id)],
  };
}

const STATUS_VARIANT = {
  UPLOADED: 'gray', SCANNING: 'info', OCR_PROCESSING: 'info', AI_EXTRACTION: 'info',
  COMPLETED: 'success', NEEDS_REVIEW: 'warning', FAILED: 'danger',
};
const STATUS_LABEL = {
  UPLOADED: 'Uploaded', SCANNING: 'Scanning', OCR_PROCESSING: 'OCR Processing',
  AI_EXTRACTION: 'AI Extraction', COMPLETED: 'Completed', NEEDS_REVIEW: 'Needs Review', FAILED: 'Failed',
};
const VALIDATION_VARIANT = { valid: 'success', warning: 'warning', invalid: 'danger' };
const isInReview = (status) => status === 'NEEDS_REVIEW';

const TABS = [
  { key: 'overview', label: 'Overview', icon: Info },
  { key: 'timeline', label: 'Processing Timeline', icon: Clock },
  { key: 'ocr', label: 'OCR', icon: FileSearch },
  { key: 'extraction', label: 'AI Extraction', icon: Sparkles },
  { key: 'dataset', label: 'Structured Dataset', icon: Database },
  { key: 'validation', label: 'Validation', icon: ShieldCheck },
  { key: 'export', label: 'Export', icon: FileOutput },
  { key: 'audit', label: 'Audit', icon: ScrollText },
];

export default function DocumentDetails() {
  const { documentId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = TABS.some((t) => t.key === searchParams.get('tab')) ? searchParams.get('tab') : 'overview';
  const setTab = useCallback((tab) => setSearchParams({ tab }), [setSearchParams]);

  const [doc, setDoc] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [renaming, setRenaming] = useState(false);
  const [renameForm, setRenameForm] = useState({ filename: '', folder: '' });
  const [busy, setBusy] = useState(false);
  const [sendReviewModal, setSendReviewModal] = useState(false);
  const [assignees, setAssignees] = useState([]);
  const [version, setVersion] = useState(0); // bumped after manual structuring saves, to force the Extraction/Dataset tabs to refetch

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    documentApi.getDocument(documentId)
      .then((d) => { setDoc(d); setRenameForm({ filename: d.filename, folder: d.folder ?? '/' }); })
      .catch(() => setError('Document not found or failed to load.'))
      .finally(() => setLoading(false));
  }, [documentId]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { reviewQueueApi.getFacets().then((f) => setAssignees(f.assignees)).catch(() => {}); }, []);

  const runProcess = async () => { setBusy(true); try { await documentApi.processDocument(documentId); load(); } finally { setBusy(false); } };
  const runReprocess = async () => { setBusy(true); try { await documentApi.reprocessDocument(documentId); load(); } finally { setBusy(false); } };
  const confirmSendToReview = async (reviewerId, note) => {
    await documentApi.sendToReview(documentId, { reviewerId, note });
    setSendReviewModal(false);
    load();
  };
  const runCancelReview = async () => { setBusy(true); try { await documentApi.cancelReview(documentId); load(); } finally { setBusy(false); } };
  // Real bug found live (user-reported): NEEDS_REVIEW showed "Cancel
  // Review" with no way to actually reach the review item — this is the
  // new "Review" button's handler, looking up the open item's id and
  // navigating straight to its Review Queue detail page.
  const runGoToReview = async () => {
    setBusy(true);
    try {
      const { review_id } = await documentApi.getReviewItem(documentId);
      if (review_id) navigate(`/app/doc-intel/review/${review_id}`);
      else { window.alert('This document no longer has an open review item — it may have already been resolved elsewhere.'); load(); }
    } finally { setBusy(false); }
  };
  const runDelete = async () => {
    if (!window.confirm(`Delete "${doc.filename}"? This cannot be undone.`)) return;
    await documentApi.deleteDocument(documentId);
    navigate('/app/doc-intel/documents');
  };
  // "Open Pipeline" bug fix — this used to be a bare navigate('/app/pipeline')
  // with no document data, so the button always opened an empty canvas.
  // Now: check for a pipeline already linked to this document (avoids
  // creating a duplicate on every click) and reopen it via Transformation.jsx's
  // existing ?pipelineId= loader; otherwise build a real Source node from
  // this document's structured dataset and hand it to Transformation.jsx
  // via router state (the same location.state.initialPipeline contract
  // Document Intelligence's "Continue to Transformation" already uses).
  const openPipeline = async () => {
    setBusy(true);
    try {
      const { pipeline_id } = await documentApi.getLinkedPipeline(documentId);
      if (pipeline_id) {
        navigate(`/app/transform?pipelineId=${pipeline_id}`);
        return;
      }
      const initialPipeline = await buildInitialPipelineFromDocument(doc);
      navigate('/app/transform', { state: { initialPipeline, documentId: doc.id } });
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not open a pipeline for this document.'));
    } finally {
      setBusy(false);
    }
  };
  const submitRename = async () => {
    await documentApi.updateDocument(documentId, renameForm);
    setRenaming(false);
    load();
  };
  const onManualStructuringSaved = () => { load(); setVersion((v) => v + 1); };

  if (loading) return <FullPageSpinner />;

  if (error || !doc) {
    return (
      <div className="doc-page">
        <EmptyState
          icon={<AlertTriangle size={28} />}
          title="Document not found"
          description={error ?? `No document matches "${documentId}".`}
          action={{ label: 'Back to Documents', onClick: () => navigate('/app/doc-intel/documents') }}
        />
      </div>
    );
  }

  return (
    <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_VIEW} showReason>
      <div className="doc-page">
        {/* Header */}
        <div className="docd-header">
          <button className="docd-back-btn" onClick={() => navigate('/app/doc-intel/documents')}>
            <ArrowLeft size={14} /> Documents
          </button>
          <div className="docd-header-row">
            <div>
              <div className="docd-title-row">
                <h1 className="docd-title">{doc.filename}</h1>
                <StatusBadge variant={STATUS_VARIANT[doc.status] ?? 'gray'} dot>{STATUS_LABEL[doc.status] ?? doc.status}</StatusBadge>
              </div>
              <p className="docd-subtitle">doc_{doc.id} &middot; {doc.doc_type} &middot; {formatFileSize(doc.size_bytes)} &middot; {doc.pipeline ?? 'No pipeline assigned yet'}</p>
            </div>

            <div className="docd-actions-col">
              <div className="docd-actions">
                <Button variant="secondary" size="sm" icon={<Eye size={13} />} onClick={() => setTab('overview')}>View</Button>

                {doc.status === 'UPLOADED' && (
                  <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_PROCESS} fallback={null}>
                    <Button variant="primary" size="sm" icon={<PlayCircle size={13} />} loading={busy} onClick={runProcess}>Process</Button>
                  </PermissionGate>
                )}
                {['COMPLETED', 'FAILED'].includes(doc.status) && (
                  <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_REPROCESS} fallback={null}>
                    <Button variant="secondary" size="sm" icon={<RefreshCw size={13} />} loading={busy} onClick={runReprocess}>Reprocess</Button>
                  </PermissionGate>
                )}
                <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_DOWNLOAD} fallback={null}>
                  <Button variant="secondary" size="sm" icon={<Download size={13} />} onClick={() => documentApi.downloadDocument(doc.id, doc.filename)}>Download</Button>
                </PermissionGate>
                <PermissionGate module={MODULES.DOCUMENT_INTELLIGENCE} action={ACTIONS.EDIT} fallback={null}>
                  <Button variant="secondary" size="sm" icon={<Pencil size={13} />} disabled={isInReview(doc.status)}
                    title={isInReview(doc.status) ? 'Resolve or cancel the review first' : undefined} onClick={() => setRenaming(true)}>
                    Rename
                  </Button>
                </PermissionGate>
                <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_REVIEW} fallback={null}>
                  {isInReview(doc.status) ? (
                    <>
                      <Button variant="primary" size="sm" icon={<FileSearch size={13} />} loading={busy} onClick={runGoToReview}>Review</Button>
                      <Button variant="danger" size="sm" icon={<XCircle size={13} />} loading={busy} onClick={runCancelReview}>Cancel Review</Button>
                    </>
                  ) : (
                    <Button variant="secondary" size="sm" icon={<CheckSquare size={13} />} onClick={() => setSendReviewModal(true)}>Send to Review</Button>
                  )}
                </PermissionGate>
                <Button
                  variant="secondary" size="sm" icon={<GitBranch size={13} />}
                  loading={busy}
                  disabled={!['COMPLETED', 'NEEDS_REVIEW'].includes(doc.status)}
                  title={!['COMPLETED', 'NEEDS_REVIEW'].includes(doc.status) ? 'Available once this document has finished processing' : undefined}
                  onClick={openPipeline}
                >
                  Open Pipeline
                </Button>
                <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_DELETE} fallback={null}>
                  <Button variant="danger" size="sm" icon={<Trash2 size={13} />} disabled={isInReview(doc.status)}
                    title={isInReview(doc.status) ? 'Resolve or cancel the review first' : undefined} onClick={runDelete}>
                    Delete
                  </Button>
                </PermissionGate>
              </div>

              {/* On its own row — this control is two dropdowns + a button,
                  not a single icon button like its neighbors above, so
                  mixing it into that row wrapped unevenly (see
                  documents.css's .docd-actions-col comment). */}
              {!['UPLOADED', 'SCANNING', 'OCR_PROCESSING'].includes(doc.status) && (
                <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_PROCESS} fallback={null}>
                  <div className="docd-extract-row">
                    <RunExtractionControl documentId={doc.id} onDone={onManualStructuringSaved} />
                  </div>
                </PermissionGate>
              )}
            </div>
          </div>
        </div>

        {/* Tab nav */}
        <div className="docd-tab-nav">
          {TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button key={t.key} className={`docd-tab-btn ${activeTab === t.key ? 'active' : ''}`} onClick={() => setTab(t.key)}>
                <Icon size={13} /> {t.label}
              </button>
            );
          })}
        </div>

        <div className="docd-tab-content">
          {activeTab === 'overview'   && <OverviewTab doc={doc} />}
          {activeTab === 'timeline'   && <TimelineTab doc={doc} />}
          {activeTab === 'ocr'        && <OcrTab documentId={doc.id} />}
          {activeTab === 'extraction' && <ExtractionTab key={`ext-${version}`} documentId={doc.id} doc={doc} onManualSaved={onManualStructuringSaved} />}
          {activeTab === 'dataset'    && <DatasetTab key={`ds-${version}`} documentId={doc.id} navigate={navigate} />}
          {activeTab === 'validation' && <ValidationTab doc={doc} navigate={navigate} />}
          {activeTab === 'export'     && <ExportTab documentId={doc.id} filename={doc.filename} />}
          {activeTab === 'audit'      && <AuditTab doc={doc} />}
        </div>
      </div>

      <Modal
        open={renaming}
        onClose={() => setRenaming(false)}
        title="Rename / Move Document"
        size="sm"
        footer={<><Button variant="secondary" onClick={() => setRenaming(false)}>Cancel</Button><Button variant="primary" onClick={submitRename}>Save</Button></>}
      >
        <label className="doc-filter-label" style={{ display: 'block', marginBottom: 6 }}>Document Name</label>
        <input className="doc-select" style={{ width: '100%', marginBottom: 12 }} value={renameForm.filename}
          onChange={(e) => setRenameForm((f) => ({ ...f, filename: e.target.value }))} />
        <label className="doc-filter-label" style={{ display: 'block', marginBottom: 6 }}>Folder</label>
        <input className="doc-select" style={{ width: '100%' }} value={renameForm.folder}
          onChange={(e) => setRenameForm((f) => ({ ...f, folder: e.target.value }))} placeholder="/" />
      </Modal>

      <SendToReviewModal
        open={sendReviewModal}
        onClose={() => setSendReviewModal(false)}
        onConfirm={confirmSendToReview}
        assignees={assignees}
      />
    </PermissionGate>
  );
}

// ── Overview: viewer + metadata ─────────────────────────────────

function OverviewTab({ doc }) {
  const [blobUrl, setBlobUrl] = useState(null);
  const [zoom, setZoom] = useState(1);
  const [rotation, setRotation] = useState(0);
  const [fit, setFit] = useState(true);

  useEffect(() => {
    let url;
    documentApi.getFileBlob(doc.id).then((blob) => {
      url = URL.createObjectURL(blob);
      setBlobUrl(url);
    }).catch(() => setBlobUrl(null));
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [doc.id]);

  const isImage = doc.doc_type === 'IMAGE';
  const isPdf = doc.doc_type === 'PDF';

  const meta = [
    ['Document Name', doc.filename],
    ['Document Type', doc.doc_type],
    ['Pages', doc.page_count],
    ['Size', formatFileSize(doc.size_bytes)],
    ['Uploaded By', doc.uploaded_by_name ?? '—'],
    ['Upload Date', formatDateTime(doc.uploaded_at)],
    ['Status', STATUS_LABEL[doc.status] ?? doc.status],
    ['Pipeline', doc.pipeline ?? '—'],
    ['Processing Time', doc.processing_time_seconds != null ? formatDuration(doc.processing_time_seconds) : '—'],
  ];

  return (
    <div className="docd-overview">
      <div className="docd-viewer">
        <div className="docd-viewer-toolbar">
          <button className="doc-icon-btn" title="Zoom In" onClick={() => setZoom((z) => Math.min(3, z + 0.25))}><ZoomIn size={14} /></button>
          <button className="doc-icon-btn" title="Zoom Out" onClick={() => setZoom((z) => Math.max(0.25, z - 0.25))}><ZoomOut size={14} /></button>
          <button className="doc-icon-btn" title="Rotate" onClick={() => setRotation((r) => (r + 90) % 360)}><RotateCw size={14} /></button>
          <button className="doc-icon-btn" title="Fit Page" onClick={() => { setFit(true); setZoom(1); setRotation(0); }}><Maximize2 size={14} /></button>
          <span className="docd-viewer-zoom">{Math.round(zoom * 100)}%</span>
          <div className="docd-viewer-page-nav">
            <button className="doc-icon-btn" title="Previous page" disabled><ChevronLeft size={14} /></button>
            <span>Page 1 of {doc.page_count}</span>
            <button className="doc-icon-btn" title="Next page" disabled><ChevronRight size={14} /></button>
          </div>
          <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_DOWNLOAD} fallback={null}>
            <button className="doc-icon-btn" title="Download" onClick={() => documentApi.downloadDocument(doc.id, doc.filename)}>
              <Download size={14} />
            </button>
          </PermissionGate>
        </div>
        <div className={`docd-viewer-canvas ${fit ? 'fit' : ''}`}>
          {!blobUrl ? (
            <div className="docd-viewer-loading">Loading preview…</div>
          ) : isImage ? (
            <img src={blobUrl} alt={doc.filename} style={{ transform: `scale(${zoom}) rotate(${rotation}deg)` }} onLoad={() => {}} />
          ) : isPdf ? (
            <iframe title={doc.filename} src={blobUrl} style={{ transform: `scale(${zoom})`, transformOrigin: 'top center' }} />
          ) : (
            <div className="docd-viewer-fallback">
              <FileSearch size={28} />
              <p>Inline preview isn't available for this file type.</p>
              <Button variant="secondary" size="sm" icon={<Download size={13} />} onClick={() => documentApi.downloadDocument(doc.id, doc.filename)}>
                Download to view
              </Button>
            </div>
          )}
        </div>
      </div>

      <div className="docd-meta-card">
        <div className="card-title" style={{ marginBottom: 14 }}>Document Information</div>
        <div className="docd-meta-grid">
          {meta.map(([label, value]) => (
            <div key={label} className="docd-meta-item">
              <span className="docd-meta-label">{label}</span>
              <span className="docd-meta-value">{value ?? '—'}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Processing Timeline ──────────────────────────────────────────

function TimelineTab({ doc }) {
  const stages = doc.timeline ?? [];
  if (stages.length === 0) {
    return <div style={{ padding: 20 }}><EmptyState icon={<Clock size={24} />} title="Not processed yet" description="Process this document to see its pipeline timeline." /></div>;
  }
  return (
    <div className="docd-timeline" style={{ padding: 20 }}>
      {stages.map((stage, i) => (
        <div key={`${stage.stage}_${i}`} className="docd-timeline-step">
          <div className={`docd-timeline-node docd-timeline-node-${stage.status}`} />
          {i < stages.length - 1 && <div className="docd-timeline-connector" />}
          <div className="docd-timeline-card">
            <div className="docd-timeline-card-hdr">
              <span className="docd-timeline-stage">{stage.stage}</span>
              <StatusBadge variant={stage.status === 'completed' ? 'success' : stage.status === 'failed' ? 'danger' : 'gray'} dot>{stage.status}</StatusBadge>
            </div>
            <div className="docd-timeline-grid">
              <div><span className="docd-meta-label">Start Time</span><span className="docd-meta-value">{stage.started_at ? formatDateTime(stage.started_at) : '—'}</span></div>
              <div><span className="docd-meta-label">End Time</span><span className="docd-meta-value">{stage.ended_at ? formatDateTime(stage.ended_at) : '—'}</span></div>
              <div><span className="docd-meta-label">Duration</span><span className="docd-meta-value">{stage.duration_seconds != null ? formatDuration(stage.duration_seconds) : '—'}</span></div>
              <div><span className="docd-meta-label">Confidence</span><span className="docd-meta-value">{stage.confidence != null ? `${stage.confidence}%` : '—'}</span></div>
            </div>
            {stage.error && <div className="docd-timeline-error"><AlertTriangle size={13} /> {stage.error}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── OCR ───────────────────────────────────────────────────────────

function OcrTab({ documentId }) {
  const [ocr, setOcr] = useState(null);
  const [loading, setLoading] = useState(true);
  const [notReady, setNotReady] = useState(false);

  useEffect(() => {
    setLoading(true);
    setNotReady(false);
    documentApi.getOcr(documentId)
      .then(setOcr)
      .catch(() => setNotReady(true))
      .finally(() => setLoading(false));
  }, [documentId]);

  if (loading) return <div className="docd-tab-loading">Loading OCR…</div>;
  if (notReady || !ocr) {
    return <div style={{ padding: 20 }}><EmptyState icon={<FileSearch size={24} />} title="OCR not available" description="This document hasn't been through OCR yet. Process it to generate OCR output." /></div>;
  }

  return (
    <div className="docd-ocr" style={{ padding: 20 }}>
      <div className="docd-ocr-hero">
        <div className="card-title">OCR Confidence</div>
        <div className="docd-ocr-conf">{ocr.confidence != null ? `${ocr.confidence}%` : '—'}</div>
        <LanguageInfo language={ocr.language} qualityFlags={ocr.quality_flags} />
      </div>
      <div className="docd-ocr-grid">
        <div className="docd-profile-card">
          <div className="card-title">Detected Regions</div>
          <div className="card-subtitle">Highlighted OCR regions with per-region confidence</div>
          <div className="docd-ocr-regions">
            {(ocr.regions ?? []).map((r, i) => (
              <div key={i} className="docd-ocr-region">
                <span className="docd-ocr-region-page">p{r.page}</span>
                <span className="docd-ocr-region-text">{r.text}</span>
                <span className="docd-ocr-region-conf">{r.confidence}%</span>
              </div>
            ))}
          </div>
        </div>
        <div className="docd-profile-card">
          <div className="card-title">Extracted Text</div>
          <pre className="docd-ocr-text">{ocr.full_text}</pre>
        </div>
      </div>
    </div>
  );
}

// Multilingual Document Processing, Phase 10 — real detected-language +
// OCR-quality display, straight from doc.ocr_result (Phases 4/6 already
// populate "language"/"quality_flags" there; GET /documents/{id}/ocr
// already returns them verbatim, so this is pure display, no new API).
// Mirrors ReviewQueueDetail.jsx's LanguageInfo (Phase 9), namespaced to
// docd-lang-* since this page uses the docd- prefix, not rqd-.
function LanguageInfo({ language, qualityFlags }) {
  if (!language && !(qualityFlags && qualityFlags.length)) return null;
  return (
    <div className="docd-lang-info">
      {language && (
        <div className="docd-lang-row">
          <StatusBadge variant="info" dot>{languageName(language.primary_language)}</StatusBadge>
          {language.language_detection_confidence != null && (
            <span className="text-muted text-sm">{language.language_detection_confidence.toFixed(0)}% confidence · {language.language_detection_method}</span>
          )}
          {language.is_multilingual && (
            <span className="docd-lang-pages">
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
        <div className="docd-lang-row">
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

// ── AI Extraction ─────────────────────────────────────────────────

function ExtractionTab({ documentId, doc, onManualSaved }) {
  const [extraction, setExtraction] = useState(null);
  const [loading, setLoading] = useState(true);
  const [notReady, setNotReady] = useState(false);

  useEffect(() => {
    setLoading(true);
    setNotReady(false);
    documentApi.getExtraction(documentId)
      .then(setExtraction)
      .catch(() => setNotReady(true))
      .finally(() => setLoading(false));
  }, [documentId]);

  if (loading) return <div className="docd-tab-loading">Loading extraction…</div>;

  if (notReady || !extraction || (extraction.fields ?? []).length === 0) {
    const terminal = ['COMPLETED', 'NEEDS_REVIEW', 'FAILED'].includes(doc?.status);
    if (!terminal) {
      // Either never processed, or still mid-pipeline — nothing settled to
      // reference yet (OCR may not even be done), so just point at Process
      // rather than inviting manual entry prematurely.
      return <div style={{ padding: 20 }}><EmptyState icon={<Sparkles size={24} />} title="No extracted fields" description={doc?.status === 'UPLOADED' ? "This document hasn't been through AI extraction yet. Process it to extract structured fields." : 'Processing is still in progress — check back once it finishes.'} /></div>;
    }
    // Pipeline genuinely finished but produced no fields — the real gap
    // "no AI connection configured" / "model found nothing" reported by
    // users. OCR text is available, so let the user structure it by hand.
    const aiStage = (doc?.timeline ?? []).find((s) => s.stage === 'AI Extraction');
    return <ManualStructuringPanel documentId={documentId} reason={aiStage?.error} onSaved={onManualSaved} />;
  }

  const cols = [
    { key: 'name', header: 'Field' },
    { key: 'value', header: 'Value' },
    { key: 'confidence', header: 'Confidence', sortable: true, render: (v) => `${v}%` },
    { key: 'source_page', header: 'Source Page', render: (v) => `p${v}` },
    { key: 'validation_status', header: 'Validation Status', render: (v) => <StatusBadge variant={VALIDATION_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
  ];

  return (
    <div style={{ padding: 20 }}>
      <div className="docd-meta-card" style={{ marginBottom: 16 }}>
        <div className="card-title">Overall Extraction Confidence</div>
        <div className="docd-ocr-conf">{extraction.overall_confidence != null ? `${extraction.overall_confidence}%` : '—'}</div>
      </div>
      <DataTable columns={cols} data={extraction.fields ?? []} />
    </div>
  );
}

// ── Structured Dataset ─────────────────────────────────────────────

function DatasetTab({ documentId, navigate }) {
  const [dataset, setDataset] = useState(null);
  const [loading, setLoading] = useState(true);
  const [notReady, setNotReady] = useState(false);
  const [view, setView] = useState('table');

  useEffect(() => {
    setLoading(true);
    setNotReady(false);
    documentApi.getDataset(documentId)
      .then(setDataset)
      .catch(() => setNotReady(true))
      .finally(() => setLoading(false));
  }, [documentId]);

  if (loading) return <div className="docd-tab-loading">Loading dataset…</div>;
  if (notReady || !dataset) {
    return <div style={{ padding: 20 }}><EmptyState icon={<Database size={24} />} title="No dataset generated" description="Process this document through AI extraction to generate a structured dataset." /></div>;
  }

  const tableCols = (dataset.table.columns ?? []).map((c) => ({ key: c, header: c }));
  const tableRows = (dataset.table.rows ?? []).map((row, i) => {
    const obj = { id: i };
    dataset.table.columns.forEach((c, idx) => { obj[c] = row[idx]; });
    return obj;
  });

  return (
    <div style={{ padding: 20 }}>
      <div className="docd-dataset-toolbar">
        <div className="doc-pill-group">
          {['table', 'json', 'schema'].map((v) => (
            <button key={v} className={`doc-pill ${view === v ? 'active' : ''}`} onClick={() => setView(v)}>
              {v === 'table' ? 'Table View' : v === 'json' ? 'JSON View' : 'Schema View'}
            </button>
          ))}
        </div>
        <div className="docd-dataset-actions">
          <Button variant="secondary" size="sm" icon={<Database size={13} />} onClick={() => navigate('/app/catalog')}>Open Dataset</Button>
          <PermissionGate module={MODULES.TRANSFORMATION} action={ACTIONS.CREATE} fallback={null}>
            <Button variant="secondary" size="sm" icon={<GitBranch size={13} />} onClick={() => navigate('/app/transform')}>Create Transformation</Button>
          </PermissionGate>
          <PermissionGate module={MODULES.VALIDATION} action={ACTIONS.EXECUTE} fallback={null}>
            <Button variant="primary" size="sm" icon={<ShieldCheck size={13} />} onClick={() => navigate('/app/validation')}>Run Validation</Button>
          </PermissionGate>
        </div>
      </div>

      {view === 'table' && <DataTable columns={tableCols} data={tableRows} />}
      {view === 'json' && <pre className="docd-ocr-text">{JSON.stringify(dataset.json_view, null, 2)}</pre>}
      {view === 'schema' && (
        <DataTable
          columns={[{ key: 'field', header: 'Field' }, { key: 'type', header: 'Type' }]}
          data={dataset.dataset_schema ?? []}
        />
      )}
    </div>
  );
}

// ── Validation ──────────────────────────────────────────────────────

function ValidationTab({ doc, navigate }) {
  const validationStage = (doc.timeline ?? []).find((s) => s.stage === 'Validation');

  return (
    <div style={{ padding: 20 }}>
      <div className="docd-meta-card" style={{ marginBottom: 16 }}>
        <div className="card-title">Validation Summary</div>
        {validationStage ? (
          <div className="docd-timeline-grid" style={{ marginTop: 10 }}>
            <div><span className="docd-meta-label">Status</span><span className="docd-meta-value">{validationStage.status}</span></div>
            <div><span className="docd-meta-label">Confidence</span><span className="docd-meta-value">{validationStage.confidence != null ? `${validationStage.confidence}%` : '—'}</span></div>
            <div><span className="docd-meta-label">Completed</span><span className="docd-meta-value">{validationStage.ended_at ? formatDateTime(validationStage.ended_at) : '—'}</span></div>
          </div>
        ) : (
          <p className="text-muted text-sm" style={{ marginTop: 4 }}>No validation has run for this document yet — process it first.</p>
        )}
        {validationStage?.error && <div className="docd-timeline-error" style={{ marginTop: 10 }}><AlertTriangle size={13} /> {validationStage.error}</div>}
      </div>
      <PermissionGate module={MODULES.VALIDATION} action={ACTIONS.EXECUTE} fallback={null}>
        <Button variant="primary" size="sm" icon={<ShieldCheck size={13} />} onClick={() => navigate('/app/validation')}>
          Open Validation Module
        </Button>
      </PermissionGate>
    </div>
  );
}

// ── Export ────────────────────────────────────────────────────────
// On-demand export of this document's already-persisted processed
// datasets (spec item #6) — reads from DocumentProcessedDataset rows,
// never triggers a reprocess. Independent of the automatic push-on-
// completion that Processed Data Storage/webhook settings configure —
// this lets an admin re-export or re-push any already-processed
// document at any time.

const EXPORT_DATASETS = [
  { value: 'metadata', label: 'Metadata' },
  { value: 'ocr', label: 'OCR' },
  { value: 'extraction', label: 'AI Extraction' },
  { value: 'structured_data', label: 'Structured Dataset' },
  { value: 'validation', label: 'Validation' },
  { value: 'processing_run', label: 'Processing Run' },
];

function ExportTab({ documentId, filename }) {
  const [selected, setSelected] = useState(() => new Set(EXPORT_DATASETS.map((d) => d.value)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pushResult, setPushResult] = useState(null);

  const toggle = (value) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(value)) next.delete(value); else next.add(value);
      return next;
    });
  };

  const datasetsParam = selected.size === EXPORT_DATASETS.length ? null : Array.from(selected);

  const downloadJson = async () => {
    setBusy(true); setError(''); setPushResult(null);
    try {
      const body = await documentApi.exportDatasets(documentId, { datasets: datasetsParam, destination: 'inline' });
      const blob = new Blob([JSON.stringify(body, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${filename.replace(/\.[^./]+$/, '')}_export.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(apiErrorMessage(err, 'Could not export this document.'));
    } finally {
      setBusy(false);
    }
  };

  const pushToConnection = async () => {
    setBusy(true); setError(''); setPushResult(null);
    try {
      const body = await documentApi.exportDatasets(documentId, { datasets: datasetsParam, destination: 'connection' });
      setPushResult(body);
    } catch (err) {
      setError(apiErrorMessage(err, 'Could not push this document to the configured connection.'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ padding: 20 }}>
      <div className="docd-meta-card" style={{ marginBottom: 16 }}>
        <div className="card-title">Export processed data</div>
        <p className="text-muted text-sm" style={{ marginTop: 4, marginBottom: 14 }}>
          Exports this document's already-persisted processed output — never triggers a reprocess. Pick which
          datasets to include, then either download them as JSON or re-push them through the organization's
          configured Processed Data Storage connection (Administration &gt; Settings &gt; Processed Data Storage).
        </p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 16 }}>
          {EXPORT_DATASETS.map((d) => (
            <label key={d.value} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
              <input type="checkbox" checked={selected.has(d.value)} onChange={() => toggle(d.value)} />
              {d.label}
            </label>
          ))}
        </div>

        <div className="docd-actions">
          <Button variant="secondary" size="sm" icon={<Download size={13} />} loading={busy} disabled={selected.size === 0} onClick={downloadJson}>
            Download JSON
          </Button>
          <Button variant="secondary" size="sm" icon={<FileOutput size={13} />} loading={busy} disabled={selected.size === 0} onClick={pushToConnection}>
            Push to Connection
          </Button>
        </div>

        {error && <p className="docd-timeline-error" style={{ marginTop: 12 }}><AlertTriangle size={13} /> {error}</p>}

        {pushResult && (
          <div style={{ marginTop: 12 }}>
            <StatusBadge variant={pushResult.status === 'SUCCESS' ? 'success' : 'danger'} dot>
              {pushResult.status ?? 'UNKNOWN'}
            </StatusBadge>
            {pushResult.error && <p className="docd-timeline-error" style={{ marginTop: 8 }}><AlertTriangle size={13} /> {pushResult.error}</p>}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Audit ─────────────────────────────────────────────────────────

function AuditTab({ doc }) {
  const events = [
    { label: 'Document uploaded', at: doc.uploaded_at, by: doc.uploaded_by_name },
    ...((doc.timeline ?? []).filter((s) => s.stage !== 'Uploaded').map((s) => ({
      label: `${s.stage} — ${s.status}`, at: s.ended_at ?? s.started_at, by: 'Document Intelligence Pipeline',
    }))),
  ];

  const cols = [
    { key: 'label', header: 'Event' },
    { key: 'by', header: 'Actor' },
    { key: 'at', header: 'Timestamp', render: (v) => v ? formatDateTime(v) : '—' },
  ];

  return (
    <div style={{ padding: 20 }}>
      <DataTable columns={cols} data={events.map((e, i) => ({ ...e, id: i }))}
        emptyState={<EmptyState icon={<ScrollText size={24} />} title="No audit history" description="This document has no recorded events yet." />} />
    </div>
  );
}
