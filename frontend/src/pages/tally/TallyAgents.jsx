/**
 * TallyAgents — /app/tally/agents.
 *
 * Admin list of Windows Client Agent installations (Multi-Client
 * TallyPrime Integration Platform / Agent Relay) — a second transport
 * alongside the direct-HTTP TALLY_PRIME connections managed on
 * TallyCompanies.jsx. Create a placeholder installation, issue a
 * one-time enrollment token for the Windows operator to run
 * `tally_agent enroll` with, and watch is_connected flip live once
 * their agent process comes online.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Cpu, Copy, Plus, RefreshCw } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState, Modal, LoadingSpinner } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import tallyAgentApi from '@/services/api/tallyAgentApi.js';
import './tally.css';

function apiErrorMessage(err, fallback) {
  return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback;
}

export default function TallyAgents() {
  const navigate = useNavigate();
  const [installations, setInstallations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState({ name: '', tallyHost: '127.0.0.1', tallyPort: '9000' });
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState(null);

  const [tokenResult, setTokenResult] = useState(null); // { installation, token, expiresAt } | null
  const [tokenError, setTokenError] = useState(null);
  const [issuingTokenFor, setIssuingTokenFor] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    tallyAgentApi.listInstallations()
      .then((r) => { setInstallations(r.items); setLoading(false); })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load agent installations.')); setLoading(false); });
  }, []);

  useEffect(() => { load(); }, [load]);
  // Live connections change without this page doing anything — poll so
  // is_connected/last_heartbeat_at stay current without a manual refresh.
  useEffect(() => {
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [load]);

  const openCreate = () => {
    setCreateForm({ name: '', tallyHost: '127.0.0.1', tallyPort: '9000' });
    setCreateError(null);
    setCreateOpen(true);
  };

  const handleCreate = async () => {
    if (!createForm.name.trim()) return;
    setCreating(true);
    setCreateError(null);
    try {
      const installation = await tallyAgentApi.createInstallation({
        name: createForm.name.trim(), tallyHost: createForm.tallyHost.trim(), tallyPort: Number(createForm.tallyPort) || undefined,
      });
      setCreateOpen(false);
      load();
      await issueToken(installation);
    } catch (err) {
      setCreateError(apiErrorMessage(err, 'Could not create the agent installation.'));
    } finally {
      setCreating(false);
    }
  };

  const issueToken = async (installation) => {
    setIssuingTokenFor(installation.id);
    setTokenError(null);
    try {
      const result = await tallyAgentApi.createEnrollmentToken(installation.id);
      setTokenResult({ installation, token: result.token, expiresAt: result.expires_at });
    } catch (err) {
      setTokenError(apiErrorMessage(err, 'Could not issue an enrollment token.'));
      setTokenResult({ installation, token: null, expiresAt: null });
    } finally {
      setIssuingTokenFor(null);
      load();
    }
  };

  const copyToken = () => {
    if (tokenResult?.token) navigator.clipboard?.writeText(tokenResult.token);
  };

  const columns = [
    {
      key: 'name', header: 'Agent', sortable: true,
      render: (v, row) => (
        <button className="tly-link-btn" onClick={() => navigate(`/app/tally/agents/${row.id}`)}>{v}</button>
      ),
    },
    {
      key: 'status', header: 'Status', sortable: true,
      render: (v) => (
        <StatusBadge variant={v === 'ENROLLED' ? 'success' : v === 'REVOKED' ? 'danger' : v === 'DISABLED' ? 'gray' : 'warning'}>
          {v}
        </StatusBadge>
      ),
    },
    {
      key: 'is_connected', header: 'Connected',
      render: (v) => <StatusBadge variant={v ? 'success' : 'gray'} dot>{v ? 'Online' : 'Offline'}</StatusBadge>,
    },
    { key: 'hostname', header: 'Hostname', render: (v) => v || '—' },
    { key: 'agent_version', header: 'Version', render: (v) => v || '—' },
    {
      key: 'last_heartbeat_at', header: 'Last Heartbeat',
      render: (v) => (v ? formatDateTime(v) : 'Never'),
    },
    {
      key: 'actions', header: '',
      render: (_v, row) => (
        <div style={{ display: 'flex', gap: 6 }}>
          {row.status === 'PENDING_ENROLLMENT' && (
            <Button size="sm" variant="secondary" loading={issuingTokenFor === row.id} onClick={() => issueToken(row)}>
              Enrollment Token
            </Button>
          )}
          <Button size="sm" variant="secondary" onClick={() => navigate(`/app/tally/agents/${row.id}`)}>
            Manage
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="tly-page">
      <div className="tly-header">
        <div>
          <h1 className="tly-title"><Cpu size={20} /> Tally Agents</h1>
          <p className="tly-subtitle">
            Windows Client Agents relaying TallyPrime data from machines this server can't reach directly —
            a second transport alongside direct-HTTP TallyPrime connections, for customers behind NAT or a firewall.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="secondary" icon={<RefreshCw size={14} />} onClick={load}>Refresh</Button>
          <Button icon={<Plus size={14} />} onClick={openCreate}>New Agent</Button>
        </div>
      </div>

      <div className="tly-list-table-panel">
        <DataTable
          columns={columns}
          data={installations}
          loading={loading}
          error={error}
          emptyState={
            <EmptyState
              icon={<Cpu size={28} />}
              title="No agent installations yet"
              description="Create one to get a one-time enrollment token for the Windows operator to run 'tally_agent enroll' with."
              action={{ label: 'New Agent', onClick: openCreate }}
            />
          }
        />
      </div>

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title="New Agent Installation" size="sm">
        {createError && <div className="tly-banner-error" style={{ marginBottom: 12 }}>{createError}</div>}
        <div className="tly-build-field" style={{ marginBottom: 12 }}>
          <label>Name</label>
          <input
            className="tly-inline-input" style={{ maxWidth: 'none' }}
            value={createForm.name} placeholder="e.g. Mumbai Warehouse PC"
            onChange={(e) => setCreateForm((f) => ({ ...f, name: e.target.value }))}
          />
        </div>
        <div style={{ display: 'flex', gap: 12 }}>
          <div className="tly-build-field" style={{ marginBottom: 12, flex: 1 }}>
            <label>Tally Host (on the agent's machine)</label>
            <input
              className="tly-inline-input" style={{ maxWidth: 'none' }}
              value={createForm.tallyHost} onChange={(e) => setCreateForm((f) => ({ ...f, tallyHost: e.target.value }))}
            />
          </div>
          <div className="tly-build-field" style={{ marginBottom: 12, width: 120 }}>
            <label>Tally Port</label>
            <input
              className="tly-inline-input" style={{ maxWidth: 'none' }}
              value={createForm.tallyPort} onChange={(e) => setCreateForm((f) => ({ ...f, tallyPort: e.target.value.replace(/[^0-9]/g, '') }))}
            />
          </div>
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="secondary" onClick={() => setCreateOpen(false)}>Cancel</Button>
          <Button loading={creating} disabled={!createForm.name.trim()} onClick={handleCreate}>Create &amp; Issue Token</Button>
        </div>
      </Modal>

      <Modal
        open={!!tokenResult}
        onClose={() => setTokenResult(null)}
        title={tokenResult ? `Enrollment Token — ${tokenResult.installation.name}` : ''}
        size="md"
      >
        {tokenError ? (
          <p className="tly-text-danger">{tokenError}</p>
        ) : !tokenResult?.token ? (
          <LoadingSpinner />
        ) : (
          <>
            <p className="tly-text-muted" style={{ marginBottom: 10 }}>
              Shown once — it will not be retrievable again. Run this on the Windows machine (installing the agent
              first if needed):
            </p>
            <div className="tly-xml-block" style={{ marginBottom: 10 }}>
              tally_agent enroll --server-url &lt;this server's URL&gt; --token {tokenResult.token}
            </div>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
              <span className="tly-text-muted">Expires {formatDateTime(tokenResult.expiresAt)}</span>
              <Button size="sm" variant="secondary" icon={<Copy size={14} />} onClick={copyToken}>Copy Token</Button>
            </div>
          </>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <Button variant="secondary" onClick={() => setTokenResult(null)}>Close</Button>
        </div>
      </Modal>
    </div>
  );
}
