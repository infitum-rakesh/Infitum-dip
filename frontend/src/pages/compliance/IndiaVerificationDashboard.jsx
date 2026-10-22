/**
 * IndiaVerificationDashboard — /app/india-verification (IV5, Phase 1).
 *
 * KPIs + recent cases, computed client-side from the real case list
 * (Phase 1 scale doesn't warrant a dedicated backend stats endpoint yet —
 * same "derive at read time" precedent as other module dashboards until
 * usage justifies pre-aggregation).
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Shield, Fingerprint, ExternalLink, TrendingUp } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import indiaVerificationApi from '@/services/api/indiaVerificationApi.js';
import { monitoringApi } from '@/services/api/monitoringApi.js';
import './compliance.css';

const STATUS_VARIANT = {
  VERIFIED: 'success', VERIFIED_WITH_WARNINGS: 'warning', PARTIALLY_VERIFIED: 'warning',
  MISMATCH: 'danger', REQUIRES_REVIEW: 'warning', PENDING_PROVIDER: 'info', IN_PROGRESS: 'info',
  NOT_STARTED: 'gray', NOT_CONFIGURED: 'gray', UNAVAILABLE: 'danger', FAILED: 'danger',
};

export default function IndiaVerificationDashboard() {
  const navigate = useNavigate();
  const [cases, setCases] = useState([]);
  const [loading, setLoading] = useState(true);
  const [providerCount, setProviderCount] = useState(null);
  const [overview, setOverview] = useState(null);

  useEffect(() => {
    indiaVerificationApi.listVerifications({ page_size: 100 })
      .then((r) => { setCases(r.items ?? []); setLoading(false); })
      .catch(() => setLoading(false));
    indiaVerificationApi.listProviders()
      .then((r) => setProviderCount((r.items ?? []).length))
      .catch(() => setProviderCount(0));
    monitoringApi.getIndiaVerificationOverview(30).then(setOverview).catch(() => setOverview(null));
  }, []);

  const total = cases.length;
  const verified = cases.filter((c) => c.overall_status === 'VERIFIED').length;
  const needsAttention = cases.filter((c) => ['MISMATCH', 'REQUIRES_REVIEW', 'FAILED'].includes(c.overall_status)).length;
  const unavailable = cases.filter((c) => ['UNAVAILABLE', 'NOT_CONFIGURED', 'PENDING_PROVIDER'].includes(c.overall_status)).length;

  const columns = [
    { key: 'subject_name', header: 'Subject' },
    { key: 'subject_type', header: 'Type', render: (v) => <StatusBadge variant="gray">{v}</StatusBadge> },
    {
      key: 'check_types', header: 'Checks',
      render: (_v, row) => (
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          {(row.results ?? []).map((r) => <StatusBadge key={r.id} variant="gray">{r.check_type}</StatusBadge>)}
        </div>
      ),
    },
    { key: 'overall_status', header: 'Status', render: (v) => <StatusBadge variant={STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'created_at', header: 'Created', render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: '',
      render: (_v, row) => <Button size="sm" variant="secondary" icon={<ExternalLink size={12} />} onClick={() => navigate(`/app/india-verification/cases/${row.id}`)}>View</Button>,
    },
  ];

  return (
    <div className="iv-page">
      <div className="iv-header">
        <div>
          <h1 className="iv-title"><Shield size={20} /> Compliance &amp; Verification</h1>
          <p className="iv-subtitle">
            India business/tax identity and corporate document verification — real, provider-backed checks with an
            honest status model. Format validation, provider confirmation, and identity matching are always kept
            separate; nothing here is ever reported as verified on format alone.
          </p>
        </div>
        <Button icon={<Fingerprint size={14} />} onClick={() => navigate('/app/india-verification/identity')}>Run a Verification</Button>
      </div>

      <div className="iv-kpi-grid">
        <div className="iv-kpi-card"><span className="iv-kpi-label">Total Cases</span><span className="iv-kpi-value">{total}</span></div>
        <div className="iv-kpi-card"><span className="iv-kpi-label">Verified</span><span className="iv-kpi-value success">{verified}</span></div>
        <div className="iv-kpi-card"><span className="iv-kpi-label">Needs Attention</span><span className={`iv-kpi-value ${needsAttention ? 'danger' : ''}`}>{needsAttention}</span></div>
        <div className="iv-kpi-card"><span className="iv-kpi-label">Pending / Unconfigured</span><span className={`iv-kpi-value ${unavailable ? 'warning' : ''}`}>{unavailable}</span></div>
        <div className="iv-kpi-card"><span className="iv-kpi-label">Provider Connections</span><span className="iv-kpi-value">{providerCount ?? '—'}</span></div>
      </div>

      {overview && (
        <>
          <div className="iv-section-title"><TrendingUp size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />Provider Health (last 30 days)</div>
          <div className="iv-result-card" style={{ marginBottom: 20 }}>
            <div className="iv-result-meta">
              <span>Requests: <strong>{overview.requests_total}</strong></span>
              <span>Success Rate: <strong>{overview.success_rate_pct ?? '—'}%</strong></span>
              <span>Mismatch Rate: <strong>{overview.mismatch_rate_pct ?? '—'}%</strong></span>
              <span>Unavailable Rate: <strong>{overview.unavailable_rate_pct ?? '—'}%</strong></span>
              <span>Retries: <strong>{overview.retry_count_total}</strong></span>
              <span>Stale Results: <strong>{overview.stale_results_count}</strong></span>
              <span>Needs Attention: <strong>{overview.needs_attention_count}</strong></span>
              <span>Active Rules: <strong>{overview.active_rules_count}</strong></span>
            </div>
            {Object.keys(overview.requests_by_check_type).length > 0 && (
              <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginTop: 4 }}>
                <div>
                  <div className="iv-moa-subhead">Requests by Check Type</div>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {Object.entries(overview.requests_by_check_type).map(([k, v]) => (
                      <StatusBadge key={k} variant="gray">{k}: {v}</StatusBadge>
                    ))}
                  </div>
                </div>
                <div>
                  <div className="iv-moa-subhead">Requests by Provider</div>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {Object.entries(overview.requests_by_provider).map(([k, v]) => (
                      <StatusBadge key={k} variant="gray">{k}: {v}</StatusBadge>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>
        </>
      )}

      <div className="iv-section-title">Recent Cases</div>
      <div className="iv-list-table-panel" style={{ marginTop: 8 }}>
        <DataTable
          columns={columns}
          data={cases.slice(0, 25)}
          loading={loading}
          emptyState={
            <EmptyState
              icon={<Shield size={28} />}
              title="No verification cases yet"
              description="Run your first PAN verification to see it here."
              action={{ label: 'Run a Verification', onClick: () => navigate('/app/india-verification/identity') }}
            />
          }
        />
      </div>
    </div>
  );
}
