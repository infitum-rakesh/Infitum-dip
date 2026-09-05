/**
 * AnomalyExecutionDetail — /app/anomaly/executions/:executionId
 * Full drill-down for one real AnomalyExecution: summary KPIs, an
 * ALL-columns status table (including PASS — see models/anomaly.py's
 * module docstring on why every selected column always gets a row, not
 * just the ones that found something), a per-column drill-in panel
 * (baseline vs current stats + a hand-rolled distribution bar), the
 * column's anomalous records (severity-filterable, paginated), and real
 * feedback actions (True Anomaly / False Positive / Expected / Ignored)
 * per record — never auto-suppresses future anomalies, just logs an
 * append-only signal (see anomaly_service.create_feedback's docstring).
 */
import { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { StatusBadge } from '@/components/common/index.js';
import { formatDateTime, formatNumber } from '@/utils/formatters.js';
import { anomalyApi, FEEDBACK_STATUSES } from '@/services/api/anomalyApi.js';
import './anomaly-detection.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

const RUN_STATUS_VARIANT = { SUCCESS: 'success', PARTIAL_SUCCESS: 'warning', FAILED: 'danger', RUNNING: 'info', QUEUED: 'gray', CANCELLED: 'gray' };
const OVERALL_STATUS_VARIANT = { PASS: 'success', WARNING: 'warning', CRITICAL: 'danger', ERROR: 'gray' };
const COL_STATUS_VARIANT = { PASS: 'success', INFO: 'gray', WARNING: 'warning', CRITICAL: 'danger', ERROR: 'danger', NOT_CHECKED: 'gray' };
const SEVERITY_VARIANT = { CRITICAL: 'danger', HIGH: 'danger', MEDIUM: 'warning', LOW: 'gray' };
const FEEDBACK_LABEL = { TRUE_ANOMALY: 'True Anomaly', FALSE_POSITIVE: 'False Positive', EXPECTED: 'Expected', IGNORED: 'Ignored' };

/** Real stats table — same dynamic key rendering AnomalyJobSetup's
 * ProfilingPanel uses, side by side for baseline vs current so a drift
 * is visually obvious without a separate diff engine. */
function StatsCompare({ baseline, current }) {
  const keys = useMemo(() => {
    const skip = new Set(['sampled', 'sample_size', 'category_counts', 'categories_truncated', 'detector_error', 'anomaly_records_truncated']);
    const all = new Set([...Object.keys(baseline || {}), ...Object.keys(current || {})]);
    return [...all].filter((k) => !skip.has(k));
  }, [baseline, current]);

  if (!keys.length) return <div className="anm-profile-empty">No stats recorded for this column.</div>;

  return (
    <div className="anm-profile-table-wrap">
      <table className="anm-profile-table">
        <thead><tr><th>Stat</th><th>Baseline</th><th>Current</th></tr></thead>
        <tbody>
          {keys.map((k) => {
            const bv = baseline?.[k];
            const cv = current?.[k];
            const fmt = (v) => (v === null || v === undefined ? '—' : (typeof v === 'number' ? (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(3)) : String(v)));
            const differs = bv !== undefined && cv !== undefined && fmt(bv) !== fmt(cv);
            return (
              <tr key={k}>
                <td>{k}</td>
                <td>{fmt(bv)}</td>
                <td style={differs ? { color: 'var(--color-warning-dark)', fontWeight: 700 } : undefined}>{fmt(cv)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Hand-rolled horizontal bar distribution — category_counts when the
 * column is categorical, or the p1/p25/p50/p75/p99 spread when numeric.
 * Same inline-SVG/CSS convention every chart in this app uses (see
 * AnomalyOverview.jsx's StatusDonut) — no charting library. */
function DistributionBar({ current }) {
  if (current?.category_counts) {
    const entries = Object.entries(current.category_counts).sort((a, b) => b[1] - a[1]).slice(0, 10);
    const max = Math.max(...entries.map(([, n]) => n), 1);
    return (
      <div className="anm-dist-bars">
        {entries.map(([cat, n]) => (
          <div key={cat} className="anm-dist-bar-row">
            <span className="anm-dist-bar-label" title={cat}>{cat}</span>
            <div className="anm-dist-bar-track">
              <div className="anm-dist-bar-fill" style={{ width: `${(n / max) * 100}%` }} />
            </div>
            <span className="anm-dist-bar-val">{n.toLocaleString()}</span>
          </div>
        ))}
        {current.categories_truncated && <div className="anm-setup-section-hint">Category list truncated — showing top values only.</div>}
      </div>
    );
  }
  const pcts = ['p1', 'p25', 'p50', 'p75', 'p99'].filter((k) => current?.[k] != null);
  if (!pcts.length) return null;
  const min = current[pcts[0]];
  const max = current[pcts[pcts.length - 1]];
  const span = max - min || 1;
  return (
    <div className="anm-dist-range">
      <div className="anm-dist-range-track">
        {pcts.map((k) => (
          <div key={k} className="anm-dist-range-tick" style={{ left: `${((current[k] - min) / span) * 100}%` }} title={`${k}: ${current[k]}`} />
        ))}
      </div>
      <div className="anm-dist-range-labels">
        {pcts.map((k) => <span key={k}>{k}: {typeof current[k] === 'number' ? current[k].toFixed(2) : current[k]}</span>)}
      </div>
    </div>
  );
}

function RecordRow({ record, onFeedback }) {
  const [submitting, setSubmitting] = useState(false);
  const [lastFeedback, setLastFeedback] = useState(null);

  const submit = async (status) => {
    setSubmitting(true);
    try {
      const row = await onFeedback(record.id, status);
      setLastFeedback(row);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <tr>
      <td>{record.record_identifier ?? '—'}</td>
      <td>{record.value ?? '—'}</td>
      <td>{record.expected_range ?? '—'}</td>
      <td>{record.anomaly_type}</td>
      <td>{record.score?.toFixed?.(2) ?? record.score}</td>
      <td><StatusBadge variant={SEVERITY_VARIANT[record.severity] || 'gray'} dot size="sm">{record.severity}</StatusBadge></td>
      <td>{formatDateTime(record.detected_at)}</td>
      <td>
        <select
          className="anm-select" style={{ fontSize: 11.5, padding: '3px 6px' }}
          disabled={submitting} value={lastFeedback?.status || ''}
          onChange={(e) => e.target.value && submit(e.target.value)}
        >
          <option value="">{lastFeedback ? FEEDBACK_LABEL[lastFeedback.status] : 'Mark as…'}</option>
          {FEEDBACK_STATUSES.map((s) => <option key={s} value={s}>{FEEDBACK_LABEL[s]}</option>)}
        </select>
      </td>
    </tr>
  );
}

export default function AnomalyExecutionDetail() {
  const { executionId } = useParams();
  const [execution, setExecution] = useState(null);
  const [columns, setColumns] = useState([]);
  const [selectedColId, setSelectedColId] = useState(null);
  const [records, setRecords] = useState([]);
  const [recordsLoading, setRecordsLoading] = useState(false);
  const [severityFilter, setSeverityFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    setLoading(true); setError('');
    Promise.all([anomalyApi.getExecution(executionId), anomalyApi.listColumnResults(executionId)])
      .then(([exec, cols]) => {
        setExecution(exec);
        setColumns(cols);
        const firstWithAnomalies = cols.find((c) => c.anomalies > 0) || cols[0];
        setSelectedColId(firstWithAnomalies?.id ?? null);
      })
      .catch((err) => setError(apiErrorMessage(err, 'Could not load this execution.')))
      .finally(() => setLoading(false));
  }, [executionId]);

  useEffect(() => {
    if (!selectedColId) { setRecords([]); return; }
    setRecordsLoading(true);
    anomalyApi.listRecords(selectedColId, severityFilter ? { severity: severityFilter, limit: 200 } : { limit: 200 })
      .then((r) => setRecords(r.items))
      .catch(() => setRecords([]))
      .finally(() => setRecordsLoading(false));
  }, [selectedColId, severityFilter]);

  const selectedCol = columns.find((c) => c.id === selectedColId) || null;

  const handleFeedback = async (recordId, status) => {
    return anomalyApi.createFeedback(recordId, { status });
  };

  if (loading) return <div><p style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</p></div>;
  if (error) return <div className="anm-error-banner">{error}</div>;
  if (!execution) return null;

  return (
    <div>
      <div className="anm-header">
        <div>
          <Link to="/app/anomaly/results" className="anm-back-link"><ArrowLeft size={13} /> All results</Link>
          <h1 className="anm-title">Execution #{execution.id}</h1>
          <p className="anm-subtitle">
            <StatusBadge variant={RUN_STATUS_VARIANT[execution.status] || 'gray'}>{execution.status}</StatusBadge>
            {execution.overall_status && <StatusBadge variant={OVERALL_STATUS_VARIANT[execution.overall_status] || 'gray'} dot>{execution.overall_status}</StatusBadge>}
            <span style={{ marginLeft: 8 }}>Trigger: {execution.trigger}</span>
            {execution.total_ms != null && <span> · {execution.total_ms}ms</span>}
          </p>
        </div>
      </div>

      {execution.status === 'FAILED' && (
        <div className="anm-error-banner">{execution.error_message}</div>
      )}

      <div className="anm-run-summary" style={{ marginBottom: 20 }}>
        <div className="anm-run-summary-card">
          <div className="anm-run-summary-val">{formatNumber(execution.records_scanned || 0)}</div>
          <div className="anm-run-summary-label">Records Scanned</div>
        </div>
        <div className="anm-run-summary-card">
          <div className="anm-run-summary-val">{execution.columns_scanned ?? 0}</div>
          <div className="anm-run-summary-label">Columns Scanned</div>
        </div>
        <div className="anm-run-summary-card">
          <div className="anm-run-summary-val">{formatNumber(execution.anomalies_found || 0)}</div>
          <div className="anm-run-summary-label">Anomalies Found</div>
        </div>
        <div className="anm-run-summary-card">
          <div className="anm-run-summary-val critical">{execution.critical_count ?? 0}</div>
          <div className="anm-run-summary-label">Critical</div>
        </div>
        <div className="anm-run-summary-card">
          <div className="anm-run-summary-val warning">{execution.warning_count ?? 0}</div>
          <div className="anm-run-summary-label">Warning</div>
        </div>
      </div>

      {(execution.profiling_ms != null || execution.detection_ms != null || execution.db_ms != null || execution.transfer_ms != null) && (
        <div className="anm-panel" style={{ marginBottom: 20 }}>
          <div className="anm-panel-title">Performance</div>
          <div className="anm-run-summary">
            <div className="anm-run-summary-card">
              <div className="anm-run-summary-val">{execution.profiling_ms != null ? `${formatNumber(execution.profiling_ms)}ms` : '—'}</div>
              <div className="anm-run-summary-label">Profiling</div>
            </div>
            <div className="anm-run-summary-card">
              <div className="anm-run-summary-val">{execution.detection_ms != null ? `${formatNumber(execution.detection_ms)}ms` : '—'}</div>
              <div className="anm-run-summary-label">Detection</div>
            </div>
            <div className="anm-run-summary-card">
              <div className="anm-run-summary-val">{execution.db_ms != null ? `${formatNumber(execution.db_ms)}ms` : '—'}</div>
              <div className="anm-run-summary-label">DB Time</div>
            </div>
            <div className="anm-run-summary-card">
              <div className="anm-run-summary-val">{execution.transfer_ms != null ? `${formatNumber(execution.transfer_ms)}ms` : '—'}</div>
              <div className="anm-run-summary-label">Transfer</div>
            </div>
            <div className="anm-run-summary-card">
              <div className="anm-run-summary-val">{execution.total_ms != null ? `${formatNumber(execution.total_ms)}ms` : '—'}</div>
              <div className="anm-run-summary-label">Total</div>
            </div>
          </div>
          {execution.records_scanned > 0 && execution.total_ms != null && (
            <div style={{ fontSize: 11.5, color: 'var(--text-muted)', marginTop: 8 }}>
              {formatNumber(Math.round(execution.records_scanned / Math.max(execution.total_ms, 1) * 1000))} records/sec
            </div>
          )}
        </div>
      )}

      <div className="anm-panel" style={{ marginBottom: 20 }}>
        <div className="anm-panel-title">Columns ({columns.length})</div>
        <table className="anm-table">
          <thead>
            <tr>
              <th>Column</th><th>Type</th><th>Records</th><th>Anomalies</th><th>Rate</th><th>Severity</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {columns.map((c) => (
              <tr
                key={c.id} onClick={() => setSelectedColId(c.id)}
                style={{ cursor: 'pointer', background: selectedColId === c.id ? 'var(--color-primary-light)' : undefined }}
              >
                <td style={{ fontWeight: selectedColId === c.id ? 700 : 400 }}>{c.column_name}</td>
                <td>{c.data_type}</td>
                <td>{formatNumber(c.records)}</td>
                <td>{formatNumber(c.anomalies)}</td>
                <td>{(c.anomaly_rate * 100).toFixed(2)}%</td>
                <td>{c.severity ? <StatusBadge variant={SEVERITY_VARIANT[c.severity] || 'gray'} dot size="sm">{c.severity}</StatusBadge> : '—'}</td>
                <td><StatusBadge variant={COL_STATUS_VARIANT[c.status] || 'gray'} size="sm">{c.status}</StatusBadge></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selectedCol && (
        <>
          <div className="anm-exec-panels" style={{ marginBottom: 20 }}>
            <div className="anm-panel">
              <div className="anm-panel-title">Baseline vs Current — {selectedCol.column_name}</div>
              <StatsCompare baseline={selectedCol.baseline_stats} current={selectedCol.current_stats} />
            </div>
            <div className="anm-panel">
              <div className="anm-panel-title">Distribution</div>
              <DistributionBar current={selectedCol.current_stats} />
              {!selectedCol.current_stats?.category_counts && !selectedCol.current_stats?.p1 && (
                <div className="anm-empty-inline">No distribution data for this column type.</div>
              )}
            </div>
          </div>

          <div className="anm-panel">
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
              <div className="anm-panel-title" style={{ marginBottom: 0 }}>
                Anomalous Records — {selectedCol.column_name} ({records.length})
              </div>
              <select className="anm-select" value={severityFilter} onChange={(e) => setSeverityFilter(e.target.value)}>
                <option value="">All severities</option>
                <option value="CRITICAL">Critical</option>
                <option value="HIGH">High</option>
                <option value="MEDIUM">Medium</option>
                <option value="LOW">Low</option>
              </select>
            </div>
            {recordsLoading ? (
              <div className="anm-empty-inline">Loading…</div>
            ) : records.length === 0 ? (
              <div className="anm-empty-inline">No anomalous records for this column{severityFilter ? ' at this severity' : ''}.</div>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table className="anm-table">
                  <thead>
                    <tr>
                      <th>Identifier</th><th>Value</th><th>Expected Range</th><th>Type</th><th>Score</th><th>Severity</th><th>Detected</th><th>Feedback</th>
                    </tr>
                  </thead>
                  <tbody>
                    {records.map((r) => <RecordRow key={r.id} record={r} onFeedback={handleFeedback} />)}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
