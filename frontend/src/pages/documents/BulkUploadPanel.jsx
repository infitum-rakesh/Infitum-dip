/**
 * BulkUploadPanel — drag/drop N files in, upload them in ONE real request
 * (POST /documents/upload-bulk), then track each document's real background
 * processing (OCR + AI Extraction + Structured Dataset) live until it lands
 * on a terminal status — no separate "Process" click required. This is the
 * UI half of the "process multiple files automatically by bulk upload" ask;
 * the backend half is documents.upload-bulk (api/documents.py) +
 * document_service.start_processing() (already real, already backgrounded).
 */
import { useCallback, useRef, useState } from 'react';
import { UploadCloud, File as FileIcon, X, CheckCircle2, AlertCircle, Loader2, Eye, Copy } from 'lucide-react';
import { formatFileSize } from '@/utils/formatters.js';
import { documentApi } from '@/services/api/documentApi.js';
import '@/components/common/FileUploader.css';

const DEFAULT_ACCEPT = ['.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.bmp', '.docx', '.xlsx', '.csv', '.txt'];
const DEFAULT_MAX_SIZE_MB = 25;

function extOf(name) {
  return name.includes('.') ? `.${name.split('.').pop().toLowerCase()}` : '';
}

const STAGE_LABEL = {
  queued: 'Queued', uploading: 'Uploading…', processing: 'Processing…',
  COMPLETED: 'Completed', NEEDS_REVIEW: 'Needs review', FAILED: 'Failed', error: 'Failed',
};

