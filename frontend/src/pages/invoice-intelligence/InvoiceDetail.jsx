/**
 * InvoiceDetail — /app/invoice-intel/:invoiceId
 *
 * Two tabs: Overview (INV14 — header fields, lines, validation/decision
 * reasons, confidence scores, ERP/reconciliation status, lifecycle
 * timeline, and now a real Correction History card from GET
 * /invoices/{id}/audit) and Evidence (INV15's split-screen document + bbox
 * viewer, now field-correction-capable — INV16). Corrections submitted
 * from the Evidence tab's field rows POST to the real /invoices/{id}/
 * corrections route (invoiceApi.applyCorrections), the exact same
 * review_queue_service.update_field()+approve() path every other document
 * type's corrections already use — this doesn't open a second, invoice-only
 * correction mechanism, and it genuinely re-runs materialize/validate/
 * decide server-side, not a client-side simulation. Reprocess/Revalidate
 * remain wired here from INV14.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, Receipt, RefreshCw, RotateCcw, ListChecks, History, FileStack, Truck, ScanEye, ScrollText, Eye, Send, XCircle } from 'lucide-react';
import { Button, StatusBadge, PermissionGate, LoadingSpinner, EmptyState } from '@/components/common/index.js';
import { INVOICE_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { invoiceApi } from '@/services/api/invoiceApi.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import InvoiceEvidenceViewer from './InvoiceEvidenceViewer.jsx';
import './invoice-intelligence.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

function formatCurrency(n, currency = 'USD') {
  if (n === null || n === undefined) return '—';
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(n);
  } catch {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(n);
  }
}

const STATUS_VARIANT = { NEEDS_REVIEW: 'warning', COMPLETED: 'success', REJECTED: 'danger', FAILED: 'danger' };
const DECISION_VARIANT = { STRAIGHT_THROUGH: 'success', REVIEW_REQUIRED: 'warning', REJECT: 'danger', RETRY: 'info' };
const DECISION_LABEL = { STRAIGHT_THROUGH: 'Straight-Through', REVIEW_REQUIRED: 'Review Required', REJECT: 'Rejected', RETRY: 'Retry' };
const ERP_VARIANT = { NOT_SUBMITTED: 'gray', PENDING: 'info', RETRYING: 'warning', SUCCEEDED: 'success', FAILED: 'danger', DEAD_LETTER: 'danger', CANCELLED: 'gray' };
// erp_status values a "Post to ERP" click is allowed from — mirrors
// invoice_erp_service.post_invoice()'s own pre-call guard (SUCCEEDED and
// an in-flight PENDING/RETRYING are both rejected server-side, INV18).
const ERP_POSTABLE = new Set(['NOT_SUBMITTED', 'FAILED', 'DEAD_LETTER', 'CANCELLED']);
const RECON_VARIANT = { NOT_STARTED: 'gray', RECONCILED: 'success', MISMATCH: 'danger', FAILED: 'danger' };
const VALIDATION_VARIANT = { PASS: 'success', WARNING: 'warning', FAIL: 'danger' };

const SCORE_FIELDS = [
  ['extractor_confidence', 'Extractor Confidence'],
  ['evidence_quality', 'Evidence Quality'],
  ['field_consistency', 'Field Consistency'],
  ['reference_match_score', 'Reference Match'],
  ['business_validation_score', 'Business Validation'],
];

function scoreColor(v) {
  if (v == null) return 'var(--text-faint)';
  if (v >= 90) return 'var(--color-success)';
  if (v >= 60) return 'var(--color-warning)';
  return 'var(--color-danger)';
}

export default function InvoiceDetail() {
  const { invoiceId } = useParams();
  const navigate = useNavigate();

  const [invoice, setInvoice] = useState(null);
  const [events, setEvents] = useState([]);
  const [evidence, setEvidence] = useState(null);
  const [audit, setAudit] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [actionBusy, setActionBusy] = useState(null); // 'reprocess' | 'revalidate' | null
  const [actionError, setActionError] = useState(null);
  const [tab, setTab] = useState('overview'); // 'overview' | 'evidence'

  // INV18 — ERP posting panel: connection picker, dry-run preview, post/
  // cancel actions, and the real transaction history for this invoice.
  const [erpConnections, setErpConnections] = useState([]);
  const [erpConnectionId, setErpConnectionId] = useState('');
  const [erpTransactions, setErpTransactions] = useState([]);
  const [erpPreview, setErpPreview] = useState(null);
  const [erpBusy, setErpBusy] = useState(null); // 'preview' | 'post' | 'cancel' | null
  const [erpError, setErpError] = useState(null);

  // INV16 — field-correction drafts, keyed by field name, plus which
  // field's Save is currently in flight and the last correction error.
  const [drafts, setDrafts] = useState({});
  const [savingField, setSavingField] = useState(null);
  const [correctionError, setCorrectionError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    Promise.all([
      invoiceApi.getInvoice(invoiceId),
      invoiceApi.getLifecycleEvents(invoiceId).catch(() => []),
      invoiceApi.getEvidence(invoiceId).catch(() => null),
      invoiceApi.getAudit(invoiceId).catch(() => []),
      invoiceApi.listErpTransactions(invoiceId).catch(() => []),
    ])
      .then(([inv, evs, ev, aud, txns]) => { setInvoice(inv); setEvents(evs); setEvidence(ev); setAudit(aud); setErpTransactions(txns); })
      .catch((err) => setError(apiErrorMessage(err, 'Could not load this invoice.')))
      .finally(() => setLoading(false));
  }, [invoiceId]);

  useEffect(() => { load(); }, [load]);

  // ERP connections load once, independent of invoice reloads — same
  // ORACLE_ERP_CLOUD filter InvoiceSettings.jsx's MappingsTab uses.
  useEffect(() => {
    connectionApi.list({ category: 'APPLICATION', page_size: 200 })
      .then((r) => {
        const erp = (r.items ?? []).filter((c) => c.connection_type === 'ORACLE_ERP_CLOUD');
        setErpConnections(erp);
        if (erp.length) setErpConnectionId(String(erp[0].id));
      })
      .catch(() => {});
  }, []);

  const handleDraftChange = (fieldName, value) => setDrafts((d) => ({ ...d, [fieldName]: value }));

  const handleSaveCorrection = async (fieldName) => {
    const correctedValue = drafts[fieldName];
    if (correctedValue == null) return;
    setSavingField(fieldName);
    setCorrectionError(null);
    try {
      await invoiceApi.applyCorrections(invoiceId, [
        { field_name: fieldName, corrected_value: correctedValue, reason: 'Corrected via Invoice Detail — Evidence tab' },
      ]);
      setDrafts((d) => { const next = { ...d }; delete next[fieldName]; return next; });
      load();
    } catch (err) {
      setCorrectionError(apiErrorMessage(err, `Could not save the correction for "${fieldName}".`));
    } finally {
      setSavingField(null);
    }
  };

  const handleReprocess = async () => {
    setActionBusy('reprocess'); setActionError(null);
    try {
      await invoiceApi.reprocess(invoiceId);
      await invoiceApi.pollForInvoice(invoice.document_id).catch(() => {});
      load();
    } catch (err) {
      setActionError(apiErrorMessage(err, 'Reprocess failed.'));
    } finally {
      setActionBusy(null);
    }
  };

  const handleRevalidate = async () => {
    setActionBusy('revalidate'); setActionError(null);
    try {
      await invoiceApi.revalidate(invoiceId);
      load();
    } catch (err) {
      setActionError(apiErrorMessage(err, 'Revalidate failed.'));
    } finally {
      setActionBusy(null);
    }
  };

  // INV18 — real dry-run: builds the exact payload the current mapping
  // would send, no network call, no transaction row written.
  const handlePreviewErp = async () => {
    if (!erpConnectionId) return;
    setErpBusy('preview'); setErpError(null); setErpPreview(null);
    try {
      const data = await invoiceApi.previewErpPost(invoiceId, Number(erpConnectionId));
      setErpPreview(data);
    } catch (err) {
      setErpError(apiErrorMessage(err, 'Could not build an ERP preview.'));
    } finally {
      setErpBusy(null);
    }
  };

  // Real post — the backend hands back the PENDING transaction row
  // immediately and finishes the retry loop in the background, so this
  // reloads once right away (shows PENDING) and again after a few
  // seconds to pick up the real outcome without the user having to
  // manually refresh.
  const handlePostErp = async () => {
    if (!erpConnectionId) return;
    setErpBusy('post'); setErpError(null); setErpPreview(null);
    try {
      await invoiceApi.postToErp(invoiceId, Number(erpConnectionId));
      load();
      setTimeout(load, 3000);
    } catch (err) {
      setErpError(apiErrorMessage(err, 'Could not post this invoice to the ERP.'));
    } finally {
      setErpBusy(null);
    }
  };

  const handleCancelErp = async () => {
    setErpBusy('cancel'); setErpError(null);
    try {
      await invoiceApi.cancelErpPosting(invoiceId);
      load();
    } catch (err) {
      setErpError(apiErrorMessage(err, 'Could not cancel this ERP posting.'));
    } finally {
      setErpBusy(null);
    }
  };

  if (loading) return <div style={{ padding: 60, textAlign: 'center' }}><LoadingSpinner /></div>;
  if (error || !invoice) {
    return (
      <div className="invd-page">
        <button className="invd-back" onClick={() => navigate('/app/invoice-intel')}><ArrowLeft size={14} /> Back to Invoice Inbox</button>
        <EmptyState icon={<Receipt size={28} />} title="Could not load invoice" description={error || 'This invoice does not exist.'} />
      </div>
    );
  }

  return (
    <PermissionGate {...INVOICE_PERMISSIONS.INVOICE_VIEW} showReason>
      <div className="invd-page">
        <button className="invd-back" onClick={() => navigate('/app/invoice-intel')}><ArrowLeft size={14} /> Back to Invoice Inbox</button>

        <div className="invd-header">
          <div>
            <h1 className="invd-title">
              <Receipt size={22} /> {invoice.invoice_number || `Invoice #${invoice.id}`}
              <StatusBadge variant={STATUS_VARIANT[invoice.status] ?? 'gray'} dot>{invoice.status?.replace('_', ' ')}</StatusBadge>
              {invoice.final_decision && (
                <StatusBadge variant={DECISION_VARIANT[invoice.final_decision] ?? 'gray'} dot>
                  {DECISION_LABEL[invoice.final_decision] ?? invoice.final_decision}
                </StatusBadge>
              )}
            </h1>
            <p className="invd-subtitle">{invoice.vendor_name || 'Unknown vendor'} · {invoice.document_filename || 'source document'}</p>
          </div>
          <div className="invd-actions">
            <PermissionGate {...INVOICE_PERMISSIONS.INVOICE_RUN} fallback={null}>
              <Button variant="secondary" size="sm" icon={<ListChecks size={14} />} loading={actionBusy === 'revalidate'} onClick={handleRevalidate}>
                Revalidate
              </Button>
              <Button variant="secondary" size="sm" icon={<RefreshCw size={14} />} loading={actionBusy === 'reprocess'} onClick={handleReprocess}>
                Reprocess
              </Button>
            </PermissionGate>
          </div>
        </div>

        {actionError && <div className="invd-card" style={{ borderColor: 'var(--color-danger)', color: 'var(--color-danger-dark)', fontSize: 'var(--fs-sm)' }}>{actionError}</div>}

        <div className="invd-tab-nav">
          <button className={`invd-tab-btn ${tab === 'overview' ? 'active' : ''}`} onClick={() => setTab('overview')}>
            <FileStack size={14} /> Overview
          </button>
          <button className={`invd-tab-btn ${tab === 'evidence' ? 'active' : ''}`} onClick={() => setTab('evidence')}>
            <ScanEye size={14} /> Evidence
          </button>
        </div>

        {tab === 'evidence' && (
          <>
            {correctionError && (
              <div className="invd-card" style={{ borderColor: 'var(--color-danger)', color: 'var(--color-danger-dark)', fontSize: 'var(--fs-sm)' }}>
                {correctionError}
              </div>
            )}
            <InvoiceEvidenceViewer
              documentId={invoice.document_id}
              mimeType={invoice.document_mime_type}
              fields={evidence?.fields ?? []}
              drafts={drafts}
              onDraftChange={handleDraftChange}
              onSaveCorrection={handleSaveCorrection}
              savingField={savingField}
              canEdit
            />
          </>
        )}

        {tab === 'overview' && (
        <div className="invd-grid">
          <div>
            {/* Header fields */}
            <div className="invd-card">
              <div className="invd-card-title"><FileStack size={14} /> Invoice Details</div>
              <div className="invd-field-grid">
                <div><div className="invd-field-label">Invoice Number</div><div className="invd-field-value">{invoice.invoice_number || '—'}</div></div>
                <div><div className="invd-field-label">Vendor</div><div className="invd-field-value">{invoice.vendor_name || '—'}</div></div>
                <div><div className="invd-field-label">Invoice Date</div><div className="invd-field-value">{invoice.invoice_date ? formatDateTime(invoice.invoice_date).split(',')[0] : '—'}</div></div>
                <div><div className="invd-field-label">Due Date</div><div className="invd-field-value">{invoice.due_date ? formatDateTime(invoice.due_date).split(',')[0] : '—'}</div></div>
                <div><div className="invd-field-label">Purchase Order</div><div className="invd-field-value">{invoice.purchase_order || '—'}</div></div>
                <div><div className="invd-field-label">Payment Terms</div><div className="invd-field-value">{invoice.payment_terms || '—'}</div></div>
                <div><div className="invd-field-label">Subtotal</div><div className="invd-field-value mono">{formatCurrency(invoice.subtotal, invoice.currency)}</div></div>
                <div><div className="invd-field-label">Tax</div><div className="invd-field-value mono">{formatCurrency(invoice.tax, invoice.currency)}</div></div>
                <div><div className="invd-field-label">Discount</div><div className="invd-field-value mono">{formatCurrency(invoice.discount, invoice.currency)}</div></div>
                <div><div className="invd-field-label">Freight</div><div className="invd-field-value mono">{formatCurrency(invoice.freight, invoice.currency)}</div></div>
                <div><div className="invd-field-label">Total</div><div className="invd-field-value mono" style={{ fontWeight: 700 }}>{formatCurrency(invoice.total, invoice.currency)}</div></div>
                <div><div className="invd-field-label">Duplicate Check</div><div className="invd-field-value">{invoice.duplicate_check_result || '—'}</div></div>
              </div>
            </div>

            {/* Line items */}
            <div className="invd-card">
              <div className="invd-card-title"><ListChecks size={14} /> Line Items ({invoice.lines?.length || 0})</div>
              {invoice.lines?.length ? (
                <table className="invd-lines-table">
                  <thead>
                    <tr>
                      <th>#</th><th>Description</th><th>PO Line</th>
                      <th style={{ textAlign: 'right' }}>Qty</th><th style={{ textAlign: 'right' }}>Unit Price</th>
                      <th style={{ textAlign: 'right' }}>Tax</th><th style={{ textAlign: 'right' }}>Line Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {invoice.lines.map((l) => (
                      <tr key={l.id}>
                        <td>{l.line_number}</td>
                        <td>{l.description || '—'}</td>
                        <td>{l.purchase_order_line || '—'}</td>
                        <td className="num">{l.quantity ?? '—'}</td>
                        <td className="num">{formatCurrency(l.unit_price, invoice.currency)}</td>
                        <td className="num">{formatCurrency(l.tax, invoice.currency)}</td>
                        <td className="num">{formatCurrency(l.line_total, invoice.currency)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : <p className="invd-empty-note">No line items were extracted for this invoice.</p>}
            </div>

            {/* Validation results */}
            <div className="invd-card">
              <div className="invd-card-title"><ListChecks size={14} /> Validation Rules</div>
              {invoice.validation_results?.length ? invoice.validation_results.map((r) => (
                <div key={r.id} className="invd-reason-row">
                  <StatusBadge variant={VALIDATION_VARIANT[r.result] ?? 'gray'} dot>{r.result}</StatusBadge>
                  <div>
                    <div className="invd-reason-msg">{r.message}</div>
                    <div className="invd-reason-cat">{r.category}</div>
                  </div>
                </div>
              )) : <p className="invd-empty-note">No validation results yet.</p>}
            </div>

            {/* Decision reasons */}
            <div className="invd-card">
              <div className="invd-card-title"><ListChecks size={14} /> Decision Reasons</div>
              {invoice.decision_reasons?.length ? invoice.decision_reasons.map((r, i) => (
                <div key={i} className="invd-reason-row">
                  <StatusBadge variant={VALIDATION_VARIANT[r.result] ?? 'gray'} dot>{r.result}</StatusBadge>
                  <div>
                    <div className="invd-reason-msg">{r.message}</div>
                    <div className="invd-reason-cat">{r.category}</div>
                  </div>
                </div>
              )) : <p className="invd-empty-note">This invoice has not been decisioned yet.</p>}
            </div>
          </div>

          <div>
            {/* Confidence scores */}
            <div className="invd-card">
              <div className="invd-card-title">Confidence Scores</div>
              {SCORE_FIELDS.map(([key, label]) => {
                const v = invoice[key];
                return (
                  <div key={key} className="invd-score-row">
                    <span>{label}</span>
                    <div className="invd-score-bar-track">
                      <div className="invd-score-bar-fill" style={{ width: `${v ?? 0}%`, background: scoreColor(v) }} />
                    </div>
                    <span style={{ fontWeight: 700, color: scoreColor(v) }}>{v == null ? '—' : `${v}%`}</span>
                  </div>
                );
              })}
            </div>

            {/* ERP / reconciliation status */}
            <div className="invd-card">
              <div className="invd-card-title"><Truck size={14} /> ERP & Reconciliation</div>
              <div className="invd-field-grid">
                <div><div className="invd-field-label">ERP Status</div><div className="invd-field-value"><StatusBadge variant={ERP_VARIANT[invoice.erp_status] ?? 'gray'} dot>{invoice.erp_status?.replace('_', ' ')}</StatusBadge></div></div>
                <div><div className="invd-field-label">ERP Transaction ID</div><div className="invd-field-value mono">{invoice.erp_transaction_id || '—'}</div></div>
                <div><div className="invd-field-label">Reconciliation</div><div className="invd-field-value"><StatusBadge variant={RECON_VARIANT[invoice.reconciliation_status] ?? 'gray'} dot>{invoice.reconciliation_status?.replace('_', ' ')}</StatusBadge></div></div>
                <div><div className="invd-field-label">Source</div><div className="invd-field-value">{invoice.source}</div></div>
              </div>

              {/* INV18 — ERP posting: connection picker, dry-run preview,
                  post/cancel, real transaction history. */}
              <PermissionGate {...INVOICE_PERMISSIONS.INVOICE_RUN} fallback={null}>
                {erpConnections.length ? (
                  <div className="invd-erp-panel">
                    <div className="invd-erp-row">
                      <select
                        className="invd-erp-select"
                        value={erpConnectionId}
                        onChange={(e) => { setErpConnectionId(e.target.value); setErpPreview(null); }}
                        disabled={!!erpBusy}
                      >
                        {erpConnections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                      </select>
                      <Button variant="secondary" size="sm" icon={<Eye size={14} />} loading={erpBusy === 'preview'} disabled={!!erpBusy} onClick={handlePreviewErp}>
                        Preview
                      </Button>
                      {ERP_POSTABLE.has(invoice.erp_status) && (
                        <Button variant="primary" size="sm" icon={<Send size={14} />} loading={erpBusy === 'post'} disabled={!!erpBusy} onClick={handlePostErp}>
                          Post to ERP
                        </Button>
                      )}
                      {invoice.erp_status === 'SUCCEEDED' && (
                        <Button variant="danger" size="sm" icon={<XCircle size={14} />} loading={erpBusy === 'cancel'} disabled={!!erpBusy} onClick={handleCancelErp}>
                          Cancel
                        </Button>
                      )}
                    </div>

                    {erpError && <div className="invd-erp-error">{erpError}</div>}

                    {erpPreview && (
                      <div className="invd-erp-preview">
                        <div className="invd-erp-preview-hdr">
                          <span>Dry run — nothing was sent</span>
                          <span className="mono">Idempotency-Key: {erpPreview.idempotency_key}</span>
                        </div>
                        <div className="invd-erp-preview-meta">
                          {erpPreview.endpoint_host || 'unknown host'} · mapping v{erpPreview.mapping_version ?? '—'}
                        </div>
                        <pre className="invd-erp-preview-json">{JSON.stringify(erpPreview.payload, null, 2)}</pre>
                      </div>
                    )}

                    {erpTransactions.length > 0 && (
                      <div className="invd-erp-txns">
                        {erpTransactions.map((t) => (
                          <div key={t.id} className="invd-erp-txn-row">
                            <StatusBadge variant={ERP_VARIANT[t.status] ?? 'gray'} dot>{t.status?.replace('_', ' ')}</StatusBadge>
                            <div className="invd-erp-txn-body">
                              <div className="invd-erp-txn-meta mono">attempt #{t.attempt_number} · {t.idempotency_key}</div>
                              {t.error_message && <div className="invd-erp-txn-err">{t.error_message}</div>}
                              <div className="invd-erp-txn-meta">{formatDateTime(t.created_at)}</div>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ) : (
                  <p className="invd-empty-note">No Oracle ERP connection configured — add an ORACLE_ERP_CLOUD connection under Connections to post invoices.</p>
                )}
              </PermissionGate>
            </div>

            {/* Lifecycle timeline */}
            <div className="invd-card">
              <div className="invd-card-title"><History size={14} /> Lifecycle Events</div>
              {events.length ? (
                <div className="invd-timeline">
                  {events.map((ev) => (
                    <div key={ev.id} className="invd-timeline-item">
                      <div className="invd-timeline-dot" />
                      <div className="invd-timeline-body">
                        <div className="invd-timeline-msg">{ev.message || ev.event_type}</div>
                        <div className="invd-timeline-meta">{ev.event_type} · {formatDateTime(ev.created_at)}</div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : <p className="invd-empty-note">No lifecycle events recorded yet.</p>}
            </div>

            {/* Correction history (INV16) — real ReviewAuditLog entries via GET /invoices/{id}/audit */}
            <div className="invd-card">
              <div className="invd-card-title"><ScrollText size={14} /> Correction History</div>
              {audit.length ? (
                <div className="invd-timeline">
                  {audit.map((a) => (
                    <div key={a.id} className="invd-timeline-item">
                      <div className="invd-timeline-dot" />
                      <div className="invd-timeline-body">
                        <div className="invd-timeline-msg">
                          {a.action}{a.old_value != null || a.new_value != null ? ': ' : ''}
                          {a.old_value != null && <span style={{ textDecoration: 'line-through', color: 'var(--text-faint)' }}>{a.old_value}</span>}
                          {a.old_value != null && a.new_value != null && ' → '}
                          {a.new_value != null && <span>{a.new_value}</span>}
                        </div>
                        <div className="invd-timeline-meta">
                          {a.user_name || 'System'} · {formatDateTime(a.created_at)}{a.reason ? ` · ${a.reason}` : ''}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : <p className="invd-empty-note">No corrections have been made to this invoice yet.</p>}
            </div>

            {/* Meta */}
            <div className="invd-card">
              <div className="invd-card-title">Meta</div>
              <div className="invd-field-grid">
                <div><div className="invd-field-label">Created</div><div className="invd-field-value">{formatDateTime(invoice.created_at)}</div></div>
                <div><div className="invd-field-label">Updated</div><div className="invd-field-value">{formatDateTime(invoice.updated_at)}</div></div>
                <div><div className="invd-field-label">Document Status</div><div className="invd-field-value">{invoice.document_status || '—'}</div></div>
                <div><div className="invd-field-label">Document ID</div><div className="invd-field-value mono">{invoice.document_id}</div></div>
              </div>
            </div>
          </div>
        </div>
        )}
      </div>
    </PermissionGate>
  );
}
