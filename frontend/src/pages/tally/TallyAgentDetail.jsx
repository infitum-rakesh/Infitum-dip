/**
 * TallyAgentDetail — /app/tally/agents/:installationId.
 *
 * One agent installation's full status + lifecycle actions: issue a
 * fresh enrollment token (while PENDING_ENROLLMENT), rotate its
 * credential (immediately force-disconnects it if currently online —
 * see backend/app/api/tally_agents.py's rotate-credential route), or
 * revoke it permanently (no un-revoke — a revoked installation can
 * never reconnect; re-permitting means creating a new one).
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, Copy, KeyRound, ShieldOff } from 'lucide-react';
import { Button, StatusBadge, LoadingSpinner, Modal } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import tallyAgentApi from '@/services/api/tallyAgentApi.js';
import './tally.css';

function apiErrorMessage(err, fallback) {
  return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback;
}

const STATUS_VARIANT = {
  ENROLLED: 'success', PENDING_ENROLLMENT: 'warning', REVOKED: 'danger', DISABLED: 'gray',
};

export default function TallyAgentDetail() {
  const { installationId } = useParams();
  const navigate = useNavigate();
  const [installation, setInstallation] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [rotateResult, setRotateResult] = useState(null); // { credentialId, credentialSecret, forceDisconnected } | null
  const [rotating, setRotating] = useState(false);
  const [rotateError, setRotateError] = useState(null);

  const [revokeOpen, setRevokeOpen] = useState(false);
  const [revokeReason, setRevokeReason] = useState('');
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    tallyAgentApi.getInstallation(installationId)
      .then((i) => { setInstallation(i); setLoading(false); })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load this agent installation.')); setLoading(false); });
  }, [installationId]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [load]);

  const handleRotate = async () => {
    setRotating(true);
    setRotateError(null);
    try {
      const result = await tallyAgentApi.rotateCredential(installationId);
      setRotateResult({ credentialId: result.credential_id, credentialSecret: result.credential_secret, forceDisconnected: result.force_disconnected });
      load();
    } catch (err) {
      setRotateError(apiErrorMessage(err, 'Could not rotate this agent\'s credential.'));
    } finally {
      setRotating(false);
    }
  };

  const handleRevoke = async () => {
    setRevoking(true);
    setRevokeError(null);
    try {
      await tallyAgentApi.revokeInstallation(installationId, revokeReason.trim() || undefined);
      setRevokeOpen(false);
      load();
    } catch (err) {
      setRevokeError(apiErrorMessage(err, 'Could not revoke this agent.'));
    } finally {
      setRevoking(false);
    }
  };

  const copy = (text) => navigator.clipboard?.writeText(text);

  if (loading) return <LoadingSpinner />;
  if (error) return <div className="tly-page"><div className="tly-banner-error">{error}</div></div>;
  if (!installation) return null;

  return (
    <div className="tly-page">
      <Button size="sm" variant="secondary" icon={<ArrowLeft size={14} />} onClick={() => navigate('/app/tally/agents')} style={{ marginBottom: 12 }}>
        Back to Agents
      </Button>

      <div className="tly-detail-panel">
        <div className="tly-detail-header">
          <h3>{installation.name}</h3>
          <div style={{ display: 'flex', gap: 6 }}>
            <StatusBadge variant={STATUS_VARIANT[installation.status] ?? 'gray'}>{installation.status}</StatusBadge>
            <StatusBadge variant={installation.is_connected ? 'success' : 'gray'} dot>
              {installation.is_connected ? 'Online' : 'Offline'}
            </StatusBadge>
          </div>
        </div>
        <div className="tly-detail-meta">
          <span>Tally target: <strong>{installation.tally_host}:{installation.tally_port}</strong></span>
          <span>Hostname: <strong>{installation.hostname || '—'}</strong></span>
          <span>Agent version: <strong>{installation.agent_version || '—'}</strong></span>
          <span>OS: <strong>{installation.os_version || '—'}</strong></span>
        </div>
        <div className="tly-detail-meta">
          <span>Last heartbeat: <strong>{installation.last_heartbeat_at ? formatDateTime(installation.last_heartbeat_at) : 'Never'}</strong></span>
          <span>Last connected: <strong>{installation.last_connected_at ? formatDateTime(installation.last_connected_at) : 'Never'}</strong></span>
          <span>Last disconnected: <strong>{installation.last_disconnected_at ? formatDateTime(installation.last_disconnected_at) : '—'}</strong></span>
          <span>Last seen IP: <strong>{installation.last_seen_ip || '—'}</strong></span>
        </div>
        <div className="tly-detail-meta">
          <span>Created: <strong>{formatDateTime(installation.created_at)}</strong></span>
          {installation.revoked_at && <span>Revoked: <strong>{formatDateTime(installation.revoked_at)}</strong> ({installation.revoked_reason || 'no reason given'})</span>}
        </div>
      </div>

      {installation.status !== 'REVOKED' && (
        <div className="tly-detail-panel" style={{ marginTop: 16 }}>
          <div className="tly-section-title">Lifecycle Actions</div>
          {rotateError && <div className="tly-banner-error">{rotateError}</div>}
          <div style={{ display: 'flex', gap: 8 }}>
            {installation.status === 'ENROLLED' && (
              <Button size="sm" variant="secondary" icon={<KeyRound size={14} />} loading={rotating} onClick={handleRotate}>
                Rotate Credential
              </Button>
            )}
            <Button size="sm" variant="danger" icon={<ShieldOff size={14} />} onClick={() => { setRevokeReason(''); setRevokeError(null); setRevokeOpen(true); }}>
              Revoke
            </Button>
          </div>
          <p className="tly-text-muted">
            Rotating issues a brand-new credential and immediately disconnects the agent if it's currently online —
            the operator must update their stored credential. Revoking is permanent; the agent can never reconnect
            afterward.
          </p>
        </div>
      )}

      <Modal open={!!rotateResult} onClose={() => setRotateResult(null)} title="Credential Rotated" size="md">
        {rotateResult && (
          <>
            <p className="tly-text-muted" style={{ marginBottom: 10 }}>
              Shown once — relay these to the machine's operator so they can run{' '}
              <code className="tly-code">tally_agent rotate-credential</code>.
              {rotateResult.forceDisconnected && ' The agent was online and has been disconnected.'}
            </p>
            <div className="tly-build-field" style={{ marginBottom: 10 }}>
              <label>Credential ID</label>
              <div style={{ display: 'flex', gap: 8 }}>
                <span className="tly-code" style={{ flex: 1 }}>{rotateResult.credentialId}</span>
                <Button size="sm" variant="secondary" icon={<Copy size={14} />} onClick={() => copy(rotateResult.credentialId)} />
              </div>
            </div>
            <div className="tly-build-field" style={{ marginBottom: 10 }}>
              <label>Credential Secret</label>
              <div style={{ display: 'flex', gap: 8 }}>
                <span className="tly-code" style={{ flex: 1 }}>{rotateResult.credentialSecret}</span>
                <Button size="sm" variant="secondary" icon={<Copy size={14} />} onClick={() => copy(rotateResult.credentialSecret)} />
              </div>
            </div>
          </>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <Button variant="secondary" onClick={() => setRotateResult(null)}>Close</Button>
        </div>
      </Modal>

      <Modal open={revokeOpen} onClose={() => setRevokeOpen(false)} title={`Revoke ${installation.name}?`} size="sm">
        {revokeError && <div className="tly-banner-error" style={{ marginBottom: 12 }}>{revokeError}</div>}
        <p className="tly-text-muted" style={{ marginBottom: 12 }}>
          This permanently disables this installation and disconnects it if online. There is no un-revoke —
          re-permitting this machine means creating a brand-new agent installation.
        </p>
        <div className="tly-build-field" style={{ marginBottom: 16 }}>
          <label>Reason (optional)</label>
          <input className="tly-inline-input" style={{ maxWidth: 'none' }} value={revokeReason} onChange={(e) => setRevokeReason(e.target.value)} />
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="secondary" onClick={() => setRevokeOpen(false)}>Cancel</Button>
          <Button variant="danger" loading={revoking} onClick={handleRevoke}>Revoke</Button>
        </div>
      </Modal>
    </div>
  );
}
