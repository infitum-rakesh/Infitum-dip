/**
 * Format Conversion — /app/format-conversion
 * Data Format Transformation Engine (Phase 4): converts data between
 * JSON/TABLE/CSV/Excel/XML/Parquet in any direction. A source is either
 * an uploaded file (any file format) or a real database table; a target
 * is either a downloadable file (any file format) or a real database
 * table — the same real, chunked, canonical read/write adapters the
 * backend already uses elsewhere (SQL Workspace export, Reconciliation's
 * file/connection sources), not a fake preview.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, Download, Loader2, Plus, RefreshCcw, Trash2, Upload, X, XCircle } from 'lucide-react';
import { Button, EmptyState, PermissionGate, StatusBadge } from '@/components/common/index.js';
import { TRANSFORMATION_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { transformationApi } from '@/services/api/transformationApi.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { uploadedFileApi } from '@/services/api/uploadedFileApi.js';
import './format-conversion.css';

// Value is "XLSX" (not "EXCEL") to match the backend's TransformFormat
// enum, which itself must match sql_export_writers.py's writer keys —
// a real bug found live: "EXCEL" isn't a registered writer format there.
const FILE_FORMATS = [
  { value: 'CSV', label: 'CSV' },
  { value: 'JSON', label: 'JSON' },
  { value: 'XLSX', label: 'Excel (.xlsx)' },
  { value: 'XML', label: 'XML' },
  { value: 'PARQUET', label: 'Parquet' },
];

const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);

function statusVariant(status) {
  if (status === 'SUCCEEDED') return 'success';
  if (status === 'FAILED') return 'danger';
  if (status === 'CANCELLED') return 'gray';
  return 'info';
}

// ── Endpoint picker: Upload File / Database Table (source) or
//    Download As / Database Table (target) ──────────────────────

function EndpointPicker({ role, kinds, value, onChange, disabled }) {
  const [connections, setConnections] = useState([]);
  const [schemas, setSchemas] = useState([]);
  const [objects, setObjects] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');

  useEffect(() => {
    connectionApi.list({ category: 'DATABASE', page_size: 200 }).then((r) => setConnections(r.items ?? [])).catch(() => {});
  }, []);

  useEffect(() => {
    if (value.kind !== 'CONNECTION' || !value.connection_id) { setSchemas([]); return; }
    connectionApi.getSchemas(value.connection_id).then((r) => {
      setSchemas(r.schemas ?? []);
      if ((r.schemas ?? []).length === 1 && !value.schema) onChange({ ...value, schema: r.schemas[0] });
    }).catch(() => setSchemas([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value.kind, value.connection_id]);

  useEffect(() => {
    if (value.kind !== 'CONNECTION' || !value.connection_id || !value.schema) { setObjects([]); return; }
    connectionApi.getObjects(value.connection_id, { schema: value.schema }).then((r) => setObjects(r.items ?? [])).catch(() => setObjects([]));
  }, [value.kind, value.connection_id, value.schema]);

  const switchKind = (kind) => {
    if (disabled || kind === value.kind) return;
    onChange({
      kind, format: kind === 'CONNECTION' ? 'TABLE' : (value.format === 'TABLE' ? 'CSV' : value.format),
      connection_id: null, schema: '', object: '', uploaded_file_id: null, uploaded_filename: '',
    });
  };

  const handleUpload = async (file) => {
    if (!file) return;
    setUploading(true); setUploadError('');
    try {
      const record = await uploadedFileApi.upload(file);
      const ext = (record.extension || '').toLowerCase();
      const inferred = ext === 'xlsx' ? 'XLSX' : ext === 'json' ? 'JSON' : ext === 'xml' ? 'XML' : ext === 'parquet' ? 'PARQUET' : 'CSV';
      onChange({ ...value, uploaded_file_id: record.id, uploaded_filename: record.filename, format: inferred });
    } catch (err) {
      setUploadError(err?.response?.data?.detail ?? err?.message ?? 'Upload failed.');
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="fc-endpoint">
      <div className="fc-kind-toggle" role="tablist">
        {kinds.map((k) => (
          <button key={k.value} type="button" role="tab" aria-selected={value.kind === k.value}
            className={`fc-kind-btn ${value.kind === k.value ? 'active' : ''}`} disabled={disabled}
            onClick={() => switchKind(k.value)}>
            {k.label}
          </button>
        ))}
      </div>

      {value.kind === 'UPLOADED_FILE' && (
        <>
          <label className="fc-field-label">File</label>
          {value.uploaded_file_id ? (
            <div className="fc-uploaded-file">
              <span>{value.uploaded_filename}</span>
              {!disabled && (
                <button type="button" className="fc-icon-btn" title="Remove"
                  onClick={() => onChange({ ...value, uploaded_file_id: null, uploaded_filename: '' })}>
                  <Trash2 size={12} />
                </button>
              )}
            </div>
          ) : (
            <label className="fc-upload-btn" style={{ cursor: disabled || uploading ? 'default' : 'pointer' }}>
              {uploading ? <Loader2 size={13} className="fc-spin" /> : <Upload size={13} />}
              {uploading ? 'Uploading…' : 'Choose file…'}
              <input type="file" style={{ display: 'none' }} disabled={disabled || uploading}
                onChange={(e) => handleUpload(e.target.files?.[0])} />
            </label>
          )}
          {uploadError && <p className="fc-error-text">{uploadError}</p>}
          {value.uploaded_file_id && (
            <>
              <label className="fc-field-label">Format</label>
              <select className="fc-select" disabled={disabled} value={value.format}
                onChange={(e) => onChange({ ...value, format: e.target.value })}>
                {FILE_FORMATS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
              </select>
            </>
          )}
        </>
      )}

      {value.kind === 'DOWNLOAD_FILE' && (
        <>
          <label className="fc-field-label">Format</label>
          <select className="fc-select" disabled={disabled} value={value.format}
            onChange={(e) => onChange({ ...value, format: e.target.value })}>
            {FILE_FORMATS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </>
      )}

      {value.kind === 'CONNECTION' && (
        <>
          <label className="fc-field-label">Connection</label>
          <select className="fc-select" disabled={disabled} value={value.connection_id ?? ''}
            onChange={(e) => onChange({ ...value, connection_id: e.target.value ? Number(e.target.value) : null, schema: '', object: '' })}>
            <option value="">Select connection…</option>
            {connections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
          </select>
          <label className="fc-field-label">Schema</label>
          <select className="fc-select" disabled={disabled || !value.connection_id} value={value.schema}
            onChange={(e) => onChange({ ...value, schema: e.target.value, object: '' })}>
            <option value="">Select schema…</option>
            {schemas.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <label className="fc-field-label">{role === 'source' ? 'Table / View' : 'Table name'}</label>
          {role === 'source' ? (
            <select className="fc-select" disabled={disabled || !value.schema} value={value.object}
              onChange={(e) => onChange({ ...value, object: e.target.value })}>
              <option value="">Select object…</option>
              {objects.map((o) => <option key={o.name} value={o.name}>{o.name}</option>)}
            </select>
          ) : (
            <input className="fc-select" disabled={disabled || !value.schema} value={value.object}
              placeholder="New or existing table name"
              onChange={(e) => onChange({ ...value, object: e.target.value })} />
          )}
        </>
      )}
    </div>
  );
}

// ── New conversion form ──────────────────────────────────────────

// spec item #8's target-format writer options — only meaningful for
// target.kind === 'DOWNLOAD_FILE' (a TABLE target has no equivalent).
// Empty string means "use the default", same contract format_options:
// null has on the backend (comma CSV, "Result" sheet, <rows>/<row> XML).
const FORMAT_OPTIONS_INITIAL = { delimiter: '', quotechar: '', encoding: '', sheet_name: '', root_element: '', row_element: '' };

function buildFormatOptions(target, opts) {
  if (target.kind !== 'DOWNLOAD_FILE') return null;
  const picked = {};
  if (target.format === 'CSV') {
    if (opts.delimiter) picked.delimiter = opts.delimiter;
    if (opts.quotechar) picked.quotechar = opts.quotechar;
    if (opts.encoding) picked.encoding = opts.encoding;
  } else if (target.format === 'XLSX') {
    if (opts.sheet_name) picked.sheet_name = opts.sheet_name;
  } else if (target.format === 'XML') {
    if (opts.root_element) picked.root_element = opts.root_element;
    if (opts.row_element) picked.row_element = opts.row_element;
  }
  return Object.keys(picked).length ? picked : null;
}

function NewConversionForm({ onCreated, onClose }) {
  const [name, setName] = useState('');
  const [source, setSource] = useState({ kind: 'UPLOADED_FILE', format: 'CSV', connection_id: null, schema: '', object: '', uploaded_file_id: null, uploaded_filename: '' });
  const [target, setTarget] = useState({ kind: 'DOWNLOAD_FILE', format: 'JSON', connection_id: null, schema: '', object: '' });
  const [formatOptions, setFormatOptions] = useState(FORMAT_OPTIONS_INITIAL);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const canSubmit =
    (source.kind === 'UPLOADED_FILE' ? !!source.uploaded_file_id : (!!source.connection_id && !!source.object)) &&
    (target.kind === 'DOWNLOAD_FILE' ? !!target.format : (!!target.connection_id && !!target.object));

  const handleSubmit = async () => {
    setSaving(true); setError('');
    try {
      const job = await transformationApi.create({
        name: name || 'Untitled Conversion',
        source_kind: source.kind, source_format: source.format,
        source_uploaded_file_id: source.kind === 'UPLOADED_FILE' ? source.uploaded_file_id : null,
        source_connection_id: source.kind === 'CONNECTION' ? source.connection_id : null,
        source_schema: source.kind === 'CONNECTION' ? (source.schema || null) : null,
        source_object: source.kind === 'CONNECTION' ? source.object : null,
        target_kind: target.kind, target_format: target.kind === 'CONNECTION' ? 'TABLE' : target.format,
        target_connection_id: target.kind === 'CONNECTION' ? target.connection_id : null,
        target_schema: target.kind === 'CONNECTION' ? (target.schema || null) : null,
        target_object: target.kind === 'CONNECTION' ? target.object : null,
        format_options: buildFormatOptions(target, formatOptions),
      });
      onCreated(job);
    } catch (err) {
      setError(err?.response?.data?.detail ?? err?.message ?? 'Could not start this conversion.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fc-card fc-new-form">
      <div className="fc-card-header">
        <span className="fc-card-title">New Conversion</span>
        <button type="button" className="fc-icon-btn" onClick={onClose}><X size={14} /></button>
      </div>
      <label className="fc-field-label">Name</label>
      <input className="fc-select" value={name} onChange={(e) => setName(e.target.value)} placeholder="Untitled Conversion" />

      <div className="fc-endpoints-row">
        <div className="fc-endpoint-col">
          <div className="fc-endpoint-badge source">Source</div>
          <EndpointPicker role="source" kinds={[{ value: 'UPLOADED_FILE', label: 'Upload File' }, { value: 'CONNECTION', label: 'Database Table' }]}
            value={source} onChange={setSource} disabled={saving} />
        </div>
        <ArrowRight className="fc-arrow" size={20} />
        <div className="fc-endpoint-col">
          <div className="fc-endpoint-badge target">Target</div>
          <EndpointPicker role="target" kinds={[{ value: 'DOWNLOAD_FILE', label: 'Download As' }, { value: 'CONNECTION', label: 'Database Table' }]}
            value={target} onChange={setTarget} disabled={saving} />
        </div>
      </div>

      {target.kind === 'DOWNLOAD_FILE' && target.format === 'CSV' && (
        <div className="fc-format-options">
          <label className="fc-field-label">Delimiter</label>
          <input className="fc-select" maxLength={1} placeholder="," value={formatOptions.delimiter}
            onChange={(e) => setFormatOptions({ ...formatOptions, delimiter: e.target.value })} />
          <label className="fc-field-label">Quote character</label>
          <input className="fc-select" maxLength={1} placeholder='"' value={formatOptions.quotechar}
            onChange={(e) => setFormatOptions({ ...formatOptions, quotechar: e.target.value })} />
          <label className="fc-field-label">Encoding</label>
          <input className="fc-select" placeholder="utf-8" value={formatOptions.encoding}
            onChange={(e) => setFormatOptions({ ...formatOptions, encoding: e.target.value })} />
        </div>
      )}
      {target.kind === 'DOWNLOAD_FILE' && target.format === 'XLSX' && (
        <div className="fc-format-options">
          <label className="fc-field-label">Sheet name</label>
          <input className="fc-select" placeholder="Result" value={formatOptions.sheet_name}
            onChange={(e) => setFormatOptions({ ...formatOptions, sheet_name: e.target.value })} />
        </div>
      )}
      {target.kind === 'DOWNLOAD_FILE' && target.format === 'XML' && (
        <div className="fc-format-options">
          <label className="fc-field-label">Root element</label>
          <input className="fc-select" placeholder="rows" value={formatOptions.root_element}
            onChange={(e) => setFormatOptions({ ...formatOptions, root_element: e.target.value })} />
          <label className="fc-field-label">Row element</label>
          <input className="fc-select" placeholder="row" value={formatOptions.row_element}
            onChange={(e) => setFormatOptions({ ...formatOptions, row_element: e.target.value })} />
        </div>
      )}

      {error && <p className="fc-error-text">{error}</p>}
      <div className="fc-form-actions">
        <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" onClick={handleSubmit} disabled={!canSubmit || saving} loading={saving}>Run Conversion</Button>
      </div>
    </div>
  );
}

// ── Job row ──────────────────────────────────────────────────────

function JobRow({ job, onCancel, onDelete, onDownload }) {
  return (
    <div className="fc-job-row">
      <div className="fc-job-main">
        <div className="fc-job-name">{job.name}</div>
        <div className="fc-job-formats">
          <span>{job.source_kind === 'CONNECTION' ? `TABLE (${job.source_object})` : job.source_format}</span>
          <ArrowRight size={12} />
          <span>{job.target_kind === 'CONNECTION' ? `TABLE (${job.target_object})` : job.target_format}</span>
        </div>
      </div>
      <div className="fc-job-meta">
        <StatusBadge variant={statusVariant(job.status)} dot>{job.status}</StatusBadge>
        <span className="fc-job-rows">{job.row_count.toLocaleString()} rows</span>
        <span className="fc-job-date">{formatDateTime(job.created_at)}</span>
      </div>
      {job.error_message && <div className="fc-job-error">{job.error_message}</div>}
      <div className="fc-job-actions">
        {!TERMINAL.has(job.status) && (
          <Button variant="secondary" size="sm" icon={<XCircle size={12} />} onClick={() => onCancel(job)}>Cancel</Button>
        )}
        {job.status === 'SUCCEEDED' && job.target_kind === 'DOWNLOAD_FILE' && (
          <Button variant="secondary" size="sm" icon={<Download size={12} />} onClick={() => onDownload(job)}>Download</Button>
        )}
        {TERMINAL.has(job.status) && (
          <Button variant="ghost" size="sm" icon={<Trash2 size={12} />} onClick={() => onDelete(job)}>Delete</Button>
        )}
      </div>
    </div>
  );
}

// ── Page ───────────────────────────────────────────────────────

export default function FormatConversion() {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const pollRef = useRef(null);

  const load = useCallback(() => {
    setLoading(true);
    transformationApi.list({ page_size: 50 }).then((r) => setJobs(r.items ?? [])).catch(() => {}).finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  // Poll while any job is still in flight — same 1200ms convention as
  // JobLogDrawer.jsx/BulkMigrationRunMonitor.jsx, stopped once nothing's running.
  useEffect(() => {
    const hasActive = jobs.some((j) => !TERMINAL.has(j.status));
    if (!hasActive) {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      return;
    }
    if (pollRef.current) return;
    pollRef.current = setInterval(() => {
      Promise.all(jobs.filter((j) => !TERMINAL.has(j.status)).map((j) => transformationApi.get(j.id).catch(() => null)))
        .then((updated) => {
          setJobs((prev) => prev.map((j) => updated.find((u) => u && u.id === j.id) ?? j));
        });
    }, 1200);
    return () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
  }, [jobs]);

  const handleCreated = (job) => { setJobs((prev) => [job, ...prev]); setShowForm(false); };
  const handleCancel = async (job) => { await transformationApi.cancel(job.id).catch(() => {}); load(); };
  const handleDelete = async (job) => { await transformationApi.remove(job.id).catch(() => {}); setJobs((prev) => prev.filter((j) => j.id !== job.id)); };
  const handleDownload = (job) => {
    const ext = job.target_format.toLowerCase();
    transformationApi.download(job.id, `${job.name.replace(/[^a-z0-9_-]+/gi, '_')}.${ext}`);
  };

  return (
    <PermissionGate {...TRANSFORMATION_PERMISSIONS.TRANSFORMATION_VIEW} showReason>
      <div className="fc-page">
        <div className="fc-header">
          <div>
            <h1 className="fc-title">Format Conversion</h1>
            <p className="fc-subtitle">Convert data between JSON, database tables, CSV, Excel, XML, and Parquet — in any direction.</p>
          </div>
          <div className="fc-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={load}>Refresh</Button>
            <PermissionGate {...TRANSFORMATION_PERMISSIONS.TRANSFORMATION_CREATE} fallback={null}>
              <Button variant="primary" icon={<Plus size={14} />} onClick={() => setShowForm(true)}>New Conversion</Button>
            </PermissionGate>
          </div>
        </div>

        {showForm && <NewConversionForm onCreated={handleCreated} onClose={() => setShowForm(false)} />}

        {loading ? (
          <div className="fc-loading">Loading…</div>
        ) : jobs.length === 0 ? (
          <EmptyState icon={<ArrowRight size={28} />} title="No conversions yet"
            description="Start a new conversion to convert a file or database table into a different format." />
        ) : (
          <div className="fc-job-list">
            {jobs.map((job) => (
              <JobRow key={job.id} job={job} onCancel={handleCancel} onDelete={handleDelete} onDownload={handleDownload} />
            ))}
          </div>
        )}
      </div>
    </PermissionGate>
  );
}
