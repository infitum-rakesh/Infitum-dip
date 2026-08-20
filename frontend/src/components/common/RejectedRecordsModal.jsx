import { useEffect, useState } from 'react';
import { Download } from 'lucide-react';
import { Modal } from './Modal.jsx';
import { Pagination } from './Pagination.jsx';
import { Button } from './Button.jsx';
import { LoadingSpinner } from './LoadingSpinner.jsx';
import { EmptyState } from './EmptyState.jsx';
import './RejectedRecordsModal.css';

const PAGE_SIZE = 50;

function csvCell(value) {
  const s = value === null || value === undefined ? '' : String(value);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function toCsv(items) {
  const rowKeys = new Set();
  items.forEach((it) => Object.keys(it.row_data || {}).forEach((k) => rowKeys.add(k)));
  const columns = [...rowKeys, 'error_message', 'created_at'];
  const header = columns.map(csvCell).join(',');
  const lines = items.map((it) =>
    columns.map((c) => csvCell(c === 'error_message' ? it.error_message : c === 'created_at' ? it.created_at : it.row_data?.[c])).join(',')
  );
  return [header, ...lines].join('\n');
}

function downloadCsv(filename, csv) {
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/**
 * RejectedRecordsModal — real per-row failures from a Run Load or Run
 * Pipeline target write (KK2-KK5): the actual row data and the actual
 * error the target gave for each one, not a summary count. `fetchPage`
 * is (page, pageSize) => Promise<{items, total}> — callers wire it to
 * loadApi.getRejectedRecords or pipelineJobApi.getRejectedRecords.
 *
 * CSV export lets the user take the exact bad rows + reasons back to the
 * source system to fix — this is the "rectify" half of the ask; an
 * in-app edit-and-resubmit workflow is a separate, larger feature this
 * doesn't attempt.
 */
export function RejectedRecordsModal({ open, onClose, title, fetchPage, exportFilename }) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState({ items: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setPage(1);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    fetchPage(page, PAGE_SIZE)
      .then((res) => { if (!cancelled) setData(res); })
      .catch(() => { if (!cancelled) setData({ items: [], total: 0 }); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open, page, fetchPage]);

  const handleExportAll = async () => {
    setExporting(true);
    try {
      // Real export of every rejected record, not just the page on screen
      // — pages through the same endpoint at a larger page size.
      const all = [];
      let p = 1;
      // Safety bound: 200 pages * 500/page = 100,000 rows — generous for
      // what a user would realistically review/export at once; a run with
      // more rejected rows than that has a systemic problem worth fixing
      // at the source, not exporting row-by-row.
      for (let guard = 0; guard < 200; guard++) {
        const res = await fetchPage(p, 500);
        all.push(...res.items);
        if (all.length >= res.total || res.items.length === 0) break;
        p += 1;
      }
      downloadCsv(exportFilename || 'rejected_records.csv', toCsv(all));
    } finally {
      setExporting(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title={title} size="xl">
      <div className="rrm-toolbar">
        <span className="rrm-count">{data.total.toLocaleString()} rejected record{data.total === 1 ? '' : 's'}</span>
        <Button variant="secondary" size="sm" icon={<Download size={13} />} loading={exporting} disabled={data.total === 0} onClick={handleExportAll}>
          Export CSV
        </Button>
      </div>

      {loading ? (
        <div className="rrm-loading"><LoadingSpinner /></div>
      ) : data.items.length === 0 ? (
        <EmptyState title="No rejected records" description="Every row in this run landed successfully." />
      ) : (
        <>
          <div className="rrm-table-wrap">
            <table className="rrm-table">
              <thead>
                <tr>
                  <th>Row data</th>
                  <th>Error</th>
                  <th>Rejected at</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((r) => (
                  <tr key={r.id}>
                    <td className="rrm-row-data">
                      {Object.entries(r.row_data || {}).map(([k, v]) => (
                        <div key={k} className="rrm-field">
                          <span className="rrm-field-key">{k}:</span> <span className="rrm-field-val">{v === null || v === undefined ? '—' : String(v)}</span>
                        </div>
                      ))}
                    </td>
                    <td className="rrm-error">{r.error_message}</td>
                    <td className="rrm-ts">{new Date(r.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination page={page} pageSize={PAGE_SIZE} total={data.total} onChange={setPage} />
        </>
      )}
    </Modal>
  );
}
