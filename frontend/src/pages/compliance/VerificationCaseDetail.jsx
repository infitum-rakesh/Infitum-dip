/**
 * VerificationCaseDetail — /app/india-verification/cases/:caseId.
 *
 * One case's full result breakdown + a per-check Retry action. No
 * identifier value (PAN/GSTIN/TAN/Aadhaar/Udyam, or MCA's CIN/LLPIN/
 * legal name) is ever persisted in the clear (see models/india_
 * verification.py), so Retry always asks for it again rather than
 * silently replaying a stored value — same discipline as never caching
 * a password to "retry" a login.
 */
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, RefreshCw, ShieldAlert } from 'lucide-react';
import { Button, StatusBadge, LoadingSpinner, Modal } from '@/components/common/index.js';
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

const IDENTIFIER_LABEL = {
  PAN: 'PAN', GSTIN: 'GSTIN', TAN: 'TAN', UDYAM: 'Udyam Registration Number', AADHAAR: 'Aadhaar Number',
};

function summarizeResult(r) {
  const n = r.normalized_result || {};
  if (r.check_type === 'MCA') {
    const matches = n.matches || [];
    if (matches.length === 0) return '—';
    if (matches.length === 1) return `${matches[0].legal_name} (${matches[0].status})`;
    return `${matches.length} candidates — review required`;
  }
  if (r.check_type === 'MOA') {
    if (!n.document_type_confirmed) return 'Document type not confirmed';
    if (n.missing_fields?.length) return `${n.missing_fields.length} clause(s) missing`;
    if (n.comparisons?.length) return n.comparisons.every((c) => c.matched) ? 'MCA details matched' : 'MCA mismatch found';
    return 'Complete — no MCA record to compare';
  }
  const primary = n.holder_name || n.legal_name || n.deductor_name || n.enterprise_name;
  const secondary = n.entity_type || n.registration_status || n.classification;
  if (!primary && !secondary) return '—';
  return secondary ? `${primary ?? '—'} (${secondary})` : primary;
}

