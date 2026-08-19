/**
 * TargetDatasetConfig — the config-panel body for the 'target' node type.
 * Mirrors SourceDatasetConfig.jsx's architecture on the write side:
 *
 *   Dataset   — DB write via loading/writers/ BulkWriter, real vendor-
 *               native SQL (see LoadStrategyConfig.jsx for Full/Delta
 *               Load + SCD1/SCD2). Has its own connection picker (any
 *               DATABASE-category connection); if left unset, falls back
 *               to the pipeline's primary Source Dataset connection for
 *               backward compat with pipelines saved before this picker
 *               existed (see pipeline_job_executor.py's module note) —
 *               that fallback is what left a file-sourced pipeline (e.g.
 *               Document Intelligence's "Continue to Transformation"
 *               output, which has no upstream DB source at all) with
 *               genuinely no way to pick a database; picking one here
 *               fixes that.
 *   File      — writes under backend/pipeline_outputs/ via pandas.
 *   SFTP      — real upload via connectors/sftp/client.py's write_file()
 *               (paramiko putfo) to the node's own SFTP connection.
 *   API       — real batched POST/PUT/PATCH via connectors/api/rest.py's
 *               send_records() to the node's own REST connection. Auth
 *               (API Key/Bearer/Basic/OAuth2) is inherited entirely from
 *               the Connection, same as the API source.
 *
 * Dataset/SFTP/API targets all have their own connection picker; only
 * File doesn't (writes straight to local disk). No live "test write"
 * here — unlike Source Dataset's Preview, actually sending data to a
 * remote DB/file/API on every panel open would be a real side effect,
 * not a preview; the real test is running the pipeline.
 */
import { useEffect, useState } from 'react';
import { Plus, X, AlertTriangle } from 'lucide-react';
import { connectionApi } from '@/services/api/connectionApi.js';
import { LoadStrategyConfig } from './LoadStrategyConfig.jsx';
import './pipeline.css';

const TARGET_TYPES = [
  { value: 'dataset', label: 'Dataset' },
  { value: 'file', label: 'File' },
  { value: 'sftp', label: 'SFTP' },
  { value: 'api', label: 'API' },
];
const FILE_TYPES = [
  { value: 'csv', label: 'CSV' },
  { value: 'excel', label: 'Excel (.xlsx)' },
  { value: 'json', label: 'JSON' },
];
const HTTP_WRITE_METHODS = ['POST', 'PUT', 'PATCH'];

function listSetter(cfg, set, key) {
  return {
    items: cfg[key] || [],
    setItem: (idx, field, value) => {
      const next = [...(cfg[key] || [])];
      next[idx] = { ...next[idx], [field]: value };
      set(key, next);
    },
    add: () => set(key, [...(cfg[key] || []), { name: '', value: '' }]),
    remove: (idx) => set(key, (cfg[key] || []).filter((_, i) => i !== idx)),
  };
}

function ParamList({ cfg, set, cfgKey, addLabel }) {
  const { items, setItem, add, remove } = listSetter(cfg, set, cfgKey);
  return (
    <>
      {items.map((p, idx) => (
        <div key={idx} className="sdc-param-row">
          <input className="pc-config-input" placeholder="name" value={p.name} onChange={(e) => setItem(idx, 'name', e.target.value)} />
          <input className="pc-config-input" placeholder="value" value={p.value} onChange={(e) => setItem(idx, 'value', e.target.value)} />
          <button type="button" className="sdc-icon-btn" onClick={() => remove(idx)}><X size={12} /></button>
        </div>
      ))}
      <button type="button" className="sdc-secondary-btn" onClick={add}><Plus size={12} /> {addLabel}</button>
    </>
  );
}

