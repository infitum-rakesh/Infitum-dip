/**
 * Licenses — /app/admin/licenses
 * Read-only for organization admins: Seller controls purchased modules,
 * subscription plan, limits and expiration; org admins only control which
 * of their users get access to purchased modules (Administration → Users).
 * Reuses MainLayout/Header/Sidebar/DataTable/StatusBadge/PermissionContext
 * and utils/permissions.js' MODULE_META — no duplicated module labels.
 */
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ShieldCheck, Gauge, Clock, Layers, ArrowRight, Info,
} from 'lucide-react';
import { DataTable, StatusBadge, PermissionGate, FullPageSpinner, EmptyState } from '@/components/common/index.js';
import { LICENSE_PERMISSIONS } from '@/utils/constants.js';
import { MODULE_META } from '@/utils/permissions.js';
import { formatDate, formatNumber } from '@/utils/formatters.js';
import { licenseApi } from '@/services/api/licenseApi.js';
import './licenses.css';

const STATUS_VARIANT = { trial: 'info', active: 'success', expiring: 'warning', expired: 'danger', suspended: 'danger' };
const STATUS_LABEL = { trial: 'Trial', active: 'Active', expiring: 'Expiring', expired: 'Expired', suspended: 'Suspended' };

const USAGE_LABEL = { users: 'Users', documents: 'Documents', ai_calls: 'AI Calls', storage_gb: 'Storage (GB)' };

function usageFillClass(used, limit) {
  if (!limit) return '';
  const pct = used / limit;
  if (pct >= 0.9) return 'danger';
  if (pct >= 0.7) return 'warn';
  return '';
}

