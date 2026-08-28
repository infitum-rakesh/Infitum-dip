/**
 * SqlEditor — Monaco-based SQL editor for SQL Workspace. Real syntax
 * highlighting and keyword completion come from Monaco's own built-in
 * "sql" language contribution (monaco-editor ships this — not hand-
 * rolled here). Table/column completion is real too: it's backed by
 * connectionApi.getSchemas()/getObjects()/getColumns() against the
 * ACTIVE connection, the same live metadata the DatabaseExplorer tree
 * uses — never a hardcoded or fabricated symbol list. Requires internet
 * access at runtime: @monaco-editor/react's default loader fetches the
 * Monaco assets from a CDN rather than bundling ~5MB of editor code into
 * this app's own build; disclosed here since it's a real dependency, not
 * an oversight.
 */
import { useEffect, useRef } from 'react';
import Editor from '@monaco-editor/react';
import { connectionApi } from '@/services/api/connectionApi.js';

// Module-level cache: { [connectionId]: { tables: [{schema,name}], columnsByTable: {qname: [names]} } }
// Shared across every SqlEditor instance/tab so switching tabs on the
// same connection doesn't re-fetch metadata Monaco already learned.
const _metaCache = new Map();

async function loadConnectionMeta(connectionId) {
  if (_metaCache.has(connectionId)) return _metaCache.get(connectionId);
  const meta = { tables: [], columnsByTable: {} };
  try {
    const { schemas } = await connectionApi.getSchemas(connectionId);
    const schemaList = schemas.length ? schemas : [null];
    for (const schema of schemaList) {
      try {
        const { items } = await connectionApi.getObjects(connectionId, { schema: schema ?? undefined });
        for (const o of items) meta.tables.push({ schema: schema || o.schema_name, name: o.name });
      } catch { /* connector may not support browsing for this type — leave tables empty */ }
    }
  } catch { /* connection may not support schema browsing at all — completion just won't offer tables */ }
  _metaCache.set(connectionId, meta);
  return meta;
}

async function loadTableColumns(connectionId, schema, table) {
  const meta = await loadConnectionMeta(connectionId);
  const qname = `${schema || ''}.${table}`;
  if (meta.columnsByTable[qname]) return meta.columnsByTable[qname];
  try {
    const { columns } = await connectionApi.getColumns(connectionId, table, { schema: schema || undefined });
    const names = columns.map((c) => c.name);
    meta.columnsByTable[qname] = names;
    return names;
  } catch {
    return [];
  }
}

// Bust the cache for a connection after a DDL/write statement runs —
// called from SqlWorkspace after a successful Run. Silent/lazy by design:
// this only clears the cached metadata, it doesn't eagerly re-fetch, so
// autocomplete just picks up fresh data next time it's invoked. (The
// DatabaseExplorer tree intentionally does NOT get an equivalent bust —
// see its own comment for why an eager cache-clear there caused a visible
// "tree goes blank" bug instead of a harmless lazy refresh.)
export function invalidateEditorMeta(connectionId) {
  _metaCache.delete(connectionId);
}

let _providerRegistered = false;
// Holds whatever the currently-focused editor's connectionId is — the
// completion provider is registered once globally (Monaco has one
// language-completion registry, not one per editor instance) and reads
// this ref at suggestion time to know which connection's metadata to use.
const _activeConnectionRef = { current: null };

function registerSqlCompletionProvider(monaco) {
  if (_providerRegistered) return;
  _providerRegistered = true;
  monaco.languages.registerCompletionItemProvider('sql', {
    triggerCharacters: ['.', ' '],
    provideCompletionItems: async (model, position) => {
      const connectionId = _activeConnectionRef.current;
      if (!connectionId) return { suggestions: [] };

      const textUntil = model.getValueInRange({ startLineNumber: position.lineNumber, startColumn: 1, endLineNumber: position.lineNumber, endColumn: position.column });
      const dotMatch = textUntil.match(/([A-Za-z_][\w]*)\.\s*$/);
      const word = model.getWordUntilPosition(position);
      const range = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: word.startColumn, endColumn: word.endColumn };

      const meta = await loadConnectionMeta(connectionId);

      if (dotMatch) {
        // "table." just typed — offer that table's real columns.
        const typed = dotMatch[1];
        const match = meta.tables.find((t) => t.name.toLowerCase() === typed.toLowerCase());
        if (!match) return { suggestions: [] };
        const columns = await loadTableColumns(connectionId, match.schema, match.name);
        return {
          suggestions: columns.map((name) => ({
            label: name, kind: monaco.languages.CompletionItemKind.Field, insertText: name, range,
          })),
        };
      }

      // Otherwise offer real table names (qualified with schema when there is one).
      return {
        suggestions: meta.tables.map((t) => ({
          label: t.name,
          kind: monaco.languages.CompletionItemKind.Struct,
          insertText: t.name,
          detail: t.schema || undefined,
          range,
        })),
      };
    },
  });
}

export default function SqlEditor({ value, onChange, onRun, onSave, onDropSql, connectionId, height }) {
  const editorRef = useRef(null);
  const monacoRef = useRef(null);

  useEffect(() => { _activeConnectionRef.current = connectionId; }, [connectionId]);
  useEffect(() => { if (connectionId) loadConnectionMeta(connectionId); }, [connectionId]);

  const handleMount = (editor, monaco) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
    registerSqlCompletionProvider(monaco);

    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => onRun?.());
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => onSave?.());

    // Drag-and-drop from DatabaseExplorer — same payload convention as
    // the plain-textarea version this replaced (a real generated SELECT
    // string set as text/plain in DatabaseExplorer's onDragStart).
    const domNode = editor.getDomNode();
    if (domNode) {
      domNode.addEventListener('dragover', (e) => e.preventDefault());
      domNode.addEventListener('drop', (e) => {
        e.preventDefault();
        const sql = e.dataTransfer.getData('text/plain');
        if (sql) onDropSql?.(sql);
      });
    }
  };

  return (
    <div className="sqlw-monaco-wrap" style={height ? { height, flex: 'none' } : undefined}>
      <Editor
        height="100%"
        language="sql"
        theme="vs"
        value={value}
        onChange={(v) => onChange(v ?? '')}
        onMount={handleMount}
        options={{
          minimap: { enabled: false },
          fontSize: 13,
          fontFamily: 'var(--font-mono)',
          lineNumbers: 'on',
          scrollBeyondLastLine: false,
          automaticLayout: true,
          wordWrap: 'on',
          folding: true,
          renderLineHighlight: 'line',
          tabSize: 2,
          padding: { top: 10, bottom: 10 },
        }}
      />
    </div>
  );
}
