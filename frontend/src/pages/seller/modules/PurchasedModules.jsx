/**
 * PurchasedModules — /app/seller/purchased-modules (Seller Portal Phase 4).
 * "One of the MOST IMPORTANT Seller screens" per the spec: the module
 * catalog plus per-customer module licensing (enable/disable/configure
 * limits+expiry). Real business-rule enforcement (suspended/expired/
 * missing dependency all block enabling) happens server-side — this page
 * just surfaces whatever the API says, never overrides it client-side.
 */
import { useCallback, useEffect, useState } from 'react';
import { CheckCircle2, XCircle, AlertTriangle, Puzzle, Link2 } from 'lucide-react';
import { StatusBadge, EmptyState, PermissionGate, Button } from '@/components/common/index.js';
import { SELLER_PERMISSIONS } from '@/utils/constants.js';
import { customerApi } from '@/services/api/seller/customerApi.js';
import { moduleApi } from '@/services/api/seller/moduleApi.js';
import { billingInvoicesApi } from '@/services/api/seller/billingInvoicesApi.js';
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
  const [rowMessage, setRowMessage] = useState('');
  const [busyModule, setBusyModule] = useState(null);
  const [payBusyModule, setPayBusyModule] = useState(null);

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

  /** `busyModule` blocks the toggle button for the SAME module while its
   * request is in flight — without this, a seller clicking twice before
   * the first response lands (because nothing visibly changed yet — see
   * below) fired two "enable" requests, and each one created its own
   * Subscription + invoice for the same module (found live: duplicate
   * rows showing up on the Subscriptions screen). The backend now also
   * rejects a second purchase outright (subscription_service.add_module()),
   * but disabling here avoids ever sending the redundant request at all. */
  const toggleModule = async (item) => {
    if (busyModule) return;
    setRowError(''); setRowMessage('');
    setBusyModule(item.module);
    try {
      const result = await moduleApi.setModule(customerId, item.module, { enabled: !item.enabled });
      loadCustomerModules();
      moduleApi.getCatalog().then(setCatalog).catch(() => {});
      // Before this fix, enabling a module gave NO feedback at all — the
      // toggle can stay showing "Disabled" even on success (a paid org's
      // grant starts SUSPENDED until its invoice is paid), which read as
      // "nothing happened" and was exactly what prompted repeated clicks.
      if (!item.enabled) {
        if (result.status === 'ACTIVE') {
          setRowMessage(`${item.label} enabled.`);
        } else if (result.pending_invoice_id) {
          setRowMessage(`${item.label} added — pending payment. An invoice was created; it will activate once paid.`);
        } else {
          setRowMessage(`${item.label} enabled.`);
        }
      } else {
        setRowMessage(`${item.label} disabled.`);
      }
    } catch (err) {
      setRowError(err?.message ?? 'Could not update this module.');
    } finally {
      setBusyModule(null);
    }
  };

  /** Pay for a module's outstanding invoice straight from this row — same
   * popup-blocker-safe pattern as Customers.jsx/SellerInvoices.jsx: open
   * the tab synchronously on click, redirect it once the real URL comes
   * back, and fall back to navigating this tab if the popup was blocked. */
  const payForModule = async (item) => {
    if (!item.pending_invoice_id) return;
    const tab = window.open('', '_blank');
    setPayBusyModule(item.module);
    try {
      const result = await billingInvoicesApi.createPaymentLink(item.pending_invoice_id);
      if (tab) tab.location.href = result.payment_url;
      else window.location.href = result.payment_url;
    } catch (err) {
      if (tab) tab.close();
      setRowError(err?.message ?? 'Could not generate a payment link.');
    } finally {
      setPayBusyModule(null);
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
        {rowMessage && <div className="pm-form-message">{rowMessage}</div>}

        {!customerId ? (
          <EmptyState icon={<Puzzle size={28} />} title="Select a customer" description="Choose a customer above to view and manage their module licenses." />
        ) : loadingCustomerModules ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>Loading…</div>
        ) : customerModules && (
          <div className="pm-table-wrap">
            <table className="pm-table">
              <thead>
                <tr><th>Module</th><th>Category</th><th>Purchased</th><th>Enabled</th><th>Payment</th><th>License Type</th><th>Max Users</th><th>Monthly Credits</th><th>Max Docs</th><th>Expiry</th><th>Actions</th></tr>
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
                        disabled={busyModule === item.module || (!item.enabled && item.dependencies.length > 0 && !item.dependencies_met)}
                        onClick={() => toggleModule(item)}
                        title={selectedCustomerName}
                      >
                        {busyModule === item.module ? 'Saving…' : item.enabled ? 'Enabled' : 'Disabled'}
                      </button>
                    </td>
                    <td>
                      {item.pending_invoice_id ? (
                        <div className="pm-pending-payment">
                          <StatusBadge variant="warning" dot>Pending Payment</StatusBadge>
                          <Button variant="ghost" size="sm" icon={<Link2 size={12} />} loading={payBusyModule === item.module} onClick={() => payForModule(item)}>
                            Pay
                          </Button>
                        </div>
                      ) : '—'}
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
