/**
 * Active Sessions — /app/admin/sessions
 * Real, DB-backed view of who's currently signed in to the org (password
 * or SSO — see backend app/models/user_session.py) plus the seat usage
 * that gates new logins (Organization.max_users, enforced in
 * auth_service.issue_tokens). An Admin can revoke a session here to free
 * a seat immediately instead of waiting for it to expire.
 */
import { useCallback, useEffect, useState } from 'react';
import { RefreshCcw, Monitor, LogOut, Laptop } from 'lucide-react';
import { Button, StatusBadge, DataTable, EmptyState, PermissionGate } from '@/components/common/index.js';
import { SESSION_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { useAuth } from '@/hooks/useAuth.js';
import { sessionApi } from '@/services/api/sessionApi.js';
import './sessions.css';

const METHOD_LABEL = { PASSWORD: 'Password', SSO: 'SSO' };

export default function ActiveSessions() {
  const { user: me } = useAuth();

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [revokingId, setRevokingId] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    sessionApi.list()
      .then(setData)
      .catch(() => setError('Failed to load active sessions. Please try again.'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const runRevoke = async (row) => {
    if (!window.confirm(`Sign out ${row.user_full_name}'s session on this device? This frees the seat immediately.`)) return;
    setRevokingId(row.id);
    try {
      await sessionApi.revoke(row.id);
      load();
    } catch (err) {
      window.alert(err?.message ?? 'Could not revoke this session.');
    } finally {
      setRevokingId(null);
    }
  };

  const unlimited = data && data.max_users <= 0;
  const seatsUsed = data?.seats_used ?? 0;
  const maxUsers = data?.max_users ?? 0;
  const nearLimit = !unlimited && maxUsers > 0 && seatsUsed >= maxUsers;

  const columns = [
    {
      key: 'user_full_name', header: 'User',
      render: (v, row) => (
        <div className="sess-user-cell">
          <div className="sess-avatar">{(v || '?').slice(0, 1).toUpperCase()}</div>
          <div>
            <div className="sess-user-name">{v}{row.user_id === me?.id ? ' (you)' : ''}</div>
            <div className="sess-user-email">{row.user_email}</div>
          </div>
        </div>
      ),
    },
    { key: 'login_method', header: 'Method', render: (v) => <StatusBadge variant={v === 'SSO' ? 'info' : 'gray'} dot>{METHOD_LABEL[v] ?? v}</StatusBadge> },
    { key: 'ip_address', header: 'IP Address', render: (v) => v || '—' },
    { key: 'user_agent', header: 'Device / Browser', render: (v) => <span className="sess-ua" title={v || ''}>{v || '—'}</span> },
    { key: 'created_at', header: 'Signed In', sortable: true, render: (v) => formatDateTime(v) },
    { key: 'last_seen_at', header: 'Last Active', sortable: true, render: (v) => formatDateTime(v) },
    { key: 'expires_at', header: 'Expires', render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <PermissionGate {...SESSION_PERMISSIONS.SESSION_REVOKE} fallback={null}>
          <Button
            variant="ghost" size="sm" icon={<LogOut size={13} />}
            loading={revokingId === row.id}
            onClick={() => runRevoke(row)}
          >
            {row.is_current ? 'Sign out (this session)' : 'Sign out'}
          </Button>
        </PermissionGate>
      ),
    },
  ];

  return (
    <PermissionGate {...SESSION_PERMISSIONS.SESSION_VIEW} showReason>
      <div className="sess-page">
        <div className="sess-header">
          <div>
            <h1 className="sess-title">Active Sessions</h1>
            <p className="sess-subtitle">Everyone currently signed in to your organization — password or SSO.</p>
          </div>
          <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={load}>Refresh</Button>
        </div>

        <div className="sess-kpis">
          <div className="sess-kpi-card">
            <div className="sess-kpi-val">{loading ? '—' : seatsUsed}</div>
            <div className="sess-kpi-label">Seats in use</div>
          </div>
          <div className={`sess-kpi-card ${nearLimit ? 'sess-kpi-card-warning' : ''}`}>
            <div className="sess-kpi-val">{loading ? '—' : (unlimited ? 'Unlimited' : maxUsers)}</div>
            <div className="sess-kpi-label">Plan seat limit</div>
          </div>
          <div className="sess-kpi-card">
            <div className="sess-kpi-val">{loading ? '—' : (data?.total ?? 0)}</div>
            <div className="sess-kpi-label">Active session rows</div>
          </div>
        </div>

        {nearLimit && (
          <div className="sess-limit-banner">
            <Monitor size={14} /> All {maxUsers} seat{maxUsers !== 1 ? 's' : ''} on your plan are in use. New users can't sign in
            until someone is signed out below, or the plan is upgraded.
          </div>
        )}

        <div className="sess-table-panel">
          <DataTable
            columns={columns}
            data={data?.items ?? []}
            loading={loading}
            error={error}
            emptyState={
              <EmptyState
                icon={<Laptop size={28} />}
                title="No active sessions"
                description="Nobody in your organization is currently signed in."
              />
            }
          />
        </div>
      </div>
    </PermissionGate>
  );
}
