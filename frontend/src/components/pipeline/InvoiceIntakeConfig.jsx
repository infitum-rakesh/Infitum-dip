/**
 * InvoiceIntakeConfig — the config-panel body for the 'invoice_intake'
 * node type (INV24/25).
 *
 * Connection picker is filtered to real file-based connections (category
 * STORAGE: SFTP / FTP / Network File) — LOCAL_FILE is a retired
 * connection type (see connection_service.py's _schema_for(), confirmed
 * unusable live during INV24) so it's deliberately not offered here even
 * though the enum member still exists.
 *
 * Path/File Pattern/Recursive are accepted here but only File Pattern
 * and Recursive are actually read by the executor (see
 * pipeline_job_executor.py::_run_invoice_intake) — they're threaded into
 * the exact same discover_files()/compute_file_hash()/
 * upsert_registry_entry() machinery Document Source Schedules use, with
 * schedule_id=None marking discovered rows as a Pipeline Builder run
 * rather than a Schedule. Path is logged for visibility but the real
 * scan root is the connection's own configured base path.
 */
import { useEffect, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { connectionApi } from '@/services/api/connectionApi.js';
import './pipeline.css';

export function InvoiceIntakeConfig({ cfg, set }) {
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    connectionApi.list({ category: 'STORAGE', page_size: 100 })
      .then((r) => setConnections(r.items ?? r ?? []))
      .catch(() => setConnections([]))
      .finally(() => setLoading(false));
  }, []);

  return (
    <>
      <div className="pc-config-row">
        <label className="pc-config-label">Connection <span style={{ color: 'var(--color-danger)' }}>*</span></label>
        <select className="pc-config-select" value={cfg.connectionId ?? ''} onChange={(e) => set('connectionId', e.target.value ? Number(e.target.value) : null)}>
          <option value="">{loading ? 'Loading…' : 'Select a connection…'}</option>
          {connections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
        </select>
        {!loading && connections.length === 0 && (
          <span className="sdc-inline-warning"><AlertTriangle size={11} /> No SFTP/FTP/Network File connections yet — create one in Connections first.</span>
        )}
      </div>

      <div className="pc-config-row">
        <label className="pc-config-label">Path <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>(display only — the connection's own configured base path is what's actually scanned)</span></label>
        <input className="pc-config-input" value={cfg.path ?? '/'} onChange={(e) => set('path', e.target.value)} placeholder="/" />
      </div>

      <div className="pc-config-row">
        <label className="pc-config-label">File pattern</label>
        <input className="pc-config-input" value={cfg.filePattern ?? ''} onChange={(e) => set('filePattern', e.target.value)} placeholder="e.g. *.pdf (blank = every supported file)" />
      </div>

      <label className="pc-config-checkbox-row">
        <input type="checkbox" checked={cfg.recursive ?? false} onChange={(e) => set('recursive', e.target.checked)} /> Scan subfolders recursively
      </label>

      <div className="pc-config-row">
        <label className="pc-config-label">Batch size <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>(max files discovered per run)</span></label>
        <input className="pc-config-input" type="number" min={1} value={cfg.batchSize ?? 50} onChange={(e) => set('batchSize', Number(e.target.value))} />
      </div>

      <div className="pc-config-notice">
        <AlertTriangle size={13} />
        <span>
          Real discovery + content-hash dedup against this org's Document Source Registry — a file already processed by any prior Schedule run or Pipeline Builder run is skipped, not re-ingested. Each newly discovered file becomes a real, stored Document.
        </span>
      </div>
    </>
  );
}
