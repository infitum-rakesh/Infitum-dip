/**
 * Stage 1 — Image Scanner
 * Handles file upload, preview, enhancement controls and quality scoring.
 * Calls onComplete(scanResult) to advance the pipeline.
 */
import { useState, useRef, useCallback } from 'react';
import { UploadCloud, RotateCw, ZoomIn, FileText, CheckCircle2, ArrowRight } from 'lucide-react';
import { Button, LoadingSpinner } from '@/components/common/index.js';
import { documentApi } from '@/services/api/documentApi.js';
import { LANGUAGE_NAMES } from '@/utils/languageNames.js';
import './document-intelligence.css';

const ACCEPTED_TYPES = ['application/pdf', 'image/jpeg', 'image/png', 'image/tiff', 'image/bmp', 'image/gif'];
const FILE_LABELS    = ['PDF', 'JPG', 'PNG', 'TIFF', 'BMP'];
const OCR_LANGUAGE_OPTIONS = ['auto', 'en', 'hi', 'kn', 'ta', 'te', 'ml', 'mr', 'bn', 'gu', 'pa', 'or', 'as', 'ur'];
const AUTO_DETECT_LANGUAGES = OCR_LANGUAGE_OPTIONS.filter((code) => code !== 'auto');

function languageConfigForSelection(language) {
  if (!language || language === 'auto') {
    return { mode: 'auto', selected_languages: AUTO_DETECT_LANGUAGES, primary_language: 'auto' };
  }
  return { mode: 'explicit', selected_languages: ['en', language], primary_language: language };
}

const ENHANCEMENTS = [
  { key: 'autoRotate',    label: 'Auto Rotate', defaultOn: true  },
  { key: 'deskew',        label: 'Deskew',       defaultOn: true  },
  { key: 'noiseRemoval',  label: 'Noise Removal',defaultOn: true  },
  { key: 'contrast',      label: 'Enhance Contrast', defaultOn: false },
  { key: 'sharpen',       label: 'Sharpen',      defaultOn: false },
  { key: 'blankPage',     label: 'Blank Page Detection', defaultOn: true },
];

