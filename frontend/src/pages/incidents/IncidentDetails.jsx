/**
 * IncidentDetails — /app/incidents/:incidentId
 * Root cause + AI investigation, timeline, evidence, and the full action
 * set (Assign / Start Investigation / Change Severity / Add Comment /
 * Add Evidence / Resolve / Reopen / Close).
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft, UserPlus, PlayCircle, Gauge, CheckCircle2, RotateCcw, Lock,
  AlertTriangle, Sparkles, FileWarning, ScrollText, Paperclip, Send, Plus,
} from 'lucide-react';
import { Button, StatusBadge, Modal, EmptyState, PermissionGate, FullPageSpinner } from '@/components/common/index.js';
import { INCIDENT_PERMISSIONS } from '@/utils/constants.js';
import { useAuth } from '@/hooks/useAuth.js';
import { incidentApi } from '@/services/api/incidentApi.js';
import { relativeTime } from './incidentFormatters.js';
import './incidents.css';

const SEVERITY_VARIANT = { CRITICAL: 'danger', HIGH: 'warning', MEDIUM: 'info', LOW: 'gray' };
const STATUS_VARIANT = { OPEN: 'gray', INVESTIGATING: 'info', MITIGATED: 'warning', RESOLVED: 'success', CLOSED: 'gray' };
const SOURCE_LABEL = {
  PIPELINE: 'Pipeline', CONNECTION: 'Connection', OCR: 'OCR', AI_EXTRACTION: 'AI Extraction',
  TRANSFORMATION: 'Transformation', VALIDATION: 'Validation', RECONCILIATION: 'Reconciliation',
  SCHEDULE: 'Scheduled Job', DATA_QUALITY: 'Data Quality',
};
const TIMELINE_LABEL = {
  DETECTED: 'Detected', ASSIGNED: 'Assigned', INVESTIGATION_STARTED: 'Investigation Started',
  ROOT_CAUSE_IDENTIFIED: 'Root Cause Identified', MITIGATION: 'Mitigation', RESOLVED: 'Resolved',
  REOPENED: 'Reopened', CLOSED: 'Closed', SEVERITY_CHANGED: 'Severity Changed',
  COMMENT: 'Comment', EVIDENCE_ADDED: 'Evidence Added',
};
const EVIDENCE_TYPE_LABEL = {
  LOG: 'Logs', VALIDATION_FAILURE: 'Validation Failure', PIPELINE_RUN: 'Pipeline Run',
  DATA_SAMPLE: 'Data Sample', CONNECTION_STATUS: 'Connection Status', RECONCILIATION_RESULT: 'Reconciliation Result',
};

export default function IncidentDetails() {
  const { incidentId } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();

  const [incident, setIncident] = useState(null);
  const [timeline, setTimeline] = useState([]);
  const [evidence, setEvidence] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const [assignOpen, setAssignOpen] = useState(false);
  const [severityOpen, setSeverityOpen] = useState(false);
  const [resolveOpen, setResolveOpen] = useState(false);
  const [reopenOpen, setReopenOpen] = useState(false);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [comment, setComment] = useState('');

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    Promise.all([incidentApi.get(incidentId), incidentApi.getTimeline(incidentId), incidentApi.getEvidence(incidentId)])
      .then(([inc, tl, ev]) => { setIncident(inc); setTimeline(tl.items); setEvidence(ev.items); })
      .catch(() => setError('Incident not found or failed to load.'))
      .finally(() => setLoading(false));
  }, [incidentId]);

  useEffect(() => { load(); }, [load]);

  const withBusy = (fn) => async (...args) => {
    setBusy(true);
    try { await fn(...args); load(); } finally { setBusy(false); }
  };

  const runStartInvestigation = withBusy(() => incidentApi.startInvestigation(incidentId));
  const runClose = withBusy(() => incidentApi.close(incidentId));
  const runComment = withBusy(async () => {
    if (!comment.trim()) return;
    await incidentApi.addComment(incidentId, comment.trim());
    setComment('');
  });

  if (loading) return <FullPageSpinner />;
  if (error || !incident) {
    return (
      <div className="inc-page">
        <EmptyState icon={<AlertTriangle size={28} />} title="Incident not found" description={error ?? `No incident matches "${incidentId}".`}
          action={{ label: 'Back to Incidents', onClick: () => navigate('/app/incidents') }} />
      </div>
    );
  }

  const isOpenState = !['RESOLVED', 'CLOSED'].includes(incident.status);
  const isClosed = incident.status === 'CLOSED';

  return (
    <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_VIEW} showReason>
      <div className="inc-page">
        <button className="incd-back-btn" onClick={() => navigate('/app/incidents')}>
          <ArrowLeft size={14} /> Incidents
        </button>

        {/* Header */}
        <div className="incd-header">
          <div>
            <div className="incd-title-row">
              <span className="incd-id">{incident.incident_number}</span>
              <h1 className="incd-title">{incident.title}</h1>
              <StatusBadge variant={SEVERITY_VARIANT[incident.severity] ?? 'gray'} dot>{incident.severity}</StatusBadge>
              <StatusBadge variant={STATUS_VARIANT[incident.status] ?? 'gray'} dot>{incident.status}</StatusBadge>
            </div>
            <p className="incd-subtitle">{SOURCE_LABEL[incident.source] ?? incident.source} &middot; {incident.environment} &middot; Detected {relativeTime(incident.detected_at)}</p>
          </div>

          <div className="incd-actions">
            <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_ASSIGN} fallback={null}>
              <Button variant="secondary" size="sm" icon={<UserPlus size={13} />} onClick={() => setAssignOpen(true)}>
                {incident.owner_name || incident.owner_team ? `Owner: ${incident.owner_name ?? incident.owner_team}` : 'Assign'}
              </Button>
            </PermissionGate>
            {incident.status === 'OPEN' && (
              <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_UPDATE} fallback={null}>
                <Button variant="secondary" size="sm" icon={<PlayCircle size={13} />} loading={busy} onClick={runStartInvestigation}>Start Investigation</Button>
              </PermissionGate>
            )}
            <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_UPDATE} fallback={null}>
              <Button variant="secondary" size="sm" icon={<Gauge size={13} />} onClick={() => setSeverityOpen(true)}>Change Severity</Button>
            </PermissionGate>
            {isOpenState && (
              <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_RESOLVE} fallback={null}>
                <Button variant="primary" size="sm" icon={<CheckCircle2 size={13} />} onClick={() => setResolveOpen(true)}>Resolve</Button>
              </PermissionGate>
            )}
            {!isOpenState && (
              <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_UPDATE} fallback={null}>
                <Button variant="secondary" size="sm" icon={<RotateCcw size={13} />} onClick={() => setReopenOpen(true)}>Reopen</Button>
              </PermissionGate>
            )}
            {incident.status === 'RESOLVED' && (
              <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_RESOLVE} fallback={null}>
                <Button variant="secondary" size="sm" icon={<Lock size={13} />} loading={busy} onClick={runClose}>Close</Button>
              </PermissionGate>
            )}
          </div>
        </div>

        {incident.sla_breached && (
          <div className="incd-sla-banner"><AlertTriangle size={15} /> SLA breached — this incident has been open longer than its severity's response window.</div>
        )}

        {/* Meta grid */}
        <div className="incd-meta-grid">
          <MetaItem label="Incident ID" value={incident.incident_number} />
          <MetaItem label="Severity" value={incident.severity} />
          <MetaItem label="Status" value={incident.status} />
          <MetaItem label="Detected" value={new Date(incident.detected_at).toLocaleString()} />
          <MetaItem label="Dataset" value={incident.dataset_name || '—'} />
          <MetaItem label="Pipeline" value={incident.pipeline_name || '—'} />
          <MetaItem label="Environment" value={incident.environment} />
          <MetaItem label="Owner" value={incident.owner_name ?? incident.owner_team ?? 'Unassigned'} />
        </div>

        {/* Two-column body */}
        <div className="incd-grid">
          {/* Left column */}
          <div>
            {/* Root Cause */}
            <div className="incd-card">
              <div className="incd-card-title"><FileWarning size={15} /> Root Cause</div>
              {incident.root_cause
                ? <p className="incd-root-cause-text">{incident.root_cause}</p>
                : <p className="incd-root-cause-empty">No root cause recorded yet.</p>}

              <div className="incd-affected-grid">
                <div>
                  <div className="incd-affected-label">Affected Records</div>
                  <div className="inc-title-text">{incident.affected_records != null ? incident.affected_records.toLocaleString() : '—'}</div>
                </div>
                <div>
                  <div className="incd-affected-label">Affected Pipelines</div>
                  <TagList items={incident.affected_pipelines} />
                </div>
                <div>
                  <div className="incd-affected-label">Affected Datasets</div>
                  <TagList items={incident.affected_datasets} />
                </div>
                <div>
                  <div className="incd-affected-label">Affected Connections</div>
                  <TagList items={incident.affected_connections} />
                </div>
              </div>

              <div className="incd-affected-label" style={{ marginTop: 16 }}><Sparkles size={11} style={{ verticalAlign: -2 }} /> AI Investigation</div>
              {incident.ai_investigation
                ? <div className="incd-ai-box" style={{ marginTop: 6 }}>{incident.ai_investigation}</div>
                : <div className="incd-ai-box incd-ai-box-empty" style={{ marginTop: 6 }}>AI investigation has not produced a finding yet.</div>}
            </div>

            {/* Evidence */}
            <div className="incd-card">
              <div className="incd-card-title" style={{ justifyContent: 'space-between' }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 7 }}><Paperclip size={15} /> Evidence</span>
                <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_UPDATE} fallback={null}>
                  <Button variant="ghost" size="sm" icon={<Plus size={13} />} onClick={() => setEvidenceOpen(true)}>Add Evidence</Button>
                </PermissionGate>
              </div>
              {evidence.length === 0 ? (
                <p className="incd-root-cause-empty">No evidence attached yet — logs, validation failures, pipeline runs, data samples, connection status or reconciliation results.</p>
              ) : (
                evidence.map((e) => (
                  <div key={e.id} className="incd-evidence-item">
                    <div className="incd-evidence-hdr">
                      <span className="incd-evidence-title">{e.title}</span>
                      <StatusBadge variant="gray" size="sm">{EVIDENCE_TYPE_LABEL[e.evidence_type] ?? e.evidence_type}</StatusBadge>
                    </div>
                    <pre className="incd-evidence-content">{JSON.stringify(e.content, null, 2)}</pre>
                    <div className="incd-evidence-meta">{e.created_by_name ?? 'System'} &middot; {relativeTime(e.created_at)}</div>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* Right column: Timeline */}
          <div className="incd-card">
            <div className="incd-card-title"><ScrollText size={15} /> Timeline</div>
            {timeline.length === 0 ? (
              <p className="incd-root-cause-empty">No timeline events yet.</p>
            ) : (
              <div className="incd-timeline">
                {timeline.map((ev, i) => (
                  <div key={ev.id} className="incd-timeline-item">
                    <div className="incd-timeline-rail">
                      <div className="incd-timeline-dot" />
                      {i < timeline.length - 1 && <div className="incd-timeline-line" />}
                    </div>
                    <div className="incd-timeline-body">
                      <div className="incd-timeline-type">{TIMELINE_LABEL[ev.event_type] ?? ev.event_type}</div>
                      {ev.message && <div className="incd-timeline-msg">{ev.message}</div>}
                      <div className="incd-timeline-meta">{ev.actor_name ?? 'System'} &middot; {relativeTime(ev.created_at)}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}

            <PermissionGate {...INCIDENT_PERMISSIONS.INCIDENT_UPDATE} fallback={null}>
              <div className="incd-comment-row">
                <input
                  className="incd-comment-input"
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  placeholder="Add a comment…"
                  onKeyDown={(e) => { if (e.key === 'Enter' && comment.trim()) runComment(); }}
                />
                <Button variant="secondary" size="sm" icon={<Send size={13} />} loading={busy} disabled={!comment.trim()} onClick={runComment}>Post</Button>
              </div>
            </PermissionGate>
          </div>
        </div>
      </div>

      {/* Assign modal */}
      <AssignModal open={assignOpen} onClose={() => setAssignOpen(false)} incident={incident} currentUser={user} onDone={load} />

      {/* Change severity modal */}
      <SeverityModal open={severityOpen} onClose={() => setSeverityOpen(false)} incident={incident} onDone={load} />

      {/* Resolve modal */}
      <ResolveModal open={resolveOpen} onClose={() => setResolveOpen(false)} incidentId={incidentId} onDone={load} />

      {/* Reopen modal */}
      <ReopenModal open={reopenOpen} onClose={() => setReopenOpen(false)} incidentId={incidentId} onDone={load} />

      {/* Add evidence modal */}
      <EvidenceModal open={evidenceOpen} onClose={() => setEvidenceOpen(false)} incidentId={incidentId} onDone={load} />
    </PermissionGate>
  );
}

function MetaItem({ label, value }) {
  return (
    <div className="incd-meta-item">
      <span className="incd-meta-label">{label}</span>
      <span className="incd-meta-value">{value}</span>
    </div>
  );
}

function TagList({ items }) {
  if (!items || items.length === 0) return <span className="incd-empty-inline">None</span>;
  return <div className="inc-tag-list">{items.map((t) => <span key={t} className="inc-tag">{t}</span>)}</div>;
}

// ── Action modals ────────────────────────────────────────────────

function AssignModal({ open, onClose, incident, currentUser, onDone }) {
  const [ownerTeam, setOwnerTeam] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => { if (open) setOwnerTeam(incident?.owner_team ?? ''); }, [open, incident]);

  const submit = async (assignToMe) => {
    setSaving(true);
    try {
      await incidentApi.assign(incident.id, { owner_id: assignToMe ? currentUser?.id : undefined, owner_team: ownerTeam || undefined });
      onDone();
      onClose();
    } finally { setSaving(false); }
  };

  return (
    <Modal open={open} onClose={onClose} title="Assign Incident" size="sm"
      footer={<>
        <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" loading={saving} onClick={() => submit(false)}>Assign</Button>
      </>}>
      <div className="inc-field" style={{ marginBottom: 12 }}>
        <label className="inc-field-label">Owner team</label>
        <input className="inc-input" style={{ width: '100%' }} value={ownerTeam} onChange={(e) => setOwnerTeam(e.target.value)} placeholder="e.g. Data Engineering" />
      </div>
      <Button variant="ghost" size="sm" loading={saving} onClick={() => submit(true)}>Assign to me</Button>
    </Modal>
  );
}

function SeverityModal({ open, onClose, incident, onDone }) {
  const [severity, setSeverity] = useState('MEDIUM');
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => { if (open) { setSeverity(incident?.severity ?? 'MEDIUM'); setReason(''); } }, [open, incident]);

  const submit = async () => {
    setSaving(true);
    try { await incidentApi.changeSeverity(incident.id, severity, reason || undefined); onDone(); onClose(); }
    finally { setSaving(false); }
  };

  return (
    <Modal open={open} onClose={onClose} title="Change Severity" size="sm"
      footer={<><Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button><Button variant="primary" loading={saving} onClick={submit}>Save</Button></>}>
      <div className="inc-field" style={{ marginBottom: 12 }}>
        <label className="inc-field-label">Severity</label>
        <select className="inc-input" style={{ width: '100%' }} value={severity} onChange={(e) => setSeverity(e.target.value)}>
          {['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>
      <div className="inc-field">
        <label className="inc-field-label">Reason (optional)</label>
        <textarea className="inc-textarea" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why is the severity changing?" />
      </div>
    </Modal>
  );
}

function ResolveModal({ open, onClose, incidentId, onDone }) {
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => { if (open) setNote(''); }, [open]);

  const submit = async () => {
    setSaving(true);
    try { await incidentApi.resolve(incidentId, note || undefined); onDone(); onClose(); }
    finally { setSaving(false); }
  };

  return (
    <Modal open={open} onClose={onClose} title="Resolve Incident" size="sm"
      footer={<><Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button><Button variant="primary" loading={saving} onClick={submit}>Resolve</Button></>}>
      <div className="inc-field">
        <label className="inc-field-label">Resolution note (optional)</label>
        <textarea className="inc-textarea" value={note} onChange={(e) => setNote(e.target.value)} placeholder="What fixed it?" autoFocus />
      </div>
    </Modal>
  );
}

function ReopenModal({ open, onClose, incidentId, onDone }) {
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => { if (open) setReason(''); }, [open]);

  const submit = async () => {
    setSaving(true);
    try { await incidentApi.reopen(incidentId, reason || undefined); onDone(); onClose(); }
    finally { setSaving(false); }
  };

  return (
    <Modal open={open} onClose={onClose} title="Reopen Incident" size="sm"
      footer={<><Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button><Button variant="primary" loading={saving} onClick={submit}>Reopen</Button></>}>
      <div className="inc-field">
        <label className="inc-field-label">Reason (optional)</label>
        <textarea className="inc-textarea" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why is this reopening?" autoFocus />
      </div>
    </Modal>
  );
}

function EvidenceModal({ open, onClose, incidentId, onDone }) {
  const [evidenceType, setEvidenceType] = useState('LOG');
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => { if (open) { setEvidenceType('LOG'); setTitle(''); setText(''); setError(''); } }, [open]);

  const submit = async () => {
    if (!title.trim()) { setError('Title is required.'); return; }
    setSaving(true);
    setError('');
    try {
      await incidentApi.addEvidence(incidentId, { evidence_type: evidenceType, title: title.trim(), content: { text } });
      onDone();
      onClose();
    } catch (err) {
      setError(err?.message ?? 'Could not add evidence.');
    } finally { setSaving(false); }
  };

  return (
    <Modal open={open} onClose={onClose} title="Add Evidence" size="sm"
      footer={<><Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button><Button variant="primary" loading={saving} onClick={submit}>Add Evidence</Button></>}>
      {error && <div className="inc-form-error">{error}</div>}
      <div className="inc-field" style={{ marginBottom: 12 }}>
        <label className="inc-field-label">Type</label>
        <select className="inc-input" style={{ width: '100%' }} value={evidenceType} onChange={(e) => setEvidenceType(e.target.value)}>
          {Object.entries(EVIDENCE_TYPE_LABEL).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select>
      </div>
      <div className="inc-field" style={{ marginBottom: 12 }}>
        <label className="inc-field-label">Title <span className="inc-required">*</span></label>
        <input className="inc-input" style={{ width: '100%' }} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Validation run #4021" autoFocus />
      </div>
      <div className="inc-field">
        <label className="inc-field-label">Details</label>
        <textarea className="inc-textarea" value={text} onChange={(e) => setText(e.target.value)} placeholder="Paste logs, failure details, sample rows…" />
      </div>
    </Modal>
  );
}
