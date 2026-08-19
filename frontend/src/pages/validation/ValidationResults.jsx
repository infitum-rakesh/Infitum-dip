/**
 * ValidationResults — summary metrics, rule-by-rule table, live
 * failed-record drill-down (re-runs the exact SQL condition this run
 * evaluated — see backend/app/validation/engine.py). Props: run (from
 * validationApi.runRuleSet/getRun), objectName, onReRun.
 */
import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { DataTable, StatusBadge, Button } from '@/components/common/index.js';
import { RULE_TYPES, validationApi } from '@/services/api/validationApi.js';
import './validation.css';

function DonutChart({ passed, failed, warning, total, size = 100 }) {
  const sw = 12;
  const r = (size - sw) / 2;
  const c = 2 * Math.PI * r;
  const seg = (v) => (v / total) * c;
  const passLen = seg(passed);
  const warnLen = seg(warning);
  const failLen = seg(failed);
  let cum = 0;
  const arc = (len, color, key) => {
    const offset = c / 4 - cum;
    cum += len;
    return len > 0 ? (
      <circle key={key} cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={sw} strokeDasharray={`${len} ${c - len}`} strokeDashoffset={offset} />
    ) : null;
  };
  const pct = total > 0 ? Math.round((passed / total) * 100) : 0;
  return (
    <div className="vld-donut-wrap">
      <div style={{ position: 'relative', flexShrink: 0 }}>
        <svg width={size} height={size}>
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#F1F5F9" strokeWidth={sw} />
          {arc(passLen, '#10B981', 'pass')}
          {arc(warnLen, '#F59E0B', 'warn')}
          {arc(failLen, '#EF4444', 'fail')}
        </svg>
        <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
          <span style={{ fontSize: 22, fontWeight: 800, color: 'var(--text-primary)', lineHeight: 1 }}>{pct}%</span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontWeight: 600 }}>Success</span>
        </div>
      </div>
      <div className="vld-donut-legend">
        {[
          { color: '#10B981', val: passed.toLocaleString(), label: 'Passed' },
          { color: '#F59E0B', val: warning.toLocaleString(), label: 'Warning' },
          { color: '#EF4444', val: failed.toLocaleString(), label: 'Failed' },
        ].map((s) => (
          <div key={s.label} className="vld-donut-legend-item">
            <div className="vld-donut-legend-dot" style={{ background: s.color }} />
            <span className="vld-donut-legend-val">{s.val}</span>
            <span className="vld-donut-legend-lbl">{s.label} rules</span>
          </div>
        ))}
      </div>
    </div>
  );
}

const RULE_STATUS_VARIANT = { PASSED: 'success', FAILED: 'danger', WARNING: 'warning' };

