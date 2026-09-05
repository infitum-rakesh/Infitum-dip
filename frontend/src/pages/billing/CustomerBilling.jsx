/**
 * Customer Billing — /app/billing (spec section 45's Customer Billing
 * screens, SB29). Org-scoped, read-only Subscriptions/Invoices/Payments
 * views + a self-service "Pay Now" action, all backed by real rows from
 * backend/app/api/billing/*.py (automatically scoped to the caller's own
 * organization server-side — see _require_customer_org()/_require_org_
 * scoped() there). Nothing here is mocked.
 */
import { useCallback, useEffect, useState } from 'react';
import { CreditCard, Download, ExternalLink, Loader2, Receipt, Wallet } from 'lucide-react';
import { Button, DataTable, EmptyState, Modal, StatusBadge } from '@/components/common/index.js';
import { PAGINATION } from '@/utils/constants.js';
import { formatDate, formatDateTime } from '@/utils/formatters.js';
import { billingApi } from '@/services/api/billingApi.js';
import './customer-billing.css';

const SUB_STATUS_VARIANT = { TRIAL: 'info', ACTIVE: 'success', EXPIRING: 'warning', PAST_DUE: 'warning', PAYMENT_PENDING: 'warning', EXPIRED: 'gray', SUSPENDED: 'danger', CANCELLED: 'gray', TERMINATED: 'gray', PENDING: 'info' };
const INVOICE_STATUS_VARIANT = { PAID: 'success', NO_PAYMENT_REQUIRED: 'success', PAYMENT_PENDING: 'warning', PARTIALLY_PAID: 'warning', OVERDUE: 'danger', VOID: 'gray', CANCELLED: 'gray', DRAFT: 'gray', ISSUED: 'info' };
const PAYMENT_STATUS_VARIANT = { SUCCEEDED: 'success', PENDING: 'warning', FAILED: 'danger', REFUNDED: 'info', CHARGEBACK: 'danger' };

const TABS = [
  { id: 'subscriptions', label: 'Subscriptions', icon: Wallet },
  { id: 'invoices', label: 'Invoices', icon: Receipt },
  { id: 'payments', label: 'Payments', icon: CreditCard },
];

function fmtCurrency(n, currency = 'USD') {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(n || 0);
}
function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

export default function CustomerBilling() {
  const [tab, setTab] = useState('subscriptions');

  return (
    <div className="cb-page">
      <div className="cb-header">
        <h1>Billing</h1>
        <p className="cb-subtitle">Your organization's subscriptions, invoices, and payments.</p>
      </div>

      <div className="cb-tab-nav">
        {TABS.map((t) => {
          const Icon = t.icon;
          return (
            <button key={t.id} className={`cb-tab-btn${tab === t.id ? ' active' : ''}`} onClick={() => setTab(t.id)}>
              <Icon size={14} /> {t.label}
            </button>
          );
        })}
      </div>

      {tab === 'subscriptions' && <SubscriptionsTab />}
      {tab === 'invoices' && <InvoicesTab />}
      {tab === 'payments' && <PaymentsTab />}
    </div>
  );
}

