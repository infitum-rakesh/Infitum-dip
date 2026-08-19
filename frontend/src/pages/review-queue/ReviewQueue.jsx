/**
 * Review Queue — /app/doc-intel/review
 * Human-in-the-loop workflow: documents/fields that failed the Document
 * Intelligence confidence/validation check land here for human review
 * before they're allowed into the structured dataset.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { X, CheckSquare, XCircle, UserPlus } from 'lucide-react';
import {
  Button, StatusBadge, DataTable, SearchBox, EmptyState, Modal, PermissionGate,
} from '@/components/common/index.js';
import { PAGINATION, REVIEW_QUEUE_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { useAuth } from '@/hooks/useAuth.js';
import { reviewQueueApi } from '@/services/api/reviewQueueApi.js';
import { ReviewQueueFilters } from './ReviewQueueFilters.jsx';
import './review-queue.css';

const PRIORITY_VARIANT = { CRITICAL: 'danger', HIGH: 'warning', MEDIUM: 'info', LOW: 'gray' };
const STATUS_VARIANT = { PENDING: 'gray', IN_PROGRESS: 'info', APPROVED: 'success', REJECTED: 'danger', ESCALATED: 'warning', CANCELLED: 'gray' };
const STATUS_LABEL = { PENDING: 'Pending', IN_PROGRESS: 'In Progress', APPROVED: 'Approved', REJECTED: 'Rejected', ESCALATED: 'Escalated', CANCELLED: 'Cancelled' };

const EMPTY_FILTERS = { status: 'All', priority: 'All', review_type: 'All', assigned: 'All', assigned_user_id: '', document_type: 'All' };

function ageLabel(minutes) {
  if (minutes < 60) return `${minutes} min${minutes === 1 ? '' : 's'}`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

export default function ReviewQueue() {
  const navigate = useNavigate();
  const { user } = useAuth();

  const [stats, setStats] = useState(null);
  const [facets, setFacets] = useState(null);

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
  const [rejectModal, setRejectModal] = useState(false);
  const [rejectReason, setRejectReason] = useState('');

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadStats = useCallback(() => { reviewQueueApi.getStats().then(setStats).catch(() => {}); }, []);
  const loadFacets = useCallback(() => { reviewQueueApi.getFacets().then(setFacets).catch(() => {}); }, []);

  useEffect(() => { loadStats(); loadFacets(); }, [loadStats, loadFacets]);

  const loadItems = useCallback(() => {
    setLoading(true);
    setError(null);
    reviewQueueApi
      .list({
        q: search,
        status: filters.status,
        priority: filters.priority,
        review_type: filters.review_type,
        assigned: filters.assigned,
        assigned_user_id: filters.assigned_user_id || undefined,
        document_type: filters.document_type,
        page, page_size: pageSize,
      })
      .then((res) => { setRows(res.items); setTotal(res.total); })
      .catch(() => setError('Failed to load the review queue. Please try again.'))
      .finally(() => setLoading(false));
  }, [search, filters, page, pageSize]);

  useEffect(() => { loadItems(); }, [loadItems]);

  const refreshAll = () => { loadStats(); loadItems(); };

  const handleFiltersChange = (next) => { setFilters(next); setPage(1); };
  const handleClearFilters  = () => { setFilters(EMPTY_FILTERS); setPage(1); };
  const handleClearSearch   = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const toggleRow = (id) => setSelected((s) => {
    const next = new Set(s);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });
  const toggleAll = () => setSelected((s) => (s.size === rows.length ? new Set() : new Set(rows.map((r) => r.id))));
  const clearSelection = () => setSelected(new Set());

  const bulkApprove = async () => {
    const ids = Array.from(selected);
    if (!ids.length) return;
    await reviewQueueApi.bulkApprove(ids);
    clearSelection();
    refreshAll();
  };
  const bulkAssignToMe = async () => {
    const ids = Array.from(selected);
    if (!ids.length) return;
    // "Assign Selected" with no target picker in the toolbar assigns to the
    // acting reviewer — the common case; use the per-row action for others.
    await reviewQueueApi.bulkAssign(ids, user.id);
    clearSelection();
    refreshAll();
  };
  const openBulkReject = () => setRejectModal(true);
  const submitBulkReject = async () => {
    const ids = Array.from(selected);
    if (!ids.length || !rejectReason.trim()) return;
    await reviewQueueApi.bulkReject(ids, rejectReason.trim());
    setRejectModal(false);
    setRejectReason('');
    clearSelection();
    refreshAll();
  };

  const columns = [
    {
      key: '_select',
      header: <input type="checkbox" checked={rows.length > 0 && selected.size === rows.length} onChange={toggleAll} onClick={(e) => e.stopPropagation()} />,
      width: 34,
      render: (_v, row) => <input type="checkbox" checked={selected.has(row.id)} onChange={() => toggleRow(row.id)} onClick={(e) => e.stopPropagation()} />,
    },
    {
      key: 'priority', header: 'Priority', sortable: true,
      render: (v) => <StatusBadge variant={PRIORITY_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge>,
    },
    { key: 'document_name', header: 'Document' },
    { key: 'document_type', header: 'Document Type' },
    { key: 'issue', header: 'Issue' },
    { key: 'field_name', header: 'Field', render: (v) => v ?? '—' },
    { key: 'extracted_value', header: 'Extracted Value', render: (v) => v || <span className="text-muted">missing</span> },
    { key: 'confidence', header: 'Confidence', render: (v) => v == null ? '—' : `${v}%` },
    { key: 'expected_value', header: 'Expected Value', render: (v) => v ?? '—' },
    { key: 'assigned_to_name', header: 'Assigned To', render: (v) => v ?? 'Unassigned' },
    { key: 'created_at', header: 'Created', sortable: true, render: (v) => formatDateTime(v) },
    { key: 'age_minutes', header: 'Age', render: (v) => ageLabel(v) },
    {
      key: 'status', header: 'Status',
      render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{STATUS_LABEL[v] ?? v}</StatusBadge>,
    },
    {
      key: 'actions', header: 'Action',
      render: (_v, row) => (
        <Button variant="secondary" size="sm" onClick={(e) => { e.stopPropagation(); navigate(`/app/doc-intel/review/${row.id}`); }}>
          Review
        </Button>
      ),
    },
  ];

  const kpis = [
    { label: 'Pending Review', value: stats ? stats.pending_review : '—' },
    { label: 'Assigned to Me', value: stats ? stats.assigned_to_me : '—', variant: 'info' },
    { label: 'High Priority', value: stats ? stats.high_priority : '—', variant: 'warning' },
    { label: 'Overdue', value: stats ? stats.overdue : '—', variant: 'danger' },
    { label: 'Approved Today', value: stats ? stats.approved_today : '—', variant: 'success' },
    { label: 'Rejected Today', value: stats ? stats.rejected_today : '—', variant: 'danger' },
  ];

  const hasActiveFilters = search || Object.entries(filters).some(([k, v]) => v && v !== 'All' && k !== 'assigned_user_id');

  return (
    <PermissionGate {...REVIEW_QUEUE_PERMISSIONS.REVIEW_QUEUE_VIEW} showReason>
      <div className="rq-page">
        {/* Header */}
        <div className="rq-header">
          <div>
            <h1 className="rq-title">Review Queue</h1>
            <p className="rq-subtitle">Review documents and AI-extracted data that require human validation.</p>
          </div>
        </div>

        {/* KPIs */}
        <div className="rq-kpis">
          {kpis.map((k) => (
            <div key={k.label} className="rq-kpi-card">
              <div className={`rq-kpi-val ${k.variant ? `rq-kpi-val-${k.variant}` : ''}`}>{k.value}</div>
              <div className="rq-kpi-label">{k.label}</div>
            </div>
          ))}
        </div>

        {/* Search */}
        <div className="rq-search-row">
          <SearchBox
            value={searchInput}
            onChange={setSearchInput}
            placeholder="Search by document, issue, field, value, status or assignee…"
            className="rq-search-box"
          />
          {searchInput && (
            <button className="rq-clear-search-btn" onClick={handleClearSearch}>
              <X size={13} /> Clear
            </button>
          )}
        </div>

        {/* Bulk action bar */}
        {selected.size > 0 && (
          <div className="rq-bulk-bar">
            <span>{selected.size} selected</span>
            <div className="rq-bulk-actions">
              <PermissionGate {...REVIEW_QUEUE_PERMISSIONS.REVIEW_QUEUE_ASSIGN} fallback={null}>
                <Button variant="secondary" size="sm" icon={<UserPlus size={13} />} onClick={bulkAssignToMe}>Assign to Me</Button>
              </PermissionGate>
              <PermissionGate {...REVIEW_QUEUE_PERMISSIONS.REVIEW_QUEUE_APPROVE} fallback={null}>
                <Button variant="secondary" size="sm" icon={<CheckSquare size={13} />} onClick={bulkApprove}>Approve Selected</Button>
              </PermissionGate>
              <PermissionGate {...REVIEW_QUEUE_PERMISSIONS.REVIEW_QUEUE_REJECT} fallback={null}>
                <Button variant="danger" size="sm" icon={<XCircle size={13} />} onClick={openBulkReject}>Reject Selected</Button>
              </PermissionGate>
              <Button variant="ghost" size="sm" onClick={clearSelection}>Clear</Button>
            </div>
          </div>
        )}

        {/* Body: filters + table */}
        <div className="rq-body">
          <ReviewQueueFilters filters={filters} onChange={handleFiltersChange} onClear={handleClearFilters} assignees={facets?.assignees} />

          <div className="rq-table-panel">
            <DataTable
              columns={columns}
              data={rows}
              loading={loading}
              error={error}
              onRowClick={(row) => navigate(`/app/doc-intel/review/${row.id}`)}
              pagination={{ page, pageSize, total, onChange: setPage }}
              emptyState={
                <EmptyState
                  icon={<CheckSquare size={28} />}
                  title={hasActiveFilters ? 'No matching review items' : 'Queue is empty'}
                  description={hasActiveFilters ? 'Try a different search term or clear your filters.' : 'Nothing needs human review right now.'}
                  action={hasActiveFilters ? { label: 'Clear search & filters', onClick: () => { handleClearSearch(); handleClearFilters(); } } : undefined}
                />
              }
            />
          </div>
        </div>
      </div>

      <Modal
        open={rejectModal}
        onClose={() => setRejectModal(false)}
        title={`Reject ${selected.size} Item${selected.size === 1 ? '' : 's'}`}
        size="sm"
        footer={<><Button variant="secondary" onClick={() => setRejectModal(false)}>Cancel</Button><Button variant="danger" onClick={submitBulkReject} disabled={!rejectReason.trim()}>Reject</Button></>}
      >
        <label className="rq-filter-label" style={{ display: 'block', marginBottom: 6 }}>Rejection reason (required)</label>
        <textarea
          className="rq-select" style={{ width: '100%', minHeight: 80, resize: 'vertical' }}
          value={rejectReason} onChange={(e) => setRejectReason(e.target.value)}
          placeholder="Explain why these items are being rejected…"
          autoFocus
        />
      </Modal>
    </PermissionGate>
  );
}
