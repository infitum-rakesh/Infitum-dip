/**
 * PayPage — the public Custom Gateway checkout page at /pay/:token
 * (spec sections 17/58's "Payment Link UX", SB16/SB31). No auth, no app
 * shell — a customer reaches this straight from an emailed/shared
 * payment link, exactly like a real hosted-checkout page from any
 * external gateway. Submitting fires POST /api/pay/:token/submit, which
 * itself round-trips through a real signed webhook back into the
 * platform (see backend/app/api/payments/pay.py) rather than faking a
 * success — found live during SB31 that this page never existed at all,
 * even though every "Pay Now"/"Generate Payment Link" action already
 * produced a real, working link to it.
 */
import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { CheckCircle2, CreditCard, Loader2, ShieldCheck, XCircle } from 'lucide-react';
import { payApi } from '@/services/api/payApi.js';
import './pay.css';

function fmtCurrency(n, currency = 'USD') {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(n || 0);
}

export default function PayPage() {
  const { token } = useParams();
  const [invoice, setInvoice] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  const [paid, setPaid] = useState(false);

  useEffect(() => {
    payApi.getInvoice(token)
      .then((d) => { setInvoice(d); setPaid(d.already_paid); })
      .catch((err) => setLoadError(err?.response?.data?.detail || 'This payment link could not be loaded.'))
      .finally(() => setLoading(false));
  }, [token]);

  async function handlePay() {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await payApi.submit(token);
      setPaid(true);
    } catch (err) {
      setSubmitError(err?.response?.data?.detail || 'Payment could not be completed. Please try again.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="pp-wrap">
      <div className="pp-card">
        <div className="pp-brand">
          <div className="pp-brand-mark">DI</div>
          <span>DataIntelligence</span>
        </div>

        {loading && (
          <div className="pp-state"><Loader2 size={20} className="pp-spin" /> Loading your invoice…</div>
        )}

        {!loading && loadError && (
          <div className="pp-state pp-state-error"><XCircle size={22} /> {loadError}</div>
        )}

        {!loading && !loadError && invoice && !paid && (
          <>
            <div className="pp-invoice-head">
              <span className="pp-invoice-label">Invoice</span>
              <span className="pp-invoice-number">{invoice.invoice_number}</span>
            </div>
            <div className="pp-org">{invoice.organization_name}</div>
            <div className="pp-amount">{fmtCurrency(invoice.total, invoice.currency)}</div>

            <button className="pp-pay-btn" onClick={handlePay} disabled={submitting}>
              {submitting ? <Loader2 size={16} className="pp-spin" /> : <CreditCard size={16} />}
              {submitting ? 'Processing…' : `Pay ${fmtCurrency(invoice.total, invoice.currency)}`}
            </button>

            {submitError && <div className="pp-inline-error">{submitError}</div>}

            <div className="pp-secure"><ShieldCheck size={13} /> Secured checkout — DataIntelligence Custom Gateway</div>
          </>
        )}

        {!loading && !loadError && paid && (
          <div className="pp-state pp-state-success">
            <CheckCircle2 size={28} />
            <div className="pp-success-title">Payment received</div>
            <p className="pp-success-body">
              {invoice ? `Invoice ${invoice.invoice_number} is now paid.` : 'This invoice is now paid.'} You can close this page.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
