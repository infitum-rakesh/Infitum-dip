/**
 * CompanyVerification — /app/india-verification/company (Phase Group 2,
 * IV12). MCA company/LLP search + Udyam/MSME verification.
 *
 * MCA is NOT a single-identifier check like the others: a legal-name
 * search can return several real candidates, and this page (like the
 * backend — see company_verification_service.py) never auto-picks one.
 * An AMBIGUOUS_MATCH result shows every candidate for a human to review.
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Building2, ExternalLink, AlertTriangle } from 'lucide-react';
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
const MCA_MODES = [
  { value: 'cin', label: 'CIN', placeholder: 'U72200MH2015PTC268234' },
  { value: 'llpin', label: 'LLPIN', placeholder: 'ABC1234' },
  { value: 'legalName', label: 'Legal Name', placeholder: 'Shree Enterprises' },
];

export default function CompanyVerification() {
  const navigate = useNavigate();
  const [checkType, setCheckType] = useState('MCA');
  const [mcaMode, setMcaMode] = useState('legalName');
  const [providers, setProviders] = useState([]);
  const [providersLoading, setProvidersLoading] = useState(true);

  const [form, setForm] = useState({ subjectType: 'COMPANY', subjectName: '', searchValue: '', identifier: '', connectionId: '' });
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

  const canRun = () => {
    if (!form.subjectName.trim()) return false;
    return checkType === 'MCA' ? !!form.searchValue.trim() : !!form.identifier.trim();
  };

  const handleRun = async () => {
    if (!canRun()) return;
    setRunning(true);
    setError(null);
    try {
      const body = {
        checkType, subjectType: form.subjectType, subjectName: form.subjectName.trim(), connectionId: form.connectionId || undefined,
      };
      if (checkType === 'MCA') {
        body[mcaMode] = form.searchValue.trim();
      } else {
        body.identifier = form.identifier.trim();
      }
      const result = await indiaVerificationApi.runVerification(body);
      setLastCase(result);
      loadRecent();
    } catch (err) {
      setError(apiErrorMessage(err, 'Verification failed to run.'));
    } finally {
      setRunning(false);
    }
  };

  const lastResult = lastCase?.results?.find((r) => r.check_type === checkType);
  const mcaMatches = lastResult?.normalized_result?.matches ?? [];

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
          <h1 className="iv-title"><Building2 size={20} /> Company &amp; Startup Verification</h1>
          <p className="iv-subtitle">
            MCA company/LLP registration search and Udyam/MSME verification. A legal-name search can return several
            real candidates — this platform never auto-picks one; review and choose manually when a search is ambiguous.
          </p>
        </div>
      </div>

      <div className="iv-build-panel">
        <div className="iv-tab-nav">
          <button type="button" className={`iv-tab-btn ${checkType === 'MCA' ? 'active' : ''}`} onClick={() => setCheckType('MCA')}>MCA Company/LLP</button>
          <button type="button" className={`iv-tab-btn ${checkType === 'UDYAM' ? 'active' : ''}`} onClick={() => setCheckType('UDYAM')}>Udyam/MSME</button>
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
        </div>

        {checkType === 'MCA' ? (
          <div className="iv-build-row">
            <div className="iv-build-field">
              <label>Search By</label>
              <select className="iv-inline-select" value={mcaMode} onChange={(e) => setMcaMode(e.target.value)}>
                {MCA_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </div>
            <div className="iv-build-field">
              <label>{MCA_MODES.find((m) => m.value === mcaMode).label}</label>
              <input className="iv-inline-input" value={form.searchValue} onChange={setField('searchValue')} placeholder={MCA_MODES.find((m) => m.value === mcaMode).placeholder} style={{ textTransform: mcaMode === 'legalName' ? 'none' : 'uppercase' }} />
            </div>
          </div>
        ) : (
          <div className="iv-build-row">
            <div className="iv-build-field">
              <label>Udyam Registration Number</label>
              <input className="iv-inline-input" value={form.identifier} onChange={setField('identifier')} placeholder="UDYAM-MH-01-0001234" style={{ textTransform: 'uppercase' }} />
            </div>
          </div>
        )}

        <div className="iv-build-row">
          <div className="iv-build-field">
            <label>Verification Provider</label>
            <select className="iv-inline-select" value={form.connectionId} onChange={setField('connectionId')}>
              <option value="">None — will report NOT_CONFIGURED</option>
              {providers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
          <Button icon={<Building2 size={14} />} loading={running} disabled={!canRun()} onClick={handleRun}>
            Run {checkType === 'MCA' ? 'MCA Search' : 'Udyam Verification'}
          </Button>
        </div>
      </div>

      {lastResult && checkType === 'MCA' && (
        <div className="iv-result-card" style={{ marginBottom: 20 }}>
          <div className="iv-result-header">
            <h3>Result — {lastCase.subject_name}</h3>
            <StatusBadge variant={STATUS_VARIANT[lastResult.overall_status] ?? 'gray'} dot>{lastResult.overall_status}</StatusBadge>
          </div>
          <div className="iv-result-meta">
            <span>Format: <strong>{lastResult.format_status}</strong></span>
            <span>Provider Outcome: <strong>{lastResult.provider_status_code ?? '—'}</strong></span>
          </div>
          {lastResult.provider_status_code === 'AMBIGUOUS_MATCH' && (
            <div className="iv-banner-info" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <AlertTriangle size={14} /> Several companies match this name — review and pick the right one manually; none is auto-selected.
            </div>
          )}
          {lastResult.provider_status_code === 'NOT_FOUND' && (
            <div className="iv-banner-info" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <AlertTriangle size={14} /> No matching MCA record was found.
            </div>
          )}
          {mcaMatches.length > 0 && (
            <table className="iv-mini-table">
              <thead><tr><th>Legal Name</th><th>CIN</th><th>Type</th><th>Status</th><th>Incorporated</th><th>Registered Office</th></tr></thead>
              <tbody>
                {mcaMatches.map((m) => (
                  <tr key={m.cin}>
                    <td>{m.legal_name}</td>
                    <td><code>{m.cin}</code></td>
                    <td>{m.entity_type}</td>
                    <td><StatusBadge variant={m.status === 'Active' ? 'success' : 'warning'}>{m.status}</StatusBadge></td>
                    <td>{m.incorporation_date}</td>
                    <td>{m.registered_office}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <Button size="sm" variant="secondary" icon={<ExternalLink size={12} />} onClick={() => navigate(`/app/india-verification/cases/${lastCase.id}`)}>
            Open Case
          </Button>
        </div>
      )}

      {lastResult && checkType === 'UDYAM' && (
        <div className="iv-result-card" style={{ marginBottom: 20 }}>
          <div className="iv-result-header">
            <h3>Result — {lastCase.subject_name}</h3>
            <StatusBadge variant={STATUS_VARIANT[lastResult.overall_status] ?? 'gray'} dot>{lastResult.overall_status}</StatusBadge>
          </div>
          <div className="iv-result-meta">
            <span>Format: <strong>{lastResult.format_status}</strong></span>
            <span>Provider Outcome: <strong>{lastResult.provider_status_code ?? '—'}</strong></span>
            {lastResult.normalized_result?.enterprise_name && <span>Enterprise: <strong>{lastResult.normalized_result.enterprise_name}</strong></span>}
            {lastResult.normalized_result?.classification && <span>Classification: <strong>{lastResult.normalized_result.classification}</strong></span>}
          </div>
          {lastResult.overall_status === 'REQUIRES_REVIEW' && (
            <div className="iv-banner-info" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <AlertTriangle size={14} /> The provider did not confirm this Udyam registration — review before proceeding.
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
          emptyState={<EmptyState icon={<Building2 size={28} />} title="No verifications yet" description="Run a check above to get started." />}
        />
      </div>
    </div>
  );
}
