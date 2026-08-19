import { useCallback, useRef, useState } from 'react';
import { UploadCloud, File as FileIcon, X, RotateCcw, CheckCircle2, AlertCircle } from 'lucide-react';
import { formatFileSize } from '@/utils/formatters.js';
import './FileUploader.css';

const DEFAULT_ACCEPT = ['.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.bmp', '.docx', '.xlsx', '.csv', '.txt'];
const DEFAULT_MAX_SIZE_MB = 25;

function extOf(name) {
  return name.includes('.') ? `.${name.split('.').pop().toLowerCase()}` : '';
}

/**
 * FileUploader — reusable drag & drop / browse uploader with per-file
 * progress, cancel and retry. Used by the Documents module; generic enough
 * for any future upload surface.
 *
 * Props:
 *   accept:        string[] of allowed extensions, e.g. ['.pdf', '.png']
 *   maxSizeMb:      number
 *   multiple:       boolean — allow multiple files / folder upload
 *   directory:      boolean — when true, browse picks a whole folder
 *   onUpload:       (file, { onProgress }) => Promise<any>  — required
 *   onComplete:     (file, result) => void
 *   onError:        (file, message) => void
 */
export function FileUploader({
  accept = DEFAULT_ACCEPT,
  maxSizeMb = DEFAULT_MAX_SIZE_MB,
  multiple = true,
  directory = false,
  onUpload,
  onComplete,
  onError,
}) {
  const inputRef = useRef(null);
  const [dragOver, setDragOver] = useState(false);
  const [queue, setQueue] = useState([]); // [{ id, file, progress, status, error }]

  const validate = useCallback((file) => {
    const ext = extOf(file.name);
    if (!accept.includes(ext)) {
      return `"${ext || 'unknown'}" is not a supported file type. Allowed: ${accept.join(', ')}`;
    }
    if (file.size > maxSizeMb * 1024 * 1024) {
      return `File exceeds the ${maxSizeMb} MB upload limit.`;
    }
    if (file.size === 0) {
      return 'File is empty.';
    }
    return null;
  }, [accept, maxSizeMb]);

  const startUpload = useCallback((entry) => {
    setQueue((q) => q.map((e) => (e.id === entry.id ? { ...e, status: 'uploading', progress: 0, error: null } : e)));
    onUpload(entry.file, {
      onProgress: (pct) => setQueue((q) => q.map((e) => (e.id === entry.id ? { ...e, progress: pct } : e))),
    })
      .then((result) => {
        setQueue((q) => q.map((e) => (e.id === entry.id ? { ...e, status: 'done', progress: 100 } : e)));
        onComplete?.(entry.file, result);
      })
      .catch((err) => {
        const message = err?.message || 'Upload failed.';
        setQueue((q) => q.map((e) => (e.id === entry.id ? { ...e, status: 'error', error: message } : e)));
        onError?.(entry.file, message);
      });
  }, [onUpload, onComplete, onError]);

  const addFiles = useCallback((fileList) => {
    const files = Array.from(fileList);
    const entries = files.map((file) => {
      const error = validate(file);
      return {
        id: `${file.name}_${file.size}_${Math.random().toString(36).slice(2, 8)}`,
        file,
        progress: 0,
        status: error ? 'error' : 'queued',
        error,
      };
    });
    setQueue((q) => [...q, ...entries]);
    entries.filter((e) => e.status === 'queued').forEach(startUpload);
  }, [validate, startUpload]);

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

  const retry = (entry) => startUpload(entry);
  const remove = (id) => setQueue((q) => q.filter((e) => e.id !== id));

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
          Supports {accept.join(', ').toUpperCase()} &middot; up to {maxSizeMb} MB per file
        </p>
        <input
          ref={inputRef}
          type="file"
          multiple={multiple}
          accept={accept.join(',')}
          onChange={handleInputChange}
          hidden
          {...(directory ? { webkitdirectory: 'true', directory: 'true' } : {})}
        />
      </div>

      {queue.length > 0 && (
        <ul className="uploader-queue">
          {queue.map((entry) => (
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
                {entry.status === 'error' && <span className="uploader-row-error">{entry.error}</span>}
              </div>
              <div className="uploader-row-actions">
                {entry.status === 'done' && <CheckCircle2 size={15} className="uploader-status-ok" />}
                {entry.status === 'error' && (
                  <>
                    <AlertCircle size={15} className="uploader-status-err" />
                    <button className="uploader-icon-btn" title="Retry" onClick={() => retry(entry)}>
                      <RotateCcw size={13} />
                    </button>
                  </>
                )}
                <button className="uploader-icon-btn" title="Remove" onClick={() => remove(entry.id)}>
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
