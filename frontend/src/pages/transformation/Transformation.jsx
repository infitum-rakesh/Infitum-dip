/**
 * Transformation — page entry point.
 * Tabs (Designer / History), then mounts TransformationDesigner for the
 * active pipeline. No quick-start templates — this used to seed a new
 * pipeline with hardcoded fake nodes/dataset names (INVOICE_GOLD,
 * CUSTOMER_CLEAN, ...) that didn't correspond to anything real; removed
 * rather than left as decoration. Every pipeline here starts blank and
 * is built from real connections/uploaded files.
 *
 * "Open Pipeline" lets you load any pipeline already saved to the
 * backend (via /api/pipelines — the same real store Pipeline Builder
 * lists from) back into the designer to keep editing it. Running a
 * pipeline always happens from Pipeline Builder, not here.
 *
 * Can also arrive with a pipeline already prepared via router state —
 * Document Intelligence's "Continue to Transformation" (StructuredDataset.jsx)
 * uploads the AI-extracted record as a real file and hands it here as
 * `location.state.initialPipeline` (a real Source node pointing at that
 * upload + an Output Dataset node) instead of the old behaviour of just
 * navigating here and silently dropping the extracted data.
 */
import { useState, useEffect, useCallback } from 'react';
import { useSearchParams, useLocation, useNavigate } from 'react-router-dom';
import { Plus, FolderOpen, X, Search } from 'lucide-react';
import { Button } from '@/components/common/Button.jsx';
import { TransformationDesigner } from './TransformationDesigner.jsx';
import { pipelineVersionApi } from '@/services/api/pipelineApi.js';
import './transformation.css';

// ── Open Pipeline picker ─────────────────────────────────────────

function PipelinePickerModal({ pipelines, loading, onPick, onClose }) {
  const [search, setSearch] = useState('');
  const filtered = pipelines.filter((p) => !search || p.name.toLowerCase().includes(search.toLowerCase()));

  return (
    <div className="txp-modal-overlay" onClick={onClose}>
      <div className="txp-modal" onClick={(e) => e.stopPropagation()}>
        <div className="txp-modal-header">
          <span>Open Pipeline</span>
          <button className="txp-modal-close" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="txp-modal-search">
          <Search size={14} />
          <input autoFocus placeholder="Search pipelines…" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <div className="txp-modal-body">
          {loading ? (
            <div className="txp-modal-empty">Loading pipelines…</div>
          ) : filtered.length === 0 ? (
            <div className="txp-modal-empty">
              {pipelines.length === 0 ? 'No saved pipelines yet — build one and click Save.' : 'No pipelines match your search.'}
            </div>
          ) : (
            filtered.map((p) => (
              <button key={p.id} className="txp-modal-item" onClick={() => onPick(p)}>
                <div>
                  <div className="txp-modal-item-name">{p.name}</div>
                  <div className="txp-modal-item-meta">
                    v{p.current_version} · updated {new Date(p.updated_at).toLocaleString()}
                    {p.updated_by_name ? ` by ${p.updated_by_name}` : ''}
                  </div>
                </div>
                <span className="txp-modal-item-open">Open →</span>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

// ── Page ───────────────────────────────────────────────────────

export default function Transformation() {
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [activePipeline, setActivePipeline] = useState(() => location.state?.initialPipeline ?? null);
  const [activeTab, setActiveTab]           = useState('designer');
  const [loadingDeepLink, setLoadingDeepLink] = useState(!!searchParams.get('pipelineId'));

  // Consume the router-state pipeline exactly once — replace it out of
  // history so a later refresh of this same URL doesn't re-apply it and
  // clobber whatever the user has since edited on the canvas.
  useEffect(() => {
    if (location.state?.initialPipeline) {
      navigate(location.pathname, { replace: true, state: null });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [pipelines, setPipelines]     = useState([]);
  const [listLoading, setListLoading] = useState(true);
  const [showPicker, setShowPicker]   = useState(false);
  const [openError, setOpenError]     = useState('');

  const refreshList = useCallback(() => {
    setListLoading(true);
    pipelineVersionApi.list()
      .then((r) => setPipelines(r.items ?? []))
      .catch(() => {})
      .finally(() => setListLoading(false));
  }, []);

  useEffect(() => { refreshList(); }, [refreshList]);

  // Deep link from Pipeline Builder's "Edit" action — /app/transform?pipelineId=123
  useEffect(() => {
    const id = searchParams.get('pipelineId');
    if (!id) return;
    setOpenError('');
    pipelineVersionApi.get(id)
      .then((full) => setActivePipeline(full))
      .catch(() => setOpenError('Could not load that pipeline. It may have been deleted.'))
      .finally(() => setLoadingDeepLink(false));
  }, [searchParams]);

  const newPipeline = () => {
    setSearchParams({}, { replace: true });
    setActivePipeline({ name: 'Untitled Pipeline', nodes: [], edges: [] });
  };

  const handlePick = async (meta) => {
    setOpenError('');
    try {
      const full = await pipelineVersionApi.get(meta.id);
      setSearchParams({}, { replace: true });
      setActivePipeline(full);
      setShowPicker(false);
    } catch {
      setOpenError('Could not load that pipeline. It may have been deleted.');
    }
  };

  // Fires after every successful Save inside the designer — this is what
  // makes a just-saved pipeline actually show up in the picker/Pipeline
  // Builder without needing a manual page refresh.
  const handleDesignerSaved = () => refreshList();

  return (
    <div className="txp-page">
      <div className="txp-header">
        <div>
          <h1 className="txp-title">Transformation Workspace</h1>
          <p className="txp-subtitle">Build visual data pipelines with drag-and-drop transformation nodes.</p>
        </div>
        <div className="txp-actions">
          <Button variant="secondary" icon={<FolderOpen size={14} />} onClick={() => { setOpenError(''); setShowPicker(true); }}>
            Open Pipeline
          </Button>
          <Button variant="primary" icon={<Plus size={14} />} onClick={newPipeline}>
            New Pipeline
          </Button>
        </div>
      </div>

      {openError && <div className="txp-open-error">{openError}</div>}

      {/* Tab navigation */}
      <div className="txp-tab-nav">
        {[['designer', 'Pipeline Designer'], ['history', 'Run History']].map(([id, label]) => (
          <button key={id} className={`txp-tab-btn ${activeTab === id ? 'active' : ''}`} onClick={() => setActiveTab(id)}>
            {label}
          </button>
        ))}
      </div>

      {/* Designer tab */}
      {activeTab === 'designer' && (
        loadingDeepLink ? (
          <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>Loading pipeline…</div>
        ) : (
          <TransformationDesigner
            key={activePipeline?.id ?? activePipeline?.name ?? 'empty'}
            initialPipeline={activePipeline}
            onSaved={handleDesignerSaved}
          />
        )
      )}

      {/* History tab — placeholder */}
      {activeTab === 'history' && (
        <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 'var(--radius-xl)', padding: 40, textAlign: 'center', color: 'var(--text-muted)', fontSize: 14, marginTop: 0, borderTop: 'none', borderTopLeftRadius: 0, borderTopRightRadius: 0 }}>
          <div style={{ fontSize: 36, marginBottom: 12 }}>📋</div>
          <p>Pipeline run history now lives in Pipeline Builder — open a pipeline there to see its runs and logs.</p>
        </div>
      )}

      {showPicker && (
        <PipelinePickerModal
          pipelines={pipelines}
          loading={listLoading}
          onPick={handlePick}
          onClose={() => setShowPicker(false)}
        />
      )}
    </div>
  );
}
