/**
 * VerificationJobs — /app/india-verification/jobs (Phase Group 4,
 * IV21/IV22). Two kinds of run shown side by side (see
 * verification_job_service.py's own docstring for why they're not
 * symmetric):
 *  - MANUAL: a real batch the user triggers here with real identifiers
 *    — each item runs real checks, partial failures are isolated.
 *  - SCHEDULE: a recurring expired-check advisory sweep (configured on
 *    a Schedule, see ScheduleFormModal's "India Verification" type) —
 *    items land as NEEDS_ATTENTION, never a fabricated re-verification,
 *    since the identifier was never stored.
 */
import { useEffect, useState } from 'react';
import { Boxes, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { Button, Modal, StatusBadge, EmptyState, LoadingSpinner } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import indiaVerificationApi from '@/services/api/indiaVerificationApi.js';
import './compliance.css';

function apiErrorMessage(err, fallback) {
  return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback;
}

const JOB_STATUS_VARIANT = {
  PENDING: 'gray', RUNNING: 'info', COMPLETED: 'success', COMPLETED_WITH_ERRORS: 'warning', FAILED: 'danger',
};
const ITEM_STATUS_VARIANT = {
  PENDING: 'gray', RUNNING: 'info', SUCCESS: 'success', FAILED: 'danger', NEEDS_ATTENTION: 'warning',
};
const BATCHABLE_CHECK_TYPES = ['PAN', 'GSTIN', 'TAN', 'UDYAM'];
const EMPTY_SUBJECT = () => ({ subjectType: 'VENDOR', subjectName: '', identifiers: {} });

export default function VerificationJobs() {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [providers, setProviders] = useState([]);

  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState({ businessProcess: '', checkTypes: ['GSTIN'], connectionId: '', subjects: [EMPTY_SUBJECT()] });
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState(null);

  const [detailJob, setDetailJob] = useState(null);
  const [retrying, setRetrying] = useState(false);

  const load = () => {
    setLoading(true);
    indiaVerificationApi.listJobs({ page_size: 30 })
      .then((r) => { setJobs(r.items ?? []); setLoading(false); })
      .catch(() => setLoading(false));
  };
  useEffect(() => {
    load();
    indiaVerificationApi.listProviders().then((r) => setProviders(r.items ?? [])).catch(() => setProviders([]));
  }, []);

  const openDetail = async (job) => {
    setDetailJob(job);
    const fresh = await indiaVerificationApi.getJob(job.id);
    setDetailJob(fresh);
  };

  const refreshDetail = async () => {
    if (!detailJob) return;
    const fresh = await indiaVerificationApi.getJob(detailJob.id);
    setDetailJob(fresh);
    load();
  };

  const toggleCheckType = (t) => setForm((f) => ({ ...f, checkTypes: f.checkTypes.includes(t) ? f.checkTypes.filter((x) => x !== t) : [...f.checkTypes, t] }));

  const updateSubject = (idx, patch) => setForm((f) => ({ ...f, subjects: f.subjects.map((s, i) => (i === idx ? { ...s, ...patch } : s)) }));
  const updateIdentifier = (idx, checkType, value) => setForm((f) => ({
    ...f, subjects: f.subjects.map((s, i) => (i === idx ? { ...s, identifiers: { ...s.identifiers, [checkType]: value } } : s)),
  }));
  const addSubject = () => setForm((f) => ({ ...f, subjects: [...f.subjects, EMPTY_SUBJECT()] }));
  const removeSubject = (idx) => setForm((f) => ({ ...f, subjects: f.subjects.filter((_, i) => i !== idx) }));

  const canCreate = () => form.checkTypes.length > 0 && form.subjects.length > 0 && form.subjects.every((s) => s.subjectName.trim());

  const handleCreate = async () => {
    if (!canCreate()) return;
    setCreating(true);
    setCreateError(null);
    try {
      await indiaVerificationApi.createJob({
        businessProcess: form.businessProcess.trim() || undefined, checkTypes: form.checkTypes,
        connectionId: form.connectionId || undefined,
        subjects: form.subjects.map((s) => ({ subject_type: s.subjectType, subject_name: s.subjectName.trim(), identifiers: s.identifiers })),
      });
      setFormOpen(false);
      setForm({ businessProcess: '', checkTypes: ['GSTIN'], connectionId: '', subjects: [EMPTY_SUBJECT()] });
      load();
    } catch (err) {
      setCreateError(apiErrorMessage(err, 'Could not start this batch.'));
    } finally {
      setCreating(false);
    }
  };

  const handleRetryFailed = async () => {
    if (!detailJob) return;
    setRetrying(true);
    try {
      await indiaVerificationApi.retryFailedJobItems(detailJob.id);
      await refreshDetail();
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div className="iv-page">
      <div className="iv-header">
        <div>
          <h1 className="iv-title"><Boxes size={20} /> Verification Batches</h1>
          <p className="iv-subtitle">
            Run identifier checks (PAN/GSTIN/TAN/Udyam) across several subjects at once. MCA, Aadhaar, and MOA aren't
            batchable — MCA needs a human to pick an ambiguous match, Aadhaar needs fresh per-run consent, MOA is
            document-driven. A failed record never blocks the rest of the batch.
          </p>
        </div>
        <Button icon={<Plus size={14} />} onClick={() => setFormOpen(true)}>New Batch</Button>
      </div>

      <div className="iv-list-table-panel">
        {loading ? (
          <LoadingSpinner />
        ) : jobs.length === 0 ? (
          <EmptyState icon={<Boxes size={28} />} title="No batches yet" description="Run one above, or configure a scheduled expired-check sweep." />
        ) : (
          <table className="iv-mini-table">
            <thead>
              <tr>
                <th>Trigger</th><th>Business Process</th><th>Checks</th><th>Status</th><th>Progress</th><th>Started</th><th></th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id}>
                  <td><StatusBadge variant="gray">{j.trigger_type}</StatusBadge></td>
                  <td>{j.business_process ?? <em>—</em>}</td>
                  <td>{j.check_types.join(', ') || <em>—</em>}</td>
                  <td><StatusBadge variant={JOB_STATUS_VARIANT[j.status] ?? 'gray'} dot>{j.status}</StatusBadge></td>
                  <td>{j.success_items}/{j.total_items} ok{j.failed_items ? `, ${j.failed_items} failed` : ''}{j.needs_attention_items ? `, ${j.needs_attention_items} needs attention` : ''}</td>
                  <td>{j.started_at ? formatDateTime(j.started_at) : formatDateTime(j.created_at)}</td>
                  <td><Button size="sm" variant="secondary" onClick={() => openDetail(j)}>View</Button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Modal open={formOpen} onClose={() => setFormOpen(false)} title="New Verification Batch" size="lg">
        {createError && <div className="iv-banner-error" style={{ marginBottom: 12 }}>{createError}</div>}
        <div className="iv-build-row">
          <div className="iv-build-field">
            <label>Business Process (optional)</label>
            <input className="iv-inline-input" value={form.businessProcess} onChange={(e) => setForm((f) => ({ ...f, businessProcess: e.target.value }))} placeholder="e.g. VENDOR_ONBOARDING" />
          </div>
          <div className="iv-build-field">
            <label>Verification Provider</label>
            <select className="iv-inline-select" value={form.connectionId} onChange={(e) => setForm((f) => ({ ...f, connectionId: e.target.value }))}>
              <option value="">None — checks will report NOT_CONFIGURED</option>
              {providers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
        </div>
        <div className="iv-build-field" style={{ marginBottom: 12 }}>
          <label>Check Types</label>
          <div style={{ display: 'flex', gap: 10 }}>
            {BATCHABLE_CHECK_TYPES.map((t) => (
              <label key={t} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 'var(--fs-sm)' }}>
                <input type="checkbox" checked={form.checkTypes.includes(t)} onChange={() => toggleCheckType(t)} />{t}
              </label>
            ))}
          </div>
        </div>

        <div className="iv-section-title" style={{ marginBottom: 4 }}>Subjects</div>
        {form.subjects.map((s, idx) => (
          <div key={idx} className="iv-result-card" style={{ padding: 12, marginBottom: 8 }}>
            <div className="iv-build-row">
              <div className="iv-build-field">
                <label>Subject Name</label>
                <input className="iv-inline-input" value={s.subjectName} onChange={(e) => updateSubject(idx, { subjectName: e.target.value })} placeholder="e.g. Acme Supplies Pvt Ltd" />
              </div>
              <div className="iv-build-field">
                <label>Subject Type</label>
                <select className="iv-inline-select" value={s.subjectType} onChange={(e) => updateSubject(idx, { subjectType: e.target.value })}>
                  <option>VENDOR</option><option>CUSTOMER</option><option>COMPANY</option><option>OTHER</option>
                </select>
              </div>
              {form.subjects.length > 1 && (
                <Button size="sm" variant="secondary" icon={<Trash2 size={12} />} onClick={() => removeSubject(idx)}>Remove</Button>
              )}
            </div>
            <div className="iv-build-row">
              {form.checkTypes.map((t) => (
                <div key={t} className="iv-build-field">
                  <label>{t}</label>
                  <input className="iv-inline-input" value={s.identifiers[t] || ''} onChange={(e) => updateIdentifier(idx, t, e.target.value)} style={{ textTransform: 'uppercase' }} />
                </div>
              ))}
            </div>
          </div>
        ))}
        <Button size="sm" variant="secondary" icon={<Plus size={12} />} onClick={addSubject} style={{ marginBottom: 16 }}>Add Subject</Button>

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="secondary" onClick={() => setFormOpen(false)}>Cancel</Button>
          <Button loading={creating} disabled={!canCreate()} onClick={handleCreate}>Run Batch</Button>
        </div>
      </Modal>

      <Modal open={!!detailJob} onClose={() => setDetailJob(null)} title={detailJob ? `Batch #${detailJob.id}` : ''} size="lg">
        {detailJob && (
          <>
            <div className="iv-result-meta" style={{ marginBottom: 12 }}>
              <span>Status: <StatusBadge variant={JOB_STATUS_VARIANT[detailJob.status] ?? 'gray'} dot>{detailJob.status}</StatusBadge></span>
              <span>Success: <strong>{detailJob.success_items}</strong></span>
              <span>Failed: <strong>{detailJob.failed_items}</strong></span>
              <span>Needs Attention: <strong>{detailJob.needs_attention_items}</strong></span>
            </div>
            <table className="iv-mini-table">
              <thead><tr><th>Subject</th><th>Check</th><th>Status</th><th>Result</th><th>Error</th></tr></thead>
              <tbody>
                {(detailJob.items ?? []).map((it) => (
                  <tr key={it.id}>
                    <td>{it.subject_name}</td>
                    <td>{it.check_type ?? Object.keys(it.result_summary || {}).join(', ')}</td>
                    <td><StatusBadge variant={ITEM_STATUS_VARIANT[it.status] ?? 'gray'} dot>{it.status}</StatusBadge></td>
                    <td>{Object.entries(it.result_summary || {}).map(([k, v]) => `${k}:${v}`).join(', ') || '—'}</td>
                    <td>{it.error_message ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {detailJob.failed_items > 0 && (
              <div style={{ marginTop: 12 }}>
                <Button size="sm" icon={<RefreshCw size={12} />} loading={retrying} onClick={handleRetryFailed}>Retry Failed</Button>
              </div>
            )}
          </>
        )}
      </Modal>
    </div>
  );
}
