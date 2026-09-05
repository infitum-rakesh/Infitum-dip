/**
 * SellerSettings — /app/seller/settings (Seller Portal Phase 9 — the
 * final section of the original 9-phase build).
 *
 * Company / Branding / Default License Settings all wrap the singleton
 * Seller row (GET/PUT /api/seller/settings — see backend/app/services/
 * seller/seller_settings_service.py). The Plans tab is NOT a new backend
 * — it finally surfaces planApi.create()/update() (subscriptionApi.js),
 * which Phase 3 built but no page ever called directly; Subscriptions.jsx
 * only *reads* the plan list for its change-plan dropdown.
 */
import { useCallback, useEffect, useState } from 'react';
import { Building2, Palette, KeyRound, Layers, Save, Lock, RefreshCcw, Plus, Pencil, Power, PowerOff, Mail, Send, CheckCircle2, XCircle, Server, Star, Briefcase, DollarSign } from 'lucide-react';
import { Button, StatusBadge, PermissionGate, FullPageSpinner, EmptyState } from '@/components/common/index.js';
import { SELLER_PERMISSIONS } from '@/utils/constants.js';
import { MODULE_META } from '@/utils/permissions.js';
import { formatDateTime } from '@/utils/formatters.js';
import { sellerSettingsApi } from '@/services/api/seller/sellerSettingsApi.js';
import { planApi } from '@/services/api/seller/subscriptionApi.js';
import { moduleApi } from '@/services/api/seller/moduleApi.js';
import { companyApi } from '@/services/api/seller/companyApi.js';
import { sellerTenantDatabaseApi } from '@/services/api/tenantDatabaseApi.js';
import { sellerTenantMigrationApi } from '@/services/api/tenantMigrationApi.js';
import { TenantDatabaseTab } from '@/components/settings/TenantDatabaseTab.jsx';
import { PlanModal } from './PlanModal.jsx';
import { CompanyModal } from './CompanyModal.jsx';
import './seller-settings.css';

const TABS = [
  { id: 'company', label: 'Company', icon: Building2 },
  { id: 'companies', label: 'Companies', icon: Briefcase },
  { id: 'branding', label: 'Branding', icon: Palette },
  { id: 'email', label: 'Email', icon: Mail },
  { id: 'database', label: 'Database', icon: Server },
  { id: 'plans', label: 'Plans', icon: Layers },
  { id: 'module-prices', label: 'Module Prices', icon: DollarSign },
  { id: 'defaults', label: 'Default License Settings', icon: KeyRound },
];

