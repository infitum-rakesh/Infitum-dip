/**
 * UserManagement — create, view, edit, deactivate users.
 * Org Admin cannot grant modules that the seller hasn't licensed to this org.
 */
import { useState, useEffect } from 'react';
import { Plus, Pencil, UserX, RefreshCw, AlertCircle } from 'lucide-react';
import { Button, Modal, StatusBadge, SearchBox } from '@/components/common/index.js';
import { usePermission }      from '@/hooks/usePermission.js';
import { userApi }            from '@/services/api/userApi.js';
import { ROLES, MODULES }     from '@/utils/constants.js';
import { MODULE_META }        from '@/utils/permissions.js';
import { initials }           from '@/utils/formatters.js';
import '../administration/administration.css';

const ROLE_OPTIONS = [
  { value: ROLES.ORG_ADMIN,     label: 'Org Admin' },
  { value: ROLES.DATA_ENGINEER, label: 'Data Engineer' },
  { value: ROLES.ANALYST,       label: 'Analyst' },
  { value: ROLES.VIEWER,        label: 'Viewer' },
];

const ROLE_VARIANT = {
  [ROLES.ORG_ADMIN]:     'primary',
  [ROLES.DATA_ENGINEER]: 'info',
  [ROLES.ANALYST]:       'warning',
  [ROLES.VIEWER]:        'gray',
};

const BLANK = { full_name: '', email: '', role: ROLES.ANALYST, accessible_modules: null };

