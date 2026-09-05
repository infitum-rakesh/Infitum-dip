/**
 * CompanyModal — Create/Edit a Company (billing entity) on the Companies
 * tab. `company` is the CompanyDetailOut row to edit (fetched fresh via
 * companyApi.get() so the full digital_signature comes along — the list
 * response deliberately omits it), or null to create a new one.
 */
import { useEffect, useState } from 'react';
import { Modal, Button } from '@/components/common/index.js';
import { companyApi } from '@/services/api/seller/companyApi.js';
import './seller-settings.css';

const EMPTY = {
  name: '', legal_name: '', email: '', phone: '', website: '',
  address_line: '', city: '', state: '', postal_code: '', country: '',
  logo: null, digital_signature: null, is_default: false,
};

const MAX_IMAGE_BYTES = 800 * 1024; // 800KB — plenty for a logo/signature, keeps the invoice PDF/DB row small

function readImageAsBase64(file) {
  return new Promise((resolve, reject) => {
    if (file.size > MAX_IMAGE_BYTES) {
      reject(new Error(`${file.name} is too large (max 800KB) — please use a smaller image.`));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error(`Could not read ${file.name}.`));
    reader.readAsDataURL(file);
  });
}

function ImageUploadField({ label, value, onChange, hint }) {
  const [busy, setBusy] = useState(false);
  const [uploadError, setUploadError] = useState('');

  const handleFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ''; // allow re-selecting the same file after Clear
    if (!file) return;
    setBusy(true);
    setUploadError('');
    try {
      onChange(await readImageAsBase64(file));
    } catch (err) {
      setUploadError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sst-field">
      <label className="sst-field-label">{label}</label>
      {value ? (
        <div className="sst-image-upload-preview">
          <img src={value} alt={label} />
          <Button variant="ghost" size="sm" onClick={() => onChange(null)}>Remove</Button>
        </div>
      ) : (
        <input type="file" accept="image/*" className="sst-input" onChange={handleFile} disabled={busy} />
      )}
      {hint && <p className="sst-field-hint">{hint}</p>}
      {uploadError && <p className="sst-field-hint" style={{ color: 'var(--danger, #DC2626)' }}>{uploadError}</p>}
    </div>
  );
}

export function CompanyModal({ open, company, onClose, onSaved }) {
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    setError('');
    if (company) {
      // The list row (CompanyOut) omits the full signature — fetch the
      // detail shape so editing never silently drops an already-uploaded
      // signature the list call never sent down in the first place.
      companyApi.get(company.id).then((detail) => setForm({
        name: detail.name, legal_name: detail.legal_name ?? '', email: detail.email ?? '',
        phone: detail.phone ?? '', website: detail.website ?? '',
        address_line: detail.address_line ?? '', city: detail.city ?? '', state: detail.state ?? '',
        postal_code: detail.postal_code ?? '', country: detail.country ?? '',
        logo: detail.logo ?? null, digital_signature: detail.digital_signature ?? null,
        is_default: detail.is_default,
      }));
    } else {
      setForm(EMPTY);
    }
  }, [open, company]);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleSubmit = async () => {
    if (!form.name.trim()) { setError('Company name is required.'); return; }
    setSaving(true);
    setError('');
    try {
      const body = {
        name: form.name, legal_name: form.legal_name || null, email: form.email || null,
        phone: form.phone || null, website: form.website || null,
        address_line: form.address_line || null, city: form.city || null, state: form.state || null,
        postal_code: form.postal_code || null, country: form.country || null,
        logo: form.logo, digital_signature: form.digital_signature,
      };
      if (company) {
        await companyApi.update(company.id, body);
      } else {
        await companyApi.create({ ...body, is_default: form.is_default });
      }
      onSaved?.();
    } catch (err) {
      setError(err?.message ?? 'Could not save the company.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={company ? `Edit Company — ${company.name}` : 'New Company'}
      size="lg"
      footer={<>
        <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" onClick={handleSubmit} loading={saving}>{company ? 'Save Changes' : 'Create Company'}</Button>
      </>}
    >
      {error && <div className="sst-form-error">{error}</div>}

      <div className="sst-grid">
        <div className="sst-field">
          <label className="sst-field-label">Company Name</label>
          <input className="sst-input" value={form.name} onChange={set('name')} placeholder="e.g. Acme EMEA Ltd." autoFocus />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Legal Name</label>
          <input className="sst-input" value={form.legal_name} onChange={set('legal_name')} placeholder="e.g. Acme EMEA Limited" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Email</label>
          <input type="email" className="sst-input" value={form.email} onChange={set('email')} placeholder="billing@acme-emea.com" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Phone</label>
          <input className="sst-input" value={form.phone} onChange={set('phone')} placeholder="+44 20 7946 0958" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Website</label>
          <input className="sst-input" value={form.website} onChange={set('website')} placeholder="https://acme-emea.com" />
        </div>
      </div>

      <div className="sst-card-title" style={{ marginTop: 20 }}>Billing Address</div>
      <div className="sst-grid">
        <div className="sst-field" style={{ gridColumn: 'span 2' }}>
          <label className="sst-field-label">Address</label>
          <input className="sst-input" value={form.address_line} onChange={set('address_line')} placeholder="1 Fleet Place" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">City</label>
          <input className="sst-input" value={form.city} onChange={set('city')} placeholder="London" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">State / Region</label>
          <input className="sst-input" value={form.state} onChange={set('state')} placeholder="Greater London" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Postal Code</label>
          <input className="sst-input" value={form.postal_code} onChange={set('postal_code')} placeholder="EC4M 7RF" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Country</label>
          <input className="sst-input" value={form.country} onChange={set('country')} placeholder="United Kingdom" />
        </div>
      </div>

      <div className="sst-card-title" style={{ marginTop: 20 }}>Branding & Signature</div>
      <div className="sst-grid">
        <ImageUploadField
          label="Company Logo" value={form.logo} onChange={(v) => setForm((f) => ({ ...f, logo: v }))}
          hint="Shown on this company's invoice PDFs. PNG/JPG, max 800KB."
        />
        <ImageUploadField
          label="Digital Signature" value={form.digital_signature} onChange={(v) => setForm((f) => ({ ...f, digital_signature: v }))}
          hint="Stamped in the signature block of every invoice this company issues."
        />
      </div>

      {!company && (
        <label className="sst-checkbox-row" style={{ marginTop: 12 }}>
          <input type="checkbox" checked={form.is_default} onChange={(e) => setForm((f) => ({ ...f, is_default: e.target.checked }))} />
          Set as the default Company (used when a customer has none assigned)
        </label>
      )}
    </Modal>
  );
}
