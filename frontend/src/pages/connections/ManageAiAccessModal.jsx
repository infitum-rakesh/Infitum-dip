/**
 * ManageAiAccessModal — "organisation-level permissions" + "public/
 * private visibility" + "default AI connection selection" for AI
 * connections (AIC-P5/P6). Lists every AI Provider connection this org
 * can reach (its own + every seller-managed one), lets an org admin
 * toggle is_public on its OWN connections, grant/revoke per-user or
 * per-role access scoped to this org, and set an org-wide or personal
 * default connection. Hits /api/connections/ai-access/* and
 * /api/connections/ai-defaults/* for real — see
 * ai_connection_permission_service.py / ai_connection_resolver_service.py
 * for the authorization + precedence rules surfaced here.
 */
import { useEffect, useState } from 'react';
import { Trash2, ShieldCheck, Globe, Lock, Star, Shield } from 'lucide-react';
import { Modal, Button, StatusBadge, EmptyState } from '@/components/common/index.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import './connections.css';

const GRANT_TABLE_SCROLL_THRESHOLD = 6; // beyond this, one connection's own grants shouldn't dominate the whole modal

function UsageSummary({ usage }) {
  const { max_tokens, token_period, tokens_used, tokens_reserved, ai_rate_limit, requests_last_minute, ai_concurrency_limit, concurrent_in_flight } = usage;
  const periodLabel = (token_period || 'TOTAL').replace('_', ' ').toLowerCase();
  const consumed = tokens_used + tokens_reserved;
  const pct = max_tokens ? Math.min(100, Math.round((consumed / max_tokens) * 100)) : null;
  return (
    <div className="ai-access-usage">
      <div className="ai-access-usage-row">
        <span className="ai-access-usage-label">Tokens used this {periodLabel} period</span>
        <span className="ai-access-usage-value">
          {max_tokens ? `${consumed.toLocaleString()} / ${max_tokens.toLocaleString()}` : `${consumed.toLocaleString()} (no limit set)`}
        </span>
      </div>
      {max_tokens != null && (
        <div className="ai-access-usage-bar">
          <div className="ai-access-usage-bar-fill" style={{ width: `${pct}%`, background: pct >= 90 ? 'var(--color-danger)' : pct >= 70 ? 'var(--color-warning)' : 'var(--color-success)' }} />
        </div>
      )}
      <div className="ai-access-usage-row">
        <span className="ai-access-usage-label">Rate limit</span>
        <span className="ai-access-usage-value">{ai_rate_limit != null ? `${requests_last_minute} / ${ai_rate_limit} requests per minute` : 'No limit set'}</span>
      </div>
      <div className="ai-access-usage-row">
        <span className="ai-access-usage-label">Concurrent requests</span>
        <span className="ai-access-usage-value">{ai_concurrency_limit != null ? `${concurrent_in_flight} / ${ai_concurrency_limit} in flight` : 'No limit set'}</span>
      </div>
    </div>
  );
}

