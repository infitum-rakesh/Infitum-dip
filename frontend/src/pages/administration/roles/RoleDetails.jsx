/**
 * RoleDetails — /app/admin/roles/:roleId
 * Users / Permissions / Modules / Audit History.
 * Reuses userAdminApi (Administration → Users) to look up organization
 * users for assignment — no duplicate user-listing logic.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft, Power, Trash2, ShieldCheck, Users as UsersIcon, Lock,
  Blocks, History, Save, UserPlus, X,
} from 'lucide-react';
import { Button, StatusBadge, EmptyState, PermissionGate, FullPageSpinner } from '@/components/common/index.js';
import { ROLE_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime, initials } from '@/utils/formatters.js';
import { roleApi } from '@/services/api/roleApi.js';
import { userAdminApi } from '@/services/api/userAdminApi.js';
import { PermissionMatrix, cellsToSet, setToCells } from './PermissionMatrix.jsx';
import './roles.css';

const STATUS_VARIANT = { ACTIVE: 'success', INACTIVE: 'gray' };
const USER_STATUS_VARIANT = { ACTIVE: 'success', INACTIVE: 'gray', PENDING: 'warning' };
const EVENT_LABEL = {
  CREATED: 'Role created', UPDATED: 'Role updated', PERMISSIONS_CHANGED: 'Permissions changed',
  STATUS_CHANGED: 'Status changed', USER_ASSIGNED: 'User assigned', USER_UNASSIGNED: 'User unassigned',
  DELETED: 'Role deleted',
};

const TABS = [
  { id: 'users', label: 'Users', icon: UsersIcon },
  { id: 'permissions', label: 'Permissions', icon: Lock },
  { id: 'modules', label: 'Modules', icon: Blocks },
  { id: 'audit', label: 'Audit History', icon: History },
];

export default function RoleDetails() {
  const { roleId } = useParams();
  const navigate = useNavigate();

  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState('users');

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    roleApi.get(roleId).then(setDetail).catch(() => setError('Role not found or failed to load.')).finally(() => setLoading(false));
  }, [roleId]);

  useEffect(() => { load(); }, [load]);

  const runToggleStatus = async () => {
    await roleApi.update(roleId, { status: detail.status === 'ACTIVE' ? 'INACTIVE' : 'ACTIVE' });
    load();
  };
  const runDelete = async () => {
    if (!window.confirm(`Delete role "${detail.name}"? This cannot be undone.`)) return;
    try { await roleApi.delete(roleId); navigate('/app/admin/roles'); }
    catch (err) { window.alert(err?.message ?? 'Could not delete role.'); }
  };

  if (loading) return <FullPageSpinner />;
  if (error || !detail) {
    return (
      <div className="rol-page">
        <EmptyState icon={<ShieldCheck size={28} />} title="Role not found" description={error ?? `No role matches "${roleId}".`}
          action={{ label: 'Back to Roles', onClick: () => navigate('/app/admin/roles') }} />
      </div>
    );
  }

  return (
    <PermissionGate {...ROLE_PERMISSIONS.ROLE_VIEW} showReason>
      <div className="rol-page">
        <button className="rold-back-btn" onClick={() => navigate('/app/admin/roles')}>
          <ArrowLeft size={14} /> Roles
        </button>

        {/* Header */}
        <div className="rold-header">
          <div>
            <div className="rold-title-row">
              <h1 className="rold-title">{detail.name}</h1>
              {detail.is_system && <span className="rol-system-badge">Starter</span>}
              <StatusBadge variant={STATUS_VARIANT[detail.status] ?? 'gray'} dot>{detail.status.charAt(0) + detail.status.slice(1).toLowerCase()}</StatusBadge>
            </div>
            <p className="rold-subtitle">{detail.description || 'No description'} · {detail.user_count} user{detail.user_count === 1 ? '' : 's'} · {detail.permission_count} permission{detail.permission_count === 1 ? '' : 's'}</p>
          </div>

          <div className="rold-actions">
            <PermissionGate {...ROLE_PERMISSIONS.ROLE_EDIT} fallback={null}>
              <Button variant="secondary" size="sm" icon={<Power size={13} />} onClick={runToggleStatus}>
                {detail.status === 'ACTIVE' ? 'Deactivate' : 'Activate'}
              </Button>
            </PermissionGate>
            <PermissionGate {...ROLE_PERMISSIONS.ROLE_DELETE} fallback={null}>
              <Button
                variant="danger" size="sm" icon={<Trash2 size={13} />}
                disabled={detail.is_system || detail.user_count > 0}
                title={detail.is_system ? 'Starter roles cannot be deleted' : detail.user_count > 0 ? 'Unassign all users first' : undefined}
                onClick={runDelete}
              >
                Delete
              </Button>
            </PermissionGate>
          </div>
        </div>

        {/* Tabs */}
        <div className="rold-tab-nav">
          {TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button key={t.id} className={`rold-tab-btn ${tab === t.id ? 'active' : ''}`} onClick={() => setTab(t.id)}>
                <Icon size={14} /> {t.label}
              </button>
            );
          })}
        </div>

        {tab === 'users' && <UsersTab detail={detail} onChanged={load} />}
        {tab === 'permissions' && <PermissionsTab detail={detail} onSaved={load} />}
        {tab === 'modules' && <ModulesTab detail={detail} />}
        {tab === 'audit' && <AuditTab roleId={roleId} />}
      </div>
    </PermissionGate>
  );
}

