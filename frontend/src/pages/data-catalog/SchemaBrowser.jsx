import { Database, FileText, Cloud, FileBox, X } from 'lucide-react';

const SOURCE_TYPE_ICON = {
  Database: Database,
  File:     FileText,
  API:      Cloud,
  Document: FileBox,
};

const QUALITY_LABEL = { high: 'High (90%+)', medium: 'Medium (70–89%)', low: 'Low (<70%)' };

/**
 * SchemaBrowser — catalog filter panel.
 * Source Type is rendered as pill buttons (per spec); everything else as selects.
 * `facets` comes from GET /data-catalog/facets, `filters` + `onChange` are controlled.
 */
export function SchemaBrowser({ facets, filters, onChange, onClear }) {
  const set = (key) => (e) => onChange({ ...filters, [key]: e.target.value });
  const setSourceType = (val) => onChange({ ...filters, source_type: val });

  const activeCount = Object.values(filters).filter((v) => v && v !== 'All').length;

  return (
    <aside className="dc-filters">
      <div className="dc-filters-hdr">
        <span>Filters</span>
        {activeCount > 0 && (
          <button className="dc-filters-clear" onClick={onClear}>
            <X size={12} /> Clear ({activeCount})
          </button>
        )}
      </div>

      {/* Source Type — pill group */}
      <div className="dc-filter-group">
        <label className="dc-filter-label">Source Type</label>
        <div className="dc-pill-group">
          <button
            className={`dc-pill ${!filters.source_type || filters.source_type === 'All' ? 'active' : ''}`}
            onClick={() => setSourceType('All')}
          >
            All
          </button>
          {(facets?.source_type ?? []).map((t) => {
            const Icon = SOURCE_TYPE_ICON[t] ?? Database;
            return (
              <button
                key={t}
                className={`dc-pill ${filters.source_type === t ? 'active' : ''}`}
                onClick={() => setSourceType(t)}
              >
                <Icon size={12} /> {t}
              </button>
            );
          })}
        </div>
      </div>

      <FilterSelect label="Environment"  value={filters.environment}  onChange={set('environment')}  options={facets?.environment} />
      <FilterSelect label="Database"     value={filters.database}     onChange={set('database')}     options={facets?.database} />
      <FilterSelect label="Schema"       value={filters.schema}       onChange={set('schema')}       options={facets?.schema} />
      <FilterSelect label="Asset Type"   value={filters.asset_type}   onChange={set('asset_type')}   options={facets?.asset_type} />
      <FilterSelect label="Source System" value={filters.source}      onChange={set('source')}       options={facets?.source} />
      <FilterSelect label="Owner"        value={filters.owner}        onChange={set('owner')}        options={facets?.owner} />
      <FilterSelect label="Status"       value={filters.status}       onChange={set('status')}       options={facets?.status} />

      <div className="dc-filter-group">
        <label className="dc-filter-label">Data Quality</label>
        <select className="dc-select" value={filters.quality ?? 'All'} onChange={set('quality')}>
          <option value="All">All</option>
          {(facets?.quality ?? []).map((q) => (
            <option key={q} value={q}>{QUALITY_LABEL[q] ?? q}</option>
          ))}
        </select>
      </div>

      <div className="dc-filter-group">
        <label className="dc-filter-label">Tags</label>
        <select className="dc-select" value={filters.tag ?? 'All'} onChange={set('tag')}>
          <option value="All">All</option>
          {(facets?.tags ?? []).map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
      </div>
    </aside>
  );
}

function FilterSelect({ label, value, onChange, options = [] }) {
  return (
    <div className="dc-filter-group">
      <label className="dc-filter-label">{label}</label>
      <select className="dc-select" value={value ?? 'All'} onChange={onChange}>
        <option value="All">All</option>
        {(options ?? []).map((o) => (
          <option key={o} value={o}>{o}</option>
        ))}
      </select>
    </div>
  );
}