/** Shared dirty-state hook — same pattern as administration/settings/Settings.jsx's useSectionDraft. */
function useSectionDraft(initial) {
  const [draft, setDraft] = useState(initial);
  const key = JSON.stringify(initial);
  useEffect(() => { setDraft(initial); }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify(draft) !== key;
  const set = (k) => (value) => setDraft((d) => ({ ...d, [k]: value }));
  const reset = () => setDraft(initial);
  return { draft, set, dirty, reset };
}

function SaveBar({ dirty, saving, error, onSave, onReset }) {
  return (
    <div className="sst-save-bar">
      {error && <div className="sst-form-error">{error}</div>}
      <PermissionGate {...SELLER_PERMISSIONS.SELLER_PORTAL_ACCESS} fallback={<p className="sst-readonly-note"><Lock size={12} /> You don't have permission to edit this section.</p>}>
        <div className="sst-save-actions">
          <Button variant="secondary" size="sm" disabled={!dirty || saving} onClick={onReset}>Discard changes</Button>
          <Button variant="primary" size="sm" icon={<Save size={13} />} loading={saving} disabled={!dirty} onClick={onSave}>Save changes</Button>
        </div>
      </PermissionGate>
    </div>
  );
}

export default function SellerSettings() {
  const [settings, setSettings] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState('company');

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    sellerSettingsApi.get().then(setSettings).catch(() => setError('Failed to load Seller settings. Please try again.')).finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async (patch) => {
    const updated = await sellerSettingsApi.update(patch);
    setSettings(updated);
    return updated;
  };

  if (loading) return <FullPageSpinner />;
  if (error || !settings) {
    return (
      <div className="sst-page">
        <EmptyState icon={<Lock size={28} />} title="Settings unavailable" description={error ?? 'Could not load Seller settings.'}
          action={{ label: 'Retry', onClick: load }} />
      </div>
    );
  }

  return (
    <PermissionGate {...SELLER_PERMISSIONS.SELLER_PORTAL_ACCESS} showReason>
      <div className="sst-page">
        <div className="sst-header">
          <div>
            <h1 className="sst-title">Seller Settings</h1>
            <p className="sst-subtitle">Company profile, branding, the plan catalog and default license behavior for {settings.name}.</p>
          </div>
          <div className="sst-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={load}>Refresh</Button>
          </div>
        </div>

        <div className="sst-meta-line">Last updated {formatDateTime(settings.updated_at)}</div>

        <div className="sst-tab-nav">
          {TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button key={t.id} className={`sst-tab-btn ${tab === t.id ? 'active' : ''}`} onClick={() => setTab(t.id)}>
                <Icon size={14} /> {t.label}
              </button>
            );
          })}
        </div>

        {tab === 'company' && <CompanyTab settings={settings} onSave={save} />}
        {tab === 'companies' && <CompaniesTab />}
        {tab === 'branding' && <BrandingTab settings={settings} onSave={save} />}
        {tab === 'email' && <EmailTab settings={settings} onReload={load} />}
        {tab === 'database' && (
          <TenantDatabaseTab
            api={sellerTenantDatabaseApi}
            migrationApi={sellerTenantMigrationApi}
            isPlatform
            editPermission={SELLER_PERMISSIONS.SELLER_PORTAL_ACCESS}
          />
        )}
        {tab === 'plans' && <PlansTab />}
        {tab === 'module-prices' && <ModulePricesTab />}
        {tab === 'defaults' && <DefaultsTab settings={settings} onSave={save} />}
      </div>
    </PermissionGate>
  );
}

// ── Company ───────────────────────────────────────────────────