export function ManageAiAccessModal({ open, onClose }) {
  const [items, setItems] = useState(null);
  const [grantees, setGrantees] = useState(null);
  const [defaults, setDefaults] = useState(null);
  const [error, setError] = useState('');
  const [busyConn, setBusyConn] = useState(null);
  const [grantForm, setGrantForm] = useState({}); // connectionId -> { type, id, can_view, can_use }
  // Per-card errors (a failed grant/toggle on ONE connection) are kept
  // separate from `error` (reserved for the initial load failing) so the
  // message shows right next to the control that caused it instead of in
  // a single banner far above a long scrolled list of connections.
  const [cardErrors, setCardErrors] = useState({}); // connectionId -> message
  const setCardError = (connId, message) => setCardErrors((e) => ({ ...e, [connId]: message }));

  const load = () => {
    setError('');
    Promise.all([connectionApi.listAiAccess(), connectionApi.getAiAccessGrantees(), connectionApi.getAiDefaults()])
      .then(([access, opts, defs]) => { setItems(access.items); setGrantees(opts); setDefaults(defs); })
      .catch((err) => setError(err?.message ?? 'Could not load AI connection access.'));
  };

  useEffect(() => { if (open) load(); else { setItems(null); setGrantees(null); setDefaults(null); setCardErrors({}); } }, [open]);

  const setMyDefault = async (item) => {
    setBusyConn(item.connection_id);
    setCardError(item.connection_id, '');
    try {
      const isMine = defaults?.user_default_connection_id === item.connection_id;
      setDefaults(isMine ? await connectionApi.clearMyAiDefault() : await connectionApi.setMyAiDefault(item.connection_id));
    } catch (err) {
      setCardError(item.connection_id, err?.message ?? 'Could not update your default AI connection.');
    } finally {
      setBusyConn(null);
    }
  };

  const setOrgDefault = async (item) => {
    setBusyConn(item.connection_id);
    setCardError(item.connection_id, '');
    try {
      const isOrgDefault = defaults?.org_default_connection_id === item.connection_id;
      setDefaults(isOrgDefault ? await connectionApi.clearOrgAiDefault() : await connectionApi.setOrgAiDefault(item.connection_id));
    } catch (err) {
      setCardError(item.connection_id, err?.message ?? 'Could not update the organization default AI connection.');
    } finally {
      setBusyConn(null);
    }
  };

  const togglePublic = async (item) => {
    setBusyConn(item.connection_id);
    setCardError(item.connection_id, '');
    try {
      const result = await connectionApi.setAiAccessPublic(item.connection_id, !item.is_public);
      setItems(result.items);
    } catch (err) {
      setCardError(item.connection_id, err?.message ?? 'Could not update public visibility.');
    } finally {
      setBusyConn(null);
    }
  };

  const formFor = (connId) => grantForm[connId] ?? { type: 'USER', id: '', can_view: true, can_use: true };
  const setFormFor = (connId, patch) => setGrantForm((f) => ({ ...f, [connId]: { ...formFor(connId), ...patch } }));

  const addGrant = async (item) => {
    const form = formFor(item.connection_id);
    if (!form.id) return;
    setBusyConn(item.connection_id);
    setCardError(item.connection_id, '');
    try {
      await connectionApi.createAiAccessGrant(item.connection_id, {
        grantee_type: form.type, grantee_id: Number(form.id), can_view: form.can_view, can_use: form.can_use,
      });
      setFormFor(item.connection_id, { id: '' });
      load();
    } catch (err) {
      setCardError(item.connection_id, err?.message ?? 'Could not add this grant.');
    } finally {
      setBusyConn(null);
    }
  };

  const removeGrant = async (item, grant) => {
    setBusyConn(item.connection_id);
    setCardError(item.connection_id, '');
    try {
      await connectionApi.deleteAiAccessGrant(item.connection_id, grant.id);
      load();
    } catch (err) {
      setCardError(item.connection_id, err?.message ?? 'Could not remove this grant.');
    } finally {
      setBusyConn(null);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Manage AI Connection Access" size="xl" footer={<Button variant="secondary" onClick={onClose}>Close</Button>}>
      {error && <div className="conn-form-error">{error}</div>}
      {!items ? (
        <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>Loading…</div>
      ) : items.length === 0 ? (
        <EmptyState
          icon={<Shield size={28} />}
          title="No AI connections yet"
          description="Add an AI Provider connection on the Connections page, or ask your Seller to grant one, to manage access here."
        />
      ) : (
        <div className="ai-access-list">
          {items.map((item) => {
            const options = grantees ? (formFor(item.connection_id).type === 'USER' ? grantees.users : grantees.roles) : [];
            return (
              <div key={item.connection_id} className="ai-access-card">
                <div className="ai-access-card-head">
                  <div>
                    <strong>{item.name}</strong>{' '}
                    <span className="conn-category-tag">{item.connection_type}</span>{' '}
                    {item.is_seller_managed && <StatusBadge variant="info">Seller-managed</StatusBadge>}
                    {defaults?.effective_connection_id === item.connection_id && <StatusBadge variant="success">Effective default</StatusBadge>}
                  </div>
                  <div className="ai-access-public-toggle">
                    <button
                      className={`ai-access-default-btn ${defaults?.org_default_connection_id === item.connection_id ? 'on' : ''}`}
                      disabled={busyConn === item.connection_id}
                      onClick={() => setOrgDefault(item)}
                      title="The organization-wide default — used when a feature needs one AI connection and the user has no personal default set"
                    >
                      <Star size={13} /> Org Default
                    </button>
                    <button
                      className={`ai-access-default-btn ${defaults?.user_default_connection_id === item.connection_id ? 'on' : ''}`}
                      disabled={busyConn === item.connection_id}
                      onClick={() => setMyDefault(item)}
                      title="Your own personal default — takes priority over the organization default for you specifically"
                    >
                      <Star size={13} /> My Default
                    </button>
                    {item.can_toggle_public ? (
                      <button
                        className={`ai-access-public-btn ${item.is_public ? 'on' : 'off'}`}
                        disabled={busyConn === item.connection_id}
                        onClick={() => togglePublic(item)}
                        title="Public connections are visible/usable to every user in your organization without an individual grant"
                      >
                        {item.is_public ? <Globe size={13} /> : <Lock size={13} />} {item.is_public ? 'Public to your org' : 'Private'}
                      </button>
                    ) : (
                      <span className="ai-access-public-readonly" title="Visibility for seller-managed connections is set at the platform level">
                        {item.is_public ? <Globe size={13} /> : <Lock size={13} />} {item.is_public ? 'Public (platform-wide)' : 'Private (platform-wide)'}
                      </span>
                    )}
                  </div>
                </div>

                {item.usage && <UsageSummary usage={item.usage} />}

                <div className={item.grants.length > GRANT_TABLE_SCROLL_THRESHOLD ? 'ai-access-grant-table-scroll' : undefined}>
                  <table className="ai-access-grant-table">
                    <thead><tr><th>Grantee</th><th>View</th><th>Use</th><th /></tr></thead>
                    <tbody>
                      {item.grants.length === 0 && <tr><td colSpan={4} className="ai-access-empty-row">No individual grants — {item.is_public ? 'not needed, this connection is public to your org.' : 'only users with an explicit grant below (or an org admin) can use this connection.'}</td></tr>}
                      {item.grants.map((g) => (
                        <tr key={g.id}>
                          <td>{g.grantee_type === 'USER' ? 'User' : 'Role'}: {g.grantee_label}</td>
                          <td>{g.can_view ? <ShieldCheck size={13} color="var(--color-success)" /> : '—'}</td>
                          <td>{g.can_use ? <ShieldCheck size={13} color="var(--color-success)" /> : '—'}</td>
                          <td><button className="ai-access-remove-btn" disabled={busyConn === item.connection_id} onClick={() => removeGrant(item, g)}><Trash2 size={13} /></button></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {cardErrors[item.connection_id] && (
                  <div className="conn-form-error ai-access-card-error">{cardErrors[item.connection_id]}</div>
                )}

                <div className="ai-access-add-grant">
                  <select className="conn-input" value={formFor(item.connection_id).type} onChange={(e) => setFormFor(item.connection_id, { type: e.target.value, id: '' })}>
                    <option value="USER">User</option>
                    <option value="ROLE">Role</option>
                  </select>
                  <select className="conn-input" value={formFor(item.connection_id).id} onChange={(e) => setFormFor(item.connection_id, { id: e.target.value })}>
                    <option value="">Select…</option>
                    {options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                  </select>
                  <label className="ai-access-checkbox"><input type="checkbox" checked={formFor(item.connection_id).can_view} onChange={(e) => setFormFor(item.connection_id, { can_view: e.target.checked })} /> View</label>
                  <label className="ai-access-checkbox"><input type="checkbox" checked={formFor(item.connection_id).can_use} onChange={(e) => setFormFor(item.connection_id, { can_use: e.target.checked })} /> Use</label>
                  <Button variant="secondary" size="sm" disabled={!formFor(item.connection_id).id || busyConn === item.connection_id} onClick={() => addGrant(item)}>Add Grant</Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Modal>
  );
}
