/**
 * TallyVoucherPreview — /app/tally/vouchers (TALLY-G, Phase 13 UI for
 * TALLY-D's backend).
 *
 * Two tabs against one selected TALLY_PRIME connection:
 *   - Account Mapping: the real, editable, versioned ledger-name mapping
 *     (tally_mapping_service.py) — only "fixed" slots (the actual ledger
 *     names: Purchase Accounts/CGST/SGST/IGST/Cess/TDS Payable) are
 *     editable here; passthrough/lookup slots are structural and shown
 *     read-only, matching what the builder actually consumes them for.
 *   - Voucher Previews: pick a real ApInvoice, build (or rebuild) the
 *     voucher TALLY-D would construct for it, and inspect the ledger
 *     entries, the real Tally Import-XML it would send, and the 5
 *     deterministic validation checks (REQUIRED_FIELDS/BALANCE/
 *     LEDGER_EXISTS/GST/DUPLICATE) — READY/BLOCKED, never guessed.
 *
 * Write-Back to Tally (TALLY-I): once approval_status===APPROVED, the
 * "Write Back to Tally" button calls the one real write-capable endpoint
 * in this whole integration (tally_writeback_service.post_voucher(),
 * idempotent + retried + circuit-breaker-protected in the backend). This
 * page only ever reflects preview.write_status and the real transaction/
 * verification history — it never assumes success itself.
 */
import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Calculator, RefreshCw, Save, FileText, Search, X, CheckCircle2, XCircle, AlertTriangle, ShieldCheck, Send } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState, SearchBox, LoadingSpinner } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { invoiceApi } from '@/services/api/invoiceApi.js';
import tallyApi from '@/services/api/tallyApi.js';
import './tally.css';

function apiErrorMessage(err, fallback) {
  return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback;
}