function CompanyTab({ settings, onSave }) {
  const initial = {
    name: settings.name, legal_name: settings.legal_name ?? '', support_email: settings.support_email ?? '',
    billing_email: settings.billing_email ?? '', website: settings.website ?? '',
    sales_contact_name: settings.sales_contact_name ?? '', sales_email: settings.sales_email ?? '',
    sales_phone: settings.sales_phone ?? '', sales_url: settings.sales_url ?? '',
  };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const handleSave = async () => {
    setSaving(true);
    setError('');
    try {
      await onSave({
        name: draft.name, legal_name: draft.legal_name || null, support_email: draft.support_email || null,
        billing_email: draft.billing_email || null, website: draft.website || null,
        sales_contact_name: draft.sales_contact_name || null, sales_email: draft.sales_email || null,
        sales_phone: draft.sales_phone || null, sales_url: draft.sales_url || null,
      });
    } catch (err) { setError(err?.message ?? 'Could not save.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="sst-card">
      <div className="sst-card-title">Company Profile</div>
      <p className="sst-card-hint">Shown across the Seller Portal and, once branding is wired to customer-facing surfaces, to customers.</p>
      <div className="sst-grid">
        <div className="sst-field">
          <label className="sst-field-label">Company Name</label>
          <input className="sst-input" value={draft.name} onChange={(e) => set('name')(e.target.value)} />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Legal Name</label>
          <input className="sst-input" value={draft.legal_name} onChange={(e) => set('legal_name')(e.target.value)} placeholder="e.g. Acme Platform, Inc." />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Support Email</label>
          <input type="email" className="sst-input" value={draft.support_email} onChange={(e) => set('support_email')(e.target.value)} placeholder="support@yourcompany.com" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Billing Email</label>
          <input type="email" className="sst-input" value={draft.billing_email} onChange={(e) => set('billing_email')(e.target.value)} placeholder="billing@yourcompany.com" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Website</label>
          <input className="sst-input" value={draft.website} onChange={(e) => set('website')(e.target.value)} placeholder="https://yourcompany.com" />
        </div>
      </div>

      <div className="sst-card-title" style={{ marginTop: 24 }}>Sales Contact</div>
      <p className="sst-card-hint">Shown to customers on expired-subscription messaging and the all-expired block screen (SB30) — the real "Contact Sales" destination, not a hardcoded one.</p>
      <div className="sst-grid">
        <div className="sst-field">
          <label className="sst-field-label">Contact Name</label>
          <input className="sst-input" value={draft.sales_contact_name} onChange={(e) => set('sales_contact_name')(e.target.value)} placeholder="e.g. Sales Team" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Sales Email</label>
          <input type="email" className="sst-input" value={draft.sales_email} onChange={(e) => set('sales_email')(e.target.value)} placeholder="sales@yourcompany.com" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Sales Phone</label>
          <input className="sst-input" value={draft.sales_phone} onChange={(e) => set('sales_phone')(e.target.value)} placeholder="+1 (555) 123-4567" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Sales Page URL</label>
          <input className="sst-input" value={draft.sales_url} onChange={(e) => set('sales_url')(e.target.value)} placeholder="https://yourcompany.com/contact-sales" />
        </div>
      </div>
      <SaveBar dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}

// ── Companies ─────────────────────────────────────────────────
// A Seller can register more than one Company (billing entity) — each
// customer Organization is assigned to exactly one (Customers ->
// Create/Edit -> Company field), which decides whose logo/address/
// signature brand that org's invoices (services/billing/invoice_pdf.py).

function CompaniesTab() {
  const [companies, setCompanies] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editTarget, setEditTarget] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    companyApi.list().then(setCompanies).catch(() => setError('Failed to load companies.')).finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);

  const openCreate = () => { setEditTarget(null); setModalOpen(true); };
  const openEdit = (company) => { setEditTarget(company); setModalOpen(true); };

  const toggleActive = async (company) => {
    try {
      if (company.is_active) await companyApi.deactivate(company.id);
      else await companyApi.activate(company.id);
      load();
    } catch (err) { window.alert(err?.message ?? 'Could not update the company.'); }
  };

  const makeDefault = async (company) => {
    try { await companyApi.setDefault(company.id); load(); }
    catch (err) { window.alert(err?.message ?? 'Could not set the default company.'); }
  };

  if (loading) return <div className="sst-card"><p style={{ color: 'var(--text-muted)' }}>Loading companies…</p></div>;

  return (
    <div className="sst-card">
      <div className="sst-companies-header">
        <div>
          <div className="sst-card-title">Companies</div>
          <p className="sst-card-hint" style={{ marginBottom: 0 }}>
            Legal entities/brands you issue invoices under. Assign one to each customer in Customers -&gt; Create/Edit
            — its logo, address and signature brand that customer's invoice PDFs.
          </p>
        </div>
        <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={openCreate}>New Company</Button>
      </div>

      {error && <div className="sst-form-error">{error}</div>}

      {companies.length === 0 ? (
        <EmptyState icon={<Briefcase size={26} />} title="No companies yet" description="Register your first billing entity to brand customer invoices." action={{ label: 'New Company', onClick: openCreate }} />
      ) : (
        <div className="sst-table-wrap">
          <table className="sst-table">
            <thead>
              <tr><th>Company</th><th>Contact</th><th>Location</th><th>Status</th><th>Actions</th></tr>
            </thead>
            <tbody>
              {companies.map((c) => (
                <tr key={c.id}>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                      {c.logo
                        ? <img src={c.logo} alt="" className="sst-company-logo-cell" />
                        : <div className="sst-company-logo-placeholder">No logo</div>}
                      <div>
                        <div style={{ fontWeight: 600 }}>{c.name}{c.is_default && <span className="sst-default-badge">Default</span>}</div>
                        {c.legal_name && <div style={{ fontSize: 11, color: 'var(--text-faint)' }}>{c.legal_name}</div>}
                      </div>
                    </div>
                  </td>
                  <td style={{ fontSize: 12 }}>{c.email || '—'}{c.phone ? ` · ${c.phone}` : ''}</td>
                  <td style={{ fontSize: 12 }}>{[c.city, c.country].filter(Boolean).join(', ') || '—'}</td>
                  <td><StatusBadge variant={c.is_active ? 'success' : 'gray'} dot>{c.is_active ? 'Active' : 'Inactive'}</StatusBadge></td>
                  <td>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <Button variant="ghost" size="sm" icon={<Pencil size={13} />} onClick={() => openEdit(c)}>Edit</Button>
                      {!c.is_default && (
                        <Button variant="ghost" size="sm" icon={<Star size={13} />} onClick={() => makeDefault(c)}>Set Default</Button>
                      )}
                      <Button variant="ghost" size="sm" icon={c.is_active ? <PowerOff size={13} /> : <Power size={13} />} onClick={() => toggleActive(c)}>
                        {c.is_active ? 'Deactivate' : 'Activate'}
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <CompanyModal open={modalOpen} company={editTarget} onClose={() => setModalOpen(false)} onSaved={() => { setModalOpen(false); load(); }} />
    </div>
  );
}

// ── Branding ──────────────────────────────────────────────────

function BrandingTab({ settings, onSave }) {
  const initial = {
    logo_url: settings.branding?.logo_url ?? '', primary_color: settings.branding?.primary_color ?? '#4F46E5',
    digital_signature: settings.digital_signature ?? null,
  };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [sigBusy, setSigBusy] = useState(false);
  const [sigError, setSigError] = useState('');

  const handleSignatureFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    if (file.size > 800 * 1024) { setSigError('Image is too large (max 800KB).'); return; }
    setSigBusy(true);
    setSigError('');
    try {
      const dataUri = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error('Could not read the file.'));
        reader.readAsDataURL(file);
      });
      set('digital_signature')(dataUri);
    } catch (err) { setSigError(err.message); }
    finally { setSigBusy(false); }
  };

  const handleSave = async () => {
    setSaving(true);
    setError('');
    try {
      await onSave({
        branding: { logo_url: draft.logo_url || null, primary_color: draft.primary_color || null },
        digital_signature: draft.digital_signature,
      });
    } catch (err) { setError(err?.message ?? 'Could not save.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="sst-card">
      <div className="sst-card-title">Branding</div>
      <p className="sst-card-hint">Logo, primary color and digital signature, stored on the Seller record (SELLER PORTAL spec section 9). The signature is stamped on every invoice PDF alongside the issuing Company's own (Companies tab).</p>

      <div className="sst-brand-preview">
        {draft.logo_url
          ? <img src={draft.logo_url} alt="Logo preview" className="sst-brand-logo" onError={(e) => { e.target.style.display = 'none'; }} />
          : <div className="sst-brand-logo-placeholder">No logo</div>}
        <div className="sst-color-swatch" style={{ background: draft.primary_color || '#e2e8f0' }} />
        <div>
          <div style={{ fontWeight: 700, fontSize: 13 }}>{settings.name}</div>
          <div style={{ fontSize: 11, color: 'var(--text-faint)' }}>Live preview — updates as you type</div>
        </div>
      </div>

      <div className="sst-grid">
        <div className="sst-field">
          <label className="sst-field-label">Logo URL</label>
          <input className="sst-input" value={draft.logo_url} onChange={(e) => set('logo_url')(e.target.value)} placeholder="https://yourcompany.com/logo.png" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Primary Color</label>
          <div className="sst-color-input-row">
            <input type="color" value={/^#[0-9A-Fa-f]{6}$/.test(draft.primary_color) ? draft.primary_color : '#4F46E5'} onChange={(e) => set('primary_color')(e.target.value)} />
            <input className="sst-input" value={draft.primary_color} onChange={(e) => set('primary_color')(e.target.value)} placeholder="#4F46E5" />
          </div>
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Digital Signature</label>
          {draft.digital_signature ? (
            <div className="sst-image-upload-preview">
              <img src={draft.digital_signature} alt="Signature preview" />
              <Button variant="ghost" size="sm" onClick={() => set('digital_signature')(null)}>Remove</Button>
            </div>
          ) : (
            <input type="file" accept="image/*" className="sst-input" onChange={handleSignatureFile} disabled={sigBusy} />
          )}
          <p className="sst-field-hint">Shown on every invoice's signature block. PNG/JPG, max 800KB.</p>
          {sigError && <p className="sst-field-hint" style={{ color: 'var(--danger, #DC2626)' }}>{sigError}</p>}
        </div>
      </div>
      <SaveBar dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}

// ── Defaults ──────────────────────────────────────────────────

function DefaultsTab({ settings, onSave }) {
  const initial = { default_trial_days: settings.default_trial_days };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const handleSave = async () => {
    setSaving(true);
    setError('');
    try { await onSave({ default_trial_days: Number(draft.default_trial_days) }); }
    catch (err) { setError(err?.message ?? 'Could not save.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="sst-card">
      <div className="sst-card-title">Default License Settings</div>
      <p className="sst-card-hint">
        The one real, backend-stored default this platform has today: how long a new customer's trial runs before
        it needs a real Subscription. Per-plan module lists and usage limits are configured on the Plans tab instead
        of duplicated here as separate "defaults."
      </p>
      <div className="sst-grid">
        <div className="sst-field">
          <label className="sst-field-label">Default Trial Length (days)</label>
          <input type="number" min={1} className="sst-input" value={draft.default_trial_days} onChange={(e) => set('default_trial_days')(e.target.value)} />
          <p className="sst-field-hint">Used when seeding a new customer's trial subscription.</p>
        </div>
      </div>
      <SaveBar dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />
    </div>
  );
}

// ── Email (SMTP) ──────────────────────────────────────────────

function EmailTab({ settings, onReload }) {
  const smtp = settings.smtp || {};
  const initial = {
    host: smtp.host ?? '', port: smtp.port ?? 587, username: smtp.username ?? '',
    password: '', from_address: smtp.from_address ?? '', use_tls: smtp.use_tls ?? true,
  };
  const { draft, set, dirty, reset } = useSectionDraft(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [testTo, setTestTo] = useState(smtp.username ?? '');
  const [testResult, setTestResult] = useState(null);
  const [testing, setTesting] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    setError('');
    try {
      const body = {
        host: draft.host || null, port: draft.port ? Number(draft.port) : null,
        username: draft.username || null, from_address: draft.from_address || null,
        use_tls: draft.use_tls,
      };
      if (draft.password) body.password = draft.password;
      const updated = await sellerSettingsApi.updateSmtp(body);
      onReload();
      return updated;
    } catch (err) { setError(err?.message ?? 'Could not save.'); }
    finally { setSaving(false); }
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await sellerSettingsApi.sendTestEmail(testTo);
      setTestResult(result);
    } catch (err) {
      setTestResult({ delivered: false, detail: err?.message ?? 'Test send failed.' });
    } finally { setTesting(false); }
  };

  return (
    <div className="sst-card">
      <div className="sst-card-title">Email (SMTP)</div>
      <p className="sst-card-hint">
        Used to email a new customer's Admin their login credentials when you create their organization
        (see the Customers page), and to resend them later. Nothing is sent until this is configured.
      </p>
      <div className="sst-grid">
        <div className="sst-field">
          <label className="sst-field-label">SMTP Host</label>
          <input className="sst-input" value={draft.host} onChange={(e) => set('host')(e.target.value)} placeholder="smtp.yourprovider.com" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Port</label>
          <input type="number" className="sst-input" value={draft.port} onChange={(e) => set('port')(e.target.value)} placeholder="587" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Username</label>
          <input className="sst-input" value={draft.username} onChange={(e) => set('username')(e.target.value)} placeholder="apikey or account username" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Password {smtp.password_set && <span style={{ fontWeight: 400, color: 'var(--text-faint)' }}>(already set — leave blank to keep)</span>}</label>
          <input type="password" className="sst-input" value={draft.password} onChange={(e) => set('password')(e.target.value)} placeholder={smtp.password_set ? '••••••••' : ''} />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">From Address</label>
          <input type="email" className="sst-input" value={draft.from_address} onChange={(e) => set('from_address')(e.target.value)} placeholder="no-reply@yourcompany.com" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Use TLS</label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
            <input type="checkbox" checked={draft.use_tls} onChange={(e) => set('use_tls')(e.target.checked)} /> STARTTLS
          </label>
        </div>
      </div>
      <SaveBar dirty={dirty} saving={saving} error={error} onSave={handleSave} onReset={reset} />

      <div style={{ marginTop: 20, paddingTop: 16, borderTop: '1px solid var(--border-color, #E2E8F0)' }}>
        <div className="sst-card-title" style={{ fontSize: 13 }}>Send a test email</div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
          <input type="email" className="sst-input" style={{ maxWidth: 280 }} value={testTo} onChange={(e) => setTestTo(e.target.value)} placeholder="you@example.com" />
          <Button variant="secondary" size="sm" icon={<Send size={13} />} loading={testing} disabled={!testTo} onClick={handleTest}>Send Test</Button>
        </div>
        {testResult && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8, fontSize: 12, color: testResult.delivered ? 'var(--success, #10B981)' : 'var(--danger, #DC2626)' }}>
            {testResult.delivered ? <CheckCircle2 size={13} /> : <XCircle size={13} />} {testResult.detail}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Plans ─────────────────────────────────────────────────────

function PlansTab() {
  const [plans, setPlans] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editTarget, setEditTarget] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    planApi.list().then(setPlans).catch(() => setError('Failed to load plans.')).finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);

  const toggleActive = async (plan) => {
    try { await planApi.update(plan.id, { is_active: !plan.is_active }); load(); }
    catch (err) { window.alert(err?.message ?? 'Could not update the plan.'); }
  };

  const openCreate = () => { setEditTarget(null); setModalOpen(true); };
  const openEdit = (plan) => { setEditTarget(plan); setModalOpen(true); };

  if (loading) return <div className="sst-card"><p style={{ color: 'var(--text-muted)' }}>Loading plans…</p></div>;

  return (
    <div className="sst-card">
      <div className="sst-plans-header">
        <div>
          <div className="sst-card-title">Plan Catalog</div>
          <p className="sst-card-hint" style={{ marginBottom: 0 }}>Real, configurable plans — never hardcoded. Used across Subscriptions and Usage limits.</p>
        </div>
        <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={openCreate}>New Plan</Button>
      </div>

      {error && <div className="sst-form-error">{error}</div>}

      {plans.length === 0 ? (
        <EmptyState icon={<Layers size={26} />} title="No plans yet" description="Create your first plan to get started." action={{ label: 'New Plan', onClick: openCreate }} />
      ) : (
        <div className="sst-table-wrap">
          <table className="sst-table">
            <thead>
              <tr><th>Plan</th><th>Modules</th><th>Limits</th><th>Price</th><th>Trial</th><th>Status</th><th>Actions</th></tr>
            </thead>
            <tbody>
              {plans.map((p) => (
                <tr key={p.id}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{p.name}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-faint)', fontFamily: 'var(--font-mono)' }}>
                      {p.code} {p.is_system && <span className="sst-system-badge">· SYSTEM</span>}
                    </div>
                  </td>
                  <td>
                    <div className="sst-module-chips">
                      {p.modules.slice(0, 4).map((m) => <span key={m} className="sst-module-chip">{MODULE_META[m]?.label ?? m}</span>)}
                      {p.modules.length > 4 && <span className="sst-module-chip">+{p.modules.length - 4} more</span>}
                    </div>
                  </td>
                  <td style={{ fontSize: 12 }}>
                    {p.limits.users === -1 ? '∞' : p.limits.users} users · {p.limits.documents === -1 ? '∞' : p.limits.documents} docs
                  </td>
                  <td>${p.price.toFixed(0)}/{p.billing_cycle.toLowerCase()}</td>
                  <td>{p.trial_days ? `${p.trial_days}d` : '—'}</td>
                  <td><StatusBadge variant={p.is_active ? 'success' : 'gray'} dot>{p.is_active ? 'Active' : 'Inactive'}</StatusBadge></td>
                  <td>
                    <div style={{ display: 'flex', gap: 4 }}>
                      <button className="sst-icon-btn" title="Edit" onClick={() => openEdit(p)}><Pencil size={13} /></button>
                      <button className="sst-icon-btn" title={p.is_active ? 'Deactivate' : 'Activate'} onClick={() => toggleActive(p)}>
                        {p.is_active ? <PowerOff size={13} /> : <Power size={13} />}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <PlanModal open={modalOpen} plan={editTarget} onClose={() => setModalOpen(false)} onSaved={() => { setModalOpen(false); load(); }} />
    </div>
  );
}

// ── Module Prices ─────────────────────────────────────────────
// What ad-hoc Purchased-Modules grants and "Custom" plan subscriptions
// are billed at (services/seller/module_service.py's price_for_modules()).

function ModulePricesTab() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [editingModule, setEditingModule] = useState(null);
  const [editValue, setEditValue] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    moduleApi.getModulePrices().then(setItems).catch(() => setError('Failed to load module prices.')).finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);

  const startEdit = (item) => {
    setEditingModule(item.module);
    setEditValue(String(item.price));
    setSaveError('');
  };
  const cancelEdit = () => { setEditingModule(null); setSaveError(''); };

  const saveEdit = async (item) => {
    const price = Number(editValue);
    if (Number.isNaN(price) || price < 0) { setSaveError('Enter a valid, non-negative price.'); return; }
    setSaving(true);
    setSaveError('');
    try {
      await moduleApi.setModulePrice(item.module, { price, currency: item.currency });
      setEditingModule(null);
      load();
    } catch (err) { setSaveError(err?.message ?? 'Could not save the price.'); }
    finally { setSaving(false); }
  };

  if (loading) return <div className="sst-card"><p style={{ color: 'var(--text-muted)' }}>Loading module prices…</p></div>;

  return (
    <div className="sst-card">
      <div className="sst-card-title">Module Prices</div>
      <p className="sst-card-hint">
        What each module is billed at when purchased individually — enabling a module ad hoc from Purchased Modules,
        or picking modules under the "Custom" plan option when creating a customer or subscription. Trial-mode
        customers still get modules for free regardless of these prices.
      </p>

      {error && <div className="sst-form-error">{error}</div>}
      {saveError && <div className="sst-form-error">{saveError}</div>}

      <div className="sst-table-wrap">
        <table className="sst-table">
          <thead>
            <tr><th>Module</th><th>Category</th><th>Price</th><th>Actions</th></tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.module}>
                <td style={{ fontWeight: 600 }}>{item.label}</td>
                <td style={{ fontSize: 12, color: 'var(--text-faint)' }}>{item.category}</td>
                <td>
                  {editingModule === item.module ? (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ fontSize: 12 }}>{item.currency}</span>
                      <input
                        type="number" min={0} step="0.01" className="sst-input" style={{ maxWidth: 110 }}
                        value={editValue} onChange={(e) => setEditValue(e.target.value)} autoFocus
                        onKeyDown={(e) => { if (e.key === 'Enter') saveEdit(item); if (e.key === 'Escape') cancelEdit(); }}
                      />
                    </div>
                  ) : (
                    <span>{item.currency} {item.price.toFixed(2)}<span style={{ color: 'var(--text-faint)' }}>/mo</span></span>
                  )}
                </td>
                <td>
                  {editingModule === item.module ? (
                    <div style={{ display: 'flex', gap: 6 }}>
                      <Button variant="primary" size="sm" loading={saving} onClick={() => saveEdit(item)}>Save</Button>
                      <Button variant="secondary" size="sm" disabled={saving} onClick={cancelEdit}>Cancel</Button>
                    </div>
                  ) : (
                    <PermissionGate {...SELLER_PERMISSIONS.SELLER_PORTAL_ACCESS} fallback={null}>
                      <button className="sst-icon-btn" title="Edit price" onClick={() => startEdit(item)}><Pencil size={13} /></button>
                    </PermissionGate>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
