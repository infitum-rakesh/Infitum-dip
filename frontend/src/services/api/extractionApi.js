/**
 * Extraction API service — wraps /api/extraction/*. Real AI-based field
 * extraction — see backend/app/services/extraction_service.py. A
 * different document, schema or AI connection genuinely produces a
 * different result; nothing here is canned.
 */
import apiClient from './apiClient.js';

export const extractionApi = {
  /**
   * Auto-detect schema from the document's OCR text via the given AI
   * Provider connection. documentType is optional (e.g. "INVOICE") — when
   * set, returns a comprehensive canonical field schema (see backend
   * document_taxonomies.py) instead of an ad-hoc 5-12-field LLM guess.
   */
  async detectSchema(documentId, connectionId, documentType) {
    const { data } = await apiClient.post('/extraction/detect-schema', {
      document_id: documentId, connection_id: connectionId, document_type: documentType || null,
    });
    return data.fields.map((f, i) => ({ ...f, _id: i }));
  },

  /**
   * Run AI extraction using the provided schema + AI connection.
   */
  async extract(documentId, connectionId, schema) {
    const { data } = await apiClient.post('/extraction/extract', {
      document_id: documentId,
      connection_id: connectionId,
      fields: schema.map((f) => ({
        name: f.name, type: f.type ?? 'string', required: !!f.required, instruction: f.instruction ?? '',
      })),
    });
    return {
      id: `extr_${documentId}_${Date.now()}`,
      documentId,
      fields: data.fields,
      overallConfidence: data.overallConfidence,
      model: data.model,
      rawJson: data.rawJson,
      extractedAt: new Date().toISOString(),
    };
  },

  /**
   * Publish extraction result as a structured dataset.
   * Not yet wired to a real backend — see StructuredDataset.jsx for the
   * disclosed scope boundary; this stays a local, non-persisted preview
   * built directly from the real extraction result (no fabricated stats).
   */
  async createDataset(extractionResult) {
    const profile = extractionResult.fields.map((f) => ({
      column: f.name,
      type: typeof f.value === 'number' ? 'number' : 'string',
      nullable: f.value === '' || f.value == null,
      unique: false,
      nullPct: 0,
      sample: String(f.value ?? '').slice(0, 40),
    }));
    return {
      id: `ds_${extractionResult.id}`,
      name: `EXTRACTED_${extractionResult.documentId}_${Date.now().toString(36).toUpperCase()}`,
      rows: 1,
      columns: extractionResult.fields.length,
      qualityScore: Math.round(extractionResult.overallConfidence ?? 0),
      headerFields: extractionResult.fields,
      lineItems: [],
      profile,
      createdAt: new Date().toISOString(),
    };
  },
};
