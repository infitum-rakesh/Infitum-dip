/**
 * OCR API service — wraps all /api/ocr/* endpoints.
 * Mock returns realistic invoice OCR output with bounding boxes.
 * Real implementation: replace mock bodies with apiClient.post() calls.
 */
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// ── Mock OCR output for a sample invoice ──────────────────────

const MOCK_OCR_TEXT = `INVOICE

ACME CORPORATION                           Invoice #: INV-2024-001
123 Business Street                        Date: March 15, 2024
New York, NY 10001                         Due Date: April 14, 2024
Tel: +1 (555) 100-2000
tax@acmecorp.com

BILL TO:
TechStart Inc.
456 Innovation Avenue
San Francisco, CA 94105
Contact: Jane Smith
jane.smith@techstart.io

DESCRIPTION                    QTY    UNIT PRICE    TOTAL
Enterprise Software License     1      $5,000.00   $5,000.00
Implementation Services         3      $1,500.00   $4,500.00
Training & Onboarding           2      $750.00     $1,500.00

                                       Subtotal:  $11,000.00
                                       Tax (10%):  $1,100.00
                                       TOTAL:     $12,100.00

Payment Terms: Net 30
Bank: First National Bank  |  Account: 123-456-789  |  Routing: 021000021

Thank you for your business!`.trim();

// Bounding boxes as { top, left, width, height } in percentage of the preview area
const MOCK_BBOXES = [
  { id: 'bb1',  label: 'Invoice Header',     top:  2, left:  2, width: 96, height:  5, field: 'header' },
  { id: 'bb2',  label: 'Vendor Address',     top:  8, left:  2, width: 45, height: 14, field: 'vendor' },
  { id: 'bb3',  label: 'Invoice Number',     top:  8, left: 60, width: 38, height:  5, field: 'invoice_number' },
  { id: 'bb4',  label: 'Invoice Date',       top: 13, left: 60, width: 38, height:  5, field: 'invoice_date' },
  { id: 'bb5',  label: 'Due Date',           top: 18, left: 60, width: 38, height:  5, field: 'due_date' },
  { id: 'bb6',  label: 'Bill To',            top: 26, left:  2, width: 44, height: 16, field: 'customer' },
  { id: 'bb7',  label: 'Line Items Table',   top: 46, left:  2, width: 96, height: 24, field: 'line_items' },
  { id: 'bb8',  label: 'Subtotal',           top: 72, left: 55, width: 43, height:  4, field: 'subtotal' },
  { id: 'bb9',  label: 'Tax',                top: 76, left: 55, width: 43, height:  4, field: 'tax' },
  { id: 'bb10', label: 'Total Amount',       top: 80, left: 55, width: 43, height:  5, field: 'total' },
  { id: 'bb11', label: 'Payment Terms',      top: 88, left:  2, width: 60, height:  4, field: 'payment_terms' },
];

const MOCK_OCR_RESULT = {
  confidence: 97.2,
  language: 'en',
  pageCount: 1,
  wordCount: 485,
  charCount: 2840,
  tableCount: 1,
  pages: [
    {
      pageNum: 1,
      width: 794,
      height: 1123,
      confidence: 97.2,
      text: MOCK_OCR_TEXT,
      boundingBoxes: MOCK_BBOXES,
    },
  ],
  fullText: MOCK_OCR_TEXT,
};

// ── Service ────────────────────────────────────────────────────

export const ocrApi = {
  /**
   * Run OCR on a document.
   * Real implementation: POST /api/ocr/process  { document_id, options }
   */
  async process(documentId, options = {}) {
    await delay(2000); // simulate OCR engine processing time
    return {
      id: `ocr_${Date.now()}`,
      documentId,
      ...MOCK_OCR_RESULT,
      processedAt: new Date().toISOString(),
      engine: options.engine || 'dip-ocr-v2',
    };
  },

  /**
   * Fetch a cached OCR result.
   * Real implementation: GET /api/ocr/{document_id}
   */
  async getResult(documentId) {
    await delay(300);
    return { id: `ocr_${documentId}`, documentId, ...MOCK_OCR_RESULT };
  },
};
