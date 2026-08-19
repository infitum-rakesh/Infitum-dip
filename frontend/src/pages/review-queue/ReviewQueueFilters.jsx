import { X } from 'lucide-react';

const STATUSES = [
  { value: 'PENDING', label: 'Pending' },
  { value: 'IN_PROGRESS', label: 'In Progress' },
  { value: 'APPROVED', label: 'Approved' },
  { value: 'REJECTED', label: 'Rejected' },
  { value: 'ESCALATED', label: 'Escalated' },
  { value: 'CANCELLED', label: 'Cancelled' },
];

const PRIORITIES = [
  { value: 'CRITICAL', label: 'Critical' },
  { value: 'HIGH', label: 'High' },
  { value: 'MEDIUM', label: 'Medium' },
  { value: 'LOW', label: 'Low' },
];

const REVIEW_TYPES = [
  { value: 'OCR', label: 'OCR' },
  { value: 'AI_EXTRACTION', label: 'AI Extraction' },
  { value: 'VALIDATION', label: 'Validation' },
  { value: 'BUSINESS_RULE', label: 'Business Rule' },
];

const ASSIGNED = [
  { value: 'All', label: 'All' },
  { value: 'unassigned', label: 'Unassigned' },
  { value: 'me', label: 'Me' },
  { value: 'specific_user', label: 'Specific User' },
];

const DOCUMENT_TYPES = [
  { value: 'Invoice', label: 'Invoice' },
  { value: 'Contract', label: 'Contract' },
  { value: 'Purchase Order', label: 'Purchase Order' },
  { value: 'Customer', label: 'Customer' },
  { value: 'Other', label: 'Other' },
];

export function ReviewQueueFilters({ filters, onChange, onClear, assignees = [] }) {
  const set = (key) => (e) => onChange({ ...filters, [key]: e.target.value });
  const setPill = (key, val) => onChange({ ...filters, [key]: val });

  const activeCount = Object.entries(filters).filter(
    ([k, v]) => v && v !== 'All' && k !== 'assigned_user_id'
  ).length;

  return (
    <aside className="rq-filters">
      <div className="rq-filters-hdr">
        <span>Filters</span>
        {activeCount > 0 && (
          <button className="rq-filters-clear" onClick={onClear}>
            <X size={12} /> Clear ({activeCount})
          </button>
        )}
      </div>

      <div className="rq-filter-group">
        <label className="rq-filter-label">Status</label>
        <select className="rq-select" value={filters.status ?? 'All'} onChange={set('status')}>
          <option value="All">All</option>
          {STATUSES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>
      </div>

      <div className="rq-filter-group">
        <label className="rq-filter-label">Priority</label>
        <div className="rq-pill-group">
          <button className={`rq-pill ${!filters.priority || filters.priority === 'All' ? 'active' : ''}`} onClick={() => setPill('priority', 'All')}>All</button>
          {PRIORITIES.map((p) => (
            <button key={p.value} className={`rq-pill rq-pill-${p.value.toLowerCase()} ${filters.priority === p.value ? 'active' : ''}`} onClick={() => setPill('priority', p.value)}>
              {p.label}
            </button>
          ))}
        </div>
      </div>

      <div className="rq-filter-group">
        <label className="rq-filter-label">Review Type</label>
        <select className="rq-select" value={filters.review_type ?? 'All'} onChange={set('review_type')}>
          <option value="All">All</option>
          {REVIEW_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
        </select>
      </div>

      <div className="rq-filter-group">
        <label className="rq-filter-label">Assigned To</label>
        <select className="rq-select" value={filters.assigned ?? 'All'} onChange={set('assigned')}>
          {ASSIGNED.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
        </select>
        {filters.assigned === 'specific_user' && (
          <select className="rq-select" style={{ marginTop: 6 }} value={filters.assigned_user_id ?? ''} onChange={set('assigned_user_id')}>
            <option value="">Select user…</option>
            {assignees.map((u) => <option key={u.id} value={u.id}>{u.full_name}</option>)}
          </select>
        )}
      </div>

      <div className="rq-filter-group">
        <label className="rq-filter-label">Document Type</label>
        <select className="rq-select" value={filters.document_type ?? 'All'} onChange={set('document_type')}>
          <option value="All">All</option>
          {DOCUMENT_TYPES.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
        </select>
      </div>
    </aside>
  );
}

export { STATUSES, PRIORITIES, REVIEW_TYPES, ASSIGNED, DOCUMENT_TYPES };
