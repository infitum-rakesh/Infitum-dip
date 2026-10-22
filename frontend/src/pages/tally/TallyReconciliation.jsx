/**
 * TallyReconciliation — /app/tally/reconciliation (TALLY-J7, UI for
 * TALLY-E's reconciliation engine + TALLY-J's scheduled variant).
 *
 * Two sections against one selected TALLY_PRIME connection:
 *   - Run Now: triggers a real synchronous reconciliation run
 *     (tallyApi.runReconciliation -> reconciliation_service.run_reconciliation(),
 *     the exact same engine a scheduled run calls via
 *     tally_scheduled_reconciliation_service.run_scheduled_reconciliation()).
 *   - Runs history: every run (manual or Schedule-triggered, see the
 *     Trigger column), with a drilldown into that run's real exceptions
 *     (TallyReconciliationException rows — some of which are also live in
 *     Review Queue via TALLY-F's review_item_id anchor).
 *
 * Scheduling itself is NOT duplicated here — a "Tally Reconciliation"
 * schedule is created/edited on the existing Schedules page
 * (ScheduleFormModal's TALLY_RECONCILIATION source type, see TALLY-J3);
 * this page only shows read-only status of what's already scheduled for
 * the selected connection, with a link across to manage it.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Scale, RefreshCw, ExternalLink, CalendarClock, AlertTriangle, Workflow, Plus, Trash2 } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState, LoadingSpinner, Modal } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { scheduleApi } from '@/services/api/scheduleApi.js';
import tallyApi from '@/services/api/tallyApi.js';
import './tally.css';

function apiErrorMessage(err, fallback) {
  return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback;
}

const RUN_STATUS_VARIANT = { RUNNING: 'info', SUCCEEDED: 'success', WARNING: 'warning', FAILED: 'danger' };
const EXCEPTION_LABEL = {
  MATCHED: 'Matched', MISMATCH: 'Amount Mismatch', EXTRA: 'Extra in Tally', MISSING: 'Missing in Tally',
  AMBIGUOUS: 'Ambiguous Match', DUPLICATE_TARGET_MATCH: 'Duplicate Target Match',
  LEDGER_BALANCE_MISMATCH: 'Ledger Balance Mismatch', PAYMENT_VOUCHER_MISSING: 'Payment Voucher Missing',
};

export default function TallyReconciliation() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [connections, setConnections] = useState([]);
  const [connectionsLoading, setConnectionsLoading] = useState(true);
  const connectionId = searchParams.get('connection') || '';
  const connection = connections.find((c) => String(c.id) === String(connectionId));

  const [company, setCompany] = useState('');
  const [tolerance, setTolerance] = useState('0.02');
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState(null);

  const [runs, setRuns] = useState([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [schedules, setSchedules] = useState([]);
  const [exceptionRun, setExceptionRun] = useState(null); // run object or null
  const [exceptions, setExceptions] = useState([]);
  const [exceptionsLoading, setExceptionsLoading] = useState(false);

  // Vendor -> Company Mapping (Invoice Auto-Sync) — org-wide, not scoped to
  // the connection picker above (each row carries its own connection_id).
  const [vendorMappings, setVendorMappings] = useState([]);
  const [vendorMappingsLoading, setVendorMappingsLoading] = useState(true);
  const [mappingModalOpen, setMappingModalOpen] = useState(false);
  const [mappingForm, setMappingForm] = useState({ vendorName: '', connectionId: '', companyName: '' });
  const [mappingSaving, setMappingSaving] = useState(false);
  const [mappingError, setMappingError] = useState(null);

  useEffect(() => {
    connectionApi.list({ connection_type: 'TALLY_PRIME', page_size: 200 })
      .then((r) => {
        setConnections(r.items);
        setConnectionsLoading(false);
        if (!connectionId && r.items.length > 0) {
          setSearchParams({ connection: String(r.items[0].id) }, { replace: true });
        }
      })
      .catch(() => setConnectionsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (connection) setCompany(connection.config?.company_name || '');
  }, [connection]);

  const loadRuns = useCallback(() => {
    if (!connectionId) return;
    setRunsLoading(true);
    tallyApi.listReconciliationRuns(connectionId, { page_size: 50 })
      .then((r) => { setRuns(r.items); setRunsLoading(false); })
      .catch(() => setRunsLoading(false));
  }, [connectionId]);

  const loadSchedules = useCallback(() => {
    if (!connectionId) return;
    scheduleApi.list({ tally_connection_id: connectionId, page_size: 20 })
      .then((r) => setSchedules(r.items ?? []))
      .catch(() => setSchedules([]));
  }, [connectionId]);

  useEffect(() => { loadRuns(); loadSchedules(); }, [loadRuns, loadSchedules]);

  const loadVendorMappings = useCallback(() => {
    setVendorMappingsLoading(true);
    tallyApi.listVendorMappings()
      .then((r) => { setVendorMappings(r.items ?? []); setVendorMappingsLoading(false); })
      .catch(() => setVendorMappingsLoading(false));
  }, []);

  useEffect(() => { loadVendorMappings(); }, [loadVendorMappings]);

  const selectConnection = (id) => setSearchParams({ connection: id }, { replace: true });

  const openAddMapping = () => {
    setMappingForm({ vendorName: '', connectionId: connection ? String(connection.id) : '', companyName: company || '' });
    setMappingError(null);
    setMappingModalOpen(true);
  };

  const openEditMapping = (row) => {
    setMappingForm({ vendorName: row.vendor_name || '', connectionId: String(row.connection_id), companyName: row.company_name });
    setMappingError(null);
    setMappingModalOpen(true);
  };

  const handleSaveMapping = async () => {
    if (!mappingForm.connectionId || !mappingForm.companyName.trim()) return;
    setMappingSaving(true);
    setMappingError(null);
    try {
      await tallyApi.setVendorMapping({
        vendorName: mappingForm.vendorName.trim() || null,
        connectionId: Number(mappingForm.connectionId),
        companyName: mappingForm.companyName.trim(),
      });
      setMappingModalOpen(false);
      loadVendorMappings();
    } catch (err) {
      setMappingError(apiErrorMessage(err, 'Could not save this mapping.'));
    } finally {
      setMappingSaving(false);
    }
  };

  const handleDeleteMapping = async (row) => {
    if (!window.confirm(`Remove the routing rule for "${row.vendor_name || 'Default (fallback)'}"?`)) return;
    try {
      await tallyApi.clearVendorMapping(row.id);
      loadVendorMappings();
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not remove this mapping.'));
    }
  };

  const handleRun = async () => {
    if (!connection || !company.trim()) return;
    setRunning(true);
    setRunError(null);
    try {
      await tallyApi.runReconciliation(connection.id, { company: company.trim(), tolerance: Number(tolerance) || 0 });
      loadRuns();
    } catch (err) {
      setRunError(apiErrorMessage(err, 'Reconciliation run failed.'));
    } finally {
      setRunning(false);
    }
  };

  const openExceptions = async (run) => {
    setExceptionRun(run);
    setExceptionsLoading(true);
    try {
      const r = await tallyApi.listReconciliationExceptions(run.id, { page_size: 500 });
      setExceptions(r.items);
    } catch {
      setExceptions([]);
    } finally {
      setExceptionsLoading(false);
    }
  };

  const runColumns = [
    {
      key: 'status', header: 'Status', sortable: true,
      render: (v) => <StatusBadge variant={RUN_STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge>,
    },
    { key: 'company_name', header: 'Company' },
    {
      key: 'trigger', header: 'Trigger',
      render: (v) => <StatusBadge variant={v === 'Schedule' ? 'info' : 'gray'}>{v || 'Manual'}</StatusBadge>,
    },
    {
      key: 'counts', header: 'Source / Target / Matched',
      render: (_v, row) => `${row.source_count ?? '—'} / ${row.target_count ?? '—'} / ${row.matched_count ?? '—'}`,
    },
    {
      key: 'exceptions', header: 'Exceptions',
      render: (_v, row) => {
        const total = (row.mismatched_count || 0) + (row.extra_count || 0) + (row.missing_count || 0)
          + (row.ambiguous_count || 0) + (row.duplicate_target_count || 0)
          + (row.ledger_balance_mismatch_count || 0) + (row.payment_voucher_missing_count || 0);
        return total > 0
          ? <span className="tly-text-danger" style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><AlertTriangle size={12} /> {total}</span>
          : <span className="tly-text-muted">0</span>;
      },
    },
    { key: 'created_at', header: 'Run At', render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: '',
      render: (_v, row) => <Button size="sm" variant="secondary" onClick={() => openExceptions(row)}>View Exceptions</Button>,
    },
  ];

  const vendorMappingColumns = [
    {
      key: 'vendor_name', header: 'Vendor',
      render: (v) => v || <StatusBadge variant="info">Default (fallback)</StatusBadge>,
    },
    {
      key: 'connection_id', header: 'Tally Connection',
      render: (v) => connections.find((c) => c.id === v)?.name || `#${v}`,
    },
    { key: 'company_name', header: 'Tally Company' },
    { key: 'updated_at', header: 'Updated', render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: '',
      render: (_v, row) => (
        <div style={{ display: 'flex', gap: 6 }}>
          <Button size="sm" variant="secondary" onClick={() => openEditMapping(row)}>Edit</Button>
          <Button size="sm" variant="secondary" icon={<Trash2 size={12} />} onClick={() => handleDeleteMapping(row)} />
        </div>
      ),
    },
  ];

  const exceptionColumns = [
    { key: 'exception_type', header: 'Type', render: (v) => <StatusBadge variant={v === 'MATCHED' ? 'success' : 'warning'}>{EXCEPTION_LABEL[v] || v}</StatusBadge> },
    { key: 'party_name', header: 'Party', render: (v) => v || '—' },
    { key: 'column', header: 'Field', render: (v) => v || '—' },
    { key: 'source_value', header: 'Source Value', render: (v) => v ?? '—' },
    { key: 'target_value', header: 'Target Value', render: (v) => v ?? '—' },
    { key: 'message', header: 'Message' },
    {
      key: 'review_item_id', header: 'Review Queue',
      render: (v) => v ? <Button size="sm" variant="secondary" onClick={() => navigate(`/app/doc-intel/review/${v}`)}>View #{v}</Button> : <span className="tly-text-muted">—</span>,
    },
  ];

  return (
    <div className="tly-page">
      <div className="tly-header">
        <div>
          <h1 className="tly-title"><Scale size={20} /> Tally Reconciliation</h1>
          <p className="tly-subtitle">
            Real two-sided reconciliation (invoice-vs-voucher, GST/TDS, ledger balances, payment vouchers) against a
            live TallyPrime connection — the same engine used by scheduled runs. Read-only for Tally itself; escalated
            mismatches land in Review Queue via TALLY-F.
          </p>
        </div>
        {connections.length > 0 && (
          <select className="tly-conn-select" value={connectionId} onChange={(e) => selectConnection(e.target.value)}>
            {connections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        )}
      </div>

      {connectionsLoading ? (
        <LoadingSpinner />
      ) : connections.length === 0 ? (
        <EmptyState
          icon={<Scale size={28} />}
          title="No TallyPrime connections yet"
          description="Create a TallyPrime connection from Connections first."
          action={{ label: 'Go to Connections', onClick: () => navigate('/app/connections') }}
        />
      ) : (
        <>
          <div className="tly-section-title" style={{ marginTop: 4 }}>
            <Workflow size={14} style={{ verticalAlign: -2, marginRight: 4 }} />
            Vendor → Company Mapping (Invoice Auto-Sync)
          </div>
          <p className="tly-text-muted" style={{ marginTop: 4, marginBottom: 8 }}>
            Routes a STRAIGHT_THROUGH invoice from Invoice Intelligence to the correct Tally connection/company by
            vendor name, then automatically builds the voucher, runs it through Approval, and writes it back to
            Tally — see Settings → Document Intelligence → "Enable Tally Auto-Sync". A row with no vendor name is
            this org's fallback, used when no vendor-specific row matches.
          </p>
          <div className="tly-list-table-panel" style={{ marginBottom: 18 }}>
            <DataTable
              columns={vendorMappingColumns}
              data={vendorMappings}
              loading={vendorMappingsLoading}
              emptyState={
                <EmptyState
                  icon={<Workflow size={28} />}
                  title="No routing rules yet"
                  description="Add a vendor-specific rule, or a default fallback rule, to enable automatic invoice sync."
                />
              }
            />
            <div style={{ padding: '10px 16px', borderTop: '1px solid var(--border-color, #e5e7eb)' }}>
              <Button size="sm" icon={<Plus size={14} />} onClick={openAddMapping}>Add Mapping</Button>
            </div>
          </div>
        </>
      )}

      {connectionsLoading ? null : connection && (
        <>
          <div className="tly-build-panel">
            {runError && <div className="tly-banner-error">{runError}</div>}
            <div className="tly-build-row">
              <div className="tly-build-field">
                <label>Tally Company</label>
                <input className="tly-inline-input" value={company} onChange={(e) => setCompany(e.target.value)} placeholder="Exact company name in Tally" />
              </div>
              <div className="tly-build-field" style={{ maxWidth: 160 }}>
                <label>Tolerance (₹)</label>
                <input type="number" min="0" step="0.01" className="tly-inline-input" value={tolerance} onChange={(e) => setTolerance(e.target.value)} />
              </div>
              <Button icon={<RefreshCw size={14} />} loading={running} disabled={!company.trim()} onClick={handleRun}>
                Run Reconciliation Now
              </Button>
            </div>
          </div>

          <div className="tly-section-title" style={{ marginTop: 18 }}>
            <CalendarClock size={14} style={{ verticalAlign: -2, marginRight: 4 }} />
            Scheduled Runs
          </div>
          {schedules.length === 0 ? (
            <p className="tly-text-muted" style={{ marginTop: 4 }}>
              No schedule linked to this connection yet.{' '}
              <button type="button" className="tly-link-btn" onClick={() => navigate('/app/schedules')}>Create one on Schedules</button> —
              choose Source Type "Tally Reconciliation (Scheduled)".
            </p>
          ) : (
            <div className="tly-list-table-panel" style={{ marginTop: 8, marginBottom: 18 }}>
              <DataTable
                columns={[
                  { key: 'name', header: 'Schedule' },
                  { key: 'frequency', header: 'Frequency' },
                  { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={v === 'ACTIVE' ? 'success' : 'gray'}>{v}</StatusBadge> },
                  { key: 'next_run_at', header: 'Next Run', render: (v) => v ? formatDateTime(v) : '—' },
                  { key: 'actions', header: '', render: (_v, row) => <Button size="sm" variant="secondary" icon={<ExternalLink size={12} />} onClick={() => navigate(`/app/schedules?scheduleId=${row.id}`)}>Manage</Button> },
                ]}
                data={schedules}
                loading={false}
              />
            </div>
          )}

          <div className="tly-section-title">Run History</div>
          <div className="tly-list-table-panel" style={{ marginTop: 8 }}>
            <DataTable
              columns={runColumns}
              data={runs}
              loading={runsLoading}
              emptyState={
                <EmptyState
                  icon={<Scale size={28} />}
                  title="No reconciliation runs yet"
                  description="Run one now, or wait for the next scheduled run."
                />
              }
            />
          </div>
        </>
      )}

      <Modal
        open={!!exceptionRun}
        onClose={() => setExceptionRun(null)}
        title={exceptionRun ? `Exceptions — Run #${exceptionRun.id}` : ''}
        size="xl"
      >
        {exceptionsLoading ? (
          <LoadingSpinner />
        ) : exceptions.length === 0 ? (
          <p className="tly-text-muted">No exceptions recorded for this run — everything matched.</p>
        ) : (
          <DataTable columns={exceptionColumns} data={exceptions} loading={false} />
        )}
      </Modal>

      <Modal
        open={mappingModalOpen}
        onClose={() => setMappingModalOpen(false)}
        title="Vendor → Company Mapping"
        size="md"
      >
        <div className="tly-build-field" style={{ marginBottom: 12 }}>
          <label>Vendor Name</label>
          <input
            className="tly-inline-input"
            value={mappingForm.vendorName}
            onChange={(e) => setMappingForm((f) => ({ ...f, vendorName: e.target.value }))}
            placeholder="Exact vendor name as it appears on the invoice — leave blank for the org default"
          />
          <p className="tly-text-muted" style={{ marginTop: 4, fontSize: 12 }}>
            Matched case/whitespace-insensitively against the invoice's vendor name. Leave blank to set/replace this
            org's default fallback row, used when no vendor-specific row matches.
          </p>
        </div>
        <div className="tly-build-field" style={{ marginBottom: 12 }}>
          <label>Tally Connection</label>
          <select
            className="tly-inline-input"
            value={mappingForm.connectionId}
            onChange={(e) => setMappingForm((f) => ({ ...f, connectionId: e.target.value }))}
          >
            <option value="">Select a connection…</option>
            {connections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </div>
        <div className="tly-build-field" style={{ marginBottom: 12 }}>
          <label>Tally Company</label>
          <input
            className="tly-inline-input"
            value={mappingForm.companyName}
            onChange={(e) => setMappingForm((f) => ({ ...f, companyName: e.target.value }))}
            placeholder="Exact company name in Tally"
          />
        </div>
        {mappingError && <div className="tly-banner-error">{mappingError}</div>}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
          <Button variant="secondary" onClick={() => setMappingModalOpen(false)}>Cancel</Button>
          <Button
            loading={mappingSaving}
            disabled={!mappingForm.connectionId || !mappingForm.companyName.trim()}
            onClick={handleSaveMapping}
          >
            Save
          </Button>
        </div>
      </Modal>
    </div>
  );
}
