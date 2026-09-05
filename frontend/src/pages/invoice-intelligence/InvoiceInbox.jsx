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
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Receipt, Upload, X, FileText } from 'lucide-react';
import {
  Button, StatusBadge, DataTable, SearchBox, EmptyState, Modal, PermissionGate, FileUploader,
} from '@/components/common/index.js';
import { PAGINATION, INVOICE_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { invoiceApi } from '@/services/api/invoiceApi.js';
import './invoice-intelligence.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

function formatCurrency(n, currency = 'USD') {
  if (n === null || n === undefined) return '—';
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(n);
  } catch {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(n);
  }
}

const STATUS_VARIANT = { NEEDS_REVIEW: 'warning', COMPLETED: 'success', REJECTED: 'danger', FAILED: 'danger' };
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
  const [uploadStatuses, setUploadStatuses] = useState([]); // [{ id, filename, stage, error }]

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

  const handleFiltersChange = (patch) => { setFilters((f) => ({ ...f, ...patch })); setPage(1); };
  const handleClearFilters  = () => { setFilters(EMPTY_FILTERS); setPage(1); };
  const handleClearSearch   = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const handleUpload = useCallback((file, { onProgress }) => {
    const entryId = `${file.name}_${file.size}_${Math.random().toString(36).slice(2, 8)}`;
    setUploadStatuses((s) => [...s, { id: entryId, filename: file.name, stage: 'uploading', error: null }]);
    return invoiceApi.upload(file, { onProgress }).then((res) => {
      setUploadStatuses((s) => s.map((e) => (e.id === entryId ? { ...e, stage: 'processing', documentId: res.document_id } : e)));
      invoiceApi
        .pollForInvoice(res.document_id)
        .then(() => {
          setUploadStatuses((s) => s.map((e) => (e.id === entryId ? { ...e, stage: 'done' } : e)));
          refreshAll();
        })
        .catch((err) => {
          setUploadStatuses((s) => s.map((e) => (e.id === entryId ? { ...e, stage: 'error', error: err?.message || 'Processing failed.' } : e)));
        });
      return res;
    });
  }, []);

  const columns = [
    {
      key: 'invoice_number', header: 'Invoice #', sortable: true,
      render: (v, row) => <button className="inv-invoice-link" onClick={() => navigate(`/app/invoice-intel/${row.id}`)}>{v || `#${row.id}`}</button>,
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
      render: (v) => <StatusBadge variant={ERP_VARIANT[v] ?? 'gray'} dot>{v?.replace('_', ' ') ?? v}</StatusBadge>,
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
            <option value="NEEDS_REVIEW">Needs Review</option>
            <option value="COMPLETED">Completed</option>
            <option value="REJECTED">Rejected</option>
            <option value="FAILED">Failed</option>
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
            data={rows}
            loading={loading}
            error={error}
            onRowClick={(row) => navigate(`/app/invoice-intel/${row.id}`)}
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
          materialization, validation, and the decision engine — no shortcuts.
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
            <StatusBadge
              variant={e.stage === 'done' ? 'success' : e.stage === 'error' ? 'danger' : 'info'}
              dot
            >
              {e.stage === 'uploading' && 'Uploading…'}
              {e.stage === 'processing' && 'Processing…'}
              {e.stage === 'done' && 'Ready'}
              {e.stage === 'error' && (e.error || 'Failed')}
            </StatusBadge>
          </div>
        ))}
      </Modal>
    </PermissionGate>
  );
}
