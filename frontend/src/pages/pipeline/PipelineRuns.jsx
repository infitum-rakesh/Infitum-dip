/**
 * PipelineRuns — global execution history across all pipelines.
 * Route: /app/pipeline-runs
 *
 * Real data — GET /api/pipeline-jobs (backend/app/api/pipeline_jobs.py),
 * the same PipelineJob rows Pipeline Builder's Run/Abort actions create
 * via backend/app/execution/pipeline_job_executor.py. Clicking a row
 * opens the same live JobLogDrawer used there.
 */
import { useState, useEffect, useCallback } from 'react';
import { RotateCw } from 'lucide-react';
import { Button, StatusBadge, SearchBox } from '@/components/common/index.js';
import { JobLogDrawer } from '@/components/pipeline/JobLogDrawer.jsx';
import { pipelineJobApi } from '@/services/api/pipelineApi.js';
import '../pipeline/pipeline-builder.css';

const STATUS_VARIANT = { SUCCEEDED: 'success', FAILED: 'danger', RUNNING: 'info', PENDING: 'gray', CANCELLED: 'gray' };

function durationSeconds(job) {
  if (!job.started_at || !job.completed_at) return null;
  return Math.round((new Date(job.completed_at) - new Date(job.started_at)) / 1000);
}

export default function PipelineRuns() {
  const [jobs, setJobs]           = useState([]);
  const [loading, setLoading]     = useState(true);
  const [search, setSearch]       = useState('');
  const [statusFilter, setStatus] = useState('all');
  const [openJobId, setOpenJobId] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    pipelineJobApi.list({ page_size: 100 }).then((r) => { setJobs(r.items ?? []); setLoading(false); });
  }, []);

  useEffect(() => { load(); }, [load]);

  const filtered = jobs.filter((j) => {
    if (statusFilter !== 'all' && j.status !== statusFilter) return false;
    if (search && !j.pipeline_name.toLowerCase().includes(search.toLowerCase()) && String(j.id) !== search) return false;
    return true;
  });

  const total     = jobs.length;
  const succeeded = jobs.filter((j) => j.status === 'SUCCEEDED').length;
  const failed    = jobs.filter((j) => j.status === 'FAILED').length;
  const running   = jobs.filter((j) => j.status === 'RUNNING' || j.status === 'PENDING').length;
  const totalRecords = jobs.reduce((s, j) => s + (j.records_out ?? 0), 0);

  return (
    <div>
      {/* Page header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 20, gap: 16, flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ fontSize: 'var(--fs-3xl)', fontWeight: 700, color: 'var(--text-primary)', lineHeight: 1.2 }}>Pipeline Runs</h1>
          <p style={{ fontSize: 'var(--fs-md)', color: 'var(--text-muted)', marginTop: 3 }}>Real execution history across every pipeline (backend/app/execution/pipeline_job_executor.py).</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="secondary" size="sm" icon={<RotateCw size={13} />} onClick={load}>Refresh</Button>
        </div>
      </div>

      {/* Summary metrics */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 12, marginBottom: 20 }}>
        {[
          { val: total,                         label: 'Total Runs',    sub: '' },
          { val: succeeded,                      label: 'Succeeded',     sub: '', color: 'var(--color-success-dark)', bg: 'var(--color-success-bg)' },
          { val: failed,                         label: 'Failed',        sub: '', color: 'var(--color-danger-dark)',  bg: 'var(--color-danger-bg)' },
          { val: running,                        label: 'Running Now',   sub: '', color: 'var(--color-info-dark,#1D4ED8)', bg: 'var(--color-info-bg)' },
          { val: totalRecords.toLocaleString(),  label: 'Records Processed', sub: 'all time' },
        ].map((m) => (
          <div key={m.label} style={{ background: m.bg ?? 'white', border: '1px solid var(--border)', borderRadius: 12, padding: '16px', textAlign: 'center', boxShadow: 'var(--shadow-xs)' }}>
            <div style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.5px', lineHeight: 1, color: m.color ?? 'var(--text-primary)' }}>{m.val}</div>
            <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.4px', color: m.color ?? 'var(--text-muted)', marginTop: 4 }}>{m.label}</div>
            {m.sub && <div style={{ fontSize: 10, color: 'var(--text-faint)', marginTop: 2 }}>{m.sub}</div>}
          </div>
        ))}
      </div>

      {/* Filters */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap', alignItems: 'center' }}>
        <SearchBox value={search} onChange={setSearch} placeholder="Search by pipeline or job ID…" style={{ maxWidth: 280 }} />
        <div style={{ display: 'flex', gap: 4, border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden', background: 'white' }}>
          {['all', 'SUCCEEDED', 'FAILED', 'RUNNING', 'CANCELLED'].map((s) => (
            <button key={s} onClick={() => setStatus(s)} style={{ padding: '5px 12px', fontSize: 11, fontWeight: 600, textTransform: 'capitalize', background: statusFilter === s ? 'var(--color-primary)' : 'white', color: statusFilter === s ? 'white' : 'var(--text-muted)', border: 'none', cursor: 'pointer', transition: 'all 0.15s' }}>
              {s.toLowerCase()}
            </button>
          ))}
        </div>
        <span style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-muted)', marginLeft: 'auto' }}>{filtered.length} runs</span>
      </div>

      {/* Runs table */}
      <div style={{ background: 'white', border: '1px solid var(--border)', borderRadius: 12, overflow: 'hidden' }}>
        <div className="pb-run-row" style={{ gridTemplateColumns: '90px 1fr 90px 90px 130px 80px 90px 100px', background: '#F8FAFC', borderBottom: '1px solid var(--border)', cursor: 'default' }}>
          {['Job ID', 'Pipeline', 'Status', 'Trigger', 'Started', 'Duration', 'Records In', 'Records Out'].map((h) => (
            <span key={h} style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>{h}</span>
          ))}
        </div>

        {loading ? (
          <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>Loading runs…</div>
        ) : filtered.length === 0 ? (
          <div style={{ padding: 48, textAlign: 'center' }}>
            <div style={{ fontSize: 32, marginBottom: 10 }}>📋</div>
            <p style={{ fontWeight: 600, fontSize: 14, color: 'var(--text-primary)', marginBottom: 4 }}>No runs yet</p>
            <p style={{ fontSize: 13, color: 'var(--text-muted)' }}>Run a pipeline from Pipeline Builder to see it here.</p>
          </div>
        ) : (
          filtered.map((job) => {
            const dur = durationSeconds(job);
            return (
              <div key={job.id} className="pb-run-row" style={{ gridTemplateColumns: '90px 1fr 90px 90px 130px 80px 90px 100px' }} onClick={() => setOpenJobId(job.id)}>
                <span className="pb-run-id">#{job.id}</span>
                <span style={{ fontSize: 'var(--fs-sm)', fontWeight: 500 }}>{job.pipeline_name}</span>
                <StatusBadge variant={STATUS_VARIANT[job.status] ?? 'gray'} dot size="sm">{job.status.toLowerCase()}</StatusBadge>
                <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-muted)' }}>{job.trigger}</span>
                <div>
                  <div style={{ fontSize: 'var(--fs-xs)', fontWeight: 500 }}>{job.started_at ? new Date(job.started_at).toLocaleDateString() : '—'}</div>
                  <div className="pb-run-time">{job.started_at ? new Date(job.started_at).toLocaleTimeString() : ''}</div>
                </div>
                <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-muted)' }}>{dur != null ? `${dur}s` : job.status === 'RUNNING' ? '…' : '—'}</span>
                <span style={{ fontSize: 'var(--fs-xs)' }}>{job.records_in?.toLocaleString() ?? '—'}</span>
                <span style={{ fontSize: 'var(--fs-xs)', fontWeight: 600, color: job.status === 'FAILED' ? 'var(--color-danger-dark)' : 'var(--color-success-dark)' }}>
                  {job.records_out?.toLocaleString() ?? '—'}
                </span>
              </div>
            );
          })
        )}
      </div>

      {openJobId && (
        <JobLogDrawer jobId={openJobId} onClose={() => setOpenJobId(null)} />
      )}
    </div>
  );
}
