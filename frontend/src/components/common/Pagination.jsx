import { ChevronLeft, ChevronRight } from 'lucide-react';

/** Standard pagination bar */
export function Pagination({ page, pageSize, total, onChange }) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to   = Math.min(page * pageSize, total);

  return (
    <div className="pagination">
      <span className="pagination-info">
        {total === 0 ? 'No results' : `${from}–${to} of ${total.toLocaleString()}`}
      </span>
      <div className="pagination-btns">
        <button
          className="pg-btn"
          onClick={() => onChange(page - 1)}
          disabled={page <= 1}
          aria-label="Previous page"
        >
          <ChevronLeft size={14} />
        </button>
        {Array.from({ length: Math.min(totalPages, 5) }, (_, i) => {
          const n = i + 1;
          return (
            <button
              key={n}
              className={`pg-btn ${n === page ? 'pg-btn-active' : ''}`}
              onClick={() => onChange(n)}
            >
              {n}
            </button>
          );
        })}
        <button
          className="pg-btn"
          onClick={() => onChange(page + 1)}
          disabled={page >= totalPages}
          aria-label="Next page"
        >
          <ChevronRight size={14} />
        </button>
      </div>
    </div>
  );
}
