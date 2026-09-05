/**
 * SourceDatasetConfig — the config-panel body for the 'source' node type.
 * Real, connection-backed schema/object browsing, column introspection and
 * bounded preview (see backend/app/connectors/database/base.py) — nothing
 * here is mocked. Table/View/Query/File object types are fully wired;
 * Stored Procedure/API are listed per spec but have no connector yet (see
 * connectors/registry.py), so selecting one shows an honest "not yet
 * supported" notice instead of a fake browse/preview UI.
 *
 * File sources (Phase 5) reuse the exact same object/column/preview
 * endpoints Table/View browsing already uses — a "File" object IS one
 * matched file (see connectors/files/base.py), so no separate UI plumbing
 * was needed there. The one real difference: cfg.object doubles as a
 * match pattern for execution (glob or "re:<regex>" — see
 * execution/pipeline_executor.py), so it's a free-text field here, not a
 * strict picker — Browse Files lists what currently matches, but typing
 * "*.csv" to process every matching file at run time is expected, not an
 * error, even though Browse Columns/Preview need one literal, existing
 * filename to actually open.
 *
 * File sources also offer a direct "Upload File" option (see
 * services/api/pipelineFileApi.js + backend/app/services/
 * pipeline_file_service.py) as an alternative to browsing an existing
 * SFTP connection — real upload, real delimiter sniffing, real Excel
 * sheet names, uniquely linked to this pipeline once it's first saved
 * (see TransformationDesigner.jsx's handleSave). The two file sub-modes
 * are mutually exclusive per node: once a file is uploaded, the
 * connection-based browsing UI is hidden rather than shown alongside it.
 * Running extraction against an uploaded file (RunExtractionPanel below)
 * isn't wired — that panel talks to a Connection, which an upload
 * doesn't have — so it's simply not shown in upload mode rather than
 * shown broken.
 */
import { useEffect, useState } from 'react';
import { Search, Play, Columns3, Plus, X, Loader2, AlertTriangle, Upload, FileSpreadsheet, Trash2 } from 'lucide-react';
import { connectionApi } from '@/services/api/connectionApi.js';
import { pipelineFileApi } from '@/services/api/pipelineFileApi.js';
import { RunExtractionPanel } from './RunExtractionPanel.jsx';
import './pipeline.css';

const OBJECT_TYPES = [
  { value: 'TABLE', label: 'Table', supported: true },
  { value: 'VIEW', label: 'View', supported: true },
  { value: 'QUERY', label: 'Custom Query', supported: true },
  { value: 'FILE', label: 'File', supported: true },
  { value: 'API', label: 'API', supported: true },
  // NETSUITE_SEARCH: NetSuite SOAP saved searches — a genuinely different
  // shape (record type + saved search, no columns until you run it) from
  // Table/View, but reuses the same cfg.schema/cfg.object node-config
  // keys execution_service.py already expects for source_kind
  // "NETSUITE_SEARCH" — see NetSuiteSoapConnector.list_schemas/list_objects.
  { value: 'NETSUITE_SEARCH', label: 'NetSuite Saved Search', supported: true },
  { value: 'STORED_PROCEDURE', label: 'Stored Procedure', supported: false },
];
// NetSuiteSoapConnector.list_schemas() returns this same list server-side
// as a *convenience* set, not an exhaustive one — any other valid NetSuite
// record type name is still accepted; kept here too so the free-text
// Record Type field can suggest them without a round trip.
const NETSUITE_COMMON_RECORD_TYPES = [
  'customer', 'vendor', 'employee', 'contact', 'partner', 'lead',
  'transaction', 'salesOrder', 'invoice', 'purchaseOrder', 'estimate',
  'creditMemo', 'vendorBill', 'vendorPayment', 'customerPayment',
  'item', 'inventoryItem', 'journalEntry', 'opportunity', 'task',
];
const PREVIEW_LIMITS = [10, 50, 100, 1000];
const LOAD_TYPES = ['BULK', 'AUTO', 'ROW_BY_ROW'];
const HTTP_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];
const PAGINATION_STYLES = [
  { value: 'none', label: 'None — single request' },
  { value: 'offset', label: 'Offset (offset/limit)' },
  { value: 'page', label: 'Page number' },
  { value: 'cursor', label: 'Cursor' },
  { value: 'token', label: 'Token' },
];

function paramsToObject(parameters) {
  return Object.fromEntries((parameters || []).filter((p) => p.name).map((p) => [p.name, p.value]));
}
function apiErrorMessage(err, fallback) {
  // apiClient's response interceptor already normalises Axios/FastAPI
  // errors into { status, message, data } — see services/api/apiClient.js.
  return err?.message || fallback;
}

