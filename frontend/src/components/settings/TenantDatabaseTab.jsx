/**
 * TenantDatabaseTab — the "Database" tab shared by Administration -> Settings
 * (Organization scope) and Seller Portal -> Seller Settings (platform scope),
 * Bring-Your-Own-Database Phase 1. Same component, parametrized by which
 * `api` instance (org vs seller — see services/api/tenantDatabaseApi.js) and
 * permission map to use, rather than two near-identical hand-written tabs.
 *
 * Phase 1 scope only: this tab configures and REAL-Test-Connects the chosen
 * backend database (Postgres/MySQL/SQL Server), reusing the same credential
 * form (DynamicConnectionFields) and masked-secret convention the
 * Connections page already uses. It does NOT yet move any traffic there —
 * `is_active` stays false until a later phase's migration/cutover engine
 * exists (see tenant_database_service.py's module docstring on the
 * backend). That's disclosed here, not hidden, via the banner below.
 */
import { useEffect, useState } from 'react';
import { CheckCircle2, XCircle, Loader2, PlayCircle, Info, Lock, ArrowRightLeft, History } from 'lucide-react';
import { Button, StatusBadge, PermissionGate, Modal } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { DynamicConnectionFields } from '@/pages/connections/DynamicConnectionFields.jsx';
import { TenantMigrationLogDrawer } from '@/components/settings/TenantMigrationLogDrawer.jsx';
import '@/pages/connections/connections.css';
// This component renders on both administration/settings/Settings.jsx (which
// already loads this stylesheet) and seller/settings/SellerSettings.jsx
// (which uses its own sst-* prefixed classes instead) — imported directly
// here so the shared stg-* classes below are always available regardless
// of which page mounts it.
import '@/pages/administration/settings/settings.css';

const MASK = '••••••••';
const TYPES = ['POSTGRESQL', 'MYSQL', 'SQL_SERVER'];
const TYPE_LABELS = { POSTGRESQL: 'PostgreSQL', MYSQL: 'MySQL', SQL_SERVER: 'SQL Server' };
const MIGRATION_ACTIVE = new Set(['PENDING', 'PROVISIONING_SCHEMA', 'COPYING', 'VERIFYING', 'CUTTING_OVER']);
const MIGRATION_STATUS_VARIANT = {
  PENDING: 'gray', PROVISIONING_SCHEMA: 'info', COPYING: 'info', VERIFYING: 'info',
  CUTTING_OVER: 'warning', COMPLETED: 'success', FAILED: 'danger',
};

/** migrationApi is optional — only passed once a migration engine exists
 * for that scope (both org and Seller/platform now do, Phase 4 + 6).
 * isPlatform switches the copy below from "your organization" phrasing
 * to platform-wide phrasing when this tab is mounted from Seller
 * Settings — a platform migration moves every org's remaining shared
 * data (except orgs already on their own individual database), not one
 * org's data, and the wording should say so honestly rather than reuse
 * the org-scoped copy verbatim. */