function formatAmount(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

const CHECK_VARIANT = { PASS: 'success', WARNING: 'warning', FAIL: 'danger' };
const CHECK_ICON = { PASS: <CheckCircle2 size={14} />, WARNING: <AlertTriangle size={14} />, FAIL: <XCircle size={14} /> };
const APPROVAL_VARIANT = { NOT_SUBMITTED: 'gray', PENDING: 'warning', APPROVED: 'success', REJECTED: 'danger' };
const WRITE_VARIANT = { NOT_WRITTEN: 'gray', PENDING: 'warning', RETRYING: 'warning', SUCCEEDED: 'success', FAILED: 'danger', DEAD_LETTER: 'danger' };
const VERIFY_VARIANT = { VERIFIED: 'success', MISMATCH: 'warning', NOT_FOUND: 'danger', FAILED: 'danger' };

export default function TallyVoucherPreview() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [connections, setConnections] = useState([]);
  const [connectionsLoading, setConnectionsLoading] = useState(true);
  const connectionId = searchParams.get('connection') || '';
  const connection = connections.find((c) => String(c.id) === String(connectionId));

  const [activeTab, setActiveTab] = useState(searchParams.get('tab') === 'previews' ? 'previews' : 'mapping');

  useEffect(() => {
    connectionApi.list({ connection_type: 'TALLY_PRIME', page_size: 200 })
      .then((r) => {
        setConnections(r.items);
        setConnectionsLoading(false);
        if (!connectionId && r.items.length > 0) {
          setSearchParams({ connection: String(r.items[0].id), tab: activeTab }, { replace: true });
        }
      })
      .catch(() => setConnectionsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectTab = (tab) => {
    setActiveTab(tab);
    setSearchParams({ connection: connectionId, tab }, { replace: true });
  };
  const selectConnection = (id) => setSearchParams({ connection: id, tab: activeTab }, { replace: true });

  return (
    <div className="tly-page">
      <div className="tly-header">
        <div>
          <h1 className="tly-title"><Calculator size={20} /> Voucher Preview &amp; Mapping</h1>
          <p className="tly-subtitle">
            Build and validate what an invoice would become in Tally before anything is ever written —
            read-only preview + deterministic checks, per connection.
          </p>
        </div>
        {connections.length > 0 && (
          <select
            className="tly-conn-select"
            value={connectionId}
            onChange={(e) => selectConnection(e.target.value)}
          >
            {connections.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        )}
      </div>

      {connectionsLoading ? (
        <LoadingSpinner />
      ) : connections.length === 0 ? (
        <EmptyState
          icon={<Calculator size={28} />}
          title="No TallyPrime connections yet"
          description="Create a TallyPrime connection from Connections first."
        />
      ) : (
        <>
          <div className="tly-tab-nav">
            <button className={`tly-tab-btn ${activeTab === 'mapping' ? 'active' : ''}`} onClick={() => selectTab('mapping')}>
              Account Mapping
            </button>
            <button className={`tly-tab-btn ${activeTab === 'previews' ? 'active' : ''}`} onClick={() => selectTab('previews')}>
              Voucher Previews
            </button>
          </div>

          {activeTab === 'mapping' && connection && <AccountMappingTab connection={connection} />}
          {activeTab === 'previews' && connection && <VoucherPreviewsTab connection={connection} />}
        </>
      )}
    </div>
  );
}

// ── Account Mapping tab ────────────────────────────────────────────

function AccountMappingTab({ connection }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saveMessage, setSaveMessage] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    tallyApi.getMapping(connection.id)
      .then((r) => { setRows(r.items); setLoading(false); setDirty(false); })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load account mapping.')); setLoading(false); });
  }, [connection.id]);

  useEffect(() => { load(); }, [load]);

  const updateDefaultValue = (id, value) => {
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, default_value: value } : r)));
    setDirty(true);
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveMessage(null);
    try {
      const body = rows.map((r) => ({
        canonical_field: r.canonical_field, tally_field: r.tally_field,
        is_line_mapping: r.is_line_mapping, default_value: r.default_value, transform_config: r.transform_config,
      }));
      const result = await tallyApi.publishMapping(connection.id, body);
      setRows(result.items);
      setDirty(false);
      setSaveMessage({ type: 'success', text: `Published as version ${result.items[0]?.version ?? '?'}.` });
    } catch (err) {
      setSaveMessage({ type: 'error', text: apiErrorMessage(err, 'Publish failed.') });
    } finally {
      setSaving(false);
    }
  };

  const currentVersion = rows[0]?.version;

  const columns = [
    { key: 'canonical_field', header: 'Canonical Field', render: (v) => <code className="tly-code">{v}</code> },
    { key: 'tally_field', header: 'Tally Slot', render: (v) => <code className="tly-code">{v}</code> },
    {
      key: 'default_value', header: 'Value',
      render: (v, row) => {
        const type = row.transform_config?.type;
        if (type === 'fixed') {
          return (
            <input
              className="tly-inline-input"
              value={v || ''}
              onChange={(e) => updateDefaultValue(row.id, e.target.value)}
              placeholder="Ledger name in Tally"
            />
          );
        }
        return <span className="tly-text-muted">{type === 'voucher_type_lookup' ? 'Looked up from voucher_type' : `From ${row.canonical_field}`}</span>;
      },
    },
    {
      key: 'required', header: 'Required',
      render: (_v, row) => (row.transform_config?.required ? <StatusBadge variant="warning">Required</StatusBadge> : <span className="tly-text-muted">Optional</span>),
    },
  ];

  return (
    <div className="tly-tab-panel">
      <div className="tly-mapping-toolbar">
        <div>
          {currentVersion != null && <StatusBadge variant="info">Version {currentVersion}</StatusBadge>}
          <span className="tly-text-muted" style={{ marginLeft: 8 }}>
            Ledger names are editable — everything else reflects how the voucher is actually built.
          </span>
        </div>
        <Button size="sm" icon={<Save size={14} />} loading={saving} disabled={!dirty} onClick={handleSave}>
          Publish New Version
        </Button>
      </div>
      {saveMessage && (
        <div className={saveMessage.type === 'success' ? 'tly-banner-success' : 'tly-banner-error'}>{saveMessage.text}</div>
      )}
      <div className="tly-list-table-panel">
        <DataTable columns={columns} data={rows} loading={loading} error={error} />
      </div>
    </div>
  );
}

