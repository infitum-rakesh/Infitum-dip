/**
 * CreateRoleModal — "+ Create Role" on the Roles admin page.
 * Categories the org isn't licensed for are locked in the matrix — the
 * backend clamps the same way if anything slips through (see
 * backend/app/services/role_service.py clamp_permissions).
 */
import { useState } from 'react';
import { Modal, Button } from '@/components/common/index.js';
import { roleApi } from '@/services/api/roleApi.js';
import { PermissionMatrix, setToCells } from './PermissionMatrix.jsx';
import './roles.css';

export function CreateRoleModal({ open, onClose, categories, licensedCategories, onCreated }) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [permissions, setPermissions] = useState(() => new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const handleClose = () => { setName(''); setDescription(''); setPermissions(new Set()); setError(''); onClose(); };

  const handleSubmit = async () => {
    if (!name.trim()) { setError('Role name is required.'); return; }
    setSaving(true);
    setError('');
    try {
      const created = await roleApi.create({ name: name.trim(), description: description.trim() || null, permissions: setToCells(permissions) });
      handleClose();
      onCreated?.(created);
    } catch (err) {
      setError(err?.message ?? 'Could not create role.');
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title="Create Role"
      size="xl"
      footer={
        <>
          <Button variant="secondary" onClick={handleClose} disabled={saving}>Cancel</Button>
          <Button variant="primary" onClick={handleSubmit} loading={saving}>Create Role</Button>
        </>
      }
    >
      {error && <div className="rol-form-error">{error}</div>}

      <div className="rol-field">
        <label className="rol-field-label">Role Name <span className="rol-required">*</span></label>
        <input className="rol-input" style={{ width: '100%' }} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Data Steward" autoFocus />
      </div>
      <div className="rol-field">
        <label className="rol-field-label">Description</label>
        <textarea className="rol-textarea" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What is this role for?" />
      </div>

      <div className="rol-section-title">Permissions</div>
      <p className="rol-hint">Click a category name to toggle every action for that row. Grayed-out categories aren't licensed for your organization.</p>
      <PermissionMatrix categories={categories} licensedCategories={licensedCategories} value={permissions} onChange={setPermissions} />
    </Modal>
  );
}