export function TargetDatasetConfig({ cfg, set, sourceColumns = [] }) {
  const [connections, setConnections] = useState([]);
  const [connectionsLoading, setConnectionsLoading] = useState(true);
  const targetType = cfg.targetType ?? 'dataset';
  const isDataset = targetType === 'dataset';
  const isSftp = targetType === 'sftp';
  const isApi = targetType === 'api';

  useEffect(() => {
    // Same catalogue Source Dataset uses — filtered client-side per
    // target type below so the picker only offers connections that
    // actually make sense for Dataset (DATABASE category) vs SFTP vs API.
    connectionApi.list({ page_size: 200 })
      .then((r) => setConnections(r.items))
      .catch(() => {})
      .finally(() => setConnectionsLoading(false));
  }, []);

  const filteredConnections = isDataset ? connections.filter((c) => c.category === 'DATABASE')
    : isSftp ? connections.filter((c) => c.connection_type === 'SFTP')
    : isApi ? connections.filter((c) => c.connection_type === 'REST')
    : connections;
  const selectedConnection = connections.find((c) => c.id === cfg.connectionId) ?? null;

  return (
    <>
      <div className="pc-config-row">
        <label className="pc-config-label">Target Type</label>
        <select
          className="pc-config-select" value={targetType}
          onChange={(e) => {
            // Dataset/SFTP/API each keep their own connectionId meaning
            // (a DATABASE vs SFTP vs REST connection) — clear it on type
            // switch so a stale id from the previous type can't get
            // silently resubmitted unselected in the new picker.
            set('targetType', e.target.value);
            set('connectionId', null);
          }}
        >
          {TARGET_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
        </select>
      </div>

      {/* Dataset — real BulkWriter DB write, Full/Delta/SCD1/SCD2 */}
      {isDataset && (
        <>
          <div className="pc-config-row">
            <label className="pc-config-label">Connection</label>
            <select className="pc-config-select" value={cfg.connectionId ?? ''} onChange={(e) => set('connectionId', e.target.value ? Number(e.target.value) : null)}>
              <option value="">{connectionsLoading ? 'Loading…' : 'Use the pipeline’s primary source connection'}</option>
              {filteredConnections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
            </select>
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              Leave unset to reuse the upstream Source Dataset node's connection — required when the source isn't a database (e.g. an uploaded file, or a Document Intelligence extraction).
            </span>
            {!connectionsLoading && filteredConnections.length === 0 && (
              <span className="sdc-inline-warning"><AlertTriangle size={11} /> No database connections yet — create one on the Connections page.</span>
            )}
            {selectedConnection?.status === 'UNSUPPORTED' && (
              <span className="sdc-inline-warning"><AlertTriangle size={11} /> This connection type has no working connector yet.</span>
            )}
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Dataset name</label>
            <input className="pc-config-input" value={cfg.datasetName ?? ''} onChange={(e) => set('datasetName', e.target.value)} placeholder="e.g. INVOICE_DATASET" />
          </div>
          <LoadStrategyConfig cfg={cfg} set={set} sourceColumns={sourceColumns} />
        </>
      )}

      {/* File — local disk, backend/pipeline_outputs/ */}
      {targetType === 'file' && (
        <>
          <div className="pc-config-row">
            <label className="pc-config-label">File name</label>
            <input className="pc-config-input" value={cfg.fileName ?? ''} onChange={(e) => set('fileName', e.target.value)} placeholder="e.g. invoice_export" />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>Without extension — added automatically from File Type below.</span>
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">File Type</label>
            <select className="pc-config-select" value={cfg.fileType ?? 'csv'} onChange={(e) => set('fileType', e.target.value)}>
              {FILE_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </div>
          {cfg.fileType !== 'excel' && cfg.fileType !== 'json' && (
            <div className="pc-config-row">
              <label className="pc-config-label">Delimiter</label>
              <select className="pc-config-select" value={cfg.fileDelimiter ?? ','} onChange={(e) => set('fileDelimiter', e.target.value)}>
                <option value=",">Comma (,)</option>
                <option value=";">Semicolon (;)</option>
                <option value="\t">Tab</option>
                <option value="|">Pipe (|)</option>
              </select>
            </div>
          )}
          <div className="pc-config-row">
            <label className="pc-config-label">Write mode</label>
            <select className="pc-config-select" value={cfg.mode ?? 'replace'} onChange={(e) => set('mode', e.target.value)}>
              <option value="replace">Replace (overwrite each run)</option>
              <option value="append">Append (new file per run)</option>
            </select>
          </div>
        </>
      )}

      {/* SFTP — real upload via connectors/sftp/client.py write_file() */}
      {isSftp && (
        <>
          <div className="pc-config-row">
            <label className="pc-config-label">Connection</label>
            <select className="pc-config-select" value={cfg.connectionId ?? ''} onChange={(e) => set('connectionId', e.target.value ? Number(e.target.value) : null)}>
              <option value="">{connectionsLoading ? 'Loading…' : 'Select an SFTP connection…'}</option>
              {filteredConnections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
            </select>
            {!connectionsLoading && filteredConnections.length === 0 && (
              <span className="sdc-inline-warning"><AlertTriangle size={11} /> No SFTP connections yet — create one on the Connections page.</span>
            )}
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Remote Directory</label>
            <input
              className="pc-config-input" style={{ fontFamily: 'var(--font-mono)' }}
              placeholder="/upload/out (defaults to the connection's Base Path)"
              value={cfg.remoteDir ?? ''} onChange={(e) => set('remoteDir', e.target.value)}
            />
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">File name</label>
            <input className="pc-config-input" value={cfg.fileName ?? ''} onChange={(e) => set('fileName', e.target.value)} placeholder="e.g. invoice_export" />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>Without extension — added automatically from File Type below.</span>
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">File Type</label>
            <select className="pc-config-select" value={cfg.fileType ?? 'csv'} onChange={(e) => set('fileType', e.target.value)}>
              {FILE_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </div>
          {cfg.fileType !== 'excel' && cfg.fileType !== 'json' && (
            <div className="pc-config-row">
              <label className="pc-config-label">Delimiter</label>
              <select className="pc-config-select" value={cfg.fileDelimiter ?? ','} onChange={(e) => set('fileDelimiter', e.target.value)}>
                <option value=",">Comma (,)</option>
                <option value=";">Semicolon (;)</option>
                <option value="\t">Tab</option>
                <option value="|">Pipe (|)</option>
              </select>
            </div>
          )}
          <div className="pc-config-row">
            <label className="pc-config-label">Write mode</label>
            <select className="pc-config-select" value={cfg.mode ?? 'replace'} onChange={(e) => set('mode', e.target.value)}>
              <option value="replace">Replace (overwrite same remote file each run)</option>
              <option value="append">Append (new remote file per run, timestamp-suffixed)</option>
            </select>
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              "Append" here means a new file each run, same as the local File target — not a byte-level append to an existing remote file.
            </span>
          </div>
        </>
      )}

      {/* API — real batched POST/PUT/PATCH via connectors/api/rest.py send_records() */}
      {isApi && (
        <>
          <div className="pc-config-row">
            <label className="pc-config-label">Connection</label>
            <select className="pc-config-select" value={cfg.connectionId ?? ''} onChange={(e) => set('connectionId', e.target.value ? Number(e.target.value) : null)}>
              <option value="">{connectionsLoading ? 'Loading…' : 'Select a REST connection…'}</option>
              {filteredConnections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
            </select>
            {!connectionsLoading && filteredConnections.length === 0 && (
              <span className="sdc-inline-warning"><AlertTriangle size={11} /> No REST connections yet — create one on the Connections page.</span>
            )}
            {selectedConnection?.status === 'UNSUPPORTED' && (
              <span className="sdc-inline-warning"><AlertTriangle size={11} /> This connection type has no working connector yet.</span>
            )}
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">HTTP Method</label>
            <select className="pc-config-select" style={{ width: 120 }} value={cfg.apiMethod || 'POST'} onChange={(e) => set('apiMethod', e.target.value)}>
              {HTTP_WRITE_METHODS.map((m) => <option key={m}>{m}</option>)}
            </select>
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Endpoint <span style={{ color: 'var(--color-danger)' }}>*</span></label>
            <input
              className="pc-config-input" style={{ fontFamily: 'var(--font-mono)' }}
              placeholder="/customers" value={cfg.apiEndpoint || ''} onChange={(e) => set('apiEndpoint', e.target.value)}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>Appended to the connection's Base URL.</span>
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Query Parameters</label>
            <ParamList cfg={cfg} set={set} cfgKey="apiQueryParams" addLabel="Add Parameter" />
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Headers</label>
            <ParamList cfg={cfg} set={set} cfgKey="apiHeaders" addLabel="Add Header" />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              Auth headers (API Key / Bearer / OAuth2) come from the Connection automatically — only add extras here.
            </span>
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Records Key (optional)</label>
            <input
              className="pc-config-input" style={{ fontFamily: 'var(--font-mono)' }}
              placeholder="e.g. customers" value={cfg.apiRecordsKey || ''} onChange={(e) => set('apiRecordsKey', e.target.value)}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              Sends <code>{`{ "${cfg.apiRecordsKey || 'key'}": [...] }`}</code> instead of a raw array — leave blank to POST the batch as a plain JSON array.
            </span>
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Batch Size</label>
            <input
              type="number" className="pc-config-input" style={{ width: 100 }} min={1}
              value={cfg.apiBatchSize ?? 500} onChange={(e) => set('apiBatchSize', Number(e.target.value))}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              Records per HTTP request. Each batch is a separate real request — earlier batches already succeed even if a later one fails (no cross-batch transaction).
            </span>
          </div>
        </>
      )}
    </>
  );
}
