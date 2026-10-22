/**
 * CorporateDocumentVerification — /app/india-verification/moa (Phase
 * Group 3, IV17). MOA document verification.
 *
 * Deliberately reuses the EXISTING Document Intelligence pipeline for
 * everything document-shaped (upload, OCR, AI extraction) — see
 * documentApi.js — rather than duplicating any of it here. This page's
 * only new work is the "assess what Document Intelligence already
 * extracted" step: POST /compliance/moa/verify (moa_verification_
 * service.py), which never re-runs OCR/extraction itself.
 *
 * Note: Document.doc_type (set at upload time by file extension, see
 * guess_doc_type()) is a different concept from AI Extraction's
 * document_type parameter used here ("MOA") — a document is never
 * durably "tagged" MOA, so the document picker below lists recent
 * documents generally rather than filtering by doc_type.
 *
 * Flow: pick or upload a document → Run AI Extraction with document
 * type "MOA" (if not already done) → Run MOA Verification → review the
 * completeness/authenticity result, the MCA cross-check comparison
 * table (only shown if an MCA result already exists on the linked
 * case), and per-field evidence (which page a clause was found on).
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  FileCheck2, ExternalLink, AlertTriangle, CheckCircle2, XCircle, UploadCloud, ScanSearch,
} from 'lucide-react';
import { Button, StatusBadge, EmptyState, FileUploader, LoadingSpinner } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { documentApi } from '@/services/api/documentApi.js';
import indiaVerificationApi from '@/services/api/indiaVerificationApi.js';
import './compliance.css';

function apiErrorMessage(err, fallback) {
  return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback;
}

const STATUS_VARIANT = {
  VERIFIED: 'success', VERIFIED_WITH_WARNINGS: 'warning', PARTIALLY_VERIFIED: 'warning',
  MISMATCH: 'danger', REQUIRES_REVIEW: 'warning', PENDING_PROVIDER: 'info', IN_PROGRESS: 'info',
  NOT_STARTED: 'gray', NOT_CONFIGURED: 'gray', UNAVAILABLE: 'danger', FAILED: 'danger',
};

const MOA_OUTCOME_COPY = {
  AUTHENTICITY_NOT_ESTABLISHED: { icon: XCircle, tone: 'danger', text: "This document's own text does not confirm it is a Memorandum of Association." },
  MISMATCH_FOUND: { icon: XCircle, tone: 'danger', text: 'MOA details do not match the MCA record on this case — sent to Review Queue.' },
  REQUIRES_MANUAL_REVIEW: { icon: AlertTriangle, tone: 'warning', text: 'Missing required clauses or low-quality OCR — sent to Review Queue.' },
  REGISTRY_DETAILS_MATCHED: { icon: CheckCircle2, tone: 'success', text: 'Company name/CIN match the MCA record already on this case.' },
  COMPLETENESS_CHECK_PASSED: { icon: CheckCircle2, tone: 'success', text: 'All required clauses are present. No MCA record on this case yet to cross-check against.' },
};

const SUBJECT_TYPES = ['VENDOR', 'CUSTOMER', 'COMPANY', 'OTHER'];

// Mirrors moa_verification_service.REQUIRED_MOA_FIELDS — used only as a
// client-side heuristic for "does this document already look extracted
// as MOA" so the Run Extraction button doesn't needlessly re-show.
const REQUIRED_MOA_FIELD_NAMES = ['company_name', 'registered_office_address', 'objects_clause_text', 'liability_clause_text', 'capital_clause_text'];

export default function CorporateDocumentVerification() {
  const navigate = useNavigate();

  const [doc, setDoc] = useState(null); // DocumentDetailOut
  const [docError, setDocError] = useState(null);
  const [extraction, setExtraction] = useState(null); // {fields, overall_confidence} | null
  const [ocr, setOcr] = useState(null); // {full_text, quality_flags, ...} | null

  const [recentDocs, setRecentDocs] = useState([]);
  const [recentDocsLoading, setRecentDocsLoading] = useState(true);

  const [extracting, setExtracting] = useState(false);
  const [extractError, setExtractError] = useState(null);

  const [form, setForm] = useState({ subjectType: 'COMPANY', subjectName: '', caseId: '' });
  const [verifying, setVerifying] = useState(false);
  const [verifyError, setVerifyError] = useState(null);
  const [verifiedCase, setVerifiedCase] = useState(null);

  const [recentMoaCases, setRecentMoaCases] = useState([]);
  const [recentCasesLoading, setRecentCasesLoading] = useState(true);

  useEffect(() => {
    loadRecentDocs();
    loadRecentCases();
  }, []);

  const loadRecentDocs = () => {
    setRecentDocsLoading(true);
    documentApi.listDocuments({ page_size: 10, sort_by: 'uploaded_at', sort_dir: 'desc' })
      .then((r) => { setRecentDocs(r.items ?? []); setRecentDocsLoading(false); })
      .catch(() => setRecentDocsLoading(false));
  };

  const loadRecentCases = () => {
    setRecentCasesLoading(true);
    indiaVerificationApi.listVerifications({ page_size: 20 })
      .then((r) => {
        const withMoa = (r.items ?? []).filter((c) => (c.results ?? []).some((res) => res.check_type === 'MOA'));
        setRecentMoaCases(withMoa);
        setRecentCasesLoading(false);
      })
      .catch(() => setRecentCasesLoading(false));
  };

  const loadPipelineOutputs = async (documentId) => {
    const [ext, ocrRes] = await Promise.all([
      documentApi.getExtraction(documentId).catch(() => null),
      documentApi.getOcr(documentId).catch(() => null),
    ]);
    setExtraction(ext);
    setOcr(ocrRes);
  };

  const resetForNewDocument = async (docRow) => {
    setDoc(docRow);
    setDocError(null);
    setExtractError(null);
    setVerifyError(null);
    setVerifiedCase(null);
    setExtraction(null);
    setOcr(null);
    if (docRow) await loadPipelineOutputs(docRow.id);
  };

  const handleUpload = (file, { onProgress }) => documentApi.uploadDocument(file, { onProgress });

  const pickExisting = async (docSummary) => {
    try {
      const full = await documentApi.getDocument(docSummary.id);
      await resetForNewDocument(full);
    } catch (err) {
      setDocError(apiErrorMessage(err, 'Could not load that document.'));
    }
  };

  const hasMoaFields = extraction?.fields?.length > 0 && extraction.fields.some((f) => REQUIRED_MOA_FIELD_NAMES.includes(f.name) && f.value);

  const runMoaExtraction = async () => {
    if (!doc) return;
    setExtracting(true);
    setExtractError(null);
    try {
      await documentApi.runExtraction(doc.id, 'MOA', null);
      const updated = await documentApi.pollUntilDone(doc.id);
      setDoc(updated);
      await loadPipelineOutputs(doc.id);
    } catch (err) {
      setExtractError(apiErrorMessage(err, 'AI Extraction failed.'));
    } finally {
      setExtracting(false);
    }
  };

  const canVerify = () => !!doc && hasMoaFields && (form.caseId.trim() || form.subjectName.trim());

  const runMoaVerification = async () => {
    if (!canVerify()) return;
    setVerifying(true);
    setVerifyError(null);
    try {
      const body = { documentId: doc.id };
      if (form.caseId.trim()) body.caseId = Number(form.caseId.trim());
      else { body.subjectType = form.subjectType; body.subjectName = form.subjectName.trim(); }
      const result = await indiaVerificationApi.verifyMoa(body);
      setVerifiedCase(result);
      loadRecentCases();
    } catch (err) {
      setVerifyError(apiErrorMessage(err, 'MOA verification failed to run.'));
    } finally {
      setVerifying(false);
    }
  };

  const moaResult = verifiedCase?.results?.find((r) => r.check_type === 'MOA');
  const moaData = moaResult?.normalized_result;
  const outcomeCopy = moaResult?.provider_status_code ? MOA_OUTCOME_COPY[moaResult.provider_status_code] : null;
  const OutcomeIcon = outcomeCopy?.icon;

  return (
    <div className="iv-page">
      <div className="iv-header">
        <div>
          <h1 className="iv-title"><FileCheck2 size={20} /> Corporate Document Verification</h1>
          <p className="iv-subtitle">
            Memorandum of Association (MOA) analysis — reuses this document's own real OCR text and AI-extracted
            clauses. Confirms completeness and, when an MCA record already exists on the case, cross-checks company
            name/CIN against it. Never asserts legal validity or official filing status on its own.
          </p>
        </div>
      </div>

      {!doc ? (
        <>
          <div className="iv-build-panel">
            <div className="iv-section-title" style={{ marginBottom: 4 }}><UploadCloud size={15} style={{ verticalAlign: '-2px', marginRight: 6 }} />Upload an MOA document</div>
            {docError && <div className="iv-banner-error">{docError}</div>}
            <FileUploader
              accept={['.pdf', '.png', '.jpg', '.jpeg', '.tiff']}
              multiple={false}
              onUpload={handleUpload}
              onComplete={(_file, result) => resetForNewDocument(result)}
              onError={(_file, message) => setDocError(message)}
            />
          </div>

          <div className="iv-section-title">Or pick an existing document</div>
          <div className="iv-list-table-panel" style={{ marginTop: 8, padding: recentDocs.length ? 0 : 16 }}>
            {recentDocsLoading ? (
              <LoadingSpinner />
            ) : recentDocs.length === 0 ? (
              <EmptyState icon={<FileCheck2 size={28} />} title="No documents yet" description="Upload an MOA PDF above to get started." />
            ) : (
              <table className="iv-mini-table">
                <thead><tr><th>Filename</th><th>Status</th><th>Uploaded</th><th></th></tr></thead>
                <tbody>
                  {recentDocs.map((d) => (
                    <tr key={d.id}>
                      <td>{d.filename}</td>
                      <td><StatusBadge variant="gray">{d.status}</StatusBadge></td>
                      <td>{formatDateTime(d.uploaded_at)}</td>
                      <td><Button size="sm" variant="secondary" onClick={() => pickExisting(d)}>Select</Button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      ) : (
        <>
          <div className="iv-result-card" style={{ marginBottom: 20 }}>
            <div className="iv-result-header">
              <h3>{doc.filename}</h3>
              <div style={{ display: 'flex', gap: 8 }}>
                <StatusBadge variant="gray">{doc.status}</StatusBadge>
                <Button size="sm" variant="secondary" onClick={() => resetForNewDocument(null)}>Choose Different Document</Button>
              </div>
            </div>

            {!hasMoaFields ? (
              <>
                <div className="iv-banner-info" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <ScanSearch size={14} /> No MOA fields extracted yet — run AI Extraction with document type "MOA" before verifying.
                </div>
                {extractError && <div className="iv-banner-error">{extractError}</div>}
                <Button icon={<ScanSearch size={14} />} loading={extracting} onClick={runMoaExtraction}>
                  Run AI Extraction (MOA)
                </Button>
              </>
            ) : (
              <>
                <div className="iv-result-meta">
                  <span>Fields Extracted: <strong>{extraction.fields.length}</strong></span>
                  <span>OCR Quality Flags: <strong>{(ocr?.quality_flags ?? []).length || 'None'}</strong></span>
                  <Button size="sm" variant="secondary" icon={<ScanSearch size={12} />} loading={extracting} onClick={runMoaExtraction}>
                    Re-run Extraction
                  </Button>
                </div>

                <div className="iv-moa-grid">
                  <div>
                    <div className="iv-moa-subhead">Original OCR Text (excerpt)</div>
                    <div className="iv-moa-text-box">{(ocr?.full_text || '').slice(0, 1200) || '—'}</div>
                  </div>
                  <div>
                    <div className="iv-moa-subhead">Extracted Clauses</div>
                    <div className="iv-moa-field-list">
                      {extraction.fields.map((f) => (
                        <div key={f.name} className="iv-moa-field-row">
                          <span className="iv-moa-field-name">{f.name.replace(/_/g, ' ')}</span>
                          <span className="iv-moa-field-value" title={f.value}>{f.value ? String(f.value).slice(0, 140) : <em>missing</em>}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>

                {!verifiedCase && (
                  <>
                    <div className="iv-section-title" style={{ marginTop: 4 }}>Run MOA Verification</div>
                    <div className="iv-build-row">
                      <div className="iv-build-field">
                        <label>Attach to Existing Case ID (optional)</label>
                        <input className="iv-inline-input" value={form.caseId} onChange={(e) => setForm((f) => ({ ...f, caseId: e.target.value }))} placeholder="e.g. 42" />
                      </div>
                    </div>
                    {!form.caseId.trim() && (
                      <div className="iv-build-row">
                        <div className="iv-build-field">
                          <label>Subject Type</label>
                          <select className="iv-inline-select" value={form.subjectType} onChange={(e) => setForm((f) => ({ ...f, subjectType: e.target.value }))}>
                            {SUBJECT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                          </select>
                        </div>
                        <div className="iv-build-field">
                          <label>Subject Name (new case)</label>
                          <input className="iv-inline-input" value={form.subjectName} onChange={(e) => setForm((f) => ({ ...f, subjectName: e.target.value }))} placeholder="e.g. Infitum Technologies Pvt Ltd" />
                        </div>
                      </div>
                    )}
                    {verifyError && <div className="iv-banner-error">{verifyError}</div>}
                    <Button icon={<FileCheck2 size={14} />} loading={verifying} disabled={!canVerify()} onClick={runMoaVerification}>
                      Run MOA Verification
                    </Button>
                  </>
                )}
              </>
            )}
          </div>

          {moaResult && (
            <div className="iv-result-card" style={{ marginBottom: 20 }}>
              <div className="iv-result-header">
                <h3>Verification Result — {verifiedCase.subject_name}</h3>
                <StatusBadge variant={STATUS_VARIANT[moaResult.overall_status] ?? 'gray'} dot>{moaResult.overall_status}</StatusBadge>
              </div>

              {outcomeCopy && (
                <div className={`iv-banner-${outcomeCopy.tone === 'danger' ? 'error' : outcomeCopy.tone === 'warning' ? 'info' : 'success'}`} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  {OutcomeIcon && <OutcomeIcon size={14} />} {outcomeCopy.text}
                </div>
              )}

              {moaData?.missing_fields?.length > 0 && (
                <div>
                  <div className="iv-moa-subhead">Missing Required Clauses</div>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {moaData.missing_fields.map((f) => <StatusBadge key={f} variant="danger">{f.replace(/_/g, ' ')}</StatusBadge>)}
                  </div>
                </div>
              )}

              {moaData?.comparisons?.length > 0 && (
                <div>
                  <div className="iv-moa-subhead">MCA Cross-Check</div>
                  <table className="iv-mini-table">
                    <thead><tr><th>Field</th><th>MOA Value</th><th>MCA Value</th><th>Match</th></tr></thead>
                    <tbody>
                      {moaData.comparisons.map((c) => (
                        <tr key={c.field}>
                          <td>{c.field.replace(/_/g, ' ')}</td>
                          <td>{c.moa_value ?? '—'}</td>
                          <td>{c.mca_value ?? '—'}</td>
                          <td>{c.matched ? <StatusBadge variant="success">Matched</StatusBadge> : <StatusBadge variant="danger">Mismatch</StatusBadge>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <Button size="sm" variant="secondary" icon={<ExternalLink size={12} />} onClick={() => navigate(`/app/india-verification/cases/${verifiedCase.id}`)}>
                Open Full Case
              </Button>
            </div>
          )}
        </>
      )}

      <div className="iv-section-title">Recent MOA Verification Cases</div>
      <div className="iv-list-table-panel" style={{ marginTop: 8, padding: recentMoaCases.length ? 0 : 16 }}>
        {recentCasesLoading ? (
          <LoadingSpinner />
        ) : recentMoaCases.length === 0 ? (
          <EmptyState icon={<FileCheck2 size={28} />} title="No MOA verifications yet" description="Run one above to get started." />
        ) : (
          <table className="iv-mini-table">
            <thead><tr><th>Subject</th><th>MOA Outcome</th><th>Overall</th><th>Updated</th><th></th></tr></thead>
            <tbody>
              {recentMoaCases.map((c) => {
                const r = c.results.find((res) => res.check_type === 'MOA');
                return (
                  <tr key={c.id}>
                    <td>{c.subject_name}</td>
                    <td>{r?.provider_status_code ?? '—'}</td>
                    <td><StatusBadge variant={STATUS_VARIANT[c.overall_status] ?? 'gray'} dot>{c.overall_status}</StatusBadge></td>
                    <td>{formatDateTime(c.updated_at)}</td>
                    <td><Button size="sm" variant="secondary" icon={<ExternalLink size={12} />} onClick={() => navigate(`/app/india-verification/cases/${c.id}`)}>View</Button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
