import { X } from 'lucide-react';

const DOC_TYPES = [
  { value: 'PDF', label: 'PDF' },
  { value: 'IMAGE', label: 'Image' },
  { value: 'EXCEL', label: 'Excel' },
  { value: 'WORD', label: 'Word' },
  { value: 'TEXT', label: 'Text' },
  { value: 'OTHER', label: 'Other' },
];

const STATUSES = [
  { value: 'UPLOADED', label: 'Uploaded' },
  { value: 'SCANNING', label: 'Scanning' },
  { value: 'OCR_PROCESSING', label: 'OCR Processing' },
  { value: 'AI_EXTRACTION', label: 'AI Extraction' },
  { value: 'COMPLETED', label: 'Completed' },
  { value: 'NEEDS_REVIEW', label: 'Needs Review' },
  { value: 'FAILED', label: 'Failed' },
];

const DATE_PRESETS = [
  { value: 'All', label: 'All Time' },
  { value: 'today', label: 'Today' },
  { value: 'yesterday', label: 'Yesterday' },
  { value: 'last_7_days', label: 'Last 7 Days' },
  { value: 'last_30_days', label: 'Last 30 Days' },
  { value: 'custom', label: 'Custom' },
];

const OWNERS = [
  { value: 'All', label: 'All' },
  { value: 'current_user', label: 'Current User' },
  { value: 'organization', label: 'Organization' },
  { value: 'specific_user', label: 'Specific User' },
];

export function DocumentFilters({ filters, onChange, onClear, uploaders = [] }) {
  const set = (key) => (e) => onChange({ ...filters, [key]: e.target.value });
  const setPill = (key, val) => onChange({ ...filters, [key]: val });

  const activeCount = Object.entries(filters).filter(
    ([k, v]) => v && v !== 'All' && !['date_from', 'date_to', 'owner_user_id'].includes(k)
  ).length;

  return (
    <aside className="doc-filters">
      <div className="doc-filters-hdr">
        <span>Filters</span>
        {activeCount > 0 && (
          <button className="doc-filters-clear" onClick={onClear}>
            <X size={12} /> Clear ({activeCount})
          </button>
        )}
      </div>

      {/* Document Type */}
      <div className="doc-filter-group">
        <label className="doc-filter-label">Document Type</label>
        <div className="doc-pill-group">
          <button className={`doc-pill ${!filters.doc_type || filters.doc_type === 'All' ? 'active' : ''}`} onClick={() => setPill('doc_type', 'All')}>All</button>
          {DOC_TYPES.map((t) => (
            <button key={t.value} className={`doc-pill ${filters.doc_type === t.value ? 'active' : ''}`} onClick={() => setPill('doc_type', t.value)}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* Status */}
      <div className="doc-filter-group">
        <label className="doc-filter-label">Status</label>
        <select className="doc-select" value={filters.status ?? 'All'} onChange={set('status')}>
          <option value="All">All</option>
          {STATUSES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>
      </div>

      {/* Date */}
      <div className="doc-filter-group">
        <label className="doc-filter-label">Date</label>
        <select className="doc-select" value={filters.date_preset ?? 'All'} onChange={set('date_preset')}>
          {DATE_PRESETS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
        </select>
        {filters.date_preset === 'custom' && (
          <div className="doc-date-range">
            <input type="date" className="doc-select" value={filters.date_from ?? ''} onChange={set('date_from')} />
            <input type="date" className="doc-select" value={filters.date_to ?? ''} onChange={set('date_to')} />
          </div>
        )}
      </div>

      {/* Owner */}
      <div className="doc-filter-group">
        <label className="doc-filter-label">Owner</label>
        <select className="doc-select" value={filters.owner ?? 'All'} onChange={set('owner')}>
          {OWNERS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        {filters.owner === 'specific_user' && (
          <select className="doc-select" style={{ marginTop: 6 }} value={filters.owner_user_id ?? ''} onChange={set('owner_user_id')}>
            <option value="">Select user…</option>
            {uploaders.map((u) => <option key={u.id} value={u.id}>{u.full_name}</option>)}
          </select>
        )}
      </div>
    </aside>
  );
}

export { DOC_TYPES, STATUSES, DATE_PRESETS, OWNERS };
