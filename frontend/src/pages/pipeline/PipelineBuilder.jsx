/**
 * PipelineBuilder — real pipeline list (GET /api/pipelines), card + table
 * views, and Run/Abort/Edit/Clone/Delete actions.
 *
 * This is the ONLY place a pipeline runs from — Transform Workspace
 * builds/saves pipelines but has no Run button (see
 * pages/transformation/TransformationDesigner.jsx). Editing a pipeline
 * found here opens it in Transform Workspace's real canvas
 * (pages/transformation/TransformationDesigner.jsx via Transformation.jsx)
 * rather than a separate designer — there is only one real pipeline
 * canvas in this app now.
 *
 * Running executes the pipeline for real via
 * backend/app/execution/pipeline_job_executor.py (a bounded, in-memory,
 * pandas-based DAG engine — see that module's docstring for exactly what
 * node types are real vs. disclosed-skipped) and opens a live log view
 * (JobLogDrawer).
 */
import { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, Play, Square, ScrollText, Pencil, Copy, Trash2, LayoutGrid, List, Clock, GitBranch, AlertTriangle, CheckCircle2, Loader2 } from 'lucide-react';
import { Button, StatusBadge, SearchBox } from '@/components/common/index.js';
import { JobLogDrawer } from '@/components/pipeline/JobLogDrawer.jsx';
import { pipelineVersionApi, pipelineJobApi } from '@/services/api/pipelineApi.js';
import './pipeline-builder.css';

const JOB_STATUS_VARIANT = { SUCCEEDED: 'success', FAILED: 'danger', RUNNING: 'info', PENDING: 'gray', CANCELLED: 'gray' };
const RUNNING_STATUSES = new Set(['PENDING', 'RUNNING']);

function fmtJobStatus(job) {
  if (!job) return 'never run';
  return job.status.toLowerCase();
}

// ── Pipeline card ──────────────────────────────────────────────

function PipelineCard({ pipeline, job, onOpenLog, onRun, onAbort, onEdit, onClone, onDelete, busy }) {
  const running = job && RUNNING_STATUSES.has(job.status);
  const statusBarClass = running ? 'running' : job?.status === 'SUCCEEDED' ? 'success' : job?.status === 'FAILED' ? 'failed' : 'idle';

  return (
    <div className="pb-card" onClick={() => onEdit(pipeline)}>
      <div className={`pb-card-status-bar ${statusBarClass}`} />

      <div className="pb-card-header">
        <div>
          <div className="pb-card-name">{pipeline.name}</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 4 }}>
            <span style={{ fontSize: 10, color: 'var(--text-faint)' }}>v{pipeline.current_version}</span>
          </div>
        </div>
        <StatusBadge variant={job ? (JOB_STATUS_VARIANT[job.status] ?? 'gray') : 'gray'} dot size="sm">
          {fmtJobStatus(job)}
        </StatusBadge>
      </div>

      <div className="pb-card-meta">
        <span className="pb-card-meta-item"><GitBranch size={11} />{pipeline.updated_by_name ?? 'system'}</span>
        <span className="pb-card-meta-item"><Clock size={11} />updated {new Date(pipeline.updated_at).toLocaleDateString()}</span>
      </div>

      <div className="pb-card-actions" onClick={(e) => e.stopPropagation()}>
        {running ? (
          <Button variant="danger" size="sm" icon={<Square size={12} fill="currentColor" />} onClick={() => onAbort(job.id)}>Abort</Button>
        ) : (
          <Button variant="primary" size="sm" icon={<Play size={12} fill="currentColor" />} loading={busy} onClick={() => onRun(pipeline)}>Run</Button>
        )}
        {job && <Button variant="secondary" size="sm" icon={<ScrollText size={12} />} onClick={() => onOpenLog(job.id)}>Logs</Button>}
        <Button variant="ghost" size="sm" icon={<Pencil size={12} />} onClick={() => onEdit(pipeline)}>Edit</Button>
        <Button variant="ghost" size="sm" icon={<Copy size={12} />} onClick={() => onClone(pipeline)} title="Clone" />
        <Button variant="ghost" size="sm" icon={<Trash2 size={12} />} style={{ color: 'var(--color-danger)' }} onClick={() => onDelete(pipeline)} title="Delete" />
      </div>
    </div>
  );
}

// ── Table row ────────────────────────────────────────────────

