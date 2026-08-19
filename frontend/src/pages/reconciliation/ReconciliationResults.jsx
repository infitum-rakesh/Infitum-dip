/**
 * ReconciliationResults — real summary counts, match bar, filterable
 * exceptions table (persisted, capped sample — see
 * backend/app/services/reconciliation_service.py's MAX_PERSISTED_EXCEPTIONS;
 * the summary counts above are always the true, complete totals even
 * when the exception list itself is truncated).
 */
import { useState, useEffect } from 'react';
import { RotateCw } from 'lucide-react';
import { Button, StatusBadge } from '@/components/common/index.js';
import { reconciliationApi } from '@/services/api/reconciliationApi.js';
import './reconciliation.css';

const EXCEPTION_TYPE_VARIANT = { MISMATCH: 'warning', MISSING: 'danger', EXTRA: 'info' };
const EXCEPTION_TYPE_LABEL = { MISMATCH: 'Mismatch', MISSING: 'Missing', EXTRA: 'Extra', all: 'All' };
const FILTER_OPTIONS = ['all', 'MISMATCH', 'MISSING', 'EXTRA'];

function keyText(keyValue) {
  if (!keyValue) return '—';
  return Object.values(keyValue).join(', ');
}

export function ReconciliationResults({ run, sourceObject, targetObject, onReRun }) {
  const [filter, setFilter] = useState('all');
  const [exceptions, setExceptions] = useState([]);
  const [excLoading, setExcLoading] = useState(false);

  useEffect(() => {
    if (!run || run.status !== 'SUCCEEDED') return;
    setExcLoading(true);
    reconciliationApi.getExceptions(run.id, filter === 'all' ? {} : { exception_type: filter })
      .then(({ items }) => setExceptions(items))
      .finally(() => setExcLoading(false));
  }, [run, filter]);

  if (!run) {
    return (
      <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
        <div style={{ fontSize: 36, marginBottom: 12 }}>⚖️</div>
        <p style={{ fontWeight: 600, fontSize: 14, color: 'var(--text-primary)', marginBottom: 6 }}>No reconciliation results</p>
        <p>Configure source and target, then click "Run Reconciliation".</p>
      </div>
    );
  }

  if (run.status === 'FAILED') {
    return (
      <div style={{ padding: 32 }}>
        <div style={{ padding: '12px 16px', background: 'var(--color-danger-bg)', color: 'var(--color-danger-dark)', borderRadius: 8, fontSize: 13 }}>
          {run.error_message}
        </div>
      </div>
    );
  }

  const sourceCount = run.source_count ?? 0;
  const hasKeyDiff = run.missing_count != null;
  const matched = run.matched_count ?? 0;
  const matchRate = sourceCount > 0 ? ((matched / sourceCount) * 100).toFixed(1) : '—';
  const duration = run.started_at && run.completed_at ? `${((new Date(run.completed_at) - new Date(run.started_at)) / 1000).toFixed(1)}s` : '—';

  const pct = (n) => (sourceCount > 0 ? (n / sourceCount) * 100 : 0);

  return (
    <div>
      <div style={{ padding: '10px 16px', background: '#F8FAFC', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', fontSize: 'var(--fs-sm)' }}>
        <span style={{ color: 'var(--text-muted)' }}>Source: <strong style={{ color: 'var(--text-primary)' }}>{sourceObject}</strong></span>
        <span style={{ color: 'var(--text-muted)' }}>→</span>
        <span style={{ color: 'var(--text-muted)' }}>Target: <strong style={{ color: 'var(--text-primary)' }}>{targetObject}</strong></span>
        <span style={{ color: 'var(--text-muted)' }}>·</span>
        <span style={{ color: 'var(--text-muted)' }}>{duration}</span>
        <div style={{ marginLeft: 'auto' }}>
          <Button variant="secondary" size="sm" icon={<RotateCw size={12} />} onClick={onReRun}>Re-run</Button>
        </div>
      </div>

      <div className="rcn-summary-grid">
        {[
          { label: 'Source Count', val: sourceCount.toLocaleString(), cls: 'neutral' },
          { label: 'Target Count', val: (run.target_count ?? 0).toLocaleString(), cls: 'neutral' },
          { label: 'Matched', val: hasKeyDiff ? matched.toLocaleString() : '—', cls: 'success', border: hasKeyDiff ? 'bordered-success' : '' },
          { label: 'Missing', val: hasKeyDiff ? run.missing_count.toLocaleString() : '—', cls: run.missing_count > 0 ? 'danger' : 'neutral', border: run.missing_count > 0 ? 'bordered-danger' : '' },
          { label: 'Extra', val: hasKeyDiff ? run.extra_count.toLocaleString() : '—', cls: run.extra_count > 0 ? 'warning' : 'neutral', border: run.extra_count > 0 ? 'bordered-warning' : '' },
          { label: 'Mismatched', val: hasKeyDiff ? run.mismatched_count.toLocaleString() : '—', cls: run.mismatched_count > 0 ? 'warning' : 'neutral', border: run.mismatched_count > 0 ? 'bordered-warning' : '' },
        ].map((c) => (
          <div key={c.label} className={`rcn-count-card ${c.border ?? ''}`}>
            <div className="rcn-count-label">{c.label}</div>
            <div className={`rcn-count-val ${c.cls}`}>{c.val}</div>
          </div>
        ))}
      </div>

      {hasKeyDiff && (
        <div className="rcn-match-bar-wrap">
          <div className="rcn-match-bar-hdr">
            <span>Match rate</span>
            <span style={{ fontWeight: 700, color: parseFloat(matchRate) >= 99 ? 'var(--color-success-dark)' : parseFloat(matchRate) >= 95 ? 'var(--color-warning-dark)' : 'var(--color-danger-dark)' }}>
              {matchRate}% matched
            </span>
          </div>
          <div className="rcn-match-bar">
            <div className="rcn-match-seg" style={{ width: `${pct(matched)}%`, background: 'var(--color-success)' }} />
            <div className="rcn-match-seg" style={{ width: `${pct(run.mismatched_count)}%`, background: 'var(--color-warning)' }} />
            <div className="rcn-match-seg" style={{ width: `${pct(run.missing_count)}%`, background: 'var(--color-danger)' }} />
            <div className="rcn-match-seg" style={{ width: `${pct(run.extra_count)}%`, background: 'var(--color-info)' }} />
          </div>
          <div style={{ display: 'flex', gap: 16, marginTop: 8, flexWrap: 'wrap' }}>
            {[['#10B981', 'Matched'], ['#F59E0B', 'Mismatched'], ['#EF4444', 'Missing'], ['#3B82F6', 'Extra']].map(([c, l]) => (
              <div key={l} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, color: 'var(--text-muted)' }}>
                <div style={{ width: 8, height: 8, borderRadius: '50%', background: c, flexShrink: 0 }} />{l}
              </div>
            ))}
          </div>
        </div>
      )}

      {run.checksum_match != null && (
        <div style={{ padding: '12px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-muted)' }}>Checksum comparison:</span>
          <StatusBadge variant={run.checksum_match ? 'success' : 'danger'} dot>
            {run.checksum_match ? 'Match' : 'Mismatch — datasets differ'}
          </StatusBadge>
        </div>
      )}

      {hasKeyDiff && (
        <>
          <div className="rcn-filter-row">
            <span style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-muted)', fontWeight: 500 }}>Filter:</span>
            {FILTER_OPTIONS.map((f) => (
              <button key={f} className={`rcn-filter-btn ${filter === f ? 'active' : ''}`} onClick={() => setFilter(f)}>
                {EXCEPTION_TYPE_LABEL[f]}
              </button>
            ))}
            <span style={{ marginLeft: 'auto', fontSize: 'var(--fs-sm)', color: 'var(--text-muted)' }}>
              {exceptions.length} shown{run.exceptions_truncated ? ' (sample truncated — totals above are exact)' : ''}
            </span>
          </div>

          <div className="rcn-exc-row" style={{ background: '#F8FAFC', borderBottom: '1px solid var(--border)', cursor: 'default' }}>
            {['Key', 'Column', 'Source Value', 'Target Value', 'Type'].map((h) => (
              <span key={h} style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>{h}</span>
            ))}
          </div>

          {excLoading ? (
            <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>Loading exceptions…</div>
          ) : exceptions.length === 0 ? (
            <div style={{ padding: 48, textAlign: 'center' }}>
              <div style={{ fontSize: 32, marginBottom: 10 }}>✅</div>
              <p style={{ fontSize: 14, fontWeight: 600, color: 'var(--color-success-dark)' }}>No exceptions found</p>
              <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 4 }}>Source and target match for the selected filter.</p>
            </div>
          ) : (
            exceptions.map((ex) => (
              <div key={ex.id} className="rcn-exc-row">
                <span className="rcn-exc-key">{keyText(ex.key_value)}</span>
                <span className="rcn-exc-col">{ex.column ?? '—'}</span>
                <span className="rcn-exc-val">{ex.source_value ?? '—'}</span>
                <span className="rcn-exc-val">{ex.target_value ?? '—'}</span>
                <StatusBadge variant={EXCEPTION_TYPE_VARIANT[ex.exception_type] ?? 'gray'} dot size="sm">
                  {EXCEPTION_TYPE_LABEL[ex.exception_type] ?? ex.exception_type}
                </StatusBadge>
              </div>
            ))
          )}
        </>
      )}
    </div>
  );
}
