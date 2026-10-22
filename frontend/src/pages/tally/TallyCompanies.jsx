/**
 * TallyCompanies — /app/tally/companies (TALLY-A, Phase A scope only).
 *
 * Lists saved Tally connections of EITHER transport — direct-HTTP
 * TALLY_PRIME and Agent Relay TALLY_PRIME_AGENT (see connectors/
 * application/tallyprime_agent.py's module docstring: both transports
 * return identical shapes, so this page treats them identically; the
 * generic /connections list endpoint only filters by one connection_type
 * at a time, so both are fetched and merged here) — and lets the user
 * run real company discovery against each one (tallyApi.discoverCompanies
 * -> real TDL "List of Companies" request, relayed through the agent for
 * TALLY_PRIME_AGENT connections). No caching: every click re-queries
 * Tally live, per this integration's no-fake-success discipline — a
 * stale company list is never shown as current.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Calculator, RefreshCw, ExternalLink, Users } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState, Modal, LoadingSpinner } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import tallyApi from '@/services/api/tallyApi.js';
import tallyAgentApi from '@/services/api/tallyAgentApi.js';
import './tally.css';

function apiErrorMessage(err, fallback) {
  return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback;
}

export default function TallyCompanies() {
  const navigate = useNavigate();
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [discovering, setDiscovering] = useState(null); // connection id in flight
  const [companiesByConn, setCompaniesByConn] = useState({}); // { [id]: { items, error } }

  const [partiesConn, setPartiesConn] = useState(null); // connection object or null (modal open state)
  const [parties, setParties] = useState([]);
  const [partiesLoading, setPartiesLoading] = useState(false);
  const [partiesError, setPartiesError] = useState(null);

  const [agentNameById, setAgentNameById] = useState({}); // resolves TALLY_PRIME_AGENT's raw agent_installation_id host_display to a real name

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    Promise.all([
      connectionApi.list({ connection_type: 'TALLY_PRIME', page_size: 200 }),
      connectionApi.list({ connection_type: 'TALLY_PRIME_AGENT', page_size: 200 }),
    ])
      .then(([direct, viaAgent]) => {
        setConnections([...direct.items, ...viaAgent.items]);
        setLoading(false);
      })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load TallyPrime connections.')); setLoading(false); });
    tallyAgentApi.listInstallations()
      .then((r) => setAgentNameById(Object.fromEntries((r.items ?? []).map((a) => [String(a.id), a.name]))))
      .catch(() => setAgentNameById({}));
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleDiscover = async (conn) => {
    setDiscovering(conn.id);
    setCompaniesByConn((prev) => ({ ...prev, [conn.id]: { ...(prev[conn.id] || {}), error: null } }));
    try {
      const result = await tallyApi.discoverCompanies(conn.id);
      setCompaniesByConn((prev) => ({ ...prev, [conn.id]: { items: result.items, error: null } }));
    } catch (err) {
      setCompaniesByConn((prev) => ({
        ...prev,
        [conn.id]: { items: prev[conn.id]?.items || [], error: apiErrorMessage(err, 'Discovery failed.') },
      }));
    } finally {
      setDiscovering(null);
    }
  };

  const openParties = async (conn) => {
    setPartiesConn(conn);
    setPartiesLoading(true);
    setPartiesError(null);
    try {
      const result = await tallyApi.listCanonicalParties(conn.id, { page_size: 500 });
      setParties(result.items);
    } catch (err) {
      setPartiesError(apiErrorMessage(err, 'Could not load party masters.'));
      setParties([]);
    } finally {
      setPartiesLoading(false);
    }
  };

  const partyColumns = [
    { key: 'name', header: 'Party Name', sortable: true },
    {
      key: 'party_type', header: 'Type',
      render: (v, row) => <StatusBadge variant={row.parent === 'Sundry Debtors' ? 'info' : 'gray'}>{row.parent || v || '—'}</StatusBadge>,
    },
    { key: 'gstin', header: 'GSTIN', render: (v) => v || '—' },
    { key: 'state', header: 'State', render: (v) => v || '—' },
    { key: 'opening_balance', header: 'Opening Balance', render: (v) => v != null ? `₹${v.toLocaleString('en-IN')}` : '—' },
    { key: 'closing_balance', header: 'Closing Balance', render: (v) => v != null ? `₹${v.toLocaleString('en-IN')}` : '—' },
  ];

  const columns = [
    { key: 'name', header: 'Connection', sortable: true },
    {
      key: 'status', header: 'Status', sortable: true,
      render: (_v, row) => (
        <StatusBadge variant={row.status === 'ACTIVE' ? 'success' : row.status === 'ERROR' ? 'danger' : 'gray'}>
          {row.status}
        </StatusBadge>
      ),
    },
    {
      key: 'host_display', header: 'Host / Agent',
      // TALLY_PRIME_AGENT's host_display is the raw agent_installation_id
      // (connection_service.py's to_out() has no DB session to resolve a
      // name with — see its _HOST_FIELD comment); resolved to the real
      // agent name here from the Tally Agents list fetched in load().
      render: (v, row) => row.connection_type === 'TALLY_PRIME_AGENT' ? (agentNameById[v] || `Agent #${v}`) : (v || '—'),
    },
    {
      key: 'last_tested_at', header: 'Last Tested',
      render: (v) => (v ? formatDateTime(v) : 'Never'),
    },
    {
      key: 'companies', header: 'Companies',
      render: (_v, row) => {
        const entry = companiesByConn[row.id];
        if (discovering === row.id) return <span className="tly-text-muted">Discovering…</span>;
        if (entry?.error) return <span className="tly-text-danger">{entry.error}</span>;
        if (!entry?.items) return <span className="tly-text-muted">Not discovered yet</span>;
        if (entry.items.length === 0) return <span className="tly-text-muted">No companies found</span>;
        return (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {entry.items.map((c) => (
              <StatusBadge key={c.name} variant={c.is_default ? 'info' : 'gray'}>
                {c.name}{c.is_default ? ' (configured)' : ''}
              </StatusBadge>
            ))}
          </div>
        );
      },
    },
    {
      key: 'actions', header: '',
      render: (_v, row) => (
        <div style={{ display: 'flex', gap: 6 }}>
          <Button size="sm" variant="secondary" icon={<RefreshCw size={14} />} loading={discovering === row.id} onClick={() => handleDiscover(row)}>
            Discover Companies
          </Button>
          <Button size="sm" variant="secondary" icon={<Users size={14} />} onClick={() => openParties(row)}>
            View Parties
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="tly-page">
      <div className="tly-header">
        <div>
          <h1 className="tly-title"><Calculator size={20} /> Tally Integration</h1>
          <p className="tly-subtitle">
            TallyPrime connections — direct-HTTP and via a Windows Agent — and real company discovery.
          </p>
        </div>
        <Button variant="secondary" icon={<ExternalLink size={14} />} onClick={() => navigate('/app/connections')}>
          Manage Connections
        </Button>
      </div>

      <div className="tly-list-table-panel">
        <DataTable
          columns={columns}
          data={connections}
          loading={loading}
          error={error}
          emptyState={
            <EmptyState
              icon={<Calculator size={28} />}
              title="No TallyPrime connections yet"
              description="Create a TallyPrime or TallyPrime (via Agent) connection from Connections to get started."
              action={{ label: 'Go to Connections', onClick: () => navigate('/app/connections') }}
            />
          }
        />
      </div>

      <Modal
        open={!!partiesConn}
        onClose={() => setPartiesConn(null)}
        title={partiesConn ? `Party Master — ${partiesConn.name}` : ''}
        size="xl"
      >
        {partiesLoading ? (
          <LoadingSpinner />
        ) : partiesError ? (
          <p className="tly-text-danger">{partiesError}</p>
        ) : parties.length === 0 ? (
          <p className="tly-text-muted">
            No party master data yet — run a PARTY_MASTER extraction for this connection first.
          </p>
        ) : (
          <>
            <p className="tly-text-muted" style={{ marginBottom: 8 }}>
              {parties.length} part{parties.length === 1 ? 'y' : 'ies'} extracted from Tally (Sundry Debtors + Sundry Creditors ledgers).
            </p>
            <DataTable columns={partyColumns} data={parties} loading={false} />
          </>
        )}
      </Modal>
    </div>
  );
}
