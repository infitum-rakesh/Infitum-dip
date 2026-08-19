/**
 * Settings — /app/admin/settings
 * Organization-level configuration across 8 sections. Each tab is its own
 * independent dirty-state form (same pattern as UserDetails' ProfileTab /
 * RoleDetails' PermissionsTab): local draft initialized from server data,
 * a Save button that's disabled until something in that tab actually
 * changed, and a save that PATCHes only that one section — other tabs'
 * in-progress edits are never touched or lost.
 *
 * There are no secret fields on this page by design: AI/OCR provider
 * *credentials* live in Connections → Manage Secret (CONNECTION_MANAGE_SECRET)
 * and are never duplicated or displayed here — Settings only stores which
 * provider/model/threshold to use, never an API key, password, OAuth
 * secret, token secret, or database password. See the note on the AI and
 * Document Intelligence tabs.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Building2, Database, FileScan, Sparkles, Bell, ShieldCheck, ScrollText,
  Workflow, Save, Plus, X, Lock, RefreshCcw,
} from 'lucide-react';
import { Button, PermissionGate, FullPageSpinner, EmptyState } from '@/components/common/index.js';
import { SETTINGS_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { settingsApi } from '@/services/api/settingsApi.js';
import './settings.css';

const TABS = [
  { id: 'general', label: 'General', icon: Building2 },
  { id: 'data', label: 'Data', icon: Database },
  { id: 'document_intelligence', label: 'Document Intelligence', icon: FileScan },
  { id: 'ai', label: 'AI', icon: Sparkles },
  { id: 'notifications', label: 'Notifications', icon: Bell },
  { id: 'security', label: 'Security', icon: ShieldCheck },
  { id: 'audit', label: 'Audit', icon: ScrollText },
  { id: 'pipelines', label: 'Pipelines', icon: Workflow },
];

const TIMEZONES = [
  'UTC', 'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
  'Europe/London', 'Europe/Berlin', 'Europe/Paris', 'Asia/Kolkata', 'Asia/Singapore',
  'Asia/Tokyo', 'Asia/Dubai', 'Australia/Sydney',
];
const DATE_FORMATS = ['YYYY-MM-DD', 'MM/DD/YYYY', 'DD/MM/YYYY', 'DD-MMM-YYYY'];
const LANGUAGES = [
  { value: 'en', label: 'English' }, { value: 'es', label: 'Spanish' }, { value: 'fr', label: 'French' },
  { value: 'de', label: 'German' }, { value: 'pt', label: 'Portuguese' }, { value: 'hi', label: 'Hindi' },
  { value: 'ja', label: 'Japanese' }, { value: 'zh', label: 'Chinese' },
];
const ENVIRONMENTS = ['DEVELOPMENT', 'STAGING', 'PRODUCTION'];
const VISIBILITIES = ['PRIVATE', 'TEAM', 'ORG'];
// "tesseract" needs no Connection — it's the built-in local engine. The
// other three genuinely run OCR through that vendor's real API (see
// connectors/ocr/*), but only once an OCR Provider connection of the
// matching type is configured on the Connections page — see the note
// rendered below the dropdown.
const OCR_PROVIDERS = ['tesseract', 'aws-textract', 'google-vision', 'azure-form-recognizer'];
const AI_PROVIDERS = ['openai', 'anthropic', 'azure-openai', 'google-vertex'];
const MFA_OPTIONS = ['OFF', 'OPTIONAL', 'REQUIRED'];
const AUDIT_LEVELS = ['MINIMAL', 'STANDARD', 'VERBOSE'];

export default function Settings() {
  const [settings, setSettings] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState('general');

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    settingsApi.get().then(setSettings).catch(() => setError('Failed to load settings. Please try again.')).finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async (patch) => {
    const updated = await settingsApi.update(patch);
    setSettings(updated);
    return updated;
  };

  if (loading) return <FullPageSpinner />;
  if (error || !settings) {
    return (
      <div className="stg-page">
        <EmptyState icon={<Lock size={28} />} title="Settings unavailable" description={error ?? 'Could not load organization settings.'}
          action={{ label: 'Retry', onClick: load }} />
      </div>
    );
  }

  return (
    <PermissionGate {...SETTINGS_PERMISSIONS.SETTINGS_VIEW} showReason>
      <div className="stg-page">
        <div className="stg-header">
          <div>
            <h1 className="stg-title">Settings</h1>
            <p className="stg-subtitle">Organization-level configuration for {settings.organization_name}.</p>
          </div>
          <div className="stg-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={load}>Refresh</Button>
          </div>
        </div>

        <div className="stg-meta-line">
          Last updated {formatDateTime(settings.updated_at)}{settings.updated_by_name ? ` by ${settings.updated_by_name}` : ''}
        </div>

        <div className="stg-tab-nav">
          {TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button key={t.id} className={`stg-tab-btn ${tab === t.id ? 'active' : ''}`} onClick={() => setTab(t.id)}>
                <Icon size={14} /> {t.label}
              </button>
            );
          })}
        </div>

        {tab === 'general' && <GeneralTab settings={settings} onSave={save} />}
        {tab === 'data' && <DataTab settings={settings} onSave={save} />}
        {tab === 'document_intelligence' && <DocumentIntelligenceTab settings={settings} onSave={save} />}
        {tab === 'ai' && <AITab settings={settings} onSave={save} />}
        {tab === 'notifications' && <NotificationsTab settings={settings} onSave={save} />}
        {tab === 'security' && <SecurityTab settings={settings} onSave={save} />}
        {tab === 'audit' && <AuditRetentionTab settings={settings} onSave={save} />}
        {tab === 'pipelines' && <PipelinesTab settings={settings} onSave={save} />}
      </div>
    </PermissionGate>
  );
}

// ── Shared field primitives ─────────────────────────────────────

function Field({ label, hint, children }) {
  return (
    <div className="stg-field">
      <label className="stg-field-label">{label}</label>
      {children}
      {hint && <p className="stg-field-hint">{hint}</p>}
    </div>
  );
}

function TextInput(props) { return <input type="text" className="stg-input" {...props} />; }
function NumberInput(props) { return <input type="number" className="stg-input" {...props} />; }
function Select({ options, ...props }) {
  return (
    <select className="stg-input" {...props}>
      {options.map((o) => (typeof o === 'string' ? <option key={o} value={o}>{o}</option> : <option key={o.value} value={o.value}>{o.label}</option>))}
    </select>
  );
}
function CheckboxRow({ label, checked, onChange, description }) {
  return (
    <label className="stg-checkbox-row">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <div>
        <div className="stg-checkbox-label">{label}</div>
        {description && <div className="stg-checkbox-desc">{description}</div>}
      </div>
    </label>
  );
}

function SaveBar({ permission, dirty, saving, error, onSave, onReset }) {
  return (
    <div className="stg-save-bar">
      {error && <div className="stg-form-error">{error}</div>}
      <PermissionGate {...permission} fallback={<p className="stg-readonly-note"><Lock size={12} /> You don't have permission to edit this section.</p>}>
        <div className="stg-save-actions">
          <Button variant="secondary" size="sm" disabled={!dirty || saving} onClick={onReset}>Discard changes</Button>
          <Button variant="primary" size="sm" icon={<Save size={13} />} loading={saving} disabled={!dirty} onClick={onSave}>Save changes</Button>
        </div>
      </PermissionGate>
    </div>
  );
}

/**
 * Shared dirty-state hook for a single settings section.
 * `initial` is a fresh object literal on every render (the caller doesn't
 * memoize it), so the reset effect keys off its serialized value rather
 * than its reference — otherwise it would fire on every render, including
 * the one caused by the user's own edit, and silently wipe the draft back
 * to the server value before it could ever register as dirty.
 */
