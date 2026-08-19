/**
 * AiAssistantPanel — SQL Workspace sidebar tab. Natural-language → SQL
 * via whichever AI Provider connection (OpenAI/Anthropic/Ollama — same
 * connectors Document Intelligence's AI Extraction already uses) the
 * user picks. Real schema context is built server-side from the active
 * tab's own database connection (see backend/app/services/
 * sql_ai_service.py) — a different question, connection, or schema
 * genuinely produces a different prompt and a different real response.
 *
 * The AI only PROPOSES SQL here — it never runs it. "Insert into Editor"
 * hands the text to the normal editor/Run flow, the same human-in-the-
 * loop boundary every other SQL Workspace action already respects.
 */
import { useEffect, useState } from 'react';
import { Sparkles, Loader2, AlertTriangle, ArrowDownToLine, Info } from 'lucide-react';
import { Button, EmptyState } from '@/components/common/index.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { sqlWorkspaceApi } from '@/services/api/sqlWorkspaceApi.js';

const CATEGORY_LABEL = { READ: 'Read', WRITE: 'Write', DDL: 'Schema change', DCL: 'Permissions', UNKNOWN: 'Unrecognized' };

function apiErrorMessage(err, fallback) { return err?.message || fallback; }

export default function AiAssistantPanel({ connectionId, schemaName, currentSql, onInsertSql }) {
  const [aiConnections, setAiConnections] = useState([]);
  const [loadingAi, setLoadingAi] = useState(true);
  const [aiConnectionId, setAiConnectionId] = useState(null);
  const [question, setQuestion] = useState('');
  const [generating, setGenerating] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');

  // Load AI Agent connections once, default to the first active one —
  // same pattern as document-intelligence/AIExtraction.jsx.
  useEffect(() => {
    connectionApi.list({ category: 'AI_PROVIDER', page_size: 50 })
      .then((r) => {
        const items = r.items ?? [];
        setAiConnections(items);
        const active = items.find((c) => c.status === 'ACTIVE') ?? items[0];
        if (active) setAiConnectionId(active.id);
      })
      .catch(() => {})
      .finally(() => setLoadingAi(false));
  }, []);

  const generate = async () => {
    if (!connectionId || !aiConnectionId || !question.trim()) return;
    setGenerating(true);
    setError('');
    setResult(null);
    try {
      const r = await sqlWorkspaceApi.generateSql(connectionId, {
        ai_connection_id: aiConnectionId,
        question: question.trim(),
        schema_name: schemaName || null,
        current_sql: currentSql || null,
      });
      setResult(r);
    } catch (err) {
      setError(apiErrorMessage(err, 'Could not generate SQL for this request.'));
    } finally {
      setGenerating(false);
    }
  };

  if (!connectionId) {
    return (
      <EmptyState
        icon={<Sparkles size={28} />}
        title="Select a connection"
        description="Pick a database connection above to ask the AI Assistant about its schema."
      />
    );
  }

  return (
    <div className="sqlw-ai-panel">
      <div className="sqlw-ai-field">
        <label>AI connection</label>
        {loadingAi ? (
          <div className="sqlw-tree-empty"><Loader2 size={13} className="sqlw-spin" /> Loading…</div>
        ) : aiConnections.length === 0 ? (
          <div className="sqlw-ai-empty">No AI Agent connections configured — add one on the Connections page.</div>
        ) : (
          <select
            className="sqlw-select"
            value={aiConnectionId ?? ''}
            onChange={(e) => setAiConnectionId(e.target.value ? Number(e.target.value) : null)}
          >
            {aiConnections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        )}
      </div>

      <div className="sqlw-ai-field">
        <label>Ask a question about this database</label>
        <textarea
          className="sqlw-ai-question"
          placeholder="e.g. Show me the 10 most recent orders with customer name and email"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) generate(); }}
          rows={4}
        />
      </div>

      <Button
        variant="primary" size="sm" icon={<Sparkles size={14} />} loading={generating}
        disabled={!aiConnectionId || !question.trim()} onClick={generate} full
      >
        Generate SQL
      </Button>

      {error && (
        <div className="sqlw-banner error small" style={{ marginTop: 10 }}>
          <AlertTriangle size={13} /> {error}
        </div>
      )}

      {result && (
        <div className="sqlw-ai-result">
          <div className="sqlw-ai-result-head">
            <span className={`sqlw-ai-badge sqlw-ai-badge-${result.statement_category.toLowerCase()}`}>
              {CATEGORY_LABEL[result.statement_category] || result.statement_category}
            </span>
            <span className="sqlw-ai-model">{result.model}</span>
          </div>
          <pre className="sqlw-ai-sql">{result.sql}</pre>
          {result.explanation && (
            <p className="sqlw-ai-explanation"><Info size={12} /> {result.explanation}</p>
          )}
          {result.warnings.map((w, i) => (
            <div key={i} className="sqlw-banner warning small"><AlertTriangle size={13} /> {w}</div>
          ))}
          <Button variant="secondary" size="sm" icon={<ArrowDownToLine size={14} />} onClick={() => onInsertSql(result.sql)} full>
            Insert into Editor
          </Button>
          <p className="sqlw-ai-disclaimer">The AI only writes SQL here — it never executes it. Review the statement, then hit Run yourself.</p>
        </div>
      )}
    </div>
  );
}