// ── Users tab ────────────────────────────────────────────────────

function UsersTab({ detail, onChanged }) {
  const [candidates, setCandidates] = useState([]);
  const [pickId, setPickId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    userAdminApi.list({ page_size: 200 }).then((r) => {
      const assigned = new Set(detail.users.map((u) => u.id));
      setCandidates(r.items.filter((u) => !assigned.has(u.id)));
    }).catch(() => {});
  }, [detail]);

  const addUser = async () => {
    if (!pickId) return;
    setBusy(true);
    setError('');
    try { await roleApi.assignUser(detail.id, Number(pickId)); setPickId(''); onChanged(); }
    catch (err) { setError(err?.message ?? 'Could not assign user.'); }
    finally { setBusy(false); }
  };

  const removeUser = async (userId) => {
    setBusy(true);
    try { await roleApi.unassignUser(detail.id, userId); onChanged(); }
    finally { setBusy(false); }
  };

  return (
    <div className="rold-card">
      <div className="rold-card-title">Users in this Role</div>
      {error && <div className="rol-form-error">{error}</div>}
      {detail.users.length === 0 ? (
        <p className="rol-empty-inline">No users assigned to this role yet.</p>
      ) : (
        detail.users.map((u) => (
          <div key={u.id} className="rold-user-row">
            <div className="rold-user-avatar">{initials(u.full_name)}</div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="rold-user-name">{u.full_name}</div>
              <div className="rold-user-email">{u.email}{u.department ? ` · ${u.department}` : ''}</div>
            </div>
            <StatusBadge variant={USER_STATUS_VARIANT[u.status] ?? 'gray'} dot size="sm">{u.status === 'PENDING' ? 'Pending' : u.status.charAt(0) + u.status.slice(1).toLowerCase()}</StatusBadge>
            <PermissionGate {...ROLE_PERMISSIONS.ROLE_ASSIGN} fallback={null}>
              <button className="rol-icon-btn" title="Remove from role" disabled={busy} onClick={() => removeUser(u.id)}><X size={14} /></button>
            </PermissionGate>
          </div>
        ))
      )}

      <PermissionGate {...ROLE_PERMISSIONS.ROLE_ASSIGN} fallback={null}>
        <div className="rold-add-user-row">
          <select className="rol-input" style={{ flex: 1 }} value={pickId} onChange={(e) => setPickId(e.target.value)}>
            <option value="">Select a user to add…</option>
            {candidates.map((u) => <option key={u.id} value={u.id}>{u.full_name} ({u.email})</option>)}
          </select>
          <Button variant="secondary" size="sm" icon={<UserPlus size={13} />} loading={busy} disabled={!pickId} onClick={addUser}>Add</Button>
        </div>
      </PermissionGate>
    </div>
  );
}