function SubscriptionsTab() {
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  const [modulesFor, setModulesFor] = useState(null);
  const [modules, setModules] = useState(null);
  const [modulesLoading, setModulesLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    billingApi.listMySubscriptions({ page, page_size: pageSize })
      .then((d) => { setRows(d.items); setTotal(d.total); setError(null); })
      .catch((err) => setError(err?.response?.data?.detail || 'Could not load subscriptions.'))
      .finally(() => setLoading(false));
  }, [page, pageSize]);

  function openModules(row) {
    setModulesFor(row);
    setModules(null);
    setModulesLoading(true);
    billingApi.getSubscriptionModules(row.id).then((d) => setModules(d.items)).finally(() => setModulesLoading(false));
  }

  const columns = [
    { key: 'plan_name', header: 'Plan', render: (v) => v || 'Custom' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={SUB_STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'start_date', header: 'Start', render: (v) => formatDate(v) },
    { key: 'end_date', header: 'Renews / Expires', render: (v) => (v ? formatDate(v) : '—') },
    { key: 'billing_cycle', header: 'Cycle' },
    { key: 'amount', header: 'Amount', render: (v, row) => fmtCurrency(v, row.currency) },
    {
      key: 'payment_status', header: 'Payment',
      render: (v) => (v ? <StatusBadge variant={INVOICE_STATUS_VARIANT[v] ?? 'gray'} dot>{v.replace(/_/g, ' ')}</StatusBadge> : <span className="cb-muted">No invoices yet</span>),
    },
  ];

  return (
    <>
      <DataTable
        columns={columns}
        data={rows}
        loading={loading}
        error={error}
        onRowClick={openModules}
        emptyState={<EmptyState icon={<Wallet size={26} />} title="No subscriptions" description="Your organization doesn't have any subscriptions yet. Contact Sales to get started." />}
        pagination={{ page, pageSize, total, onChange: setPage }}
      />

      {modulesFor && (
        <Modal open onClose={() => setModulesFor(null)} title={`Modules — ${modulesFor.plan_name || 'Custom Subscription'}`} size="md">
          {modulesLoading || !modules ? (
            <div className="cb-modal-loading"><Loader2 size={18} className="cb-spin" /> Loading…</div>
          ) : modules.length === 0 ? (
            <EmptyState icon={<Wallet size={22} />} title="No modules" description="This subscription doesn't grant any modules." />
          ) : (
            <table className="cb-modules-table">
              <thead><tr><th>Module</th><th>Status</th><th>Enabled</th><th>Expires</th></tr></thead>
              <tbody>
                {modules.map((m) => (
                  <tr key={m.module}>
                    <td>{m.module.replace(/_/g, ' ')}</td>
                    <td><StatusBadge variant={m.status === 'ACTIVE' ? 'success' : m.status === 'SUSPENDED' ? 'warning' : 'gray'} dot>{m.status}</StatusBadge></td>
                    <td>{m.enabled ? 'Yes' : 'No'}</td>
                    <td>{m.expiry_date ? formatDate(m.expiry_date) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Modal>
      )}
    </>
  );
}

function InvoicesTab() {
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  const [detailId, setDetailId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [pdfBusy, setPdfBusy] = useState(false);
  const [payBusy, setPayBusy] = useState(false);
  const [payResult, setPayResult] = useState(null);
  const [payError, setPayError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    billingApi.listInvoices({ page, page_size: pageSize })
      .then((d) => { setRows(d.items); setTotal(d.total); setError(null); })
      .catch((err) => setError(err?.response?.data?.detail || 'Could not load invoices.'))
      .finally(() => setLoading(false));
  }, [page, pageSize]);

  useEffect(() => { load(); }, [load]);

  function openDetail(row) {
    setDetailId(row.id);
    setDetail(null);
    setPayResult(null);
    setPayError(null);
    setDetailLoading(true);
    billingApi.getInvoice(row.id).then(setDetail).finally(() => setDetailLoading(false));
  }

  async function handleDownloadPdf(id, invoiceNumber) {
    setPdfBusy(true);
    try {
      const blob = await billingApi.downloadInvoicePdf(id);
      downloadBlob(blob, `${invoiceNumber}.pdf`);
    } finally {
      setPdfBusy(false);
    }
  }

  async function handlePayNow(id) {
    setPayBusy(true);
    setPayError(null);
    try {
      const result = await billingApi.payInvoice(id);
      setPayResult(result);
    } catch (err) {
      setPayError(err?.response?.data?.detail || 'Could not start payment.');
    } finally {
      setPayBusy(false);
    }
  }

  const columns = [
    { key: 'invoice_number', header: 'Invoice #', sortable: true },
    { key: 'reason', header: 'Reason' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={INVOICE_STATUS_VARIANT[v] || 'gray'} dot>{v.replace(/_/g, ' ')}</StatusBadge> },
    { key: 'total', header: 'Total', render: (v, row) => fmtCurrency(v, row.currency) },
    { key: 'issue_date', header: 'Issued', render: (v) => formatDate(v) },
    { key: 'due_date', header: 'Due', render: (v) => (v ? formatDate(v) : '—') },
  ];

  const canPay = detail && detail.status !== 'PAID' && detail.status !== 'NO_PAYMENT_REQUIRED' && detail.status !== 'VOID' && detail.status !== 'CANCELLED';

  return (
    <>
      <DataTable
        columns={columns}
        data={rows}
        loading={loading}
        error={error}
        onRowClick={openDetail}
        emptyState={<EmptyState icon={<Receipt size={26} />} title="No invoices" description="Invoices generated by your subscription lifecycle will appear here." />}
        pagination={{ page, pageSize, total, onChange: setPage }}
      />

      {detailId && (
        <Modal open onClose={() => setDetailId(null)} title="Invoice" size="lg">
          {detailLoading || !detail ? (
            <div className="cb-modal-loading"><Loader2 size={18} className="cb-spin" /> Loading…</div>
          ) : (
            <div className="cb-invoice-detail">
              <div className="cb-invoice-detail-head">
                <div>
                  <div className="cb-invoice-number">{detail.invoice_number}</div>
                  <div className="cb-invoice-reason">{detail.reason}</div>
                </div>
                <StatusBadge variant={INVOICE_STATUS_VARIANT[detail.status] || 'gray'} dot>{detail.status.replace(/_/g, ' ')}</StatusBadge>
              </div>

              <div className="cb-invoice-meta-grid">
                <div><span>Issued</span><strong>{formatDate(detail.issue_date)}</strong></div>
                <div><span>Due</span><strong>{detail.due_date ? formatDate(detail.due_date) : '—'}</strong></div>
                <div><span>Paid</span><strong>{detail.paid_at ? formatDateTime(detail.paid_at) : '—'}</strong></div>
              </div>

              <table className="cb-line-items">
                <thead><tr><th>Item</th><th>Qty</th><th>Unit Price</th><th>Total</th></tr></thead>
                <tbody>
                  {detail.line_items.map((li) => (
                    <tr key={li.id}>
                      <td>{li.description}</td>
                      <td>{li.quantity}</td>
                      <td>{fmtCurrency(li.unit_price, detail.currency)}</td>
                      <td>{fmtCurrency(li.total, detail.currency)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr><td colSpan={3}>Subtotal</td><td>{fmtCurrency(detail.subtotal, detail.currency)}</td></tr>
                  {detail.tax > 0 && <tr><td colSpan={3}>Tax</td><td>{fmtCurrency(detail.tax, detail.currency)}</td></tr>}
                  <tr className="cb-line-items-total"><td colSpan={3}>Total</td><td>{fmtCurrency(detail.total, detail.currency)}</td></tr>
                </tfoot>
              </table>

              <div className="cb-invoice-actions">
                <Button variant="secondary" icon={<Download size={14} />} onClick={() => handleDownloadPdf(detail.id, detail.invoice_number)} disabled={pdfBusy}>
                  {pdfBusy ? 'Downloading…' : 'Download PDF'}
                </Button>
                {canPay && (
                  <Button variant="primary" icon={<Wallet size={14} />} onClick={() => handlePayNow(detail.id)} disabled={payBusy}>
                    {payBusy ? 'Starting…' : 'Pay Now'}
                  </Button>
                )}
              </div>

              {payError && <div className="cb-inline-error">{payError}</div>}
              {payResult && (
                <div className="cb-payment-link-result">
                  <span>Complete your payment (link expires {formatDateTime(payResult.expires_at)}):</span>
                  <a href={payResult.payment_url} target="_blank" rel="noreferrer" className="cb-pay-link">
                    Open Payment Page <ExternalLink size={13} />
                  </a>
                </div>
              )}
            </div>
          )}
        </Modal>
      )}
    </>
  );
}

function PaymentsTab() {
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  useEffect(() => {
    setLoading(true);
    billingApi.listPayments({ page, page_size: pageSize })
      .then((d) => { setRows(d.items); setTotal(d.total); setError(null); })
      .catch((err) => setError(err?.response?.data?.detail || 'Could not load payments.'))
      .finally(() => setLoading(false));
  }, [page, pageSize]);

  const columns = [
    { key: 'invoice_number', header: 'Invoice #', sortable: true },
    { key: 'gateway_provider', header: 'Gateway' },
    { key: 'payment_method', header: 'Method', render: (v) => v || '—' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={PAYMENT_STATUS_VARIANT[v] || 'gray'} dot>{v}</StatusBadge> },
    { key: 'amount', header: 'Amount', render: (v, row) => fmtCurrency(v, row.currency) },
    { key: 'paid_at', header: 'Paid At', render: (v) => (v ? formatDateTime(v) : '—') },
  ];

  return (
    <DataTable
      columns={columns}
      data={rows}
      loading={loading}
      error={error}
      emptyState={<EmptyState icon={<CreditCard size={26} />} title="No payments" description="Your successful and failed payment attempts will appear here." />}
      pagination={{ page, pageSize, total, onChange: setPage }}
    />
  );
}
