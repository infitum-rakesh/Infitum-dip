import { useEffect, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { Button, Modal } from '@/components/common/index.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { pipelineApi } from '@/services/api/pipelineApi.js';
import { scheduleApi } from '@/services/api/scheduleApi.js';

const DUPLICATE_BEHAVIORS = [
  { value: 'SKIP_DUPLICATE', label: 'Skip — same content already processed elsewhere' },
  { value: 'PROCESS_AS_SEPARATE_DOCUMENT', label: 'Process anyway, as a separate document' },
];

const EMPTY_DOCUMENT_CONFIG = {
  // PAY18/PAY20 — "FILE" (default, unchanged), "DATABASE" (runs
  // source_query against source_connection_id, a DB-category connection,
  // instead of scanning a path), or "API" (runs the source_api_* fields
  // below against source_connection_id, an API-category connection) —
  // see source_kind's own note on DocumentScheduleConfig for why
  // source_path stays a free-text label rather than a filesystem path
  // for either non-file kind.
  source_kind: 'FILE', source_query: '', source_record_key_column: '',
  source_api_endpoint: '', source_api_method: 'GET',
  source_api_pagination_style: 'none', source_api_pagination_page_size: '',
  source_api_json_path: '', source_erp_query: '',
  source_connection_id: '', source_path: '', file_pattern: '*.pdf', recursive_scan: false,
  batch_size: 500, max_concurrency: 3, confidence_threshold: 90, accuracy_threshold: 90,
  archive_after_success: false, archive_path: '', move_failed_to_review_path: '',
  duplicate_content_behavior: 'SKIP_DUPLICATE', max_file_size_mb: 100,
  document_type: '', invoice_auto_post: false, invoice_erp_connection_id: '',
};

const DOCUMENT_TYPES = [
  { value: '', label: 'Auto-detect' },
  { value: 'INVOICE', label: 'Invoice (Invoice Intelligence)' },
];

// A DATABASE/API source has no OCR/AI Extraction stage to fall back on —
// its query's own column names (or the API response's own field names)
// ARE the mapping (see document_service.build_fields_from_record()) — so
// Document Type must be pinned up front; only INVOICE has a real
// taxonomy wired to either path today.
const DATABASE_DOCUMENT_TYPES = [{ value: 'INVOICE', label: 'Invoice (Invoice Intelligence)' }];

const API_PAGINATION_STYLES = [
  { value: 'none', label: 'None — single request' },
  { value: 'page', label: 'Page number (?page=1&page_size=…)' },
  { value: 'offset', label: 'Offset (?offset=0&limit=…)' },
  { value: 'cursor', label: 'Cursor (?cursor=…&limit=…)' },
  { value: 'token', label: 'Page token (?page_token=…&limit=…)' },
];

const FREQUENCIES = [
  { value: 'ONCE', label: 'Once' },
  { value: 'HOURLY', label: 'Hourly' },
  { value: 'DAILY', label: 'Daily' },
  { value: 'WEEKLY', label: 'Weekly' },
  { value: 'MONTHLY', label: 'Monthly' },
  { value: 'CUSTOM_CRON', label: 'Custom Cron' },
];
const ENVIRONMENTS = ['PRODUCTION', 'STAGING', 'DEVELOPMENT', 'TEST'];
const TIMEZONES = [
  'UTC', 'America/New_York', 'America/Chicago', 'America/Los_Angeles', 'America/Sao_Paulo',
  'Europe/London', 'Europe/Berlin', 'Asia/Kolkata', 'Asia/Dubai', 'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney',
];

const EMPTY_FORM = {
  name: '', pipeline_id: '', pipeline_name: '', environment: 'DEVELOPMENT', timezone: 'UTC',
  frequency: 'DAILY', start_date: '', start_time: '02:00', cron_expression: '',
  retry_count: 0, retry_interval_seconds: 60, timeout_seconds: 3600, concurrency: 1,
  notify_on_failure: true, notify_on_success: false, notification_emails: '',
};

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** Converts a UTC ISO timestamp to {date, time} wall-clock strings in the
 * given IANA timezone — used to re-populate the form when editing a
 * schedule, since start_at is always stored/returned in UTC. */
function toLocalParts(iso, timeZone) {
  const d = new Date(iso);
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  });
  const parts = Object.fromEntries(fmt.formatToParts(d).map((p) => [p.type, p.value]));
  return { date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` };
}

/** ScheduleFormModal — create or edit. Reuses the existing pipeline catalogue
 * (pipelineApi.js) for the Pipeline picker — never invents a second pipeline
 * registry. */
export function ScheduleFormModal({ open, onClose, schedule, onSaved }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [pipelines, setPipelines] = useState([]);
  const [sourceType, setSourceType] = useState('PIPELINE'); // 'PIPELINE' | 'DOCUMENT_SOURCE' | 'INVOICE_SOURCE'
  const [docConfig, setDocConfig] = useState(EMPTY_DOCUMENT_CONFIG);
  const [storageConnections, setStorageConnections] = useState([]);
  const [dbConnections, setDbConnections] = useState([]);
  const [apiConnections, setApiConnections] = useState([]);
  const [erpConnections, setErpConnections] = useState([]);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => { pipelineApi.list().then(setPipelines).catch(() => {}); }, []);
  useEffect(() => {
    if (!open) return;
    // STORAGE-category connections only (Connections module) — the same
    // "reuse existing Connections, never a second credential system"
    // Source Dataset node already follows (spec section 35).
    connectionApi.list({ category: 'STORAGE', page_size: 200 }).then((r) => setStorageConnections(r.items ?? [])).catch(() => {});
    // DATABASE-category connections (PAY18) — a DATABASE-kind document
    // schedule points source_connection_id at one of these instead of a
    // STORAGE connection.
    connectionApi.list({ category: 'DATABASE', page_size: 200 }).then((r) => setDbConnections(r.items ?? [])).catch(() => {});
    // API-category connections (PAY20) — an API-kind document schedule
    // points source_connection_id at one of these instead.
    connectionApi.list({ category: 'API', page_size: 200 }).then((r) => setApiConnections(r.items ?? [])).catch(() => {});
    // ERP connections (INV11) — same "reuse existing Connections" rule;
    // APPLICATION category also holds Salesforce/NetSuite, so filter down
    // to the actual Oracle ERP Cloud connection type client-side.
    connectionApi.list({ category: 'APPLICATION', page_size: 200 })
      .then((r) => setErpConnections((r.items ?? []).filter((c) => c.connection_type === 'ORACLE_ERP_CLOUD')))
      .catch(() => {});
  }, [open]);

  useEffect(() => {
    if (!open) return;
    if (schedule) {
      const { date, time } = toLocalParts(schedule.start_at, schedule.timezone);
      setForm({
        name: schedule.name, pipeline_id: schedule.pipeline_id, pipeline_name: schedule.pipeline_name,
        environment: schedule.environment, timezone: schedule.timezone, frequency: schedule.frequency,
        start_date: date, start_time: time,
        cron_expression: schedule.cron_expression ?? '',
        retry_count: schedule.retry_count, retry_interval_seconds: schedule.retry_interval_seconds,
        timeout_seconds: schedule.timeout_seconds, concurrency: schedule.concurrency,
        notify_on_failure: schedule.notify_on_failure, notify_on_success: schedule.notify_on_success,
        notification_emails: schedule.notification_emails ?? '',
      });
      if (schedule.document_config) {
        // Invoice Intelligence schedules ARE Document Source schedules
        // with document_type="INVOICE" (INV11 — reuses the exact same
        // scheduler/idempotency/dispatch infrastructure rather than a
        // parallel one); re-derive the friendlier top-level choice from
        // that so an invoice schedule reopens showing "Invoice Source",
        // not the generic "Document Source".
        setSourceType(schedule.document_config.document_type === 'INVOICE' ? 'INVOICE_SOURCE' : 'DOCUMENT_SOURCE');
        // source_api_pagination comes back from the API as a nested
        // object ({style, page_size, ...}) — flatten it into the two
        // form fields this modal actually edits.
        const pagination = schedule.document_config.source_api_pagination || {};
        setDocConfig({
          ...EMPTY_DOCUMENT_CONFIG, ...schedule.document_config,
          source_api_pagination_style: pagination.style || 'none',
          source_api_pagination_page_size: pagination.page_size ?? '',
        });
      } else {
        setSourceType('PIPELINE');
        setDocConfig(EMPTY_DOCUMENT_CONFIG);
      }
    } else {
      setForm({ ...EMPTY_FORM, start_date: todayISO() });
      setSourceType('PIPELINE');
      setDocConfig(EMPTY_DOCUMENT_CONFIG);
    }
    setError(null);
    setAdvancedOpen(false);
  }, [open, schedule]);

  const set = (key) => (e) => {
    const val = e?.target ? (e.target.type === 'checkbox' ? e.target.checked : e.target.value) : e;
    setForm((f) => ({ ...f, [key]: val }));
  };

  const setDoc = (key) => (e) => {
    const val = e?.target ? (e.target.type === 'checkbox' ? e.target.checked : e.target.value) : e;
    setDocConfig((c) => ({ ...c, [key]: val }));
  };

  const setPipeline = (id) => {
    const p = pipelines.find((x) => x.id === id);
    setForm((f) => ({ ...f, pipeline_id: id, pipeline_name: p?.name ?? '' }));
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const isDocSource = sourceType === 'DOCUMENT_SOURCE' || sourceType === 'INVOICE_SOURCE';
      const body = {
        name: form.name,
        pipeline_id: sourceType === 'PIPELINE' ? form.pipeline_id : '',
        pipeline_name: sourceType === 'PIPELINE' ? form.pipeline_name : '',
        environment: form.environment, timezone: form.timezone, frequency: form.frequency,
        start_date: form.start_date, start_time: form.start_time,
        cron_expression: form.frequency === 'CUSTOM_CRON' ? form.cron_expression : null,
        retry_count: Number(form.retry_count), retry_interval_seconds: Number(form.retry_interval_seconds),
        timeout_seconds: Number(form.timeout_seconds), concurrency: Number(form.concurrency),
        notify_on_failure: form.notify_on_failure, notify_on_success: form.notify_on_success,
        notification_emails: form.notification_emails || null,
      };
      if (isDocSource) {
        const isDatabase = docConfig.source_kind === 'DATABASE';
        const isApi = docConfig.source_kind === 'API';
        const isErp = docConfig.source_kind === 'ERP';
        const isNonFile = isDatabase || isApi || isErp;
        // source_api_pagination is null for "none" (a single request) —
        // matches fetch_api()'s own default when the field is omitted.
        const pagination = (isApi && docConfig.source_api_pagination_style !== 'none')
          ? { style: docConfig.source_api_pagination_style, page_size: Number(docConfig.source_api_pagination_page_size) || 100 }
          : null;
        body.document_config = {
          source_kind: docConfig.source_kind,
          source_query: isDatabase ? docConfig.source_query : null,
          source_record_key_column: isNonFile ? (docConfig.source_record_key_column || null) : null,
          source_api_endpoint: isApi ? docConfig.source_api_endpoint : null,
          source_api_method: isApi ? docConfig.source_api_method : 'GET',
          source_api_pagination: pagination,
          source_api_json_path: (isApi && docConfig.source_api_json_path) ? docConfig.source_api_json_path : null,
          source_erp_query: (isErp && docConfig.source_erp_query) ? docConfig.source_erp_query : null,
          source_connection_id: Number(docConfig.source_connection_id),
          source_path: docConfig.source_path,
          file_pattern: isNonFile ? '*' : (docConfig.file_pattern || '*'),
          recursive_scan: isNonFile ? false : docConfig.recursive_scan,
          batch_size: Number(docConfig.batch_size),
          max_concurrency: Number(docConfig.max_concurrency),
          confidence_threshold: Number(docConfig.confidence_threshold),
          accuracy_threshold: Number(docConfig.accuracy_threshold),
          archive_after_success: isNonFile ? false : docConfig.archive_after_success,
          archive_path: (!isNonFile && docConfig.archive_after_success) ? docConfig.archive_path : null,
          move_failed_to_review_path: isNonFile ? null : (docConfig.move_failed_to_review_path || null),
          duplicate_content_behavior: docConfig.duplicate_content_behavior,
          max_file_size_mb: Number(docConfig.max_file_size_mb),
          // A DATABASE/API/ERP source requires Document Type == INVOICE
          // server-side (see DocumentScheduleConfigIn's own validators) —
          // force it here too so the picker's "Auto-detect" default can't
          // slip through. Same forcing for the "Invoice Source" top-level
          // choice (FILE kind included) — that choice exists specifically
          // so Invoice Intelligence scheduling doesn't require knowing
          // this nested field even exists.
          document_type: (isNonFile || sourceType === 'INVOICE_SOURCE') ? 'INVOICE' : (docConfig.document_type || null),
          invoice_auto_post: (isNonFile || sourceType === 'INVOICE_SOURCE' || docConfig.document_type === 'INVOICE') ? docConfig.invoice_auto_post : false,
          invoice_erp_connection_id: ((isNonFile || sourceType === 'INVOICE_SOURCE' || docConfig.document_type === 'INVOICE') && docConfig.invoice_auto_post && docConfig.invoice_erp_connection_id)
            ? Number(docConfig.invoice_erp_connection_id) : null,
        };
      }
      if (schedule) await scheduleApi.update(schedule.id, body);
      else await scheduleApi.create(body);
      onSaved();
      onClose();
    } catch (err) {
      setError(err.response?.data?.detail || err.message || 'Failed to save schedule.');
    } finally {
      setSaving(false);
    }
  };

  const isDocSourceType = sourceType === 'DOCUMENT_SOURCE' || sourceType === 'INVOICE_SOURCE';
  const docSourceValid = !isDocSourceType || (
    docConfig.source_connection_id && docConfig.source_path
    && (docConfig.source_kind !== 'DATABASE' || docConfig.source_query)
    && (docConfig.source_kind !== 'API' || docConfig.source_api_endpoint)
    && (docConfig.source_kind === 'FILE'
      ? (!docConfig.archive_after_success || docConfig.archive_path)
      : true)
    && Number(docConfig.batch_size) > 0 && Number(docConfig.max_concurrency) > 0
    && Number(docConfig.confidence_threshold) >= 0 && Number(docConfig.confidence_threshold) <= 100
    && Number(docConfig.accuracy_threshold) >= 0 && Number(docConfig.accuracy_threshold) <= 100
    && (!docConfig.invoice_auto_post || ((sourceType === 'INVOICE_SOURCE' || docConfig.document_type === 'INVOICE') && docConfig.invoice_erp_connection_id))
  );
  const valid = form.name && form.start_date && form.start_time
    && (form.frequency !== 'CUSTOM_CRON' || form.cron_expression)
    && (sourceType === 'PIPELINE' ? form.pipeline_id : true)
    && docSourceValid;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={schedule ? `Edit ${schedule.name}` : 'Create Schedule'}
      size="lg"
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button><Button variant="primary" loading={saving} disabled={!valid} onClick={handleSave}>Save Schedule</Button></>}
    >
      {error && <div className="sched-form-error">{error}</div>}

      <div className="sched-field-grid">
        <div className="sched-field">
          <label className="sched-field-label">Schedule Name<span className="sched-required">*</span></label>
          <input className="sched-input" value={form.name} onChange={set('name')} placeholder="e.g. Daily Customer Validation" />
        </div>
        <div className="sched-field">
          <label className="sched-field-label">Source Type</label>
          <select className="sched-input" value={sourceType} onChange={(e) => setSourceType(e.target.value)}>
            <option value="PIPELINE">Pipeline Builder Pipeline</option>
            <option value="DOCUMENT_SOURCE">Document Source (Scheduled Document Intelligence)</option>
            <option value="INVOICE_SOURCE">Invoice Source (Scheduled Invoice Intelligence)</option>
          </select>
          {sourceType === 'INVOICE_SOURCE' && (
            <div className="sched-field-hint">
              Same real intake/OCR/AI pipeline as Document Source, pinned to the Invoice taxonomy — batches straight into Invoice Intelligence
              (Review Queue or straight-through ERP posting), not the generic Documents list.
            </div>
          )}
        </div>

        {sourceType === 'PIPELINE' && (
          <div className="sched-field" style={{ gridColumn: '1 / -1' }}>
            <label className="sched-field-label">Pipeline<span className="sched-required">*</span></label>
            <select className="sched-input" value={form.pipeline_id} onChange={(e) => setPipeline(e.target.value)}>
              <option value="">Select a pipeline…</option>
              {pipelines.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </div>
        )}

        <div className="sched-field">
          <label className="sched-field-label">Environment</label>
          <select className="sched-input" value={form.environment} onChange={set('environment')}>
            {ENVIRONMENTS.map((e) => <option key={e} value={e}>{e.charAt(0) + e.slice(1).toLowerCase()}</option>)}
          </select>
        </div>
        <div className="sched-field">
          <label className="sched-field-label">Timezone</label>
          <select className="sched-input" value={form.timezone} onChange={set('timezone')}>
            {TIMEZONES.map((tz) => <option key={tz} value={tz}>{tz}</option>)}
          </select>
        </div>

        <div className="sched-field">
          <label className="sched-field-label">Start Date<span className="sched-required">*</span></label>
          <input type="date" className="sched-input" value={form.start_date} onChange={set('start_date')} />
        </div>
        <div className="sched-field">
          <label className="sched-field-label">Start Time<span className="sched-required">*</span></label>
          <input type="time" className="sched-input" value={form.start_time} onChange={set('start_time')} />
        </div>

        <div className="sched-field">
          <label className="sched-field-label">Frequency</label>
          <select className="sched-input" value={form.frequency} onChange={set('frequency')}>
            {FREQUENCIES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
        {form.frequency === 'CUSTOM_CRON' && (
          <div className="sched-field">
            <label className="sched-field-label">Cron Expression<span className="sched-required">*</span></label>
            <input className="sched-input" value={form.cron_expression} onChange={set('cron_expression')} placeholder="*/15 * * * *" />
          </div>
        )}
      </div>

      {isDocSourceType && (
        <>
          <div className="sched-form-divider" />
          <div className="sched-section-title">{sourceType === 'INVOICE_SOURCE' ? 'Invoice Source' : 'Source'}</div>
          <div className="sched-field-grid">
            <div className="sched-field">
              <label className="sched-field-label">Source Kind</label>
              <select className="sched-input" value={docConfig.source_kind} onChange={setDoc('source_kind')}>
                <option value="FILE">File (scan a folder for documents)</option>
                <option value="DATABASE">Database Query (one invoice per row)</option>
                <option value="API">API Endpoint (one invoice per row)</option>
                <option value="ERP">ERP System (one invoice per row)</option>
              </select>
              <div className="sched-field-hint">
                {docConfig.source_kind === 'DATABASE'
                  ? 'Runs a SQL query against a database connection — each returned row becomes one invoice, with no OCR/AI step (the query\'s own columns are already structured data).'
                  : docConfig.source_kind === 'API'
                  ? 'Calls a REST API endpoint on an API connection — each returned record becomes one invoice, with no OCR/AI step (the response\'s own fields are already structured data).'
                  : docConfig.source_kind === 'ERP'
                  ? 'Pulls invoices directly from an Oracle ERP Cloud connection\'s Payables Invoices resource — each returned invoice becomes one record, with no OCR/AI step.'
                  : 'Scans a folder on a storage connection for scannable documents (PDF/image).'}
              </div>
            </div>
          </div>

          {docConfig.source_kind === 'DATABASE' ? (
            <div className="sched-field-grid">
              <div className="sched-field">
                <label className="sched-field-label">Database Connection<span className="sched-required">*</span></label>
                <select className="sched-input" value={docConfig.source_connection_id} onChange={setDoc('source_connection_id')}>
                  <option value="">Select a connection…</option>
                  {dbConnections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
                </select>
                {dbConnections.length === 0 && (
                  <div className="sched-field-hint">No database connections yet — create a Postgres/MySQL/SQL Server/etc connection on the Connections page first.</div>
                )}
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Source Label<span className="sched-required">*</span></label>
                <input className="sched-input" value={docConfig.source_path} onChange={setDoc('source_path')} placeholder="AP invoices table" />
                <div className="sched-field-hint">A free-text label for this source — shown wherever a file schedule would show its path.</div>
              </div>
              <div className="sched-field" style={{ gridColumn: '1 / -1' }}>
                <label className="sched-field-label">SQL Query<span className="sched-required">*</span></label>
                <textarea
                  className="sched-input" rows={4} style={{ fontFamily: 'monospace', resize: 'vertical' }}
                  value={docConfig.source_query} onChange={setDoc('source_query')}
                  placeholder="SELECT invoice_number, invoice_date, vendor_name, total_amount, line_items FROM ap_invoices"
                />
                <div className="sched-field-hint">
                  Column names should match the Invoice taxonomy's field names (invoice_number, vendor_name, total_amount, ...) — unrecognized columns are still kept as extra fields.
                  A <code>line_items</code> column returning a JSON array becomes the line-items table.
                </div>
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Record Key Column</label>
                <input className="sched-input" value={docConfig.source_record_key_column} onChange={setDoc('source_record_key_column')} placeholder="invoice_number" />
                <div className="sched-field-hint">Column that uniquely identifies a row, used for duplicate detection across runs. Optional — falls back to a content hash.</div>
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Document Type</label>
                <select className="sched-input" value="INVOICE" disabled>
                  {DATABASE_DOCUMENT_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                </select>
                <div className="sched-field-hint">A database source has no OCR/AI stage to auto-detect a type — only Invoice is supported today.</div>
              </div>
              <div className="sched-field" style={{ gridColumn: '1 / -1' }}>
                <div className="sched-field-hint">
                  Database-sourced invoices always go to the Review Queue for a human decision — there's no source page to verify extracted values against,
                  so this never qualifies for straight-through ERP auto-posting (same as a manually-typed invoice).
                </div>
              </div>
            </div>
          ) : docConfig.source_kind === 'API' ? (
            <div className="sched-field-grid">
              <div className="sched-field">
                <label className="sched-field-label">API Connection<span className="sched-required">*</span></label>
                <select className="sched-input" value={docConfig.source_connection_id} onChange={setDoc('source_connection_id')}>
                  <option value="">Select a connection…</option>
                  {apiConnections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
                </select>
                {apiConnections.length === 0 && (
                  <div className="sched-field-hint">No API connections yet — create a REST API connection on the Connections page first.</div>
                )}
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Source Label<span className="sched-required">*</span></label>
                <input className="sched-input" value={docConfig.source_path} onChange={setDoc('source_path')} placeholder="Vendor invoices API" />
                <div className="sched-field-hint">A free-text label for this source — shown wherever a file schedule would show its path.</div>
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Endpoint<span className="sched-required">*</span></label>
                <input className="sched-input" value={docConfig.source_api_endpoint} onChange={setDoc('source_api_endpoint')} placeholder="/invoices" />
                <div className="sched-field-hint">Path (or full URL) appended to the connection's base URL.</div>
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Method</label>
                <select className="sched-input" value={docConfig.source_api_method} onChange={setDoc('source_api_method')}>
                  <option value="GET">GET</option>
                  <option value="POST">POST</option>
                </select>
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Pagination Style</label>
                <select className="sched-input" value={docConfig.source_api_pagination_style} onChange={setDoc('source_api_pagination_style')}>
                  {API_PAGINATION_STYLES.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
                </select>
              </div>
              {docConfig.source_api_pagination_style !== 'none' && (
                <div className="sched-field">
                  <label className="sched-field-label">Page Size</label>
                  <input type="number" min="1" className="sched-input" value={docConfig.source_api_pagination_page_size} onChange={setDoc('source_api_pagination_page_size')} placeholder="100" />
                </div>
              )}
              <div className="sched-field">
                <label className="sched-field-label">JSON Path</label>
                <input className="sched-input" value={docConfig.source_api_json_path} onChange={setDoc('source_api_json_path')} placeholder="data.items" />
                <div className="sched-field-hint">Optional dot-path to the array of records within the response envelope. Leave blank if the response is a top-level array.</div>
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Record Key Column</label>
                <input className="sched-input" value={docConfig.source_record_key_column} onChange={setDoc('source_record_key_column')} placeholder="invoice_number" />
                <div className="sched-field-hint">Field that uniquely identifies a record, used for duplicate detection across runs. Optional — falls back to a content hash.</div>
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Document Type</label>
                <select className="sched-input" value="INVOICE" disabled>
                  {DATABASE_DOCUMENT_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                </select>
                <div className="sched-field-hint">An API source has no OCR/AI stage to auto-detect a type — only Invoice is supported today.</div>
              </div>
              <div className="sched-field" style={{ gridColumn: '1 / -1' }}>
                <div className="sched-field-hint">
                  API-sourced invoices always go to the Review Queue for a human decision — there's no source page to verify extracted values against,
                  so this never qualifies for straight-through ERP auto-posting (same as a manually-typed invoice).
                </div>
              </div>
            </div>
          ) : docConfig.source_kind === 'ERP' ? (
            <div className="sched-field-grid">
              <div className="sched-field">
                <label className="sched-field-label">ERP Connection<span className="sched-required">*</span></label>
                <select className="sched-input" value={docConfig.source_connection_id} onChange={setDoc('source_connection_id')}>
                  <option value="">Select a connection…</option>
                  {erpConnections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
                {erpConnections.length === 0 && (
                  <div className="sched-field-hint">No Oracle ERP Cloud connections yet — create one on the Connections page first.</div>
                )}
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Source Label<span className="sched-required">*</span></label>
                <input className="sched-input" value={docConfig.source_path} onChange={setDoc('source_path')} placeholder="Oracle ERP AP invoices" />
                <div className="sched-field-hint">A free-text label for this source — shown wherever a file schedule would show its path.</div>
              </div>
              <div className="sched-field" style={{ gridColumn: '1 / -1' }}>
                <label className="sched-field-label">Query Filter</label>
                <input className="sched-input" value={docConfig.source_erp_query} onChange={setDoc('source_erp_query')} placeholder="ValidationStatus='NEEDS_REVALIDATION'" />
                <div className="sched-field-hint">Optional Oracle REST query-language filter, passed through unmodified. Leave blank to pull every invoice the connection's credentials can see.</div>
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Record Key Column</label>
                <input className="sched-input" value={docConfig.source_record_key_column} onChange={setDoc('source_record_key_column')} placeholder="invoice_number" />
                <div className="sched-field-hint">Field that uniquely identifies a record, used for duplicate detection across runs. Optional — falls back to a content hash.</div>
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Document Type</label>
                <select className="sched-input" value="INVOICE" disabled>
                  {DATABASE_DOCUMENT_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                </select>
                <div className="sched-field-hint">An ERP source has no OCR/AI stage to auto-detect a type — only Invoice is supported today.</div>
              </div>
              <div className="sched-field" style={{ gridColumn: '1 / -1' }}>
                <div className="sched-field-hint">
                  ERP-sourced invoices always go to the Review Queue for a human decision — there's no source page to verify extracted values against,
                  so this never qualifies for straight-through ERP auto-posting (same as a manually-typed invoice).
                </div>
              </div>
            </div>
          ) : (
            <div className="sched-field-grid">
              <div className="sched-field">
                <label className="sched-field-label">Connection<span className="sched-required">*</span></label>
                <select className="sched-input" value={docConfig.source_connection_id} onChange={setDoc('source_connection_id')}>
                  <option value="">Select a connection…</option>
                  {storageConnections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
                </select>
                {storageConnections.length === 0 && (
                  <div className="sched-field-hint">No storage connections yet — create an SFTP/FTP/Network File/Azure/AWS connection on the Connections page first.</div>
                )}
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Path<span className="sched-required">*</span></label>
                <input className="sched-input" value={docConfig.source_path} onChange={setDoc('source_path')} placeholder="/incoming/invoices" />
              </div>
              <div className="sched-field">
                <label className="sched-field-label">File Pattern</label>
                <input className="sched-input" value={docConfig.file_pattern} onChange={setDoc('file_pattern')} placeholder="*.pdf" />
              </div>
              <div className="sched-field">
                <label className="sched-field-label">Document Type</label>
                {sourceType === 'INVOICE_SOURCE' ? (
                  <>
                    <select className="sched-input" value="INVOICE" disabled>
                      {DATABASE_DOCUMENT_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                    </select>
                    <div className="sched-field-hint">Pinned by the Invoice Source type selected above.</div>
                  </>
                ) : (
                  <>
                    <select className="sched-input" value={docConfig.document_type} onChange={setDoc('document_type')}>
                      {DOCUMENT_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                    </select>
                    <div className="sched-field-hint">Pins intake to a known canonical schema instead of auto-detecting — required for straight-through ERP auto-post below.</div>
                  </>
                )}
              </div>
              <label className="sched-field sched-field-checkbox">
                <input type="checkbox" checked={docConfig.recursive_scan} onChange={setDoc('recursive_scan')} />
                <span>Scan subdirectories recursively</span>
              </label>
            </div>
          )}

          <div className="sched-section-title">Processing</div>
          <div className="sched-field-grid">
            <div className="sched-field">
              <label className="sched-field-label">Batch Size</label>
              <input type="number" min="1" className="sched-input" value={docConfig.batch_size} onChange={setDoc('batch_size')} />
            </div>
            <div className="sched-field">
              <label className="sched-field-label">{docConfig.source_kind === 'FILE' ? 'Max Concurrent Files' : 'Max Concurrent Records'}</label>
              <input type="number" min="1" className="sched-input" value={docConfig.max_concurrency} onChange={setDoc('max_concurrency')} />
            </div>
            {docConfig.source_kind === 'FILE' && (
              <div className="sched-field">
                <label className="sched-field-label">Max File Size (MB)</label>
                <input type="number" min="1" className="sched-input" value={docConfig.max_file_size_mb} onChange={setDoc('max_file_size_mb')} />
              </div>
            )}
            <div className="sched-field">
              <label className="sched-field-label">Duplicate Content</label>
              <select className="sched-input" value={docConfig.duplicate_content_behavior} onChange={setDoc('duplicate_content_behavior')}>
                {DUPLICATE_BEHAVIORS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
              </select>
            </div>
          </div>

          <div className="sched-section-title">Validation</div>
          <div className="sched-field-grid">
            <div className="sched-field">
              <label className="sched-field-label">Confidence Threshold (%)</label>
              <input type="number" min="0" max="100" className="sched-input" value={docConfig.confidence_threshold} onChange={setDoc('confidence_threshold')} />
              <div className="sched-field-hint">Below this, a document is routed to Review Queue instead of auto-completing.</div>
            </div>
            <div className="sched-field">
              <label className="sched-field-label">Accuracy Threshold (%)</label>
              <input type="number" min="0" max="100" className="sched-input" value={docConfig.accuracy_threshold} onChange={setDoc('accuracy_threshold')} />
            </div>
          </div>

          {docConfig.source_kind === 'FILE' && (
          <>
          <div className="sched-section-title">Post-Processing</div>
          <div className="sched-field-grid">
            <label className="sched-field sched-field-checkbox">
              <input type="checkbox" checked={docConfig.archive_after_success} onChange={setDoc('archive_after_success')} />
              <span>Archive file after successful processing</span>
            </label>
            {docConfig.archive_after_success && (
              <div className="sched-field">
                <label className="sched-field-label">Archive Path<span className="sched-required">*</span></label>
                <input className="sched-input" value={docConfig.archive_path} onChange={setDoc('archive_path')} placeholder="/archive" />
              </div>
            )}
          </div>
          </>
          )}

          {(sourceType === 'INVOICE_SOURCE' || docConfig.document_type === 'INVOICE') && docConfig.source_kind === 'FILE' && (
            <>
              <div className="sched-section-title">Invoice ERP Posting</div>
              <div className="sched-field-grid">
                <label className="sched-field sched-field-checkbox">
                  <input type="checkbox" checked={docConfig.invoice_auto_post} onChange={setDoc('invoice_auto_post')} />
                  <span>Auto-post straight-through invoices to ERP</span>
                </label>
                {docConfig.invoice_auto_post && (
                  <div className="sched-field">
                    <label className="sched-field-label">ERP Connection<span className="sched-required">*</span></label>
                    <select className="sched-input" value={docConfig.invoice_erp_connection_id} onChange={setDoc('invoice_erp_connection_id')}>
                      <option value="">Select an ERP connection…</option>
                      {erpConnections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                    </select>
                    {erpConnections.length === 0 && (
                      <div className="sched-field-hint">No Oracle ERP Cloud connections yet — create one on the Connections page first.</div>
                    )}
                  </div>
                )}
              </div>
              <div className="sched-field-hint" style={{ marginTop: -8, marginBottom: 12 }}>
                Only invoices the decision engine scores as STRAIGHT_THROUGH are ever auto-posted — anything needing review still goes to the Review Queue exactly as today, regardless of this setting.
              </div>
            </>
          )}
        </>
      )}

      <div className="sched-form-divider" />

      <button type="button" className="sched-advanced-toggle" onClick={() => setAdvancedOpen((o) => !o)}>
        {advancedOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Advanced Options
      </button>

      {advancedOpen && (
        <div className="sched-field-grid" style={{ marginTop: 12 }}>
          <div className="sched-field">
            <label className="sched-field-label">Retry Count</label>
            <input type="number" min="0" className="sched-input" value={form.retry_count} onChange={set('retry_count')} />
          </div>
          <div className="sched-field">
            <label className="sched-field-label">Retry Interval (seconds)</label>
            <input type="number" min="0" className="sched-input" value={form.retry_interval_seconds} onChange={set('retry_interval_seconds')} />
          </div>
          <div className="sched-field">
            <label className="sched-field-label">Timeout (seconds)</label>
            <input type="number" min="1" className="sched-input" value={form.timeout_seconds} onChange={set('timeout_seconds')} />
          </div>
          <div className="sched-field">
            <label className="sched-field-label">Concurrency</label>
            <input type="number" min="1" className="sched-input" value={form.concurrency} onChange={set('concurrency')} />
          </div>
          <label className="sched-field sched-field-checkbox">
            <input type="checkbox" checked={form.notify_on_failure} onChange={set('notify_on_failure')} />
            <span>Failure Notification</span>
          </label>
          <label className="sched-field sched-field-checkbox">
            <input type="checkbox" checked={form.notify_on_success} onChange={set('notify_on_success')} />
            <span>Success Notification</span>
          </label>
          <div className="sched-field" style={{ gridColumn: '1 / -1' }}>
            <label className="sched-field-label">Notification Emails</label>
            <input className="sched-input" value={form.notification_emails} onChange={set('notification_emails')} placeholder="alerts@company.com, team@company.com" />
          </div>
        </div>
      )}
    </Modal>
  );
}
