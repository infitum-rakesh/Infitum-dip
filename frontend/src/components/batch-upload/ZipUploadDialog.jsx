/**
 * ZipUploadDialog (ZB11) — chunked ZIP upload via File.slice(), real
 * multipart chunk POSTs against the ZB10 API (zipBatchApi.js), driving the
 * real BatchUpload -> DocumentBatch lifecycle (init -> chunk* -> complete).
 * Nothing here is simulated: the validation summary shown at the end
 * reflects the real counts document_batch_service.create_batch_from_upload()
 * computed while walking the actual extracted ZIP.
 *
 * Whole-file SHA-256 is computed authoritatively server-side while
 * streaming the assembled file (see batch_upload_service.py) — this dialog
 * does not attempt a client-side whole-file hash (the Web Crypto API can't
 * do incremental hashing without a WASM library).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { UploadCloud, FileArchive, X, CheckCircle2, AlertCircle, Loader2, ArrowRight, ChevronDown, ChevronRight } from 'lucide-react';
import { Button } from '@/components/common/index.js';
import { formatFileSize } from '@/utils/formatters.js';
import { DUPLICATE_POLICIES, DUPLICATE_POLICY_LABEL } from '@/services/api/zipBatchApi.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import '@/components/common/FileUploader.css';
import './batch-upload.css';

// Mirrors models/document_storage.py's enums — the same Storage config the
// org-wide Processed Data Storage settings tab exposes, scoped per-batch
// here (Part 21 — "DATA STORAGE FOR ZIP PROCESSING"). SQLite is the
// default — leaving this section collapsed/untouched sends storage_type
// unset, meaning this batch's documents fall through to the org-wide
// config exactly as before this feature existed.
const STORAGE_TYPES = [
  { value: 'SQLITE', label: 'SQLite (default — local only)' },
  { value: 'DATABASE', label: 'Existing DB Connection' },
  { value: 'FILE', label: 'File' },
];
const LOAD_MODES = [
  { value: 'CREATE_IF_NOT_EXISTS', label: 'Create if not exists (upsert)' },
  { value: 'APPEND', label: 'Append (always insert)' },
  { value: 'TRUNCATE_AND_LOAD', label: 'Truncate then load (once)' },
  { value: 'DROP_AND_RECREATE', label: 'Drop & recreate (once)' },
];
const FILE_FORMATS = [
  { value: 'JSONL', label: 'JSON Lines (.jsonl)' },
  { value: 'CSV', label: 'CSV' },
  { value: 'PARQUET', label: 'Parquet (one file per document)' },
  { value: 'XML', label: 'XML (one file per document)' },
];

// Matches core/config.py's upload_chunk_size_mb default (8 MB) and
// upload_concurrency default (4) — see that file's ZB1-ZB12 docstring.
const CHUNK_SIZE = 8 * 1024 * 1024;
const CHUNK_CONCURRENCY = 4;

function apiErrorMessage(err, fallback) { return err?.data?.detail || err?.message || fallback; }

export function ZipUploadDialog({ zipBatchApi, onBatchCreated, onViewBatch, onClose }) {
  const inputRef = useRef(null);
  const [file, setFile] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const [duplicatePolicy, setDuplicatePolicy] = useState('SKIP_DUPLICATE');
  const [autoProcess, setAutoProcess] = useState(true);

  // Storage override (Part 21) — collapsed and defaulted to SQLite (no
  // override at all) so the common case looks exactly like it did before
  // this section existed.
  const [storageOpen, setStorageOpen] = useState(false);
  const [storageType, setStorageType] = useState('SQLITE');
  const [connections, setConnections] = useState([]);
  const [connectionsLoaded, setConnectionsLoaded] = useState(false);
  const [storageConnectionId, setStorageConnectionId] = useState('');
  const [storageSchema, setStorageSchema] = useState('');
  const [storageTable, setStorageTable] = useState('dip_processed_documents');
  const [storageLoadMode, setStorageLoadMode] = useState('CREATE_IF_NOT_EXISTS');
  const [storageFileFormat, setStorageFileFormat] = useState('JSONL');

  useEffect(() => {
    if (!storageOpen || connectionsLoaded) return;
    Promise.all([
      connectionApi.list({ category: 'DATABASE', page_size: 200 }),
      connectionApi.list({ category: 'STORAGE', connection_type: 'LOCAL_FILE', page_size: 200 }),
    ])
      .then(([dbList, fileList]) => setConnections([...(dbList.items ?? []), ...(fileList.items ?? [])]))
      .catch(() => {})
      .finally(() => setConnectionsLoaded(true));
  }, [storageOpen, connectionsLoaded]);

  // 'picking' | 'uploading' | 'completing' | 'done' | 'error'
  const [phase, setPhase] = useState('picking');
  const [progress, setProgress] = useState(0); // 0..1, chunk-upload phase only
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null); // { upload, is_duplicate_upload, batch }
  const chunkBytesRef = useRef([]);
  const cancelledRef = useRef(false);
  const uploadIdRef = useRef(null);

  const pickFile = useCallback((f) => {
    if (!f) return;
    if (!f.name.toLowerCase().endsWith('.zip')) {
      setError('Please choose a .zip file.');
      return;
    }
    setError(null);
    setFile(f);
  }, []);

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files?.[0]) pickFile(e.dataTransfer.files[0]);
  }, [pickFile]);

  const updateOverallProgress = useCallback((totalChunks) => {
    const done = chunkBytesRef.current.reduce((a, b) => a + b, 0);
    setProgress(totalChunks ? Math.min(1, done / totalChunks) : 0);
  }, []);

  const startUpload = useCallback(async () => {
    if (!file) return;
    setPhase('uploading');
    setError(null);
    cancelledRef.current = false;

    const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE));
    chunkBytesRef.current = new Array(totalChunks).fill(0);

    try {
      const upload = await zipBatchApi.initUpload({
        filename: file.name, size_bytes: file.size, chunk_size: CHUNK_SIZE, total_chunks: totalChunks,
      });
      uploadIdRef.current = upload.id;

      // Bounded-concurrency chunk uploads — a simple worker-index pool,
      // mirroring the claim-based worker pools used elsewhere in this app
      // (bulk_migration_service.py / document_batch_service.py) but purely
      // client-side here since there's nothing to coordinate across tabs.
      let nextChunk = 0;
      const worker = async () => {
        while (nextChunk < totalChunks) {
          if (cancelledRef.current) return;
          const i = nextChunk++;
          const start = i * CHUNK_SIZE;
          const blob = file.slice(start, Math.min(start + CHUNK_SIZE, file.size));
          await zipBatchApi.uploadChunk(upload.id, i, blob, {
            onProgress: (frac) => { chunkBytesRef.current[i] = frac; updateOverallProgress(totalChunks); },
          });
          chunkBytesRef.current[i] = 1;
          updateOverallProgress(totalChunks);
        }
      };
      await Promise.all(Array.from({ length: Math.min(CHUNK_CONCURRENCY, totalChunks) }, worker));
      if (cancelledRef.current) return;

      setPhase('completing');
      const completed = await zipBatchApi.completeUpload(upload.id, {
        auto_process: autoProcess, duplicate_policy: duplicatePolicy,
        // storage_type=null (the default, untouched Storage section) means
        // no override — this batch inherits the org-wide Processed Data
        // Storage config exactly as before this feature existed.
        storage_type: storageOpen && storageType !== 'SQLITE' ? storageType : null,
        storage_target_connection_id: storageOpen && storageType !== 'SQLITE' && storageConnectionId ? Number(storageConnectionId) : null,
        storage_target_schema: storageOpen && storageType === 'DATABASE' ? (storageSchema || null) : null,
        storage_target_table: storageOpen && storageType !== 'SQLITE' ? (storageTable || null) : null,
        storage_load_mode: storageOpen && storageType === 'DATABASE' ? storageLoadMode : null,
        storage_file_format: storageOpen && storageType === 'FILE' ? storageFileFormat : null,
      });
      setResult(completed);
      setPhase('done');
      onBatchCreated?.(completed.batch);
    } catch (err) {
      setError(apiErrorMessage(err, 'Upload failed.'));
      setPhase('error');
    }
  }, [
    file, autoProcess, duplicatePolicy, zipBatchApi, onBatchCreated, updateOverallProgress,
    storageOpen, storageType, storageConnectionId, storageSchema, storageTable, storageLoadMode, storageFileFormat,
  ]);

  // Closing the dialog NEVER cancels anything by itself — the chunk upload
  // and completion call are plain in-flight requests that keep running
  // after this component unmounts (React doesn't abort them), so the batch
  // is created/processed regardless of whether the dialog stays open. Only
  // an explicit, confirmed "Cancel Upload" click actually aborts the
  // upload session server-side.
  const handleRunInBackground = useCallback(() => { onClose?.(); }, [onClose]);

  const handleCancelUpload = useCallback(() => {
    if (!window.confirm('Cancel this upload? Anything uploaded so far will be discarded.')) return;
    cancelledRef.current = true;
    if (uploadIdRef.current) {
      zipBatchApi.cancelUpload(uploadIdRef.current).catch(() => {});
    }
    setPhase('picking');
    setFile(null);
  }, [zipBatchApi]);

  const percent = Math.round(progress * 100);

  return (
    <div className="zb-upload-dialog">
      {phase === 'picking' && (
        <>
          {!file ? (
            <div
              className={`uploader-dropzone ${dragOver ? 'drag-over' : ''}`}
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={handleDrop}
              onClick={() => inputRef.current?.click()}
              role="button"
              tabIndex={0}
            >
              <UploadCloud size={30} className="uploader-icon" />
              <p className="uploader-title">Drag &amp; drop a .zip file here, or click to browse</p>
              <p className="uploader-hint">Every document inside is extracted, deduplicated, and processed through the same pipeline as a normal upload.</p>
              <input ref={inputRef} type="file" accept=".zip" hidden onChange={(e) => { pickFile(e.target.files?.[0]); e.target.value = ''; }} />
            </div>
          ) : (
            <>
              <div className="zb-picked-file">
                <FileArchive size={18} />
                <div className="zb-picked-file-body">
                  <div className="zb-picked-file-name">{file.name}</div>
                  <div className="zb-picked-file-size">{formatFileSize(file.size)}</div>
                </div>
                <button className="uploader-icon-btn" title="Choose a different file" onClick={() => setFile(null)}><X size={14} /></button>
              </div>

              <div className="zb-option-row">
                <label className="zb-option-label">If a file already exists</label>
                <select className="zb-select" value={duplicatePolicy} onChange={(e) => setDuplicatePolicy(e.target.value)}>
                  {DUPLICATE_POLICIES.map((p) => <option key={p} value={p}>{DUPLICATE_POLICY_LABEL[p]}</option>)}
                </select>
              </div>
              <label className="zb-checkbox-row">
                <input type="checkbox" checked={autoProcess} onChange={(e) => setAutoProcess(e.target.checked)} />
                Start processing automatically once the ZIP is validated
              </label>

              <button type="button" className="zb-storage-toggle" onClick={() => setStorageOpen((v) => !v)}>
                {storageOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                Storage
                <span className="zb-storage-toggle-current">{STORAGE_TYPES.find((t) => t.value === storageType)?.label ?? 'SQLite (default — local only)'}</span>
              </button>
              {storageOpen && (
                <div className="zb-storage-panel">
                  <div className="zb-option-row" style={{ marginTop: 0 }}>
                    <label className="zb-option-label">Storage</label>
                    <select className="zb-select" value={storageType} onChange={(e) => setStorageType(e.target.value)}>
                      {STORAGE_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                    </select>
                  </div>

                  {storageType !== 'SQLITE' && (
                    <>
                      <div className="zb-option-row">
                        <label className="zb-option-label">Connection</label>
                        <select className="zb-select" value={storageConnectionId} onChange={(e) => setStorageConnectionId(e.target.value)}>
                          <option value="">— Select a connection —</option>
                          {connections
                            .filter((c) => (storageType === 'FILE' ? c.connection_type === 'LOCAL_FILE' : c.connection_type !== 'LOCAL_FILE'))
                            .map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
                        </select>
                      </div>
                      {storageType === 'DATABASE' && (
                        <div className="zb-option-row">
                          <label className="zb-option-label">Schema (optional)</label>
                          <input className="zb-text-input" value={storageSchema} onChange={(e) => setStorageSchema(e.target.value)} placeholder="e.g. public" />
                        </div>
                      )}
                      <div className="zb-option-row">
                        <label className="zb-option-label">{storageType === 'FILE' ? 'File path' : 'Table name'}</label>
                        <input className="zb-text-input" value={storageTable} onChange={(e) => setStorageTable(e.target.value)} />
                      </div>
                      {storageType === 'DATABASE' && (
                        <div className="zb-option-row">
                          <label className="zb-option-label">Load Mode</label>
                          <select className="zb-select" value={storageLoadMode} onChange={(e) => setStorageLoadMode(e.target.value)}>
                            {LOAD_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
                          </select>
                        </div>
                      )}
                      {storageType === 'FILE' && (
                        <div className="zb-option-row">
                          <label className="zb-option-label">Format</label>
                          <select className="zb-select" value={storageFileFormat} onChange={(e) => setStorageFileFormat(e.target.value)}>
                            {FILE_FORMATS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
                          </select>
                        </div>
                      )}
                      <p className="zb-storage-hint">
                        Overrides the org-wide Processed Data Storage destination for this batch only — every document is still also stored locally regardless of this choice.
                      </p>
                    </>
                  )}
                </div>
              )}

              <div className="zb-dialog-actions">
                <Button variant="secondary" onClick={() => setFile(null)}>Back</Button>
                <Button variant="primary" onClick={startUpload}>Upload &amp; {autoProcess ? 'Process' : 'Register'}</Button>
              </div>
            </>
          )}
          {error && <div className="zb-error-banner">{error}</div>}
        </>
      )}

      {(phase === 'uploading' || phase === 'completing') && (
        <div className="zb-progress-phase">
          <FileArchive size={22} />
          <div className="zb-progress-phase-name">{file?.name}</div>
          <div className="uploader-progress-track" style={{ width: '100%', marginTop: 10 }}>
            <div className="uploader-progress-fill" style={{ width: `${phase === 'completing' ? 100 : percent}%` }} />
          </div>
          <div className="zb-progress-phase-label">
            <Loader2 size={13} className="uploader-spin" />
            {phase === 'uploading' ? `Uploading… ${percent}%` : 'Validating and extracting the archive…'}
          </div>
          <p className="zb-progress-phase-hint">
            Safe to close — the upload keeps running in the background and won't be lost.
          </p>
          <div className="zb-dialog-actions">
            <Button variant="secondary" size="sm" onClick={handleRunInBackground}>Close</Button>
            {phase === 'uploading' && <Button variant="ghost" size="sm" onClick={handleCancelUpload}>Cancel Upload</Button>}
          </div>
        </div>
      )}

      {phase === 'error' && (
        <div className="zb-progress-phase">
          <AlertCircle size={22} className="zb-error-icon" />
          <div className="zb-error-banner">{error}</div>
          <div className="zb-dialog-actions">
            <Button variant="secondary" onClick={onClose}>Close</Button>
            <Button variant="primary" onClick={() => { setPhase('picking'); }}>Try Again</Button>
          </div>
        </div>
      )}

      {phase === 'done' && result && (
        <div className="zb-summary">
          <div className="zb-summary-header">
            <CheckCircle2 size={20} className="zb-summary-ok-icon" />
            <div>
              <div className="zb-summary-title">Batch created</div>
              <div className="zb-summary-sub">
                {result.is_duplicate_upload ? 'This ZIP was uploaded before — reusing the existing batch.' : 'The archive was validated and registered for processing.'}
              </div>
            </div>
          </div>
          <div className="zb-summary-grid">
            <div className="zb-summary-stat"><div className="zb-summary-stat-val">{result.batch.total_entries}</div><div className="zb-summary-stat-label">Total Entries</div></div>
            <div className="zb-summary-stat zb-summary-stat-success"><div className="zb-summary-stat-val">{result.batch.valid_files}</div><div className="zb-summary-stat-label">Valid</div></div>
            <div className="zb-summary-stat zb-summary-stat-warn"><div className="zb-summary-stat-val">{result.batch.duplicate_files}</div><div className="zb-summary-stat-label">Duplicate</div></div>
            <div className="zb-summary-stat zb-summary-stat-warn"><div className="zb-summary-stat-val">{result.batch.unsupported_files}</div><div className="zb-summary-stat-label">Unsupported</div></div>
          </div>
          <div className="zb-dialog-actions">
            <Button variant="secondary" onClick={onClose}>Close</Button>
            <Button variant="primary" icon={<ArrowRight size={14} />} onClick={() => onViewBatch?.(result.batch)}>View Batch</Button>
          </div>
        </div>
      )}
    </div>
  );
}
