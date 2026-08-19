/**
 * UserDetails — /app/admin/users/:userId
 * Profile / Roles / Permissions / Module Access / Activity / Login History.
 * Reuses utils/permissions.js (ROLE_DEFAULT_ACTIONS, ROLE_RANK) and
 * MODULE_META instead of duplicating role/module knowledge here.
 */
import { useCallback, useContext, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft, Pencil, Power, PowerOff, KeyRound, ShieldCheck, Trash2,
  User as UserIcon, Shield, Lock, Blocks, Activity as ActivityIcon, History, Save,
} from 'lucide-react';
import { Button, StatusBadge, EmptyState, PermissionGate, FullPageSpinner } from '@/components/common/index.js';
import { USER_PERMISSIONS } from '@/utils/constants.js';
import { ROLE_DEFAULT_ACTIONS, ROLE_RANK, MODULE_META } from '@/utils/permissions.js';
import { formatDateTime, initials } from '@/utils/formatters.js';
import { useAuth } from '@/hooks/useAuth.js';
import { OrganizationContext } from '@/context/OrganizationContext.jsx';
import { userAdminApi } from '@/services/api/userAdminApi.js';
import { ChangeRoleModal, ResetPasswordModal } from './UserActionModals.jsx';
import './users.css';

const STATUS_VARIANT = { ACTIVE: 'success', INACTIVE: 'gray', PENDING: 'warning' };
const ROLE_LABEL = {
  super_admin: 'Super Admin', seller: 'Seller', org_admin: 'Org Admin',
  data_engineer: 'Data Engineer', analyst: 'Analyst', viewer: 'Viewer',
};
const EVENT_LABEL = {
  LOGIN: 'Signed in', LOGIN_FAILED: 'Failed sign-in attempt', INVITED: 'Invited',
  UPDATED: 'Profile updated', ROLE_CHANGED: 'Role changed', DISABLED: 'Account disabled',
  ENABLED: 'Account enabled', PASSWORD_RESET: 'Password reset', DELETED: 'Account deleted',
};

const TABS = [
  { id: 'profile', label: 'Profile', icon: UserIcon },
  { id: 'roles', label: 'Roles', icon: Shield },
  { id: 'permissions', label: 'Permissions', icon: Lock },
  { id: 'modules', label: 'Module Access', icon: Blocks },
  { id: 'activity', label: 'Activity', icon: ActivityIcon },
  { id: 'logins', label: 'Login History', icon: History },
];

