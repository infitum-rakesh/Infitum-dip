/**
 * LicenseManagement — /app/seller/license-management (Seller Portal Phase 5).
 * "Manages the actual license lifecycle" — real Activate/Suspend/Extend/
 * Renew/Upgrade/Downgrade/Revoke on the CustomerModule rows Phase 4 built
 * (see backend/app/services/seller/license_service.py). Expiration alerts
 * (Expired Today / Expires in 7 Days / Expires in 30 Days) are computed
 * from the real license list — no separate fabricated feed.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  Plus, RefreshCcw, MoreVertical, Power, PowerOff, RotateCw,
  ArrowUpCircle, ArrowDownCircle, Ban, Clock, AlertTriangle, KeyRound,
} from 'lucide-react';
import { Button, StatusBadge, DataTable, SearchBox, EmptyState, PermissionGate } from '@/components/common/index.js';
import { PAGINATION, SELLER_PERMISSIONS } from '@/utils/constants.js';
import { formatDate } from '@/utils/formatters.js';
import { licenseApi } from '@/services/api/seller/licenseApi.js';
import { CreateLicenseModal, ExtendLicenseModal, ChangeLicenseTypeModal } from './LicenseModals.jsx';
import './licenses.css';

const STATUS_VARIANT = { ACTIVE: 'success', SUSPENDED: 'danger', EXPIRED: 'gray', REVOKED: 'gray' };
const STATUSES = ['ACTIVE', 'EXPIRED', 'SUSPENDED', 'REVOKED'];

function daysUntil(dateStr) {
  if (!dateStr) return null;
  const ms = new Date(dateStr).getTime() - Date.now();
  return Math.ceil(ms / 86_400_000);
}

export default function LicenseManagement() {
  const [stats, setStats] = useState(null);
  const [allLicenses, setAllLicenses] = useState([]);
  const [statusFilter, setStatusFilter] = useState('All');
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(PAGINATION.DEFAULT_PAGE);
  const [pageSize] = useState(PAGINATION.DEFAULT_PAGE_SIZE);

  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [extendTarget, setExtendTarget] = useState(null);
  const [typeModal, setTypeModal] = useState(null); // { license, mode }

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const loadStats = useCallback(() => { licenseApi.getStats().then(setStats).catch(() => {}); }, []);
  useEffect(() => { loadStats(); }, [loadStats]);

  // Full unpaginated fetch drives the expiration alert buckets (need every
  // license's real expiry_date, not just the current page).
  const loadAllForAlerts = useCallback(() => {
    licenseApi.list({ page_size: 1000 }).then((r) => setAllLicenses(r.items)).catch(() => {});
  }, []);
  useEffect(() => { loadAllForAlerts(); }, [loadAllForAlerts]);

  const loadLicenses = useCallback(() => {
    setLoading(true);
    setError(null);
    licenseApi.list({ status_filter: statusFilter === 'All' ? undefined : statusFilter, page, page_size: pageSize })
      .then((res) => {
        const filtered = search ? res.items.filter((r) => r.organization_name.toLowerCase().includes(search.toLowerCase())) : res.items;
        setRows(filtered);
        setTotal(search ? filtered.length : res.total);
      })
      .catch(() => setError('Failed to load licenses. Please try again.'))
      .finally(() => setLoading(false));
  }, [statusFilter, search, page, pageSize]);

  useEffect(() => { loadLicenses(); }, [loadLicenses]);

  const refreshAll = () => { loadStats(); loadAllForAlerts(); loadLicenses(); };
  const handleClearSearch = () => { setSearchInput(''); setSearch(''); setPage(1); };

  const runActivate = async (row) => { try { await licenseApi.activate(row.id); refreshAll(); } catch (err) { window.alert(err?.message ?? 'Could not activate.'); } };
  const runSuspend = async (row) => { await licenseApi.suspend(row.id); refreshAll(); };
  const runRenew = async (row) => { await licenseApi.renew(row.id); refreshAll(); };
  const runRevoke = async (row) => {
    if (!window.confirm(`Revoke the ${row.module_label} license for "${row.organization_name}"? This is a terminal action.`)) return;
    await licenseApi.revoke(row.id); refreshAll();
  };

  const expiredToday = allLicenses.filter((l) => l.expiry_date && daysUntil(l.expiry_date) === 0 && l.status === 'ACTIVE');
  const expires7 = allLicenses.filter((l) => l.expiry_date && daysUntil(l.expiry_date) > 0 && daysUntil(l.expiry_date) <= 7 && l.status === 'ACTIVE');
  const expires30 = allLicenses.filter((l) => l.expiry_date && daysUntil(l.expiry_date) > 7 && daysUntil(l.expiry_date) <= 30 && l.status === 'ACTIVE');

  const columns = [
    { key: 'id', header: 'ID', render: (v) => `#${v}` },
    { key: 'organization_name', header: 'Customer', sortable: true },
    { key: 'module_label', header: 'Module' },
    { key: 'license_type', header: 'License Type', render: (v) => v.replace(/_/g, ' ') },
    { key: 'start_date', header: 'Start', sortable: true, render: (v) => v ? formatDate(v) : '—' },
    { key: 'expiry_date', header: 'Expiry', sortable: true, render: (v) => v ? formatDate(v) : '—' },
    { key: 'max_users', header: 'Users', render: (v) => v ?? '∞' },
    { key: 'usage_limit', header: 'Usage Limit', render: (v) => v ?? '∞' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div onClick={(e) => e.stopPropagation()}>
          <RowMenu
            row={row}
            onActivate={() => runActivate(row)}
            onSuspend={() => runSuspend(row)}
            onExtend={() => setExtendTarget(row)}
            onRenew={() => runRenew(row)}
            onUpgrade={() => setTypeModal({ license: row, mode: 'upgrade' })}
            onDowngrade={() => setTypeModal({ license: row, mode: 'downgrade' })}
            onRevoke={() => runRevoke(row)}
          />
        </div>
      ),
    },
  ];

  const kpis = [
    { label: 'Total Licenses', value: stats ? stats.total : '—' },
    { label: 'Active', value: stats ? stats.active : '—', variant: 'success' },
    { label: 'Expiring', value: stats ? stats.expiring : '—', variant: 'warning' },
    { label: 'Expired', value: stats ? stats.expired : '—', variant: 'danger' },
    { label: 'Suspended', value: stats ? stats.suspended : '—', variant: 'danger' },
  ];

  return (
    <PermissionGate {...SELLER_PERMISSIONS.SELLER_PORTAL_ACCESS} showReason>
      <div className="lm-page">
        <div className="lm-header">
          <div>
            <h1>License Management</h1>
            <p className="lm-subtitle">Manage the license lifecycle — create, extend, renew, revoke.</p>
          </div>
          <div className="lm-header-actions">
            <Button variant="secondary" icon={<RefreshCcw size={14} />} onClick={refreshAll}>Refresh</Button>
            <Button variant="primary" icon={<Plus size={14} />} onClick={() => setCreateOpen(true)}>New License</Button>
          </div>
        </div>

        <div className="lm-kpis">
          {kpis.map((k) => (
            <div key={k.label} className="lm-kpi-card">
              <div className={`lm-kpi-val ${k.variant ? `lm-kpi-val-${k.variant}` : ''}`}>{k.value}</div>
              <div className="lm-kpi-label">{k.label}</div>
            </div>
          ))}
        </div>

        <div className="lm-alerts">
          <div className="lm-alert-card danger">
            <div className="lm-alert-title"><AlertTriangle size={12} /> Expired Today</div>
            <div className="lm-alert-count">{expiredToday.length}</div>
            <div className="lm-alert-names">{expiredToday.slice(0, 3).map((l) => `${l.organization_name} · ${l.module_label}`).join(', ') || 'None'}</div>
          </div>
          <div className="lm-alert-card warning">
            <div className="lm-alert-title"><Clock size={12} /> Expires in 7 Days</div>
            <div className="lm-alert-count">{expires7.length}</div>
            <div className="lm-alert-names">{expires7.slice(0, 3).map((l) => `${l.organization_name} · ${l.module_label}`).join(', ') || 'None'}</div>
          </div>
          <div className="lm-alert-card info">
            <div className="lm-alert-title"><Clock size={12} /> Expires in 30 Days</div>
            <div className="lm-alert-count">{expires30.length}</div>
            <div className="lm-alert-names">{expires30.slice(0, 3).map((l) => `${l.organization_name} · ${l.module_label}`).join(', ') || 'None'}</div>
          </div>
        </div>

        <div className="lm-search-row">
          <SearchBox value={searchInput} onChange={setSearchInput} placeholder="Search by customer name…" />
          <select className="lm-input lm-inline-filter" value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }}>
            <option value="All">All Statuses</option>
            {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>

        <DataTable
          columns={columns}
          data={rows}
          loading={loading}
          error={error}
          pagination={{ page, pageSize, total, onChange: setPage }}
          emptyState={
            <EmptyState
              icon={<KeyRound size={28} />}
              title={search || statusFilter !== 'All' ? 'No matching licenses' : 'No licenses yet'}
              description={search || statusFilter !== 'All' ? 'Try a different search term or clear your filters.' : 'Create a license for a customer to get started.'}
              action={{ label: 'New License', onClick: () => setCreateOpen(true) }}
            />
          }
        />
      </div>

      <CreateLicenseModal open={createOpen} onClose={() => setCreateOpen(false)} onCreated={refreshAll} />
      <ExtendLicenseModal license={extendTarget} onClose={() => setExtendTarget(null)} onSaved={() => { setExtendTarget(null); refreshAll(); }} />
      {typeModal && (
        <ChangeLicenseTypeModal
          license={typeModal.license} mode={typeModal.mode}
          onClose={() => setTypeModal(null)}
          onSaved={() => { setTypeModal(null); refreshAll(); }}
        />
      )}
    </PermissionGate>
  );
}

// ── Row kebab menu ───────────────────────────────────────────────

function RowMenu({ row, onActivate, onSuspend, onExtend, onRenew, onUpgrade, onDowngrade, onRevoke }) {
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState(null);
  const btnRef = useRef(null);

  const toggle = () => {
    if (!open && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setCoords({ top: r.bottom + 4, left: r.right - 190 });
    }
    setOpen((o) => !o);
  };

  const act = (fn) => () => { setOpen(false); fn(); };
  const suspended = row.status === 'SUSPENDED';
  const revoked = row.status === 'REVOKED';

  return (
    <div className="lm-row-menu">
      <button ref={btnRef} className="lm-icon-btn" title="More actions" onClick={toggle}><MoreVertical size={14} /></button>
      {open && coords && createPortal(
        <>
          <div className="lm-row-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="lm-row-menu-list" style={{ position: 'fixed', top: coords.top, left: coords.left }}>
            {suspended || revoked
              ? <button onClick={act(onActivate)}><Power size={13} /> Activate</button>
              : <button className="lm-row-menu-warn" onClick={act(onSuspend)}><PowerOff size={13} /> Suspend</button>}
            <button onClick={act(onExtend)}><Clock size={13} /> Extend</button>
            <button onClick={act(onRenew)}><RotateCw size={13} /> Renew</button>
            <button onClick={act(onUpgrade)}><ArrowUpCircle size={13} /> Upgrade</button>
            <button onClick={act(onDowngrade)}><ArrowDownCircle size={13} /> Downgrade</button>
            {!revoked && <button className="lm-row-menu-danger" onClick={act(onRevoke)}><Ban size={13} /> Revoke</button>}
          </div>
        </>,
        document.body
      )}
    </div>
  );
}
