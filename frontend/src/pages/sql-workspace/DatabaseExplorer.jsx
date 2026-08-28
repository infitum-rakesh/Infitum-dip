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

// An identifier is only safe to leave unquoted if it's lowercase, starts
// with a letter/underscore, and has no special characters — Postgres (and
// most ANSI dialects) fold an UNQUOTED identifier to lowercase before
// comparing it against the catalog, so a real mixed-case object created
// via a quoted identifier (e.g. an uploaded file's original filename
// becoming a table name — see Run Load / pipeline target-table naming)
// would otherwise generate a SELECT that fails with "relation does not
// exist" the moment it's dragged in or a script is generated for it.
function needsQuoting(name) {
  return !/^[a-z_][a-z0-9_$]*$/.test(name || '');
}

// Quote character is dialect-specific — MySQL uses backticks, SQL Server/
// Fabric use brackets, everything else here (Postgres, Oracle, Snowflake,
// SAP, Vertica, NetSuite SuiteQL) is standard SQL double-quoting.
function quoteIdent(name, connectionType) {
  if (!needsQuoting(name)) return name;
  const str = String(name);
  if (connectionType === 'MYSQL') return `\`${str.replace(/`/g, '``')}\``;
  if (connectionType === 'SQL_SERVER' || connectionType === 'FABRIC') return `[${str.replace(/]/g, ']]')}]`;
  return `"${str.replace(/"/g, '""')}"`;
}

function qualifiedName(schema, name, connectionType) {
  const qName = quoteIdent(name, connectionType);
  return schema ? `${quoteIdent(schema, connectionType)}.${qName}` : qName;
}

function generateSelectSql(schema, name, columns, opts, connectionType) {
  const qname = qualifiedName(schema, name, connectionType);
  const colList = opts.includeColumns && columns?.length
    ? columns.map((c) => quoteIdent(c.name, connectionType)).join(',\n    ')
    : '*';
  let sql = `SELECT\n    ${colList}\nFROM ${qname}`;
  if (opts.addWhere) sql += `\nWHERE 1 = 1`;
  if (opts.addOrderBy) {
    const pk = columns?.find((c) => c.primary_key);
    sql += `\nORDER BY ${quoteIdent(pk ? pk.name : (columns?.[0]?.name ?? '1'), connectionType)}`;
  }
  if (opts.addLimit) sql += `\nLIMIT 100`;
  return `${sql};`;
}

function generateInsertSql(schema, name, columns, connectionType) {
  const writable = (columns || []).filter((c) => !(c.primary_key && /^(nextval|autoincrement)/i.test(c.default || '')));
  const cols = (writable.length ? writable : columns || []).map((c) => c.name);
  const placeholders = cols.map((c) => `<${c}>`);
  const qCols = cols.map((c) => quoteIdent(c, connectionType));
  return `INSERT INTO ${qualifiedName(schema, name, connectionType)} (\n    ${qCols.join(',\n    ')}\n) VALUES (\n    ${placeholders.join(',\n    ')}\n);`;
}

function generateUpdateSql(schema, name, columns, connectionType) {
  const pk = (columns || []).find((c) => c.primary_key);
  const setCols = (columns || []).filter((c) => !c.primary_key);
  const setClause = setCols.map((c) => `${quoteIdent(c.name, connectionType)} = <${c.name}>`).join(',\n    ') || '<column> = <value>';
  const whereClause = pk ? `${quoteIdent(pk.name, connectionType)} = <${pk.name}>` : '<key_column> = <value>';
  return `UPDATE ${qualifiedName(schema, name, connectionType)}\nSET\n    ${setClause}\nWHERE ${whereClause};`;
}

function generateDeleteSql(schema, name, columns, connectionType) {
  const pk = (columns || []).find((c) => c.primary_key);
  const whereClause = pk ? `${quoteIdent(pk.name, connectionType)} = <${pk.name}>` : '<key_column> = <value>';
  return `DELETE FROM ${qualifiedName(schema, name, connectionType)}\nWHERE ${whereClause};`;
}