export default function UserDetails() {
  const { userId } = useParams();
  const navigate = useNavigate();
  const { user: me } = useAuth();
  const { licensedModules } = useContext(OrganizationContext);

  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState('profile');

  const [roleTarget, setRoleTarget] = useState(null);
  const [resetTarget, setResetTarget] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    userAdminApi.get(userId).then(setDetail).catch(() => setError('User not found or failed to load.')).finally(() => setLoading(false));
  }, [userId]);

  useEffect(() => { load(); }, [load]);

  const isSelf = detail?.id === me?.id;

  const runDisable = async () => { await userAdminApi.disable(userId); load(); };
  const runEnable = async () => { await userAdminApi.enable(userId); load(); };
  const runDelete = async () => {
    if (!window.confirm(`Delete "${detail.full_name}"? This cannot be undone.`)) return;
    try { await userAdminApi.delete(userId); navigate('/app/admin/users'); }
    catch (err) { window.alert(err?.message ?? 'Could not delete user.'); }
  };

  if (loading) return <FullPageSpinner />;
  if (error || !detail) {
    return (
      <div className="usr-page">
        <EmptyState icon={<UserIcon size={28} />} title="User not found" description={error ?? `No user matches "${userId}".`}
          action={{ label: 'Back to Users', onClick: () => navigate('/app/admin/users') }} />
      </div>
    );
  }

  return (
    <PermissionGate {...USER_PERMISSIONS.USER_VIEW} showReason>
      <div className="usr-page">
        <button className="usrd-back-btn" onClick={() => navigate('/app/admin/users')}>
          <ArrowLeft size={14} /> Users
        </button>

        {/* Header */}
        <div className="usrd-header">
          <div className="usrd-header-left">
            <div className="usr-avatar usrd-avatar">{initials(detail.full_name)}</div>
            <div>
              <div className="usrd-title-row">
                <h1 className="usrd-title">{detail.full_name}</h1>
                <span className="usr-role-pill">{ROLE_LABEL[detail.role] ?? detail.role}</span>
                <StatusBadge variant={STATUS_VARIANT[detail.status] ?? 'gray'} dot>
                  {detail.status === 'PENDING' ? 'Pending Invitation' : detail.status.charAt(0) + detail.status.slice(1).toLowerCase()}
                </StatusBadge>
              </div>
              <p className="usrd-subtitle">{detail.email} {detail.department ? `· ${detail.department}` : ''}</p>
            </div>
          </div>

          <div className="usrd-actions">
            <PermissionGate {...USER_PERMISSIONS.USER_EDIT} fallback={null}>
              <Button variant="secondary" size="sm" icon={<Pencil size={13} />} onClick={() => setTab('profile')}>Edit</Button>
            </PermissionGate>
            <PermissionGate {...USER_PERMISSIONS.USER_DISABLE} fallback={null}>
              {detail.status === 'INACTIVE'
                ? <Button variant="secondary" size="sm" icon={<Power size={13} />} onClick={runEnable}>Enable</Button>
                : <Button variant="secondary" size="sm" icon={<PowerOff size={13} />} disabled={isSelf} title={isSelf ? "You can't disable your own account" : undefined} onClick={runDisable}>Disable</Button>}
            </PermissionGate>
            <PermissionGate {...USER_PERMISSIONS.USER_EDIT} fallback={null}>
              <Button variant="secondary" size="sm" icon={<KeyRound size={13} />} onClick={() => setResetTarget(detail)}>Reset Password</Button>
            </PermissionGate>
            <PermissionGate {...USER_PERMISSIONS.USER_ASSIGN_ROLE} fallback={null}>
              <Button variant="secondary" size="sm" icon={<ShieldCheck size={13} />} onClick={() => setRoleTarget(detail)}>Change Role</Button>
            </PermissionGate>
            <PermissionGate {...USER_PERMISSIONS.USER_DELETE} fallback={null}>
              <Button variant="danger" size="sm" icon={<Trash2 size={13} />} disabled={isSelf} title={isSelf ? "You can't delete your own account" : undefined} onClick={runDelete}>Delete</Button>
            </PermissionGate>
          </div>
        </div>

        {/* Tabs */}
        <div className="usrd-tab-nav">
          {TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button key={t.id} className={`usrd-tab-btn ${tab === t.id ? 'active' : ''}`} onClick={() => setTab(t.id)}>
                <Icon size={14} /> {t.label}
              </button>
            );
          })}
        </div>

        {tab === 'profile' && <ProfileTab detail={detail} onSaved={load} />}
        {tab === 'roles' && <RolesTab detail={detail} onChangeRole={() => setRoleTarget(detail)} />}
        {tab === 'permissions' && <PermissionsTab detail={detail} />}
        {tab === 'modules' && <ModuleAccessTab detail={detail} licensedModules={licensedModules} onSaved={load} />}
        {tab === 'activity' && <FeedTab loader={() => userAdminApi.getActivity(userId)} emptyLabel="No activity recorded yet." />}
        {tab === 'logins' && <FeedTab loader={() => userAdminApi.getLoginHistory(userId)} emptyLabel="No sign-ins recorded yet." />}
      </div>

      <ChangeRoleModal user={roleTarget} onClose={() => setRoleTarget(null)} onDone={load} />
      <ResetPasswordModal user={resetTarget} onClose={() => setResetTarget(null)} />
    </PermissionGate>
  );
}

// ── Profile tab ────────────────────────────────────────────────

