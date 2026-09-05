/**
 * Seller Portal -> Payments — /app/seller/billing/payments (SB26).
 * Real Payment rows from GET /api/billing/payments (backend/app/api/
 * billing/invoices.py) — every row was written by services/billing/
 * payment_service.py::record_payment(), which only ever fires off a
 * real gateway webhook/verification, never a frontend "success" claim.
 */
import { useCallback, useEffect, useState } from 'react';
import { CreditCard } from 'lucide-react';
import { DataTable, EmptyState, StatusBadge } from '@/components/common/index.js';
import { PAGINATION } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { billingInvoicesApi } from '@/services/api/seller/billingInvoicesApi.js';
import './seller-billing.css';

const STATUS_VARIANT = { SUCCEEDED: 'success', PENDING: 'warning', FAILED: 'danger', REFUNDED: 'info', CHARGEBACK: 'danger' };

function fmtCurrency(n, currency = 'USD') {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(n || 0);
}

export default function SellerPayments() {
  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    billingInvoicesApi.listPayments({ page, page_size: pageSize })
      .then((d) => { setRows(d.items); setTotal(d.total); setError(null); })
      .catch((err) => setError(err?.response?.data?.detail || 'Could not load payments.'))
      .finally(() => setLoading(false));
  }, [page, pageSize]);

  useEffect(() => { load(); }, [load]);

  const columns = [
    { key: 'invoice_number', header: 'Invoice #', sortable: true },
    { key: 'organization_name', header: 'Customer', sortable: true },
    { key: 'gateway_provider', header: 'Gateway' },
    { key: 'payment_method', header: 'Method', render: (v) => v || '—' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] || 'gray'} dot>{v}</StatusBadge> },
    { key: 'amount', header: 'Amount', render: (v, row) => fmtCurrency(v, row.currency) },
    { key: 'paid_at', header: 'Paid At', render: (v) => (v ? formatDateTime(v) : '—') },
    { key: 'gateway_transaction_id', header: 'Transaction ID', render: (v) => <code className="sb-txn-id">{v}</code> },
  ];

  return (
    <div className="sb-page">
      <div className="sb-header">
        <h1>Payments</h1>
        <p className="sb-subtitle">Every gateway-verified payment attempt across every customer.</p>
      </div>

      <DataTable
        columns={columns}
        data={rows}
        loading={loading}
        error={error}
        emptyState={<EmptyState icon={<CreditCard size={26} />} title="No payments" description="Successful and failed payment attempts will appear here." />}
        pagination={{ page, pageSize, total, onChange: setPage }}
      />
    </div>
  );
}