export default function Licenses() {
  const [dashboard, setDashboard] = useState(null);
  const [modules, setModules] = useState(null);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    Promise.all([licenseApi.getDashboard(), licenseApi.getModules(), licenseApi.getStatus()])
      .then(([d, m, s]) => { setDashboard(d); setModules(m.items); setStatus(s); })
      .catch(() => setError('Failed to load license information.'));
  }, []);

  if (error) {
    return (
      <div className="lic-page">
        <EmptyState icon={<ShieldCheck size={28} />} title="Could not load licenses" description={error} />
      </div>
    );
  }
  if (!dashboard || !modules || !status) return <FullPageSpinner />;

  const columns = [
    {
      key: 'module', header: 'Module',
      render: (v) => {
        const meta = MODULE_META[v];
        return (
          <div className="lic-module-cell">
            <span className="lic-module-icon">{meta?.icon ?? '📦'}</span>
            {meta?.label ?? v}
          </div>
        );
      },
    },
    { key: 'purchased', header: 'Purchased', render: (v) => v ? <span className="lic-flag-yes">Yes</span> : <span className="lic-flag-no">No</span> },
    { key: 'enabled', header: 'Enabled', render: (v) => v ? <span className="lic-flag-yes">Yes</span> : <span className="lic-flag-no">No</span> },
    {
      key: 'usage', header: 'Usage',
      render: (v, row) => v == null || row.limit == null
        ? <span className="lic-na">{row.purchased ? 'Not tracked' : '—'}</span>
        : <span className="lic-usage-cell">{formatNumber(v)} / {formatNumber(row.limit)}</span>,
    },
    { key: 'limit', header: 'Limit', render: (v) => v == null ? <span className="lic-na">—</span> : formatNumber(v) },
    { key: 'expiry', header: 'Expiry', render: (v) => v ? formatDate(v) : <span className="lic-na">—</span> },
  ];

  return (
    <PermissionGate {...LICENSE_PERMISSIONS.LICENSE_VIEW} showReason>
      <div className="lic-page">
        {/* Header */}
        <div className="lic-header">
          <div>
            <h1 className="lic-title">Licenses</h1>
            <p className="lic-subtitle">Your organization's purchased modules and license usage.</p>
          </div>
        </div>

        {/* Hierarchy banner */}
        <div className="lic-hierarchy-banner">
          <Info size={15} style={{ flexShrink: 0, color: 'var(--color-primary)' }} />
          <span>
            <span className="lic-hierarchy-chain">
              Seller <ArrowRight size={12} className="lic-hierarchy-sep" /> Organization <ArrowRight size={12} className="lic-hierarchy-sep" /> Org Admin <ArrowRight size={12} className="lic-hierarchy-sep" /> Users
            </span>
            {' '}— this page is read-only. Your Seller controls which modules are purchased, plan limits and expiration.
            You control <Link to="/app/admin/users" style={{ color: 'var(--color-primary)', fontWeight: 600 }}>which users</Link> get access to purchased modules.
          </span>
        </div>

        {/* Trial banner */}
        {status.is_trial && (
          <div className="lic-trial-banner">
            <Clock size={16} style={{ flexShrink: 0, color: 'var(--color-warning-dark)' }} />
            <span>
              You're on a <strong>Free Trial</strong> — started {formatDate(status.trial_start)}, ends {formatDate(status.trial_end)}
              {status.days_remaining != null && <> ({status.days_remaining} day{status.days_remaining === 1 ? '' : 's'} remaining)</>}.
            </span>
          </div>
        )}

        {/* License Dashboard */}
        <div className="lic-card">
          <div className="lic-card-title"><ShieldCheck size={16} /> License Dashboard</div>
          <div className="lic-dashboard-grid">
            <div className="lic-dash-item">
              <span className="lic-dash-label">License Status</span>
              <StatusBadge variant={STATUS_VARIANT[dashboard.license_status] ?? 'gray'} dot>{STATUS_LABEL[dashboard.license_status] ?? dashboard.license_status}</StatusBadge>
            </div>
            <div className="lic-dash-item">
              <span className="lic-dash-label">Plan</span>
              <span className="lic-dash-value lic-dash-value-sm">{dashboard.plan_label}</span>
            </div>
            <div className="lic-dash-item">
              <span className="lic-dash-label">Start Date</span>
              <span className="lic-dash-value lic-dash-value-sm">{formatDate(dashboard.start_date)}</span>
            </div>
            <div className="lic-dash-item">
              <span className="lic-dash-label">Expiry Date</span>
              <span className="lic-dash-value lic-dash-value-sm">{dashboard.expiry_date ? formatDate(dashboard.expiry_date) : '—'}</span>
            </div>
            <div className="lic-dash-item">
              <span className="lic-dash-label">Days Remaining</span>
              <span className="lic-dash-value">{dashboard.days_remaining ?? '—'}</span>
            </div>
            <div className="lic-dash-item">
              <span className="lic-dash-label">Users Allowed</span>
              <span className="lic-dash-value">{dashboard.users_allowed}</span>
            </div>
            <div className="lic-dash-item">
              <span className="lic-dash-label">Users Used</span>
              <span className="lic-dash-value">{dashboard.users_used}</span>
              <span className="lic-dash-sub">{Math.max(0, dashboard.users_allowed - dashboard.users_used)} seat{dashboard.users_allowed - dashboard.users_used === 1 ? '' : 's'} left</span>
            </div>
            <div className="lic-dash-item">
              <span className="lic-dash-label">Modules Purchased</span>
              <span className="lic-dash-value">{dashboard.modules_purchased} <span className="lic-dash-sub">/ {dashboard.modules_total}</span></span>
            </div>
          </div>
        </div>

        {/* Usage */}
        <div className="lic-card">
          <div className="lic-card-title"><Gauge size={16} /> Usage</div>
          <div className="lic-usage-grid">
            {Object.entries(dashboard.usage).map(([key, metric]) => (
              <div key={key} className="lic-usage-item">
                <div className="lic-usage-hdr">
                  <span className="lic-usage-label">{USAGE_LABEL[key] ?? key}</span>
                  <span className="lic-usage-nums">
                    {formatNumber(metric.used)}{metric.limit != null ? ` / ${formatNumber(metric.limit)}` : ''}
                  </span>
                </div>
                {metric.limit != null ? (
                  <div className="lic-usage-track">
                    <div className={`lic-usage-fill ${usageFillClass(metric.used, metric.limit)}`} style={{ width: `${Math.min(100, (metric.used / metric.limit) * 100)}%` }} />
                  </div>
                ) : (
                  <span className="lic-usage-unlimited">Unlimited on this plan</span>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Module Licenses */}
        <div className="lic-card-title" style={{ margin: '4px 0 10px' }}><Layers size={16} /> Module Licenses</div>
        <div className="lic-table-panel">
          <DataTable columns={columns} data={modules} />
        </div>
      </div>
    </PermissionGate>
  );
}
