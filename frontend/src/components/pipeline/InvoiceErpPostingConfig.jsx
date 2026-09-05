/**
 * InvoiceErpPostingConfig — the config-panel body for the
 * 'invoice_erp_posting' node type (INV24/25).
 *
 * Connection picker is filtered to ORACLE_ERP_CLOUD — currently the only
 * real ERP connector this app ships (see connectors/application/
 * oracle_erp.py, disclosed-unverified against a live Oracle Fusion
 * tenant but exercised for real against a mock Oracle ERP HTTP server
 * during INV9/INV18/INV19's own verification).
 *
 * ERP posting itself is fire-and-forget on the backend (a PENDING
 * transaction is created immediately, the real HTTP round trip runs in
 * a background asyncio task) — this node polls erp_status up to Settle
 * Timeout before moving each invoice on to Reconciliation, so a short
 * timeout can genuinely leave an invoice's erp_status still PENDING/
 * RETRYING when the node finishes (that's disclosed here, not hidden).
 */
import { useEffect, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { connectionApi } from '@/services/api/connectionApi.js';
import './pipeline.css';

export function InvoiceErpPostingConfig({ cfg, set }) {
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    connectionApi.list({ connection_type: 'ORACLE_ERP_CLOUD', page_size: 100 })
      .then((r) => setConnections(r.items ?? r ?? []))
      .catch(() => setConnections([]))
      .finally(() => setLoading(false));
  }, []);

  return (
    <>
      <div className="pc-config-row">
        <label className="pc-config-label">ERP Connection <span style={{ color: 'var(--color-danger)' }}>*</span></label>
        <select className="pc-config-select" value={cfg.erpConnectionId ?? ''} onChange={(e) => set('erpConnectionId', e.target.value ? Number(e.target.value) : null)}>
          <option value="">{loading ? 'Loading…' : 'Select an ERP connection…'}</option>
          {connections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        {!loading && connections.length === 0 && (
          <span className="sdc-inline-warning"><AlertTriangle size={11} /> No Oracle ERP Cloud connections yet — create one in Connections first.</span>
        )}
      </div>

      <div className="pc-config-row">
        <label className="pc-config-label">Settle timeout (seconds) <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>(how long this node polls erp_status before moving on)</span></label>
        <input className="pc-config-input" type="number" min={5} value={cfg.settleTimeoutSeconds ?? 60} onChange={(e) => set('settleTimeoutSeconds', Number(e.target.value))} />
      </div>

      <div className="pc-config-notice">
        <AlertTriangle size={13} />
        <span>
          Calls the real, idempotent ERP posting service — same idempotency key + retry/circuit-breaker logic as the Invoice Detail page's own "Post to ERP" action. Invoices already SUCCEEDED are skipped (never double-posted). If the real ERP round trip is still PENDING/RETRYING when Settle Timeout elapses, the invoice still moves on to Reconciliation with whatever status it currently has — a longer timeout only affects how long this node waits, not whether the post itself succeeds.
        </span>
      </div>
    </>
  );
}