function useSectionDraft(initial) {
  const [draft, setDraft] = useState(initial);
  const key = JSON.stringify(initial);
  useEffect(() => { setDraft(initial); }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify(draft) !== key;
  const set = (k) => (value) => setDraft((d) => ({ ...d, [k]: value }));
  const reset = () => setDraft(initial);
  return { draft, set, dirty, reset };
}

// ── General ───────────────────────────────────────────────────

function GeneralTab({ settings, onSave }) {
  const initial = {
    organization_name: settings.organization_name,
    logo_url: settings.logo_url ?? '',
    timezone: settings.general.timezone,
    date_format: settings.general.date_format,
    default_language: settings.general.default_language,
  };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const handleSave = async () => {
    setSaving(true); setError('');
    try {
      await onSave({
        organization_name: draft.organization_name,
        logo_url: draft.logo_url || null,
        general: { timezone: draft.timezone, date_format: draft.date_format, default_language: draft.default_language },
      });
    } catch (err) { setError(err?.message ?? 'Could not save General settings.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">General</div>
      <div className="stg-grid">
        <Field label="Organization Name">
          <TextInput value={draft.organization_name} onChange={(e) => set('organization_name')(e.target.value)} />
        </Field>
        <Field label="Logo" hint="URL to the organization's logo image.">
          <TextInput value={draft.logo_url} onChange={(e) => set('logo_url')(e.target.value)} placeholder="https://…" />
        </Field>
        <Field label="Timezone">
          <Select options={TIMEZONES} value={draft.timezone} onChange={(e) => set('timezone')(e.target.value)} />
        </Field>
        <Field label="Date Format">
          <Select options={DATE_FORMATS} value={draft.date_format} onChange={(e) => set('date_format')(e.target.value)} />
        </Field>
        <Field label="Default Language">
          <Select options={LANGUAGES} value={draft.default_language} onChange={(e) => set('default_language')(e.target.value)} />
        </Field>
      </div>
      <SaveBar permission={SETTINGS_PERMISSIONS.SETTINGS_EDIT} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}

// ── Data ──────────────────────────────────────────────────────

function DataTab({ settings, onSave }) {
  const initial = { ...settings.data };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const handleSave = async () => {
    setSaving(true); setError('');
    try { await onSave({ data: draft }); }
    catch (err) { setError(err?.message ?? 'Could not save Data settings.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">Data</div>
      <div className="stg-grid">
        <Field label="Default Environment">
          <Select options={ENVIRONMENTS} value={draft.default_environment} onChange={(e) => set('default_environment')(e.target.value)} />
        </Field>
        <Field label="Default Data Retention" hint="Days before ingested data is eligible for retention cleanup.">
          <NumberInput min={1} value={draft.default_data_retention_days} onChange={(e) => set('default_data_retention_days')(Number(e.target.value))} />
        </Field>
        <Field label="Maximum Upload Size" hint="Megabytes (MB).">
          <NumberInput min={1} value={draft.max_upload_size_mb} onChange={(e) => set('max_upload_size_mb')(Number(e.target.value))} />
        </Field>
        <Field label="Default Dataset Visibility">
          <Select options={VISIBILITIES} value={draft.default_dataset_visibility} onChange={(e) => set('default_dataset_visibility')(e.target.value)} />
        </Field>
      </div>
      <SaveBar permission={SETTINGS_PERMISSIONS.SETTINGS_EDIT} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}

// ── Document Intelligence ────────────────────────────────────────

function DocumentIntelligenceTab({ settings, onSave }) {
  const initial = { ...settings.document_intelligence };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const handleSave = async () => {
    setSaving(true); setError('');
    try { await onSave({ document_intelligence: draft }); }
    catch (err) { setError(err?.message ?? 'Could not save Document Intelligence settings.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">Document Intelligence</div>
      <p className="stg-secret-note"><Lock size={12} /> Provider credentials are configured in Connections → Manage Secret and are never stored or shown here.</p>
      <div className="stg-grid">
        <Field
          label="OCR Provider"
          hint={draft.ocr_provider === 'tesseract' || !draft.ocr_provider
            ? 'Tesseract is the built-in local engine — no connection needed.'
            : 'Requires an active OCR Provider connection of this type on the Connections page, or documents will fail OCR with a clear error.'}
        >
          <Select options={OCR_PROVIDERS} value={draft.ocr_provider} onChange={(e) => set('ocr_provider')(e.target.value)} />
        </Field>
        <Field label="Default OCR Language">
          <Select options={LANGUAGES} value={draft.default_ocr_language} onChange={(e) => set('default_ocr_language')(e.target.value)} />
        </Field>
        <Field label="Default Confidence Threshold" hint="Percent (0–100).">
          <NumberInput min={0} max={100} value={draft.default_confidence_threshold} onChange={(e) => set('default_confidence_threshold')(Number(e.target.value))} />
        </Field>
      </div>
      <SaveBar permission={SETTINGS_PERMISSIONS.SETTINGS_EDIT} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}

// ── AI ────────────────────────────────────────────────────────

function AITab({ settings, onSave }) {
  const initial = { ...settings.ai };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const handleSave = async () => {
    setSaving(true); setError('');
    try { await onSave({ ai: draft }); }
    catch (err) { setError(err?.message ?? 'Could not save AI settings.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">AI</div>
      <p className="stg-secret-note"><Lock size={12} /> API keys for AI providers are configured in Connections → Manage Secret and are never stored or shown here.</p>
      <div className="stg-grid">
        <Field label="AI Provider">
          <Select options={AI_PROVIDERS} value={draft.ai_provider} onChange={(e) => set('ai_provider')(e.target.value)} />
        </Field>
        <Field label="Default Model">
          <TextInput value={draft.default_model} onChange={(e) => set('default_model')(e.target.value)} placeholder="e.g. gpt-4o" />
        </Field>
        <Field label="Temperature" hint="0.0 (deterministic) – 1.0 (creative).">
          <NumberInput min={0} max={1} step={0.1} value={draft.temperature} onChange={(e) => set('temperature')(Number(e.target.value))} />
        </Field>
        <Field label="Default Extraction Confidence Threshold" hint="Percent (0–100).">
          <NumberInput min={0} max={100} value={draft.default_extraction_confidence_threshold} onChange={(e) => set('default_extraction_confidence_threshold')(Number(e.target.value))} />
        </Field>
      </div>
      <SaveBar permission={SETTINGS_PERMISSIONS.SETTINGS_AI} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}

// ── Notifications ─────────────────────────────────────────────

const NOTIFICATION_FIELDS = [
  { key: 'email_notifications_enabled', label: 'Email Notifications', description: 'Master switch for all email notifications below.' },
  { key: 'notify_pipeline_failure', label: 'Pipeline Failure', description: 'Notify when a pipeline run fails.' },
  { key: 'notify_validation_failure', label: 'Validation Failure', description: 'Notify when a validation rule fails.' },
  { key: 'notify_reconciliation_failure', label: 'Reconciliation Failure', description: 'Notify when a reconciliation run finds unmatched records.' },
  { key: 'notify_review_queue', label: 'Review Queue', description: 'Notify when new items enter the review queue.' },
  { key: 'notify_incident_alerts', label: 'Incident Alerts', description: 'Notify when a new incident is reported or escalated.' },
];

function NotificationsTab({ settings, onSave }) {
  const initial = { ...settings.notifications };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const handleSave = async () => {
    setSaving(true); setError('');
    try { await onSave({ notifications: draft }); }
    catch (err) { setError(err?.message ?? 'Could not save Notification settings.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">Notifications</div>
      <div className="stg-checkbox-list">
        {NOTIFICATION_FIELDS.map((f) => (
          <CheckboxRow
            key={f.key} label={f.label} description={f.description}
            checked={draft[f.key]}
            onChange={set(f.key)}
          />
        ))}
      </div>
      <SaveBar permission={SETTINGS_PERMISSIONS.SETTINGS_NOTIFICATIONS} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}

// ── Security ──────────────────────────────────────────────────

function SecurityTab({ settings, onSave }) {
  const initial = { ...settings.security, password_policy: { ...settings.security.password_policy }, ip_restrictions: [...settings.security.ip_restrictions] };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [ipInput, setIpInput] = useState('');

  const setPolicy = (key) => (value) => set('password_policy')({ ...draft.password_policy, [key]: value });
  const addIp = () => {
    const v = ipInput.trim();
    if (!v || draft.ip_restrictions.includes(v)) return;
    set('ip_restrictions')([...draft.ip_restrictions, v]);
    setIpInput('');
  };
  const removeIp = (v) => set('ip_restrictions')(draft.ip_restrictions.filter((x) => x !== v));

  const handleSave = async () => {
    setSaving(true); setError('');
    try { await onSave({ security: draft }); }
    catch (err) { setError(err?.message ?? 'Could not save Security settings.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">Security</div>
      <div className="stg-grid">
        <Field label="Session Timeout" hint="Minutes of inactivity before a session expires.">
          <NumberInput min={1} value={draft.session_timeout_minutes} onChange={(e) => set('session_timeout_minutes')(Number(e.target.value))} />
        </Field>
        <Field label="Multi-Factor Authentication">
          <Select options={MFA_OPTIONS} value={draft.mfa} onChange={(e) => set('mfa')(e.target.value)} />
        </Field>
      </div>

      <div className="stg-subsection-title">Password Policy</div>
      <div className="stg-grid">
        <Field label="Minimum Length">
          <NumberInput min={6} max={64} value={draft.password_policy.min_length} onChange={(e) => setPolicy('min_length')(Number(e.target.value))} />
        </Field>
      </div>
      <div className="stg-checkbox-list">
        <CheckboxRow label="Require uppercase letter" checked={draft.password_policy.require_uppercase} onChange={setPolicy('require_uppercase')} />
        <CheckboxRow label="Require number" checked={draft.password_policy.require_number} onChange={setPolicy('require_number')} />
        <CheckboxRow label="Require symbol" checked={draft.password_policy.require_symbol} onChange={setPolicy('require_symbol')} />
      </div>

      <div className="stg-subsection-title">IP Restrictions</div>
      <p className="stg-field-hint" style={{ marginBottom: 10 }}>Only these IPs/CIDR ranges may sign in. Leave empty to allow any IP.</p>
      {draft.ip_restrictions.length > 0 && (
        <div className="stg-tag-list">
          {draft.ip_restrictions.map((ip) => (
            <span key={ip} className="stg-tag">
              {ip}
              <button type="button" onClick={() => removeIp(ip)}><X size={11} /></button>
            </span>
          ))}
        </div>
      )}
      <div className="stg-add-row">
        <TextInput value={ipInput} onChange={(e) => setIpInput(e.target.value)} placeholder="e.g. 203.0.113.4 or 203.0.113.0/24"
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addIp(); } }} />
        <Button variant="secondary" size="sm" icon={<Plus size={13} />} onClick={addIp}>Add</Button>
      </div>

      <SaveBar permission={SETTINGS_PERMISSIONS.SETTINGS_SECURITY} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}

// ── Audit ─────────────────────────────────────────────────────

function AuditRetentionTab({ settings, onSave }) {
  const initial = { ...settings.audit };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const handleSave = async () => {
    setSaving(true); setError('');
    try { await onSave({ audit: draft }); }
    catch (err) { setError(err?.message ?? 'Could not save Audit settings.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">Audit</div>
      <div className="stg-grid">
        <Field label="Audit Retention" hint="Days audit events are kept before they're eligible for cleanup.">
          <NumberInput min={30} value={draft.audit_retention_days} onChange={(e) => set('audit_retention_days')(Number(e.target.value))} />
        </Field>
        <Field label="Audit Logging Level" hint="VERBOSE captures the most detail; MINIMAL captures the least.">
          <Select options={AUDIT_LEVELS} value={draft.audit_logging_level} onChange={(e) => set('audit_logging_level')(e.target.value)} />
        </Field>
      </div>
      <SaveBar permission={SETTINGS_PERMISSIONS.SETTINGS_EDIT} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}

// ── Pipelines ─────────────────────────────────────────────────

function PipelinesTab({ settings, onSave }) {
  const initial = { ...settings.pipelines };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const handleSave = async () => {
    setSaving(true); setError('');
    try { await onSave({ pipelines: draft }); }
    catch (err) { setError(err?.message ?? 'Could not save Pipeline settings.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">Pipelines</div>
      <div className="stg-grid">
        <Field label="Default Retry Count">
          <NumberInput min={0} max={10} value={draft.default_retry_count} onChange={(e) => set('default_retry_count')(Number(e.target.value))} />
        </Field>
        <Field label="Default Timeout" hint="Seconds.">
          <NumberInput min={1} value={draft.default_timeout_seconds} onChange={(e) => set('default_timeout_seconds')(Number(e.target.value))} />
        </Field>
        <Field label="Maximum Concurrent Pipelines">
          <NumberInput min={1} max={50} value={draft.max_concurrent_pipelines} onChange={(e) => set('max_concurrent_pipelines')(Number(e.target.value))} />
        </Field>
      </div>
      <SaveBar permission={SETTINGS_PERMISSIONS.SETTINGS_EDIT} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}
