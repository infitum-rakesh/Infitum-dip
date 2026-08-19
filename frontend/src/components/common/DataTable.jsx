import { useState } from 'react';
import { ChevronUp, ChevronDown } from 'lucide-react';
import { LoadingSpinner } from './LoadingSpinner.jsx';
import { EmptyState } from './EmptyState.jsx';
import { Pagination } from './Pagination.jsx';
import './DataTable.css';

/**
 * DataTable — reusable, sortable, paginated table.
 *
 * columns: [{ key, header, render?, width?, sortable? }]
 * data:    row objects
 * loading, error, empty: state props
 * pagination: { page, pageSize, total, onChange }
 * onRowClick: (row) => void
 */
export function DataTable({
  columns = [],
  data = [],
  loading = false,
  error = null,
  emptyState = null,
  pagination = null,
  onRowClick,
  className = '',
}) {
  const [sortKey, setSortKey]   = useState(null);
  const [sortDir, setSortDir]   = useState('asc');

  const handleSort = (key) => {
    if (!key) return;
    if (sortKey === key) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    else { setSortKey(key); setSortDir('asc'); }
  };

  const sorted = sortKey
    ? [...data].sort((a, b) => {
        const av = a[sortKey] ?? ''; const bv = b[sortKey] ?? '';
        return sortDir === 'asc'
          ? String(av).localeCompare(String(bv), undefined, { numeric: true })
          : String(bv).localeCompare(String(av), undefined, { numeric: true });
      })
    : data;

  return (
    <div className={`data-table-wrap ${className}`}>
      <div className="data-table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              {columns.map((col) => (
                <th
                  key={col.key}
                  style={col.width ? { width: col.width } : undefined}
                  className={col.sortable ? 'sortable' : ''}
                  onClick={() => col.sortable && handleSort(col.key)}
                >
                  <span className="th-inner">
                    {col.header}
                    {col.sortable && (
                      <span className="sort-icon">
                        {sortKey === col.key
                          ? (sortDir === 'asc' ? <ChevronUp size={12} /> : <ChevronDown size={12} />)
                          : <ChevronUp size={12} style={{ opacity: 0.3 }} />}
                      </span>
                    )}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={columns.length} className="table-state-cell">
                  <LoadingSpinner />
                </td>
              </tr>
            ) : error ? (
              <tr>
                <td colSpan={columns.length} className="table-state-cell table-error">
                  {error}
                </td>
              </tr>
            ) : sorted.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="table-state-cell">
                  {emptyState ?? <EmptyState title="No data" description="Nothing to display." />}
                </td>
              </tr>
            ) : (
              sorted.map((row, idx) => (
                <tr
                  key={row.id ?? idx}
                  onClick={() => onRowClick?.(row)}
                  className={onRowClick ? 'row-clickable' : ''}
                >
                  {columns.map((col) => (
                    <td key={col.key}>
                      {col.render ? col.render(row[col.key], row) : (row[col.key] ?? '—')}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {pagination && !loading && sorted.length > 0 && (
        <div className="table-footer">
          <Pagination
            page={pagination.page}
            pageSize={pagination.pageSize}
            total={pagination.total}
            onChange={pagination.onChange}
          />
        </div>
      )}
    </div>
  );
}
