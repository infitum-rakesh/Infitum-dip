/**
 * LicenseModals — Create / Extend / Change Type, bundled together (same
 * pattern as Subscriptions' SubscriptionModals.jsx). All hit real
 * endpoints in backend/app/services/seller/license_service.py.
 */
import { useEffect, useState } from 'react';
import { Modal, Button } from '@/components/common/index.js';
import { customerApi } from '@/services/api/seller/customerApi.js';
import { moduleApi } from '@/services/api/seller/moduleApi.js';
import { licenseApi } from '@/services/api/seller/licenseApi.js';
import './licenses.css';

const LICENSE_TYPES = ['TRIAL', 'SUBSCRIPTION', 'PER_USER', 'PER_DOCUMENT', 'PER_RECORD', 'USAGE_BASED', 'UNLIMITED', 'CUSTOM'];

const EMPTY_CREATE = {
  organization_id: '', module: '', license_type: 'SUBSCRIPTION',
  start_date: '', expiry_date: '', max_users: '', record_limit: '', document_limit: '', storage_limit_gb: '', ai_credit_limit: '',
};

export function CreateLicenseModal({ open, onClose, onCreated }) {
  const [customers, setCustomers] = useState([]);
  const [catalog, setCatalog] = useState([]);
  const [form, setForm] = useState(EMPTY_CREATE);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    customerApi.list({ page_size: 200 }).then((r) => setCustomers(r.items)).catch(() => {});
    moduleApi.getCatalog().then(setCatalog).catch(() => {});
  }, [open]);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const handleClose = () => { setForm(EMPTY_CREATE); setError(''); onClose(); };

  const handleSubmit = async () => {
    if (!form.organization_id || !form.module) { setError('Customer and Module are required.'); return; }
    setSaving(true);
    setError('');
    try {
      await licenseApi.create({
        organization_id: Number(form.organization_id), module: form.module, license_type: form.license_type,
        start_date: form.start_date ? new Date(form.start_date).toISOString() : null,
        expiry_date: form.expiry_date ? new Date(form.expiry_date).toISOString() : null,
        max_users: form.max_users === '' ? null : Number(form.max_users),
        record_limit: form.record_limit === '' ? null : Number(form.record_limit),
        document_limit: form.document_limit === '' ? null : Number(form.document_limit),
        storage_limit_gb: form.storage_limit_gb === '' ? null : Number(form.storage_limit_gb),
        ai_credit_limit: form.ai_credit_limit === '' ? null : Number(form.ai_credit_limit),
      });
      onCreated?.();
      handleClose();
    } catch (err) {
      setError(err?.message ?? 'Could not create license.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onClose={handleClose} title="New License" size="lg"
      footer={<>
        <Button variant="secondary" onClick={handleClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" onClick={handleSubmit} loading={saving}>Create License</Button>
      </>}>
      {error && <div className="lm-form-error">{error}</div>}
      <div className="lm-field-grid">
        <div className="lm-field">
          <label className="lm-field-label">Customer <span style={{ color: 'var(--color-danger)' }}>*</span></label>
          <select className="lm-input" value={form.organization_id} onChange={set('organization_id')}>
            <option value="">Select customer…</option>
            {customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </div>
        <div className="lm-field">
          <label className="lm-field-label">Module <span style={{ color: 'var(--color-danger)' }}>*</span></label>
          <select className="lm-input" value={form.module} onChange={set('module')}>
            <option value="">Select module…</option>
            {catalog.map((m) => <option key={m.module} value={m.module}>{m.label}</option>)}
          </select>
        </div>
        <div className="lm-field">
          <label className="lm-field-label">License Type</label>
          <select className="lm-input" value={form.license_type} onChange={set('license_type')}>
            {LICENSE_TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, ' ')}</option>)}
          </select>
        </div>
        <div className="lm-field">
          <label className="lm-field-label">User Limit</label>
          <input type="number" className="lm-input" value={form.max_users} onChange={set('max_users')} placeholder="Unlimited" />
        </div>
        <div className="lm-field">
          <label className="lm-field-label">Start Date</label>
          <input type="date" className="lm-input" value={form.start_date} onChange={set('start_date')} />
        </div>
        <div className="lm-field">
          <label className="lm-field-label">Expiry Date</label>
          <input type="date" className="lm-input" value={form.expiry_date} onChange={set('expiry_date')} />
        </div>
        <div className="lm-field">
          <label className="lm-field-label">Record Limit</label>
          <input type="number" className="lm-input" value={form.record_limit} onChange={set('record_limit')} placeholder="Unlimited" />
        </div>
        <div className="lm-field">
          <label className="lm-field-label">Document Limit</label>
          <input type="number" className="lm-input" value={form.document_limit} onChange={set('document_limit')} placeholder="Unlimited" />
        </div>
        <div className="lm-field">
          <label className="lm-field-label">Storage Limit (GB)</label>
          <input type="number" className="lm-input" value={form.storage_limit_gb} onChange={set('storage_limit_gb')} placeholder="Unlimited" />
        </div>
        <div className="lm-field">
          <label className="lm-field-label">AI Credit Limit</label>
          <input type="number" className="lm-input" value={form.ai_credit_limit} onChange={set('ai_credit_limit')} placeholder="Unlimited" />
        </div>
      </div>
    </Modal>
  );
}

export function ExtendLicenseModal({ license, onClose, onSaved }) {
  const [days, setDays] = useState(30);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  if (!license) return null;

  const handleSubmit = async () => {
    setSaving(true);
    setError('');
    try {
      await licenseApi.extend(license.id, Number(days));
      onSaved?.();
    } catch (err) {
      setError(err?.message ?? 'Could not extend this license.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open onClose={onClose} title={`Extend ${license.module_label}`} size="sm"
      footer={<>
        <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" onClick={handleSubmit} loading={saving}>Extend</Button>
      </>}>
      {error && <div className="lm-form-error">{error}</div>}
      <div className="lm-field">
        <label className="lm-field-label">Extend by (days)</label>
        <input type="number" className="lm-input" value={days} onChange={(e) => setDays(e.target.value)} />
      </div>
    </Modal>
  );
}

export function ChangeLicenseTypeModal({ license, mode, onClose, onSaved }) {
  const [licenseType, setLicenseType] = useState(license?.license_type ?? 'SUBSCRIPTION');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => { if (license) setLicenseType(license.license_type); }, [license]);

  if (!license) return null;
  const title = mode === 'upgrade' ? 'Upgrade License Type' : mode === 'downgrade' ? 'Downgrade License Type' : 'Change License Type';

  const handleSubmit = async () => {
    setSaving(true);
    setError('');
    try {
      await licenseApi.changeType(license.id, licenseType);
      onSaved?.();
    } catch (err) {
      setError(err?.message ?? 'Could not change license type.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open onClose={onClose} title={title} size="sm"
      footer={<>
        <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" onClick={handleSubmit} loading={saving}>Save</Button>
      </>}>
      {error && <div className="lm-form-error">{error}</div>}
      <div className="lm-field">
        <label className="lm-field-label">License Type</label>
        <select className="lm-input" value={licenseType} onChange={(e) => setLicenseType(e.target.value)}>
          {LICENSE_TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, ' ')}</option>)}
        </select>
      </div>
    </Modal>
  );
}