function generateCreateSql(schema, name, columns, connectionType) {
  const lines = (columns || []).map((c) => {
    let line = `    ${quoteIdent(c.name, connectionType)} ${c.data_type}`;
    if (!c.nullable) line += ' NOT NULL';
    if (c.default) line += ` DEFAULT ${c.default}`;
    return line;
  });
  const pkCols = (columns || []).filter((c) => c.primary_key).map((c) => quoteIdent(c.name, connectionType));
  if (pkCols.length) lines.push(`    PRIMARY KEY (${pkCols.join(', ')})`);
  for (const c of columns || []) {
    if (c.foreign_key) {
      lines.push(`    FOREIGN KEY (${quoteIdent(c.name, connectionType)}) REFERENCES ${qualifiedName(c.foreign_key.schema, c.foreign_key.table, connectionType)} (${quoteIdent(c.foreign_key.column, connectionType)})`);
    }
  }
  return `CREATE TABLE ${qualifiedName(schema, name, connectionType)} (\n${lines.join(',\n')}\n);`;
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
  connectionId, schema, obj, expanded, onToggle, onGenerateSql, onPreview, onDragStartSql, connectionType,
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
          e.dataTransfer.setData('text/plain', generateSelectSql(schema, obj.name, cols, { includeColumns: true, addLimit: true }, connectionType));
        }}
        onClick={() => onToggle(obj.name)}
        title={qualifiedName(schema, obj.name, connectionType)}
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
                  <button onClick={async () => { onGenerateSql(generateInsertSql(schema, obj.name, await ensureColumns(), connectionType)); setMenuOpen(false); }}>
                    <FilePlus2 size={12} /> Generate INSERT
                  </button>
                  <button onClick={async () => { onGenerateSql(generateUpdateSql(schema, obj.name, await ensureColumns(), connectionType)); setMenuOpen(false); }}>
                    <FilePlus2 size={12} /> Generate UPDATE
                  </button>
                  <button onClick={async () => { onGenerateSql(generateDeleteSql(schema, obj.name, await ensureColumns(), connectionType)); setMenuOpen(false); }}>
                    <FilePlus2 size={12} /> Generate DELETE
                  </button>
                  <button onClick={async () => { onGenerateSql(generateCreateSql(schema, obj.name, await ensureColumns(), connectionType)); setMenuOpen(false); }}>
                    <Code2 size={12} /> Generate CREATE Script
                  </button>
                </>
              )}
              <div className="sqlw-exp-menu-sep" />
              <button onClick={() => { copyText(obj.name); setMenuOpen(false); }}><Copy size={12} /> Copy Object Name</button>
              <button onClick={() => { copyText(qualifiedName(schema, obj.name, connectionType)); setMenuOpen(false); }}><Copy size={12} /> Copy Full Name</button>
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
                onClick={async () => { onGenerateSql(generateSelectSql(schema, obj.name, await ensureColumns(), genPopover, connectionType)); setMenuOpen(false); setGenPopover(null); }}
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

export default function DatabaseExplorer({ connectionId, onInsertSql, onPreviewObject, connectionType }) {
  const [schemas, setSchemas] = useState([]);
  const [loadingSchemas, setLoadingSchemas] = useState(false);
  const [expandedSchema, setExpandedSchema] = useState(null);
  const [objectsCache, setObjectsCache] = useState({});
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');

  const debouncedSetSearch = useMemo(() => debounce(setSearch, 300), []);

  // Deliberately does NOT reset/reload on every query execution — this
  // tree stays exactly as populated for as long as `connectionId` stays
  // the same (i.e. for the whole session on this connection), only
  // re-fetching when the user actually switches to a different
  // connection. An earlier version bumped a `refreshKey` prop after every
  // successful Run to bust `objectsCache` (in case a DDL/write changed
  // the schema), but that only ever emptied the visible tree — the fetch
  // that would repopulate it is gated on `expanded` changing, which a
  // cache-only reset never triggers — so the tree just went blank and
  // stayed blank until the user manually collapsed/re-expanded the
  // schema. Reported as "schema browser disappears after running a
  // query." If a real DDL-triggered refresh is wanted later, it needs to
  // also re-fetch the currently-expanded schema, not just clear the
  // cache; for now the schema tree simply persists for the connection
  // session, matching what most SQL IDEs do (explicit refresh, not an
  // implicit one on every statement).
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

  const objectProps = {
    onGenerateSql: (sql) => onInsertSql(sql),
    onPreview: (schema, name, columns) => onPreviewObject(schema, name, columns),
    connectionType,
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
