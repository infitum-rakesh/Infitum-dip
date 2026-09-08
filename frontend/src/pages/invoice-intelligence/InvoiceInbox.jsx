/**
 * Invoice Inbox — /app/invoice-intel
 * Real list of ApInvoice rows (invoiceApi.list()) materialized from
 * documents that went through the Invoice Intelligence pipeline (real OCR
 * -> real AI Extraction -> invoice_processing_service.materialize_invoice()
 * -> invoice_validation_service -> invoice_decision_service.decide(),
 * INV5-INV7). Upload here reuses the exact same Document Intelligence
 * upload/processing pipeline every other document goes through
 * (invoiceApi.upload() -> POST /invoices/upload), just pinned to the
 * INVOICE taxonomy from the first run — nothing invoice-specific is
 * re-implemented at intake.
 *
 * Row click opens InvoiceDetail (INV14 ships a minimal-but-real detail
 * view; INV15 replaces it with the full workspace + evidence viewer).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Receipt, Upload, X, FileText } from 'lucide-react';
import {
  Button, StatusBadge, DataTable, SearchBox, EmptyState, Modal, PermissionGate, FileUploader,
} from '@/components/common/index.js';
import { PAGINATION, INVOICE_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { invoiceApi, INVOICE_STATUSES, INVOICE_STATUS_LABEL, INVOICE_STATUS_VARIANT } from '@/services/api/invoiceApi.js';
import { documentApi } from '@/services/api/documentApi.js';
import './invoice-intelligence.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

// Real bug found live: `processingDocs` used to live ONLY in this
// component's React state, with nothing backing it — navigating to any
// other module and back remounts InvoiceInbox from scratch, so that state
// (and the "in progress" row it drove) was gone. The background OCR/AI
// Extraction pipeline itself was never affected (document_service.py's
// start_processing() fires a detached asyncio task, fully decoupled from
// this page or even the original HTTP request — see its own docstring),
// but from here it looked exactly like the upload had been silently
// cancelled: no in-progress row, and no real ApInvoice row yet either if
// the pipeline hadn't finished materializing one by the time the user came
// back. Persisting the tracked list to localStorage (keyed per
// organization, so switching orgs in the same browser doesn't cross-show
// another org's in-flight uploads) survives the remount; resuming a real
// poll against each entry on mount (below) confirms it against the
// server's actual current state rather than trusting stale local data —
// self-healing for an entry that already finished (or was deleted) while
// the page was away, same as a poll that started fresh would.
function processingDocsStorageKey() {
  try {
    const org = JSON.parse(localStorage.getItem('dip_organization'));
    return `dip.invoiceInbox.processingDocs.${org?.id ?? 'none'}`;
  } catch {
    return 'dip.invoiceInbox.processingDocs.none';
  }
}
function loadPersistedProcessingDocs() {
  try {
    const raw = localStorage.getItem(processingDocsStorageKey());
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}
function persistProcessingDocs(docs) {
  try {
    const key = processingDocsStorageKey();
    if (docs.length === 0) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(docs));
  } catch { /* localStorage unavailable (private mode, quota, ...) — tracking just won't survive a remount this time */ }
}

function formatCurrency(n, currency = 'USD') {
  if (n === null || n === undefined) return '—';
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(n);
  } catch {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(n);
  }
}

// STATUS_VARIANT used to be a hand-guessed { NEEDS_REVIEW, COMPLETED,
// REJECTED, FAILED } — none of which (bar REJECTED) exist on the real
// ApInvoiceStatus enum, and the Status filter dropdown offered exactly
// those same four wrong values. Found live: selecting anything but
// "Rejected" or "All Statuses" 400'd ("Unknown status '…'"), because
// invoice_inbox_service.list_invoices() validates the filter against the
// actual enum. Now sourced from invoiceApi.js's INVOICE_STATUS_VARIANT,
// the single correct copy also used by InvoiceDetail.jsx.
const STATUS_VARIANT = INVOICE_STATUS_VARIANT;
const DECISION_VARIANT = { STRAIGHT_THROUGH: 'success', REVIEW_REQUIRED: 'warning', REJECT: 'danger', RETRY: 'info' };
const DECISION_LABEL = { STRAIGHT_THROUGH: 'Straight-Through', REVIEW_REQUIRED: 'Review Required', REJECT: 'Rejected', RETRY: 'Retry' };
const ERP_VARIANT = { NOT_SUBMITTED: 'gray', PENDING: 'info', RETRYING: 'warning', SUCCEEDED: 'success', FAILED: 'danger', DEAD_LETTER: 'danger', CANCELLED: 'gray' };

