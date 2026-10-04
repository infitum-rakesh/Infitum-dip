/**
 * TallyCompanies — /app/tally/companies (TALLY-A, Phase A scope only).
 *
 * Lists saved TALLY_PRIME connections (generic connectionApi — Tally has
 * no dedicated create/edit/test route, see api/tally.py's docstring) and
 * lets the user run real company discovery against each one
 * (tallyApi.discoverCompanies -> real TDL "List of Companies" request).
 * No caching: every click re-queries Tally live, per this integration's
 * no-fake-success discipline — a stale company list is never shown as
 * current.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Calculator, RefreshCw, ExternalLink } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import tallyApi from '@/services/api/tallyApi.js';
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

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    connectionApi.list({ connection_type: 'TALLY_PRIME', page_size: 200 })
      .then((r) => { setConnections(r.items); setLoading(false); })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load TallyPrime connections.')); setLoading(false); });
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
    { key: 'host_display', header: 'Host', render: (v) => v || '—' },
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
        <Button size="sm" variant="secondary" icon={<RefreshCw size={14} />} loading={discovering === row.id} onClick={() => handleDiscover(row)}>
          Discover Companies
        </Button>
      ),
    },
  ];

  return (
    <div className="tly-page">
      <div className="tly-header">
        <div>
          <h1 className="tly-title"><Calculator size={20} /> Tally Integration</h1>
          <p className="tly-subtitle">
            TallyPrime connections and real company discovery. Read-only for now — extraction, reconciliation
            and write-back land in later phases.
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
              description="Create a TallyPrime connection from Connections to get started — host, port, and the company to target."
              action={{ label: 'Go to Connections', onClick: () => navigate('/app/connections') }}
            />
          }
        />
      </div>
    </div>
  );
}