export default function UserManagement() {
  const { isAdmin, hasModule } = usePermission();
  const [users, setUsers]       = useState([]);
  const [loading, setLoading]   = useState(true);
  const [search, setSearch]     = useState('');
  const [modalOpen, setModal]   = useState(false);
  const [editUser, setEditUser] = useState(null);
  const [form, setForm]         = useState(BLANK);
  const [saving, setSaving]     = useState(false);
  const [error, setError]       = useState('');

  useEffect(() => {
    userApi.list('org_1').then(({ items }) => { setUsers(items); setLoading(false); });
  }, []);

  const openAdd  = () => { setEditUser(null); setForm({ ...BLANK }); setError(''); setModal(true); };
  const openEdit = (u) => { setEditUser(u); setForm({ full_name: u.full_name, email: u.email, role: u.role, accessible_modules: u.accessible_modules }); setError(''); setModal(true); };

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const handleSave = async () => {
    if (!form.full_name.trim() || !form.email.trim()) { setError('Name and email are required.'); return; }
    setSaving(true);
    try {
      if (editUser) {
        const updated = await userApi.update(editUser.id, form);
        setUsers((prev) => prev.map((u) => u.id === editUser.id ? { ...u, ...updated } : u));
      } else {
        const created = await userApi.create({ ...form, organization_id: 'org_1' });
        setUsers((prev) => [...prev, created]);
      }
      setModal(false);
    } finally { setSaving(false); }
  };

  const handleDeactivate = async (id) => {
    await userApi.deactivate(id);
    setUsers((prev) => prev.map((u) => u.id === id ? { ...u, is_active: false } : u));
  };

  const filtered = users.filter((u) =>
    !search || u.full_name.toLowerCase().includes(search.toLowerCase()) || u.email.includes(search)
  );

  const active = users.filter((u) => u.is_active).length;

  return (
    <div>
      <div className="adm-header">
        <div>
          <h1 className="adm-title">User Management</h1>
          <p className="adm-subtitle">{active} active users · {users.length} total</p>
        </div>
        <div className="adm-actions">
          <SearchBox value={search} onChange={setSearch} placeholder="Search users…" style={{ width: 220 }} />
          {isAdmin() && <Button variant="primary" icon={<Plus size={14} />} onClick={openAdd}>Invite User</Button>}
        </div>
      </div>

      <div className="table-wrap">
        {/* Header */}
        <div className="adm-user-row" style={{ background:'#F8FAFC', borderBottom:'1px solid var(--border)', cursor:'default' }}>
          <span />
          {['Name / Email','Role','Status','Modules','Last Login',''].map((h) => (
            <span key={h} style={{ fontSize:10, fontWeight:700, color:'var(--text-muted)', textTransform:'uppercase', letterSpacing:'.4px' }}>{h}</span>
          ))}
        </div>

        {loading ? (
          <div style={{ padding:40, textAlign:'center', color:'var(--text-muted)' }}>Loading users…</div>
        ) : filtered.map((u) => (
          <div key={u.id} className="adm-user-row">
            <div className="adm-user-avatar">{initials(u.full_name)}</div>
            <div>
              <div className="adm-user-name">{u.full_name}</div>
              <div className="adm-user-email">{u.email}</div>
            </div>
            <StatusBadge variant={ROLE_VARIANT[u.role] ?? 'gray'} size="sm">
              {u.role.replace(/_/g,' ')}
            </StatusBadge>
            <StatusBadge variant={u.is_active ? 'success' : 'danger'} dot size="sm">
              {u.is_active ? 'Active' : 'Inactive'}
            </StatusBadge>
            <div style={{ fontSize:11, color:'var(--text-muted)' }}>
              {u.accessible_modules
                ? `${u.accessible_modules.length} assigned`
                : <span style={{ color:'var(--color-success-dark)', fontWeight:600 }}>All org modules</span>}
            </div>
            <div style={{ fontSize:11, color:'var(--text-muted)' }}>
              {u.last_login ? new Date(u.last_login).toLocaleDateString() : 'Never'}
            </div>
            <div style={{ display:'flex', gap:4 }}>
              {isAdmin() && <Button variant="ghost" size="sm" onClick={() => openEdit(u)}><Pencil size={12}/></Button>}
              {isAdmin() && u.is_active && <Button variant="ghost" size="sm" onClick={() => handleDeactivate(u.id)} style={{ color:'var(--color-danger)' }}><UserX size={12}/></Button>}
            </div>
          </div>
        ))}
      </div>

      {/* Create/Edit modal */}
      <Modal open={modalOpen} onClose={() => setModal(false)} title={editUser ? 'Edit User' : 'Invite User'}
        footer={<><Button variant="secondary" onClick={() => setModal(false)}>Cancel</Button><Button variant="primary" loading={saving} onClick={handleSave}>{editUser ? 'Save' : 'Send Invite'}</Button></>}
      >
        {error && <div style={{ padding:'8px 12px', background:'var(--color-danger-bg)', borderRadius:8, fontSize:13, color:'var(--color-danger-dark)', marginBottom:14, display:'flex', gap:6 }}><AlertCircle size={14}/>{error}</div>}
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:12 }}>
          <div>
            <label style={{ fontSize:12, fontWeight:600, color:'var(--text-secondary)', display:'block', marginBottom:5 }}>Full Name *</label>
            <input style={{ width:'100%', padding:'7px 10px', border:'1px solid var(--border)', borderRadius:7, fontSize:13, outline:'none' }} value={form.full_name} onChange={set('full_name')} placeholder="Jane Smith" />
          </div>
          <div>
            <label style={{ fontSize:12, fontWeight:600, color:'var(--text-secondary)', display:'block', marginBottom:5 }}>Email *</label>
            <input type="email" style={{ width:'100%', padding:'7px 10px', border:'1px solid var(--border)', borderRadius:7, fontSize:13, outline:'none' }} value={form.email} onChange={set('email')} placeholder="jane@company.com" disabled={!!editUser} />
          </div>
        </div>
        <div style={{ marginTop:12 }}>
          <label style={{ fontSize:12, fontWeight:600, color:'var(--text-secondary)', display:'block', marginBottom:5 }}>Role</label>
          <select style={{ width:'100%', padding:'7px 10px', border:'1px solid var(--border)', borderRadius:7, fontSize:13, background:'white', outline:'none' }} value={form.role} onChange={set('role')}>
            {ROLE_OPTIONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
          </select>
        </div>
        <p style={{ fontSize:11, color:'var(--text-muted)', marginTop:12 }}>
          Module access can be configured separately in Module Access → assign per-module permissions.
        </p>
      </Modal>
    </div>
  );
}
