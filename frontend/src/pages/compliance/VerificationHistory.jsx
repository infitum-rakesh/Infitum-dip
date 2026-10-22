/**
 * VerificationHistory — /app/india-verification/history (Phase Group 5,
 * IV27). A filterable, paginated view over every VerificationCase, plus
 * a CSV/Excel export that uses the exact same filters — see api/
 * compliance.py's _apply_case_filters(), shared by both the list and
 * export routes so "what you see here is exactly what you export."
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { History, Download, ExternalLink } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState, Pagination } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import indiaVerificationApi from '@/services/api/indiaVerificationApi.js';
import './compliance.css';

const STATUS_VARIANT = {
  VERIFIED: 'success', VERIFIED_WITH_WARNINGS: 'warning', PARTIALLY_VERIFIED: 'warning',
  MISMATCH: 'danger', REQUIRES_REVIEW: 'warning', PENDING_PROVIDER: 'info', IN_PROGRESS: 'info',
  NOT_STARTED: 'gray', NOT_CONFIGURED: 'gray', UNAVAILABLE: 'danger', FAILED: 'danger',
};
const CHECK_TYPES = ['', 'PAN', 'GSTIN', 'TAN', 'UDYAM', 'AADHAAR', 'MCA', 'MOA'];
const STATUSES = ['', 'VERIFIED', 'VERIFIED_WITH_WARNINGS', 'PARTIALLY_VERIFIED', 'MISMATCH', 'REQUIRES_REVIEW', 'PENDING_PROVIDER', 'NOT_CONFIGURED', 'UNAVAILABLE', 'FAILED', 'NOT_STARTED', 'IN_PROGRESS'];
const EMPTY_FILTERS = { businessProcess: '', subjectSearch: '', checkType: '', overallStatus: '', dateFrom: '', dateTo: '' };

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function toParams(filters) {
  return {
    business_process: filters.businessProcess || undefined, subject_search: filters.subjectSearch || undefined,
    check_type: filters.checkType || undefined, overall_status: filters.overallStatus || undefined,
    date_from: filters.dateFrom || undefined, date_to: filters.dateTo || undefined,
  };
}

export default function VerificationHistory() {
  const navigate = useNavigate();
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const pageSize = 20;
  const [cases, setCases] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);

  const load = () => {
    setLoading(true);
    indiaVerificationApi.listVerifications({ ...toParams(filters), page, page_size: pageSize })
      .then((r) => { setCases(r.items ?? []); setTotal(r.total ?? 0); setLoading(false); })
      .catch(() => setLoading(false));
  };
  useEffect(() => { load(); }, [page, filters]);

  const setField = (key) => (e) => { setPage(1); setFilters((f) => ({ ...f, [key]: e.target.value })); };

  const handleExport = async (format) => {
    setExporting(true);
    try {
      const blob = await indiaVerificationApi.exportVerifications(format, toParams(filters));
      downloadBlob(blob, `verifications.${format}`);
    } finally {
      setExporting(false);
    }
  };

  const columns = [
    { key: 'id', header: 'Case' },
    { key: 'subject_name', header: 'Subject' },
    { key: 'subject_type', header: 'Type', render: (v) => <StatusBadge variant="gray">{v}</StatusBadge> },
    { key: 'business_process', header: 'Business Process', render: (v) => v || <em>—</em> },
    {
      key: 'check_types', header: 'Checks',
      render: (_v, row) => (
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          {(row.results ?? []).map((r) => <StatusBadge key={r.id} variant="gray">{r.check_type}</StatusBadge>)}
        </div>
      ),
    },
    { key: 'overall_status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'created_at', header: 'Created', render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: '',
      render: (_v, row) => <Button size="sm" variant="secondary" icon={<ExternalLink size={12} />} onClick={() => navigate(`/app/india-verification/cases/${row.id}`)}>View</Button>,
    },
  ];

  return (
    <div className="iv-page">
      <div className="iv-header">
        <div>
          <h1 className="iv-title"><History size={20} /> Verification History</h1>
          <p className="iv-subtitle">Every verification case, filterable by business process, check type, status, subject, and date range.</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="secondary" icon={<Download size={14} />} loading={exporting} onClick={() => handleExport('csv')}>Export CSV</Button>
          <Button variant="secondary" icon={<Download size={14} />} loading={exporting} onClick={() => handleExport('xlsx')}>Export Excel</Button>
        </div>
      </div>

      <div className="iv-build-panel">
        <div className="iv-build-row">
          <div className="iv-build-field">
            <label>Subject</label>
            <input className="iv-inline-input" value={filters.subjectSearch} onChange={setField('subjectSearch')} placeholder="Search subject name…" />
          </div>
          <div className="iv-build-field">
            <label>Business Process</label>
            <input className="iv-inline-input" value={filters.businessProcess} onChange={setField('businessProcess')} placeholder="e.g. VENDOR_ONBOARDING" />
          </div>
          <div className="iv-build-field">
            <label>Check Type</label>
            <select className="iv-inline-select" value={filters.checkType} onChange={setField('checkType')}>
              {CHECK_TYPES.map((t) => <option key={t} value={t}>{t || 'All'}</option>)}
            </select>
          </div>
          <div className="iv-build-field">
            <label>Status</label>
            <select className="iv-inline-select" value={filters.overallStatus} onChange={setField('overallStatus')}>
              {STATUSES.map((s) => <option key={s} value={s}>{s || 'All'}</option>)}
            </select>
          </div>
          <div className="iv-build-field">
            <label>From</label>
            <input className="iv-inline-input" type="date" value={filters.dateFrom} onChange={setField('dateFrom')} />
          </div>
          <div className="iv-build-field">
            <label>To</label>
            <input className="iv-inline-input" type="date" value={filters.dateTo} onChange={setField('dateTo')} />
          </div>
        </div>
      </div>

      <div className="iv-list-table-panel">
        <DataTable
          columns={columns}
          data={cases}
          loading={loading}
          emptyState={<EmptyState icon={<History size={28} />} title="No cases match these filters" description="Try widening the date range or clearing a filter." />}
        />
      </div>
      {total > pageSize && (
        <div style={{ marginTop: 12, display: 'flex', justifyContent: 'center' }}>
          <Pagination page={page} pageSize={pageSize} total={total} onChange={setPage} />
        </div>
      )}
    </div>
  );
}
