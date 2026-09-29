/**
 * Documents — /app/doc-intel/documents
 * The centralized document repository (management/repository layer).
 * Processing itself is delegated to the Document Intelligence pipeline via
 * documentApi (POST /documents/{id}/process|reprocess) — this page never
 * re-implements Image Scanner / OCR / AI Extraction.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import {
  Upload, FolderUp, FileArchive, Layers, PlayCircle, X, FileText, Image as ImageIcon, FileSpreadsheet,
  FileType2, File as FileIcon, Eye, RefreshCw, Download, MoreVertical, Pencil,
  Trash2, CheckSquare, XCircle, GitBranch, Sparkles, Database,
} from 'lucide-react';
import {
  Button, StatusBadge, DataTable, SearchBox, EmptyState, Modal, PermissionGate,
} from '@/components/common/index.js';
import { MODULES, ACTIONS, PAGINATION, DOCUMENT_PERMISSIONS } from '@/utils/constants.js';
import { formatFileSize, formatDateTime } from '@/utils/formatters.js';
import { usePermission } from '@/hooks/usePermission.js';
import { documentApi } from '@/services/api/documentApi.js';
import { reviewQueueApi } from '@/services/api/reviewQueueApi.js';
import { documentZipBatchApi } from '@/services/api/zipBatchApi.js';
import { ZipUploadDialog } from '@/components/batch-upload/ZipUploadDialog.jsx';
import { ZipBatchListModal } from '@/components/batch-upload/ZipBatchListModal.jsx';
import { DocumentFilters } from './DocumentFilters.jsx';
import { SendToReviewModal } from './SendToReviewModal.jsx';
import { BulkUploadPanel } from './BulkUploadPanel.jsx';
import { RunExtractionControl } from './RunExtractionControl.jsx';
import './documents.css';

// Actions that would interfere with an active human review (rename, delete,
// reprocess) are disabled while a document is NEEDS_REVIEW — the review
// must be resolved, or explicitly cancelled, first.
const isInReview = (status) => status === 'NEEDS_REVIEW';

function apiErrorMessage(err, fallback) {
  return err?.response?.data?.detail?.message || err?.response?.data?.detail || err?.data?.detail?.message || err?.data?.detail || err?.message || fallback;
}

const TYPE_ICON = { PDF: FileText, IMAGE: ImageIcon, EXCEL: FileSpreadsheet, WORD: FileType2, TEXT: FileText, OTHER: FileIcon };

const STATUS_VARIANT = {
  UPLOADED: 'gray', SCANNING: 'info', OCR_PROCESSING: 'info', AI_EXTRACTION: 'info',
  COMPLETED: 'success', NEEDS_REVIEW: 'warning', FAILED: 'danger',
};
const STATUS_LABEL = {
  UPLOADED: 'Uploaded', SCANNING: 'Scanning', OCR_PROCESSING: 'OCR Processing',
  AI_EXTRACTION: 'AI Extraction', COMPLETED: 'Completed', NEEDS_REVIEW: 'Needs Review', FAILED: 'Failed',
};

const EMPTY_FILTERS = { doc_type: 'All', status: 'All', date_preset: 'All', date_from: '', date_to: '', owner: 'All', owner_user_id: '' };

// Same thresholds document_service.py uses to auto-flag NEEDS_REVIEW
// (overall_conf < 90) and per-field validation_status (>=90 valid, 80-89
// warning, <80 invalid) — so the list's color-coding always agrees with
// why a document actually landed in Needs Review.
function ConfidenceCell(value) {
  if (value == null) return <span className="doc-conf-cell doc-conf-empty">—</span>;
  const cls = value >= 90 ? 'doc-conf-good' : value >= 80 ? 'doc-conf-warn' : 'doc-conf-poor';
  return <span className={`doc-conf-cell ${cls}`}>{value}%</span>;
}

export default function Documents() {
  const navigate = useNavigate();
  const { can } = usePermission();

  const [stats, setStats] = useState(null);
  const [facets, setFacets] = useState(null);
  const [assignees, setAssignees] = useState([]);

  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');

  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [selected, setSelected] = useState(new Set());
  const [uploadModal, setUploadModal] = useState(null); // 'file' | 'folder' | 'zip' | null
  const [zipBatchListOpen, setZipBatchListOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState(null);
  const [renameForm, setRenameForm] = useState({ filename: '', folder: '' });
  const [busyId, setBusyId] = useState(null);
  const [sendReviewModal, setSendReviewModal] = useState(null); // { type: 'single', row } | { type: 'bulk' } | null
  const [runExtractionTarget, setRunExtractionTarget] = useState(null); // row | null
  const [actionError, setActionError] = useState(null);

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadStats = useCallback(() => { documentApi.getStats().then(setStats).catch(() => {}); }, []);
  const loadFacets = useCallback(() => { documentApi.getFacets().then(setFacets).catch(() => {}); }, []);
  const loadAssignees = useCallback(() => { reviewQueueApi.getFacets().then((f) => setAssignees(f.assignees)).catch(() => {}); }, []);

  useEffect(() => { loadStats(); loadFacets(); loadAssignees(); }, [loadStats, loadFacets, loadAssignees]);

  const loadDocuments = useCallback(() => {
    setLoading(true);
    setError(null);
    documentApi
      .listDocuments({
        q: search,
        doc_type: filters.doc_type,
        status: filters.status,
        date_preset: filters.date_preset,
        date_from: filters.date_from,
        date_to: filters.date_to,
        owner: filters.owner,
        owner_user_id: filters.owner_user_id || undefined,
        page, page_size: pageSize,
      })
      .then((res) => { setRows(res.items); setTotal(res.total); })
      .catch(() => setError('Failed to load documents. Please try again.'))
      .finally(() => setLoading(false));
  }, [search, filters, page, pageSize]);

  useEffect(() => { loadDocuments(); }, [loadDocuments]);

  const refreshAll = () => { loadStats(); loadDocuments(); };

  const handleFiltersChange = (next) => { setFilters(next); setPage(1); };
  const handleClearFilters  = () => { setFilters(EMPTY_FILTERS); setPage(1); };
  const handleClearSearch   = () => { setSearchInput(''); setSearch(''); setPage(1); };

  // ── Selection ─────────────────────────────────────────────────
  const toggleRow = (id) => setSelected((s) => {
    const next = new Set(s);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });
  const toggleAll = () => setSelected((s) => (s.size === rows.length ? new Set() : new Set(rows.map((r) => r.id))));
  const clearSelection = () => setSelected(new Set());

  // ── Single-doc actions ───────────────────────────────────────────
  const runProcess = async (id) => {
    setBusyId(id);
    try { await documentApi.processDocument(id); refreshAll(); } finally { setBusyId(null); }
  };
  const runReprocess = async (id) => {
    setBusyId(id);
    try { await documentApi.reprocessDocument(id); refreshAll(); } finally { setBusyId(null); }
  };
  const runDelete = async (row) => {
    if (!window.confirm(`Delete "${row.filename}"? This cannot be undone.`)) return;
    setBusyId(row.id);
    try { await documentApi.deleteDocument(row.id); refreshAll(); } finally { setBusyId(null); }
  };
  const runCancelReview = async (id) => {
    setBusyId(id);
    try { await documentApi.cancelReview(id); refreshAll(); } finally { setBusyId(null); }
  };
  const runDownload = (row) => documentApi.downloadDocument(row.id, row.filename);
  const openRename = (row) => { setRenameTarget(row); setRenameForm({ filename: row.filename, folder: row.folder ?? '/' }); };
  const submitRename = async () => {
    if (!renameTarget) return;
    await documentApi.updateDocument(renameTarget.id, { filename: renameForm.filename, folder: renameForm.folder });
    setRenameTarget(null);
    refreshAll();
  };

  // ── Send to Review (confirmation + optional reviewer, single or bulk) ────
  const confirmSendToReview = async (reviewerId, note) => {
    if (sendReviewModal.type === 'single') {
      await documentApi.sendToReview(sendReviewModal.row.id, { reviewerId, note });
    } else {
      await documentApi.bulkAction(Array.from(selected), 'send_to_review', { reviewerId, note });
      clearSelection();
    }
    setSendReviewModal(null);
    refreshAll();
  };

  // ── Bulk actions ─────────────────────────────────────────────────
  const bulkRun = async (action) => {
    const ids = Array.from(selected);
    if (ids.length === 0) return;
    if (action === 'delete' && !window.confirm(`Delete ${ids.length} selected document(s)? This cannot be undone.`)) return;
    await documentApi.bulkAction(ids, action);
    clearSelection();
    refreshAll();
  };
  const bulkDownload = async () => {
    for (const row of rows.filter((r) => selected.has(r.id))) {
      // eslint-disable-next-line no-await-in-loop
      await documentApi.downloadDocument(row.id, row.filename);
    }
  };
  const startProcessing = async () => {
    setActionError(null);
    // Real bug found live: this used to silently fall back to every
    // UPLOADED row when nothing was checked, and returned with zero
    // feedback if that fallback set was also empty — so clicking the
    // button with nothing selected either did something surprising
    // (bulk-process everything) or nothing at all, with no indication
    // either way. The button is now selection-gated (see the JSX below,
    // disabled when selected.size === 0), so this only fires with an
    // explicit selection; this guard stays as defense in depth.
    const ids = Array.from(selected);
    if (ids.length === 0) {
      setActionError('Please select at least one job to process.');
      return;
    }
    try {
      await documentApi.bulkAction(ids, 'process');
      clearSelection();
      refreshAll();
    } catch (err) {
      setActionError(apiErrorMessage(err, 'Could not start processing the selected document(s).'));
    }
  };

  const columns = [
    {
      key: '_select',
      header: (
        <input type="checkbox" checked={rows.length > 0 && selected.size === rows.length} onChange={toggleAll} onClick={(e) => e.stopPropagation()} />
      ),
      width: 34,
      render: (_v, row) => (
        <input type="checkbox" checked={selected.has(row.id)} onChange={() => toggleRow(row.id)} onClick={(e) => e.stopPropagation()} />
      ),
    },
    {
      key: 'filename', header: 'Document Name', sortable: true,
      render: (val, row) => {
        const Icon = TYPE_ICON[row.doc_type] ?? FileIcon;
        return (
          <div className="doc-name-cell">
            <Icon size={16} className="doc-name-icon" />
            <div>
              <div className="doc-name-text">{val}</div>
              <div className="doc-name-sub">ID: doc_{row.id}</div>
            </div>
          </div>
        );
      },
    },
    { key: 'doc_type', header: 'Type', sortable: true },
    { key: 'page_count', header: 'Pages', render: (v) => `${v ?? 1} page${v === 1 ? '' : 's'}` },
    { key: 'size_bytes', header: 'Size', sortable: true, render: (v) => formatFileSize(v) },
    { key: 'uploaded_by_name', header: 'Uploaded By', render: (v) => v ?? '—' },
    { key: 'uploaded_at', header: 'Uploaded At', sortable: true, render: (v) => formatDateTime(v) },
    {
      key: 'status', header: 'Processing Status',
      render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{STATUS_LABEL[v] ?? v}</StatusBadge>,
    },
    { key: 'ocr_percent', header: 'OCR %', sortable: true, render: ConfidenceCell },
    { key: 'extraction_percent', header: 'Extraction %', sortable: true, render: ConfidenceCell },
    { key: 'confidence', header: 'Confidence', sortable: true, render: ConfidenceCell },
    { key: 'pipeline', header: 'Pipeline', render: (v) => v ?? '—' },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        // Each button sits in an explicit, fixed grid-column slot (1-4) —
        // NOT DOM order — because Process only renders for UPLOADED,
        // Reprocess only for COMPLETED/FAILED, and either can also be
        // hidden by PermissionGate; without a fixed slot, Download/the
        // kebab menu would shift left on rows missing that button,
        // leaving the action icons misaligned column-to-column between
        // rows (see documents.css's .doc-row-actions grid).
        <div className="doc-row-actions" onClick={(e) => e.stopPropagation()}>
          <button className="doc-icon-btn" style={{ gridColumn: 1 }} title="View" onClick={() => navigate(`/app/doc-intel/documents/${row.id}`)}>
            <Eye size={14} />
          </button>

          {row.status === 'UPLOADED' && (
            <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_PROCESS} fallback={null}>
              <button className="doc-icon-btn" style={{ gridColumn: 2 }} title="Process" disabled={busyId === row.id} onClick={() => runProcess(row.id)}>
                <PlayCircle size={14} />
              </button>
            </PermissionGate>
          )}
          {['COMPLETED', 'FAILED'].includes(row.status) && (
            <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_REPROCESS} fallback={null}>
              <button className="doc-icon-btn" style={{ gridColumn: 2 }} title="Reprocess" disabled={busyId === row.id} onClick={() => runReprocess(row.id)}>
                <RefreshCw size={14} />
              </button>
            </PermissionGate>
          )}

          <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_DOWNLOAD} fallback={null}>
            <button className="doc-icon-btn" style={{ gridColumn: 3 }} title="Download" onClick={() => runDownload(row)}>
              <Download size={14} />
            </button>
          </PermissionGate>

          <RowMenu
            row={row}
            style={{ gridColumn: 4 }}
            onRename={() => openRename(row)}
            onSendToReview={() => setSendReviewModal({ type: 'single', row })}
            onCancelReview={() => runCancelReview(row.id)}
            onOpenPipeline={() => navigate('/app/pipeline')}
            onRunExtraction={() => setRunExtractionTarget(row)}
            onViewExtraction={() => navigate(`/app/doc-intel/documents/${row.id}?tab=extraction`)}
            onCreateDataset={() => navigate(`/app/doc-intel/documents/${row.id}?tab=dataset`)}
            onDelete={() => runDelete(row)}
          />
        </div>
      ),
    },
  ];

  const kpis = [
    { label: 'Total Documents', value: stats ? stats.total_documents : '—' },
    { label: 'Processing', value: stats ? stats.processing : '—', variant: 'info' },
    { label: 'Completed', value: stats ? stats.completed : '—', variant: 'success' },
    { label: 'Needs Review', value: stats ? stats.needs_review : '—', variant: 'warning' },
    { label: 'Failed', value: stats ? stats.failed : '—', variant: 'danger' },
    { label: 'Storage Used', value: stats ? formatFileSize(stats.storage_used_bytes) : '—' },
  ];

  const hasActiveFilters = search || Object.entries(filters).some(([k, v]) => v && v !== 'All' && !['date_from', 'date_to', 'owner_user_id'].includes(k));

  return (
    <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_VIEW} showReason>
      <div className="doc-page">
        {/* Header */}
        <div className="doc-header">
          <div>
            <h1 className="doc-title">Documents</h1>
            <p className="doc-subtitle">Upload, manage and process documents using Document Intelligence.</p>
          </div>
          <div className="doc-header-actions">
            <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_UPLOAD} fallback={null}>
              <Button variant="secondary" icon={<Upload size={14} />} onClick={() => setUploadModal('file')}>Upload Document</Button>
              <Button variant="secondary" icon={<FolderUp size={14} />} onClick={() => setUploadModal('folder')}>Upload Folder</Button>
              <Button variant="secondary" icon={<FileArchive size={14} />} onClick={() => setUploadModal('zip')}>Upload ZIP</Button>
              <Button variant="ghost" icon={<Layers size={14} />} onClick={() => setZipBatchListOpen(true)}>ZIP Batches</Button>
            </PermissionGate>
            <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_PROCESS} fallback={null}>
              <Button
                variant="primary"
                icon={<PlayCircle size={14} />}
                onClick={startProcessing}
                disabled={selected.size === 0}
                title={selected.size === 0 ? 'Select at least one document to process' : undefined}
              >
                Start Processing{selected.size > 0 ? ` (${selected.size})` : ''}
              </Button>
            </PermissionGate>
          </div>
        </div>

        {actionError && <div className="doc-error-banner">{actionError}</div>}

        {/* KPIs */}
        <div className="doc-kpis">
          {kpis.map((k) => (
            <div key={k.label} className="doc-kpi-card">
              <div className={`doc-kpi-val ${k.variant ? `doc-kpi-val-${k.variant}` : ''}`}>{k.value}</div>
              <div className="doc-kpi-label">{k.label}</div>
            </div>
          ))}
        </div>

        {/* Search */}
        <div className="doc-search-row">
          <SearchBox
            value={searchInput}
            onChange={setSearchInput}
            placeholder="Search by name, ID, type, uploader, status, extracted entity or pipeline…"
            className="doc-search-box"
          />
          {searchInput && (
            <button className="doc-clear-search-btn" onClick={handleClearSearch}>
              <X size={13} /> Clear
            </button>
          )}
        </div>

        {/* Bulk action bar */}
        {selected.size > 0 && (
          <div className="doc-bulk-bar">
            <span>{selected.size} selected</span>
            <div className="doc-bulk-actions">
              <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_PROCESS} fallback={null}>
                <Button variant="secondary" size="sm" icon={<PlayCircle size={13} />} onClick={() => bulkRun('process')}>Process Selected</Button>
              </PermissionGate>
              <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_REVIEW} fallback={null}>
                <Button variant="secondary" size="sm" icon={<CheckSquare size={13} />} onClick={() => setSendReviewModal({ type: 'bulk' })}>Send to Review</Button>
              </PermissionGate>
              <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_DOWNLOAD} fallback={null}>
                <Button variant="secondary" size="sm" icon={<Download size={13} />} onClick={bulkDownload}>Download Selected</Button>
              </PermissionGate>
              <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_DELETE} fallback={null}>
                <Button variant="danger" size="sm" icon={<Trash2 size={13} />} onClick={() => bulkRun('delete')}>Delete Selected</Button>
              </PermissionGate>
              <Button variant="ghost" size="sm" onClick={clearSelection}>Clear</Button>
            </div>
          </div>
        )}

        {/* Body: filters + table */}
        <div className="doc-body">
          <DocumentFilters filters={filters} onChange={handleFiltersChange} onClear={handleClearFilters} uploaders={facets?.uploaders} />

          <div className="doc-table-panel">
            <DataTable
              columns={columns}
              data={rows}
              loading={loading}
              error={error}
              onRowClick={(row) => navigate(`/app/doc-intel/documents/${row.id}`)}
              pagination={{ page, pageSize, total, onChange: setPage }}
              emptyState={
                <EmptyState
                  icon={<FileText size={28} />}
                  title={hasActiveFilters ? 'No matching documents' : 'No documents yet'}
                  description={hasActiveFilters ? 'Try a different search term or clear your filters.' : 'Upload a document to get started.'}
                  action={
                    hasActiveFilters
                      ? { label: 'Clear search & filters', onClick: () => { handleClearSearch(); handleClearFilters(); } }
                      : can(DOCUMENT_PERMISSIONS.DOCUMENT_UPLOAD) ? { label: 'Upload Document', onClick: () => setUploadModal('file') } : undefined
                  }
                />
              }
            />
          </div>
        </div>
      </div>

      {/* Upload modal — bulk upload + automatic processing, no separate "Process" click */}
      <Modal open={uploadModal === 'file' || uploadModal === 'folder'} onClose={() => setUploadModal(null)} title={uploadModal === 'folder' ? 'Upload Folder' : 'Upload Document'} size="md">
        <BulkUploadPanel
          directory={uploadModal === 'folder'}
          onDocumentReady={refreshAll}
          onAllSettled={refreshAll}
        />
      </Modal>

      {/* Upload ZIP — chunked upload of a .zip of many documents, extracted,
          deduplicated, and processed through this same pipeline in bounded
          parallel (ZB1-ZB12). Real backend, not a variant of BulkUploadPanel. */}
      <Modal open={uploadModal === 'zip'} onClose={() => setUploadModal(null)} title="Upload ZIP" size="md">
        <ZipUploadDialog
          zipBatchApi={documentZipBatchApi}
          onBatchCreated={refreshAll}
          onViewBatch={(batch) => { setUploadModal(null); navigate(`/app/doc-intel/documents/zip-batches/${batch.id}`); }}
          onClose={() => setUploadModal(null)}
        />
      </Modal>

      {/* ZIP Batches — always-available way back to a batch's live progress
          page, regardless of whether the upload dialog was closed early. */}
      <Modal open={zipBatchListOpen} onClose={() => setZipBatchListOpen(false)} title="ZIP Batches" size="lg">
        <ZipBatchListModal
          zipBatchApi={documentZipBatchApi}
          onOpenBatch={(batchId) => { setZipBatchListOpen(false); navigate(`/app/doc-intel/documents/zip-batches/${batchId}`); }}
        />
      </Modal>

      {/* Rename / move modal */}
      <Modal
        open={!!renameTarget}
        onClose={() => setRenameTarget(null)}
        title="Rename / Move Document"
        size="sm"
        footer={<><Button variant="secondary" onClick={() => setRenameTarget(null)}>Cancel</Button><Button variant="primary" onClick={submitRename}>Save</Button></>}
      >
        <label className="doc-filter-label" style={{ display: 'block', marginBottom: 6 }}>Document Name</label>
        <input className="doc-select" style={{ width: '100%', marginBottom: 12 }} value={renameForm.filename}
          onChange={(e) => setRenameForm((f) => ({ ...f, filename: e.target.value }))} />
        <label className="doc-filter-label" style={{ display: 'block', marginBottom: 6 }}>Folder</label>
        <input className="doc-select" style={{ width: '100%' }} value={renameForm.folder}
          onChange={(e) => setRenameForm((f) => ({ ...f, folder: e.target.value }))} placeholder="/" />
      </Modal>

      {/* Send to review — confirmation + optional reviewer */}
      <SendToReviewModal
        open={!!sendReviewModal}
        onClose={() => setSendReviewModal(null)}
        onConfirm={confirmSendToReview}
        assignees={assignees}
        count={sendReviewModal?.type === 'bulk' ? selected.size : 1}
      />

      {/* Run AI Extraction — one click from the list, no need to open the document first */}
      <Modal open={!!runExtractionTarget} onClose={() => setRunExtractionTarget(null)} title="Run AI Extraction" size="sm">
        {runExtractionTarget && (
          <>
            <p className="text-muted text-sm" style={{ marginBottom: 12 }}>
              Re-run AI Extraction for <strong>{runExtractionTarget.filename}</strong>. Pick a document type to use a comprehensive field schema, or leave it on Auto.
            </p>
            <RunExtractionControl
              documentId={runExtractionTarget.id}
              size="md"
              onDone={() => { setRunExtractionTarget(null); refreshAll(); }}
            />
          </>
        )}
      </Modal>
    </PermissionGate>
  );
}