export function ImageScanner({ onComplete }) {
  const [document, setDocument]           = useState(null);  // raw uploaded doc object
  const [isDragging, setIsDragging]       = useState(false);
  const [uploading, setUploading]         = useState(false);
  const [processing, setProcessing]       = useState(false);
  const [scanResult, setScanResult]       = useState(null);
  const [enhancements, setEnhancements]   = useState(
    () => Object.fromEntries(ENHANCEMENTS.map((e) => [e.key, e.defaultOn]))
  );
  const [ocrLanguage, setOcrLanguage]     = useState('auto');
  const [error, setError]                 = useState('');
  const fileInputRef = useRef(null);

  // ── File handling ──────────────────────────────────────────

  const handleFile = useCallback(async (file) => {
    if (!file) return;
    if (!ACCEPTED_TYPES.includes(file.type) && !file.name.endsWith('.tiff')) {
      setError('Unsupported file type. Please upload PDF, JPG, PNG or TIFF.');
      return;
    }
    setError('');
    setUploading(true);
    setScanResult(null);
    try {
      // Real upload — POST /api/documents/upload (Documents repository).
      // This is the same document the real OCR stage (OCR.jsx) processes,
      // so what you see recognised downstream is genuinely this file's
      // content, not a canned sample. The backend's DocumentDetailOut uses
      // snake_case (mime_type/page_count) and has no client preview URL —
      // aliased back to what this wizard's UI already reads.
      const languageConfig = languageConfigForSelection(ocrLanguage);
      const doc = await documentApi.uploadDocument(file, { languageConfig });
      setDocument({ ...doc, type: doc.mime_type, pageCount: doc.page_count, previewUrl: URL.createObjectURL(file) });
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || 'Upload failed. Please try again.');
    } finally {
      setUploading(false);
    }
  }, [ocrLanguage]);

  const onFileInput = (e) => handleFile(e.target.files?.[0]);

  const onDrop = (e) => {
    e.preventDefault();
    setIsDragging(false);
    handleFile(e.dataTransfer.files?.[0]);
  };

  // ── Scan / enhance ─────────────────────────────────────────

  const handleScan = async () => {
    if (!document) return;
    setProcessing(true);
    try {
      const result = await documentApi.scan(document.id, enhancements);
      setScanResult(result);
    } catch {
      setError('Scanning failed. Please try again.');
    } finally {
      setProcessing(false);
    }
  };

  const toggleEnhancement = (key) =>
    setEnhancements((prev) => ({ ...prev, [key]: !prev[key] }));

  // ── Helpers ────────────────────────────────────────────────

  const isImage = document?.type?.startsWith('image/');
  const confColor = (score) => score >= 90 ? 'high' : score >= 75 ? 'med' : 'low';

  return (
    <div className="di-stage">
      <div className="di-stage-header">
        <div>
          <h2 className="di-stage-title">Image Scanner</h2>
          <p className="di-stage-subtitle">Upload your document and apply image enhancements before OCR.</p>
        </div>
        {scanResult && (
          <div className="di-stage-actions">
            <Button
              variant="primary"
              icon={<ArrowRight size={14} />}
              onClick={() => onComplete({ document, scanResult, enhancements })}
            >
              Proceed to OCR
            </Button>
          </div>
        )}
      </div>

      {error && (
        <div style={{ padding: '10px 14px', marginBottom: 16, background: 'var(--color-danger-bg)', border: '1px solid var(--color-danger-100, #FEE2E2)', borderRadius: 8, fontSize: 13, color: 'var(--color-danger-dark)' }}>
          {error}
        </div>
      )}

      <div className="di-scanner-layout">
        {/* ── Left: preview / upload ── */}
        <div>
          {!document ? (
            <>
              <div className="di-controls-card" style={{ marginBottom: 12 }}>
                <div className="di-controls-section">
                  <div className="di-controls-section-title">OCR Language</div>
                  <select className="pc-config-select" value={ocrLanguage} onChange={(e) => setOcrLanguage(e.target.value)}>
                    {OCR_LANGUAGE_OPTIONS.map((code) => (
                      <option key={code} value={code}>{code === 'auto' ? 'Auto Detect' : LANGUAGE_NAMES[code]}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div
                className={`di-upload-zone ${isDragging ? 'drag-over' : ''}`}
                onDragOver={(e) => { e.preventDefault(); setIsDragging(true); }}
                onDragLeave={() => setIsDragging(false)}
                onDrop={onDrop}
                onClick={() => fileInputRef.current?.click()}
                role="button"
                aria-label="Upload document"
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".pdf,.jpg,.jpeg,.png,.tiff,.tif,.bmp"
                  style={{ display: 'none' }}
                  onChange={onFileInput}
                />
                {uploading ? (
                  <>
                    <LoadingSpinner size="lg" />
                    <p className="di-upload-title">Uploading…</p>
                  </>
                ) : (
                  <>
                    <div className="di-upload-icon"><UploadCloud size={24} /></div>
                    <p className="di-upload-title">Drop your document here</p>
                    <p className="di-upload-sub">or click to browse files</p>
                    <div className="di-upload-types">
                      {FILE_LABELS.map((l) => <span key={l} className="di-file-tag">{l}</span>)}
                    </div>
                  </>
                )}
              </div>
            </>
          ) : (
            <div className="di-doc-preview" style={{ position: 'relative' }}>
              {/* Toolbar */}
              <div className="di-doc-preview-bar">
                <span style={{ fontSize: 12, color: '#94A3B8', display: 'flex', alignItems: 'center', gap: 6 }}>
                  <FileText size={13} /> {document.filename}
                </span>
                <span style={{ fontSize: 11, color: '#64748B', marginLeft: 8 }}>
                  Page 1 / {document.pageCount}
                </span>
                <button
                  onClick={() => { setDocument(null); setScanResult(null); }}
                  style={{ marginLeft: 'auto', fontSize: 11, color: '#64748B', background: 'none', border: 'none', cursor: 'pointer' }}
                >
                  Change file
                </button>
              </div>

              {/* Preview */}
              <div className="di-doc-preview-body" style={{ padding: 16, minHeight: 380 }}>
                {isImage ? (
                  <img src={document.previewUrl} alt="Preview" className="di-doc-preview-img" />
                ) : (
                  /* PDF mock visualization */
                  <div className="di-doc-preview-mock">
                    <div className="di-mock-line h" />
                    <div style={{ height: 10 }} />
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 12 }}>
                      <div>
                        <div className="di-mock-line" /><div className="di-mock-line m" /><div className="di-mock-line s" />
                      </div>
                      <div>
                        <div className="di-mock-line s" /><div className="di-mock-line m" /><div className="di-mock-line s" />
                      </div>
                    </div>
                    <div className="di-mock-line xs" style={{ marginBottom: 12 }} />
                    <div><div className="di-mock-line m" /><div className="di-mock-line" /><div className="di-mock-line s" /></div>
                    <div className="di-mock-table" style={{ marginTop: 16 }}>
                      {[0,1,2,3].map((r) => (
                        <div key={r} className="di-mock-row">
                          {[0,1,2,3].map((c) => <div key={c} className={`di-mock-cell ${r===0?'hd':''}`} />)}
                        </div>
                      ))}
                    </div>
                    <div style={{ marginTop: 12, display: 'flex', justifyContent: 'flex-end', flexDirection: 'column', alignItems: 'flex-end', gap: 4 }}>
                      <div className="di-mock-line xs" /><div className="di-mock-line xs" /><div className="di-mock-line xs" />
                    </div>
                  </div>
                )}
              </div>

              {/* Processing overlay */}
              {processing && (
                <div className="di-processing-overlay">
                  <LoadingSpinner size="lg" />
                  <p className="di-processing-text">Scanning &amp; enhancing document…</p>
                  <p className="di-processing-sub">Detecting quality, rotation and document type</p>
                </div>
              )}
            </div>
          )}
        </div>

        {/* ── Right: controls ── */}
        <div>
          {document && (
            <div className="di-controls-card">
              <div className="di-controls-section">
                <div className="di-controls-section-title">Language</div>
                <select className="pc-config-select" value={ocrLanguage} onChange={(e) => setOcrLanguage(e.target.value)} disabled={Boolean(document)}>
                  {OCR_LANGUAGE_OPTIONS.map((code) => (
                    <option key={code} value={code}>{code === 'auto' ? 'Auto Detect' : LANGUAGE_NAMES[code]}</option>
                  ))}
                </select>
              </div>

              {/* Enhancements */}
              <div className="di-controls-section">
                <div className="di-controls-section-title">Image Enhancements</div>
                <div className="di-check-list">
                  {ENHANCEMENTS.map((e) => (
                    <div
                      key={e.key}
                      className={`di-check-item ${enhancements[e.key] ? 'checked' : ''}`}
                      onClick={() => toggleEnhancement(e.key)}
                      role="checkbox"
                      aria-checked={enhancements[e.key]}
                    >
                      <div className="di-check-box">
                        {enhancements[e.key] && <span className="di-check-box-tick">✓</span>}
                      </div>
                      <span>{e.label}</span>
                    </div>
                  ))}
                </div>
                <Button
                  variant="primary"
                  full
                  style={{ marginTop: 14 }}
                  loading={processing}
                  onClick={handleScan}
                  disabled={processing}
                >
                  {scanResult ? 'Re-scan Document' : 'Scan Document'}
                </Button>
              </div>

              {/* Quality metrics — only after scan */}
              {scanResult && (
                <>
                  <div className="di-controls-section">
                    <div className="di-controls-section-title">Quality Metrics</div>
                    <div className="di-quality-grid">
                      <div className="di-quality-item">
                        <div className="di-quality-val">{scanResult.quality.resolution}</div>
                        <div className="di-quality-label">Resolution</div>
                      </div>
                      <div className="di-quality-item">
                        <div className="di-quality-val">{scanResult.quality.blurScore}</div>
                        <div className="di-quality-label">Blur Score</div>
                      </div>
                      <div className="di-quality-item">
                        <div className="di-quality-val">{scanResult.quality.skewAngle}</div>
                        <div className="di-quality-label">Skew Angle</div>
                      </div>
                      <div className="di-quality-item">
                        <div className="di-quality-val">{scanResult.quality.contrast}</div>
                        <div className="di-quality-label">Contrast</div>
                      </div>
                    </div>
                    <div className="di-quality-score">
                      <div className="di-score-big">{scanResult.quality.overallScore}</div>
                      <div>
                        <div className="di-score-label">Quality Score</div>
                        <div style={{ fontSize: 11, color: 'var(--color-success)', marginTop: 2 }}>
                          <CheckCircle2 size={11} style={{ display: 'inline', marginRight: 3 }} />
                          Excellent — ready for OCR
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="di-controls-section">
                    <div className="di-controls-section-title">Document Classification</div>
                    <div className="di-classification">
                      <div className="di-class-icon">📄</div>
                      <div>
                        <div className="di-class-name">{scanResult.classification.type}</div>
                        <div className="di-class-conf">
                          {scanResult.classification.confidence}% confidence · {scanResult.classification.language}
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="di-controls-section">
                    <Button
                      variant="primary"
                      full
                      icon={<ArrowRight size={14} />}
                      onClick={() => onComplete({ document, scanResult, enhancements })}
                    >
                      Proceed to OCR
                    </Button>
                  </div>
                </>
              )}
            </div>
          )}

          {!document && (
            <div className="di-controls-card">
              <div className="di-controls-section" style={{ textAlign: 'center', padding: 32 }}>
                <div style={{ fontSize: 32, marginBottom: 10 }}>📤</div>
                <div style={{ fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.6 }}>
                  Upload a document to see quality metrics, classification and enhancement controls.
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
