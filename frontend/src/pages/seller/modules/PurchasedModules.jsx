/**
 * PurchasedModules — /app/seller/purchased-modules (Seller Portal Phase 4).
 * "One of the MOST IMPORTANT Seller screens" per the spec: the module
 * catalog plus per-customer module licensing (enable/disable/configure
 * limits+expiry). Real business-rule enforcement (suspended/expired/
 * missing dependency all block enabling) happens server-side — this page
 * just surfaces whatever the API says, never overrides it client-side.
 */
import { useCallback, useEffect, useState } from 'react';
import { CheckCircle2, XCircle, AlertTriangle, Puzzle } from 'lucide-react';
import { StatusBadge, EmptyState, PermissionGate } from '@/components/common/index.js';
import { SELLER_PERMISSIONS } from '@/utils/constants.js';
import { customerApi } from '@/services/api/seller/customerApi.js';
import { moduleApi } from '@/services/api/seller/moduleApi.js';
import { ConfigureModuleModal } from './ConfigureModuleModal.jsx';
import './purchased-modules.css';

export default function PurchasedModules() {
  const [catalog, setCatalog] = useState([]);
  const [customers, setCustomers] = useState([]);
  const [customerId, setCustomerId] = useState('');
  const [customerModules, setCustomerModules] = useState(null);
  const [loadingCustomerModules, setLoadingCustomerModules] = useState(false);
  const [configureTarget, setConfigureTarget] = useState(null);
  const [rowError, setRowError] = useState('');

  useEffect(() => {
    moduleApi.getCatalog().then(setCatalog).catch(() => {});
    customerApi.list({ page_size: 200 }).then((r) => setCustomers(r.items)).catch(() => {});
  }, []);

  const loadCustomerModules = useCallback(() => {
    if (!customerId) { setCustomerModules(null); return; }
    setLoadingCustomerModules(true);
    moduleApi.getCustomerModules(customerId).then(setCustomerModules).catch(() => setCustomerModules(null)).finally(() => setLoadingCustomerModules(false));
  }, [customerId]);

  useEffect(() => { loadCustomerModules(); }, [loadCustomerModules]);

  const toggleModule = async (item) => {
    setRowError('');
    try {
      await moduleApi.setModule(customerId, item.module, { enabled: !item.enabled });
      loadCustomerModules();
      moduleApi.getCatalog().then(setCatalog).catch(() => {});
    } catch (err) {
      setRowError(err?.message ?? 'Could not update this module.');
    }
  };

  const selectedCustomerName = customers.find((c) => c.id === Number(customerId))?.name;

  return (
    <PermissionGate {...SELLER_PERMISSIONS.SELLER_PORTAL_ACCESS} showReason>
      <div className="pm-page">
        <div className="pm-header">
          <h1>Purchased Modules</h1>
          <p className="pm-subtitle">Control which platform modules are licensed to each customer.</p>
        </div>

        <div className="pm-section-title">Module Catalog</div>
        <div className="pm-table-wrap">
          <table className="pm-table">
            <thead>
              <tr><th>Module</th><th>Category</th><th>Version</th><th>Available</th><th>Customers</th><th>License Type</th><th>Status</th><th>Dependencies</th></tr>
            </thead>
            <tbody>
              {catalog.map((m) => (
                <tr key={m.module}>
                  <td>{m.label}</td>
                  <td><span className="pm-category-tag">{m.category}</span></td>
                  <td>{m.version}</td>
                  <td>{m.available ? <StatusBadge variant="success" dot>Yes</StatusBadge> : <StatusBadge variant="gray">No</StatusBadge>}</td>
                  <td>{m.customers}</td>
                  <td>{m.license_type.replace(/_/g, ' ')}</td>
                  <td><StatusBadge variant="success" dot>{m.status}</StatusBadge></td>
                  <td><span className="pm-deps">{m.dependencies.length ? m.dependencies.join(', ') : '—'}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="pm-section-title">Customer Module Assignment</div>
        <div className="pm-customer-picker">
          <select className="pm-input" value={customerId} onChange={(e) => setCustomerId(e.target.value)}>
            <option value="">Select a customer…</option>
            {customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </div>

        {rowError && <div className="pm-form-error">{rowError}</div>}

        {!customerId ? (
          <EmptyState icon={<Puzzle size={28} />} title="Select a customer" description="Choose a customer above to view and manage their module licenses." />
        ) : loadingCustomerModules ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>Loading…</div>
        ) : customerModules && (
          <div className="pm-table-wrap">
            <table className="pm-table">
              <thead>
                <tr><th>Module</th><th>Category</th><th>Purchased</th><th>Enabled</th><th>License Type</th><th>Max Users</th><th>Monthly Credits</th><th>Max Docs</th><th>Expiry</th><th>Actions</th></tr>
              </thead>
              <tbody>
                {customerModules.items.map((item) => (
                  <tr key={item.module}>
                    <td>
                      {item.label}
                      {item.dependencies.length > 0 && !item.dependencies_met && (
                        <div className="pm-dep-warning"><AlertTriangle size={11} /> Needs {item.dependencies.join(', ')}</div>
                      )}
                    </td>
                    <td><span className="pm-category-tag">{item.category}</span></td>
                    <td>{item.purchased ? <CheckCircle2 size={15} color="var(--color-success)" /> : <XCircle size={15} color="var(--text-faint)" />}</td>
                    <td>
                      <button
                        className={`pm-toggle-btn ${item.enabled ? 'on' : 'off'}`}
                        disabled={!item.enabled && item.dependencies.length > 0 && !item.dependencies_met}
                        onClick={() => toggleModule(item)}
                        title={selectedCustomerName}
                      >
                        {item.enabled ? 'Enabled' : 'Disabled'}
                      </button>
                    </td>
                    <td>{item.license_type.replace(/_/g, ' ')}</td>
                    <td>{item.max_users ?? '∞'}</td>
                    <td>{item.monthly_credits ?? '∞'}</td>
                    <td>{item.max_documents ?? '∞'}</td>
                    <td>{item.expiry_date ? item.expiry_date.slice(0, 10) : '—'}</td>
                    <td><button className="pm-configure-btn" onClick={() => setConfigureTarget(item)}>Configure</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {configureTarget && (
        <ConfigureModuleModal
          customerId={customerId} item={configureTarget}
          onClose={() => setConfigureTarget(null)}
          onSaved={() => { setConfigureTarget(null); loadCustomerModules(); moduleApi.getCatalog().then(setCatalog).catch(() => {}); }}
        />
      )}
    </PermissionGate>
  );
}
