/**
 * ZipBatchListModal — "ZIP Batches" quick-access panel, real data from the
 * existing GET .../zip-batches list endpoint (ZB10). Exists so a user who
 * closed the upload dialog (or reopened the app later) can always find
 * their way back to a batch's live progress page — closing the upload
 * dialog never loses the batch, but until this existed there was no way to
 * navigate back to it without knowing the URL.
 */
import { useCallback, useEffect, useState } from 'react';
import { StatusBadge, DataTable } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { BATCH_STATUS_LABEL, BATCH_STATUS_VARIANT } from '@/services/api/zipBatchApi.js';

export function ZipBatchListModal({ zipBatchApi, onOpenBatch }) {
  const [batches, setBatches] = useState({ items: [], total: 0, page: 1, page_size: 20 });
  const [loading, setLoading] = useState(true);

  const load = useCallback((page = 1) => {
    setLoading(true);
    zipBatchApi.listBatches({ page, page_size: 20 })
      .then((r) => { setBatches(r); setLoading(false); })
      .catch(() => setLoading(false));
  }, [zipBatchApi]);

  useEffect(() => { load(1); }, [load]);

  const columns = [
    { key: 'original_filename', header: 'ZIP File' },
    {
      key: 'status', header: 'Status',
      render: (v) => <StatusBadge variant={BATCH_STATUS_VARIANT[v] || 'gray'} dot>{BATCH_STATUS_LABEL[v] || v}</StatusBadge>,
    },
    { key: 'total_entries', header: 'Files' },
    { key: 'completed_files', header: 'Completed' },
    { key: 'failed_files', header: 'Failed' },
    { key: 'created_at', header: 'Uploaded', render: (v) => formatDateTime(v) },
  ];

  return (
    <div className="zb-panel">
      <DataTable
        columns={columns}
        data={batches.items}
        loading={loading}
        onRowClick={(row) => onOpenBatch?.(row.id)}
        pagination={{ page: batches.page, pageSize: batches.page_size, total: batches.total, onChange: load }}
        emptyState={<div style={{ padding: 24, textAlign: 'center', color: 'var(--text-faint)' }}>No ZIP batches uploaded yet.</div>}
      />
    </div>
  );
}