function ProfileTab({ detail, onSaved }) {
  const parts = detail.full_name.split(' ');
  const [firstName, setFirstName] = useState(parts[0] ?? '');
  const [lastName, setLastName] = useState(parts.slice(1).join(' '));
  const [department, setDepartment] = useState(detail.department ?? '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const p = detail.full_name.split(' ');
    setFirstName(p[0] ?? '');
    setLastName(p.slice(1).join(' '));
    setDepartment(detail.department ?? '');
  }, [detail]);

  const dirty = firstName !== (parts[0] ?? '') || lastName !== parts.slice(1).join(' ') || department !== (detail.department ?? '');

  const save = async () => {
    setSaving(true);
    setError('');
    try { await userAdminApi.update(detail.id, { first_name: firstName, last_name: lastName, department }); onSaved(); }
    catch (err) { setError(err?.message ?? 'Could not save profile.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="usrd-card">
      <div className="usrd-card-title">Profile</div>
      {error && <div className="usr-form-error">{error}</div>}
      <div className="usr-field-grid">
        <PermissionGate {...USER_PERMISSIONS.USER_EDIT} fallback={<MetaItem label="First Name" value={firstName} />}>
          <div className="usr-field">
            <label className="usr-field-label">First Name</label>
            <input className="usr-input" style={{ width: '100%' }} value={firstName} onChange={(e) => setFirstName(e.target.value)} />
          </div>
        </PermissionGate>
        <PermissionGate {...USER_PERMISSIONS.USER_EDIT} fallback={<MetaItem label="Last Name" value={lastName} />}>
          <div className="usr-field">
            <label className="usr-field-label">Last Name</label>
            <input className="usr-input" style={{ width: '100%' }} value={lastName} onChange={(e) => setLastName(e.target.value)} />
          </div>
        </PermissionGate>
        <div className="usr-field">
          <label className="usr-field-label">Email</label>
          <input className="usr-input" style={{ width: '100%' }} value={detail.email} disabled />
        </div>
        <PermissionGate {...USER_PERMISSIONS.USER_EDIT} fallback={<MetaItem label="Department" value={department || '—'} />}>
          <div className="usr-field">
            <label className="usr-field-label">Department</label>
            <input className="usr-input" style={{ width: '100%' }} value={department} onChange={(e) => setDepartment(e.target.value)} />
          </div>
        </PermissionGate>
      </div>

      <div className="usrd-meta-grid" style={{ marginTop: 6 }}>
        <MetaItem label="Created" value={formatDateTime(detail.created_at)} />
        <MetaItem label="Last Login" value={detail.last_login ? formatDateTime(detail.last_login) : 'Never'} />
        <MetaItem label="Last Updated" value={formatDateTime(detail.updated_at)} />
      </div>

      <PermissionGate {...USER_PERMISSIONS.USER_EDIT} fallback={null}>
        <div style={{ marginTop: 16 }}>
          <Button variant="primary" size="sm" icon={<Save size={13} />} loading={saving} disabled={!dirty} onClick={save}>Save Changes</Button>
        </div>
      </PermissionGate>
    </div>
  );
}

function MetaItem({ label, value }) {
  return (
    <div className="usrd-meta-item">
      <span className="usrd-meta-label">{label}</span>
      <span className="usrd-meta-value">{value}</span>
    </div>
  );
}

// ── Roles tab ────────────────────────────────────────────────────

function RolesTab({ detail, onChangeRole }) {
  const rank = ROLE_RANK[detail.role] ?? 0;
  const allRoles = Object.entries(ROLE_RANK).sort((a, b) => b[1] - a[1]);

  return (
    <div className="usrd-card">
      <div className="usrd-card-title">Current Role</div>
      <div className="usrd-meta-grid" style={{ marginBottom: 16 }}>
        <MetaItem label="Role" value={ROLE_LABEL[detail.role] ?? detail.role} />
        <MetaItem label="Hierarchy Rank" value={`${rank} of ${Object.keys(ROLE_RANK).length - 1}`} />
      </div>
      <PermissionGate {...USER_PERMISSIONS.USER_ASSIGN_ROLE} fallback={null}>
        <Button variant="secondary" size="sm" icon={<ShieldCheck size={13} />} onClick={onChangeRole}>Change Role</Button>
      </PermissionGate>

      <div className="usrd-card-title" style={{ marginTop: 20 }}>Role Hierarchy</div>
      <div className="usrd-module-list">
        {allRoles.map(([role, r]) => (
          <div key={role} className="usrd-module-row">
            <span className="usrd-module-icon">{role === detail.role ? '●' : '○'}</span>
            <span className="usrd-module-name" style={{ fontWeight: role === detail.role ? 700 : 500, color: role === detail.role ? 'var(--color-primary)' : undefined }}>
              {ROLE_LABEL[role] ?? role}
            </span>
            <span className="usr-empty-inline">rank {r}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Permissions tab ──────────────────────────────────────────────

function PermissionsTab({ detail }) {
  const actions = ROLE_DEFAULT_ACTIONS[detail.role] ?? [];
  return (
    <div className="usrd-card">
      <div className="usrd-card-title">Permissions</div>
      <p className="usrd-module-hint">Actions granted to the <strong>{ROLE_LABEL[detail.role] ?? detail.role}</strong> role (from the platform's role/action matrix — the same one enforced on every module).</p>
      {actions.length === 0 ? (
        <p className="usr-empty-inline">No actions granted.</p>
      ) : (
        <div className="usrd-perm-grid">
          {actions.map((a) => <span key={a} className="usrd-perm-chip">{a}</span>)}
        </div>
      )}
    </div>
  );
}

// ── Module Access tab ────────────────────────────────────────────

function ModuleAccessTab({ detail, licensedModules, onSaved }) {
  const inherits = detail.accessible_modules == null;
  const [restrict, setRestrict] = useState(!inherits);
  const [selected, setSelected] = useState(new Set(detail.accessible_modules ?? licensedModules));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setRestrict(detail.accessible_modules != null);
    setSelected(new Set(detail.accessible_modules ?? licensedModules));
  }, [detail, licensedModules]);

  const toggle = (mod) => setSelected((s) => { const n = new Set(s); n.has(mod) ? n.delete(mod) : n.add(mod); return n; });

  const save = async () => {
    setSaving(true);
    setError('');
    try {
      await userAdminApi.update(detail.id, { accessible_modules: restrict ? [...selected] : null });
      onSaved();
    } catch (err) { setError(err?.message ?? 'Could not save module access.'); }
    finally { setSaving(false); }
  };

  return (
    <div className="usrd-card">
      <div className="usrd-card-title">Module Access</div>
      {error && <div className="usr-form-error">{error}</div>}
      <label className="usr-checkbox-row" style={{ marginBottom: 12 }}>
        <input type="checkbox" checked={restrict} onChange={(e) => setRestrict(e.target.checked)} />
        Restrict this user to specific modules (unchecked = inherits every module the organization is licensed for)
      </label>

      <div className="usrd-module-list">
        {licensedModules.length === 0 ? (
          <p className="usr-empty-inline">The organization has no licensed modules yet.</p>
        ) : (
          licensedModules.map((mod) => {
            const meta = MODULE_META[mod];
            return (
              <label key={mod} className="usrd-module-row" style={{ cursor: restrict ? 'pointer' : 'default' }}>
                <input type="checkbox" checked={!restrict || selected.has(mod)} disabled={!restrict} onChange={() => toggle(mod)} />
                <span className="usrd-module-icon">{meta?.icon ?? '📦'}</span>
                <span className="usrd-module-name">{meta?.label ?? mod}</span>
              </label>
            );
          })
        )}
      </div>

      <PermissionGate {...USER_PERMISSIONS.USER_EDIT} fallback={null}>
        <div style={{ marginTop: 16 }}>
          <Button variant="primary" size="sm" icon={<Save size={13} />} loading={saving} onClick={save}>Save Module Access</Button>
        </div>
      </PermissionGate>
    </div>
  );
}

// ── Activity / Login History (shared feed renderer) ──────────────

function FeedTab({ loader, emptyLabel }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setItems(null);
    loader().then((r) => setItems(r.items)).catch(() => setError('Failed to load.'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="usrd-card">
      {items == null ? (
        <p className="usr-empty-inline">Loading…</p>
      ) : error ? (
        <p className="usr-empty-inline">{error}</p>
      ) : items.length === 0 ? (
        <p className="usr-empty-inline">{emptyLabel}</p>
      ) : (
        <div className="usrd-feed">
          {items.map((ev) => (
            <div key={ev.id} className="usrd-feed-item">
              <div className="usrd-feed-dot" />
              <div className="usrd-feed-body">
                <div className="usrd-feed-type">{EVENT_LABEL[ev.event_type] ?? ev.event_type}</div>
                {ev.message && <div className="usrd-feed-msg">{ev.message}</div>}
                <div className="usrd-feed-meta">{ev.actor_name ? `By ${ev.actor_name} · ` : ''}{formatDateTime(ev.created_at)}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
