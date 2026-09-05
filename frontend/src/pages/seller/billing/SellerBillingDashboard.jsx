/**
 * Seller Portal -> Billing Dashboard — /app/seller/billing (SB25, Multi-
 * Subscription Billing & Payments Platform). Every number comes from
 * GET /api/seller/billing/dashboard (backend/app/services/billing/
 * billing_dashboard_service.py), which aggregates the platform's real
 * Invoice/Payment/Subscription/PaymentGateway tables. Nothing here is
 * mocked — an org with no billing activity yet just shows zeros/empty
 * states, not placeholder numbers.
 */
import { useEffect, useState } from 'react';
import {
  DollarSign, TrendingUp, FileWarning, CreditCard, AlertTriangle,
  XCircle, CheckCircle2, Loader2, Zap,
} from 'lucide-react';
import { StatusBadge, EmptyState } from '@/components/common/index.js';
import { billingDashboardApi } from '@/services/api/seller/billingDashboardApi.js';
import '../dashboard/seller-dashboard.css';

function fmtCurrency(n, currency = 'USD') {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(n || 0);
}
function fmtNumber(n) {
  return new Intl.NumberFormat('en-US').format(n || 0);
}
function fmtDate(iso) {
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

const INVOICE_STATUS_VARIANT = {
  PAID: 'success', NO_PAYMENT_REQUIRED: 'success',
  PAYMENT_PENDING: 'warning', PARTIALLY_PAID: 'warning',
  OVERDUE: 'danger', VOID: 'gray', CANCELLED: 'gray', DRAFT: 'gray', ISSUED: 'info',
};
const PAYMENT_STATUS_VARIANT = {
  SUCCEEDED: 'success', PENDING: 'warning', FAILED: 'danger', REFUNDED: 'info', CHARGEBACK: 'danger',
};

export default function SellerBillingDashboard() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    billingDashboardApi.getDashboard()
      .then(setData)
      .catch((err) => setError(err?.response?.data?.detail || err?.message || 'Could not load the Billing Dashboard.'))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return <div className="sd-loading"><Loader2 size={20} className="sd-spin" /> Loading Billing Dashboard…</div>;
  }
  if (error) {
    return <div className="sd-banner error"><AlertTriangle size={15} /> {error}</div>;
  }

  const kpis = [
    { label: 'Total Revenue', value: fmtCurrency(data.total_revenue), icon: DollarSign, color: 'success' },
    { label: 'MRR', value: fmtCurrency(data.mrr), icon: TrendingUp, color: 'primary' },
    { label: 'Outstanding Invoices', value: fmtNumber(data.outstanding_invoices_count), sub: fmtCurrency(data.outstanding_invoices_amount), icon: FileWarning, color: 'warning' },
    { label: 'Active Subscriptions', value: fmtNumber(data.active_subscriptions), icon: CreditCard, color: 'info' },
    { label: 'Expiring Soon', value: fmtNumber(data.expiring_subscriptions), icon: AlertTriangle, color: 'warning' },
    { label: 'Expired', value: fmtNumber(data.expired_subscriptions), icon: XCircle, color: 'danger' },
    {
      label: 'Payment Success Rate (30d)',
      value: data.payment_success_rate === null ? '—' : `${data.payment_success_rate}%`,
      sub: `${data.payments_succeeded_30d} ok / ${data.payments_failed_30d} failed`,
      icon: CheckCircle2, color: 'success',
    },
    { label: 'Active Gateways', value: `${data.active_gateways.length} / ${data.configured_gateways}`, sub: data.active_gateways.join(', ') || 'none active', icon: Zap, color: 'primary' },
  ];

  return (
    <div className="sd-page">
      <div className="sd-header">
        <h1>Billing Dashboard</h1>
        <p className="sd-subtitle">Revenue, subscriptions, invoices and payment health across every customer.</p>
      </div>

      <div className="sd-kpis">
        {kpis.map((k) => {
          const Icon = k.icon;
          return (
            <div className="sd-kpi-card" key={k.label}>
              <div className={`sd-kpi-icon sd-kpi-icon-${k.color}`}><Icon size={17} /></div>
              <div className="sd-kpi-val">{k.value}</div>
              <div className="sd-kpi-label">{k.label}</div>
              {k.sub && <div className="sd-kpi-sub" style={{ marginTop: 4, display: 'block' }}>{k.sub}</div>}
            </div>
          );
        })}
      </div>

      <div className="sd-charts-row">
        <div className="sd-card">
          <div className="sd-card-title">Recent Invoices</div>
          {data.recent_invoices.length === 0 ? (
            <EmptyState icon={<FileWarning size={26} />} title="No invoices yet" description="Invoices will appear here as subscriptions are created, renewed, or changed." />
          ) : (
            <div className="sd-table-wrap">
              <table className="sd-table">
                <thead><tr><th>Invoice</th><th>Customer</th><th>Status</th><th>Total</th><th>Issued</th></tr></thead>
                <tbody>
                  {data.recent_invoices.map((inv) => (
                    <tr key={inv.id}>
                      <td>{inv.invoice_number}</td>
                      <td>{inv.organization_name}</td>
                      <td><StatusBadge variant={INVOICE_STATUS_VARIANT[inv.status] || 'gray'} dot>{inv.status.replace(/_/g, ' ')}</StatusBadge></td>
                      <td>{fmtCurrency(inv.total, inv.currency)}</td>
                      <td>{fmtDate(inv.issue_date)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="sd-card">
          <div className="sd-card-title">Recent Payments</div>
          {data.recent_payments.length === 0 ? (
            <EmptyState icon={<CreditCard size={26} />} title="No payments yet" description="Successful and failed payment attempts will appear here." />
          ) : (
            <div className="sd-table-wrap">
              <table className="sd-table">
                <thead><tr><th>Customer</th><th>Method</th><th>Status</th><th>Amount</th><th>Date</th></tr></thead>
                <tbody>
                  {data.recent_payments.map((p) => (
                    <tr key={p.id}>
                      <td>{p.organization_name}</td>
                      <td>{p.payment_method || '—'}</td>
                      <td><StatusBadge variant={PAYMENT_STATUS_VARIANT[p.status] || 'gray'} dot>{p.status}</StatusBadge></td>
                      <td>{fmtCurrency(p.amount, p.currency)}</td>
                      <td>{fmtDate(p.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
