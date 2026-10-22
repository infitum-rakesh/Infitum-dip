/**
 * TaxRegistrationVerification — /app/india-verification/tax (Phase Group
 * 2, IV12). GSTIN + TAN, same page shape as IdentityVerification.jsx.
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Landmark, ExternalLink, AlertTriangle } from 'lucide-react';
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

const SUBJECT_TYPES = ['VENDOR', 'CUSTOMER', 'COMPANY', 'OTHER'];
const PLACEHOLDER = { GSTIN: '27AAACI1681G1ZP', TAN: 'DELI12345A' };

export default function TaxRegistrationVerification() {
  const navigate = useNavigate();
  const [checkType, setCheckType] = useState('GSTIN');
  const [providers, setProviders] = useState([]);
  const [providersLoading, setProvidersLoading] = useState(true);

  const [form, setForm] = useState({ subjectType: 'VENDOR', subjectName: '', identifier: '', submittedName: '', connectionId: '' });
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [lastCase, setLastCase] = useState(null);

  const [recentCases, setRecentCases] = useState([]);
  const [recentLoading, setRecentLoading] = useState(true);

  useEffect(() => {
    indiaVerificationApi.listProviders().then((r) => { setProviders(r.items ?? []); setProvidersLoading(false); }).catch(() => setProvidersLoading(false));
    loadRecent();
  }, []);

  const loadRecent = () => {
    setRecentLoading(true);
    indiaVerificationApi.listVerifications({ page_size: 20 })
      .then((r) => { setRecentCases(r.items ?? []); setRecentLoading(false); })
      .catch(() => setRecentLoading(false));
  };

  const setField = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleRun = async () => {
    if (!form.subjectName.trim() || !form.identifier.trim()) return;
    setRunning(true);
    setError(null);
    try {
      const result = await indiaVerificationApi.runVerification({
        checkType, subjectType: form.subjectType, subjectName: form.subjectName.trim(),
        identifier: form.identifier.trim(), submittedName: form.submittedName.trim() || undefined,
        connectionId: form.connectionId || undefined,
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
          <h1 className="iv-title"><Landmark size={20} /> Tax Registration Verification</h1>
          <p className="iv-subtitle">
            GSTIN and TAN verification against a configured Verification Provider. GSTIN checks distinguish a
            format-valid number from a live, currently-ACTIVE registration — a cancelled or suspended registration is
            never reported as simply verified.
          </p>
        </div>
      </div>

      <div className="iv-build-panel">
        <div className="iv-tab-nav">
          <button type="button" className={`iv-tab-btn ${checkType === 'GSTIN' ? 'active' : ''}`} onClick={() => setCheckType('GSTIN')}>GSTIN</button>
          <button type="button" className={`iv-tab-btn ${checkType === 'TAN' ? 'active' : ''}`} onClick={() => setCheckType('TAN')}>TAN</button>
        </div>

        {error && <div className="iv-banner-error">{error}</div>}
        {!providersLoading && providers.length === 0 && (
          <div className="iv-banner-info">
            No Verification Provider connection configured yet — checks will still run and correctly report
            NOT_CONFIGURED rather than a fabricated pass.
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
            <input className="iv-inline-input" value={form.subjectName} onChange={setField('subjectName')} placeholder="e.g. Infitum Technologies Pvt Ltd" />
          </div>
          <div className="iv-build-field">
            <label>{checkType}</label>
            <input className="iv-inline-input" value={form.identifier} onChange={setField('identifier')} placeholder={PLACEHOLDER[checkType]} style={{ textTransform: 'uppercase' }} />
          </div>
        </div>
        <div className="iv-build-row">
          <div className="iv-build-field">
            <label>Name to Match (optional)</label>
            <input className="iv-inline-input" value={form.submittedName} onChange={setField('submittedName')} placeholder={checkType === 'GSTIN' ? "Checked against the provider's legal name" : "Checked against the provider's deductor name"} />
          </div>
          <div className="iv-build-field">
            <label>Verification Provider</label>
            <select className="iv-inline-select" value={form.connectionId} onChange={setField('connectionId')}>
              <option value="">None — will report NOT_CONFIGURED</option>
              {providers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
          <Button icon={<Landmark size={14} />} loading={running} disabled={!form.subjectName.trim() || !form.identifier.trim()} onClick={handleRun}>
            Run {checkType} Verification
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
            {lastResult.normalized_result?.state_name && <span>State: <strong>{lastResult.normalized_result.state_name}</strong></span>}
            <span>Provider Outcome: <strong>{lastResult.provider_status_code ?? '—'}</strong></span>
            <span>Name Match: <strong>{lastResult.match_status ?? 'Not compared'}</strong></span>
            {(lastResult.normalized_result?.legal_name || lastResult.normalized_result?.deductor_name) && (
              <span>Name on Record: <strong>{lastResult.normalized_result.legal_name ?? lastResult.normalized_result.deductor_name}</strong></span>
            )}
            {lastResult.normalized_result?.registration_status && <span>Registration Status: <strong>{lastResult.normalized_result.registration_status}</strong></span>}
          </div>
          {lastResult.overall_status === 'REQUIRES_REVIEW' && (
            <div className="iv-banner-info" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <AlertTriangle size={14} /> The provider did not confirm an active {checkType} registration — review before proceeding.
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
          emptyState={<EmptyState icon={<Landmark size={28} />} title="No verifications yet" description="Run a check above to get started." />}
        />
      </div>
    </div>
  );
}
