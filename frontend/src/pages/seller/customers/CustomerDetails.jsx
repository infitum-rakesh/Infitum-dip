/**
 * CustomerDetails — /app/seller/customers/:customerId (Seller Portal Phase 2).
 * Tabs: Overview | Users | Subscription | Modules | Usage | Invoices | Activity | Audit.
 * Reuses StatusBadge/Button/EmptyState/PermissionGate — no duplicated components.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft, Pencil, Power, PowerOff, ShieldAlert, Building2, Users as UsersIcon,
  CreditCard, Puzzle, Gauge, Receipt, Activity as ActivityIcon, ScrollText,
  Mail, Copy, Check, CheckCircle2, XCircle,
} from 'lucide-react';
import { Button, StatusBadge, EmptyState, PermissionGate, FullPageSpinner, Modal } from '@/components/common/index.js';
import { SELLER_PERMISSIONS } from '@/utils/constants.js';
import { MODULE_META } from '@/utils/permissions.js';
import { formatDate, formatDateTime, formatNumber } from '@/utils/formatters.js';
import { useAuth } from '@/hooks/useAuth.js';
import { customerApi } from '@/services/api/seller/customerApi.js';
import { EditCustomerModal } from './EditCustomerModal.jsx';
import './customers.css';
import './customer-details.css';

const STATUS_VARIANT = { active: 'success', trial: 'info', expiring: 'warning', expired: 'gray', suspended: 'danger' };

const TABS = [
  { id: 'overview', label: 'Overview', icon: Building2 },
  { id: 'users', label: 'Users', icon: UsersIcon },
  { id: 'subscription', label: 'Subscription', icon: CreditCard },
  { id: 'modules', label: 'Modules', icon: Puzzle },
  { id: 'usage', label: 'Usage', icon: Gauge },
  { id: 'invoices', label: 'Invoices', icon: Receipt },
  { id: 'activity', label: 'Activity', icon: ActivityIcon },
  { id: 'audit', label: 'Audit', icon: ScrollText },
];

export default function CustomerDetails() {
  const { customerId } = useParams();
  const navigate = useNavigate();
  const auth = useAuth();

  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState('overview');
  const [editOpen, setEditOpen] = useState(false);
  const [impersonateOpen, setImpersonateOpen] = useState(false);
  const [impersonating, setImpersonating] = useState(false);
  const [resendResult, setResendResult] = useState(null);
  const [resending, setResending] = useState(false);
  const [copied, setCopied] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    customerApi.get(customerId).then(setDetail).catch(() => setError('Customer not found or failed to load.')).finally(() => setLoading(false));
  }, [customerId]);

  useEffect(() => { load(); }, [load]);

  const runActivate = async () => { await customerApi.activate(customerId); load(); };
  const runSuspend = async () => {
    if (!window.confirm(`Suspend "${detail.name}"? Their users will lose access immediately.`)) return;
    await customerApi.suspend(customerId); load();
  };
  const runResendWelcome = async () => {
    if (!window.confirm(`Reset the password for ${detail.name}'s Admin and email them a new one?`)) return;
    setResending(true);
    try {
      const result = await customerApi.resendWelcomeEmail(customerId);
      setResendResult(result);
    } catch (err) {
      window.alert(err?.message ?? 'Could not resend welcome email.');
    } finally {
      setResending(false);
    }
  };
  const copyResendPassword = () => {
    navigator.clipboard?.writeText(resendResult.temp_password).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
  };

  const confirmImpersonate = async () => {
    setImpersonating(true);
    try {
      const session = await customerApi.impersonate(customerId);
      auth.impersonate(session);
      navigate('/app/dashboard');
    } catch (err) {
      window.alert(err?.message ?? 'Could not impersonate this customer.');
    } finally {
      setImpersonating(false);
    }
  };

  if (loading) return <FullPageSpinner />;
  if (error || !detail) return <EmptyState icon={<Building2 size={28} />} title="Customer not found" description={error} action={{ label: 'Back to Customers', onClick: () => navigate('/app/seller/customers') }} />;

  const suspended = detail.license_status === 'suspended';

  return (
    <PermissionGate {...SELLER_PERMISSIONS.SELLER_PORTAL_ACCESS} showReason>
      <div className="scd-page">
        <button className="scd-back" onClick={() => navigate('/app/seller/customers')}><ArrowLeft size={14} /> Customers</button>

        <div className="scd-header">
          <div className="scd-header-main">
            <div className="scd-avatar"><Building2 size={20} /></div>
            <div>
              <div className="scd-title-row">
                <h1>{detail.name}</h1>
                <StatusBadge variant={STATUS_VARIANT[detail.license_status] ?? 'gray'} dot>
                  {detail.license_status.charAt(0).toUpperCase() + detail.license_status.slice(1)}
                </StatusBadge>
                <span className="sc-plan-pill">{detail.plan}</span>
              </div>
              <p className="scd-subtitle">
                {detail.legal_name ? `${detail.legal_name} · ` : ''}{detail.industry || 'Industry not set'} · {detail.country || 'Country not set'}
              </p>
            </div>
          </div>
          <div className="scd-actions">
            <Button variant="secondary" size="sm" icon={<Pencil size={13} />} onClick={() => setEditOpen(true)}>Edit</Button>
            {suspended
              ? <Button variant="secondary" size="sm" icon={<Power size={13} />} onClick={runActivate}>Activate</Button>
              : <Button variant="secondary" size="sm" icon={<PowerOff size={13} />} onClick={runSuspend}>Suspend</Button>}
            <Button variant="secondary" size="sm" icon={<Mail size={13} />} loading={resending} onClick={runResendWelcome}>Resend Welcome Email</Button>
            <Button variant="secondary" size="sm" icon={<ShieldAlert size={13} />} onClick={() => setImpersonateOpen(true)}>Impersonate</Button>
          </div>
        </div>

        <div className="scd-tab-nav">
          {TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button key={t.id} className={`scd-tab-btn ${tab === t.id ? 'active' : ''}`} onClick={() => setTab(t.id)}>
                <Icon size={14} /> {t.label}
              </button>
            );
          })}
        </div>

        {tab === 'overview' && <OverviewTab detail={detail} onSaved={load} />}
        {tab === 'users' && <UsersTab detail={detail} />}
        {tab === 'subscription' && <SubscriptionTab detail={detail} />}
        {tab === 'modules' && <ModulesTab detail={detail} />}
        {tab === 'usage' && <UsageTab detail={detail} />}
        {tab === 'invoices' && <InvoicesTab />}
        {tab === 'activity' && <ActivityTab detail={detail} />}
        {tab === 'audit' && <AuditTab customerId={customerId} />}
      </div>

      <EditCustomerModal
        open={editOpen}
        customer={detail}
        onClose={() => setEditOpen(false)}
        onSaved={() => { setEditOpen(false); load(); }}
      />

      <Modal
        open={impersonateOpen}
        onClose={() => setImpersonateOpen(false)}
        title="Impersonate Customer"
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setImpersonateOpen(false)} disabled={impersonating}>Cancel</Button>
            <Button variant="primary" onClick={confirmImpersonate} loading={impersonating}>Log In As Admin</Button>
          </>
        }
      >
        <div className="sc-impersonate-warning">
          <ShieldAlert size={18} style={{ flexShrink: 0 }} />
          <span>
            You are about to log in as the primary admin of <strong>{detail.name}</strong>. This action is audited and
            cannot be done silently — a permanent record will be created with your identity, the target account, and
            a timestamp.
          </span>
        </div>
      </Modal>

      <Modal
        open={!!resendResult}
        onClose={() => setResendResult(null)}
        title="Welcome Email Resent"
        size="sm"
        footer={<Button variant="primary" onClick={() => setResendResult(null)}>Done</Button>}
      >
        {resendResult && (
          <div className="sc-credential-box">
            <div className="sc-credential-label">New temporary password for {resendResult.admin_email}</div>
            <div className="sc-credential-row">
              <span className="sc-credential-value">{resendResult.temp_password}</span>
              <Button variant="ghost" size="sm" icon={copied ? <Check size={13} /> : <Copy size={13} />} onClick={copyResendPassword}>
                {copied ? 'Copied' : 'Copy'}
              </Button>
            </div>
            <p className="sc-credential-note" style={{ display: 'flex', alignItems: 'center', gap: 6, color: resendResult.email_delivered ? 'var(--success, #10B981)' : 'var(--danger, #DC2626)' }}>
              {resendResult.email_delivered ? <CheckCircle2 size={13} /> : <XCircle size={13} />} {resendResult.email_delivered ? 'Emailed successfully.' : resendResult.email_detail}
            </p>
            <p className="sc-credential-note">Their old password no longer works — this won't be shown again.</p>
          </div>
        )}
      </Modal>
    </PermissionGate>
  );
}

// ── Tabs ─────────────────────────────────────────────────────────

function OverviewTab({ detail }) {
  return (
    <div className="scd-grid">
      <div className="scd-card">
        <div className="scd-card-title">Organization</div>
        <dl className="scd-dl">
          <dt>Legal Name</dt><dd>{detail.legal_name || '—'}</dd>
          <dt>Industry</dt><dd>{detail.industry || '—'}</dd>
          <dt>Country</dt><dd>{detail.country || '—'}</dd>
          <dt>Timezone</dt><dd>{detail.timezone || '—'}</dd>
          <dt>Created</dt><dd>{formatDate(detail.created_at)}</dd>
        </dl>
      </div>
      <div className="scd-card">
        <div className="scd-card-title">Primary Contact</div>
        <dl className="scd-dl">
          <dt>Name</dt><dd>{detail.primary_contact_name || '—'}</dd>
          <dt>Email</dt><dd>{detail.primary_contact_email || '—'}</dd>
          <dt>Phone</dt><dd>{detail.primary_contact_phone || '—'}</dd>
        </dl>
      </div>
      <div className="scd-card">
        <div className="scd-card-title">Billing Address &amp; Company</div>
        <dl className="scd-dl">
          <dt>Address</dt><dd>{detail.address_line || '—'}</dd>
          <dt>City</dt><dd>{detail.city || '—'}</dd>
          <dt>Branch</dt><dd>{detail.branch || '—'}</dd>
          <dt>Postal Code</dt><dd>{detail.postal_code || '—'}</dd>
          <dt>Invoiced Through</dt><dd>{detail.company_name || 'Default company'}</dd>
        </dl>
      </div>
      <div className="scd-card">
        <div className="scd-card-title">Subscription</div>
        <dl className="scd-dl">
          <dt>Plan</dt><dd>{detail.plan}</dd>
          <dt>Status</dt><dd><StatusBadge variant={STATUS_VARIANT[detail.license_status] ?? 'gray'} dot>{detail.license_status}</StatusBadge></dd>
          <dt>Start</dt><dd>{detail.subscription ? formatDate(detail.subscription.start_date) : '—'}</dd>
          <dt>End</dt><dd>{detail.subscription?.end_date ? formatDate(detail.subscription.end_date) : (detail.trial_expires_at ? formatDate(detail.trial_expires_at) : '—')}</dd>
        </dl>
      </div>
      <div className="scd-card">
        <div className="scd-card-title">Usage &amp; Storage</div>
        <dl className="scd-dl">
          <dt>Users</dt><dd>{detail.users_count} / {detail.max_users > 0 ? detail.max_users : '∞'}</dd>
          <dt>Documents</dt><dd>{formatNumber(detail.usage.documents.used)}{detail.usage.documents.limit != null ? ` / ${formatNumber(detail.usage.documents.limit)}` : ''}</dd>
          <dt>AI Calls</dt><dd>{formatNumber(detail.usage.ai_calls.used)}{detail.usage.ai_calls.limit != null ? ` / ${formatNumber(detail.usage.ai_calls.limit)}` : ''}</dd>
          <dt>Storage</dt><dd>{detail.usage.storage_gb.used.toFixed(2)} GB{detail.usage.storage_gb.limit != null ? ` / ${detail.usage.storage_gb.limit} GB` : ''}</dd>
        </dl>
      </div>
      <div className="scd-card scd-card-wide">
        <div className="scd-card-title">Licensed Modules ({detail.licensed_modules.length})</div>
        <div className="scd-module-chips">
          {detail.licensed_modules.length === 0
            ? <span className="scd-muted">No modules licensed yet.</span>
            : detail.licensed_modules.map((m) => <span key={m} className="scd-module-chip">{MODULE_META[m]?.label ?? m}</span>)}
        </div>
      </div>
      <div className="scd-card scd-card-wide">
        <div className="scd-card-title">Recent Activity</div>
        {detail.recent_activity.length === 0
          ? <span className="scd-muted">No activity recorded yet.</span>
          : (
            <ul className="scd-activity-list">
              {detail.recent_activity.slice(0, 5).map((a, i) => (
                <li key={i}><strong>{a.user_name}</strong> — {a.message || a.event_type} <span className="scd-muted">· {formatDateTime(a.timestamp)}</span></li>
              ))}
            </ul>
          )}
      </div>
    </div>
  );
}

function UsersTab({ detail }) {
  if (detail.users.length === 0) return <EmptyState icon={<UsersIcon size={26} />} title="No users yet" description="This customer hasn't created any user accounts." />;
  return (
    <div className="scd-table-wrap">
      <table className="scd-table">
        <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Last Login</th></tr></thead>
        <tbody>
          {detail.users.map((u) => (
            <tr key={u.id}>
              <td>{u.full_name}</td><td>{u.email}</td><td>{u.role}</td>
              <td><StatusBadge variant={u.status === 'ACTIVE' ? 'success' : u.status === 'PENDING' ? 'warning' : 'gray'} dot>{u.status}</StatusBadge></td>
              <td>{u.last_login ? formatDateTime(u.last_login) : 'Never'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SubscriptionTab({ detail }) {
  return (
    <div className="scd-card">
      <div className="scd-card-title">Subscription History</div>
      {detail.subscription_history.length === 0 ? (
        <EmptyState icon={<CreditCard size={26} />} title="No subscription history" />
      ) : (
        <div className="scd-table-wrap">
          <table className="scd-table">
            <thead><tr><th>Status</th><th>Start</th><th>End</th><th>Billing Cycle</th><th>Amount</th></tr></thead>
            <tbody>
              {detail.subscription_history.map((s) => (
                <tr key={s.id}>
                  <td><StatusBadge variant={s.status === 'ACTIVE' ? 'success' : s.status === 'SUSPENDED' ? 'danger' : 'gray'} dot>{s.status}</StatusBadge></td>
                  <td>{formatDate(s.start_date)}</td>
                  <td>{s.end_date ? formatDate(s.end_date) : '—'}</td>
                  <td>{s.billing_cycle}</td>
                  <td>${formatNumber(s.amount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ModulesTab({ detail }) {
  return (
    <div className="scd-table-wrap">
      <table className="scd-table">
        <thead><tr><th>Module</th><th>Purchased</th><th>Enabled</th><th>Usage</th><th>Limit</th><th>Expiry</th></tr></thead>
        <tbody>
          {detail.modules.map((m) => (
            <tr key={m.module}>
              <td>{MODULE_META[m.module]?.label ?? m.module}</td>
              <td>{m.purchased ? <StatusBadge variant="success" dot>Yes</StatusBadge> : <StatusBadge variant="gray">No</StatusBadge>}</td>
              <td>{m.enabled ? <StatusBadge variant="success" dot>Yes</StatusBadge> : <StatusBadge variant="gray">No</StatusBadge>}</td>
              <td>{m.usage != null ? formatNumber(m.usage) : '—'}</td>
              <td>{m.limit != null ? formatNumber(m.limit) : '—'}</td>
              <td>{m.expiry ? formatDate(m.expiry) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function UsageTab({ detail }) {
  const rows = [
    { label: 'Users', ...detail.usage.users },
    { label: 'Documents', ...detail.usage.documents },
    { label: 'AI Calls', ...detail.usage.ai_calls },
    { label: 'Storage (GB)', ...detail.usage.storage_gb },
  ];
  return (
    <div className="scd-grid">
      {rows.map((r) => {
        const pct = r.limit ? Math.min(100, Math.round((r.used / r.limit) * 100)) : null;
        return (
          <div className="scd-card" key={r.label}>
            <div className="scd-card-title">{r.label}</div>
            <div className="scd-usage-val">{formatNumber(r.used)}{r.limit != null ? <span className="scd-muted"> / {formatNumber(r.limit)}</span> : <span className="scd-muted"> / Unlimited</span>}</div>
            {pct != null && (
              <div className="scd-usage-track"><div className="scd-usage-fill" style={{ width: `${pct}%`, background: pct >= 90 ? 'var(--color-danger)' : pct >= 70 ? 'var(--color-warning)' : 'var(--color-success)' }} /></div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function InvoicesTab() {
  return (
    <EmptyState
      icon={<Receipt size={28} />}
      title="Invoicing not available yet"
      description="This platform doesn't have a billing/invoicing system wired up yet — no invoice records exist to show. Subscription amounts are tracked under the Subscription tab."
    />
  );
}

function ActivityTab({ detail }) {
  if (detail.recent_activity.length === 0) return <EmptyState icon={<ActivityIcon size={26} />} title="No activity yet" />;
  return (
    <ul className="scd-activity-list scd-activity-list-full">
      {detail.recent_activity.map((a, i) => (
        <li key={i}><strong>{a.user_name}</strong> — {a.message || a.event_type} <span className="scd-muted">· {formatDateTime(a.timestamp)}</span></li>
      ))}
    </ul>
  );
}

function AuditTab({ customerId }) {
  const [items, setItems] = useState(null);
  useEffect(() => { customerApi.getAudit(customerId).then((r) => setItems(r.items)).catch(() => setItems([])); }, [customerId]);
  if (items === null) return <div className="scd-muted">Loading…</div>;
  if (items.length === 0) return <EmptyState icon={<ScrollText size={26} />} title="No audit events yet" />;
  return (
    <div className="scd-table-wrap">
      <table className="scd-table">
        <thead><tr><th>Timestamp</th><th>Source</th><th>Action</th><th>Module</th><th>Resource</th><th>Status</th></tr></thead>
        <tbody>
          {items.map((e) => (
            <tr key={e.id}>
              <td>{formatDateTime(e.timestamp)}</td>
              <td><StatusBadge variant={e.source === 'seller' ? 'primary' : 'gray'}>{e.source === 'seller' ? 'Seller' : 'Organization'}</StatusBadge></td>
              <td>{e.action}</td>
              <td>{e.module}</td>
              <td>{e.resource || '—'}</td>
              <td><StatusBadge variant={e.status === 'SUCCESS' ? 'success' : e.status === 'FAILURE' ? 'danger' : 'warning'} dot>{e.status}</StatusBadge></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
