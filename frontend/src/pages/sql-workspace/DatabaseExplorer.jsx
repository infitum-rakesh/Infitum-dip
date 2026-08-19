/**
 * DatabaseExplorer — SQL Workspace's left-side schema/object browser.
 * Everything here is real, live data from the connected database via the
 * existing connectionApi (getSchemas/getObjects/getColumns) — no
 * fabricated tree nodes. Stored procedures/functions/synonyms are
 * deliberately NOT shown: no connector in this codebase reflects them
 * yet (SQLAlchemy's cross-dialect Inspector has no generic routine-
 * listing API — it would need real, separate per-dialect catalog
 * queries), so this only ever renders what get_objects()/get_columns()
 * can genuinely answer: Tables and Views, with each column's real type/
 * PK/unique/FK metadata.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ChevronRight, ChevronDown, Table2, Eye, Loader2, Search, X,
  MoreVertical, KeyRound, Link2, Copy, FilePlus2, Code2,
} from 'lucide-react';
import { connectionApi } from '@/services/api/connectionApi.js';

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ── Real SQL text generation from real reflected column metadata ───

function qualifiedName(schema, name) {
  return schema ? `${schema}.${name}` : name;
}

function generateSelectSql(schema, name, columns, opts) {
  const qname = qualifiedName(schema, name);
  const colList = opts.includeColumns && columns?.length ? columns.map((c) => c.name).join(',\n    ') : '*';
  let sql = `SELECT\n    ${colList}\nFROM ${qname}`;
  if (opts.addWhere) sql += `\nWHERE 1 = 1`;
  if (opts.addOrderBy) {
    const pk = columns?.find((c) => c.primary_key);
    sql += `\nORDER BY ${pk ? pk.name : (columns?.[0]?.name ?? '1')}`;
  }
  if (opts.addLimit) sql += `\nLIMIT 100`;
  return `${sql};`;
}

function generateInsertSql(schema, name, columns) {
  const writable = (columns || []).filter((c) => !(c.primary_key && /^(nextval|autoincrement)/i.test(c.default || '')));
  const cols = (writable.length ? writable : columns || []).map((c) => c.name);
  const placeholders = cols.map((c) => `<${c}>`);
  return `INSERT INTO ${qualifiedName(schema, name)} (\n    ${cols.join(',\n    ')}\n) VALUES (\n    ${placeholders.join(',\n    ')}\n);`;
}

function generateUpdateSql(schema, name, columns) {
  const pk = (columns || []).find((c) => c.primary_key);
  const setCols = (columns || []).filter((c) => !c.primary_key);
  const setClause = setCols.map((c) => `${c.name} = <${c.name}>`).join(',\n    ') || '<column> = <value>';
  const whereClause = pk ? `${pk.name} = <${pk.name}>` : '<key_column> = <value>';
  return `UPDATE ${qualifiedName(schema, name)}\nSET\n    ${setClause}\nWHERE ${whereClause};`;
}

function generateDeleteSql(schema, name, columns) {
  const pk = (columns || []).find((c) => c.primary_key);
  const whereClause = pk ? `${pk.name} = <${pk.name}>` : '<key_column> = <value>';
  return `DELETE FROM ${qualifiedName(schema, name)}\nWHERE ${whereClause};`;
}

function generateCreateSql(schema, name, columns) {
  const lines = (columns || []).map((c) => {
    let line = `    ${c.name} ${c.data_type}`;
    if (!c.nullable) line += ' NOT NULL';
    if (c.default) line += ` DEFAULT ${c.default}`;
    return line;
  });
  const pkCols = (columns || []).filter((c) => c.primary_key).map((c) => c.name);
  if (pkCols.length) lines.push(`    PRIMARY KEY (${pkCols.join(', ')})`);
  for (const c of columns || []) {
    if (c.foreign_key) {
      lines.push(`    FOREIGN KEY (${c.name}) REFERENCES ${qualifiedName(c.foreign_key.schema, c.foreign_key.table)} (${c.foreign_key.column})`);
    }
  }
  return `CREATE TABLE ${qualifiedName(schema, name)} (\n${lines.join(',\n')}\n);`;
}

// ── Column badge row ─────────────────────────────────────────────

function ColumnRow({ col }) {
  return (
    <div className="sqlw-exp-col">
      <span className="sqlw-exp-col-name">{col.name}</span>
      <span className="sqlw-exp-col-type">{col.data_type}</span>
      <span className="sqlw-exp-col-badges">
        {col.primary_key && <span className="sqlw-badge pk" title="Primary key"><KeyRound size={10} /> PK</span>}
        {col.foreign_key && (
          <span className="sqlw-badge fk" title={`References ${qualifiedName(col.foreign_key.schema, col.foreign_key.table)}.${col.foreign_key.column}`}>
            <Link2 size={10} /> FK
          </span>
        )}
        {!col.primary_key && col.unique && <span className="sqlw-badge unique" title="Unique">UQ</span>}
        {!col.nullable && <span className="sqlw-badge notnull" title="Not nullable">NN</span>}
      </span>
    </div>
  );
}

// ── Object (table/view) row with its own expand + context menu ─────

function ObjectRow({
  connectionId, schema, obj, expanded, onToggle, onGenerateSql, onPreview, onDragStartSql,
}) {
  const [columns, setColumns] = useState(null);
  const [loadingCols, setLoadingCols] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [genPopover, setGenPopover] = useState(null); // {includeColumns, addWhere, addLimit, addOrderBy}
  const menuRef = useRef(null);

  useEffect(() => {
    if (!expanded || columns !== null) return;
    setLoadingCols(true);
    connectionApi.getColumns(connectionId, obj.name, { schema })
      .then((r) => setColumns(r.columns))
      .catch(() => setColumns([]))
      .finally(() => setLoadingCols(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded]);

  useEffect(() => {
    if (!menuOpen && !genPopover) return;
    const close = (e) => { if (menuRef.current && !menuRef.current.contains(e.target)) { setMenuOpen(false); setGenPopover(null); } };
    window.addEventListener('mousedown', close);
    return () => window.removeEventListener('mousedown', close);
  }, [menuOpen, genPopover]);

  const ensureColumns = async () => {
    if (columns) return columns;
    const r = await connectionApi.getColumns(connectionId, obj.name, { schema });
    setColumns(r.columns);
    return r.columns;
  };

  const copyText = (text) => { navigator.clipboard?.writeText(text).catch(() => {}); };

  const Icon = obj.object_type === 'VIEW' ? Eye : Table2;

  return (
    <div className="sqlw-exp-object">
      <div
        className="sqlw-exp-object-row"
        draggable
        onDragStart={async (e) => {
          const cols = await ensureColumns();
          e.dataTransfer.setData('text/plain', generateSelectSql(schema, obj.name, cols, { includeColumns: true, addLimit: true }));
        }}
        onClick={() => onToggle(obj.name)}
        title={qualifiedName(schema, obj.name)}
      >
        {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <Icon size={12} className="sqlw-exp-object-icon" />
        <span className="sqlw-exp-object-name">{obj.name}</span>
        <div className="sqlw-exp-object-menu-wrap" ref={menuRef}>
          <button
            className="sqlw-exp-menu-btn"
            onClick={(e) => { e.stopPropagation(); setMenuOpen((m) => !m); setGenPopover(null); }}
            title="Object actions"
          >
            <MoreVertical size={12} />
          </button>
          {menuOpen && (
            <div className="sqlw-exp-menu" onClick={(e) => e.stopPropagation()}>
              <button onClick={() => setGenPopover({ includeColumns: true, addWhere: false, addLimit: true, addOrderBy: false })}>
                <Code2 size={12} /> Generate SELECT…
              </button>
              <button onClick={async () => { onPreview(schema, obj.name, await ensureColumns()); setMenuOpen(false); }}>
                <Eye size={12} /> Preview Data
              </button>
              {obj.object_type === 'TABLE' && (
                <>
                  <button onClick={async () => { onGenerateSql(generateInsertSql(schema, obj.name, await ensureColumns())); setMenuOpen(false); }}>
                    <FilePlus2 size={12} /> Generate INSERT
                  </button>
                  <button onClick={async () => { onGenerateSql(generateUpdateSql(schema, obj.name, await ensureColumns())); setMenuOpen(false); }}>
                    <FilePlus2 size={12} /> Generate UPDATE
                  </button>
                  <button onClick={async () => { onGenerateSql(generateDeleteSql(schema, obj.name, await ensureColumns())); setMenuOpen(false); }}>
                    <FilePlus2 size={12} /> Generate DELETE
                  </button>
                  <button onClick={async () => { onGenerateSql(generateCreateSql(schema, obj.name, await ensureColumns())); setMenuOpen(false); }}>
                    <Code2 size={12} /> Generate CREATE Script
                  </button>
                </>
              )}
              <div className="sqlw-exp-menu-sep" />
              <button onClick={() => { copyText(obj.name); setMenuOpen(false); }}><Copy size={12} /> Copy Object Name</button>
              <button onClick={() => { copyText(qualifiedName(schema, obj.name)); setMenuOpen(false); }}><Copy size={12} /> Copy Full Name</button>
            </div>
          )}
          {genPopover && (
            <div className="sqlw-exp-genpopover" onClick={(e) => e.stopPropagation()}>
              <label><input type="checkbox" checked={genPopover.includeColumns} onChange={(e) => setGenPopover((p) => ({ ...p, includeColumns: e.target.checked }))} /> Include columns</label>
              <label><input type="checkbox" checked={genPopover.addWhere} onChange={(e) => setGenPopover((p) => ({ ...p, addWhere: e.target.checked }))} /> Add WHERE</label>
              <label><input type="checkbox" checked={genPopover.addLimit} onChange={(e) => setGenPopover((p) => ({ ...p, addLimit: e.target.checked }))} /> Add LIMIT</label>
              <label><input type="checkbox" checked={genPopover.addOrderBy} onChange={(e) => setGenPopover((p) => ({ ...p, addOrderBy: e.target.checked }))} /> Add ORDER BY</label>
              <button
                className="sqlw-exp-genpopover-insert"
                onClick={async () => { onGenerateSql(generateSelectSql(schema, obj.name, await ensureColumns(), genPopover)); setMenuOpen(false); setGenPopover(null); }}
              >
                Insert into Editor
              </button>
            </div>
          )}
        </div>
      </div>
      {expanded && (
        <div className="sqlw-exp-columns">
          {loadingCols && <div className="sqlw-tree-empty small"><Loader2 size={11} className="sqlw-spin" /> Loading columns…</div>}
          {!loadingCols && columns?.length === 0 && <div className="sqlw-tree-empty small">No columns.</div>}
          {!loadingCols && columns?.map((c) => <ColumnRow key={c.name} col={c} />)}
        </div>
      )}
    </div>
  );
}

// ── Schema group ─────────────────────────────────────────────────

function SchemaGroup({ connectionId, schemaName, expanded, onToggleSchema, cache, onLoaded, ...objectProps }) {
  const [loading, setLoading] = useState(false);
  const objects = cache[schemaName];

  useEffect(() => {
    if (!expanded || objects) return;
    setLoading(true);
    connectionApi.getObjects(connectionId, { schema: schemaName })
      .then((r) => onLoaded(schemaName, r.items))
      .catch(() => onLoaded(schemaName, []))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded]);

  const [expandedObj, setExpandedObj] = useState(null);
  const tables = (objects || []).filter((o) => o.object_type === 'TABLE');
  const views = (objects || []).filter((o) => o.object_type === 'VIEW');

  return (
    <div className="sqlw-exp-schema">
      <button className="sqlw-exp-schema-row" onClick={() => onToggleSchema(schemaName)}>
        {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <span>{schemaName}</span>
      </button>
      {expanded && (
        <div className="sqlw-exp-schema-body">
          {loading && <div className="sqlw-tree-empty small"><Loader2 size={12} className="sqlw-spin" /> Loading objects…</div>}
          {!loading && objects && objects.length === 0 && <div className="sqlw-tree-empty small">No tables/views.</div>}
          {!loading && tables.length > 0 && (
            <div className="sqlw-exp-group">
              <div className="sqlw-exp-group-label">Tables ({tables.length})</div>
              {tables.map((o) => (
                <ObjectRow key={o.name} connectionId={connectionId} schema={schemaName} obj={o}
                  expanded={expandedObj === o.name} onToggle={(n) => setExpandedObj((c) => (c === n ? null : n))}
                  {...objectProps} />
              ))}
            </div>
          )}
          {!loading && views.length > 0 && (
            <div className="sqlw-exp-group">
              <div className="sqlw-exp-group-label">Views ({views.length})</div>
              {views.map((o) => (
                <ObjectRow key={o.name} connectionId={connectionId} schema={schemaName} obj={o}
                  expanded={expandedObj === o.name} onToggle={(n) => setExpandedObj((c) => (c === n ? null : n))}
                  {...objectProps} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Search results (cross-schema, debounced) ────────────────────

function SearchResults({ connectionId, schemas, query, ...objectProps }) {
  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!query.trim()) { setResults(null); return; }
    let cancelled = false;
    setLoading(true);
    const effectiveSchemas = schemas.length ? schemas : [null];
    Promise.all(effectiveSchemas.map((s) => connectionApi.getObjects(connectionId, { schema: s ?? undefined, q: query }).catch(() => ({ items: [] }))))
      .then((responses) => {
        if (cancelled) return;
        const flat = [];
        responses.forEach((r, i) => (r.items || []).forEach((o) => flat.push({ ...o, schema: effectiveSchemas[i] ?? o.schema_name })));
        setResults(flat);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [query, connectionId, schemas]);

  if (loading) return <div className="sqlw-tree-empty small"><Loader2 size={12} className="sqlw-spin" /> Searching…</div>;
  if (!results) return null;
  if (results.length === 0) return <div className="sqlw-tree-empty small">No objects match "{query}".</div>;
  return (
    <div className="sqlw-exp-group">
      <div className="sqlw-exp-group-label">{results.length} match{results.length === 1 ? '' : 'es'}</div>
      {results.map((o) => (
        <ObjectRow key={`${o.schema}.${o.name}`} connectionId={connectionId} schema={o.schema} obj={o}
          expanded={false} onToggle={() => {}} {...objectProps} />
      ))}
    </div>
  );
}

// ── Page-level component ────────────────────────────────────────

export default function DatabaseExplorer({ connectionId, refreshKey, onInsertSql, onPreviewObject }) {
  const [schemas, setSchemas] = useState([]);
  const [loadingSchemas, setLoadingSchemas] = useState(false);
  const [expandedSchema, setExpandedSchema] = useState(null);
  const [objectsCache, setObjectsCache] = useState({});
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');

  const debouncedSetSearch = useMemo(() => debounce(setSearch, 300), []);

  useEffect(() => {
    setSchemas([]); setExpandedSchema(null); setObjectsCache({}); setSearchInput(''); setSearch('');
    if (!connectionId) return;
    setLoadingSchemas(true);
    connectionApi.getSchemas(connectionId).then((r) => {
      setSchemas(r.schemas);
      if (r.schemas.length === 1) setExpandedSchema(r.schemas[0]);
      if (r.schemas.length === 0) setExpandedSchema(null); // flat-namespace connector (e.g. Salesforce) — objects load directly
    }).catch(() => setSchemas([])).finally(() => setLoadingSchemas(false));
  }, [connectionId]);

  useEffect(() => {
    if (!connectionId || refreshKey === 0) return;
    setObjectsCache({});
  }, [refreshKey]);

  const objectProps = {
    onGenerateSql: (sql) => onInsertSql(sql),
    onPreview: (schema, name, columns) => onPreviewObject(schema, name, columns),
  };

  if (!connectionId) {
    return <div className="sqlw-tree-empty">Select a connection to browse its schema.</div>;
  }
  if (loadingSchemas) return <div className="sqlw-tree-empty"><Loader2 size={14} className="sqlw-spin" /> Loading schemas…</div>;

  return (
    <div className="sqlw-explorer">
      <div className="sqlw-exp-search">
        <Search size={12} />
        <input
          placeholder="Search objects…"
          value={searchInput}
          onChange={(e) => { setSearchInput(e.target.value); debouncedSetSearch(e.target.value); }}
        />
        {searchInput && <X size={12} className="sqlw-exp-search-clear" onClick={() => { setSearchInput(''); setSearch(''); }} />}
      </div>

      {search.trim() ? (
        <div className="sqlw-tree">
          <SearchResults connectionId={connectionId} schemas={schemas} query={search} {...objectProps} />
        </div>
      ) : schemas.length === 0 ? (
        // No schema concept (e.g. Salesforce) — list objects directly under a synthetic "flat" group.
        <div className="sqlw-tree">
          <SchemaGroup connectionId={connectionId} schemaName="" expanded onToggleSchema={() => {}}
            cache={objectsCache} onLoaded={(s, items) => setObjectsCache((m) => ({ ...m, [s]: items }))}
            {...objectProps} />
        </div>
      ) : (
        <div className="sqlw-tree">
          {schemas.map((s) => (
            <SchemaGroup key={s} connectionId={connectionId} schemaName={s} expanded={expandedSchema === s}
              onToggleSchema={(name) => setExpandedSchema((cur) => (cur === name ? null : name))}
              cache={objectsCache} onLoaded={(sc, items) => setObjectsCache((m) => ({ ...m, [sc]: items }))}
              {...objectProps} />
          ))}
        </div>
      )}
    </div>
  );
}

export { generateSelectSql };
