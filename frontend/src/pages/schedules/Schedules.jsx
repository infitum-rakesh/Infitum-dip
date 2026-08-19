/**
 * Schedules — /app/schedules
 * Automates execution of existing pipelines. Reuses MainLayout/Header/
 * Sidebar/Modal/DataTable/StatusBadge/Button/apiClient/auth/permissions —
 * no duplicate components. Never runs pipeline logic itself; see
 * backend schedule_service.py.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  Plus, RefreshCcw, X, PlayCircle, Pause, Play, Pencil, MoreVertical, Copy, Trash2, History, Table2, CalendarDays,
} from 'lucide-react';
import { Button, StatusBadge, DataTable, SearchBox, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAGINATION, SCHEDULE_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { scheduleApi } from '@/services/api/scheduleApi.js';
import { relativeRunLabel } from './scheduleFormatters.js';
import { ScheduleFormModal } from './ScheduleFormModal.jsx';
import { ScheduleHistoryModal } from './ScheduleHistoryModal.jsx';
import { ScheduleCalendar } from './ScheduleCalendar.jsx';
import './schedules.css';

const STATUS_VARIANT = { ACTIVE: 'success', PAUSED: 'gray', RUNNING: 'info', FAILED: 'danger', COMPLETED: 'success', DISABLED: 'gray' };
const EMPTY_FILTERS = { status: 'All', environment: 'All', frequency: 'All' };

export default function Schedules() {
  const [stats, setStats] = useState(null);
  const [view, setView] = useState('table'); // 'table' | 'calendar'

  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');

  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [formTarget, setFormTarget] = useState(null); // { schedule: null|obj } | null
  const [historyTarget, setHistoryTarget] = useState(null); // schedule | null
  const [busyId, setBusyId] = useState(null);

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadStats = useCallback(() => { scheduleApi.getStats().then(setStats).catch(() => {}); }, []);
  useEffect(() => { loadStats(); }, [loadStats]);

  const loadSchedules = useCallback(() => {
    setLoading(true);
    setError(null);
    scheduleApi
      .list({ q: search, status: filters.status, environment: filters.environment, frequency: filters.frequency, page, page_size: pageSize })
      .then((res) => { setRows(res.items); setTotal(res.total); })
      .catch(() => setError('Failed to load schedules. Please try again.'))
      .finally(() => setLoading(false));
  }, [search, filters, page, pageSize]);

  useEffect(() => { loadSchedules(); }, [loadSchedules]);

  const refreshAll = () => { loadStats(); loadSchedules(); };
  const handleClearSearch = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const runNow = async (row) => {
    setBusyId(row.id);
    try { await scheduleApi.runNow(row.id); refreshAll(); } finally { setBusyId(null); }
  };
  const togglePause = async (row) => {
    setBusyId(row.id);
    try {
      if (row.status === 'PAUSED') await scheduleApi.resume(row.id);
      else await scheduleApi.pause(row.id);
      refreshAll();
    } finally {
      setBusyId(null);
    }
  };
  const runClone = async (row) => { await scheduleApi.clone(row.id); refreshAll(); };
  const runDelete = async (row) => {
    if (!window.confirm(`Delete schedule "${row.name}"? This cannot be undone.`)) return;
    await scheduleApi.remove(row.id);
    refreshAll();
  };

  const columns = [
    { key: 'name', header: 'Schedule Name', sortable: true, render: (v) => <span className="sched-name-text">{v}</span> },
    { key: 'pipeline_name', header: 'Pipeline' },
    { key: 'frequency_display', header: 'Frequency' },
    { key: 'next_run_at', header: 'Next Run', sortable: true, render: (v, row) => relativeRunLabel(v, row.timezone) },
    { key: 'last_run_at', header: 'Last Run', render: (v, row) => v ? `${relativeRunLabel(v, row.timezone)} — ${row.last_run_status ?? ''}` : 'Never' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'owner_name', header: 'Owner', render: (v) => v ?? '—' },
    { key: 'environment', header: 'Environment', render: (v) => v?.charAt(0) + v?.slice(1).toLowerCase() },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="sched-row-actions" onClick={(e) => e.stopPropagation()}>
          <PermissionGate {...SCHEDULE_PERMISSIONS.SCHEDULE_RUN} fallback={null}>
            <button className="sched-icon-btn" title="Run Now" disabled={busyId === row.id} onClick={() => runNow(row)}><PlayCircle size={14} /></button>
          </PermissionGate>
          <PermissionGate {...SCHEDULE_PERMISSIONS.SCHEDULE_PAUSE} fallback={null}>
            <button className="sched-icon-btn" title={row.status === 'PAUSED' ? 'Resume' : 'Pause'} disabled={busyId === row.id} onClick={() => togglePause(row)}>
              {row.status === 'PAUSED' ? <Play size={14} /> : <Pause size={14} />}
            </button>
          </PermissionGate>
          <button className="sched-icon-btn" title="Execution History" onClick={() => setHistoryTarget(row)}><History size={14} /></button>
          <PermissionGate {...SCHEDULE_PERMISSIONS.SCHEDULE_EDIT} fallback={null}>
            <button className="sched-icon-btn" title="Edit" onClick={() => setFormTarget({ schedule: row })}><Pencil size={14} /></button>
          </PermissionGate>
          <RowMenu onClone={() => runClone(row)} onDelete={() => runDelete(row)} />
        </div>
      ),
    },
  ];

  const kpis = [
    { label: 'Active Schedules', value: stats ? stats.active : '—', variant: 'success' },
    { label: 'Paused', value: stats ? stats.paused : '—' },
    { label: 'Running Today', value: stats ? stats.running_today : '—', variant: 'info' },
    { label: 'Failed Today', value: stats ? stats.failed_today : '—', variant: 'danger' },
    {
      label: 'Next Run',
      value: stats?.next_run_at ? relativeRunLabel(stats.next_run_at) : 'None scheduled',
      sub: stats?.next_run_schedule_name,
    },
  ];

  const hasActiveFilters = search || filters.status !== 'All' || filters.environment !== 'All' || filters.frequency !== 'All';

  return (
    <PermissionGate {...SCHEDULE_PERMISSIONS.SCHEDULE_VIEW} showReason>
      <div className="sched-page">
        {/* Header */}
        <div className="sched-header">
          <div>
            <h1 className="sched-title">Schedules</h1>
            <p className="sched-subtitle">Automate pipelines and data processing jobs.</p>
          </div>
          <div className="sched-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={refreshAll}>Refresh</Button>
            <PermissionGate {...SCHEDULE_PERMISSIONS.SCHEDULE_CREATE} fallback={null}>
              <Button variant="primary" icon={<Plus size={14} />} onClick={() => setFormTarget({ schedule: null })}>Create Schedule</Button>
            </PermissionGate>
          </div>
        </div>

        {/* KPIs */}
        <div className="sched-kpis">
          {kpis.map((k) => (
            <div key={k.label} className="sched-kpi-card">
              <div className={`sched-kpi-val ${k.variant ? `sched-kpi-val-${k.variant}` : ''}`} title={k.sub ? `Schedule: ${k.sub}` : undefined}>{k.value}</div>
              <div className="sched-kpi-label">{k.label}</div>
            </div>
          ))}
        </div>

        {/* View toggle */}
        <div className="sched-view-toggle">
          <button className={`sched-pill ${view === 'table' ? 'active' : ''}`} onClick={() => setView('table')}><Table2 size={13} /> Table</button>
          <button className={`sched-pill ${view === 'calendar' ? 'active' : ''}`} onClick={() => setView('calendar')}><CalendarDays size={13} /> Calendar</button>
        </div>

        {view === 'table' ? (
          <>
            {/* Search + inline filters */}
            <div className="sched-search-row">
              <SearchBox value={searchInput} onChange={setSearchInput} placeholder="Search by name, pipeline, status, owner…" className="sched-search-box" />
              <select className="sched-input sched-inline-filter" value={filters.status} onChange={(e) => { setFilters((f) => ({ ...f, status: e.target.value })); setPage(1); }}>
                <option value="All">All Statuses</option>
                <option value="ACTIVE">Active</option>
                <option value="PAUSED">Paused</option>
                <option value="RUNNING">Running</option>
                <option value="FAILED">Failed</option>
                <option value="COMPLETED">Completed</option>
                <option value="DISABLED">Disabled</option>
              </select>
              <select className="sched-input sched-inline-filter" value={filters.environment} onChange={(e) => { setFilters((f) => ({ ...f, environment: e.target.value })); setPage(1); }}>
                <option value="All">All Environments</option>
                <option value="PRODUCTION">Production</option>
                <option value="STAGING">Staging</option>
                <option value="DEVELOPMENT">Development</option>
                <option value="TEST">Test</option>
              </select>
              <select className="sched-input sched-inline-filter" value={filters.frequency} onChange={(e) => { setFilters((f) => ({ ...f, frequency: e.target.value })); setPage(1); }}>
                <option value="All">All Frequencies</option>
                <option value="ONCE">Once</option>
                <option value="HOURLY">Hourly</option>
                <option value="DAILY">Daily</option>
                <option value="WEEKLY">Weekly</option>
                <option value="MONTHLY">Monthly</option>
                <option value="CUSTOM_CRON">Custom Cron</option>
              </select>
              {searchInput && <button className="sched-clear-search-btn" onClick={handleClearSearch}><X size={13} /> Clear</button>}
            </div>

            <div className="sched-table-panel">
              <DataTable
                columns={columns}
                data={rows}
                loading={loading}
                error={error}
                onRowClick={(row) => setHistoryTarget(row)}
                pagination={{ page, pageSize, total, onChange: setPage }}
                emptyState={
                  <EmptyState
                    icon={<Plus size={28} />}
                    title={hasActiveFilters ? 'No matching schedules' : 'No schedules yet'}
                    description={hasActiveFilters ? 'Try a different search term or clear your filters.' : 'Create a schedule to automate a pipeline.'}
                    action={hasActiveFilters ? { label: 'Clear filters', onClick: () => { handleClearSearch(); setFilters(EMPTY_FILTERS); } } : { label: 'Create Schedule', onClick: () => setFormTarget({ schedule: null }) }}
                  />
                }
              />
            </div>
          </>
        ) : (
          <ScheduleCalendar onSelectSchedule={(s) => setHistoryTarget(s)} />
        )}
      </div>

      {formTarget && (
        <ScheduleFormModal open={!!formTarget} onClose={() => setFormTarget(null)} schedule={formTarget.schedule} onSaved={refreshAll} />
      )}
      <ScheduleHistoryModal open={!!historyTarget} onClose={() => setHistoryTarget(null)} schedule={historyTarget} />
    </PermissionGate>
  );
}

// ── Row kebab menu ───────────────────────────────────────────────

function RowMenu({ onClone, onDelete }) {
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState(null);
  const btnRef = useRef(null);

  const toggle = () => {
    if (!open && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setCoords({ top: r.bottom + 4, left: r.right - 170 });
    }
    setOpen((o) => !o);
  };

  return (
    <div className="sched-row-menu">
      <button ref={btnRef} className="sched-icon-btn" title="More actions" onClick={toggle}><MoreVertical size={14} /></button>
      {open && coords && createPortal(
        <>
          <div className="sched-row-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="sched-row-menu-list" style={{ position: 'fixed', top: coords.top, left: coords.left }}>
            <PermissionGate {...SCHEDULE_PERMISSIONS.SCHEDULE_CREATE} fallback={null}>
              <button onClick={() => { setOpen(false); onClone(); }}><Copy size={13} /> Clone</button>
            </PermissionGate>
            <PermissionGate {...SCHEDULE_PERMISSIONS.SCHEDULE_DELETE} fallback={null}>
              <button className="sched-row-menu-danger" onClick={() => { setOpen(false); onDelete(); }}><Trash2 size={13} /> Delete</button>
            </PermissionGate>
          </div>
        </>,
        document.body
      )}
    </div>
  );
}
