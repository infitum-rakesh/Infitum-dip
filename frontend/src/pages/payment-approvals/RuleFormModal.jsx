/**
 * RuleFormModal — Create/publish-new-version form for a Payment Approval
 * rule. Rules are versioned, never mutated in place (PAY3/PAY8): editing
 * an existing rule here always calls the same POST /approvals/rules
 * publish endpoint with the same name, which the backend turns into the
 * next version — this modal never PATCHes anything.
 *
 * `rule` prop: null for a brand-new rule, or an ApprovalRuleDetailOut
 * (conditions+steps included) to pre-fill a new version of an existing
 * one.
 */
import { useEffect, useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { Modal, Button } from '@/components/common/index.js';
import { paymentApprovalApi } from '@/services/api/paymentApprovalApi.js';
import { userAdminApi } from '@/services/api/userAdminApi.js';
import { roleApi } from '@/services/api/roleApi.js';
import './payment-approvals.css';

const CONDITION_FIELDS = ['SUPPLIER', 'BUSINESS_UNIT', 'DEPARTMENT', 'COST_CENTER', 'COMPANY', 'PAYMENT_METHOD', 'SOURCE_SYSTEM'];
const CONDITION_OPERATORS = ['EQ', 'NEQ', 'IN', 'NOT_IN'];
const APPROVER_TYPES = ['USER', 'ROLE', 'DEPARTMENT', 'POOL'];

function emptyCondition() { return { field: 'SUPPLIER', operator: 'EQ', value: '' }; }
function emptyStep(stepNumber) {
  return {
    step_type: 'APPROVAL', step_number: stepNumber, approver_type: 'USER',
    approver_role_id: null, approver_user_id: null, approver_department: '', approver_pool: [],
    label: '', sla_hours: '', reminder_after_hours: '', escalation_after_hours: '',
    escalation_approver_type: '', escalation_role_id: null, escalation_user_id: null,
  };
}

function toRuleFormState(rule) {
  if (!rule) {
    return {
      name: '', description: '', priority: 100, status: 'ACTIVE',
      min_amount: '', max_amount: '', currency: '', invoice_type: '',
      action: 'REQUIRE_APPROVAL', approval_required: true, approval_count: 1, approval_mode: 'SEQUENTIAL',
      peer_review_required: false, peer_review_count: 0, peer_review_mode: 'SEQUENTIAL',
      default_sla_hours: '', conditions: [], steps: [emptyStep(1)],
    };
  }
  return {
    name: rule.name, description: rule.description || '', priority: rule.priority, status: rule.status,
    min_amount: rule.min_amount ?? '', max_amount: rule.max_amount ?? '', currency: rule.currency || '', invoice_type: rule.invoice_type || '',
    action: rule.action, approval_required: rule.approval_required, approval_count: rule.approval_count, approval_mode: rule.approval_mode,
    peer_review_required: rule.peer_review_required, peer_review_count: rule.peer_review_count, peer_review_mode: rule.peer_review_mode,
    default_sla_hours: rule.default_sla_hours ?? '',
    conditions: (rule.conditions || []).map((c) => ({ field: c.field, operator: c.operator, value: Array.isArray(c.value) ? c.value.join(', ') : c.value })),
    steps: (rule.steps || []).map((s) => ({
      step_type: s.step_type, step_number: s.step_number, approver_type: s.approver_type,
      approver_role_id: s.approver_role_id, approver_user_id: s.approver_user_id,
      approver_department: s.approver_department || '', approver_pool: s.approver_pool || [],
      label: s.label || '', sla_hours: s.sla_hours ?? '', reminder_after_hours: s.reminder_after_hours ?? '',
      escalation_after_hours: s.escalation_after_hours ?? '', escalation_approver_type: s.escalation_approver_type || '',
      escalation_role_id: s.escalation_role_id, escalation_user_id: s.escalation_user_id,
    })),
  };
}

const num = (v) => (v === '' || v === null || v === undefined ? null : Number(v));

export function RuleFormModal({ open, onClose, rule, onSaved }) {
  const [form, setForm] = useState(() => toRuleFormState(rule));
  const [users, setUsers] = useState([]);
  const [roles, setRoles] = useState([]);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState([]);

  useEffect(() => {
    if (!open) return;
    setForm(toRuleFormState(rule));
    setErrors([]);
    userAdminApi.list({ page_size: 200 }).then((res) => setUsers(res.items || [])).catch(() => setUsers([]));
    roleApi.list().then((res) => setRoles(res.items || [])).catch(() => setRoles([]));
  }, [open, rule]);

  const set = (patch) => setForm((f) => ({ ...f, ...patch }));

  const updateCondition = (i, patch) => setForm((f) => ({ ...f, conditions: f.conditions.map((c, idx) => (idx === i ? { ...c, ...patch } : c)) }));
  const addCondition = () => setForm((f) => ({ ...f, conditions: [...f.conditions, emptyCondition()] }));
  const removeCondition = (i) => setForm((f) => ({ ...f, conditions: f.conditions.filter((_, idx) => idx !== i) }));

  const updateStep = (i, patch) => setForm((f) => ({ ...f, steps: f.steps.map((s, idx) => (idx === i ? { ...s, ...patch } : s)) }));
  const addStep = () => setForm((f) => ({ ...f, steps: [...f.steps, emptyStep(f.steps.length + 1)] }));
  const removeStep = (i) => setForm((f) => ({ ...f, steps: f.steps.filter((_, idx) => idx !== i) }));

  const handleSubmit = async () => {
    if (!form.name.trim()) { setErrors(['Rule name is required.']); return; }
    setSaving(true);
    setErrors([]);
    const body = {
      name: form.name.trim(), description: form.description.trim() || null, priority: num(form.priority) ?? 100, status: form.status,
      min_amount: num(form.min_amount), max_amount: num(form.max_amount), currency: form.currency.trim() || null, invoice_type: form.invoice_type.trim() || null,
      action: form.action, approval_required: form.approval_required, approval_count: num(form.approval_count) ?? 0, approval_mode: form.approval_mode,
      peer_review_required: form.peer_review_required, peer_review_count: num(form.peer_review_count) ?? 0, peer_review_mode: form.peer_review_mode,
      default_sla_hours: num(form.default_sla_hours),
      conditions: form.conditions.map((c) => ({
        field: c.field, operator: c.operator,
        value: (c.operator === 'IN' || c.operator === 'NOT_IN') ? c.value.split(',').map((v) => v.trim()).filter(Boolean) : c.value,
      })),
      steps: form.steps.map((s) => ({
        step_type: s.step_type, step_number: num(s.step_number), approver_type: s.approver_type,
        approver_role_id: s.approver_type === 'ROLE' ? num(s.approver_role_id) : null,
        approver_user_id: s.approver_type === 'USER' ? num(s.approver_user_id) : null,
        approver_department: s.approver_type === 'DEPARTMENT' ? s.approver_department.trim() || null : null,
        approver_pool: s.approver_type === 'POOL' ? s.approver_pool : null,
        label: s.label.trim() || null, sla_hours: num(s.sla_hours), reminder_after_hours: num(s.reminder_after_hours),
        escalation_after_hours: num(s.escalation_after_hours), escalation_approver_type: s.escalation_approver_type || null,
        escalation_role_id: s.escalation_approver_type === 'ROLE' ? num(s.escalation_role_id) : null,
        escalation_user_id: s.escalation_approver_type === 'USER' ? num(s.escalation_user_id) : null,
      })),
    };
    try {
      await paymentApprovalApi.publishRule(body);
      onSaved?.();
    } catch (err) {
      const detail = err?.response?.data?.detail;
      setErrors(Array.isArray(detail?.errors) ? detail.errors : [detail?.message || detail || err?.message || 'Could not save this rule.']);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={rule ? `Publish New Version — ${rule.name}` : 'New Approval Rule'}
      size="xl"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button variant="primary" onClick={handleSubmit} loading={saving}>{rule ? 'Publish New Version' : 'Create Rule'}</Button>
        </>
      }
    >
      {errors.length > 0 && (
        <div className="par-form-errors">
          {errors.map((e, i) => <div key={i}>{e}</div>)}
        </div>
      )}

      <div className="par-grid-2">
        <div className="par-field">
          <label className="par-label">Rule Name <span className="par-required">*</span></label>
          <input className="par-input" value={form.name} onChange={(e) => set({ name: e.target.value })} placeholder="e.g. High-Value Vendor Payments" autoFocus={!rule} disabled={!!rule} />
          {rule && <div className="par-hint">Rule name never changes across versions.</div>}
        </div>
        <div className="par-field">
          <label className="par-label">Priority</label>
          <input className="par-input" type="number" value={form.priority} onChange={(e) => set({ priority: e.target.value })} />
          <div className="par-hint">Lower number wins a tie in specificity.</div>
        </div>
      </div>

      <div className="par-field">
        <label className="par-label">Description</label>
        <textarea className="par-textarea" value={form.description} onChange={(e) => set({ description: e.target.value })} placeholder="What is this rule for?" />
      </div>

      <div className="par-grid-4">
        <div className="par-field">
          <label className="par-label">Min Amount</label>
          <input className="par-input" type="number" value={form.min_amount} onChange={(e) => set({ min_amount: e.target.value })} />
        </div>
        <div className="par-field">
          <label className="par-label">Max Amount</label>
          <input className="par-input" type="number" value={form.max_amount} onChange={(e) => set({ max_amount: e.target.value })} />
        </div>
        <div className="par-field">
          <label className="par-label">Currency</label>
          <input className="par-input" value={form.currency} onChange={(e) => set({ currency: e.target.value.toUpperCase() })} placeholder="USD" maxLength={8} />
        </div>
        <div className="par-field">
          <label className="par-label">Invoice Type</label>
          <input className="par-input" value={form.invoice_type} onChange={(e) => set({ invoice_type: e.target.value })} placeholder="e.g. STANDARD" />
        </div>
      </div>

      <div className="par-section-title">Conditions</div>
      {form.conditions.map((c, i) => (
        <div key={i} className="par-row par-row-condition">
          <select className="par-select" value={c.field} onChange={(e) => updateCondition(i, { field: e.target.value })}>
            {CONDITION_FIELDS.map((f) => <option key={f} value={f}>{f}</option>)}
          </select>
          <select className="par-select" value={c.operator} onChange={(e) => updateCondition(i, { operator: e.target.value })}>
            {CONDITION_OPERATORS.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
          <input
            className="par-input" value={c.value} onChange={(e) => updateCondition(i, { value: e.target.value })}
            placeholder={c.operator === 'IN' || c.operator === 'NOT_IN' ? 'comma-separated values' : 'value'}
          />
          <button className="par-remove-btn" onClick={() => removeCondition(i)} title="Remove condition"><Trash2 size={14} /></button>
        </div>
      ))}
      <Button variant="ghost" size="sm" icon={<Plus size={13} />} onClick={addCondition}>Add Condition</Button>

      <div className="par-section-title">Approval Phase</div>
      <div className="par-grid-3">
        <label className="par-checkbox-field">
          <input type="checkbox" checked={form.approval_required} onChange={(e) => set({ approval_required: e.target.checked })} />
          Approval required
        </label>
        <div className="par-field">
          <label className="par-label">Approval count required</label>
          <input className="par-input" type="number" min={0} value={form.approval_count} onChange={(e) => set({ approval_count: e.target.value })} disabled={!form.approval_required} />
        </div>
        <div className="par-field">
          <label className="par-label">Approval mode</label>
          <select className="par-select" value={form.approval_mode} onChange={(e) => set({ approval_mode: e.target.value })} disabled={!form.approval_required}>
            <option value="SEQUENTIAL">SEQUENTIAL</option>
            <option value="PARALLEL">PARALLEL</option>
          </select>
        </div>
      </div>

      <div className="par-section-title">Peer Review Phase</div>
      <div className="par-grid-3">
        <label className="par-checkbox-field">
          <input type="checkbox" checked={form.peer_review_required} onChange={(e) => set({ peer_review_required: e.target.checked })} />
          Peer review required
        </label>
        <div className="par-field">
          <label className="par-label">Peer review count required</label>
          <input className="par-input" type="number" min={0} value={form.peer_review_count} onChange={(e) => set({ peer_review_count: e.target.value })} disabled={!form.peer_review_required} />
        </div>
        <div className="par-field">
          <label className="par-label">Peer review mode</label>
          <select className="par-select" value={form.peer_review_mode} onChange={(e) => set({ peer_review_mode: e.target.value })} disabled={!form.peer_review_required}>
            <option value="SEQUENTIAL">SEQUENTIAL</option>
            <option value="PARALLEL">PARALLEL</option>
          </select>
        </div>
      </div>

      <div className="par-grid-2">
        <div className="par-field">
          <label className="par-label">Action</label>
          <select className="par-select" value={form.action} onChange={(e) => set({ action: e.target.value })}>
            <option value="REQUIRE_APPROVAL">REQUIRE_APPROVAL</option>
            <option value="AUTO_APPROVE">AUTO_APPROVE</option>
          </select>
        </div>
        <div className="par-field">
          <label className="par-label">Status</label>
          <select className="par-select" value={form.status} onChange={(e) => set({ status: e.target.value })}>
            <option value="ACTIVE">ACTIVE</option>
            <option value="INACTIVE">INACTIVE</option>
          </select>
        </div>
      </div>

      <div className="par-section-title">Steps</div>
      {form.steps.map((s, i) => (
        <div key={i} className="par-step-card">
          <div className="par-step-header">
            <strong>Step {s.step_number}</strong>
            <button className="par-remove-btn" onClick={() => removeStep(i)} title="Remove step"><Trash2 size={14} /></button>
          </div>
          <div className="par-grid-4">
            <div className="par-field">
              <label className="par-label">Type</label>
              <select className="par-select" value={s.step_type} onChange={(e) => updateStep(i, { step_type: e.target.value })}>
                <option value="APPROVAL">APPROVAL</option>
                <option value="PEER_REVIEW">PEER_REVIEW</option>
              </select>
            </div>
            <div className="par-field">
              <label className="par-label">Step #</label>
              <input className="par-input" type="number" value={s.step_number} onChange={(e) => updateStep(i, { step_number: e.target.value })} />
            </div>
            <div className="par-field">
              <label className="par-label">Approver Type</label>
              <select className="par-select" value={s.approver_type} onChange={(e) => updateStep(i, { approver_type: e.target.value })}>
                {APPROVER_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div className="par-field">
              <label className="par-label">Label</label>
              <input className="par-input" value={s.label} onChange={(e) => updateStep(i, { label: e.target.value })} placeholder="optional" />
            </div>
          </div>

          <div className="par-field">
            {s.approver_type === 'USER' && (
              <select className="par-select" value={s.approver_user_id ?? ''} onChange={(e) => updateStep(i, { approver_user_id: e.target.value })}>
                <option value="">Select a user…</option>
                {users.map((u) => <option key={u.id} value={u.id}>{u.full_name} ({u.email})</option>)}
              </select>
            )}
            {s.approver_type === 'ROLE' && (
              <select className="par-select" value={s.approver_role_id ?? ''} onChange={(e) => updateStep(i, { approver_role_id: e.target.value })}>
                <option value="">Select a role…</option>
                {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
              </select>
            )}
            {s.approver_type === 'DEPARTMENT' && (
              <input className="par-input" value={s.approver_department} onChange={(e) => updateStep(i, { approver_department: e.target.value })} placeholder="e.g. Finance" />
            )}
            {s.approver_type === 'POOL' && (
              <div className="par-pool-list">
                {users.map((u) => (
                  <label key={u.id} className="par-pool-item">
                    <input
                      type="checkbox" checked={s.approver_pool.includes(u.id)}
                      onChange={(e) => updateStep(i, { approver_pool: e.target.checked ? [...s.approver_pool, u.id] : s.approver_pool.filter((id) => id !== u.id) })}
                    />
                    {u.full_name}
                  </label>
                ))}
              </div>
            )}
          </div>

          <div className="par-grid-3">
            <div className="par-field">
              <label className="par-label">SLA (hours)</label>
              <input className="par-input" type="number" value={s.sla_hours} onChange={(e) => updateStep(i, { sla_hours: e.target.value })} />
            </div>
            <div className="par-field">
              <label className="par-label">Reminder after (hours)</label>
              <input className="par-input" type="number" value={s.reminder_after_hours} onChange={(e) => updateStep(i, { reminder_after_hours: e.target.value })} />
            </div>
            <div className="par-field">
              <label className="par-label">Escalate after (hours)</label>
              <input className="par-input" type="number" value={s.escalation_after_hours} onChange={(e) => updateStep(i, { escalation_after_hours: e.target.value })} />
            </div>
          </div>

          {s.escalation_after_hours !== '' && s.escalation_after_hours !== null && (
            <div className="par-grid-2">
              <div className="par-field">
                <label className="par-label">Escalate to type</label>
                <select className="par-select" value={s.escalation_approver_type} onChange={(e) => updateStep(i, { escalation_approver_type: e.target.value })}>
                  <option value="">— none —</option>
                  <option value="USER">USER</option>
                  <option value="ROLE">ROLE</option>
                </select>
              </div>
              {s.escalation_approver_type === 'USER' && (
                <select className="par-select" value={s.escalation_user_id ?? ''} onChange={(e) => updateStep(i, { escalation_user_id: e.target.value })}>
                  <option value="">Select a user…</option>
                  {users.map((u) => <option key={u.id} value={u.id}>{u.full_name}</option>)}
                </select>
              )}
              {s.escalation_approver_type === 'ROLE' && (
                <select className="par-select" value={s.escalation_role_id ?? ''} onChange={(e) => updateStep(i, { escalation_role_id: e.target.value })}>
                  <option value="">Select a role…</option>
                  {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
                </select>
              )}
            </div>
          )}
        </div>
      ))}
      <Button variant="ghost" size="sm" icon={<Plus size={13} />} onClick={addStep}>Add Step</Button>
    </Modal>
  );
}