export function TenantDatabaseTab({ api, editPermission, migrationApi, isPlatform = false }) {
  const [catalogue, setCatalogue] = useState(null);
  const [row, setRow] = useState(null); // null = not configured yet
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  const [connectionType, setConnectionType] = useState('POSTGRESQL');
  const [config, setConfig] = useState({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);

  const [migrations, setMigrations] = useState([]);
  const [migrationsLoading, setMigrationsLoading] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState('');
  const [activeJobId, setActiveJobId] = useState(null);

  const loadMigrations = () => {
    if (!migrationApi) return;
    setMigrationsLoading(true);
    migrationApi.list()
      .then((r) => setMigrations(r.items))
      .catch(() => {}) // history is a nice-to-have — a failed fetch shouldn't block the rest of the tab
      .finally(() => setMigrationsLoading(false));
  };
  useEffect(() => { loadMigrations(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const load = () => {
    setLoading(true);
    setLoadError('');
    Promise.all([api.getTypes(), api.get()])
      .then(([types, current]) => {
        setCatalogue(types.schemas);
        setRow(current);
        const type = current?.connection_type ?? 'POSTGRESQL';
        setConnectionType(type);
        const schema = types.schemas[type];
        const defaults = {};
        schema?.fields.forEach((f) => { if (f.default !== undefined) defaults[f.key] = f.default; });
        const initial = { ...defaults, ...(current?.config ?? {}) };
        schema?.fields.forEach((f) => { if (f.secret) initial[f.key] = ''; }); // never pre-fill secrets
        setConfig(initial);
      })
      .catch((err) => setLoadError(err?.message ?? 'Failed to load the Database settings.'))
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) return <div className="stg-card"><p style={{ color: 'var(--text-muted)' }}>Loading…</p></div>;
  if (loadError) return <div className="stg-card"><div className="stg-form-error">{loadError}</div></div>;

  const schema = catalogue?.[connectionType];
  const isSecretSet = (key) => row?.connection_type === connectionType && row?.config?.[key] === MASK;

  const changeType = (type) => {
    setConnectionType(type);
    const s = catalogue[type];
    const defaults = {};
    s.fields.forEach((f) => { if (f.default !== undefined) defaults[f.key] = f.default; });
    setConfig(defaults);
    setTestResult(null);
  };

  const buildSubmitConfig = () => {
    const out = {};
    schema.fields.forEach((f) => {
      if (f.secret) {
        if (config[f.key]) out[f.key] = config[f.key]; // only send if the user actually typed something new
      } else {
        out[f.key] = config[f.key];
      }
    });
    return out;
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveError('');
    setTestResult(null);
    try {
      const updated = await api.update({ connection_type: connectionType, config: buildSubmitConfig() });
      setRow(updated);
      const initial = { ...updated.config };
      schema.fields.forEach((f) => { if (f.secret) initial[f.key] = ''; });
      setConfig(initial);
    } catch (err) {
      setSaveError(err?.message ?? 'Could not save the database configuration.');
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await api.test();
      setTestResult(result);
      setRow((r) => (r ? { ...r, status: result.status, last_tested_at: result.tested_at, last_test_success: result.success, last_test_message: result.message } : r));
    } catch (err) {
      setTestResult({ success: false, message: err?.message ?? 'Test failed.' });
    } finally {
      setTesting(false);
    }
  };

  const handleStartMigration = async () => {
    setStarting(true);
    setStartError('');
    try {
      const job = await migrationApi.start();
      setShowConfirm(false);
      setActiveJobId(job.id);
      loadMigrations();
    } catch (err) {
      setStartError(err?.message ?? 'Could not start the migration.');
    } finally {
      setStarting(false);
    }
  };

  const handleDrawerClose = () => {
    setActiveJobId(null);
    loadMigrations();
    load(); // is_active may have just flipped — refresh the config/status panel too
  };

  const hasActiveMigration = migrations.some((m) => MIGRATION_ACTIVE.has(m.status));
  const canMigrate = !!migrationApi && row && row.last_test_success && !row.is_active && !hasActiveMigration;

  return (
    <>
    <div className="stg-card">
      <div className="stg-card-title">Database</div>
      <p className="stg-secret-note" style={{ alignItems: 'flex-start' }}>
        <Info size={13} style={{ marginTop: 1, flexShrink: 0 }} />
        <span>
          Bring your own PostgreSQL, MySQL, or SQL Server database. Configuring and testing one here does not move
          any data by itself{migrationApi ? ' — use Migrate Now below once Test Connection succeeds' : ' — migrating live traffic to it is a later phase'}.
          Right now saving only stores credentials and verifies real connectivity.
        </span>
      </p>

      <div className="conn-field-grid" style={{ marginBottom: 4 }}>
        <div className="conn-field">
          <label className="conn-field-label">Database Type</label>
          <select className="conn-input" value={connectionType} onChange={(e) => changeType(e.target.value)}>
            {TYPES.map((t) => <option key={t} value={t}>{TYPE_LABELS[t]}</option>)}
          </select>
        </div>
      </div>

      <div className="conn-form-divider" />

      {schema && (
        <DynamicConnectionFields
          fields={schema.fields}
          values={config}
          onChange={(key, val) => setConfig((c) => ({ ...c, [key]: val }))}
          editable
          isSecretSet={isSecretSet}
        />
      )}

      {row && (
        <div className="conn-status-row">
          <span style={{ fontSize: 'var(--fs-xs, 11px)', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.3px', fontWeight: 600 }}>Status</span>
          <StatusBadge variant={{ ACTIVE: 'success', ERROR: 'danger', MIGRATING: 'warning', UNTESTED: 'gray', UNCONFIGURED: 'gray' }[row.status] ?? 'gray'} dot>{row.status}</StatusBadge>
          {row.last_tested_at && <span className="text-muted text-sm">Last tested {formatDateTime(row.last_tested_at)}</span>}
          {row.secrets_rotated_at && <span className="text-muted text-sm">Credentials rotated {formatDateTime(row.secrets_rotated_at)}</span>}
          {row.is_active
            ? <span className="text-muted text-sm">Actively serving traffic</span>
            : <span className="text-muted text-sm">Not yet routing any traffic</span>}
        </div>
      )}

      {(testing || testResult) && (
        <div className={`conn-test-result ${testing ? 'testing' : testResult.success ? 'success' : 'failure'}`}>
          {testing ? (
            <><Loader2 size={16} className="conn-spin" /> Testing…</>
          ) : testResult.success ? (
            <>
              <CheckCircle2 size={16} /> Connection Successful — {testResult.message}
              {(testResult.latency_ms != null || testResult.server_version) && (
                <span className="conn-test-result-detail">
                  {testResult.latency_ms != null ? `(${testResult.latency_ms} ms` : '('}
                  {testResult.server_version ? `${testResult.latency_ms != null ? ', ' : ''}${testResult.server_version}` : ''})
                </span>
              )}
            </>
          ) : (
            <><XCircle size={16} /> Connection Failed — {testResult.message}</>
          )}
        </div>
      )}

      {saveError && <div className="stg-form-error" style={{ marginTop: 12 }}>{saveError}</div>}

      <div className="stg-save-bar">
        <PermissionGate {...editPermission} fallback={<p className="stg-readonly-note"><Lock size={12} /> You don't have permission to edit this section.</p>}>
          <div className="stg-save-actions">
            {row && (
              <Button variant="secondary" size="sm" icon={testing ? <Loader2 size={13} className="conn-spin" /> : <PlayCircle size={13} />} disabled={testing} onClick={handleTest}>
                {testing ? 'Testing…' : 'Test Connection'}
              </Button>
            )}
            <Button variant="primary" size="sm" loading={saving} onClick={handleSave}>Save</Button>
          </div>
        </PermissionGate>
      </div>

      {migrationApi && (
        <>
          <div className="conn-form-divider" />
          <div className="stg-card-title" style={{ fontSize: 'var(--fs-sm)' }}>Migration</div>

          {row?.is_active ? (
            <p className="text-muted text-sm">
              {isPlatform
                ? 'The platform is already running on the database configured above.'
                : 'This organization is already running on the database configured above.'}
            </p>
          ) : (
            <div className="conn-status-row" style={{ marginBottom: 8 }}>
              <span className="text-muted text-sm">
                {!row ? 'Configure and Test Connection above before migrating.'
                  : !row.last_test_success ? 'Test Connection must succeed before a migration can start.'
                  : hasActiveMigration ? 'A migration is already in progress — see below.'
                  : isPlatform
                    ? 'Ready to migrate — this moves the platform’s remaining shared data to the database above.'
                    : 'Ready to migrate — this moves your existing data to the database above.'}
              </span>
            </div>
          )}

          {startError && <div className="stg-form-error" style={{ marginBottom: 8 }}>{startError}</div>}

          <PermissionGate {...editPermission} fallback={null}>
            {!row?.is_active && (
              <Button variant="primary" size="sm" icon={<ArrowRightLeft size={13} />} disabled={!canMigrate} onClick={() => setShowConfirm(true)}>
                Migrate Now
              </Button>
            )}
          </PermissionGate>

          {migrations.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
                <History size={13} style={{ color: 'var(--text-muted)' }} />
                <span style={{ fontSize: 'var(--fs-xs, 11px)', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.3px', fontWeight: 600 }}>
                  Migration History
                </span>
              </div>
              <table className="stg-table">
                <thead>
                  <tr><th>Status</th><th>Progress</th><th>Started</th><th>Completed</th><th /></tr>
                </thead>
                <tbody>
                  {migrations.map((m) => (
                    <tr key={m.id}>
                      <td><StatusBadge variant={MIGRATION_STATUS_VARIANT[m.status] ?? 'gray'} dot>{m.status}</StatusBadge></td>
                      <td className="text-muted text-sm">{m.tables_done}/{m.tables_total} tables</td>
                      <td className="text-muted text-sm">{m.started_at ? formatDateTime(m.started_at) : '—'}</td>
                      <td className="text-muted text-sm">{m.completed_at ? formatDateTime(m.completed_at) : '—'}</td>
                      <td><Button variant="ghost" size="sm" onClick={() => setActiveJobId(m.id)}>View Log</Button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {migrationsLoading && migrations.length === 0 && <p className="text-muted text-sm">Loading migration history…</p>}
        </>
      )}
    </div>

    <Modal
      open={showConfirm}
      onClose={() => (starting ? null : setShowConfirm(false))}
      title="Migrate to your own database?"
      size="sm"
      footer={(
        <>
          <Button variant="secondary" size="sm" disabled={starting} onClick={() => setShowConfirm(false)}>Cancel</Button>
          <Button variant="primary" size="sm" loading={starting} onClick={handleStartMigration}>Start Migration</Button>
        </>
      )}
    >
      <p style={{ marginBottom: 10 }}>
        {isPlatform
          ? <>This copies the platform's remaining shared data — every organization that doesn't already have its own
              individual database — to the {TYPE_LABELS[row?.connection_type] ?? 'configured'} database above, verifies every
              row copied correctly, then cuts over.</>
          : <>This copies your organization's existing data to the {TYPE_LABELS[row?.connection_type] ?? 'configured'} database above,
              verifies every row copied correctly, then cuts over.</>}
      </p>
      <p style={{ marginBottom: 10 }}>
        <strong>Writes will briefly pause</strong> (typically seconds) during the final cutover step while the last few
        changes are copied over — reads keep working the whole time. There's no other downtime.
        {isPlatform && ' This pause applies to every organization without its own individual database.'}
      </p>
      <p className="text-muted text-sm">
        If anything goes wrong before cutover, nothing changes for {isPlatform ? 'the platform' : 'your organization'} — it's
        safe to retry. You can watch live progress after starting.
      </p>
    </Modal>

    {activeJobId != null && (
      <TenantMigrationLogDrawer api={migrationApi} jobId={activeJobId} isPlatform={isPlatform} onClose={handleDrawerClose} />
    )}
    </>
  );
}
