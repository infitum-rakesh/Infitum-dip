/**
 * IdentityVerification — /app/india-verification/identity.
 *
 * PAN (Phase 1) + Aadhaar (Phase Group 2) — grouped together per the
 * spec's own section numbering ("Identity Verification"). Aadhaar will
 * only ever report CONSENT_REQUIRED or NOT_CONFIGURED here: real Aadhaar
 * authentication needs a UIDAI AUA/KUA license or a licensed sub-AUA
 * relationship this environment doesn't have (see the approved plan's
 * Regulatory risks section) — format/Verhoeff-checksum validation is
 * real, but VERIFIED is structurally unreachable until a licensed
 * provider is wired. Run a check against a configured Verification
 * Provider connection (reuses the generic Connections module — create
 * one there first, category "Verification Provider"); no identifier
 * value is ever persisted, so there is nothing to "load" back into
 * either form.
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Fingerprint, ExternalLink, AlertTriangle, ShieldAlert } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import indiaVerificationApi from '@/services/api/indiaVerificationApi.js';
import './compliance.css';

function apiErrorMessage(err, fallback) {
  return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback;
}

const STATUS_VARIANT = {
  VERIFIED: 'success', VERIFIED_WITH_WARNINGS: 'warning', PARTIALLY_VERIFIED: 'warning',
  MISMATCH: 'danger', REQUIRES_REVIEW: 'warning', PENDING_PROVIDER: 'info', IN_PROGRESS: 'info',
  NOT_STARTED: 'gray', NOT_CONFIGURED: 'gray', UNAVAILABLE: 'danger', FAILED: 'danger',
};

const SUBJECT_TYPES = ['VENDOR', 'CUSTOMER', 'COMPANY', 'OTHER', 'INDIVIDUAL'];

export default function IdentityVerification() {
  const navigate = useNavigate();
  const [checkType, setCheckType] = useState('PAN');
  const [providers, setProviders] = useState([]);
  const [providersLoading, setProvidersLoading] = useState(true);

  const [form, setForm] = useState({
    subjectType: 'VENDOR', subjectName: '', identifier: '', submittedName: '', connectionId: '',
    consentReference: '', purpose: '',
  });
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [lastCase, setLastCase] = useState(null);

  const [recentCases, setRecentCases] = useState([]);
  const [recentLoading, setRecentLoading] = useState(true);

  useEffect(() => {
    indiaVerificationApi.listProviders()
      .then((r) => { setProviders(r.items ?? []); setProvidersLoading(false); })
      .catch(() => setProvidersLoading(false));
    loadRecent();
  }, []);

  const loadRecent = () => {
    setRecentLoading(true);
    indiaVerificationApi.listVerifications({ page_size: 20 })
      .then((r) => { setRecentCases(r.items ?? []); setRecentLoading(false); })
      .catch(() => setRecentLoading(false));
  };

  const setField = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const canRun = () => {
    if (!form.subjectName.trim() || !form.identifier.trim()) return false;
    if (checkType === 'AADHAAR') return !!(form.consentReference.trim() && form.purpose.trim());
    return true;
  };

  const handleRun = async () => {
    if (!canRun()) return;
    setRunning(true);
    setError(null);
    try {
      const result = await indiaVerificationApi.runVerification({
        checkType, subjectType: form.subjectType, subjectName: form.subjectName.trim(),
        identifier: form.identifier.trim(), submittedName: form.submittedName.trim() || undefined,
        connectionId: form.connectionId || undefined,
        consentReference: checkType === 'AADHAAR' ? form.consentReference.trim() : undefined,
        purpose: checkType === 'AADHAAR' ? form.purpose.trim() : undefined,
      });
      setLastCase(result);
      loadRecent();
    } catch (err) {
      setError(apiErrorMessage(err, 'Verification failed to run.'));
    } finally {
      setRunning(false);
    }
  };

  const lastResult = lastCase?.results?.find((r) => r.check_type === checkType);

  const columns = [
    { key: 'subject_name', header: 'Subject' },
    { key: 'subject_type', header: 'Type', render: (v) => <StatusBadge variant="gray">{v}</StatusBadge> },
    { key: 'overall_status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'created_at', header: 'Created', render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: '',
      render: (_v, row) => <Button size="sm" variant="secondary" icon={<ExternalLink size={12} />} onClick={() => navigate(`/app/india-verification/cases/${row.id}`)}>View</Button>,
    },
  ];

  return (
    <div className="iv-page">
      <div className="iv-header">
        <div>
          <h1 className="iv-title"><Fingerprint size={20} /> Identity Verification</h1>
          <p className="iv-subtitle">
            PAN and Aadhaar verification against a configured Verification Provider — real, auditable checks, never a
            format-only guess. Format-valid alone is never reported as verified; see each result's own status breakdown.
          </p>
        </div>
      </div>

      <div className="iv-build-panel">
        <div className="iv-tab-nav">
          <button type="button" className={`iv-tab-btn ${checkType === 'PAN' ? 'active' : ''}`} onClick={() => setCheckType('PAN')}>PAN</button>
          <button type="button" className={`iv-tab-btn ${checkType === 'AADHAAR' ? 'active' : ''}`} onClick={() => setCheckType('AADHAAR')}>Aadhaar</button>
        </div>

        {error && <div className="iv-banner-error">{error}</div>}
        {!providersLoading && providers.length === 0 && (
          <div className="iv-banner-info">
            No Verification Provider connection configured yet — checks will still run and correctly report
            NOT_CONFIGURED rather than a fabricated pass. Create one from{' '}
            <button type="button" className="iv-text-muted" style={{ textDecoration: 'underline', cursor: 'pointer', background: 'none', border: 'none', padding: 0 }}
              onClick={() => navigate('/app/connections')}>Connections</button> (category "Verification Provider") to
            run real checks.
          </div>
        )}
        {checkType === 'AADHAAR' && (
          <div className="iv-banner-info" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <ShieldAlert size={14} /> Real Aadhaar verification requires a UIDAI-licensed provider, which isn't
            configured anywhere on this platform yet — this check will report CONSENT_REQUIRED or NOT_CONFIGURED,
            never VERIFIED, until one is wired. Format/checksum validation below is real.
          </div>
        )}

        <div className="iv-build-row">
          <div className="iv-build-field">
            <label>Subject Type</label>
            <select className="iv-inline-select" value={form.subjectType} onChange={setField('subjectType')}>
              {SUBJECT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div className="iv-build-field">
            <label>Subject Name</label>
            <input className="iv-inline-input" value={form.subjectName} onChange={setField('subjectName')} placeholder="e.g. Rakesh Test Enterprises" />
          </div>
          <div className="iv-build-field">
            <label>{checkType === 'PAN' ? 'PAN' : 'Aadhaar Number'}</label>
            <input className="iv-inline-input" value={form.identifier} onChange={setField('identifier')} placeholder={checkType === 'PAN' ? 'AAAPL1234C' : '234123412346'} style={{ textTransform: 'uppercase' }} />
          </div>
        </div>
        {checkType === 'AADHAAR' ? (
          <div className="iv-build-row">
            <div className="iv-build-field">
              <label>Consent Reference</label>
              <input className="iv-inline-input" value={form.consentReference} onChange={setField('consentReference')} placeholder="e.g. consent ticket / timestamp" />
            </div>
            <div className="iv-build-field">
              <label>Purpose</label>
              <input className="iv-inline-input" value={form.purpose} onChange={setField('purpose')} placeholder="e.g. Vendor KYC" />
            </div>
          </div>
        ) : null}
        <div className="iv-build-row">
          {checkType === 'PAN' && (
            <div className="iv-build-field">
              <label>Name to Match (optional)</label>
              <input className="iv-inline-input" value={form.submittedName} onChange={setField('submittedName')} placeholder="Checked against the provider's PAN holder name" />
            </div>
          )}
          <div className="iv-build-field">
            <label>Verification Provider</label>
            <select className="iv-inline-select" value={form.connectionId} onChange={setField('connectionId')}>
              <option value="">None — will report NOT_CONFIGURED</option>
              {providers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
          <Button icon={<Fingerprint size={14} />} loading={running} disabled={!canRun()} onClick={handleRun}>
            Run {checkType === 'PAN' ? 'PAN' : 'Aadhaar'} Verification
          </Button>
        </div>
      </div>

      {lastResult && (
        <div className="iv-result-card" style={{ marginBottom: 20 }}>
          <div className="iv-result-header">
            <h3>Result — {lastCase.subject_name}</h3>
            <StatusBadge variant={STATUS_VARIANT[lastResult.overall_status] ?? 'gray'} dot>{lastResult.overall_status}</StatusBadge>
          </div>
          <div className="iv-result-meta">
            <span>Format: <strong>{lastResult.format_status}</strong></span>
            <span>Provider Outcome: <strong>{lastResult.provider_status_code ?? '—'}</strong></span>
            <span>Name Match: <strong>{lastResult.match_status ?? 'Not compared'}</strong></span>
            {lastResult.normalized_result?.holder_name && <span>Holder: <strong>{lastResult.normalized_result.holder_name}</strong></span>}
            {lastResult.normalized_result?.entity_type && <span>Entity Type: <strong>{lastResult.normalized_result.entity_type}</strong></span>}
          </div>
          {lastResult.overall_status === 'REQUIRES_REVIEW' && (
            <div className="iv-banner-info" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <AlertTriangle size={14} /> The provider did not confirm this {checkType === 'PAN' ? 'PAN' : 'Aadhaar number'} — review before proceeding.
            </div>
          )}
          <Button size="sm" variant="secondary" icon={<ExternalLink size={12} />} onClick={() => navigate(`/app/india-verification/cases/${lastCase.id}`)}>
            Open Case
          </Button>
        </div>
      )}

      <div className="iv-section-title">Recent Verifications</div>
      <div className="iv-list-table-panel" style={{ marginTop: 8 }}>
        <DataTable
          columns={columns}
          data={recentCases}
          loading={recentLoading}
          emptyState={
            <EmptyState icon={<Fingerprint size={28} />} title="No verifications yet" description="Run a check above to get started." />
          }
        />
      </div>
    </div>
  );
}
