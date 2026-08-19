/**
 * CreateIncidentModal — "+ Create Incident" on the Incidents list page.
 * POSTs straight to /api/incidents (see incidentApi.create).
 */
import { useState } from 'react';
import { Modal, Button } from '@/components/common/index.js';
import { incidentApi } from '@/services/api/incidentApi.js';
import './incidents.css';

const SOURCES = [
  { value: 'PIPELINE', label: 'Pipeline' },
  { value: 'CONNECTION', label: 'Connection' },
  { value: 'OCR', label: 'OCR' },
  { value: 'AI_EXTRACTION', label: 'AI Extraction' },
  { value: 'TRANSFORMATION', label: 'Transformation' },
  { value: 'VALIDATION', label: 'Validation' },
  { value: 'RECONCILIATION', label: 'Reconciliation' },
  { value: 'SCHEDULE', label: 'Scheduled Job' },
  { value: 'DATA_QUALITY', label: 'Data Quality Anomaly' },
];
const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];
const ENVIRONMENTS = ['PRODUCTION', 'STAGING', 'DEVELOPMENT', 'TEST'];

const EMPTY = {
  title: '', description: '', source: 'PIPELINE', severity: 'MEDIUM',
  dataset_name: '', pipeline_name: '', connection_name: '',
  environment: 'PRODUCTION', owner_team: '', affected_records: '',
};

export function CreateIncidentModal({ open, onClose, onCreated }) {
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleClose = () => { setForm(EMPTY); setError(''); onClose(); };

  const handleSubmit = async () => {
    if (!form.title.trim()) { setError('Title is required.'); return; }
    setSaving(true);
    setError('');
    try {
      const created = await incidentApi.create({
        ...form,
        affected_records: form.affected_records ? Number(form.affected_records) : null,
        affected_datasets: form.dataset_name ? [form.dataset_name] : [],
        affected_pipelines: form.pipeline_name ? [form.pipeline_name] : [],
        affected_connections: form.connection_name ? [form.connection_name] : [],
      });
      setForm(EMPTY);
      onCreated?.(created);
    } catch (err) {
      setError(err?.message ?? 'Could not create incident.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title="Create Incident"
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={handleClose} disabled={saving}>Cancel</Button>
          <Button variant="primary" onClick={handleSubmit} loading={saving}>Create Incident</Button>
        </>
      }
    >
      {error && <div className="inc-form-error">{error}</div>}

      <div className="inc-field" style={{ marginBottom: 14 }}>
        <label className="inc-field-label">Title <span className="inc-required">*</span></label>
        <input className="inc-input" style={{ width: '100%' }} value={form.title} onChange={set('title')} placeholder="e.g. Customer Data Quality Failure" autoFocus />
      </div>

      <div className="inc-field" style={{ marginBottom: 14 }}>
        <label className="inc-field-label">Description</label>
        <textarea className="inc-textarea" value={form.description} onChange={set('description')} placeholder="What happened?" />
      </div>

      <div className="inc-field-grid">
        <div className="inc-field">
          <label className="inc-field-label">Source <span className="inc-required">*</span></label>
          <select className="inc-input" style={{ width: '100%' }} value={form.source} onChange={set('source')}>
            {SOURCES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </div>
        <div className="inc-field">
          <label className="inc-field-label">Severity</label>
          <select className="inc-input" style={{ width: '100%' }} value={form.severity} onChange={set('severity')}>
            {SEVERITIES.map((s) => <option key={s} value={s}>{s.charAt(0) + s.slice(1).toLowerCase()}</option>)}
          </select>
        </div>
        <div className="inc-field">
          <label className="inc-field-label">Dataset</label>
          <input className="inc-input" style={{ width: '100%' }} value={form.dataset_name} onChange={set('dataset_name')} placeholder="e.g. CUSTOMER_GOLD" />
        </div>
        <div className="inc-field">
          <label className="inc-field-label">Pipeline</label>
          <input className="inc-input" style={{ width: '100%' }} value={form.pipeline_name} onChange={set('pipeline_name')} placeholder="e.g. Customer Pipeline" />
        </div>
        <div className="inc-field">
          <label className="inc-field-label">Connection</label>
          <input className="inc-input" style={{ width: '100%' }} value={form.connection_name} onChange={set('connection_name')} placeholder="Optional" />
        </div>
        <div className="inc-field">
          <label className="inc-field-label">Environment</label>
          <select className="inc-input" style={{ width: '100%' }} value={form.environment} onChange={set('environment')}>
            {ENVIRONMENTS.map((e) => <option key={e} value={e}>{e.charAt(0) + e.slice(1).toLowerCase()}</option>)}
          </select>
        </div>
        <div className="inc-field">
          <label className="inc-field-label">Owner team</label>
          <input className="inc-input" style={{ width: '100%' }} value={form.owner_team} onChange={set('owner_team')} placeholder="e.g. Data Engineering" />
        </div>
        <div className="inc-field">
          <label className="inc-field-label">Affected records</label>
          <input type="number" min={0} className="inc-input" style={{ width: '100%' }} value={form.affected_records} onChange={set('affected_records')} placeholder="Optional" />
        </div>
      </div>
    </Modal>
  );
}