// ── Voucher Previews tab ─────────────────────────────────────────────

function VoucherPreviewsTab({ connection }) {
  const [previews, setPreviews] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [detail, setDetail] = useState(null);

  const [invoiceSearch, setInvoiceSearch] = useState('');
  const [invoiceResults, setInvoiceResults] = useState([]);
  const [searchingInvoices, setSearchingInvoices] = useState(false);
  const [selectedInvoice, setSelectedInvoice] = useState(null);
  const [company, setCompany] = useState(connection.config?.company_name || '');
  const [building, setBuilding] = useState(false);
  const [buildError, setBuildError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    tallyApi.listVoucherPreviews(connection.id, { page: 1, page_size: 100 })
      .then((r) => { setPreviews(r.items); setTotal(r.total); setLoading(false); })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load voucher previews.')); setLoading(false); });
  }, [connection.id]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!invoiceSearch.trim()) { setInvoiceResults([]); return; }
    setSearchingInvoices(true);
    const handle = setTimeout(() => {
      invoiceApi.list({ q: invoiceSearch, page_size: 8 })
        .then((r) => { setInvoiceResults(r.items); setSearchingInvoices(false); })
        .catch(() => setSearchingInvoices(false));
    }, 300);
    return () => clearTimeout(handle);
  }, [invoiceSearch]);

  const pickInvoice = (inv) => {
    setSelectedInvoice(inv);
    setInvoiceSearch('');
    setInvoiceResults([]);
  };

  const handleBuild = async () => {
    if (!selectedInvoice || !company.trim()) return;
    setBuilding(true);
    setBuildError(null);
    try {
      const preview = await tallyApi.buildVoucherPreview(connection.id, { invoiceId: selectedInvoice.id, company: company.trim() });
      setDetail(preview);
      load();
    } catch (err) {
      setBuildError(apiErrorMessage(err, 'Could not build voucher preview.'));
    } finally {
      setBuilding(false);
    }
  };

  const openDetail = async (row) => {
    try {
      const full = await tallyApi.getVoucherPreview(row.id);
      setDetail(full);
    } catch (err) {
      setError(apiErrorMessage(err, 'Could not load preview detail.'));
    }
  };

  const handleSubmitted = async (previewId) => {
    // Refetch the full detail (approval_status/approval_workflow_id) and the list row.
    const full = await tallyApi.getVoucherPreview(previewId);
    setDetail(full);
    load();
  };

  const columns = [
    { key: 'invoice_id', header: 'Invoice', render: (v) => `#${v}` },
    { key: 'party_ledger_name', header: 'Party Ledger', render: (v) => v || '—' },
    { key: 'voucher_type', header: 'Voucher Type', render: (v) => v || '—' },
    {
      key: 'status', header: 'Status',
      render: (v) => <StatusBadge variant={v === 'READY' ? 'success' : 'danger'}>{v}</StatusBadge>,
    },
    {
      key: 'approval_status', header: 'Approval',
      render: (v) => <StatusBadge variant={APPROVAL_VARIANT[v] || 'gray'}>{(v || 'NOT_SUBMITTED').replace('_', ' ')}</StatusBadge>,
    },
    {
      key: 'write_status', header: 'Write-Back',
      render: (v) => <StatusBadge variant={WRITE_VARIANT[v] || 'gray'}>{(v || 'NOT_WRITTEN').replace('_', ' ')}</StatusBadge>,
    },
    { key: 'updated_at', header: 'Last Built', render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: '',
      render: (_v, row) => (
        <Button size="sm" variant="secondary" icon={<FileText size={14} />} onClick={() => openDetail(row)}>
          View
        </Button>
      ),
    },
  ];

  return (
    <div className="tly-tab-panel">
      <div className="tly-build-panel">
        <div className="tly-build-row">
          <div className="tly-build-field">
            <label>Invoice</label>
            {selectedInvoice ? (
              <div className="tly-selected-invoice">
                <span>#{selectedInvoice.invoice_number || selectedInvoice.id} — {selectedInvoice.vendor_name || 'Unknown vendor'} — {formatAmount(selectedInvoice.total)}</span>
                <button className="tly-chip-clear" onClick={() => setSelectedInvoice(null)} aria-label="Clear selected invoice"><X size={14} /></button>
              </div>
            ) : (
              <div className="tly-invoice-search">
                <SearchBox value={invoiceSearch} onChange={setInvoiceSearch} placeholder="Search invoice number or vendor…" />
                {invoiceSearch.trim() && (
                  <div className="tly-invoice-results">
                    {searchingInvoices ? (
                      <div className="tly-invoice-result-row tly-text-muted"><Search size={12} /> Searching…</div>
                    ) : invoiceResults.length === 0 ? (
                      <div className="tly-invoice-result-row tly-text-muted">No matching invoices.</div>
                    ) : (
                      invoiceResults.map((inv) => (
                        <button key={inv.id} className="tly-invoice-result-row" onClick={() => pickInvoice(inv)}>
                          <span>#{inv.invoice_number || inv.id}</span>
                          <span className="tly-text-muted">{inv.vendor_name || 'Unknown vendor'}</span>
                          <span>{formatAmount(inv.total)}</span>
                        </button>
                      ))
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
          <div className="tly-build-field">
            <label>Tally Company</label>
            <input className="tly-inline-input" value={company} onChange={(e) => setCompany(e.target.value)} placeholder="Exact company name in Tally" />
          </div>
          <Button icon={<RefreshCw size={14} />} loading={building} disabled={!selectedInvoice || !company.trim()} onClick={handleBuild}>
            Build Preview
          </Button>
        </div>
        {buildError && <div className="tly-banner-error">{buildError}</div>}
      </div>

      <div className="tly-list-table-panel">
        <DataTable
          columns={columns}
          data={previews}
          loading={loading}
          error={error}
          emptyState={
            <EmptyState
              icon={<FileText size={28} />}
              title="No voucher previews yet"
              description="Search for an invoice above and build its first preview."
            />
          }
        />
      </div>
      {total > previews.length && <p className="tly-text-muted">Showing {previews.length} of {total}.</p>}

      {detail && <VoucherPreviewDetail preview={detail} onClose={() => setDetail(null)} onSubmitted={handleSubmitted} />}
    </div>
  );
}

function VoucherPreviewDetail({ preview, onClose, onSubmitted }) {
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  const [writingBack, setWritingBack] = useState(false);
  const [writeBackError, setWriteBackError] = useState(null);
  const [transactions, setTransactions] = useState([]);
  const [verifications, setVerifications] = useState([]);
  const approvalStatus = preview.approval_status || 'NOT_SUBMITTED';
  const writeStatus = preview.write_status || 'NOT_WRITTEN';
  const canSubmit = preview.status === 'READY' && (approvalStatus === 'NOT_SUBMITTED' || approvalStatus === 'REJECTED');
  const canWriteBack = approvalStatus === 'APPROVED' && ['NOT_WRITTEN', 'FAILED', 'DEAD_LETTER'].includes(writeStatus);

  const handleSubmit = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await tallyApi.submitVoucherForApproval(preview.id);
      await onSubmitted(preview.id);
    } catch (err) {
      setSubmitError(apiErrorMessage(err, 'Could not submit for approval.'));
    } finally {
      setSubmitting(false);
    }
  };

  const loadWriteBackHistory = useCallback(async () => {
    const [txns, verifs] = await Promise.all([
      tallyApi.listWriteBackTransactions(preview.id),
      tallyApi.listWriteBackVerifications(preview.id),
    ]);
    setTransactions(txns.items || []);
    setVerifications(verifs.items || []);
  }, [preview.id]);

  useEffect(() => {
    loadWriteBackHistory();
  }, [loadWriteBackHistory]);

  // Write-back runs in the backend's own background retry loop — poll
  // while a run is in flight, same setInterval(1200ms) convention as
  // every other live-progress view in this app (JobLogDrawer.jsx,
  // BulkMigrationRunMonitor.jsx). Found live (TALLY-I7): post-write
  // verification (which sets tally_voucher_number) commits in a separate
  // step AFTER write_status already flips to SUCCEEDED — stopping the
  // poll the instant write_status turns terminal can catch that gap and
  // freeze the panel on "Awaiting read-back verification" forever even
  // though verification lands a moment later. Keep polling a short,
  // bounded while past SUCCEEDED specifically to catch that trailing
  // update, instead of only watching for a write_status change.
  useEffect(() => {
    const awaitingVerification = writeStatus === 'SUCCEEDED' && !preview.tally_voucher_number;
    if (!['PENDING', 'RETRYING'].includes(writeStatus) && !awaitingVerification) return undefined;
    let ticks = 0;
    const id = setInterval(async () => {
      ticks += 1;
      const full = await tallyApi.getVoucherPreview(preview.id);
      if (full.write_status !== writeStatus || full.tally_voucher_number) {
        await onSubmitted(preview.id);
      }
      loadWriteBackHistory();
      if (full.tally_voucher_number || ticks >= 10) clearInterval(id);
    }, 1200);
    return () => clearInterval(id);
  }, [writeStatus, preview.id, preview.tally_voucher_number, onSubmitted, loadWriteBackHistory]);

  const handleWriteBack = async () => {
    setWritingBack(true);
    setWriteBackError(null);
    try {
      await tallyApi.writeBackVoucher(preview.id);
      await onSubmitted(preview.id);
      await loadWriteBackHistory();
    } catch (err) {
      setWriteBackError(apiErrorMessage(err, 'Could not write back to Tally.'));
    } finally {
      setWritingBack(false);
    }
  };

  return (
    <div className="tly-detail-panel">
      <div className="tly-detail-header">
        <h3>Voucher Preview — Invoice #{preview.invoice_id}</h3>
        <button className="tly-chip-clear" onClick={onClose} aria-label="Close"><X size={16} /></button>
      </div>

      <div className="tly-detail-meta">
        <span><strong>Status:</strong> <StatusBadge variant={preview.status === 'READY' ? 'success' : 'danger'}>{preview.status}</StatusBadge></span>
        <span><strong>Voucher Type:</strong> {preview.voucher_type || '—'}</span>
        <span><strong>Party Ledger:</strong> {preview.party_ledger_name || '—'}</span>
        <span><strong>Voucher Date:</strong> {preview.voucher_date || '—'}</span>
        <span><strong>Mapping Version:</strong> {preview.mapping_version ?? '—'}</span>
      </div>

      <h4>Write-Back Approval</h4>
      <div className="tly-build-panel">
        <div className="tly-build-row" style={{ alignItems: 'center' }}>
          <span><strong>Status:</strong> <StatusBadge variant={APPROVAL_VARIANT[approvalStatus] || 'gray'}>{approvalStatus.replace('_', ' ')}</StatusBadge></span>
          {canSubmit ? (
            <Button size="sm" icon={<ShieldCheck size={14} />} loading={submitting} onClick={handleSubmit}>
              Submit for Approval
            </Button>
          ) : approvalStatus === 'NOT_SUBMITTED' ? (
            <span className="tly-text-muted">Rebuild this preview to READY before it can be submitted.</span>
          ) : null}
        </div>
        {submitError && <div className="tly-banner-error">{submitError}</div>}
        {approvalStatus === 'PENDING' && (
          <p className="tly-text-muted">Awaiting a human decision — see My Approvals or Approval Workflows for this org.</p>
        )}
      </div>

      <h4>Write Back to Tally</h4>
      <div className="tly-build-panel">
        <div className="tly-build-row" style={{ alignItems: 'center' }}>
          <span><strong>Status:</strong> <StatusBadge variant={WRITE_VARIANT[writeStatus] || 'gray'}>{writeStatus.replace('_', ' ')}</StatusBadge></span>
          {canWriteBack && (
            <Button size="sm" icon={<Send size={14} />} loading={writingBack} onClick={handleWriteBack}>
              Write Back to Tally
            </Button>
          )}
          {approvalStatus !== 'APPROVED' && writeStatus === 'NOT_WRITTEN' && (
            <span className="tly-text-muted">Approve this voucher first (see Write-Back Approval above).</span>
          )}
        </div>
        {writeBackError && <div className="tly-banner-error">{writeBackError}</div>}
        {['PENDING', 'RETRYING'].includes(writeStatus) && (
          <p className="tly-text-muted">Sending to Tally — this page updates automatically when it finishes.</p>
        )}
        {writeStatus === 'SUCCEEDED' && (
          <p className="tly-text-muted">
            Written{preview.written_at ? ` ${formatDateTime(preview.written_at)}` : ''}.
            {preview.tally_voucher_number ? ` Tally voucher #${preview.tally_voucher_number}.` : ' Awaiting read-back verification for the Tally voucher number.'}
          </p>
        )}

        {transactions.length > 0 && (
          <table className="tly-mini-table">
            <thead><tr><th>Attempt</th><th>Status</th><th>Error</th><th>When</th></tr></thead>
            <tbody>
              {transactions.map((t) => (
                <tr key={t.id}>
                  <td>{t.attempt_number}</td>
                  <td><StatusBadge variant={WRITE_VARIANT[t.status] || 'gray'}>{t.status.replace('_', ' ')}</StatusBadge></td>
                  <td>{t.error_message || '—'}</td>
                  <td>{formatDateTime(t.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {verifications.length > 0 && (
          <>
            <h4 style={{ marginTop: '0.75rem' }}>Post-Write Verification</h4>
            <table className="tly-mini-table">
              <thead><tr><th>Result</th><th>Field</th><th>Platform</th><th>Tally</th></tr></thead>
              <tbody>
                {verifications[0].field_comparisons.map((c, i) => (
                  <tr key={i}>
                    {i === 0 && (
                      <td rowSpan={verifications[0].field_comparisons.length}>
                        <StatusBadge variant={VERIFY_VARIANT[verifications[0].status] || 'gray'}>{verifications[0].status.replace('_', ' ')}</StatusBadge>
                      </td>
                    )}
                    <td>{c.field}</td>
                    <td>{String(c.platform_value ?? '—')}</td>
                    <td>{String(c.tally_value ?? '—')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>

      <h4>Validation Results</h4>
      <table className="tly-mini-table">
        <thead><tr><th>Category</th><th>Result</th><th>Message</th></tr></thead>
        <tbody>
          {preview.validation_results.map((r) => (
            <tr key={r.id}>
              <td>{r.category}</td>
              <td><StatusBadge variant={CHECK_VARIANT[r.result] || 'gray'}>{CHECK_ICON[r.result]} {r.result}</StatusBadge></td>
              <td>{r.message}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4>Ledger Entries</h4>
      <table className="tly-mini-table">
        <thead><tr><th>Ledger</th><th>Dr/Cr</th><th>Amount</th></tr></thead>
        <tbody>
          {preview.ledger_entries.map((e, i) => (
            <tr key={i}>
              <td>{e.ledger_name}</td>
              <td>{e.is_deemed_positive === 'Yes' ? 'Dr' : 'Cr'}</td>
              <td>{formatAmount(Math.abs(Number(e.amount)))}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4>Tally Import XML{writeStatus === 'SUCCEEDED' ? ' (sent)' : ' (would be sent on Write Back)'}</h4>
      <pre className="tly-xml-block">{preview.voucher_xml}</pre>
    </div>
  );
}
