/**
 * Real bug found live: BatchDetail's "View document"/"View existing" links
 * navigate by document_id, but Invoice Intelligence's detail route
 * (invoice-intel/:invoiceId) is keyed by invoice_id, not document_id — so
 * the invoice-intel zip-batches route had no viewDocPath at all and the
 * view column silently rendered nothing for every row. This resolves a
 * document_id to its ApInvoice via the existing GET /invoices/by-document/
 * {id} endpoint and redirects, mirroring DocumentDetails.jsx's friendly
 * not-found handling when a batch file points at a document that has no
 * associated invoice (or was deleted).
 */
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { EmptyState, FullPageSpinner } from '@/components/common/index.js';
import { invoiceApi } from '@/services/api/invoiceApi.js';

export default function InvoiceByDocumentRedirect() {
  const { documentId } = useParams();
  const navigate = useNavigate();
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    invoiceApi.getInvoiceByDocument(documentId)
      .then((invoice) => { if (!cancelled) navigate(`/app/invoice-intel/${invoice.id}`, { replace: true }); })
      .catch(() => { if (!cancelled) setError('No invoice found for this document — it may have been deleted.'); });
    return () => { cancelled = true; };
  }, [documentId, navigate]);

  if (error) {
    return (
      <div style={{ padding: 48 }}>
        <EmptyState title="Invoice not found" description={error} />
      </div>
    );
  }
  return <FullPageSpinner />;
}
