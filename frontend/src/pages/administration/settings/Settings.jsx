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
  Workflow, Save, Plus, X, Lock, RefreshCcw, Fingerprint, Copy, Check,
  CheckCircle2, ExternalLink, Server, UploadCloud, Webhook, Eye, EyeOff,
} from 'lucide-react';
import { Button, PermissionGate, FullPageSpinner, EmptyState, StatusBadge } from '@/components/common/index.js';
import { SETTINGS_PERMISSIONS, SSO_PERMISSIONS, DATABASE_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { settingsApi } from '@/services/api/settingsApi.js';
import { ssoApi } from '@/services/api/ssoApi.js';
import { tenantDatabaseApi } from '@/services/api/tenantDatabaseApi.js';
import { tenantMigrationApi } from '@/services/api/tenantMigrationApi.js';
import { documentApi } from '@/services/api/documentApi.js';
import { documentStorageApi } from '@/services/api/documentStorageApi.js';
import { documentWebhookApi } from '@/services/api/documentWebhookApi.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { mappingApi } from '@/services/api/mappingApi.js';
import { TenantDatabaseTab } from '@/components/settings/TenantDatabaseTab.jsx';
import { LANGUAGE_NAMES, languageName } from '@/utils/languageNames.js';
import './settings.css';

const TABS = [
  { id: 'general', label: 'General', icon: Building2 },
  { id: 'data', label: 'Data', icon: Database },
  { id: 'database', label: 'Database', icon: Server },
  { id: 'document_intelligence', label: 'Document Intelligence', icon: FileScan },
  { id: 'processed_data_storage', label: 'Processed Data Storage', icon: UploadCloud },
  { id: 'document_webhook', label: 'External API / Webhook', icon: Webhook },
  { id: 'ai', label: 'AI', icon: Sparkles },
  { id: 'notifications', label: 'Notifications', icon: Bell },
  { id: 'security', label: 'Security', icon: ShieldCheck },
  { id: 'sso', label: 'Single Sign-On', icon: Fingerprint },
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
// Multilingual Document Processing, Phase 10 — same 13 languages as the
// backend's real source of truth (ocr/language_config.py's LANGUAGE_CONFIG),
// "en" always first since it's always implicitly available regardless of
// what an org opts into (see extraction/custom_engine.py's
// _effective_languages() / multilingual_ocr_service.py's same convention).
const MULTILINGUAL_LANGUAGE_CODES = ['en', 'hi', 'kn', 'ta', 'te', 'ml', 'mr', 'bn', 'gu', 'pa', 'or', 'as', 'ur'];
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
        {tab === 'database' && <TenantDatabaseTab api={tenantDatabaseApi} migrationApi={tenantMigrationApi} editPermission={DATABASE_PERMISSIONS.DATABASE_EDIT} />}
        {tab === 'document_intelligence' && <DocumentIntelligenceTab settings={settings} onSave={save} />}
        {tab === 'processed_data_storage' && <ProcessedDataStorageTab />}
        {tab === 'document_webhook' && <DocumentWebhookTab />}
        {tab === 'ai' && <AITab settings={settings} onSave={save} />}
        {tab === 'notifications' && <NotificationsTab settings={settings} onSave={save} />}
        {tab === 'security' && <SecurityTab settings={settings} onSave={save} />}
        {tab === 'sso' && <SsoTab />}
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
function CheckboxRow({ label, checked, onChange, description, disabled = false }) {
  return (
    <label className={`stg-checkbox-row ${disabled ? 'disabled' : ''}`}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
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

  const toggleLanguage = (code) => {
    if (code === 'en') return; // always implicitly included — see hint below
    const current = draft.supported_languages ?? ['en'];
    set('supported_languages')(current.includes(code) ? current.filter((c) => c !== code) : [...current, code]);
  };

  return (
    <>
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
          <Field label="Default OCR Language" hint="Legacy field, kept for backward compatibility — not read by the real multilingual pipeline below.">
            <Select options={LANGUAGES} value={draft.default_ocr_language} onChange={(e) => set('default_ocr_language')(e.target.value)} />
          </Field>
          <Field label="Default Confidence Threshold" hint="Percent (0–100). Generic extraction-confidence gate — see OCR Confidence Threshold below for the OCR-specific one.">
            <NumberInput min={0} max={100} value={draft.default_confidence_threshold} onChange={(e) => set('default_confidence_threshold')(Number(e.target.value))} />
          </Field>
        </div>
        <div style={{ marginTop: 4 }}>
          <CheckboxRow
            label="Enable Generic Field Extraction"
            checked={!!draft.generic_extraction_enabled}
            onChange={() => set('generic_extraction_enabled')(!draft.generic_extraction_enabled)}
            description="Beyond the known fields, also look for other key-value pairs genuinely present in the document (e.g. a vendor-specific reference code). Each one is only kept if the AI model quotes the exact OCR text it found it in — never invented. OFF by default; a controlled rollout — validate on representative documents before relying on it."
          />
        </div>
        <div style={{ marginTop: 4 }}>
          <CheckboxRow
            label="Enable Mapping Profiles"
            checked={!!draft.mapping_profiles_enabled}
            onChange={() => set('mapping_profiles_enabled')(!draft.mapping_profiles_enabled)}
            description="Lets a published Column Mapping profile (Transformation → Mappings) be applied to a document's extracted fields, producing a mapped output (e.g. onto the Invoice Schema). Profiles can still be authored/versioned/published regardless of this flag — it only gates actually applying one to a real document. OFF by default; a controlled rollout."
          />
        </div>
        <div style={{ marginTop: 4 }}>
          <CheckboxRow
            label="Enable Tally Auto-Sync"
            checked={!!draft.tally_auto_sync_enabled}
            onChange={() => set('tally_auto_sync_enabled')(!draft.tally_auto_sync_enabled)}
            description="When an invoice is auto-decisioned STRAIGHT_THROUGH (Invoice Intelligence), automatically build its Tally voucher, submit it through the existing Approval engine, and write it back to Tally — no manual Voucher Preview button needed. Also requires at least one Vendor → Company Mapping row (Tally Reconciliation page) for this to do anything; with no mapping configured, this flag has no effect. OFF by default."
          />
        </div>
        <SaveBar permission={SETTINGS_PERMISSIONS.SETTINGS_EDIT} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
      </div>

      <div className="stg-card">
        <div className="stg-card-title">Languages</div>
        <p className="stg-field-hint" style={{ marginBottom: 14 }}>
          Real multilingual OCR + AI extraction — script-block/hybrid language detection, per-page Tesseract
          language selection, and quality validation all read these values directly (see Document Intelligence's
          real pipeline). Original document text is never translated; these settings only control which languages
          get real OCR recognition and how strictly results are validated.
        </p>
        <div className="stg-grid">
          <Field label="Supported Languages" hint="English is always available regardless of selection — mixed-language documents commonly carry English labels/boilerplate.">
            <div className="stg-lang-grid">
              {MULTILINGUAL_LANGUAGE_CODES.map((code) => (
                <CheckboxRow
                  key={code}
                  label={LANGUAGE_NAMES[code]}
                  checked={code === 'en' || (draft.supported_languages ?? ['en']).includes(code)}
                  onChange={() => toggleLanguage(code)}
                />
              ))}
            </div>
          </Field>
          <Field
            label="Primary Language"
            hint={draft.primary_language === 'auto' || !draft.primary_language
              ? 'Auto — real per-page hybrid language detection picks the OCR language for each page.'
              : `Pinned to ${LANGUAGE_NAMES[draft.primary_language] ?? draft.primary_language} — every page OCRs in this language (+ English); detection is skipped.`}
          >
            <Select
              options={[{ value: 'auto', label: 'Auto (detect per page)' }, ...MULTILINGUAL_LANGUAGE_CODES.map((c) => ({ value: c, label: LANGUAGE_NAMES[c] }))]}
              value={draft.primary_language ?? 'auto'}
              onChange={(e) => set('primary_language')(e.target.value)}
            />
          </Field>
          <Field label="OCR Confidence Threshold" hint="Percent (0–100). Below this, a document is flagged OCR_LOW_CONFIDENCE and routed to Review Queue.">
            <NumberInput min={0} max={100} value={draft.ocr_confidence_threshold ?? 85} onChange={(e) => set('ocr_confidence_threshold')(Number(e.target.value))} />
          </Field>
          <Field label="Language Mismatch Threshold" hint="Percent (0–100). Below this, language detection is treated as ambiguous (OCR_LANGUAGE_MISMATCH) rather than accepted.">
            <NumberInput min={0} max={100} value={draft.language_mismatch_threshold ?? 80} onChange={(e) => set('language_mismatch_threshold')(Number(e.target.value))} />
          </Field>
        </div>
        <div style={{ marginTop: 4 }}>
          <CheckboxRow
            label="Enable translation"
            checked={false}
            onChange={() => {}}
            disabled
            description="Not implemented yet — reserved for a future phase. Original OCR text is never translated regardless of this setting."
          />
        </div>
        <SaveBar permission={SETTINGS_PERMISSIONS.SETTINGS_EDIT} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
      </div>

      <OcrDiagnosticsCard supportedLanguages={draft.supported_languages ?? ['en']} primaryLanguage={draft.primary_language ?? 'auto'} />
      <ScheduledMappingDefaultsCard />
    </>
  );
}

// Scheduled Mapping (Document Intelligence spec, section 7.3) — the
// organisation's default mapping profile per document_type, consulted by
// a Document Source schedule that has no explicit mapping of its own
// (document_mapping_resolution_service.resolve_scheduled_mapping()).
// Independent of the dirty-draft Save flow above — each row upserts/
// clears immediately, same as OcrDiagnosticsCard's own standalone pattern.
function ScheduledMappingDefaultsCard() {
  const [documentTypes, setDocumentTypes] = useState([]);
  const [profiles, setProfiles] = useState([]);
  const [defaults, setDefaults] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [pending, setPending] = useState({}); // documentType -> mapping_id being chosen
  const [busyType, setBusyType] = useState(null);

  const load = useCallback(() => {
    setLoading(true); setError('');
    Promise.all([mappingApi.getDocumentTypes(), mappingApi.list({ status: 'PUBLISHED' }), mappingApi.getMappingDefaults()])
      .then(([types, mappingList, defaultsList]) => {
        setDocumentTypes(types.filter((t) => t !== 'GENERIC'));
        setProfiles((mappingList.items ?? []).filter((m) => m.source_document_type));
        setDefaults(defaultsList.items ?? []);
      })
      .catch((err) => setError(err?.response?.data?.detail ?? err?.message ?? 'Could not load mapping defaults.'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const setDefault = async (documentType) => {
    const mappingId = pending[documentType];
    if (!mappingId) return;
    setBusyType(documentType); setError('');
    try {
      await mappingApi.setMappingDefault({ document_type: documentType, mapping_id: Number(mappingId) });
      load();
    } catch (err) {
      setError(err?.response?.data?.detail ?? err?.message ?? 'Could not set default.');
    } finally {
      setBusyType(null);
    }
  };

  const clearDefault = async (documentType) => {
    setBusyType(documentType); setError('');
    try {
      await mappingApi.clearMappingDefault(documentType);
      load();
    } catch (err) {
      setError(err?.response?.data?.detail ?? err?.message ?? 'Could not clear default.');
    } finally {
      setBusyType(null);
    }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">Scheduled Mapping Defaults</div>
      <p className="stg-field-hint" style={{ marginBottom: 14 }}>
        The mapping profile a Document Source schedule falls back to when it has no explicit profile of its own
        (set per-schedule in the schedule's own form). An explicit schedule-level choice always takes precedence
        over the default below — see the schedule form's "Mapping Profile" field.
      </p>
      {loading ? (
        <p className="text-muted text-sm">Loading…</p>
      ) : error ? (
        <p className="stg-form-error">{error}</p>
      ) : documentTypes.length === 0 ? (
        <p className="text-muted text-sm">No document types available yet.</p>
      ) : (
        <table className="stg-table">
          <thead>
            <tr><th>Document Type</th><th>Current Default</th><th>Set To</th><th></th></tr>
          </thead>
          <tbody>
            {documentTypes.map((docType) => {
              const current = defaults.find((d) => d.document_type === docType);
              const eligible = profiles.filter((m) => m.source_document_type === docType || m.source_document_type === 'GENERIC');
              return (
                <tr key={docType}>
                  <td>{docType}</td>
                  <td>{current ? current.mapping_name : <span className="text-muted">None — preserves original extracted JSON</span>}</td>
                  <td>
                    <select
                      className="sched-input"
                      value={pending[docType] ?? ''}
                      onChange={(e) => setPending((p) => ({ ...p, [docType]: e.target.value }))}
                    >
                      <option value="">Select a mapping profile…</option>
                      {eligible.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                    </select>
                  </td>
                  <td style={{ display: 'flex', gap: 8 }}>
                    <Button size="sm" variant="secondary" disabled={!pending[docType] || busyType === docType} loading={busyType === docType} onClick={() => setDefault(docType)}>Set</Button>
                    {current && <Button size="sm" variant="ghost" disabled={busyType === docType} onClick={() => clearDefault(docType)}>Clear</Button>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

// Multilingual Document Processing, Phase 11 — real Tesseract readiness
// check + admin OCR sandbox. Pure diagnostics: reads the org's current
// (in-memory draft, not-yet-saved) language selection so an admin can
// check pack availability / test a sample before hitting Save above.
function OcrDiagnosticsCard({ supportedLanguages, primaryLanguage }) {
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState('');
  const [healthLoading, setHealthLoading] = useState(true);

  const [testFile, setTestFile] = useState(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [testError, setTestError] = useState('');

  const loadHealth = useCallback(() => {
    setHealthLoading(true); setHealthError('');
    documentApi.getOcrHealth()
      .then(setHealth)
      .catch((err) => setHealthError(err?.message ?? 'Could not check OCR readiness.'))
      .finally(() => setHealthLoading(false));
  }, []);

  useEffect(() => { loadHealth(); }, [loadHealth]);

  const runTest = async () => {
    if (!testFile) return;
    setTesting(true); setTestError(''); setTestResult(null);
    try {
      const result = await documentApi.testOcr(testFile, { supportedLanguages, primaryLanguage });
      setTestResult(result);
    } catch (err) {
      setTestError(err?.response?.data?.detail ?? err?.message ?? 'OCR test failed.');
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">OCR Diagnostics</div>
      <p className="stg-field-hint" style={{ marginBottom: 14 }}>
        Real checks against the local Tesseract engine on this server — not a simulation. Cloud OCR providers
        (AWS Textract / Google Vision / Azure Form Recognizer) aren't exercised here even if selected above.
      </p>

      <div className="stg-subsection-title" style={{ marginTop: 0, paddingTop: 0, borderTop: 'none' }}>Language Pack Status</div>
      {healthLoading ? (
        <p className="text-muted text-sm">Checking…</p>
      ) : healthError ? (
        <p className="stg-form-error">{healthError}</p>
      ) : health ? (
        <>
          <div className="stg-lang-health-row">
            <StatusBadge variant={health.tesseract_available ? 'success' : 'danger'} dot>
              {health.tesseract_available ? 'Tesseract available' : 'Tesseract not available'}
            </StatusBadge>
            {health.tesseract_available && (
              <StatusBadge variant={health.all_configured_languages_ready ? 'success' : 'warning'} dot>
                {health.all_configured_languages_ready ? 'All configured languages ready' : 'Some languages missing'}
              </StatusBadge>
            )}
            <Button variant="secondary" size="sm" icon={<RefreshCcw size={13} />} onClick={loadHealth}>Recheck</Button>
          </div>
          {health.tesseract_error && <p className="stg-form-error" style={{ marginTop: 8 }}>{health.tesseract_error}</p>}
          <div className="stg-lang-health-grid">
            {health.languages.map((l) => (
              <span key={l.code} className="stg-lang-health-pill" title={l.reason ?? undefined}>
                <StatusBadge variant={l.installed ? 'success' : 'danger'} size="sm" dot>{l.name}</StatusBadge>
              </span>
            ))}
          </div>
        </>
      ) : null}

      <div className="stg-subsection-title">Test OCR</div>
      <p className="stg-field-hint" style={{ marginBottom: 10 }}>
        Upload a sample image or PDF to run it through the real OCR pipeline using the Supported Languages /
        Primary Language selection above (including any unsaved changes) — no document is created.
      </p>
      <div className="stg-test-ocr-row">
        <input
          type="file"
          accept="image/*,.pdf"
          onChange={(e) => { setTestFile(e.target.files?.[0] ?? null); setTestResult(null); setTestError(''); }}
        />
        <Button variant="primary" size="sm" disabled={!testFile} loading={testing} onClick={runTest}>Run Test OCR</Button>
      </div>
      {testError && <p className="stg-form-error" style={{ marginTop: 10 }}>{testError}</p>}
      {testResult && (
        <div className="stg-test-ocr-result">
          <div className="stg-lang-health-row">
            <span className="text-muted text-sm">Confidence: <strong>{testResult.confidence}%</strong></span>
            {testResult.language && (
              <StatusBadge variant="info" dot>
                {languageName(testResult.language.primary_language)} · {testResult.language.language_detection_confidence?.toFixed(0)}% · {testResult.language.language_detection_method}
              </StatusBadge>
            )}
          </div>
          {testResult.quality_flags?.length > 0 && (
            <div className="stg-lang-health-row" style={{ marginTop: 6 }}>
              {testResult.quality_flags.map((f) => (
                <span key={f.code} title={f.message}><StatusBadge variant="warning">{f.code.replace(/_/g, ' ')}</StatusBadge></span>
              ))}
            </div>
          )}
          <pre className="docd-ocr-text" style={{ marginTop: 10, maxHeight: 200 }}>{testResult.full_text || '(no text recognized)'}</pre>
        </div>
      )}
    </div>
  );
}

// ── Processed Data Storage ───────────────────────────────────────
// Independent of the OrganizationSettings JSON blob (own model/table —
// see services/document_storage_service.py's module docstring), same
// reasoning as SsoTab/TenantDatabaseTab above. Configures where a
// document's processed output (OCR text, extraction fields, dataset,
// raw AI response) gets pushed after each document finishes — separate
// from and additive to Bring-Your-Own-Database (the Database tab),
// which migrates the org's entire operational schema, not just
// processed-document output.

const STORAGE_MODES = [
  { value: 'DEFAULT', label: 'Default (this app\'s own database only)' },
  { value: 'EXTERNAL', label: 'External (also push to a connection below)' },
];

// Load Mode only applies to a DATABASE-type target (a real TABLE) — a
// FILE target has no equivalent concept beyond "append", which
// append_csv_row()/append_jsonl()/write_parquet_record() already do
// unconditionally. See models/document_storage.py's ProcessedDataStorageLoadMode.
const LOAD_MODES = [
  { value: 'CREATE_IF_NOT_EXISTS', label: 'Create if not exists (upsert by document)' },
  { value: 'APPEND', label: 'Append (insert only — a reprocessed document lands as a new row)' },
  { value: 'TRUNCATE_AND_LOAD', label: 'Truncate and load (clears the table once, then upserts)' },
  { value: 'DROP_AND_RECREATE', label: 'Drop and recreate (drops the table once, then upserts)' },
];

// File Format only applies to a FILE-type target (a Local File connection).
const FILE_FORMATS = [
  { value: 'JSONL', label: 'JSON Lines (.jsonl)' },
  { value: 'CSV', label: 'CSV' },
  { value: 'PARQUET', label: 'Parquet (one file per document)' },
  { value: 'XML', label: 'XML (one file per document)' },
];

function ProcessedDataStorageTab() {
  const [cfg, setCfg] = useState(null);
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  const load = useCallback(() => {
    setLoading(true); setLoadError('');
    Promise.all([
      documentStorageApi.get(),
      // Every destination this feature can write to: real database
      // connectors (Postgres/MySQL/SQL Server/Oracle/Snowflake) plus
      // Local File for the flat-file destination — fetched separately
      // since they're two different categories, then merged for the picker.
      connectionApi.list({ category: 'DATABASE', page_size: 200 }),
      connectionApi.list({ category: 'STORAGE', connection_type: 'LOCAL_FILE', page_size: 200 }),
    ])
      .then(([config, dbList, fileList]) => {
        setCfg(config);
        setConnections([...(dbList.items ?? []), ...(fileList.items ?? [])]);
      })
      .catch((err) => setLoadError(err?.message ?? 'Failed to load Processed Data Storage configuration.'))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);

  if (loading) return <div className="stg-card"><div className="stg-card-title">Processed Data Storage</div><p className="stg-field-hint">Loading…</p></div>;
  if (loadError || !cfg) return <div className="stg-card"><div className="stg-form-error">{loadError || 'Could not load Processed Data Storage configuration.'}</div></div>;

  return <ProcessedDataStorageForm cfg={cfg} connections={connections} onSaved={setCfg} />;
}

function ProcessedDataStorageForm({ cfg, connections, onSaved }) {
  const initial = {
    mode: cfg.mode,
    target_connection_id: cfg.target_connection_id,
    target_schema: cfg.target_schema ?? '',
    target_table: cfg.target_table,
    load_mode: cfg.load_mode ?? 'CREATE_IF_NOT_EXISTS',
    file_format: cfg.file_format ?? 'JSONL',
    include_ocr_text: cfg.include_ocr_text,
    include_extraction_fields: cfg.include_extraction_fields,
    include_dataset: cfg.include_dataset,
    include_ai_raw_response: cfg.include_ai_raw_response,
  };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);

  const selectedConnection = connections.find((c) => c.id === draft.target_connection_id);

  const handleSave = async () => {
    setSaving(true); setError(''); setTestResult(null);
    try {
      const updated = await documentStorageApi.update({
        mode: draft.mode,
        target_connection_id: draft.target_connection_id,
        clear_target_connection: draft.target_connection_id == null,
        target_schema: draft.target_schema || null,
        clear_target_schema: !draft.target_schema,
        target_table: draft.target_table,
        load_mode: draft.load_mode,
        file_format: draft.file_format,
        include_ocr_text: draft.include_ocr_text,
        include_extraction_fields: draft.include_extraction_fields,
        include_dataset: draft.include_dataset,
        include_ai_raw_response: draft.include_ai_raw_response,
      });
      onSaved(updated);
    } catch (err) {
      setError(err?.response?.data?.detail ?? err?.message ?? 'Could not save Processed Data Storage settings.');
    } finally {
      setSaving(false);
    }
  };

  const runTest = async () => {
    setTesting(true); setTestResult(null); setError('');
    try {
      const result = await documentStorageApi.testConnection();
      setTestResult(result);
    } catch (err) {
      setError(err?.response?.data?.detail ?? err?.message ?? 'Could not test the target connection.');
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">Processed Data Storage</div>
      <p className="stg-field-hint" style={{ marginBottom: 14 }}>
        Controls where a document's processed output (OCR text, extraction fields, dataset, raw AI response) lands
        after it finishes processing. <strong>Default</strong> keeps it only on the document itself, in whatever
        database this app's tenant data already lives in (see the Database tab for that). <strong>External</strong>{' '}
        additionally pushes a flattened copy to the connection below every time a document completes — independent
        of Bring-Your-Own-Database, which migrates this app's entire schema rather than just processed output.
      </p>

      <div className="stg-grid">
        <Field label="Mode">
          <Select options={STORAGE_MODES} value={draft.mode} onChange={(e) => set('mode')(e.target.value)} />
        </Field>
      </div>

      {draft.mode === 'EXTERNAL' && (
        <>
          <div className="stg-subsection-title" style={{ marginTop: 0, paddingTop: 0, borderTop: 'none' }}>Target</div>
          <div className="stg-grid">
            <Field label="Connection" hint="A database connection (Postgres, MySQL, SQL Server, Oracle, Snowflake) or a Local File connection, configured on the Connections page.">
              <Select
                options={[
                  { value: '', label: '— Select a connection —' },
                  ...connections.map((c) => ({ value: String(c.id), label: `${c.name} (${c.connection_type})` })),
                ]}
                value={draft.target_connection_id != null ? String(draft.target_connection_id) : ''}
                onChange={(e) => set('target_connection_id')(e.target.value ? Number(e.target.value) : null)}
              />
            </Field>
            {selectedConnection?.connection_type !== 'LOCAL_FILE' && (
              <Field label="Schema" hint="Optional — leave blank to use the connection's default schema.">
                <TextInput value={draft.target_schema} onChange={(e) => set('target_schema')(e.target.value)} placeholder="e.g. public" />
              </Field>
            )}
            <Field
              label={selectedConnection?.connection_type === 'LOCAL_FILE' ? 'File path' : 'Table name'}
              hint={selectedConnection?.connection_type === 'LOCAL_FILE'
                ? 'Relative to the connection\'s base path.'
                : 'Created automatically on first push if it doesn\'t already exist.'}
            >
              <TextInput value={draft.target_table} onChange={(e) => set('target_table')(e.target.value)} />
            </Field>
            {selectedConnection?.connection_type === 'LOCAL_FILE' ? (
              <Field label="File Format" hint="JSON Lines/CSV append to the file; Parquet writes one file per document under this path.">
                <Select options={FILE_FORMATS} value={draft.file_format} onChange={(e) => set('file_format')(e.target.value)} />
              </Field>
            ) : selectedConnection && (
              <Field label="Load Mode" hint="Truncate/Drop only run ONCE, the next time a document pushes after being selected — not on every push.">
                <Select options={LOAD_MODES} value={draft.load_mode} onChange={(e) => set('load_mode')(e.target.value)} />
              </Field>
            )}
          </div>

          <div className="stg-add-row" style={{ marginBottom: 4 }}>
            <Button variant="secondary" size="sm" disabled={!draft.target_connection_id} loading={testing} onClick={runTest}>
              Test Connection
            </Button>
            {testResult && (
              <StatusBadge variant={testResult.success ? 'success' : 'danger'} dot>
                {testResult.message}{testResult.latency_ms != null ? ` (${testResult.latency_ms}ms)` : ''}
              </StatusBadge>
            )}
          </div>

          <div className="stg-subsection-title">Fields to include</div>
          <div className="stg-checkbox-list">
            <CheckboxRow label="OCR text" description="Full recognized text and OCR confidence." checked={draft.include_ocr_text} onChange={set('include_ocr_text')} />
            <CheckboxRow label="Extraction fields" description="AI/Custom-Engine extracted fields." checked={draft.include_extraction_fields} onChange={set('include_extraction_fields')} />
            <CheckboxRow label="Dataset" description="The structured dataset built from this document." checked={draft.include_dataset} onChange={set('include_dataset')} />
            <CheckboxRow label="Raw AI response" description="Verbatim raw AI model payload — larger and vendor-shaped; off by default." checked={draft.include_ai_raw_response} onChange={set('include_ai_raw_response')} />
          </div>
        </>
      )}

      <div className="stg-subsection-title">Last push</div>
      <div className="stg-lang-health-row">
        <StatusBadge variant={cfg.last_status === 'SUCCESS' ? 'success' : cfg.last_status === 'FAILED' ? 'danger' : 'gray'} dot>
          {cfg.last_status === 'NEVER_RUN' ? 'Never run' : cfg.last_status}
        </StatusBadge>
        {cfg.last_pushed_document_id != null && (
          <span className="text-muted text-sm">Document #{cfg.last_pushed_document_id}{cfg.last_pushed_at ? ` · ${formatDateTime(cfg.last_pushed_at)}` : ''}</span>
        )}
      </div>
      {cfg.last_error && <p className="stg-form-error" style={{ marginTop: 8 }}>{cfg.last_error}</p>}

      <SaveBar permission={SETTINGS_PERMISSIONS.SETTINGS_EDIT} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}

// ── Document Webhook ─────────────────────────────────────────────
// Genuinely separate feature from Processed Data Storage above — pushes
// the same kind of processed-document payload to a customer-owned
// external HTTP endpoint instead of a database/file, with real
// idempotency/retry/circuit-breaker on the backend (see services/
// document_webhook_service.py's module docstring for why this isn't a
// third mode bolted onto Processed Data Storage).

const WEBHOOK_AUTH_TYPES = [
  { value: 'NONE', label: 'None' },
  { value: 'API_KEY', label: 'API Key (custom header)' },
  { value: 'BEARER', label: 'Bearer Token' },
  { value: 'BASIC', label: 'Basic Auth (user:pass)' },
  { value: 'OAUTH2', label: 'OAuth2 (client credentials)' },
];

const WEBHOOK_HTTP_METHODS = [
  { value: 'POST', label: 'POST' },
  { value: 'PUT', label: 'PUT' },
  { value: 'PATCH', label: 'PATCH' },
];

/** Parses a textarea's "key: value" lines (one per line) into a flat
 * object — the simplest real editor for custom_headers/field_mapping
 * without building a dedicated repeatable-row component. Blank lines and
 * lines without a colon are ignored; returns null (not {}) when the
 * result would be empty, matching the backend's "no custom headers"
 * default. Throws a descriptive Error on a genuinely malformed line
 * (colon-less, non-blank) so the caller can surface it before saving. */
function parseKeyValueLines(text) {
  const result = {};
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim();
    if (!line) continue;
    const idx = line.indexOf(':');
    if (idx === -1) throw new Error(`"${line}" is missing a colon — use "Key: Value" per line.`);
    const key = line.slice(0, idx).trim();
    const value = line.slice(idx + 1).trim();
    if (!key) throw new Error(`"${line}" has no key before the colon.`);
    result[key] = value;
  }
  return Object.keys(result).length ? result : null;
}

function keyValueLinesFromObject(obj) {
  if (!obj) return '';
  return Object.entries(obj).map(([k, v]) => `${k}: ${v}`).join('\n');
}

function DocumentWebhookTab() {
  const [cfg, setCfg] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  const load = useCallback(() => {
    setLoading(true); setLoadError('');
    documentWebhookApi.get()
      .then(setCfg)
      .catch((err) => setLoadError(err?.message ?? 'Failed to load webhook configuration.'))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);

  if (loading) return <div className="stg-card"><div className="stg-card-title">External API / Webhook</div><p className="stg-field-hint">Loading…</p></div>;
  if (loadError || !cfg) return <div className="stg-card"><div className="stg-form-error">{loadError || 'Could not load webhook configuration.'}</div></div>;

  return <DocumentWebhookForm cfg={cfg} onSaved={setCfg} />;
}

function DocumentWebhookForm({ cfg, onSaved }) {
  const initial = {
    enabled: cfg.enabled,
    url: cfg.url ?? '',
    http_method: cfg.http_method ?? 'POST',
    auth_type: cfg.auth_type,
    auth_header_name: cfg.auth_header_name,
    oauth_token_url: cfg.oauth_token_url ?? '',
    oauth_client_id: cfg.oauth_client_id ?? '',
    oauth_scope: cfg.oauth_scope ?? '',
    custom_headers_text: keyValueLinesFromObject(cfg.custom_headers),
    field_mapping_text: keyValueLinesFromObject(cfg.field_mapping),
    timeout_seconds: cfg.timeout_seconds ?? 15,
    include_ocr_text: cfg.include_ocr_text,
    include_extraction_fields: cfg.include_extraction_fields,
    include_dataset: cfg.include_dataset,
    include_ai_raw_response: cfg.include_ai_raw_response,
  };
  const { draft, set, dirty: fieldsDirty, reset: resetFields } = useSectionDraft(initial);
  const [secretInput, setSecretInput] = useState(''); // blank = leave stored secret unchanged
  const [oauthSecretInput, setOauthSecretInput] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [signingSecret, setSigningSecret] = useState(null);
  const [revealingSecret, setRevealingSecret] = useState(false);
  const [deliveries, setDeliveries] = useState(null);
  const [deliveriesError, setDeliveriesError] = useState('');

  const dirty = fieldsDirty || secretInput.trim().length > 0 || oauthSecretInput.trim().length > 0;
  const reset = () => { resetFields(); setSecretInput(''); setOauthSecretInput(''); };

  const handleSave = async () => {
    setSaving(true); setError(''); setTestResult(null);
    let customHeaders, fieldMapping;
    try {
      customHeaders = parseKeyValueLines(draft.custom_headers_text);
      fieldMapping = parseKeyValueLines(draft.field_mapping_text);
    } catch (err) {
      setError(err.message);
      setSaving(false);
      return;
    }
    try {
      const body = {
        enabled: draft.enabled, url: draft.url || null, http_method: draft.http_method, auth_type: draft.auth_type,
        auth_header_name: draft.auth_header_name,
        oauth_token_url: draft.oauth_token_url || null, oauth_client_id: draft.oauth_client_id || null,
        oauth_scope: draft.oauth_scope || null,
        custom_headers: customHeaders, field_mapping: fieldMapping,
        timeout_seconds: Number(draft.timeout_seconds) || 15,
        include_ocr_text: draft.include_ocr_text, include_extraction_fields: draft.include_extraction_fields,
        include_dataset: draft.include_dataset, include_ai_raw_response: draft.include_ai_raw_response,
      };
      if (secretInput.trim()) body.secret = secretInput.trim();
      if (oauthSecretInput.trim()) body.oauth_client_secret = oauthSecretInput.trim();
      const updated = await documentWebhookApi.update(body);
      onSaved(updated);
      setSecretInput('');
      setOauthSecretInput('');
    } catch (err) {
      setError(err?.response?.data?.detail ?? err?.message ?? 'Could not save webhook settings.');
    } finally {
      setSaving(false);
    }
  };

  const runTest = async () => {
    setTesting(true); setTestResult(null); setError('');
    try {
      const result = await documentWebhookApi.test();
      setTestResult(result);
    } catch (err) {
      setError(err?.response?.data?.detail ?? err?.message ?? 'Could not send a test delivery.');
    } finally {
      setTesting(false);
    }
  };

  const revealSigningSecret = async () => {
    if (signingSecret) { setSigningSecret(null); return; }
    setRevealingSecret(true);
    try {
      const result = await documentWebhookApi.getSigningSecret();
      setSigningSecret(result.signing_secret ?? '(none yet — enable the webhook once to generate one)');
    } catch (err) {
      setError(err?.message ?? 'Could not load the signing secret.');
    } finally {
      setRevealingSecret(false);
    }
  };

  const loadDeliveries = async () => {
    setDeliveriesError('');
    try {
      const result = await documentWebhookApi.listDeliveries({ limit: 20 });
      setDeliveries(result.items);
    } catch (err) {
      setDeliveriesError(err?.message ?? 'Could not load delivery history.');
    }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">External API / Webhook</div>
      <p className="stg-field-hint" style={{ marginBottom: 14 }}>
        Pushes a document's processed output (OCR text, extraction fields, dataset, raw AI response) to a URL you
        control, right after each document finishes — a real HTTP POST with automatic retry and a circuit breaker if
        your endpoint is down, distinct from Processed Data Storage above (which writes to a database or file).
        Every delivery is signed with HMAC-SHA256 (see the signing secret below) so your endpoint can verify it
        genuinely came from this platform.
      </p>

      <div className="stg-checkbox-list" style={{ marginBottom: 4 }}>
        <CheckboxRow
          label="Enable webhook delivery" checked={draft.enabled} onChange={set('enabled')}
          description="Sends a real HTTP POST to the URL below after every document that finishes processing."
        />
      </div>

      <div className="stg-grid">
        <Field label="Webhook URL">
          <TextInput value={draft.url} onChange={(e) => set('url')(e.target.value)} placeholder="https://your-app.example.com/webhooks/documents" />
        </Field>
        <Field label="HTTP Method">
          <Select options={WEBHOOK_HTTP_METHODS} value={draft.http_method} onChange={(e) => set('http_method')(e.target.value)} />
        </Field>
        <Field label="Auth Type">
          <Select options={WEBHOOK_AUTH_TYPES} value={draft.auth_type} onChange={(e) => set('auth_type')(e.target.value)} />
        </Field>
        {draft.auth_type === 'API_KEY' && (
          <Field label="Header Name">
            <TextInput value={draft.auth_header_name} onChange={(e) => set('auth_header_name')(e.target.value)} placeholder="X-API-Key" />
          </Field>
        )}
        <Field label="Timeout (seconds)" hint="How long to wait for the endpoint to respond before treating the attempt as failed.">
          <input
            type="number" min="1" className="stg-input" value={draft.timeout_seconds}
            onChange={(e) => set('timeout_seconds')(e.target.value)}
          />
        </Field>
      </div>

      {draft.auth_type !== 'NONE' && draft.auth_type !== 'OAUTH2' && (
        <Field
          label={draft.auth_type === 'BASIC' ? 'Secret (user:pass)' : draft.auth_type === 'BEARER' ? 'Bearer Token' : 'API Key Value'}
          hint={cfg.has_secret ? 'Leave blank to keep the current secret, or type a new one to replace it.' : 'Required before enabling this auth type.'}
        >
          <input
            type="password" className="stg-input" value={secretInput} onChange={(e) => setSecretInput(e.target.value)}
            placeholder={cfg.has_secret ? '••••••••••••' : 'Enter secret'}
          />
        </Field>
      )}

      {draft.auth_type === 'OAUTH2' && (
        <>
          <div className="stg-subsection-title" style={{ marginTop: 0, paddingTop: 0, borderTop: 'none' }}>OAuth2 client credentials</div>
          <div className="stg-grid">
            <Field label="Token URL">
              <TextInput value={draft.oauth_token_url} onChange={(e) => set('oauth_token_url')(e.target.value)} placeholder="https://auth.example.com/oauth/token" />
            </Field>
            <Field label="Client ID">
              <TextInput value={draft.oauth_client_id} onChange={(e) => set('oauth_client_id')(e.target.value)} />
            </Field>
            <Field
              label="Client Secret"
              hint={cfg.has_oauth_client_secret ? 'Leave blank to keep the current secret, or type a new one to replace it.' : 'Required before enabling OAuth2.'}
            >
              <input
                type="password" className="stg-input" value={oauthSecretInput} onChange={(e) => setOauthSecretInput(e.target.value)}
                placeholder={cfg.has_oauth_client_secret ? '••••••••••••' : 'Enter client secret'}
              />
            </Field>
            <Field label="Scope" hint="Optional — space-separated OAuth2 scopes, if the token endpoint requires one.">
              <TextInput value={draft.oauth_scope} onChange={(e) => set('oauth_scope')(e.target.value)} />
            </Field>
          </div>
        </>
      )}

      <div className="stg-add-row" style={{ marginBottom: 4 }}>
        <Button variant="secondary" size="sm" disabled={!draft.url} loading={testing} onClick={runTest}>
          Test Webhook
        </Button>
        {testResult && (
          <StatusBadge variant={testResult.success ? 'success' : 'danger'} dot>
            {testResult.success ? 'Delivered' : 'Failed'}{testResult.status_code != null ? ` (HTTP ${testResult.status_code})` : ''}
          </StatusBadge>
        )}
      </div>
      {testResult && !testResult.success && testResult.response_body && (
        <p className="stg-form-error" style={{ marginTop: 4 }}>{testResult.response_body}</p>
      )}

      <div className="stg-subsection-title">Custom headers</div>
      <p className="stg-field-hint" style={{ marginTop: 0 }}>
        One header per line, as <code>Name: Value</code>. Applied to every delivery — can never override the
        idempotency key, content type, auth, or signature headers this feature sets itself.
      </p>
      <textarea
        className="stg-textarea" rows={3} value={draft.custom_headers_text}
        onChange={(e) => set('custom_headers_text')(e.target.value)}
        placeholder={'X-Tenant-Id: acme\nX-Source: dip-platform'}
      />

      <div className="stg-subsection-title">Field mapping</div>
      <p className="stg-field-hint" style={{ marginTop: 0 }}>
        Renames outbound JSON keys before sending, as <code>original_key: renamed_key</code> — one per line. A key
        not listed here is sent under its original name.
      </p>
      <textarea
        className="stg-textarea" rows={3} value={draft.field_mapping_text}
        onChange={(e) => set('field_mapping_text')(e.target.value)}
        placeholder={'document_id: docId\nfilename: fileName'}
      />

      <div className="stg-subsection-title">Fields to include</div>
      <div className="stg-checkbox-list">
        <CheckboxRow label="OCR text" description="Full recognized text and OCR confidence." checked={draft.include_ocr_text} onChange={set('include_ocr_text')} />
        <CheckboxRow label="Extraction fields" description="AI/Custom-Engine extracted fields." checked={draft.include_extraction_fields} onChange={set('include_extraction_fields')} />
        <CheckboxRow label="Dataset" description="The structured dataset built from this document." checked={draft.include_dataset} onChange={set('include_dataset')} />
        <CheckboxRow label="Raw AI response" description="Verbatim raw AI model payload — larger and vendor-shaped; off by default." checked={draft.include_ai_raw_response} onChange={set('include_ai_raw_response')} />
      </div>

      <div className="stg-subsection-title">Signing secret</div>
      <p className="stg-field-hint" style={{ marginTop: 0 }}>
        Every delivery carries an <code>X-Signature</code> header — HMAC-SHA256 of the request body, using this
        secret. Verify it on your endpoint to confirm a delivery genuinely came from this platform.
      </p>
      <div className="stg-add-row">
        <Button variant="secondary" size="sm" loading={revealingSecret} onClick={revealSigningSecret}>
          {signingSecret ? <><EyeOff size={14} /> Hide</> : <><Eye size={14} /> Reveal signing secret</>}
        </Button>
      </div>
      {signingSecret && <div className="stg-cert-status set" style={{ marginTop: 8, wordBreak: 'break-all' }}>{signingSecret}</div>}

      <div className="stg-subsection-title">Last delivery</div>
      <div className="stg-lang-health-row">
        <StatusBadge variant={cfg.last_status === 'SUCCESS' ? 'success' : cfg.last_status === 'FAILED' ? 'danger' : 'gray'} dot>
          {cfg.last_status === 'NEVER_RUN' ? 'Never run' : cfg.last_status}
        </StatusBadge>
        {cfg.last_pushed_document_id != null && (
          <span className="text-muted text-sm">Document #{cfg.last_pushed_document_id}{cfg.last_pushed_at ? ` · ${formatDateTime(cfg.last_pushed_at)}` : ''}</span>
        )}
      </div>
      {cfg.last_error && <p className="stg-form-error" style={{ marginTop: 8 }}>{cfg.last_error}</p>}

      <div className="stg-subsection-title">Delivery history</div>
      {deliveries === null ? (
        <Button variant="secondary" size="sm" onClick={loadDeliveries}>Load delivery history</Button>
      ) : (
        <>
          {deliveriesError && <p className="stg-form-error">{deliveriesError}</p>}
          {deliveries.length === 0 ? (
            <p className="stg-field-hint">No delivery attempts yet.</p>
          ) : (
            <table className="stg-table">
              <thead>
                <tr><th>Document</th><th>Attempt</th><th>Status</th><th>HTTP</th><th>Error</th><th>When</th></tr>
              </thead>
              <tbody>
                {deliveries.map((d) => (
                  <tr key={d.id}>
                    <td>#{d.document_id}</td>
                    <td>{d.attempt_number}</td>
                    <td><StatusBadge variant={d.status === 'SUCCEEDED' ? 'success' : d.status === 'DEAD_LETTER' ? 'danger' : d.status === 'RETRYING' ? 'warning' : 'gray'} dot>{d.status}</StatusBadge></td>
                    <td>{d.http_status_code ?? '—'}</td>
                    <td className="text-muted text-sm">{d.error_message ?? '—'}</td>
                    <td className="text-muted text-sm">{formatDateTime(d.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}

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

// ── Single Sign-On (SAML 2.0) ────────────────────────────────
// Independent of the OrganizationSettings JSON blob the other tabs share
// — SSO config is its own model/table (models/sso_configuration.py) with
// its own API (ssoApi.js -> /api/admin/sso), so this tab loads/saves on
// its own rather than going through the `settings`/`onSave` props above.

function CopyableRow({ label, value }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(value).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
  };
  return (
    <div className="stg-sp-row">
      <span className="stg-sp-label">{label}</span>
      <span className="stg-sp-value">{value}</span>
      <button type="button" className="stg-sp-copy-btn" title="Copy" onClick={copy}>
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </div>
  );
}

function SsoTab() {
  const [cfg, setCfg] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  const load = useCallback(() => {
    setLoading(true); setLoadError('');
    ssoApi.get().then(setCfg).catch((err) => setLoadError(err?.message ?? 'Failed to load SSO configuration.')).finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);

  if (loading) return <div className="stg-card"><div className="stg-card-title">Single Sign-On</div><p className="stg-field-hint">Loading…</p></div>;
  if (loadError || !cfg) return <div className="stg-card"><div className="stg-form-error">{loadError || 'Could not load SSO configuration.'}</div></div>;

  return <SsoForm cfg={cfg} onSaved={setCfg} />;
}

function SsoForm({ cfg, onSaved }) {
  const initial = {
    enabled: cfg.enabled, enforce_sso: cfg.enforce_sso,
    idp_entity_id: cfg.idp_entity_id, idp_sso_url: cfg.idp_sso_url, idp_slo_url: cfg.idp_slo_url,
    email_attribute: cfg.email_attribute, name_attribute: cfg.name_attribute,
  };
  const { draft, set, dirty: fieldsDirty, reset: resetFields } = useSectionDraft(initial);
  const [certInput, setCertInput] = useState(''); // blank = leave stored cert unchanged
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const dirty = fieldsDirty || certInput.trim().length > 0;
  const reset = () => { resetFields(); setCertInput(''); };

  const handleSave = async () => {
    setSaving(true); setError('');
    try {
      const body = { ...draft };
      if (certInput.trim()) body.idp_x509_cert = certInput.trim();
      const updated = await ssoApi.update(body);
      onSaved(updated);
      setCertInput('');
    } catch (err) { setError(err?.message ?? 'Could not save SSO settings.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="stg-card">
      <div className="stg-card-title">Single Sign-On</div>
      <p className="stg-secret-note">
        Real SAML 2.0 — configure your Identity Provider (Okta, Azure AD, OneLogin, etc.) to trust this application
        using the Service Provider values below, then paste your IdP's details here.
      </p>

      <div className="stg-subsection-title" style={{ marginTop: 0, paddingTop: 0, borderTop: 'none' }}>Service Provider (give these to your IdP admin)</div>
      <div className="stg-sp-info">
        <CopyableRow label="Entity ID" value={cfg.sp_entity_id} />
        <CopyableRow label="ACS URL" value={cfg.sp_acs_url} />
        <div className="stg-sp-row">
          <span className="stg-sp-label">Metadata</span>
          <a className="stg-sp-value" href={cfg.sp_metadata_url} target="_blank" rel="noreferrer" style={{ display: 'flex', alignItems: 'center', gap: 6, textDecoration: 'none' }}>
            {cfg.sp_metadata_url} <ExternalLink size={11} />
          </a>
        </div>
      </div>

      <div className="stg-subsection-title">Identity Provider</div>
      <div className="stg-grid">
        <Field label="IdP Entity ID">
          <TextInput value={draft.idp_entity_id} onChange={(e) => set('idp_entity_id')(e.target.value)} placeholder="https://idp.example.com/metadata" />
        </Field>
        <Field label="IdP SSO URL" hint="Where the browser is redirected to sign in.">
          <TextInput value={draft.idp_sso_url} onChange={(e) => set('idp_sso_url')(e.target.value)} placeholder="https://idp.example.com/sso" />
        </Field>
        <Field label="IdP Logout URL (optional)">
          <TextInput value={draft.idp_slo_url} onChange={(e) => set('idp_slo_url')(e.target.value)} placeholder="https://idp.example.com/slo" />
        </Field>
      </div>

      <Field label="IdP X.509 Certificate" hint="PEM format (with or without the BEGIN/END lines) — used to verify every sign-in is really from your IdP.">
        <div className={`stg-cert-status ${cfg.cert_set ? 'set' : 'not-set'}`}>
          {cfg.cert_set ? <CheckCircle2 size={12} /> : null} {cfg.cert_set ? 'A certificate is currently configured.' : 'No certificate configured yet.'}
        </div>
        <textarea
          className="stg-textarea" rows={5} value={certInput} onChange={(e) => setCertInput(e.target.value)}
          placeholder={cfg.cert_set ? 'Leave blank to keep the current certificate, or paste a new one to replace it.' : '-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----'}
        />
      </Field>

      <div className="stg-subsection-title">Attribute Mapping</div>
      <div className="stg-grid">
        <Field label="Email Attribute" hint="SAML assertion attribute name that carries the user's email.">
          <TextInput value={draft.email_attribute} onChange={(e) => set('email_attribute')(e.target.value)} />
        </Field>
        <Field label="Name Attribute">
          <TextInput value={draft.name_attribute} onChange={(e) => set('name_attribute')(e.target.value)} />
        </Field>
      </div>

      <div className="stg-subsection-title">Access</div>
      <div className="stg-checkbox-list">
        <CheckboxRow
          label="Enable Single Sign-On" checked={draft.enabled} onChange={set('enabled')}
          description="Lets users at this organization sign in with your IdP from the login page."
        />
        <CheckboxRow
          label="Require SSO for non-admin users" checked={draft.enforce_sso} onChange={set('enforce_sso')}
          description="Blocks password sign-in for everyone except Org Admins, so a misconfigured or unreachable IdP can never lock every admin out."
        />
      </div>

      <SaveBar permission={SSO_PERMISSIONS.SSO_EDIT} dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
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
