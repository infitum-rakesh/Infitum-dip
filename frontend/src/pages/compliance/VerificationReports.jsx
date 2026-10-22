/**
 * VerificationReports — /app/india-verification/reports (Phase Group 5,
 * IV27). Not a new audit trail — scopes the existing, already-merged
 * Audit Logs view (auditLogApi.js, backend audit_log_service.py) down to
 * module="India Verification", the module every record_event() call in
 * api/compliance.py already writes under. Reusing this instead of a
 * parallel "compliance audit log" page/table avoids a second source of
 * truth for the same underlying AuditLog rows.
 */
import { useEffect, useState } from 'react';
import { ScrollText, Download } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { auditLogApi } from '@/services/api/auditLogApi.js';
import './compliance.css';

const MODULE = 'India Verification';
const STATUS_VARIANT = { SUCCESS: 'success', FAILURE: 'danger', WARNING: 'warning', INFO: 'info' };

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

export default function VerificationReports() {
  const [entries, setEntries] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    setLoading(true);
    auditLogApi.list({ module: MODULE, page: 1, page_size: 50 })
      .then((r) => { setEntries(r.items ?? []); setTotal(r.total ?? 0); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  const handleExport = async (format) => {
    setExporting(true);
    try {
      const blob = await auditLogApi.export(format, { module: MODULE });
      downloadBlob(blob, `india-verification-audit-trail.${format}`);
    } finally {
      setExporting(false);
    }
  };

  const columns = [
    { key: 'timestamp', header: 'When', render: (v) => formatDateTime(v) },
    { key: 'user_name', header: 'Actor', render: (v) => v || 'System' },
    { key: 'action', header: 'Action' },
    { key: 'resource', header: 'Resource', render: (_v, row) => row.resource ? `${row.resource}${row.resource_id ? ` #${row.resource_id}` : ''}` : '—' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'details', header: 'Details', render: (v) => v || '—' },
  ];

  return (
    <div className="iv-page">
      <div className="iv-header">
        <div>
          <h1 className="iv-title"><ScrollText size={20} /> Reports &amp; Audit Trail</h1>
          <p className="iv-subtitle">
            Every India Verification action recorded in this organization's audit trail — case creation, checks run
            or retried, rules published/activated, batches started, exports. Scoped from the platform's single
            merged Audit Logs source, not a separate log.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="secondary" icon={<Download size={14} />} loading={exporting} onClick={() => handleExport('csv')}>Export CSV</Button>
          <Button variant="secondary" icon={<Download size={14} />} loading={exporting} onClick={() => handleExport('xlsx')}>Export Excel</Button>
        </div>
      </div>

      <div className="iv-list-table-panel">
        <DataTable
          columns={columns}
          data={entries}
          loading={loading}
          emptyState={<EmptyState icon={<ScrollText size={28} />} title="No audit entries yet" description="Actions taken in Compliance & Verification will show up here." />}
        />
      </div>
      {total > entries.length && <p className="iv-text-muted" style={{ marginTop: 8 }}>Showing {entries.length} of {total} — open Administration → Audit Logs for full pagination and search.</p>}
    </div>
  );
}