export function SourceDatasetConfig({ cfg, set, pipelineId, nodeId, pipelineDirty }) {
  const [connections, setConnections] = useState([]);
  const [connectionsLoading, setConnectionsLoading] = useState(true);

  const [schemas, setSchemas] = useState([]);
  const [schemasLoading, setSchemasLoading] = useState(false);
  const [schemaError, setSchemaError] = useState('');
  // Manual-entry fallback: some sources (NetSuite's SuiteAnalytics Connect
  // ODBC driver especially — see connectors/database/netsuite_suiteql.py)
  // can be slow enough enumerating tables/saved-searches that the browse
  // request times out server-side, or the account's catalog is just too
  // large/unusual to browse successfully at all. Rather than leave the
  // picker permanently stuck once that happens, an error on either fetch
  // auto-switches that field to a plain text input (still writing straight
  // into cfg.schema/cfg.object), and a manual "Type it in instead" link
  // offers the same switch even before any error, for a user who already
  // knows the exact name and doesn't want to wait on the browse call at
  // all. Found live: "when i configuring source object as netsuite then
  // still its not able to fetch schema list... allow to enter object name
  // manually."
  const [manualSchema, setManualSchema] = useState(false);
  const [manualObject, setManualObject] = useState(false);

  const [objects, setObjects] = useState([]);
  const [objectSearch, setObjectSearch] = useState('');
  const [objectsLoading, setObjectsLoading] = useState(false);
  const [objectError, setObjectError] = useState('');

  const [columns, setColumns] = useState(null);
  const [rowCount, setRowCount] = useState(null);
  const [columnsLoading, setColumnsLoading] = useState(false);
  const [columnsError, setColumnsError] = useState('');

  const [previewLimit, setPreviewLimit] = useState(50);
  const [preview, setPreview] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState('');

  const [uploadedFile, setUploadedFile] = useState(null);
  const [uploadedFileLoading, setUploadedFileLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');

  const selectedConnection = connections.find((c) => c.id === cfg.connectionId) ?? null;
  const objectTypeDef = OBJECT_TYPES.find((t) => t.value === cfg.objectType) ?? OBJECT_TYPES[0];
  const isTableOrView = cfg.objectType === 'TABLE' || cfg.objectType === 'VIEW';
  const isQuery = cfg.objectType === 'QUERY';
  const isFile = cfg.objectType === 'FILE';
  const isApi = cfg.objectType === 'API';
  const isNetSuiteSearch = cfg.objectType === 'NETSUITE_SEARCH';
  // Some TABLE-shaped connectors genuinely have no schema/catalog concept
  // (Salesforce's list_schemas() deliberately returns [] — SObjects aren't
  // grouped into schemas). Once a real (successful, non-error) schema
  // lookup comes back empty, treat that as "browse objects directly"
  // rather than leaving the object picker permanently unreachable behind
  // a schema selection that will never have an option.
  const noSchemaConcept = isTableOrView && !schemasLoading && !schemaError && schemas.length === 0;

  useEffect(() => {
    // DATABASE (Table/View/Query) and STORAGE (File — SFTP/Local File)
    // connections both feed this one picker; which object types apply is
    // decided by the Object Type select below, not by pre-filtering here.
    connectionApi.list({ page_size: 200 })
      .then((r) => setConnections(r.items))
      .catch(() => {})
      .finally(() => setConnectionsLoading(false));
  }, []);

  useEffect(() => {
    setColumns(null); setRowCount(null); setColumnsError('');
    setPreview(null); setPreviewError('');
  }, [cfg.connectionId, cfg.schema, cfg.object, cfg.objectType, cfg.customSql]);

  // Default back to browsing on a genuinely new connection/object-type
  // selection — only an actual fetch error (handled where each fetch is
  // kicked off, above) should auto-switch into manual entry.
  useEffect(() => {
    setManualSchema(false);
    setManualObject(false);
  }, [cfg.connectionId, cfg.objectType]);

  // Hydrate the uploaded-file panel from the backend — covers both
  // reopening this node's properties within the same session and loading
  // a previously-saved pipeline (cfg.uploadedFileId round-trips with it).
  useEffect(() => {
    if (!isFile || !cfg.uploadedFileId) { setUploadedFile(null); return; }
    setUploadedFileLoading(true);
    pipelineFileApi.getByNode(nodeId)
      .then((r) => setUploadedFile(r))
      .catch(() => setUploadedFile(null))
      .finally(() => setUploadedFileLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId, isFile, cfg.uploadedFileId]);

  const handleUploadFile = async (file) => {
    if (!file) return;
    setUploading(true);
    setUploadError('');
    try {
      const record = await pipelineFileApi.upload(file, nodeId, pipelineId);
      setUploadedFile(record);
      set('uploadedFileId', record.id);
      // Uploading replaces any connection-based file browsing for this node.
      set('connectionId', null);
      set('object', '');
    } catch (err) {
      setUploadError(apiErrorMessage(err, 'Upload failed.'));
    } finally {
      setUploading(false);
    }
  };

  const handleSheetChange = async (sheetName) => {
    if (!uploadedFile) return;
    try {
      const record = await pipelineFileApi.setSheet(uploadedFile.id, sheetName);
      setUploadedFile(record);
    } catch (err) {
      setUploadError(apiErrorMessage(err, 'Could not switch sheet.'));
    }
  };

  const handleRemoveUploadedFile = async () => {
    if (!uploadedFile) return;
    try {
      await pipelineFileApi.remove(uploadedFile.id);
    } catch {
      // best-effort — still clear locally so the user can upload a replacement
    }
    setUploadedFile(null);
    set('uploadedFileId', null);
  };

  useEffect(() => {
    if (!cfg.connectionId || !isTableOrView) { setSchemas([]); return; }
    setSchemasLoading(true); setSchemaError('');
    connectionApi.getSchemas(cfg.connectionId)
      .then((r) => setSchemas(r.schemas))
      .catch((err) => { setSchemaError(apiErrorMessage(err, 'Could not load schemas.')); setManualSchema(true); })
      .finally(() => setSchemasLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg.connectionId, cfg.objectType]);

  useEffect(() => {
    // isFile: no schema concept at all. isTableOrView + noSchemaConcept:
    // schemas genuinely loaded empty (e.g. Salesforce) — browse objects
    // directly rather than waiting on a schema selection that can never
    // be made. Otherwise (a real multi-schema connector, or NetSuite's
    // record-type-as-schema model) still require cfg.schema to be picked
    // first — NetSuiteSoapConnector.list_objects() itself refuses to run
    // without one.
    const canBrowse = isFile
      ? !!cfg.connectionId
      : isNetSuiteSearch
        ? (!!cfg.connectionId && !!cfg.schema)
        : (!!cfg.connectionId && isTableOrView && (!!cfg.schema || noSchemaConcept));
    if (!canBrowse) { setObjects([]); return; }
    setObjectsLoading(true); setObjectError('');
    const t = setTimeout(() => {
      // For FILE, cfg.object itself IS the match pattern — reuses the
      // same connector.list_files(pattern) glob/regex matching execution
      // uses, so "what would this match right now" is always accurate.
      connectionApi.getObjects(cfg.connectionId, { schema: isFile ? undefined : (cfg.schema || undefined), q: (isFile ? cfg.object : objectSearch) || undefined })
        .then((r) => setObjects(r.items))
        .catch((err) => { setObjectError(apiErrorMessage(err, 'Could not load objects.')); setManualObject(true); })
        .finally(() => setObjectsLoading(false));
    }, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg.connectionId, cfg.schema, cfg.objectType, objectSearch, noSchemaConcept, isFile ? cfg.object : null]);

  const loadColumns = async () => {
    if (!cfg.connectionId || !cfg.object) return;
    setColumnsLoading(true); setColumnsError('');
    try {
      const r = await connectionApi.getColumns(cfg.connectionId, cfg.object, { schema: cfg.schema || undefined, include_row_count: true });
      setColumns(r.columns);
      setRowCount(r.row_count);
    } catch (err) {
      setColumnsError(apiErrorMessage(err, 'Could not load columns.'));
    } finally {
      setColumnsLoading(false);
    }
  };

  const runPreview = async () => {
    if (!cfg.connectionId) return;
    setPreviewLoading(true); setPreviewError(''); setPreview(null);
    try {
      let r;
      if (isApi) {
        r = await connectionApi.previewApi(cfg.connectionId, {
          endpoint: cfg.apiEndpoint, method: cfg.apiMethod || 'GET',
          query_params: paramsToObject(cfg.apiQueryParams), headers: paramsToObject(cfg.apiHeaders),
          body: isSoap ? (cfg.apiBody || undefined) : undefined,
          json_path: cfg.apiJsonPath || undefined, pagination: cfg.apiPagination || { style: 'none' },
          limit: previewLimit,
        });
      } else {
        const body = isQuery
          ? { sql: cfg.customSql, parameters: paramsToObject(cfg.parameters), limit: previewLimit }
          : { schema_name: cfg.schema || undefined, object_name: cfg.object, limit: previewLimit };
        r = await connectionApi.preview(cfg.connectionId, body);
      }
      setPreview(r);
    } catch (err) {
      setPreviewError(apiErrorMessage(err, 'Preview failed.'));
    } finally {
      setPreviewLoading(false);
    }
  };

  const setParam = (idx, key, value) => {
    const next = [...(cfg.parameters || [])];
    next[idx] = { ...next[idx], [key]: value };
    set('parameters', next);
  };
  const addParam = () => set('parameters', [...(cfg.parameters || []), { name: '', value: '' }]);
  const removeParam = (idx) => set('parameters', (cfg.parameters || []).filter((_, i) => i !== idx));

  // API query params / headers use the exact same {name,value} list shape,
  // stored under their own config keys so they don't collide with a
  // Custom Query node's SQL `parameters`.
  const setListItem = (key, idx, field, value) => {
    const next = [...(cfg[key] || [])];
    next[idx] = { ...next[idx], [field]: value };
    set(key, next);
  };
  const addListItem = (key) => set(key, [...(cfg[key] || []), { name: '', value: '' }]);
  const removeListItem = (key, idx) => set(key, (cfg[key] || []).filter((_, i) => i !== idx));

  const pagination = cfg.apiPagination || { style: 'none' };
  const setPagination = (field, value) => set('apiPagination', { ...pagination, [field]: value });

  // SOAP operation parameters — SoapApiConnector.fetch_api()'s `body` kwarg
  // becomes the operation's keyword arguments (e.g. {searchId: "123"}).
  // Edited here as the same {name,value} row list every other param list
  // in this panel uses (cfg.apiBodyParams), with cfg.apiBody kept in sync
  // as the real plain-object form execution/pipeline_job_executor.py's
  // real whole-pipeline run already reads directly (`cfg.get("apiBody")`,
  // passed straight through as fetch_api's body= — no name/value
  // conversion happens there, so the object form has to be genuinely
  // correct in storage, not just assembled at send time).
  const isSoap = selectedConnection?.connection_type === 'SOAP';
  const setBodyParam = (idx, field, value) => {
    const next = [...(cfg.apiBodyParams || [])];
    next[idx] = { ...next[idx], [field]: value };
    set('apiBodyParams', next);
    set('apiBody', paramsToObject(next));
  };
  const addBodyParam = () => {
    const next = [...(cfg.apiBodyParams || []), { name: '', value: '' }];
    set('apiBodyParams', next);
    set('apiBody', paramsToObject(next));
  };
  const removeBodyParam = (idx) => {
    const next = (cfg.apiBodyParams || []).filter((_, i) => i !== idx);
    set('apiBodyParams', next);
    set('apiBody', paramsToObject(next));
  };

  return (
    <>
      {/* Connection */}
      <div className="pc-config-row">
        <label className="pc-config-label">Connection</label>
        <select
          className="pc-config-select"
          value={cfg.connectionId ?? ''}
          onChange={(e) => { set('connectionId', e.target.value ? Number(e.target.value) : null); set('schema', ''); set('object', ''); }}
        >
          <option value="">{connectionsLoading ? 'Loading…' : 'Select a connection…'}</option>
          {connections.map((c) => (
            <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>
          ))}
        </select>
        {selectedConnection?.status === 'UNSUPPORTED' && (
          <span className="sdc-inline-warning"><AlertTriangle size={11} /> This connection type has no working connector yet — browsing/preview will fail.</span>
        )}
      </div>

      {/* Object type */}
      <div className="pc-config-row">
        <label className="pc-config-label">Object Type</label>
        <select className="pc-config-select" value={cfg.objectType} onChange={(e) => { set('objectType', e.target.value); set('schema', ''); set('object', ''); }}>
          {OBJECT_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}{t.supported ? '' : ' (later phase)'}</option>)}
        </select>
      </div>

      {!objectTypeDef.supported && (
        <div className="sdc-unsupported-note">
          <AlertTriangle size={13} />
          <span>{objectTypeDef.label} sources aren't implemented yet — Table, View, Custom Query, File and API are supported today.</span>
        </div>
      )}

      {/* Table / View browsing */}
      {isTableOrView && cfg.connectionId && (
        <>
          {noSchemaConcept ? (
            <div className="sdc-unsupported-note">
              <span>This connection type has no schema/catalog concept — browsing objects directly.</span>
            </div>
          ) : (
            <div className="pc-config-row">
              <label className="pc-config-label">Schema</label>
              {manualSchema ? (
                <input
                  className="pc-config-input" placeholder="Type the exact schema name…"
                  value={cfg.schema} onChange={(e) => { set('schema', e.target.value); set('object', ''); }}
                />
              ) : (
                <select className="pc-config-select" value={cfg.schema} onChange={(e) => { set('schema', e.target.value); set('object', ''); }}>
                  <option value="">{schemasLoading ? 'Loading…' : 'Select a schema…'}</option>
                  {schemas.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              )}
              {schemaError && <span className="sdc-error-text">{schemaError} — enter it manually below.</span>}
              <button type="button" className="sdc-link-btn" onClick={() => setManualSchema((v) => !v)}>
                {manualSchema ? 'Browse from connection instead' : "Can't find it? Type it in instead"}
              </button>
            </div>
          )}

          {(cfg.schema || noSchemaConcept || manualObject) && (
            <div className="pc-config-row">
              <label className="pc-config-label">Object ({objectTypeDef.label})</label>
              {manualObject ? (
                <input
                  className="pc-config-input" placeholder={`Type the exact ${objectTypeDef.label.toLowerCase()} name…`}
                  value={cfg.object} onChange={(e) => set('object', e.target.value)}
                />
              ) : (
                <>
                  <div className="sdc-search-box">
                    <Search size={12} />
                    <input placeholder="Search tables/views…" value={objectSearch} onChange={(e) => setObjectSearch(e.target.value)} />
                  </div>
                  <select className="pc-config-select" style={{ marginTop: 6 }} value={cfg.object} onChange={(e) => set('object', e.target.value)}>
                    <option value="">{objectsLoading ? 'Loading…' : `Select ${objectTypeDef.label.toLowerCase()}…`}</option>
                    {objects.filter((o) => o.object_type === cfg.objectType).map((o) => (
                      <option key={o.name} value={o.name}>{o.name}</option>
                    ))}
                  </select>
                </>
              )}
              {objectError && <span className="sdc-error-text">{objectError} — enter it manually below.</span>}
              <button type="button" className="sdc-link-btn" onClick={() => setManualObject((v) => !v)}>
                {manualObject ? 'Browse from connection instead' : "Can't find it? Type it in instead"}
              </button>
            </div>
          )}

          {cfg.object && (
            <div className="pc-config-row">
              <button type="button" className="sdc-secondary-btn" onClick={loadColumns} disabled={columnsLoading}>
                {columnsLoading ? <Loader2 size={12} className="sdc-spin" /> : <Columns3 size={12} />}
                {columnsLoading ? 'Loading columns…' : 'Browse Columns'}
              </button>
              {columnsError && <span className="sdc-error-text">{columnsError}</span>}
              {columns && (
                <div className="sdc-columns-table-wrap">
                  {rowCount != null && <div className="sdc-row-count">{rowCount.toLocaleString()} rows</div>}
                  <table className="sdc-columns-table">
                    <thead><tr><th>Column</th><th>Type</th><th></th></tr></thead>
                    <tbody>
                      {columns.map((c) => (
                        <tr key={c.name}>
                          <td className="sdc-col-name">{c.name}</td>
                          <td className="sdc-col-type">{c.data_type}</td>
                          <td className="sdc-col-badges">
                            {c.primary_key && <span className="sdc-badge sdc-badge-pk" title="Primary key">PK</span>}
                            {c.unique && <span className="sdc-badge sdc-badge-uq" title="Unique">UQ</span>}
                            {!c.nullable && <span className="sdc-badge sdc-badge-nn" title="Not nullable">NN</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {/* NetSuite Saved Search — record type + saved search, genuinely
          different shape from Table/View: no column metadata exists
          separately from actually running the search (get_columns()
          raises server-side), so there's no Browse Columns step here —
          Preview below is the real way to see both columns and sample
          rows together. */}
      {isNetSuiteSearch && cfg.connectionId && (
        <>
          <div className="pc-config-row">
            <label className="pc-config-label">Record Type</label>
            <input
              className="pc-config-input" list="netsuite-record-types"
              placeholder="e.g. customer, salesOrder…"
              value={cfg.schema || ''}
              onChange={(e) => { set('schema', e.target.value); set('object', ''); }}
            />
            <datalist id="netsuite-record-types">
              {NETSUITE_COMMON_RECORD_TYPES.map((t) => <option key={t} value={t} />)}
            </datalist>
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              Any valid NetSuite record type works, not just the suggestions above.
            </span>
          </div>

          {cfg.schema && (
            <div className="pc-config-row">
              <label className="pc-config-label">Saved Search</label>
              {manualObject ? (
                <input
                  className="pc-config-input" placeholder="Type the exact saved search's internal ID…"
                  value={cfg.object} onChange={(e) => set('object', e.target.value)}
                />
              ) : (
                <>
                  <div className="sdc-search-box">
                    <Search size={12} />
                    <input placeholder="Search saved searches…" value={objectSearch} onChange={(e) => setObjectSearch(e.target.value)} />
                  </div>
                  <select className="pc-config-select" style={{ marginTop: 6 }} value={cfg.object} onChange={(e) => set('object', e.target.value)}>
                    <option value="">{objectsLoading ? 'Loading…' : 'Select a saved search…'}</option>
                    {objects.map((o) => <option key={o.name} value={o.name}>{o.name}</option>)}
                  </select>
                </>
              )}
              {objectError && <span className="sdc-error-text">{objectError} — enter it manually below.</span>}
              <button type="button" className="sdc-link-btn" onClick={() => setManualObject((v) => !v)}>
                {manualObject ? 'Browse from connection instead' : "Can't find it? Type it in instead"}
              </button>
            </div>
          )}
        </>
      )}

      {/* Upload File — real upload, real delimiter/sheet detection, no
          connection required. See services/api/pipelineFileApi.js. */}
      {isFile && (
        <div className="pc-config-row">
          <label className="pc-config-label">Upload File</label>

          {uploadedFileLoading ? (
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}><Loader2 size={12} className="sdc-spin" /> Loading…</span>
          ) : uploadedFile ? (
            <div className="sdc-uploaded-file-card">
              <div className="sdc-uploaded-file-row">
                <FileSpreadsheet size={14} />
                <span className="sdc-uploaded-file-name">{uploadedFile.filename}</span>
                <button type="button" className="sdc-icon-btn" title="Remove file" onClick={handleRemoveUploadedFile}><Trash2 size={12} /></button>
              </div>
              <div className="sdc-uploaded-file-meta">
                <span><strong>Extension:</strong> .{uploadedFile.extension}</span>
                <span><strong>Size:</strong> {(uploadedFile.size_bytes / 1024).toFixed(1)} KB</span>
                {uploadedFile.delimiter && (
                  <span><strong>Delimiter:</strong> <code>{uploadedFile.delimiter === '\t' ? '\\t (tab)' : uploadedFile.delimiter}</code></span>
                )}
                {uploadedFile.row_count != null && (
                  <span><strong>Rows × Cols:</strong> {uploadedFile.row_count.toLocaleString()} × {uploadedFile.column_count}</span>
                )}
              </div>
              {uploadedFile.sheet_names && uploadedFile.sheet_names.length > 0 && (
                <div className="sdc-uploaded-file-sheet">
                  <label className="pc-config-label" style={{ marginBottom: 3 }}>Sheet</label>
                  <select className="pc-config-select" value={uploadedFile.selected_sheet ?? ''} onChange={(e) => handleSheetChange(e.target.value)}>
                    {uploadedFile.sheet_names.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                </div>
              )}
              <span className="sdc-uploaded-file-note">
                Uploaded {!cfg.uploadedFileId ? '' : (pipelineId ? '— linked to this pipeline.' : '— will be linked to this pipeline once you Save.')}
              </span>
            </div>
          ) : (
            <>
              <label className="sdc-secondary-btn" style={{ cursor: uploading ? 'default' : 'pointer', display: 'inline-flex' }}>
                {uploading ? <Loader2 size={12} className="sdc-spin" /> : <Upload size={12} />}
                {uploading ? 'Uploading…' : 'Upload File'}
                <input
                  type="file" style={{ display: 'none' }} disabled={uploading}
                  accept=".csv,.tsv,.txt,.xlsx,.xls,.json,.jsonl"
                  onChange={(e) => { handleUploadFile(e.target.files?.[0]); e.target.value = ''; }}
                />
              </label>
              <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 5, display: 'block' }}>
                CSV, TSV, TXT, Excel (.xlsx/.xls) or JSON. Delimiter and sheet names are detected automatically —
                or browse an existing SFTP connection below instead.
              </span>
            </>
          )}
          {uploadError && <span className="sdc-error-text">{uploadError}</span>}
        </div>
      )}

      {/* File / SFTP File browsing — same object/column/preview endpoints
          as Table/View, no schema concept */}
      {isFile && !cfg.uploadedFileId && cfg.connectionId && (
        <>
          <div className="pc-config-row">
            <label className="pc-config-label">File Name / Match Pattern</label>
            <div className="sdc-search-box">
              <Search size={12} />
              <input
                placeholder="e.g. orders.csv or *.csv or re:^orders_\d+\.csv$"
                value={cfg.object || ''}
                onChange={(e) => set('object', e.target.value)}
              />
            </div>
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              A literal name matches one file (needed for Browse Columns/Preview below). A glob or
              <code> re:</code> pattern matches every file it fits when this runs — one partition per matched file.
            </span>
            {objectError && <span className="sdc-error-text">{objectError}</span>}
            {(objectsLoading || objects.length > 0) && (
              <div className="sdc-columns-table-wrap">
                {objectsLoading
                  ? <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>Loading matching files…</span>
                  : (
                    <table className="sdc-columns-table">
                      <thead><tr><th>Matching files ({objects.length})</th></tr></thead>
                      <tbody>
                        {objects.map((o) => (
                          <tr key={o.name} onClick={() => set('object', o.name)} style={{ cursor: 'pointer' }}>
                            <td className="sdc-col-name">{o.name}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
              </div>
            )}
          </div>

          {cfg.object && (
            <div className="pc-config-row">
              <button type="button" className="sdc-secondary-btn" onClick={loadColumns} disabled={columnsLoading}>
                {columnsLoading ? <Loader2 size={12} className="sdc-spin" /> : <Columns3 size={12} />}
                {columnsLoading ? 'Loading columns…' : 'Browse Columns'}
              </button>
              {columnsError && <span className="sdc-error-text">{columnsError}</span>}
              {columns && (
                <div className="sdc-columns-table-wrap">
                  <table className="sdc-columns-table">
                    <thead><tr><th>Column</th><th>Type</th></tr></thead>
                    <tbody>
                      {columns.map((c) => (
                        <tr key={c.name}><td className="sdc-col-name">{c.name}</td><td className="sdc-col-type">{c.data_type}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          <div className="pc-config-row">
            <label className="pc-config-label">Archive Directory</label>
            <input
              className="pc-config-input" placeholder="e.g. /upload/archive (optional)"
              value={cfg.archiveDir || ''} onChange={(e) => set('archiveDir', e.target.value)}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              If set, a file is genuinely moved here (via the connector's own move) once its extraction succeeds. Left blank, files stay in place.
            </span>
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Error Directory</label>
            <input
              className="pc-config-input" placeholder="e.g. /upload/errors (optional)"
              value={cfg.errorDir || ''} onChange={(e) => set('errorDir', e.target.value)}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              If set, a file that fails extraction is moved here instead, so it isn't silently re-matched by the next run.
            </span>
          </div>
        </>
      )}

      {/* REST API — real, paginated HTTP source (connectors/api/rest.py).
          SOAP shares this same objectType="API" branch (see
          connectors/api/soap.py's docstring — SoapApiConnector.fetch_api()
          deliberately mirrors RestApiConnector's contract so the exact
          same Source Dataset UI drives both), but a SOAP call is
          operation-based, not URL/verb-based — the REST-only controls
          (Method, Query Parameters, Headers, Pagination, Rate Limit
          Delay) are all silently ignored by SoapApiConnector.fetch_api()
          and hidden here instead of shown-but-broken. Auth (None/API Key/
          Bearer/Basic/OAuth2/WS-Security) is inherited entirely from the
          Connection — nothing secret is entered again here. */}
      {isApi && cfg.connectionId && (
        <>
          {!isSoap && (
            <div className="pc-config-row">
              <label className="pc-config-label">HTTP Method</label>
              <select className="pc-config-select" style={{ width: 120 }} value={cfg.apiMethod || 'GET'} onChange={(e) => set('apiMethod', e.target.value)}>
                {HTTP_METHODS.map((m) => <option key={m}>{m}</option>)}
              </select>
            </div>
          )}
          <div className="pc-config-row">
            <label className="pc-config-label">{isSoap ? 'SOAP Operation Name' : 'Endpoint'} <span style={{ color: 'var(--color-danger)' }}>*</span></label>
            <input
              className="pc-config-input" style={{ fontFamily: 'var(--font-mono)' }}
              placeholder={isSoap ? 'GetCustomers' : '/customers'} value={cfg.apiEndpoint || ''} onChange={(e) => set('apiEndpoint', e.target.value)}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              {isSoap ? 'The operation name as defined in the connection\'s WSDL (e.g. "GetCustomers").' : 'Appended to the connection\'s Base URL.'}
            </span>
          </div>

          {isSoap ? (
            <div className="pc-config-row">
              <label className="pc-config-label">Operation Parameters</label>
              {(cfg.apiBodyParams || []).map((p, idx) => (
                <div key={idx} className="sdc-param-row">
                  <input className="pc-config-input" placeholder="name" value={p.name} onChange={(e) => setBodyParam(idx, 'name', e.target.value)} />
                  <input className="pc-config-input" placeholder="value" value={p.value} onChange={(e) => setBodyParam(idx, 'value', e.target.value)} />
                  <button type="button" className="sdc-icon-btn" onClick={() => removeBodyParam(idx)}><X size={12} /></button>
                </div>
              ))}
              <button type="button" className="sdc-secondary-btn" onClick={addBodyParam}><Plus size={12} /> Add Parameter</button>
              <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
                Passed as this operation's keyword arguments (e.g. <code>searchId</code>, <code>pageIndex</code>) — SOAP has no query-string/header concept.
              </span>
            </div>
          ) : (
            <>
              <div className="pc-config-row">
                <label className="pc-config-label">Query Parameters</label>
                {(cfg.apiQueryParams || []).map((p, idx) => (
                  <div key={idx} className="sdc-param-row">
                    <input className="pc-config-input" placeholder="name" value={p.name} onChange={(e) => setListItem('apiQueryParams', idx, 'name', e.target.value)} />
                    <input className="pc-config-input" placeholder="value" value={p.value} onChange={(e) => setListItem('apiQueryParams', idx, 'value', e.target.value)} />
                    <button type="button" className="sdc-icon-btn" onClick={() => removeListItem('apiQueryParams', idx)}><X size={12} /></button>
                  </div>
                ))}
                <button type="button" className="sdc-secondary-btn" onClick={() => addListItem('apiQueryParams')}><Plus size={12} /> Add Parameter</button>
              </div>

              <div className="pc-config-row">
                <label className="pc-config-label">Headers</label>
                {(cfg.apiHeaders || []).map((h, idx) => (
                  <div key={idx} className="sdc-param-row">
                    <input className="pc-config-input" placeholder="name" value={h.name} onChange={(e) => setListItem('apiHeaders', idx, 'name', e.target.value)} />
                    <input className="pc-config-input" placeholder="value" value={h.value} onChange={(e) => setListItem('apiHeaders', idx, 'value', e.target.value)} />
                    <button type="button" className="sdc-icon-btn" onClick={() => removeListItem('apiHeaders', idx)}><X size={12} /></button>
                  </div>
                ))}
                <button type="button" className="sdc-secondary-btn" onClick={() => addListItem('apiHeaders')}><Plus size={12} /> Add Header</button>
                <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
                  Auth headers (API Key / Bearer / OAuth2) come from the Connection automatically — only add extras here.
                </span>
              </div>
            </>
          )}

          <div className="pc-config-row">
            <label className="pc-config-label">JSON Path</label>
            <input
              className="pc-config-input" style={{ fontFamily: 'var(--font-mono)' }}
              placeholder="$.data.customers" value={cfg.apiJsonPath || ''} onChange={(e) => set('apiJsonPath', e.target.value)}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              Dot-path to the record list inside the response. Leave blank if the response body itself is already the list.
            </span>
          </div>

          {!isSoap && (
          <div className="pc-config-row">
            <label className="pc-config-label">Pagination</label>
            <select className="pc-config-select" value={pagination.style} onChange={(e) => setPagination('style', e.target.value)}>
              {PAGINATION_STYLES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
            </select>
          </div>
          )}

          {!isSoap && pagination.style !== 'none' && (
            <div className="pc-config-row">
              <label className="pc-config-label">Page Size</label>
              <input
                type="number" className="pc-config-input" style={{ width: 100 }} min={1}
                value={pagination.page_size ?? 100} onChange={(e) => setPagination('page_size', Number(e.target.value))}
              />
            </div>
          )}

          {!isSoap && pagination.style === 'offset' && (
            <div className="pc-config-row">
              <label className="pc-config-label">Offset / Limit Param Names</label>
              <div className="sdc-param-row">
                <input className="pc-config-input" placeholder="offset param (e.g. offset)" value={pagination.offset_param ?? ''} onChange={(e) => setPagination('offset_param', e.target.value)} />
                <input className="pc-config-input" placeholder="limit param (e.g. limit)" value={pagination.page_size_param ?? ''} onChange={(e) => setPagination('page_size_param', e.target.value)} />
              </div>
            </div>
          )}
          {!isSoap && pagination.style === 'page' && (
            <div className="pc-config-row">
              <label className="pc-config-label">Page / Size Param Names</label>
              <div className="sdc-param-row">
                <input className="pc-config-input" placeholder="page param (e.g. page)" value={pagination.page_param ?? ''} onChange={(e) => setPagination('page_param', e.target.value)} />
                <input className="pc-config-input" placeholder="size param (e.g. page_size)" value={pagination.page_size_param ?? ''} onChange={(e) => setPagination('page_size_param', e.target.value)} />
              </div>
            </div>
          )}
          {!isSoap && (pagination.style === 'cursor' || pagination.style === 'token') && (
            <div className="pc-config-row">
              <label className="pc-config-label">{pagination.style === 'cursor' ? 'Cursor' : 'Token'} Param + Response Path</label>
              <div className="sdc-param-row">
                <input
                  className="pc-config-input" placeholder={pagination.style === 'cursor' ? 'cursor param (e.g. cursor)' : 'token param (e.g. page_token)'}
                  value={(pagination.style === 'cursor' ? pagination.cursor_param : pagination.token_param) ?? ''}
                  onChange={(e) => setPagination(pagination.style === 'cursor' ? 'cursor_param' : 'token_param', e.target.value)}
                />
                <input
                  className="pc-config-input" style={{ fontFamily: 'var(--font-mono)' }} placeholder="$.meta.next_cursor"
                  value={(pagination.style === 'cursor' ? pagination.cursor_path : pagination.token_path) ?? ''}
                  onChange={(e) => setPagination(pagination.style === 'cursor' ? 'cursor_path' : 'token_path', e.target.value)}
                />
              </div>
              <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
                JSON path to the next {pagination.style} value in each response — paging stops once it's empty.
              </span>
            </div>
          )}
          {!isSoap && pagination.style !== 'none' && (
            <div className="pc-config-row">
              <label className="pc-config-label">Max Pages (safety cap)</label>
              <input
                type="number" className="pc-config-input" style={{ width: 100 }} min={1} max={50}
                value={pagination.max_pages ?? 50} onChange={(e) => setPagination('max_pages', Number(e.target.value))}
              />
            </div>
          )}

          {!isSoap && (
          <div className="pc-config-row">
            <label className="pc-config-label">Rate Limit Delay (seconds between pages)</label>
            <input
              type="number" className="pc-config-input" style={{ width: 100 }} min={0} step={0.1}
              value={cfg.apiRateLimitDelay ?? 0} onChange={(e) => set('apiRateLimitDelay', Number(e.target.value))}
            />
          </div>
          )}
        </>
      )}

      {/* Custom Query */}
      {isQuery && cfg.connectionId && (
        <>
          <div className="pc-config-row">
            <label className="pc-config-label">SQL <span style={{ color: 'var(--color-danger)' }}>*</span></label>
            <textarea
              className="pc-config-textarea" style={{ fontFamily: 'var(--font-mono)', minHeight: 90 }}
              value={cfg.customSql} onChange={(e) => set('customSql', e.target.value)}
              placeholder={'SELECT customer_id, customer_name\nFROM sales.customer\nWHERE created_date >= :last_run_date'}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              SELECT / WITH only. Use <code>:name</code> for parameters below — never concatenated into the SQL text.
            </span>
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Parameters</label>
            {(cfg.parameters || []).map((p, idx) => (
              <div key={idx} className="sdc-param-row">
                <input className="pc-config-input" placeholder="name" value={p.name} onChange={(e) => setParam(idx, 'name', e.target.value)} />
                <input className="pc-config-input" placeholder="value" value={p.value} onChange={(e) => setParam(idx, 'value', e.target.value)} />
                <button type="button" className="sdc-icon-btn" onClick={() => removeParam(idx)}><X size={12} /></button>
              </div>
            ))}
            <button type="button" className="sdc-secondary-btn" onClick={addParam}><Plus size={12} /> Add Parameter</button>
          </div>
        </>
      )}

      {/* Preview */}
      {((isTableOrView && cfg.object) || (isQuery && cfg.customSql) || (isFile && cfg.object) || (isApi && cfg.apiEndpoint) || (isNetSuiteSearch && cfg.object)) && (
        <div className="pc-config-row">
          <label className="pc-config-label">Preview Sample Records</label>
          <div className="sdc-preview-controls">
            <select className="pc-config-select" style={{ width: 90 }} value={previewLimit} onChange={(e) => setPreviewLimit(Number(e.target.value))}>
              {PREVIEW_LIMITS.map((n) => <option key={n} value={n}>{n} rows</option>)}
            </select>
            <button type="button" className="sdc-secondary-btn" onClick={runPreview} disabled={previewLoading}>
              {previewLoading ? <Loader2 size={12} className="sdc-spin" /> : <Play size={12} />}
              {previewLoading ? 'Running…' : 'Preview'}
            </button>
          </div>
          {previewError && <span className="sdc-error-text">{previewError}</span>}
          {preview && (
            <div className="sdc-preview-table-wrap">
              <table className="sdc-preview-table">
                <thead><tr>{preview.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                <tbody>
                  {preview.rows.map((row, i) => (
                    <tr key={i}>{row.map((v, j) => <td key={j}>{v === null || v === undefined ? <span className="sdc-null">null</span> : String(v)}</td>)}</tr>
                  ))}
                </tbody>
              </table>
              {preview.truncated && <div className="sdc-truncated-note">Showing first {preview.rows.length} rows — more exist.</div>}
            </div>
          )}
        </div>
      )}

      {/* Run Extraction — real backend execution, separate from the whole-
          pipeline "Run Pipeline" design-time simulation */}
      <RunExtractionPanel
        pipelineId={pipelineId}
        nodeId={nodeId}
        pipelineDirty={pipelineDirty}
        objectType={cfg.objectType}
        ready={(isTableOrView && !!cfg.object) || (isQuery && !!cfg.customSql) || (isFile && !!cfg.object) || (isNetSuiteSearch && !!cfg.object)}
        columns={columns}
      />

      {/* Batch size / load type */}
      <div className="pc-config-row">
        <label className="pc-config-label">Batch Size</label>
        <input type="number" className="pc-config-input" min={1} value={cfg.batchSize} onChange={(e) => set('batchSize', Number(e.target.value))} />
        <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>Rows fetched per chunk once this pipeline runs (default 20,000).</span>
      </div>
      <div className="pc-config-row">
        <label className="pc-config-label">Load Type</label>
        <select className="pc-config-select" value={cfg.loadType} disabled={selectedConnection?.connection_type !== 'SALESFORCE'} onChange={(e) => set('loadType', e.target.value)}>
          {LOAD_TYPES.map((t) => <option key={t} value={t}>{t.replace('_', ' ')}</option>)}
        </select>
        <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
          {selectedConnection?.connection_type === 'SALESFORCE'
            ? 'Bulk API vs. simple REST extraction — Auto falls back to REST if Bulk isn\'t available.'
            : 'Only applies to Salesforce sources.'}
        </span>
      </div>
    </>
  );
}