// ── Permissions tab ──────────────────────────────────────────────

function PermissionsTab({ detail, onSaved }) {
  const [value, setValue] = useState(() => cellsToSet(detail.permissions));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => { setValue(cellsToSet(detail.permissions)); }, [detail]);

  const dirty = JSON.stringify([...value].sort()) !== JSON.stringify([...cellsToSet(detail.permissions)].sort());

  const save = async () => {
    setSaving(true);
    setError('');
    try { await roleApi.update(detail.id, { permissions: setToCells(value) }); onSaved(); }
    catch (err) { setError(err?.message ?? 'Could not save permissions.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="rold-card">
      <div className="rold-card-title">Permissions</div>
      {error && <div className="rol-form-error">{error}</div>}
      <p className="rol-hint">Click a category name to toggle every action for that row. Grayed-out categories aren't licensed for your organization — permissions here can never exceed the organization's licensed modules.</p>
      <PermissionMatrix
        categories={detail.categories}
        licensedCategories={detail.licensed_categories}
        categoryActions={detail.category_actions}
        value={value}
        onChange={setValue}
      />
      <PermissionGate {...ROLE_PERMISSIONS.ROLE_EDIT} fallback={null}>
        <div style={{ marginTop: 16 }}>
          <Button variant="primary" size="sm" icon={<Save size={13} />} loading={saving} disabled={!dirty} onClick={save}>Save Permissions</Button>
        </div>
      </PermissionGate>
    </div>
  );
}

// ── Modules tab ──────────────────────────────────────────────────

function ModulesTab({ detail }) {
  const categoriesWithGrants = [...new Set(detail.permissions.map((p) => p.category))].sort();
  return (
    <div className="rold-card">
      <div className="rold-card-title">Modules Covered by this Role</div>
      {categoriesWithGrants.length === 0 ? (
        <p className="rol-empty-inline">This role doesn't grant access to any module yet.</p>
      ) : (
        <div className="rold-module-chip-list">
          {categoriesWithGrants.map((cat) => {
            const actions = detail.permissions.filter((p) => p.category === cat).map((p) => p.action);
            return (
              <span key={cat} className="rold-module-chip" title={actions.join(', ')}>
                {cat} <span className="rol-count-pill">{actions.length}</span>
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Audit History tab ─────────────────────────────────────────────

function AuditTab({ roleId }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    roleApi.getAudit(roleId).then((r) => setItems(r.items)).catch(() => setError('Failed to load audit history.'));
  }, [roleId]);

  return (
    <div className="rold-card">
      <div className="rold-card-title">Audit History</div>
      {items == null ? (
        <p className="rol-empty-inline">Loading…</p>
      ) : error ? (
        <p className="rol-empty-inline">{error}</p>
      ) : items.length === 0 ? (
        <p className="rol-empty-inline">No history recorded yet.</p>
      ) : (
        <div className="rold-feed">
          {items.map((ev) => (
            <div key={ev.id} className="rold-feed-item">
              <div className="rold-feed-dot" />
              <div className="rold-feed-body">
                <div className="rold-feed-type">{EVENT_LABEL[ev.event_type] ?? ev.event_type}</div>
                {ev.message && <div className="rold-feed-msg">{ev.message}</div>}
                <div className="rold-feed-meta">{ev.actor_name ? `By ${ev.actor_name} · ` : 'System · '}{formatDateTime(ev.created_at)}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
