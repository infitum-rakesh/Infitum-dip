/**
 * VersionHistoryPanel — slide-over showing every saved version of the
 * current pipeline: view, restore (with confirmation — creates a NEW
 * version, never rewrites history) and compare any two versions.
 *
 * Backed by GET/POST /api/pipelines/{id}/versions... — see
 * backend/app/services/pipeline_service.py for the retention/restore rules
 * (only the last 5 versions are kept; restoring vN always creates a new
 * version on top, it never overwrites).
 */
import { useEffect, useState } from 'react';
import { X, History, RotateCcw, Eye, GitCompare, Plus, Minus, Pencil } from 'lucide-react';
import { Button } from '@/components/common/Button.jsx';
import { pipelineVersionApi } from '@/services/api/pipelineApi.js';
import './transformation.css';

function fmtDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
    + '  ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

// ── Restore confirmation ────────────────────────────────────────
function RestoreConfirm({ currentVersion, targetVersion, onCancel, onConfirm, restoring }) {
  return (
    <div className="vh-modal-backdrop" onMouseDown={onCancel}>
      <div className="vh-modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="vh-modal-title">Restore Pipeline?</div>
        <p className="vh-modal-body">
          This will create a new version based on v{targetVersion}.
        </p>
        <div className="vh-modal-meta">
          <div><span>Current version:</span> <strong>v{currentVersion}</strong></div>
          <div><span>Restoring:</span> <strong>v{targetVersion}</strong></div>
        </div>
        <div className="vh-modal-actions">
          <Button variant="secondary" size="sm" onClick={onCancel} disabled={restoring}>Cancel</Button>
          <Button variant="primary" size="sm" onClick={onConfirm} loading={restoring}>Restore</Button>
        </div>
      </div>
    </div>
  );
}

// ── Diff view ────────────────────────────────────────────────────
function DiffBadge({ change }) {
  if (change === 'added')    return <span className="vh-diff-badge added"><Plus size={10} /> Added</span>;
  if (change === 'removed')  return <span className="vh-diff-badge removed"><Minus size={10} /> Removed</span>;
  return <span className="vh-diff-badge modified"><Pencil size={10} /> Modified</span>;
}

function CompareView({ pipelineId, versionA, versionB, onClose }) {
  const [loading, setLoading] = useState(true);
  const [diff, setDiff] = useState([]);
  const [error, setError] = useState('');

  useEffect(() => {
    setLoading(true);
    setError('');
    pipelineVersionApi.compare(pipelineId, versionA, versionB)
      .then((r) => setDiff(r.diff))
      .catch(() => setError('Could not compare these versions.'))
      .finally(() => setLoading(false));
  }, [pipelineId, versionA, versionB]);

  return (
    <div className="vh-compare">
      <div className="vh-compare-header">
        <span>v{versionA}</span>
        <GitCompare size={13} />
        <span>v{versionB}</span>
        <Button variant="ghost" size="sm" onClick={onClose} style={{ marginLeft: 'auto' }}>Back to history</Button>
      </div>
      {loading ? (
        <div className="vh-empty">Comparing…</div>
      ) : error ? (
        <div className="vh-empty">{error}</div>
      ) : diff.length === 0 ? (
        <div className="vh-empty">No differences between v{versionA} and v{versionB}.</div>
      ) : (
        <div className="vh-diff-list">
          {diff.map((d) => (
            <div key={`${d.kind}-${d.id}-${d.change}`} className={`vh-diff-row ${d.change}`}>
              <div className="vh-diff-row-top">
                <DiffBadge change={d.change} />
                <span className="vh-diff-kind">{d.kind}</span>
                <span className="vh-diff-label">{d.label}</span>
              </div>
              {d.field_changes?.length > 0 && (
                <ul className="vh-diff-fields">
                  {d.field_changes.map((f) => <li key={f}>{f}</li>)}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Main panel ───────────────────────────────────────────────────
export function VersionHistoryPanel({ pipelineId, currentVersion, onClose, onRestored }) {
  const [versions, setVersions] = useState([]);
  const [loading, setLoading]   = useState(true);
  const [compareWith, setCompareWith] = useState(null); // versionNumber being compared vs current
  const [confirmRestore, setConfirmRestore] = useState(null); // versionNumber pending restore
  const [restoring, setRestoring] = useState(false);

  const load = () => {
    setLoading(true);
    pipelineVersionApi.listVersions(pipelineId).then((r) => setVersions(r.items)).finally(() => setLoading(false));
  };

  useEffect(() => { if (pipelineId) load(); /* eslint-disable-next-line */ }, [pipelineId]);

  const handleRestore = async () => {
    setRestoring(true);
    try {
      const updated = await pipelineVersionApi.restore(pipelineId, confirmRestore);
      setConfirmRestore(null);
      onRestored?.(updated);
      load();
    } finally {
      setRestoring(false);
    }
  };

  if (!pipelineId) {
    return (
      <div className="vh-panel">
        <div className="vh-header">
          <div className="vh-header-title"><History size={15} /> Version History</div>
          <button className="vh-close" onClick={onClose}><X size={15} /></button>
        </div>
        <div className="vh-empty">Save this pipeline once to start its version history.</div>
      </div>
    );
  }

  return (
    <div className="vh-panel">
      <div className="vh-header">
        <div className="vh-header-title"><History size={15} /> Version History</div>
        <button className="vh-close" onClick={onClose}><X size={15} /></button>
      </div>

      {compareWith != null ? (
        <CompareView pipelineId={pipelineId} versionA={compareWith} versionB={currentVersion} onClose={() => setCompareWith(null)} />
      ) : loading ? (
        <div className="vh-empty">Loading history…</div>
      ) : versions.length === 0 ? (
        <div className="vh-empty">No versions yet.</div>
      ) : (
        <div className="vh-list">
          {versions.map((v) => (
            <div key={v.version_number} className={`vh-item ${v.is_current ? 'current' : ''}`}>
              <div className="vh-item-dot" />
              <div className="vh-item-body">
                <div className="vh-item-top">
                  <span className="vh-item-version">v{v.version_number}</span>
                  {v.is_current && <span className="vh-current-pill">Current</span>}
                </div>
                <div className="vh-item-date">{fmtDate(v.created_at)}</div>
                <div className="vh-item-author">{v.created_by_name ?? 'Unknown'}</div>
                {v.change_summary && <div className="vh-item-summary">{v.change_summary}</div>}
                <div className="vh-item-actions">
                  <button className="vh-item-action" onClick={() => setCompareWith(v.version_number)} title="View / compare with current">
                    <Eye size={12} /> View
                  </button>
                  {!v.is_current && (
                    <>
                      <button className="vh-item-action" onClick={() => setCompareWith(v.version_number)}>
                        <GitCompare size={12} /> Compare
                      </button>
                      <button className="vh-item-action danger" onClick={() => setConfirmRestore(v.version_number)}>
                        <RotateCcw size={12} /> Restore
                      </button>
                    </>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {confirmRestore != null && (
        <RestoreConfirm
          currentVersion={currentVersion}
          targetVersion={confirmRestore}
          restoring={restoring}
          onCancel={() => setConfirmRestore(null)}
          onConfirm={handleRestore}
        />
      )}
    </div>
  );
}