function PipelineTableRow({ pipeline, job, onOpenLog, onRun, onAbort, onEdit, onClone, onDelete, busy }) {
  const running = job && RUNNING_STATUSES.has(job.status);
  return (
    <div className="pb-trow">
      <span className="pb-trow-name" onClick={() => onEdit(pipeline)}>{pipeline.name}</span>
      <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-muted)' }}>v{pipeline.current_version}</span>
      <StatusBadge variant={job ? (JOB_STATUS_VARIANT[job.status] ?? 'gray') : 'gray'} dot size="sm">{fmtJobStatus(job)}</StatusBadge>
      <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-muted)' }}>{pipeline.updated_by_name ?? 'system'}</span>
      <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-muted)' }}>{new Date(pipeline.updated_at).toLocaleString()}</span>
      <div className="pb-trow-actions">
        {running ? (
          <Button variant="danger" size="sm" icon={<Square size={12} fill="currentColor" />} onClick={() => onAbort(job.id)}>Abort</Button>
        ) : (
          <Button variant="primary" size="sm" icon={<Play size={12} fill="currentColor" />} loading={busy} onClick={() => onRun(pipeline)}>Run</Button>
        )}
        {job && <Button variant="secondary" size="sm" icon={<ScrollText size={12} />} onClick={() => onOpenLog(job.id)}>Logs</Button>}
        <Button variant="ghost" size="sm" icon={<Pencil size={12} />} onClick={() => onEdit(pipeline)} />
        <Button variant="ghost" size="sm" icon={<Copy size={12} />} onClick={() => onClone(pipeline)} />
        <Button variant="ghost" size="sm" icon={<Trash2 size={12} />} style={{ color: 'var(--color-danger)' }} onClick={() => onDelete(pipeline)} />
      </div>
    </div>
  );
}

// ── Main page ──────────────────────────────────────────────────