// ── Row kebab menu ───────────────────────────────────────────────

function RowMenu({ row, style, onRename, onSendToReview, onCancelReview, onOpenPipeline, onRunExtraction, onViewExtraction, onCreateDataset, onDelete }) {
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState(null);
  const btnRef = useRef(null);
  const inReview = isInReview(row.status);

  const toggle = () => {
    if (!open && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setCoords({ top: r.bottom + 4, left: r.right - 190 });
    }
    setOpen((o) => !o);
  };

  return (
    <div className="doc-row-menu" style={style}>
      <button ref={btnRef} className="doc-icon-btn" title="More actions" onClick={toggle}>
        <MoreVertical size={14} />
      </button>
      {open && coords && createPortal(
        <>
          <div className="doc-row-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="doc-row-menu-list" style={{ position: 'fixed', top: coords.top, left: coords.left }}>
            <PermissionGate module={MODULES.DOCUMENT_INTELLIGENCE} action={ACTIONS.EDIT} fallback={null}>
              <button disabled={inReview} title={inReview ? 'Resolve or cancel the review first' : undefined} onClick={() => { setOpen(false); onRename(); }}>
                <Pencil size={13} /> Rename / Move
              </button>
            </PermissionGate>
            {inReview ? (
              <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_REVIEW} fallback={null}>
                <button className="doc-row-menu-danger" onClick={() => { setOpen(false); onCancelReview(); }}><XCircle size={13} /> Cancel Review</button>
              </PermissionGate>
            ) : (
              <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_REVIEW} fallback={null}>
                <button onClick={() => { setOpen(false); onSendToReview(); }}><CheckSquare size={13} /> Send to Review</button>
              </PermissionGate>
            )}
            <button onClick={() => { setOpen(false); onOpenPipeline(); }}><GitBranch size={13} /> Open Pipeline</button>
            <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_PROCESS} fallback={null}>
              <button onClick={() => { setOpen(false); onRunExtraction(); }}><Sparkles size={13} /> Run AI Extraction…</button>
            </PermissionGate>
            <button onClick={() => { setOpen(false); onViewExtraction(); }}><Eye size={13} /> View Extraction</button>
            <button onClick={() => { setOpen(false); onCreateDataset(); }}><Database size={13} /> Create Dataset</button>
            <PermissionGate {...DOCUMENT_PERMISSIONS.DOCUMENT_DELETE} fallback={null}>
              <button className="doc-row-menu-danger" disabled={inReview} title={inReview ? 'Resolve or cancel the review first' : undefined} onClick={() => { setOpen(false); onDelete(); }}>
                <Trash2 size={13} /> Delete
              </button>
            </PermissionGate>
          </div>
        </>,
        document.body
      )}
    </div>
  );
}