export function ValidationResults({ run, objectName, onReRun }) {
  const [selectedRuleId, setSelectedRuleId] = useState(null);
  const [failedTable, setFailedTable] = useState(null);
  const [loadingRows, setLoadingRows] = useState(false);

  if (!run) {
    return (
      <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
        <div style={{ fontSize: 36, marginBottom: 12 }}>▶</div>
        <p style={{ fontWeight: 600, fontSize: 14, color: 'var(--text-primary)', marginBottom: 6 }}>No results yet</p>
        <p>Click "Run Validation" to execute all enabled rules.</p>
      </div>
    );
  }

  const results = run.results || [];
  const total = results.length;
  const passed = results.filter((r) => r.status === 'PASSED').length;
  const failed = results.filter((r) => r.status === 'FAILED').length;
  const warning = results.filter((r) => r.status === 'WARNING').length;
  const successPct = total > 0 ? ((passed / total) * 100).toFixed(1) : 0;
  const elapsedSec = run.started_at && run.completed_at
    ? ((new Date(run.completed_at) - new Date(run.started_at)) / 1000).toFixed(1)
    : null;

  const handleRuleClick = async (rr) => {
    if (rr.status === 'PASSED') return;
    if (selectedRuleId === rr.rule_id) { setSelectedRuleId(null); return; }
    setSelectedRuleId(rr.rule_id);
    setLoadingRows(true);
    try {
      const data = await validationApi.getFailedRecords(run.id, rr.rule_id, 50);
      setFailedTable(data);
    } finally { setLoadingRows(false); }
  };

  const failedRowCols = failedTable ? failedTable.columns.map((c) => ({ key: c, header: c.replace(/_/g, ' '), sortable: true })) : [];
  const failedRowData = failedTable ? failedTable.rows.map((row) => Object.fromEntries(failedTable.columns.map((c, i) => [c, row[i]]))) : [];

  return (
    <div>
      <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border)', background: '#F8FAFC', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 'var(--fs-sm)', fontWeight: 500, color: 'var(--text-muted)' }}>
          Object: <strong style={{ color: 'var(--text-primary)' }}>{objectName}</strong>
        </span>
        <span style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-muted)' }}>·</span>
        <span style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-muted)' }}>{(run.total_records ?? 0).toLocaleString()} records</span>
        {elapsedSec != null && <><span style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-muted)' }}>·</span><span style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-muted)' }}>{elapsedSec}s</span></>}
        <div style={{ marginLeft: 'auto' }}>
          <Button variant="secondary" size="sm" onClick={onReRun}>Re-run</Button>
        </div>
      </div>

      {run.status === 'FAILED' && (
        <div style={{ padding: '10px 16px', background: 'var(--color-danger-bg)', color: 'var(--color-danger-dark)', fontSize: 13 }}>
          {run.error_message}
        </div>
      )}

      {total > 0 && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: 20, padding: '20px 20px 0' }}>
            <div className="vld-summary" style={{ gridTemplateColumns: 'repeat(5, 1fr)', marginBottom: 0 }}>
              {[
                { val: total, label: 'Total Rules' },
                { val: passed, label: 'Passed', cls: 'highlight-success' },
                { val: failed, label: 'Failed', cls: 'highlight-danger' },
                { val: warning, label: 'Warning', cls: 'highlight-warning' },
                { val: `${successPct}%`, label: 'Success Rate', sub: `${(run.total_records ?? 0).toLocaleString()} records` },
              ].map((m) => (
                <div key={m.label} className={`vld-metric ${m.cls ?? ''}`}>
                  <div className="vld-metric-val">{m.val}</div>
                  <div className="vld-metric-label">{m.label}</div>
                  {m.sub && <div className="vld-metric-sub">{m.sub}</div>}
                </div>
              ))}
            </div>
            <div style={{ padding: '12px 20px 12px 0', flexShrink: 0 }}>
              <DonutChart passed={passed} failed={failed} warning={warning} total={total} size={108} />
            </div>
          </div>

          <div className="vld-quality-bar-wrap" style={{ padding: '12px 20px' }}>
            <div className="vld-quality-bar-title">
              <span style={{ fontWeight: 600, fontSize: 13 }}>Rule results</span>
              <span style={{ color: 'var(--color-success)', fontWeight: 700 }}>{successPct}% pass rate</span>
            </div>
            <div className="vld-quality-bar">
              <div className="vld-bar-seg" style={{ width: `${(passed / total) * 100}%`, background: 'var(--color-success)' }} />
              <div className="vld-bar-seg" style={{ width: `${(warning / total) * 100}%`, background: 'var(--color-warning)' }} />
              <div className="vld-bar-seg" style={{ width: `${(failed / total) * 100}%`, background: 'var(--color-danger)' }} />
            </div>
          </div>

          <div className="vld-result-row" style={{ background: '#F8FAFC', borderTop: '1px solid var(--border)', cursor: 'default' }}>
            {['Rule Name', 'Type', 'Column(s)', 'Status', 'Pass Rate', 'Records'].map((h) => (
              <span key={h} style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>{h}</span>
            ))}
          </div>

          {results.map((rr) => {
            const typeDef = RULE_TYPES[rr.rule_type] ?? {};
            const isOpen = selectedRuleId === rr.rule_id;
            const canDrill = rr.status !== 'PASSED';
            const fillCls = rr.status === 'PASSED' ? 'vld-pass-fill-success' : rr.status === 'WARNING' ? 'vld-pass-fill-warning' : 'vld-pass-fill-danger';
            return (
              <div key={rr.rule_id}>
                <div className={`vld-result-row ${isOpen ? 'selected' : ''}`} onClick={() => handleRuleClick(rr)}
                  style={{ cursor: canDrill ? 'pointer' : 'default' }} title={canDrill ? 'Click to inspect failing records' : undefined}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    {canDrill && (isOpen ? <ChevronDown size={13} style={{ flexShrink: 0, color: 'var(--color-primary)' }} /> : <ChevronRight size={13} style={{ flexShrink: 0, color: 'var(--text-muted)' }} />)}
                    <span className="vld-rule-name">{rr.rule_name}</span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 'var(--fs-xs)', color: 'var(--text-muted)' }}>
                    <span>{typeDef.icon}</span><span>{typeDef.label}</span>
                  </div>
                  <span className="vld-rule-col">{(rr.columns || []).join(', ')}</span>
                  <StatusBadge variant={RULE_STATUS_VARIANT[rr.status] ?? 'gray'} dot size="sm">{rr.status}</StatusBadge>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div className="vld-pass-bar"><div className={fillCls} style={{ width: `${rr.pass_rate}%` }} /></div>
                    <span style={{ fontSize: 11, fontWeight: 700, minWidth: 38, textAlign: 'right', color: rr.status === 'PASSED' ? 'var(--color-success-dark)' : rr.status === 'WARNING' ? 'var(--color-warning-dark)' : 'var(--color-danger-dark)' }}>{rr.pass_rate}%</span>
                  </div>
                  <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-muted)', textAlign: 'right' }}>
                    {rr.fail_count > 0
                      ? <span style={{ color: 'var(--color-danger-dark)', fontWeight: 600 }}>{rr.fail_count.toLocaleString()} issues</span>
                      : <span style={{ color: 'var(--color-success-dark)', fontWeight: 600 }}>✓ All pass</span>}
                  </div>
                </div>

                {isOpen && (
                  <div className="vld-drill-panel">
                    <div className="vld-drill-header">
                      <span className="vld-drill-title">Failing Records (live query) — {rr.rule_name}</span>
                      {failedTable?.truncated && <span style={{ fontSize: 11, color: 'var(--text-faint)' }}>showing first {failedTable.rows.length}</span>}
                    </div>
                    <DataTable
                      columns={failedRowCols}
                      data={failedRowData}
                      loading={loadingRows}
                      emptyState={<div style={{ padding: 20, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>No records to display</div>}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </>
      )}
    </div>
  );
}