export default function PipelineBuilder() {
  const navigate = useNavigate();

  const [pipelines, setPipelines] = useState([]);
  const [jobsByPipeline, setJobsByPipeline] = useState({}); // pipelineId -> latest job
  const [loading, setLoading]     = useState(true);
  const [search, setSearch]       = useState('');
  const [view, setView]           = useState('card'); // 'card' | 'table'
  const [runningIds, setRunningIds] = useState(new Set()); // pipelines with a Run in flight
  const [openLogJobId, setOpenLogJobId] = useState(null);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [pipelineList, jobList] = await Promise.all([
        pipelineVersionApi.list(),
        pipelineJobApi.list({ page_size: 100 }),
      ]);
      setPipelines(pipelineList.items ?? []);
      const latest = {};
      for (const job of jobList.items ?? []) {
        // jobs come back newest-first — keep only the first (latest) per pipeline
        if (!latest[job.pipeline_id]) latest[job.pipeline_id] = job;
      }
      setJobsByPipeline(latest);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadAll(); }, [loadAll]);

  const handleNew = () => navigate('/app/transform');
  const handleEdit = (pipeline) => navigate(`/app/transform?pipelineId=${pipeline.id}`);

  const handleRun = async (pipeline) => {
    setRunningIds((s) => new Set(s).add(pipeline.id));
    try {
      const job = await pipelineJobApi.run(pipeline.id);
      setJobsByPipeline((prev) => ({ ...prev, [pipeline.id]: job }));
      setOpenLogJobId(job.id);
    } catch (err) {
      alert(err?.response?.data?.detail ?? 'Could not start the pipeline run.');
    } finally {
      setRunningIds((s) => { const n = new Set(s); n.delete(pipeline.id); return n; });
    }
  };

  const handleAbort = async (jobId) => {
    try {
      await pipelineJobApi.cancel(jobId);
    } catch (err) {
      alert(err?.response?.data?.detail ?? 'Could not cancel the run.');
    }
  };

  const handleClone = async (pipeline) => {
    const full = await pipelineVersionApi.get(pipeline.id);
    const created = await pipelineVersionApi.create({ name: `${full.name} (Copy)`, nodes: full.nodes, edges: full.edges, settings: full.settings ?? {} });
    setPipelines((prev) => [...prev, created]);
  };

  const handleDelete = async (pipeline) => {
    if (!confirm(`Delete "${pipeline.name}"? This removes every saved version and cannot be undone.`)) return;
    await pipelineVersionApi.delete(pipeline.id);
    setPipelines((prev) => prev.filter((p) => p.id !== pipeline.id));
  };

  // Live-updates a row's status the moment JobLogDrawer's poll sees it change
  // (running -> succeeded/failed), without waiting for a full reload.
  const handleDrawerStatusChange = useCallback((job) => {
    setJobsByPipeline((prev) => ({ ...prev, [job.pipeline_id]: job }));
  }, []);

  const filtered = pipelines.filter((p) => !search || p.name.toLowerCase().includes(search.toLowerCase()));

  const stats = useMemo(() => {
    const jobs = Object.values(jobsByPipeline);
    return {
      total: pipelines.length,
      succeeded: jobs.filter((j) => j.status === 'SUCCEEDED').length,
      failed: jobs.filter((j) => j.status === 'FAILED').length,
      running: jobs.filter((j) => RUNNING_STATUSES.has(j.status)).length,
    };
  }, [pipelines, jobsByPipeline]);

  return (
    <div>
      {/* Header */}
      <div className="pb-header">
        <div>
          <h1 className="pb-title">Pipeline Builder</h1>
          <p className="pb-subtitle">Run, monitor and manage every pipeline built in Transform Workspace.</p>
        </div>
        <div className="pb-actions">
          <SearchBox value={search} onChange={setSearch} placeholder="Search pipelines…" style={{ width: 240 }} />
          <div className="pb-view-toggle">
            <button className={view === 'card' ? 'active' : ''} onClick={() => setView('card')} title="Card view"><LayoutGrid size={14} /></button>
            <button className={view === 'table' ? 'active' : ''} onClick={() => setView('table')} title="Table view"><List size={14} /></button>
          </div>
          <Button variant="primary" icon={<Plus size={14} />} onClick={handleNew}>New Pipeline</Button>
        </div>
      </div>

      {/* Stats row */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 20, flexWrap: 'wrap' }}>
        {[
          { icon: <GitBranch size={14} />,     val: stats.total,     label: 'Pipelines',        color: 'var(--color-primary)' },
          { icon: <CheckCircle2 size={14} />,  val: stats.succeeded, label: 'Last run passed',  color: 'var(--color-success)' },
          { icon: <AlertTriangle size={14} />, val: stats.failed,    label: 'Last run failed',  color: 'var(--color-danger)' },
          { icon: <Loader2 size={14} />,       val: stats.running,   label: 'Running now',      color: 'var(--color-info-dark,#1D4ED8)' },
        ].map((s) => (
          <div key={s.label} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 12px', background: 'white', border: '1px solid var(--border)', borderRadius: 8, fontSize: 13, color: s.color }}>
            {s.icon}
            <strong>{s.val}</strong>
            <span style={{ color: 'var(--text-muted)' }}>{s.label}</span>
          </div>
        ))}
      </div>

      {loading && (
        <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>Loading pipelines…</div>
      )}

      {!loading && view === 'card' && (
        <div className="pb-cards">
          <div className="pb-new-card" onClick={handleNew}>
            <div className="pb-new-icon">+</div>
            <div className="pb-new-label">Create New Pipeline</div>
            <div className="pb-new-sub">Opens Transform Workspace</div>
          </div>

          {filtered.length === 0 ? (
            <div style={{ gridColumn: '1/-1', padding: 48, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
              <p>{pipelines.length === 0 ? 'No pipelines yet — build one in Transform Workspace and save it.' : 'No pipelines match your search.'}</p>
            </div>
          ) : (
            filtered.map((pl) => (
              <PipelineCard
                key={pl.id}
                pipeline={pl}
                job={jobsByPipeline[pl.id]}
                busy={runningIds.has(pl.id)}
                onOpenLog={setOpenLogJobId}
                onRun={handleRun}
                onAbort={handleAbort}
                onEdit={handleEdit}
                onClone={handleClone}
                onDelete={handleDelete}
              />
            ))
          )}
        </div>
      )}

      {!loading && view === 'table' && (
        <div className="pb-table">
          <div className="pb-trow pb-trow-header">
            {['Name', 'Version', 'Last Status', 'Updated By', 'Updated At', 'Actions'].map((h) => <span key={h}>{h}</span>)}
          </div>
          {filtered.length === 0 ? (
            <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
              {pipelines.length === 0 ? 'No pipelines yet — build one in Transform Workspace and save it.' : 'No pipelines match your search.'}
            </div>
          ) : (
            filtered.map((pl) => (
              <PipelineTableRow
                key={pl.id}
                pipeline={pl}
                job={jobsByPipeline[pl.id]}
                busy={runningIds.has(pl.id)}
                onOpenLog={setOpenLogJobId}
                onRun={handleRun}
                onAbort={handleAbort}
                onEdit={handleEdit}
                onClone={handleClone}
                onDelete={handleDelete}
              />
            ))
          )}
        </div>
      )}

      {openLogJobId && (
        <JobLogDrawer jobId={openLogJobId} onClose={() => setOpenLogJobId(null)} onStatusChange={handleDrawerStatusChange} />
      )}
    </div>
  );
}
