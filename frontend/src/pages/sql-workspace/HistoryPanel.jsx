/**
 * HistoryPanel — SQL Workspace sidebar tab. Real, append-only execution
 * history — every row is a genuine past /run attempt (success AND
 * failure), logged server-side by sql_workspace_service.run_sql() (see
 * backend/app/models/query_execution_log.py). Non-elevated roles only
 * ever see their own history (enforced server-side, not just hidden in
 * the UI); org admins/super admins default to the whole org and can
 * narrow to "Just me". Clicking an entry loads its connection + SQL text
 * back into the active tab — it does NOT re-run it, same human-in-the-
 * loop principle as the AI Assistant's "Insert into Editor".
 */
import { useEffect, useState } from 'react';
import { History, Loader2, CheckCircle2, XCircle, Sparkles, RotateCcw } from 'lucide-react';
import { EmptyState } from '@/components/common/index.js';
import { useAuth } from '@/hooks/useAuth.js';
import { sqlWorkspaceApi } from '@/services/api/sqlWorkspaceApi.js';

const ELEVATED_ROLES = ['super_admin', 'org_admin'];
const PAGE_SIZE = 50;

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    });
  } catch { return iso; }
}

export default function HistoryPanel({ refreshKey, onLoadEntry }) {
  const { user } = useAuth();
  const elevated = ELEVATED_ROLES.includes(user?.role);

  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState('');
  const [scope, setScope] = useState('all'); // only meaningful when `elevated`

  const load = () => {
    setLoading(true);
    sqlWorkspaceApi.listHistory({
      status: statusFilter || undefined,
      scope: elevated ? scope : 'mine',
      page_size: PAGE_SIZE,
    })
      .then((r) => { setItems(r.items ?? []); setTotal(r.total ?? 0); })
      .catch(() => { setItems([]); setTotal(0); })
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [refreshKey, statusFilter, scope]);

  return (
    <div className="sqlw-history-panel">
      <div className="sqlw-history-filters">
        <select className="sqlw-select sqlw-select-narrow" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">All statuses</option>
          <option value="SUCCESS">Success</option>
          <option value="ERROR">Error</option>
        </select>
        {elevated && (
          <select className="sqlw-select sqlw-select-narrow" value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="all">Whole org</option>
            <option value="mine">Just me</option>
          </select>
        )}
        <button className="sqlw-history-refresh" onClick={load} title="Refresh"><RotateCcw size={13} /></button>
      </div>

      {loading && <div className="sqlw-tree-empty"><Loader2 size={14} className="sqlw-spin" /> Loading…</div>}

      {!loading && items.length === 0 && (
        <EmptyState icon={<History size={28} />} title="No history yet" description="Run a query and it will show up here." />
      )}

      {!loading && items.map((item) => (
        <button key={item.id} className="sqlw-history-row" onClick={() => onLoadEntry(item)} title="Load into editor">
          <div className="sqlw-history-row-top">
            {item.status === 'SUCCESS'
              ? <CheckCircle2 size={13} className="sqlw-history-ok" />
              : <XCircle size={13} className="sqlw-history-err" />}
            <span className="sqlw-history-cmd">{item.statement_command}</span>
            {item.source === 'AI_GENERATED' && <Sparkles size={11} className="sqlw-history-ai" title="AI-generated" />}
            <span className="sqlw-history-time">{fmtTime(item.created_at)}</span>
          </div>
          <div className="sqlw-history-sql">{item.sql_text}</div>
          <div className="sqlw-history-meta">
            <span className="sqlw-history-conn">{item.connection_name || `Connection #${item.connection_id}`}</span>
            {item.status === 'SUCCESS' ? (
              <span>{item.row_count ?? 0} row{item.row_count === 1 ? '' : 's'} · {item.duration_ms} ms</span>
            ) : (
              <span className="sqlw-history-errtext">{item.error_message}</span>
            )}
            {elevated && item.user_name && <span className="sqlw-history-user">{item.user_name}</span>}
          </div>
        </button>
      ))}

      {!loading && total > items.length && (
        <div className="sqlw-tree-empty small">Showing {items.length} of {total} — narrow the filters to see more.</div>
      )}
    </div>
  );
}