export function BulkUploadPanel({ directory = false, onDocumentReady, onAllSettled }) {
  const inputRef = useRef(null);
  const [dragOver, setDragOver] = useState(false);
  const [entries, setEntries] = useState([]); // [{id, file, status, error, progress, docId, confidence}]

  const validate = useCallback((file) => {
    const ext = extOf(file.name);
    if (!DEFAULT_ACCEPT.includes(ext)) {
      return `"${ext || 'unknown'}" is not a supported file type. Allowed: ${DEFAULT_ACCEPT.join(', ')}`;
    }
    if (file.size > DEFAULT_MAX_SIZE_MB * 1024 * 1024) return `File exceeds the ${DEFAULT_MAX_SIZE_MB} MB upload limit.`;
    if (file.size === 0) return 'File is empty.';
    return null;
  }, []);

  const trackDocument = useCallback((entryId, docId) => {
    documentApi.pollUntilDone(docId, {
      onTick: (doc) => setEntries((es) => es.map((e) => (e.id === entryId ? { ...e, status: 'processing', liveStatus: doc.status } : e))),
    })
      .then((doc) => {
        setEntries((es) => es.map((e) => (e.id === entryId ? { ...e, status: doc.status, confidence: doc.confidence } : e)));
        onDocumentReady?.(doc);
      })
      .catch((err) => {
        setEntries((es) => es.map((e) => (e.id === entryId ? { ...e, status: 'error', error: err?.message || 'Processing failed.' } : e)));
      })
      .finally(() => onAllSettled?.());
  }, [onDocumentReady, onAllSettled]);

  const uploadBatch = useCallback((validEntries) => {
    const files = validEntries.map((e) => e.file);
    setEntries((es) => es.map((e) => (validEntries.some((v) => v.id === e.id) ? { ...e, status: 'uploading', progress: 0 } : e)));
    documentApi
      .uploadDocumentsBulk(files, {
        onProgress: (pct) => setEntries((es) => es.map((e) => (validEntries.some((v) => v.id === e.id) ? { ...e, progress: pct } : e))),
      })
      .then((res) => {
        res.items.forEach((item, i) => {
          const entryId = validEntries[i].id;
          if (item.ok) {
            setEntries((es) => es.map((e) => (e.id === entryId ? { ...e, status: 'processing', docId: item.document.id, duplicateOfId: item.duplicate_of_id, warning: item.error } : e)));
            trackDocument(entryId, item.document.id);
          } else {
            setEntries((es) => es.map((e) => (e.id === entryId ? { ...e, status: 'error', error: item.error } : e)));
          }
        });
        onAllSettled?.();
      })
      .catch((err) => {
        const message = err?.response?.data?.detail || err?.message || 'Bulk upload failed.';
        setEntries((es) => es.map((e) => (validEntries.some((v) => v.id === e.id) ? { ...e, status: 'error', error: message } : e)));
      });
  }, [trackDocument, onAllSettled]);

  const addFiles = useCallback((fileList) => {
    const files = Array.from(fileList);
    const newEntries = files.map((file) => {
      const error = validate(file);
      return {
        id: `${file.name}_${file.size}_${Math.random().toString(36).slice(2, 8)}`,
        file, progress: 0, status: error ? 'error' : 'queued', error,
      };
    });
    setEntries((es) => [...es, ...newEntries]);
    const valid = newEntries.filter((e) => e.status === 'queued');
    if (valid.length) uploadBatch(valid);
  }, [validate, uploadBatch]);

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files);
  }, [addFiles]);

  const handleBrowse = () => inputRef.current?.click();
  const handleInputChange = (e) => {
    if (e.target.files?.length) addFiles(e.target.files);
    e.target.value = '';
  };
  const remove = (id) => setEntries((es) => es.filter((e) => e.id !== id));

  return (
    <div className="uploader">
      <div
        className={`uploader-dropzone ${dragOver ? 'drag-over' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        onClick={handleBrowse}
        role="button"
        tabIndex={0}
      >
        <UploadCloud size={30} className="uploader-icon" />
        <p className="uploader-title">Drag & drop {directory ? 'a folder' : 'files'} here, or click to browse</p>
        <p className="uploader-hint">
          Supports {DEFAULT_ACCEPT.join(', ').toUpperCase()} &middot; up to {DEFAULT_MAX_SIZE_MB} MB per file &middot; processing starts automatically for every file
        </p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={DEFAULT_ACCEPT.join(',')}
          onChange={handleInputChange}
          hidden
          {...(directory ? { webkitdirectory: 'true', directory: 'true' } : {})}
        />
      </div>

      {entries.length > 0 && (
        <ul className="uploader-queue">
          {entries.map((entry) => (
            <li key={entry.id} className={`uploader-row uploader-row-${entry.status}`}>
              <FileIcon size={15} className="uploader-row-icon" />
              <div className="uploader-row-body">
                <div className="uploader-row-top">
                  <span className="uploader-row-name" title={entry.file.name}>{entry.file.name}</span>
                  <span className="uploader-row-size">{formatFileSize(entry.file.size)}</span>
                </div>
                {entry.status === 'uploading' && (
                  <div className="uploader-progress-track">
                    <div className="uploader-progress-fill" style={{ width: `${entry.progress}%` }} />
                  </div>
                )}
                {entry.duplicateOfId && (
                  <span className="uploader-row-stage uploader-row-warn" title="Same file content as an existing document — uploaded anyway, just letting you know">
                    <Copy size={11} /> Same content as doc_{entry.duplicateOfId}
                  </span>
                )}
                {entry.warning && <span className="uploader-row-stage uploader-row-warn">{entry.warning}</span>}
                {entry.status === 'processing' && (
                  <span className="uploader-row-stage"><Loader2 size={11} className="uploader-spin" /> {STAGE_LABEL[entry.liveStatus] || 'Processing…'}</span>
                )}
                {entry.status === 'NEEDS_REVIEW' && <span className="uploader-row-stage uploader-row-warn">Needs review{entry.confidence != null ? ` — ${entry.confidence}% confidence` : ''}</span>}
                {entry.status === 'COMPLETED' && <span className="uploader-row-stage uploader-row-ok">Completed{entry.confidence != null ? ` — ${entry.confidence}% confidence` : ''}</span>}
                {(entry.status === 'error' || entry.status === 'FAILED') && <span className="uploader-row-error">{entry.error || 'Processing failed.'}</span>}
              </div>
              <div className="uploader-row-actions">
                {entry.status === 'COMPLETED' && <CheckCircle2 size={15} className="uploader-status-ok" />}
                {entry.status === 'NEEDS_REVIEW' && <AlertCircle size={15} className="uploader-status-warn" />}
                {(entry.status === 'error' || entry.status === 'FAILED') && <AlertCircle size={15} className="uploader-status-err" />}
                {entry.docId && (
                  <a className="uploader-icon-btn" title="View document" href={`/app/doc-intel/documents/${entry.docId}`} target="_blank" rel="noreferrer">
                    <Eye size={13} />
                  </a>
                )}
                <button className="uploader-icon-btn" title="Remove from list" onClick={() => remove(entry.id)}>
                  <X size={13} />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