export default function VerificationCaseDetail() {
  const { caseId } = useParams();
  const navigate = useNavigate();
  const [verificationCase, setCase] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [providers, setProviders] = useState([]);

  const [retryTarget, setRetryTarget] = useState(null); // the VerificationResultOut being retried, or null
  const [retryForm, setRetryForm] = useState({ identifier: '', cin: '', llpin: '', legalName: '', submittedName: '', connectionId: '', consentReference: '', purpose: '' });
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState(null);

  const load = () => {
    setLoading(true);
    indiaVerificationApi.getVerification(caseId)
      .then((c) => { setCase(c); setLoading(false); })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load this verification case.')); setLoading(false); });
  };

  useEffect(() => { load(); }, [caseId]);
  useEffect(() => {
    indiaVerificationApi.listProviders().then((r) => setProviders(r.items ?? [])).catch(() => setProviders([]));
  }, []);

  const openRetry = (result) => {
    // Defaults to whichever provider this check last used — retrying
    // without re-picking one would otherwise silently drop the provider
    // and the check would report NOT_CONFIGURED instead of actually
    // re-verifying, which is surprising since the user only meant to
    // resupply the identifier.
    setRetryForm({
      identifier: '', cin: '', llpin: '', legalName: '', submittedName: '',
      connectionId: result.provider_connection_id ? String(result.provider_connection_id) : '',
      consentReference: '', purpose: '',
    });
    setRetryError(null);
    setRetryTarget(result);
  };

  const setField = (key) => (e) => setRetryForm((f) => ({ ...f, [key]: e.target.value }));

  const canRetry = () => {
    if (!retryTarget) return false;
    if (retryTarget.check_type === 'MOA') return !!retryTarget.normalized_result?.document_id;
    if (retryTarget.check_type === 'MCA') return !!(retryForm.cin.trim() || retryForm.llpin.trim() || retryForm.legalName.trim());
    if (retryTarget.check_type === 'AADHAAR') return !!(retryForm.identifier.trim() && retryForm.consentReference.trim() && retryForm.purpose.trim());
    return !!retryForm.identifier.trim();
  };

  const handleRetry = async () => {
    if (!canRetry()) return;
    setRetrying(true);
    setRetryError(null);
    try {
      if (retryTarget.check_type === 'MOA') {
        // MOA isn't an identifier check — it re-assesses the SAME linked
        // document (never a re-entered value), so "retry" is just
        // re-running the analysis against this case.
        await indiaVerificationApi.verifyMoa({ documentId: retryTarget.normalized_result.document_id, caseId: Number(caseId) });
      } else {
        await indiaVerificationApi.runCheckOnCase(caseId, retryTarget.check_type, {
          identifier: retryForm.identifier.trim() || undefined,
          cin: retryForm.cin.trim() || undefined, llpin: retryForm.llpin.trim() || undefined, legalName: retryForm.legalName.trim() || undefined,
          submittedName: retryForm.submittedName.trim() || undefined, connectionId: retryForm.connectionId || undefined,
          consentReference: retryForm.consentReference.trim() || undefined, purpose: retryForm.purpose.trim() || undefined,
        });
      }
      setRetryTarget(null);
      load();
    } catch (err) {
      setRetryError(apiErrorMessage(err, 'Retry failed.'));
    } finally {
      setRetrying(false);
    }
  };

  if (loading) return <LoadingSpinner />;
  if (error) return <div className="iv-page"><div className="iv-banner-error">{error}</div></div>;
  if (!verificationCase) return null;

  return (
    <div className="iv-page">
      <Button size="sm" variant="secondary" icon={<ArrowLeft size={14} />} onClick={() => navigate('/app/india-verification')} style={{ marginBottom: 12 }}>
        Back to Dashboard
      </Button>

      <div className="iv-detail-panel">
        <div className="iv-detail-header">
          <h2>{verificationCase.subject_name}</h2>
          <StatusBadge variant={STATUS_VARIANT[verificationCase.overall_status] ?? 'gray'} dot>{verificationCase.overall_status}</StatusBadge>
        </div>
        <div className="iv-detail-meta">
          <span>Type: <strong>{verificationCase.subject_type}</strong></span>
          {verificationCase.business_process && <span>Process: <strong>{verificationCase.business_process}</strong></span>}
          <span>Created: <strong>{formatDateTime(verificationCase.created_at)}</strong></span>
          <span>Updated: <strong>{formatDateTime(verificationCase.updated_at)}</strong></span>
        </div>
      </div>

      <div className="iv-section-title">Checks</div>
      <div className="iv-list-table-panel" style={{ marginTop: 8, padding: 16 }}>
        {(verificationCase.results ?? []).length === 0 ? (
          <p className="iv-text-muted">No checks recorded on this case yet.</p>
        ) : (
          <table className="iv-mini-table">
            <thead>
              <tr>
                <th>Check</th><th>Format</th><th>Provider Outcome</th><th>Name Match</th>
                <th>Overall</th><th>Result</th><th>Verified At</th><th>Attempts</th><th></th>
              </tr>
            </thead>
            <tbody>
              {verificationCase.results.map((r) => (
                <tr key={r.id}>
                  <td><StatusBadge variant="gray">{r.check_type}</StatusBadge></td>
                  <td>{r.format_status}</td>
                  <td>{r.provider_status_code ?? '—'}</td>
                  <td>{r.match_status ?? '—'}</td>
                  <td><StatusBadge variant={STATUS_VARIANT[r.overall_status] ?? 'gray'} dot>{r.overall_status}</StatusBadge></td>
                  <td>{summarizeResult(r)}</td>
                  <td>{r.verified_at ? formatDateTime(r.verified_at) : '—'}</td>
                  <td>{r.retry_count + 1}</td>
                  <td><Button size="sm" variant="secondary" icon={<RefreshCw size={12} />} onClick={() => openRetry(r)}>Retry</Button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Modal open={!!retryTarget} onClose={() => setRetryTarget(null)} title={retryTarget ? `Retry ${retryTarget.check_type} Verification` : ''} size="sm">
        {retryTarget?.check_type !== 'MOA' && (
          <p className="iv-text-muted" style={{ marginBottom: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
            <ShieldAlert size={14} /> The value is never stored — enter it again to retry.
          </p>
        )}
        {retryError && <div className="iv-banner-error" style={{ marginBottom: 12 }}>{retryError}</div>}

        {retryTarget?.check_type === 'MOA' ? (
          <p className="iv-text-muted" style={{ marginBottom: 12 }}>
            Re-runs the completeness/authenticity analysis against the same linked document and, if an MCA result
            now exists on this case, re-attempts the cross-check — no value to re-enter.
          </p>
        ) : retryTarget?.check_type === 'MCA' ? (
          <>
            <div className="iv-build-field" style={{ marginBottom: 12 }}>
              <label>CIN</label>
              <input className="iv-inline-input" value={retryForm.cin} onChange={setField('cin')} placeholder="U72200MH2015PTC268234" style={{ textTransform: 'uppercase' }} />
            </div>
            <div className="iv-build-field" style={{ marginBottom: 12 }}>
              <label>LLPIN</label>
              <input className="iv-inline-input" value={retryForm.llpin} onChange={setField('llpin')} style={{ textTransform: 'uppercase' }} />
            </div>
            <div className="iv-build-field" style={{ marginBottom: 12 }}>
              <label>Or Legal Name (search)</label>
              <input className="iv-inline-input" value={retryForm.legalName} onChange={setField('legalName')} />
            </div>
          </>
        ) : (
          <div className="iv-build-field" style={{ marginBottom: 12 }}>
            <label>{IDENTIFIER_LABEL[retryTarget?.check_type] ?? 'Identifier'}</label>
            <input className="iv-inline-input" value={retryForm.identifier} onChange={setField('identifier')} style={{ textTransform: 'uppercase' }} />
          </div>
        )}

        {retryTarget?.check_type !== 'MCA' && retryTarget?.check_type !== 'AADHAAR' && retryTarget?.check_type !== 'MOA' && (
          <div className="iv-build-field" style={{ marginBottom: 12 }}>
            <label>Name to Match (optional)</label>
            <input className="iv-inline-input" value={retryForm.submittedName} onChange={setField('submittedName')} />
          </div>
        )}

        {retryTarget?.check_type === 'AADHAAR' && (
          <>
            <div className="iv-build-field" style={{ marginBottom: 12 }}>
              <label>Consent Reference</label>
              <input className="iv-inline-input" value={retryForm.consentReference} onChange={setField('consentReference')} placeholder="e.g. consent ticket / timestamp" />
            </div>
            <div className="iv-build-field" style={{ marginBottom: 12 }}>
              <label>Purpose</label>
              <input className="iv-inline-input" value={retryForm.purpose} onChange={setField('purpose')} placeholder="e.g. Vendor KYC" />
            </div>
          </>
        )}

        {retryTarget?.check_type !== 'MOA' && (
          <div className="iv-build-field" style={{ marginBottom: 16 }}>
            <label>Verification Provider</label>
            <select className="iv-inline-select" value={retryForm.connectionId} onChange={setField('connectionId')}>
              <option value="">None — will report NOT_CONFIGURED</option>
              {providers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="secondary" onClick={() => setRetryTarget(null)}>Cancel</Button>
          <Button loading={retrying} disabled={!canRetry()} onClick={handleRetry}>Retry</Button>
        </div>
      </Modal>
    </div>
  );
}