const EMPTY_FILTERS = { status: 'All', decision: 'All', vendor: '', date_from: '', date_to: '' };

export default function InvoiceInbox() {
  const navigate = useNavigate();

  const [stats, setStats] = useState(null);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');

  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [uploadModal, setUploadModal] = useState(false);
  const [uploadStatuses, setUploadStatuses] = useState([]); // [{ id, filename, stage, error }] — modal-local, only covers the upload POST itself
  // Lives on the page, not inside the Modal's children, so it keeps showing
  // "in progress" in the main table grid after the upload popup closes.
  // Found live: closing the popup used to look like it killed the
  // background OCR/AI Extraction pipeline — it never did (that always ran
  // server-side regardless of the popup), only the UI's only window into it
  // disappeared with it. [{ documentId, filename, stage, uploadedAt }]
  //
  // Hydrated from localStorage (see processingDocsStorageKey() above) so a
  // second real bug — navigating to another module and back losing this
  // list entirely — is fixed too; the mount effect below re-attaches a real
  // poll to every hydrated entry so it's re-verified against the server,
  // not just trusted.
  const [processingDocs, setProcessingDocs] = useState(loadPersistedProcessingDocs);
  const pendingUploadCountRef = useRef(0);

  useEffect(() => { persistProcessingDocs(processingDocs); }, [processingDocs]);

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadStats = useCallback(() => { invoiceApi.getStats().then(setStats).catch(() => {}); }, []);
  useEffect(() => { loadStats(); }, [loadStats]);

  const loadItems = useCallback(() => {
    setLoading(true);
    setError(null);
    invoiceApi
      .list({
        q: search,
        status: filters.status,
        decision: filters.decision,
        vendor: filters.vendor || undefined,
        date_from: filters.date_from || undefined,
        date_to: filters.date_to || undefined,
        page, page_size: pageSize,
      })
      .then((res) => { setRows(res.items); setTotal(res.total); })
      .catch((err) => setError(apiErrorMessage(err, 'Failed to load invoices. Please try again.')))
      .finally(() => setLoading(false));
  }, [search, filters, page, pageSize]);

  useEffect(() => { loadItems(); }, [loadItems]);

  const refreshAll = () => { loadStats(); loadItems(); };

  // Shared by a fresh upload (handleUpload below) AND the mount-time resume
  // effect for anything hydrated from localStorage — same real poll either
  // way, just a different starting point (a document_id we already have, no
  // upload step to re-run). Resuming re-verifies against the server instead
  // of trusting the persisted "stage" string: a document that already
  // finished while this page was unmounted returns a terminal status on its
  // very first tick here and is removed immediately, same as it would be if
  // this page had been open the whole time.
  const trackDocumentUntilDone = useCallback((documentId) => {
    documentApi
      .pollUntilDone(documentId, {
        onTick: (doc) => {
          if (!doc) return;
          setProcessingDocs((docs) => docs.map((d) => (d.documentId === documentId ? { ...d, stage: doc.status } : d)));
        },
      })
      // A timeout/transient poll error here just means this synthetic row
      // stops updating early — the real Document/Invoice record is
      // unaffected and still reachable from the Documents page; refreshAll()
      // below still runs so a real invoice that DID materialize shows up.
      .catch(() => {})
      .finally(() => {
        setProcessingDocs((docs) => docs.filter((d) => d.documentId !== documentId));
        refreshAll();
      });
  }, []);

  // Re-attach a real poll to every document that was still "in progress"
  // the last time this page's tracked list was persisted (see
  // processingDocsStorageKey() above) — runs once on mount, over the value
  // React lazily hydrated processingDocs with, not over whatever it grows to
  // afterward (a fresh upload in THIS session already starts its own poll
  // inline in handleUpload, so re-tracking it here too would double it up).
  const initialProcessingDocsRef = useRef(processingDocs);
  useEffect(() => {
    initialProcessingDocsRef.current.forEach((d) => trackDocumentUntilDone(d.documentId));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleFiltersChange = (patch) => { setFilters((f) => ({ ...f, ...patch })); setPage(1); };
  const handleClearFilters  = () => { setFilters(EMPTY_FILTERS); setPage(1); };
  const handleClearSearch   = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const handleUpload = useCallback((file, { onProgress }) => {
    const entryId = `${file.name}_${file.size}_${Math.random().toString(36).slice(2, 8)}`;
    setUploadStatuses((s) => [...s, { id: entryId, filename: file.name, stage: 'uploading', error: null }]);
    pendingUploadCountRef.current += 1;
    return invoiceApi.upload(file, { onProgress }).then((res) => {
      // The upload POST itself succeeded — hand tracking off to
      // processingDocs (below) and drop this file from the modal's own
      // transient list; real OCR/AI Extraction progress from here is
      // tracked against the Document, not anything the modal owns.
      setUploadStatuses((s) => s.filter((e) => e.id !== entryId));
      setProcessingDocs((docs) => [
        ...docs,
        { documentId: res.document_id, filename: res.filename || file.name, stage: res.status || 'UPLOADED', uploadedAt: new Date().toISOString() },
      ]);
      trackDocumentUntilDone(res.document_id);

      // Auto-close the popup once every file queued in this batch has
      // cleared the upload step — the point the user asked for it to close,
      // since everything past this point is now visible in the main grid
      // instead. Left open on a failed upload (below) so the error stays
      // readable instead of vanishing with the popup.
      pendingUploadCountRef.current -= 1;
      if (pendingUploadCountRef.current <= 0) setTimeout(() => setUploadModal(false), 500);
      return res;
    }).catch((err) => {
      setUploadStatuses((s) => s.map((e) => (e.id === entryId ? { ...e, stage: 'error', error: err?.message || 'Upload failed.' } : e)));
      pendingUploadCountRef.current -= 1;
      throw err;
    });
  }, [trackDocumentUntilDone]);

  const columns = [
    {
      key: 'invoice_number', header: 'Invoice #', sortable: true,
      render: (v, row) => (row.__pending
        ? <span className="inv-invoice-link" style={{ color: 'var(--text-faint)', cursor: 'default' }}>{row.document_filename}</span>
        : <button className="inv-invoice-link" onClick={() => navigate(`/app/invoice-intel/${row.id}`)}>{v || `#${row.id}`}</button>),
    },
    { key: 'vendor_name', header: 'Vendor', sortable: true, render: (v) => <span className="inv-vendor-cell">{v || '—'}</span> },
    { key: 'invoice_date', header: 'Invoice Date', sortable: true, render: (v) => (v ? formatDateTime(v).split(',')[0] : '—') },
    { key: 'due_date', header: 'Due Date', render: (v) => (v ? formatDateTime(v).split(',')[0] : '—') },
    { key: 'total', header: 'Total', sortable: true, render: (v, row) => <span className="inv-amount-cell">{formatCurrency(v, row.currency)}</span> },
    {
      key: 'final_decision', header: 'Decision',
      render: (v) => (v ? <StatusBadge variant={DECISION_VARIANT[v] ?? 'gray'} dot>{DECISION_LABEL[v] ?? v}</StatusBadge> : <span style={{ color: 'var(--text-faint)' }}>Processing…</span>),
    },
    {
      key: 'status', header: 'Status',
      render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v?.replace('_', ' ') ?? v}</StatusBadge>,
    },
    {
      key: 'erp_status', header: 'ERP Status',
      render: (v, row) => (row.__pending ? <span style={{ color: 'var(--text-faint)' }}>—</span> : <StatusBadge variant={ERP_VARIANT[v] ?? 'gray'} dot>{v?.replace('_', ' ') ?? v}</StatusBadge>),
    },
    { key: 'document_filename', header: 'Source File', render: (v) => v || '—' },
    { key: 'created_at', header: 'Uploaded', sortable: true, render: (v) => formatDateTime(v) },
  ];

  const kpis = [
    { label: 'Total Invoices', value: stats ? stats.total : '—' },
    { label: 'Needs Review', value: stats ? stats.needs_review : '—', variant: 'warning' },
    { label: 'Straight-Through', value: stats ? stats.straight_through : '—', variant: 'success' },
    { label: 'Rejected', value: stats ? stats.rejected : '—', variant: 'danger' },
    { label: 'Duplicate Blocked', value: stats ? stats.duplicate_blocked : '—', variant: 'danger' },
    { label: 'Total Amount', value: stats ? formatCurrency(stats.total_amount) : '—', variant: 'info' },
  ];

  const hasActiveFilters = search || Object.entries(filters).some(([k, v]) => v && v !== 'All' && k !== 'vendor') || filters.vendor;

  // Synthetic "in flight" rows — a real ApInvoice row doesn't exist until
  // materialization finishes at the end of the pipeline, so these are
  // sourced from the Document itself (documentApi.pollUntilDone(), started
  // in handleUpload) rather than faked. Prepended ahead of the real page of
  // invoices so an upload stays visible here the moment the popup closes.
  const pendingRows = processingDocs.map((d) => ({
    id: `pending-${d.documentId}`,
    __pending: true,
    documentId: d.documentId,
    invoice_number: null, vendor_name: null, invoice_date: null, due_date: null,
    total: null, currency: 'USD', final_decision: null,
    status: d.stage, erp_status: null,
    document_filename: d.filename, created_at: d.uploadedAt,
  }));
  const displayRows = [...pendingRows, ...rows];

  return (
    <PermissionGate {...INVOICE_PERMISSIONS.INVOICE_VIEW} showReason>
      <div className="inv-page">
        <div className="inv-header">
          <div>
            <h1 className="inv-title"><Receipt size={22} /> Invoice Inbox</h1>
            <p className="inv-subtitle">Invoices captured through OCR + AI Extraction, validated and decisioned automatically.</p>
          </div>
          <div className="inv-actions">
            <PermissionGate {...INVOICE_PERMISSIONS.INVOICE_CREATE} fallback={null}>
              <Button variant="primary" icon={<Upload size={14} />} onClick={() => { setUploadStatuses([]); setUploadModal(true); }}>
                Upload Invoice
              </Button>
            </PermissionGate>
          </div>
        </div>

        <div className="inv-kpis">
          {kpis.map((k) => (
            <div key={k.label} className="inv-kpi-card">
              <div className={`inv-kpi-val ${k.variant ? `inv-kpi-val-${k.variant}` : ''}`}>{k.value}</div>
              <div className="inv-kpi-label">{k.label}</div>
            </div>
          ))}
        </div>

        <div className="inv-toolbar">
          <SearchBox
            value={searchInput}
            onChange={setSearchInput}
            placeholder="Search invoice number, vendor, or PO…"
            className="inv-search-box"
          />
          <select className="inv-select" value={filters.status} onChange={(e) => handleFiltersChange({ status: e.target.value })}>
            <option value="All">All Statuses</option>
            {INVOICE_STATUSES.map((s) => <option key={s} value={s}>{INVOICE_STATUS_LABEL[s] ?? s}</option>)}
          </select>
          <select className="inv-select" value={filters.decision} onChange={(e) => handleFiltersChange({ decision: e.target.value })}>
            <option value="All">All Decisions</option>
            <option value="STRAIGHT_THROUGH">Straight-Through</option>
            <option value="REVIEW_REQUIRED">Review Required</option>
            <option value="REJECT">Rejected</option>
            <option value="RETRY">Retry</option>
          </select>
          <input
            className="inv-date-input" type="date" value={filters.date_from}
            onChange={(e) => handleFiltersChange({ date_from: e.target.value })} title="From date"
          />
          <input
            className="inv-date-input" type="date" value={filters.date_to}
            onChange={(e) => handleFiltersChange({ date_to: e.target.value })} title="To date"
          />
          {(searchInput || hasActiveFilters) && (
            <button className="inv-clear-btn" onClick={() => { handleClearSearch(); handleClearFilters(); }}>
              <X size={13} /> Clear
            </button>
          )}
        </div>

        <div className="inv-table-panel">
          <DataTable
            columns={columns}
            data={displayRows}
            loading={loading}
            error={error}
            onRowClick={(row) => { if (!row.__pending) navigate(`/app/invoice-intel/${row.id}`); }}
            pagination={{ page, pageSize, total, onChange: setPage }}
            emptyState={
              <EmptyState
                icon={<Receipt size={28} />}
                title={hasActiveFilters ? 'No matching invoices' : 'No invoices yet'}
                description={hasActiveFilters ? 'Try a different search term or clear your filters.' : 'Upload an invoice to see it flow through OCR, extraction, validation, and decisioning.'}
                action={hasActiveFilters ? { label: 'Clear search & filters', onClick: () => { handleClearSearch(); handleClearFilters(); } } : undefined}
              />
            }
          />
        </div>
      </div>

      <Modal
        open={uploadModal}
        onClose={() => setUploadModal(false)}
        title="Upload Invoice"
        size="md"
        footer={<Button variant="secondary" onClick={() => setUploadModal(false)}>Close</Button>}
      >
        <p className="inv-upload-hint">
          Upload a PDF or image invoice. It runs through real OCR + AI Extraction, then canonical
          materialization, validation, and the decision engine — no shortcuts. This window closes
          automatically once the upload completes; processing continues in the background and shows
          up as an in-progress row in the table behind it.
        </p>
        <FileUploader
          accept={['.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.bmp']}
          multiple
          onUpload={handleUpload}
        />
        {uploadStatuses.map((e) => (
          <div key={e.id} className="inv-upload-progress-row">
            <FileText size={14} />
            <span>{e.filename}</span>
            <StatusBadge variant={e.stage === 'error' ? 'danger' : 'info'} dot>
              {e.stage === 'uploading' && 'Uploading…'}
              {e.stage === 'error' && (e.error || 'Upload failed.')}
            </StatusBadge>
          </div>
        ))}
      </Modal>
    </PermissionGate>
  );
}
